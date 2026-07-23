"""Execution-related tool handlers for MCP server.

This module contains handlers for seed execution:
- ExecuteSeedHandler: Synchronous seed execution
- StartExecuteSeedHandler: Asynchronous (background) seed execution with job tracking
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import inspect
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError as PydanticValidationError
from rich.console import Console
import structlog
import yaml

from ouroboros.config._model_defaults import DEFAULT_SONNET_MODEL
from ouroboros.config.loader import get_auto_evaluate_enabled, get_max_parallel_workers
from ouroboros.core.conductor import (
    ConductorDirective,
    validate_conductor_successor_authorization,
)
from ouroboros.core.errors import ConfigError, ValidationError
from ouroboros.core.execution_preferences import (
    ResolvedExecutionPreferences,
    execution_preferences_from_contract,
    resolve_execution_preferences,
)
from ouroboros.core.project_paths import resolve_seed_project_path
from ouroboros.core.security import InputValidator
from ouroboros.core.seed import Seed, ac_texts
from ouroboros.core.types import Result
from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    maybe_prepare_task_workspace,
    maybe_restore_task_workspace,
    release_lock,
)
from ouroboros.evaluation.verification_artifacts import build_verification_artifacts
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.tools._dashboard import resolve_dashboard_run_url
from ouroboros.mcp.tools.background import start_background_tool_job
from ouroboros.mcp.tools.bridge_mixin import BridgeAwareMixin
from ouroboros.mcp.tools.job_observer import build_job_observer_contract
from ouroboros.mcp.tools.subagent import (
    DELEGATED_TO_PLUGIN,
    DELEGATED_TO_SUBAGENT,
    build_execute_subagent,
    build_subagent_result,
    dispatch_plugin_terminal,
    should_dispatch_via_plugin,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator import create_agent_runtime
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_CWD_ARG,
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
    DELEGATED_PARENT_SESSION_ID_ARG,
    DELEGATED_PARENT_TRANSCRIPT_PATH_ARG,
    RuntimeHandle,
)
from ouroboros.orchestrator.execution_authority import (
    _has_live_process_local_authority_registration,
    _register_process_local_authority_terminal_finalizer,
    _retire_process_local_authority_after_terminal_persistence,
    valid_process_local_authority_contract,
)
from ouroboros.orchestrator.heartbeat import is_holder_alive
from ouroboros.orchestrator.model_routing import tier_from_model_tier_arg
from ouroboros.orchestrator.runner import (
    OrchestratorRunner,
    clear_cancellation,
    request_cancellation,
)
from ouroboros.orchestrator.session import SessionRepository, SessionStatus, SessionTracker
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore
from ouroboros.providers.base import LLMAdapter

log = structlog.get_logger(__name__)


_PROCESS_LOCAL_BACKGROUND_OWNED_BLOCKS = frozenset(
    {
        "process_local_execution_in_progress",
        "process_local_authority_held_elsewhere",
        "session_not_paused",
        "typed_evidence_gate_required",
        "investment_authority_required",
        "cancellation_persistence_pending",
        "pause_persistence_pending",
        "terminal_persistence_pending",
        # Durable reconstruction was inconclusive. The retained process-local
        # owner remains the only safe retry path; the background wrapper must
        # not manufacture a generic FAILED terminal event.
        "process_local_reconstruction_pending",
        # This error path already conditionally persisted its own terminal
        # result (or observed a concurrent terminal winner). A background
        # wrapper must never append a second FAILED event over that outcome.
        "process_local_resume_unavailable",
    }
)


def _is_process_local_background_owned_error(error: object) -> bool:
    """Return whether a runner error already owns its session lifecycle result.

    Most of these typed results intentionally leave a process-local owner live;
    ``process_local_resume_unavailable`` instead conditionally persists (or
    observes) a terminal outcome itself.  In both cases the background MCP
    wrapper must preserve the runner-owned lifecycle rather than append an
    unconditional ``mark_failed``.
    """
    details = getattr(error, "details", None)
    return isinstance(details, Mapping) and (
        details.get("resume_blocked") in _PROCESS_LOCAL_BACKGROUND_OWNED_BLOCKS
    )


def _process_local_authority_is_held_elsewhere(tracker: SessionTracker) -> bool:
    """Check non-owning handler preflight before it touches a task worktree.

    A handler can only resume a process-local session through its retained exact
    runner.  If this handler has no such owner but the registry or PID lease is
    still live, fail with the typed ownership result before a task-worktree lock
    can obscure the authority outcome with a generic workspace error.
    """
    raw_contract = tracker.progress.get("execution_contract")
    if not isinstance(raw_contract, Mapping):
        return False
    authority = raw_contract.get("foundation_a_authority")
    if not valid_process_local_authority_contract(authority):
        return False
    return _has_live_process_local_authority_registration(
        tracker.session_id,
        tracker.execution_id,
        authority,
    ) or is_holder_alive(tracker.session_id)


def _process_local_resume_block_error(
    tracker: SessionTracker,
    *,
    resume_blocked: str,
) -> MCPToolError:
    """Build a typed, retryable MCP result for a live process-local owner."""
    if resume_blocked == "process_local_execution_in_progress":
        message = "This process-local session is already being resumed"
    else:
        message = "This process-local session is held by another live owner"
    return MCPToolError(
        message,
        tool_name="ouroboros_execute_seed",
        is_retriable=True,
        details={
            "session_id": tracker.session_id,
            "execution_id": tracker.execution_id,
            "resume_blocked": resume_blocked,
        },
    )


def _resolve_execution_model(runtime_backend: str | None) -> str | None:
    """Resolve the model pin for agent-runtime execution tasks.

    ``OUROBOROS_EXECUTION_MODEL`` is already honored by the MCP evolution
    executor. Keep execute-seed and auto run-handoff aligned so CLI-backed
    runtimes such as Pi can be smoke-tested against an explicitly authenticated
    provider/model without changing their global defaults.
    """
    execution_model = os.environ.get("OUROBOROS_EXECUTION_MODEL")
    if execution_model is not None:
        stripped = execution_model.strip()
        return stripped or None
    if runtime_backend == "claude":
        return DEFAULT_SONNET_MODEL
    return None


def _resolve_model_tier_request(
    arguments: Mapping[str, Any],
    *,
    is_resume: bool,
) -> tuple[str, str | None, str | None]:
    """Resolve public value, runner override, and delegated round-trip value.

    A fresh omitted request uses the public ``medium`` default and must therefore
    pin the run's base tier to ``standard``. An omitted resume is different: it
    means restore the persisted routing contract, so neither the runner nor a
    delegated plugin payload may materialize a new explicit ``medium`` override.
    """
    requested = arguments.get("model_tier")
    effective = requested if isinstance(requested, str) and requested else "medium"
    should_override = requested is not None or not is_resume
    return (
        effective,
        tier_from_model_tier_arg(effective) if should_override else None,
        effective if should_override else None,
    )


def _resolve_execution_preferences_request(
    arguments: Mapping[str, Any],
    *,
    is_resume: bool,
) -> tuple[ResolvedExecutionPreferences, str | None, str | None]:
    """Resolve public preferences while preserving resume immutability.

    Fresh runs apply the documented adaptive/observe or quality-first/off
    default mapping. A resume restores the persisted execution contract; an
    intentional preference change must start a successor execution instead of
    mutating the active or historical run.
    """
    raw_efficiency_value = arguments.get("efficiency_mode")
    raw_assurance_value = arguments.get("frugality_assurance")
    efficiency_supplied = raw_efficiency_value is not None and raw_efficiency_value != ""
    assurance_supplied = raw_assurance_value is not None and raw_assurance_value != ""
    if is_resume and (efficiency_supplied or assurance_supplied):
        raise ValueError(
            "efficiency_mode and frugality_assurance cannot be changed on resume; "
            "start a new successor execution for an intentional change"
        )
    raw_efficiency = raw_efficiency_value if efficiency_supplied else None
    raw_assurance = raw_assurance_value if assurance_supplied else None
    if raw_efficiency is not None and not isinstance(raw_efficiency, str):
        raise ValueError("efficiency_mode must be adaptive or quality_first")
    if raw_assurance is not None and not isinstance(raw_assurance, str):
        raise ValueError("frugality_assurance must be off, observe, or strict")
    preferences = resolve_execution_preferences(raw_efficiency, raw_assurance)
    return (
        preferences,
        raw_efficiency if isinstance(raw_efficiency, str) else None,
        raw_assurance if isinstance(raw_assurance, str) else None,
    )


def _persisted_execution_preferences(progress: Mapping[str, Any]) -> ResolvedExecutionPreferences:
    """Read the immutable preference contract used by a resumed session."""
    raw_contract = progress.get("execution_contract")
    if raw_contract is None:
        return resolve_execution_preferences(None, None)
    if not isinstance(raw_contract, Mapping):
        raise ValueError("Persisted execution_contract is invalid")
    raw_preferences = raw_contract.get("execution_preferences")
    preferences = execution_preferences_from_contract(raw_preferences)
    if preferences is None:
        if raw_preferences is None:
            return resolve_execution_preferences(None, None)
        raise ValueError("Persisted execution preferences are invalid")
    return preferences


async def _prepare_conductor_successor_seed(
    *,
    arguments: Mapping[str, Any],
    seed_content: str,
    event_store: EventStore | None,
    tool_name: str,
    is_resume: bool,
) -> Result[str, MCPToolError]:
    """Validate an audited successor receipt and embed its bounded directive."""
    try:
        parsed = yaml.safe_load(seed_content)
    except yaml.YAMLError as exc:
        return Result.err(MCPToolError(f"Failed to parse seed YAML: {exc}", tool_name=tool_name))
    if not isinstance(parsed, dict):
        return Result.err(MCPToolError("Seed YAML must be a mapping", tool_name=tool_name))

    raw_argument_directive = arguments.get("conductor_directive")
    raw_seed_directive = parsed.get("conductor_directive")
    raw_directive = (
        raw_argument_directive if raw_argument_directive is not None else raw_seed_directive
    )
    if raw_directive is None:
        return Result.ok(seed_content)
    if is_resume:
        return Result.err(
            MCPToolError(
                "conductor_directive starts a successor execution and cannot be injected on resume",
                tool_name=tool_name,
            )
        )
    if not isinstance(raw_directive, Mapping):
        return Result.err(
            MCPToolError("conductor_directive must be an object", tool_name=tool_name)
        )

    decision_id = arguments.get("conductor_decision_id")
    predecessor_execution_id = arguments.get("predecessor_execution_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        return Result.err(
            MCPToolError(
                "conductor_decision_id is required for a successor directive",
                tool_name=tool_name,
            )
        )
    if not isinstance(predecessor_execution_id, str) or not predecessor_execution_id.strip():
        return Result.err(
            MCPToolError(
                "predecessor_execution_id is required for a successor directive",
                tool_name=tool_name,
            )
        )
    try:
        directive = ConductorDirective.from_mapping(raw_directive)
    except (TypeError, ValueError) as exc:
        return Result.err(MCPToolError(str(exc), tool_name=tool_name))

    store = event_store or EventStore()
    owns_store = event_store is None
    try:
        await store.initialize()
        events = await store.replay("conductor_decision", decision_id.strip())
    finally:
        if owns_store:
            await store.close()
    selected = next(
        (event for event in events if event.type == "conductor.decision.selected"), None
    )
    if selected is None:
        return Result.err(
            MCPToolError(
                "No selected conductor decision receipt matches conductor_decision_id",
                tool_name=tool_name,
            )
        )
    try:
        validate_conductor_successor_authorization(
            selected.data,
            directive=directive,
            predecessor_execution_id=predecessor_execution_id.strip(),
        )
    except (TypeError, ValueError) as exc:
        return Result.err(MCPToolError(str(exc), tool_name=tool_name))

    parsed["conductor_directive"] = directive.to_event_data()
    parsed["conductor_decision_id"] = decision_id.strip()
    parsed["predecessor_execution_id"] = predecessor_execution_id.strip()
    return Result.ok(
        yaml.safe_dump(parsed, allow_unicode=True, sort_keys=False, default_flow_style=False)
    )


def _parse_seed_yaml_for_execution_mode(
    seed_content: str,
    *,
    tool_name: str,
) -> Result[tuple[Any, Any], MCPToolError]:
    """Parse seed YAML enough to apply the shared execution-mode gate."""
    try:
        seed_dict = yaml.safe_load(seed_content)
    except yaml.YAMLError as e:
        log.error("mcp.tool.execute_seed.yaml_error", error=str(e))
        return Result.err(
            MCPToolError(
                f"Failed to parse seed YAML: {e}",
                tool_name=tool_name,
            )
        )

    execution_mode = (
        seed_dict.get("orchestrator", {}).get("execution_mode")
        if isinstance(seed_dict, dict) and isinstance(seed_dict.get("orchestrator"), dict)
        else None
    )
    return Result.ok((seed_dict, execution_mode))


def _validate_fresh_execution_mode(
    execution_mode: Any,
    *,
    tool_name: str,
) -> Result[None, MCPToolError]:
    """Reject unknown fresh execution-mode selectors on every MCP path.

    ``legacy`` was removed after #978 P5 while the default runner was
    observe-mode (the opt-out token was redundant then). With verify-by-default
    the default runner enforces typed evidence plus verifier PASS acceptance,
    so ``legacy`` is re-admitted as the explicit opt-out — mirroring the CLI
    ``_resolve_fat_harness_mode`` contract.
    """
    if execution_mode not in (None, "", "fat_harness", "legacy"):
        return Result.err(
            MCPToolError(
                "seed.orchestrator.execution_mode must be 'fat_harness' or 'legacy' "
                f"when set (got {execution_mode!r}).",
                tool_name=tool_name,
            )
        )
    return Result.ok(None)


def _fresh_fat_harness_mode(execution_mode: Any) -> bool:
    """Verify-by-default resolution shared with the CLI.

    Missing/blank selector OR explicit ``fat_harness`` → True; explicit
    ``legacy`` is the supported opt-out → False.
    """
    return execution_mode != "legacy"


def _plugin_fat_harness_downgrade_meta(execution_mode: Any) -> dict[str, str]:
    """Stamp the verify-by-default downgrade for fresh plugin dispatch.

    Plugin dispatch cannot enforce typed evidence plus verifier PASS in the
    child task. Explicit ``fat_harness`` requests are rejected upstream and an
    explicit ``legacy`` opted out; only the missing/blank selector — where the
    default would have enabled fat-harness — is silently downgraded, so it must
    carry a visible note in the response meta.
    """
    if execution_mode in (None, ""):
        return {"fat_harness_downgraded": "plugin_dispatch_cannot_enforce"}
    return {}


def _validate_plugin_execution_mode(
    execution_mode: Any,
    *,
    tool_name: str,
) -> Result[None, MCPToolError]:
    """Reject acceptance modes plugin dispatch cannot enforce."""
    if execution_mode == "fat_harness":
        return Result.err(
            MCPToolError(
                "seed.orchestrator.execution_mode='fat_harness' is not supported in "
                "OpenCode plugin dispatch because the child task cannot enforce typed "
                "evidence plus verifier PASS acceptance. Run without plugin dispatch or "
                "omit the selector for the default runner.",
                tool_name=tool_name,
            )
        )
    return Result.ok(None)


async def _validate_plugin_resume_acceptance_contract(
    *,
    event_store: EventStore | None,
    execution_mode: Any,
    session_id: str | None,
    tool_name: str,
) -> Result[None, MCPToolError]:
    """Reject plugin resumes the delegated child cannot safely restore.

    Plugin dispatch can start a fresh child, but it does not instantiate the
    in-process runner that validates and restores the immutable execution
    contract (Seed semantics, backend, workspace, constructor model, and routing
    policy). Allowing any existing session through this branch would let a new
    payload silently replace those identities. Keep the older fat-harness errors
    specific, then fail closed for every other plugin resume as well.
    """
    if not session_id:
        return Result.ok(None)

    store = event_store or EventStore()
    owns_store = event_store is None
    try:
        await store.initialize()
        tracker_result = await SessionRepository(store).reconstruct_session(session_id)
        if tracker_result.is_err:
            return Result.err(
                MCPToolError(
                    f"Session resume failed: {tracker_result.error.message}",
                    tool_name=tool_name,
                )
            )
        return _validate_plugin_resume_tracker_acceptance_contract(
            tracker=tracker_result.value,
            execution_mode=execution_mode,
            tool_name=tool_name,
        )
    finally:
        if owns_store:
            await store.close()


def _validate_plugin_resume_tracker_acceptance_contract(
    *,
    tracker: SessionTracker,
    execution_mode: Any,
    tool_name: str,
) -> Result[None, MCPToolError]:
    """Apply the plugin-resume acceptance gate to an already reconstructed session."""
    persisted_fat_harness_mode = tracker.progress.get("fat_harness_mode")
    if persisted_fat_harness_mode is True:
        return Result.err(
            MCPToolError(
                "OpenCode plugin dispatch cannot resume sessions created with "
                "fat_harness_mode=True because the child task cannot enforce typed "
                "evidence plus verifier PASS acceptance. Resume without plugin dispatch.",
                tool_name=tool_name,
            )
        )
    if execution_mode == "fat_harness" and not isinstance(persisted_fat_harness_mode, bool):
        return Result.err(
            MCPToolError(
                "OpenCode plugin dispatch cannot resume sessions whose seed requests "
                "execution_mode='fat_harness' without a persisted fat_harness_mode "
                "contract because the child task cannot enforce typed evidence plus "
                "verifier PASS acceptance. Resume without plugin dispatch.",
                tool_name=tool_name,
            )
        )
    return Result.err(
        MCPToolError(
            "OpenCode plugin dispatch cannot safely resume an existing execute_seed "
            "session because the delegated child cannot restore and verify the "
            "session's immutable execution contract (Seed, backend, workspace, and "
            "model routing). Resume without plugin dispatch or start a new session.",
            tool_name=tool_name,
        )
    )


def _pause_metadata_from_progress(progress: dict[str, Any]) -> dict[str, Any]:
    """Extract pause metadata safe to expose in MCP tool results."""
    metadata: dict[str, Any] = {}
    for key in ("pause_kind", "pause_seconds", "resume_after", "resume_hint", "paused_at"):
        value = progress.get(key)
        if value is not None:
            metadata[key] = value
    reason = progress.get("pause_reason")
    if reason is not None:
        metadata["pause_reason"] = reason
    return metadata


def _classify_synchronous_execution_status(
    session_status: SessionStatus | None,
) -> tuple[str, bool | None, bool, str]:
    """Map reconstructed session status to MCP tool-result semantics."""
    if session_status == SessionStatus.COMPLETED:
        return "completed", True, False, "Seed Execution COMPLETED"
    if session_status == SessionStatus.PAUSED:
        return "paused", None, False, "Seed Execution PAUSED"
    if session_status in {SessionStatus.FAILED, SessionStatus.CANCELLED}:
        return session_status.value, False, True, "Seed Execution FINISHED"
    return "unknown", False, True, "Seed Execution FINISHED"


def _run_only_verification_meta(
    session_id: str | None,
    *,
    verification_status: str = "executed_unverified",
) -> dict[str, Any]:
    """Expose that execute_seed completion is not formal 3-stage verification."""
    next_step = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    return {
        "evaluated": False,
        "verification_status": verification_status,
        "formal_evaluation_required": True,
        "next_step": next_step,
    }


def _run_only_verification_text(
    session_id: str | None,
    *,
    verification_status: str = "executed_unverified",
) -> str:
    """Render the run-only verification warning for human-readable tool output."""
    next_step = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    return (
        f"Verification Status: {verification_status}\n"
        "Formal Evaluation: NOT evaluated by the 3-stage evaluator\n"
        "Warning: execution results are run-only and must not be treated as verified.\n"
        f"Next: {next_step}\n"
    )


def resolve_auto_evaluate(config_flag: bool, per_call_override: bool | None) -> bool:
    """Resolve execute_seed auto-evaluation from config plus per-call override."""
    if isinstance(per_call_override, bool):
        return per_call_override
    return config_flag


def _run_succeeded(result: MCPToolResult) -> bool:
    """Return True when a synchronous execute_seed result reached successful completion."""
    if result.is_error:
        return False
    return result.meta.get("success") is True


def _result_session_id(result: MCPToolResult, fallback: str | None) -> str | None:
    session_id = result.meta.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else fallback


def _result_evaluation_working_dir(result: MCPToolResult, fallback: Path) -> Path:
    worktree_path = result.meta.get("worktree_path")
    if isinstance(worktree_path, str) and worktree_path:
        return Path(worktree_path)
    return fallback


def _append_result_text(
    result: MCPToolResult,
    text: str,
    *,
    meta: dict[str, Any],
) -> MCPToolResult:
    return MCPToolResult(
        content=(
            *result.content,
            MCPContentItem(type=ContentType.TEXT, text=text),
        ),
        is_error=result.is_error,
        meta=meta,
        structured_content=result.structured_content,
    )


def _evaluation_enqueued_meta(
    run_result: MCPToolResult,
    *,
    session_id: str | None,
    evaluation_job_id: str,
) -> dict[str, Any]:
    retry_step = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    return {
        **run_result.meta,
        **_run_only_verification_meta(
            session_id,
            verification_status="evaluation_enqueued",
        ),
        "chained_evaluate_job_id": evaluation_job_id,
        "evaluation_status": "enqueued",
        "next_step": (
            f"ouroboros_job_wait {evaluation_job_id}, then ouroboros_job_result {evaluation_job_id}"
        ),
        "manual_retry_next_step": retry_step,
    }


def _evaluation_enqueued_text(session_id: str | None, evaluation_job_id: str) -> str:
    retry_step = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    return (
        "\nFormal Evaluation: queued as a bounded background job\n"
        f"Chained Evaluation Job ID: {evaluation_job_id}\n"
        f"Next: poll ouroboros_job_wait(job_id={evaluation_job_id}) and "
        f"then ouroboros_job_result(job_id={evaluation_job_id}).\n"
        f"Manual Retry: {retry_step}\n"
    )


def _evaluation_enqueue_failed_meta(
    run_result: MCPToolResult,
    *,
    session_id: str | None,
    error: str,
) -> dict[str, Any]:
    retry_step = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    meta = dict(run_result.meta)
    if "verification_status" not in meta:
        meta.update(_run_only_verification_meta(session_id))
    meta.update(
        {
            "evaluation_status": "enqueue_failed",
            "evaluation_error": error[:1000],
            "next_step": retry_step,
        }
    )
    return meta


def _evaluation_enqueue_failed_text(session_id: str | None, error: str) -> str:
    retry_step = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    return (
        "\nFormal Evaluation: enqueue failed; run result remains successful.\n"
        f"Evaluation Error: {error[:1000]}\n"
        f"Next: {retry_step}\n"
    )


def _plugin_verification_meta(
    session_id: str | None,
    *,
    auto_evaluate: bool,
) -> dict[str, Any]:
    if not auto_evaluate:
        return _run_only_verification_meta(
            session_id,
            verification_status="delegated_unverified",
        )
    retry_step = f"ooo evaluate {session_id}" if session_id else "ooo evaluate <session_id>"
    return {
        **_run_only_verification_meta(
            session_id,
            verification_status="evaluation_delegated",
        ),
        "evaluation_status": "delegated_to_plugin",
        "formal_evaluation_delegated": True,
        "next_step": "wait for delegated plugin task to complete formal evaluation",
        "manual_retry_next_step": retry_step,
    }


# ---------------------------------------------------------------------------
# Delegation context extraction
# ---------------------------------------------------------------------------


def _extract_inherited_runtime_handle(arguments: dict[str, Any]) -> RuntimeHandle | None:
    """Build a forkable parent runtime handle from internal delegated tool arguments.

    When a parent Claude session delegates to execute_seed via MCP, the
    pre-tool-use hook injects hidden ``_ooo_parent_*`` keys.  This function
    reconstitutes those into a RuntimeHandle the child runner can fork from.
    """
    session_id = arguments.get(DELEGATED_PARENT_SESSION_ID_ARG)
    if not isinstance(session_id, str) or not session_id:
        return None

    transcript_path = arguments.get(DELEGATED_PARENT_TRANSCRIPT_PATH_ARG)
    cwd = arguments.get(DELEGATED_PARENT_CWD_ARG)
    permission_mode = arguments.get(DELEGATED_PARENT_PERMISSION_MODE_ARG)

    return RuntimeHandle(
        backend="claude",
        native_session_id=session_id,
        transcript_path=transcript_path if isinstance(transcript_path, str) else None,
        cwd=cwd if isinstance(cwd, str) else None,
        approval_mode=permission_mode if isinstance(permission_mode, str) else None,
        metadata={"fork_session": True},
    )


def _extract_inherited_effective_tools(arguments: dict[str, Any]) -> list[str] | None:
    """Extract the parent effective tool set from internal delegated tool arguments."""
    tools = arguments.get(DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG)
    if not isinstance(tools, list):
        return None
    inherited = [t for t in tools if isinstance(t, str) and t]
    return inherited or None


@dataclass
class ExecuteSeedHandler(BridgeAwareMixin):
    """Handler for the execute_seed tool.

    Executes a seed (task specification) in the Ouroboros system.
    This is the primary entry point for running tasks.
    """

    event_store: EventStore | None = field(default=None, repr=False)
    llm_adapter: LLMAdapter | None = field(default=None, repr=False)
    llm_backend: str | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)
    session_signal_hub: Any | None = field(default=None, repr=False)
    _background_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    _process_local_resume_owners: dict[str, OrchestratorRunner] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _process_local_owned_event_stores: dict[str, EventStore] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _process_local_resume_handoffs: set[str] = field(
        default_factory=set,
        init=False,
        repr=False,
    )

    async def _retained_process_local_owner(
        self,
        tracker: SessionTracker,
    ) -> OrchestratorRunner | None:
        """Return the exact in-memory runner that still owns a paused session.

        This is an opaque owner handoff, not authority reconstruction: the
        original runner retains the registry-minted capability and still has to
        claim it before doing effects. A fresh MCP request may select that
        retained object, but can never manufacture a runner, adapter, or
        capability from durable correlation data.
        """
        owner = self._process_local_resume_owners.get(tracker.session_id)
        if owner is None:
            return None
        raw_contract = tracker.progress.get("execution_contract")
        if owner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            raw_contract,
        ):
            return owner
        owned_event_store: EventStore | None = None
        if self._process_local_resume_owners.get(tracker.session_id) is owner:
            self._process_local_resume_owners.pop(tracker.session_id, None)
            owned_event_store = self._process_local_owned_event_stores.pop(
                tracker.session_id,
                None,
            )
        if owned_event_store is not None:
            # The capability is already gone, so no terminal resume path can
            # reach this handler-owned store. Close it here rather than losing
            # the only reference while evicting the stale retained owner.
            try:
                close_result = owned_event_store.close()
                if inspect.isawaitable(close_result):
                    await close_result
            except Exception:
                log.exception("mcp.tool.execute_seed.event_store_close_error")
        return None

    def _begin_process_local_resume_handoff(self, session_id: str) -> bool:
        """Reserve same-handler worktree restoration for one retained owner."""
        if session_id in self._process_local_resume_handoffs:
            return False
        self._process_local_resume_handoffs.add(session_id)
        return True

    def _finish_process_local_resume_handoff(self, session_id: str) -> None:
        """Release the handler-local pre-resume gate after the runner settles."""
        self._process_local_resume_handoffs.discard(session_id)

    @staticmethod
    def _release_runner_task_workspace(
        runner: object,
        workspace: TaskWorkspace,
        *,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Release through the runner lifecycle boundary when it is available.

        Lightweight test/runtime doubles may not implement the private runner
        boundary.  The fallback preserves the legacy single-owner behavior for
        those doubles; production runners always take the idempotent,
        identity-aware path.
        """
        release_workspace = getattr(runner, "_release_task_workspace_for_identity", None)
        if callable(release_workspace):
            release_workspace(
                session_id=session_id,
                execution_id=execution_id,
            )
            return
        release_lock(workspace.lock_path)

    async def _release_terminal_process_local_owner(
        self,
        *,
        session_id: str,
        runner: OrchestratorRunner,
    ) -> None:
        """Drop this handler's retained references after a terminal transition."""
        if self._process_local_resume_owners.get(session_id) is not runner:
            return
        self._process_local_resume_owners.pop(session_id, None)
        owned_event_store = self._process_local_owned_event_stores.pop(session_id, None)
        self._process_local_resume_handoffs.discard(session_id)
        if owned_event_store is not None:
            try:
                close_result = owned_event_store.close()
                if inspect.isawaitable(close_result):
                    await close_result
            except Exception:
                log.exception("mcp.tool.execute_seed.event_store_close_error")

    async def _cleanup_unstarted_process_local_background_task(
        self,
        *,
        tracker: SessionTracker,
        runner: OrchestratorRunner,
        workspace: TaskWorkspace | None,
        owned_event_store: EventStore | None,
        retained_resume_handoff: bool,
        workspace_reservation: object | None = None,
    ) -> None:
        """Close a task cancelled before its coroutine can enter ``finally``.

        ``asyncio`` never executes a coroutine's body when its task is
        cancelled before the first scheduling turn.  A freshly prepared
        process-local session has already registered its capability, lease,
        handler owner, and possibly EventStore by that point, so the done
        callback must create an explicit terminal cleanup task instead of only
        discarding the cancelled task reference.
        """
        try:
            try:
                terminalization_error = await runner._mark_process_local_resume_unavailable(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
            except Exception:
                log.exception(
                    "mcp.tool.execute_seed.unstarted_background_terminalization_failed",
                    session_id=tracker.session_id,
                )
                return

            if (
                terminalization_error.details.get("resume_blocked")
                == "terminal_persistence_pending"
            ):
                log.warning(
                    "mcp.tool.execute_seed.unstarted_background_terminal_persistence_pending",
                    session_id=tracker.session_id,
                )
                return

            try:
                reconstructed = await runner.session_repo.reconstruct_session(tracker.session_id)
            except Exception:
                log.exception(
                    "mcp.tool.execute_seed.unstarted_background_terminal_reconstruct_failed",
                    session_id=tracker.session_id,
                )
                return
            if reconstructed.is_err or reconstructed.value.status not in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }:
                log.warning(
                    "mcp.tool.execute_seed.unstarted_background_terminal_unproven",
                    session_id=tracker.session_id,
                )
                return

            retained_store = self._process_local_owned_event_stores.get(tracker.session_id)
            try:
                await self._reconcile_terminal_process_local_owner(reconstructed.value)
            except Exception:
                log.exception(
                    "mcp.tool.execute_seed.unstarted_background_owner_cleanup_failed",
                    session_id=tracker.session_id,
                )
                return
            if owned_event_store is not None and owned_event_store is not retained_store:
                try:
                    close_result = owned_event_store.close()
                    if inspect.isawaitable(close_result):
                        await close_result
                except Exception:
                    log.exception("mcp.tool.execute_seed.event_store_close_error")
        finally:
            runner._release_task_workspace_reservation(workspace_reservation)
            if workspace is not None:
                self._release_runner_task_workspace(
                    runner,
                    workspace,
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
            if retained_resume_handoff:
                self._finish_process_local_resume_handoff(tracker.session_id)

    async def _reconcile_terminal_process_local_owner(self, tracker: SessionTracker) -> None:
        """Reconcile a terminal record through the registry-owned transition.

        This is a defensive fallback for terminal records written outside this
        handler.  The registry drains every registered runner/handler finalizer
        together; a still-active effect claim is only signalled because it owns
        its orderly terminal cleanup.
        """
        raw_contract = tracker.progress.get("execution_contract")
        authority = (
            raw_contract.get("foundation_a_authority")
            if isinstance(raw_contract, Mapping)
            else None
        )
        retired, claimed = await _retire_process_local_authority_after_terminal_persistence(
            tracker.session_id,
            tracker.execution_id,
            authority,
        )
        if claimed:
            await request_cancellation(tracker.session_id)
            return
        if retired:
            await clear_cancellation(tracker.session_id)
            return

        owner = self._process_local_resume_owners.get(tracker.session_id)
        if owner is None:
            return
        if owner.active_sessions.get(tracker.execution_id) == tracker.session_id:
            await request_cancellation(tracker.session_id)
            return
        owner._retire_process_local_authority(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        owner._unregister_session(tracker.execution_id, tracker.session_id)
        if owner._task_workspace is not None:
            self._release_runner_task_workspace(
                owner,
                owner._task_workspace,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
        await clear_cancellation(tracker.session_id)
        await self._release_terminal_process_local_owner(
            session_id=tracker.session_id,
            runner=owner,
        )

    def _remember_process_local_owner(
        self,
        tracker: SessionTracker,
        runner: OrchestratorRunner,
        *,
        owned_event_store: EventStore | None = None,
    ) -> None:
        """Keep a callable owner alive while its process-local lease is live."""
        raw_contract = tracker.progress.get("execution_contract")
        self._remember_process_local_owner_identity(
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
            raw_contract=raw_contract,
            runner=runner,
            owned_event_store=owned_event_store,
        )

    def _remember_process_local_owner_identity(
        self,
        *,
        session_id: str,
        execution_id: str,
        raw_contract: object,
        runner: OrchestratorRunner,
        owned_event_store: EventStore | None = None,
    ) -> bool:
        """Retain an exact owner even when durable reconstruction is unavailable."""
        if runner._has_live_process_local_authority(
            session_id,
            execution_id,
            raw_contract,
        ):
            self._process_local_resume_owners[session_id] = runner
            if owned_event_store is not None:
                self._process_local_owned_event_stores[session_id] = owned_event_store
            if isinstance(raw_contract, Mapping):
                _register_process_local_authority_terminal_finalizer(
                    session_id,
                    execution_id,
                    raw_contract.get("foundation_a_authority"),
                    runner._adapter,
                    ("execute-seed-handler", id(self), id(runner)),
                    lambda: self._release_terminal_process_local_owner(
                        session_id=session_id,
                        runner=runner,
                    ),
                )
            return True
        return False

    async def _reconcile_process_local_owner(
        self,
        *,
        tracker: SessionTracker,
        runner: OrchestratorRunner,
        session_repo: SessionRepository,
    ) -> tuple[bool, EventStore | None]:
        """Retain a paused owner or release a terminal owner after a run.

        Returns whether the runner remains retained and, only for a terminal
        result, an internally owned store that can be closed after the caller
        no longer needs it.
        """
        try:
            result = await session_repo.reconstruct_session(tracker.session_id)
        except Exception:
            result = None
        terminal_record_observed = False
        if result is not None and result.is_ok:
            status = result.value.status
            if status == SessionStatus.PAUSED:
                self._remember_process_local_owner(result.value, runner)
                if self._process_local_resume_owners.get(tracker.session_id) is runner:
                    return True, None
            elif status in (
                SessionStatus.COMPLETED,
                SessionStatus.CANCELLED,
                SessionStatus.FAILED,
            ):
                # An explicit terminal record is the one durable observation
                # that may evict a retained owner while its handler unwinds.
                terminal_record_observed = True

        raw_contract = tracker.progress.get("execution_contract")
        # Read errors, Result.err, and RUNNING (or a future nonterminal state)
        # are inconclusive during post-run reconciliation. Preserve the owner
        # only while its opaque capability still proves it is live. A durable
        # terminal record is intentionally the one exception.
        if not terminal_record_observed and runner._has_live_process_local_authority(
            tracker.session_id,
            tracker.execution_id,
            raw_contract,
        ):
            self._remember_process_local_owner(tracker, runner)
            return self._process_local_resume_owners.get(tracker.session_id) is runner, None

        owned_event_store: EventStore | None = None
        if self._process_local_resume_owners.get(tracker.session_id) is runner:
            self._process_local_resume_owners.pop(tracker.session_id, None)
            owned_event_store = self._process_local_owned_event_stores.pop(
                tracker.session_id,
                None,
            )
        return False, owned_event_store

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_execute_seed",
            description=(
                "Execute a seed (task specification) in Ouroboros. "
                "A seed defines a task to be executed with acceptance criteria. "
                "This is the handler for 'ooo run' commands — "
                "do NOT run 'ooo' in the shell; call this MCP tool instead."
            ),
            parameters=(
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="Inline seed YAML content to execute.",
                    required=False,
                ),
                MCPToolParameter(
                    name="seed_path",
                    type=ToolInputType.STRING,
                    description=(
                        "Path to a seed YAML file. If the path does not exist, the value is "
                        "treated as inline seed YAML."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="cwd",
                    type=ToolInputType.STRING,
                    description="Working directory used to resolve relative seed paths.",
                    required=False,
                ),
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Optional session ID to resume. If not provided, a new session is created.",
                    required=False,
                ),
                MCPToolParameter(
                    name="efficiency_mode",
                    type=ToolInputType.STRING,
                    description=(
                        "Execution efficiency policy. adaptive may start decomposed ACs "
                        "on lower-cost tiers and escalate on recovery; quality_first keeps "
                        "children at the parent starting tier. Default: adaptive."
                    ),
                    required=False,
                    enum=("adaptive", "quality_first"),
                ),
                MCPToolParameter(
                    name="frugality_assurance",
                    type=ToolInputType.STRING,
                    description=(
                        "Frugality assurance: off, lightweight observe, or explicit strict "
                        "baseline eligibility. Defaults from efficiency_mode; strict is "
                        "never enabled implicitly."
                    ),
                    required=False,
                    enum=("off", "observe", "strict"),
                ),
                MCPToolParameter(
                    name="model_tier",
                    type=ToolInputType.STRING,
                    description=(
                        "Model-tier routing: small/medium/large → frugal/standard/frontier "
                        "execution tier (trusted decomposed children may run one tier below; "
                        "retries escalate). Default: medium"
                    ),
                    required=False,
                    enum=("small", "medium", "large"),
                ),
                MCPToolParameter(
                    name="max_iterations",
                    type=ToolInputType.INTEGER,
                    description="Maximum number of execution iterations. Default: 10",
                    required=False,
                    default=10,
                ),
                MCPToolParameter(
                    name="skip_qa",
                    type=ToolInputType.BOOLEAN,
                    description="Skip post-execution QA evaluation. Default: false",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="auto_evaluate",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Override execution.auto_evaluate for this call. When true, "
                        "a successful background execute_seed run enqueues formal "
                        "3-stage evaluation as a separate bounded background job."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="conductor_decision_id",
                    type=ToolInputType.STRING,
                    description=(
                        "Selected conductor decision receipt authorizing a fresh successor."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="predecessor_execution_id",
                    type=ToolInputType.STRING,
                    description="Execution ID that the new successor follows.",
                    required=False,
                ),
                MCPToolParameter(
                    name="conductor_directive",
                    type=ToolInputType.OBJECT,
                    description=(
                        "Bounded corrective context copied exactly from the selected "
                        "conductor decision. Fresh successor executions only."
                    ),
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
        *,
        execution_id: str | None = None,
        session_id_override: str | None = None,
        synchronous: bool = False,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a seed execution request.

        Args:
            arguments: Tool arguments including seed_content or seed_path.
            execution_id: Pre-allocated execution ID (used by StartExecuteSeedHandler).
            session_id_override: Pre-allocated session ID for new executions
                (used by StartExecuteSeedHandler).
            synchronous: When True, run execution inline (blocking) instead of
                fire-and-forget.  Used by StartExecuteSeedHandler so the Job
                system can track the real execution lifetime.

        Returns:
            Result containing execution result or error.
        """
        cwd_result = self._resolve_dispatch_cwd_result(
            arguments.get("cwd"), tool_name="ouroboros_execute_seed"
        )
        if cwd_result.is_err:
            return cwd_result
        resolved_cwd = cwd_result.value
        seed_result = await self._resolve_seed_content(
            arguments=arguments,
            resolved_cwd=resolved_cwd,
            tool_name="ouroboros_execute_seed",
        )
        if seed_result.is_err:
            return seed_result
        seed_content = seed_result.value

        session_id = arguments.get("session_id")
        is_resume = bool(session_id)
        successor_seed = await _prepare_conductor_successor_seed(
            arguments=arguments,
            seed_content=seed_content,
            event_store=self.event_store,
            tool_name="ouroboros_execute_seed",
            is_resume=is_resume,
        )
        if successor_seed.is_err:
            return Result.err(successor_seed.error)
        seed_content = successor_seed.value
        arguments = {**arguments, "seed_content": seed_content}
        session_id = session_id or session_id_override
        model_tier, base_model_tier_override, delegated_model_tier = _resolve_model_tier_request(
            arguments, is_resume=is_resume
        )
        try:
            execution_preferences, raw_efficiency_mode, raw_frugality_assurance = (
                _resolve_execution_preferences_request(arguments, is_resume=is_resume)
            )
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_execute_seed"))
        # ``medium`` is the public response/default value, not an explicit resume
        # override. Preserve a session's resolved routing contract unless the
        # caller actually supplied model_tier on this invocation.
        max_iterations = arguments.get("max_iterations", 10)
        if not is_resume and session_id is None:
            session_id = f"orch_{uuid4().hex[:12]}"

        # Extract delegation context (only for new executions, not resumes)
        inherited_runtime_handle = (
            None if is_resume else _extract_inherited_runtime_handle(arguments)
        )
        inherited_effective_tools = (
            None if is_resume else _extract_inherited_effective_tools(arguments)
        )

        log.info(
            "mcp.tool.execute_seed",
            session_id=session_id,
            model_tier=model_tier,
            max_iterations=max_iterations,
            runtime_backend=self.agent_runtime_backend,
            llm_backend=self.llm_backend,
            cwd=str(resolved_cwd),
        )

        # Resolve worker cap up front so plugin and in-process paths agree.
        try:
            max_parallel_workers = get_max_parallel_workers()
        except ConfigError as e:
            return Result.err(
                MCPToolError(
                    f"Execution handler config error: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )

        seed_parse = _parse_seed_yaml_for_execution_mode(
            seed_content,
            tool_name="ouroboros_execute_seed",
        )
        if seed_parse.is_err:
            return seed_parse
        seed_dict, execution_mode = seed_parse.value
        if not is_resume:
            mode_result = _validate_fresh_execution_mode(
                execution_mode,
                tool_name="ouroboros_execute_seed",
            )
            if mode_result.is_err:
                return mode_result

        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            if is_resume:
                plugin_mode_result = await _validate_plugin_resume_acceptance_contract(
                    event_store=self.event_store,
                    execution_mode=execution_mode,
                    session_id=session_id,
                    tool_name="ouroboros_execute_seed",
                )
            else:
                plugin_mode_result = _validate_plugin_execution_mode(
                    execution_mode,
                    tool_name="ouroboros_execute_seed",
                )
            if plugin_mode_result.is_err:
                return plugin_mode_result
            # --- Subagent dispatch: gate on runtime + opencode_mode ---
            auto_evaluate = resolve_auto_evaluate(
                get_auto_evaluate_enabled(),
                arguments.get("auto_evaluate"),
            )
            _effective_tier, _runner_tier, delegated_model_tier = _resolve_model_tier_request(
                arguments,
                is_resume=is_resume,
            )
            payload = build_execute_subagent(
                seed_content=seed_content,
                session_id=session_id,
                seed_path=arguments.get("seed_path"),
                cwd=str(resolved_cwd),
                max_iterations=max_iterations,
                skip_qa=arguments.get("skip_qa", False),
                auto_evaluate=auto_evaluate,
                model_tier=delegated_model_tier,
                efficiency_mode=execution_preferences.efficiency_mode.value,
                frugality_assurance=execution_preferences.frugality_assurance.value,
                frugality_assurance_explicit=(execution_preferences.frugality_assurance_explicit),
                max_parallel_workers=max_parallel_workers,
            )
            # Preserve public response shape (#442): consumers expect
            # session_id / status keys even in plugin-dispatch mode.
            return await dispatch_plugin_terminal(
                self.event_store,
                session_id=session_id,
                payload=payload,
                response_shape={
                    "session_id": session_id,
                    "status": DELEGATED_TO_SUBAGENT,
                    "dispatch_mode": "plugin",
                    "runtime_backend": self.agent_runtime_backend,
                    "model_tier": model_tier,
                    "efficiency_mode": execution_preferences.efficiency_mode.value,
                    "frugality_assurance": execution_preferences.frugality_assurance.value,
                    **({} if is_resume else _plugin_fat_harness_downgrade_meta(execution_mode)),
                    **_plugin_verification_meta(
                        session_id,
                        auto_evaluate=auto_evaluate,
                    ),
                },
            )

        # Fall-through: real in-process execution (subprocess / non-opencode runtimes).

        # Parse seed_content YAML into Seed object
        try:
            seed = Seed.from_dict(seed_dict)
        except (ValidationError, PydanticValidationError) as e:
            log.error("mcp.tool.execute_seed.validation_error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed validation failed: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )

        verification_working_dir = self._resolve_verification_working_dir(
            seed,
            resolved_cwd,
            arguments.get("cwd"),
            arguments.get(DELEGATED_PARENT_CWD_ARG),
        )

        # Use injected or create orchestrator dependencies
        try:
            runtime_backend = self.agent_runtime_backend
            resolved_llm_backend = self.llm_backend or "default"
            event_store = self.event_store or EventStore()
            owns_event_store = self.event_store is None
            await event_store.initialize()
            # Use stderr: in MCP stdio mode, stdout is the JSON-RPC channel.
            console = Console(stderr=True)
            session_repo = SessionRepository(event_store)
            workspace: TaskWorkspace | None = None
            tracker = None
            launched = False
            retained_after_run = False
            retained_event_store_to_close: EventStore | None = None
            use_worktree = bool(arguments.get("use_worktree", True))
            retained_owner: OrchestratorRunner | None = None
            retained_resume_handoff = False
            workspace_runner: OrchestratorRunner | None = None
            workspace_session_id = session_id
            workspace_execution_id = execution_id
            workspace_reservation_runner: OrchestratorRunner | None = None
            workspace_reservation: object | None = None

            try:
                if is_resume and session_id:
                    tracker_result = await session_repo.reconstruct_session(session_id)
                    if tracker_result.is_err:
                        return Result.err(
                            MCPToolError(
                                f"Session resume failed: {tracker_result.error.message}",
                                tool_name="ouroboros_execute_seed",
                            )
                        )
                    tracker = tracker_result.value
                    workspace_session_id = tracker.session_id
                    workspace_execution_id = tracker.execution_id
                    try:
                        execution_preferences = _persisted_execution_preferences(tracker.progress)
                    except ValueError as exc:
                        return Result.err(
                            MCPToolError(str(exc), tool_name="ouroboros_execute_seed")
                        )
                    if tracker.status in (
                        SessionStatus.COMPLETED,
                        SessionStatus.CANCELLED,
                        SessionStatus.FAILED,
                    ):
                        await self._reconcile_terminal_process_local_owner(tracker)
                        return Result.err(
                            MCPToolError(
                                (
                                    f"Session {tracker.session_id} is already "
                                    f"{tracker.status.value} and cannot be resumed"
                                ),
                                tool_name="ouroboros_execute_seed",
                            )
                        )
                    retained_owner = await self._retained_process_local_owner(tracker)
                    if retained_owner is not None:
                        # Reserve this exact handler's pre-resume workspace handoff
                        # before touching the worktree. Without this gate a second
                        # same-handler resume can surface a generic lock error ahead
                        # of the process-local exclusive-claim result.
                        if not self._begin_process_local_resume_handoff(tracker.session_id):
                            return Result.err(
                                _process_local_resume_block_error(
                                    tracker,
                                    resume_blocked="process_local_execution_in_progress",
                                )
                            )
                        retained_resume_handoff = True
                    elif _process_local_authority_is_held_elsewhere(tracker):
                        # This handler has no exact retained owner. Do not let a
                        # foreign process-local capability fail later as a generic
                        # worktree collision or create a fresh runtime first.
                        return Result.err(
                            _process_local_resume_block_error(
                                tracker,
                                resume_blocked="process_local_authority_held_elsewhere",
                            )
                        )
                    if use_worktree:
                        if retained_owner is not None:
                            workspace_reservation_runner = retained_owner
                            workspace_reservation = retained_owner._reserve_task_workspace()
                        persisted = TaskWorkspace.from_progress_dict(
                            tracker.progress.get("workspace")
                        )
                        try:
                            workspace = maybe_restore_task_workspace(
                                session_id,
                                persisted,
                                fallback_source_cwd=resolved_cwd,
                                allow_dirty=inherited_runtime_handle is not None,
                            )
                        except WorktreeError as e:
                            if workspace_reservation_runner is not None:
                                workspace_reservation_runner._release_task_workspace_reservation(
                                    workspace_reservation
                                )
                                workspace_reservation = None
                            return Result.err(
                                MCPToolError(
                                    f"Task workspace error: {e.message}",
                                    tool_name="ouroboros_execute_seed",
                                )
                            )
                        if retained_owner is not None:
                            # The paused run releases its worktree lock. Restore
                            # that exact workspace before resuming through the
                            # retained runner so its resume identity check and
                            # its next effect use a freshly held task lock.
                            retained_owner._task_workspace = workspace
                elif use_worktree:
                    try:
                        workspace = maybe_prepare_task_workspace(
                            resolved_cwd,
                            session_id,
                            allow_dirty=inherited_runtime_handle is not None,
                        )
                    except WorktreeError as e:
                        return Result.err(
                            MCPToolError(
                                f"Task workspace error: {e.message}",
                                tool_name="ouroboros_execute_seed",
                            )
                        )

                if retained_owner is not None:
                    # Resume through the exact in-memory runner that owns the
                    # opaque capability. Do not construct a fresh runtime or
                    # create a fresh runner: either operation would turn a
                    # same-process continuation into a foreign-adapter path.
                    runner = retained_owner
                    agent_adapter = runner._adapter
                    if event_store is not runner._event_store:
                        # A handler without an injected store opened this
                        # short-lived observer connection solely to reconstruct
                        # the tracker above. The retained runner's store is the
                        # one that must survive the paused lifecycle.
                        if owns_event_store:
                            close_result = event_store.close()
                            if inspect.isawaitable(close_result):
                                await close_result
                        event_store = runner._event_store
                        owns_event_store = False
                    session_repo = runner._session_repo
                else:
                    delegated_permission_mode = (
                        inherited_runtime_handle.approval_mode
                        if inherited_runtime_handle and inherited_runtime_handle.approval_mode
                        else None
                    )
                    agent_adapter = create_agent_runtime(
                        backend=self.agent_runtime_backend,
                        model=_resolve_execution_model(self.agent_runtime_backend),
                        cwd=Path(workspace.effective_cwd) if workspace else resolved_cwd,
                        llm_backend=self.llm_backend,
                        startup_output_timeout_seconds=0,
                        stdout_idle_timeout_seconds=0,
                        **(
                            {"permission_mode": delegated_permission_mode}
                            if delegated_permission_mode
                            else {}
                        ),
                    )

                    # Create checkpoint store for execution state persistence
                    checkpoint_store = CheckpointStore()
                    checkpoint_store.initialize()
                    fat_harness_mode = _fresh_fat_harness_mode(execution_mode)
                    if is_resume:
                        persisted_fat_harness_mode = tracker.progress.get("fat_harness_mode")
                        if isinstance(persisted_fat_harness_mode, bool):
                            fat_harness_mode = persisted_fat_harness_mode
                        else:
                            # No persisted contract (historical session): mirror the
                            # CLI resume semantics — verify-by-default unless the
                            # seed opts out with execution_mode='legacy'.
                            fat_harness_mode = _fresh_fat_harness_mode(execution_mode)

                    # Create orchestrator runner
                    runner = OrchestratorRunner(
                        adapter=agent_adapter,
                        event_store=event_store,
                        console=console,
                        mcp_manager=self.mcp_manager,
                        mcp_tool_prefix=self.mcp_tool_prefix,
                        debug=False,
                        enable_decomposition=True,
                        inherited_runtime_handle=inherited_runtime_handle,
                        inherited_tools=inherited_effective_tools,
                        task_workspace=workspace,
                        checkpoint_store=checkpoint_store,
                        max_parallel_workers=max_parallel_workers,
                        fat_harness_mode=fat_harness_mode,
                        # Drive model-tier routing from the MCP model_tier arg
                        # (small/medium/large → frugal/standard/frontier top-level tier).
                        base_model_tier=base_model_tier_override,
                        efficiency_mode=raw_efficiency_mode,
                        frugality_assurance=raw_frugality_assurance,
                        session_signal_hub=self.session_signal_hub,
                    )
                workspace_runner = runner
                runtime_backend_attr = getattr(agent_adapter, "runtime_backend", None)
                if not (isinstance(runtime_backend_attr, str) and runtime_backend_attr):
                    runtime_backend_attr = getattr(agent_adapter, "_runtime_backend", None)
                effective_runtime_backend = (
                    runtime_backend_attr
                    if isinstance(runtime_backend_attr, str) and runtime_backend_attr
                    else runtime_backend or "unknown"
                )

                skip_qa = arguments.get("skip_qa", False)
                if not is_resume:
                    prepared = await runner.prepare_session(
                        seed,
                        execution_id=execution_id,
                        session_id=session_id,
                    )
                    if prepared.is_err:
                        prepared_details = prepared.error.details
                        detail_session_id = prepared_details.get("session_id")
                        detail_execution_id = prepared_details.get("execution_id")
                        if isinstance(detail_session_id, str):
                            workspace_session_id = detail_session_id
                        if isinstance(detail_execution_id, str):
                            workspace_execution_id = detail_execution_id
                        terminal_persistence_pending = (
                            prepared_details.get("resume_blocked") == "terminal_persistence_pending"
                        )
                        if terminal_persistence_pending:
                            retained_session_id = prepared_details.get(
                                "session_id", session_id or ""
                            )
                            retained_execution_id = prepared_details.get(
                                "execution_id", execution_id or ""
                            )
                            if isinstance(retained_session_id, str) and isinstance(
                                retained_execution_id, str
                            ):
                                retained_after_run = self._remember_process_local_owner_identity(
                                    session_id=retained_session_id,
                                    execution_id=retained_execution_id,
                                    raw_contract=runner._execution_contract,
                                    runner=runner,
                                    owned_event_store=(event_store if owns_event_store else None),
                                )
                            if not retained_after_run:
                                retained_tracker = await session_repo.reconstruct_session(
                                    retained_session_id
                                    if isinstance(retained_session_id, str)
                                    else ""
                                )
                            else:
                                retained_tracker = None
                            if retained_tracker is not None and retained_tracker.is_ok:
                                self._remember_process_local_owner(
                                    retained_tracker.value,
                                    runner,
                                    owned_event_store=(event_store if owns_event_store else None),
                                )
                                retained_after_run = (
                                    self._process_local_resume_owners.get(
                                        retained_tracker.value.session_id
                                    )
                                    is runner
                                )
                        return Result.err(
                            MCPToolError(
                                f"Execution failed: {prepared.error.message}",
                                tool_name="ouroboros_execute_seed",
                                is_retriable=terminal_persistence_pending,
                                details=dict(prepared_details),
                            )
                        )
                    tracker = prepared.value
                    workspace_session_id = tracker.session_id
                    workspace_execution_id = tracker.execution_id
                    self._remember_process_local_owner(
                        tracker,
                        runner,
                        owned_event_store=event_store if owns_event_store else None,
                    )

                background_started = False

                # Background execution coroutine — either awaited directly
                # (synchronous=True) or wrapped in create_task (fire-and-forget).
                async def _run_in_background(
                    _runner: OrchestratorRunner,
                    _seed: Seed,
                    _tracker,
                    _seed_content: str,
                    _resume_existing: bool,
                    _skip_qa: bool,
                    _workspace: TaskWorkspace | None = workspace,
                    _session_repo: SessionRepository = session_repo,
                    _event_store: EventStore = event_store,
                    _owns_event_store: bool = owns_event_store,
                    _retained_resume_handoff: bool = retained_resume_handoff,
                    _workspace_reservation_runner: OrchestratorRunner | None = (
                        workspace_reservation_runner
                    ),
                    _workspace_reservation: object | None = workspace_reservation,
                ) -> None:
                    nonlocal background_started, retained_after_run, retained_event_store_to_close
                    # This must be the first coroutine action. A done callback
                    # distinguishes cancellation before this first scheduling
                    # turn (no ``finally`` execution) from ordinary in-body
                    # cancellation, which reaches the cleanup below.
                    background_started = True
                    try:
                        if _resume_existing:
                            result = await _runner.resume_session(_tracker.session_id, _seed)
                        else:
                            result = await _runner.execute_precreated_session(
                                seed=_seed,
                                tracker=_tracker,
                                parallel=True,
                            )
                        if result.is_err:
                            log.error(
                                "mcp.tool.execute_seed.background_failed",
                                session_id=_tracker.session_id,
                                error=str(result.error),
                            )
                            if _is_process_local_background_owned_error(result.error):
                                log.info(
                                    "mcp.tool.execute_seed.background_process_local_lifecycle_owned",
                                    session_id=_tracker.session_id,
                                    resume_blocked=result.error.details.get("resume_blocked"),
                                )
                                return
                            mark_result = await _session_repo.mark_failed(
                                _tracker.session_id,
                                error_message=str(result.error),
                            )
                            if mark_result.is_err:
                                log.error(
                                    "mcp.tool.execute_seed.background_mark_failed_error",
                                    session_id=_tracker.session_id,
                                    error=str(mark_result.error),
                                )
                            elif mark_result.value is False:
                                log.info(
                                    "mcp.tool.execute_seed.background_terminal_preserved",
                                    session_id=_tracker.session_id,
                                )
                            return
                        if not result.value.success:
                            log.warning(
                                "mcp.tool.execute_seed.background_unsuccessful",
                                session_id=_tracker.session_id,
                                message=result.value.final_message,
                            )
                            return
                        if not _skip_qa:
                            from ouroboros.mcp.tools.qa import QAHandler

                            qa_handler = QAHandler(
                                llm_adapter=self.llm_adapter,
                                llm_backend=self.llm_backend,
                            )
                            quality_bar = self._derive_quality_bar(_seed)
                            execution_artifact = self._get_verification_artifact(
                                result.value.summary,
                                result.value.final_message,
                            )
                            try:
                                verification = await build_verification_artifacts(
                                    result.value.execution_id,
                                    execution_artifact,
                                    verification_working_dir,
                                    llm_adapter=self.llm_adapter,
                                    llm_backend=self.llm_backend,
                                )
                                artifact = verification.artifact
                                reference = verification.reference
                            except Exception as e:
                                artifact = execution_artifact
                                reference = f"Verification artifact generation failed: {e}"
                            await qa_handler.handle(
                                {
                                    "artifact": artifact,
                                    "artifact_type": "test_output",
                                    "quality_bar": quality_bar,
                                    "reference": reference,
                                    "seed_content": _seed_content,
                                    "pass_threshold": 0.80,
                                }
                            )
                    except Exception:
                        log.exception(
                            "mcp.tool.execute_seed.background_error",
                            session_id=_tracker.session_id,
                        )
                        try:
                            mark_result = await _session_repo.mark_failed(
                                _tracker.session_id,
                                error_message="Unexpected error in background execution",
                            )
                            if mark_result.is_err:
                                log.error(
                                    "mcp.tool.execute_seed.mark_failed_error",
                                    session_id=_tracker.session_id,
                                    error=str(mark_result.error),
                                )
                            elif mark_result.value is False:
                                log.info(
                                    "mcp.tool.execute_seed.background_terminal_preserved",
                                    session_id=_tracker.session_id,
                                )
                        except Exception:
                            log.exception("mcp.tool.execute_seed.mark_failed_error")
                    finally:
                        (
                            retained_after_run,
                            retained_event_store_to_close,
                        ) = await self._reconcile_process_local_owner(
                            tracker=_tracker,
                            runner=_runner,
                            session_repo=_session_repo,
                        )
                        if _workspace_reservation_runner is not None:
                            _workspace_reservation_runner._release_task_workspace_reservation(
                                _workspace_reservation
                            )
                        if _workspace is not None:
                            self._release_runner_task_workspace(
                                _runner,
                                _workspace,
                                session_id=_tracker.session_id,
                                execution_id=_tracker.execution_id,
                            )
                        if _owns_event_store and not retained_after_run:
                            try:
                                close_result = _event_store.close()
                                if inspect.isawaitable(close_result):
                                    await close_result
                                if retained_event_store_to_close is _event_store:
                                    retained_event_store_to_close = None
                            except Exception:
                                log.exception("mcp.tool.execute_seed.event_store_close_error")
                        if (
                            retained_event_store_to_close is not None
                            and not retained_after_run
                            and not synchronous
                        ):
                            try:
                                close_result = retained_event_store_to_close.close()
                                if inspect.isawaitable(close_result):
                                    await close_result
                                retained_event_store_to_close = None
                            except Exception:
                                log.exception("mcp.tool.execute_seed.event_store_close_error")
                        if _retained_resume_handoff:
                            self._finish_process_local_resume_handoff(_tracker.session_id)

                session_status: SessionStatus | None = None
                pause_metadata: dict[str, Any] = {}
                if synchronous:
                    # Run inline — the caller (StartExecuteSeedHandler / Job
                    # system) already handles backgrounding.  Pass
                    # _owns_event_store=False so cleanup stays with the caller;
                    # reconstruct_session below still needs the store open.
                    launched = True
                    await _run_in_background(
                        runner,
                        seed,
                        tracker,
                        seed_content,
                        is_resume,
                        skip_qa,
                        _owns_event_store=False,
                    )

                    # Derive actual outcome from session state.
                    try:
                        post_result = await session_repo.reconstruct_session(tracker.session_id)
                        if post_result.is_ok:
                            reconstructed_tracker = post_result.value
                            session_status = reconstructed_tracker.status
                            if session_status == SessionStatus.PAUSED:
                                pause_metadata = _pause_metadata_from_progress(
                                    reconstructed_tracker.progress
                                )
                        else:
                            session_status = None
                    except Exception:
                        session_status = None

                    status_label, success, is_error, status_header = (
                        _classify_synchronous_execution_status(session_status)
                    )
                else:
                    # Fire-and-forget: launch in a background task.
                    task = asyncio.create_task(
                        _run_in_background(runner, seed, tracker, seed_content, is_resume, skip_qa)
                    )
                    launched = True
                    self._background_tasks.add(task)

                    def _background_done(completed: asyncio.Task[None]) -> None:
                        self._background_tasks.discard(completed)
                        if not completed.cancelled() or background_started:
                            return
                        cleanup = asyncio.create_task(
                            self._cleanup_unstarted_process_local_background_task(
                                tracker=tracker,
                                runner=runner,
                                workspace=workspace,
                                workspace_reservation=workspace_reservation,
                                owned_event_store=event_store if owns_event_store else None,
                                retained_resume_handoff=retained_resume_handoff,
                            )
                        )
                        self._background_tasks.add(cleanup)
                        cleanup.add_done_callback(self._background_tasks.discard)

                    task.add_done_callback(_background_done)
                    status_label = "running"
                    success = None  # unknown yet
                    is_error = False
                    status_header = "Seed Execution LAUNCHED"

                # --- shared message / meta construction ---
                message = (
                    f"{status_header}\n"
                    f"{'=' * 60}\n"
                    f"Seed ID: {seed.metadata.seed_id}\n"
                    f"Session ID: {tracker.session_id}\n"
                    f"Execution ID: {tracker.execution_id}\n"
                    f"Goal: {seed.goal}\n\n"
                    f"Status: {status_label}\n"
                    f"Runtime Backend: {effective_runtime_backend}\n"
                    f"LLM Backend: {resolved_llm_backend}\n"
                    f"Efficiency Mode: {execution_preferences.efficiency_mode.value}\n"
                    "Frugality Assurance: "
                    f"{execution_preferences.frugality_assurance.value}\n"
                )
                message += _run_only_verification_text(tracker.session_id)
                # Best-effort live dashboard URL (singleton daemon, reused across
                # runs; default on, opt out via OUROBOROS_DASHBOARD=0). Offloaded
                # to a thread so the healthz/first-spawn wait never blocks the loop.
                dashboard_url = await resolve_dashboard_run_url(
                    tracker.execution_id, self.event_store
                )
                if dashboard_url:
                    message += f"Live Dashboard: {dashboard_url}\n"
                if pause_metadata:
                    if pause_metadata.get("pause_kind") is not None:
                        message += f"Pause Kind: {pause_metadata['pause_kind']}\n"
                    if pause_metadata.get("pause_seconds") is not None:
                        message += f"Pause Seconds: {pause_metadata['pause_seconds']}\n"
                    if pause_metadata.get("resume_after") is not None:
                        message += f"Resume After: {pause_metadata['resume_after']}\n"
                    if pause_metadata.get("resume_hint") is not None:
                        message += f"Resume Hint: {pause_metadata['resume_hint']}\n"
                if workspace is not None:
                    message += (
                        f"Task Worktree: {workspace.worktree_path}\n"
                        f"Task Branch: {workspace.branch}\n"
                    )
                if not synchronous:
                    message += (
                        "\nExecution is running in the background.\n"
                        "Use ouroboros_session_status to track progress.\n"
                        "Use ouroboros_query_events for detailed event history.\n"
                    )

                meta: dict[str, Any] = {
                    "seed_id": seed.metadata.seed_id,
                    "session_id": tracker.session_id,
                    "execution_id": tracker.execution_id,
                    "launched": True,
                    "status": status_label,
                    "runtime_backend": effective_runtime_backend,
                    "llm_backend": resolved_llm_backend,
                    "efficiency_mode": execution_preferences.efficiency_mode.value,
                    "frugality_assurance": (execution_preferences.frugality_assurance.value),
                    "resume_requested": is_resume,
                    **({"dashboard_url": dashboard_url} if dashboard_url else {}),
                    **_run_only_verification_meta(tracker.session_id),
                }
                if success is not None:
                    meta["success"] = success
                if session_status == SessionStatus.PAUSED:
                    meta["paused"] = True
                    meta.update(pause_metadata)
                if workspace is not None:
                    meta["worktree_path"] = workspace.worktree_path
                    meta["worktree_branch"] = workspace.branch

                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text=message),),
                        is_error=is_error,
                        meta=meta,
                    )
                )
            finally:
                # In synchronous mode, _run_in_background was told NOT to own
                # cleanup (_owns_event_store=False), so the caller cleans up
                # after reconstruct_session has finished using the store.
                if not launched or synchronous:
                    if workspace_reservation_runner is not None:
                        workspace_reservation_runner._release_task_workspace_reservation(
                            workspace_reservation
                        )
                if workspace is not None and (not launched or synchronous):
                    if workspace_runner is not None:
                        self._release_runner_task_workspace(
                            workspace_runner,
                            workspace,
                            session_id=workspace_session_id,
                            execution_id=workspace_execution_id,
                        )
                    else:
                        release_lock(workspace.lock_path)
                if owns_event_store and (not launched or synchronous) and not retained_after_run:
                    try:
                        close_result = event_store.close()
                        if inspect.isawaitable(close_result):
                            await close_result
                        if retained_event_store_to_close is event_store:
                            retained_event_store_to_close = None
                    except Exception:
                        log.exception("mcp.tool.execute_seed.event_store_close_error")
                if retained_event_store_to_close is not None and not retained_after_run:
                    try:
                        close_result = retained_event_store_to_close.close()
                        if inspect.isawaitable(close_result):
                            await close_result
                    except Exception:
                        log.exception("mcp.tool.execute_seed.event_store_close_error")
                if retained_resume_handoff and (not launched or synchronous):
                    self._finish_process_local_resume_handoff(tracker.session_id)
        except Exception as e:
            log.error("mcp.tool.execute_seed.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed execution failed: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )

    @staticmethod
    def _resolve_dispatch_cwd(raw_cwd: Any) -> Path:
        """Resolve the working directory for intercepted seed execution."""
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            return Path(raw_cwd).expanduser().resolve()
        return Path.cwd()

    @staticmethod
    def _resolve_dispatch_cwd_result(
        raw_cwd: Any,
        *,
        tool_name: str,
    ) -> Result[Path, MCPServerError]:
        """Resolve and validate the dispatch cwd before launching work.

        Background seed runs should fail closed before creating a job when the
        requested cwd is missing or is not a directory.  Otherwise the actual
        execution fails later inside the runtime with a less actionable
        ``FileNotFoundError``.
        """
        resolved_cwd = ExecuteSeedHandler._resolve_dispatch_cwd(raw_cwd)
        if not resolved_cwd.exists():
            return Result.err(
                MCPToolError(
                    f"Working directory does not exist: {resolved_cwd}",
                    tool_name=tool_name,
                )
            )
        if not resolved_cwd.is_dir():
            return Result.err(
                MCPToolError(
                    f"Working directory is not a directory: {resolved_cwd}",
                    tool_name=tool_name,
                )
            )
        return Result.ok(resolved_cwd)

    @staticmethod
    async def _resolve_seed_content(
        *,
        arguments: dict[str, Any],
        resolved_cwd: Path,
        tool_name: str,
    ) -> Result[str, MCPServerError]:
        """Resolve seed YAML from inline ``seed_content`` or a contained ``seed_path``.

        Single source of truth for both ``ExecuteSeedHandler`` and
        ``StartExecuteSeedHandler`` so the seed-path containment policy stays
        in one place. The candidate path must live inside ``resolved_cwd`` or
        ``~/.ouroboros/seeds``; non-existent paths fall back to inline YAML
        per the tool contract; ``OSError``s become :class:`MCPToolError`.
        """
        seed_content = arguments.get("seed_content")
        if seed_content:
            return Result.ok(seed_content)

        seed_path = arguments.get("seed_path")
        if not seed_path:
            return Result.err(
                MCPToolError(
                    "seed_content or seed_path is required",
                    tool_name=tool_name,
                )
            )

        seed_candidate = Path(str(seed_path)).expanduser()
        if not seed_candidate.is_absolute():
            seed_candidate = resolved_cwd / seed_candidate

        # Allow seeds from cwd and the dedicated ~/.ouroboros/seeds/ directory
        ouroboros_seeds = Path.home() / ".ouroboros" / "seeds"
        valid_cwd, _ = InputValidator.validate_path_containment(
            seed_candidate,
            resolved_cwd,
        )
        valid_home, _ = InputValidator.validate_path_containment(
            seed_candidate,
            ouroboros_seeds,
        )
        if not valid_cwd and not valid_home:
            return Result.err(
                MCPToolError(
                    f"Seed path escapes allowed directories: "
                    f"{seed_candidate} is not under {resolved_cwd} or {ouroboros_seeds}",
                    tool_name=tool_name,
                )
            )

        try:
            return Result.ok(await asyncio.to_thread(seed_candidate.read_text, encoding="utf-8"))
        except FileNotFoundError:
            # Per tool contract: treat non-existent path as inline YAML
            return Result.ok(str(seed_path))
        except OSError as e:
            return Result.err(
                MCPToolError(
                    f"Failed to read seed file: {e}",
                    tool_name=tool_name,
                )
            )

    @staticmethod
    def _derive_quality_bar(seed: Seed) -> str:
        """Derive a quality bar string from seed acceptance criteria."""
        ac_lines = [f"- {ac}" for ac in ac_texts(seed.acceptance_criteria)]
        return "The execution must satisfy all acceptance criteria:\n" + "\n".join(ac_lines)

    @staticmethod
    def _resolve_verification_working_dir(
        seed: Seed,
        dispatch_cwd: Path,
        raw_cwd: Any,
        delegated_parent_cwd: Any,
    ) -> Path:
        """Resolve the best project directory for post-run verification."""
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            return dispatch_cwd

        if isinstance(delegated_parent_cwd, str) and delegated_parent_cwd.strip():
            return Path(delegated_parent_cwd).expanduser().resolve()

        resolution = resolve_seed_project_path(seed, stable_base=dispatch_cwd)
        if resolution.path is not None:
            return resolution.path
        if resolution.rejected:
            log.warning(
                "execution_handlers.seed_project_path_rejected",
                dispatch_cwd=str(dispatch_cwd),
                reason="every seed-encoded project path escaped the dispatch cwd",
            )
        return dispatch_cwd

    @staticmethod
    def _get_verification_artifact(summary: dict[str, Any], final_message: str) -> str:
        """Prefer the structured verification report when present."""
        verification_report = summary.get("verification_report")
        if isinstance(verification_report, str) and verification_report:
            return verification_report
        return final_message or ""

    @staticmethod
    def _format_execution_result(exec_result, seed: Seed) -> str:
        """Format execution result as human-readable text.

        Args:
            exec_result: OrchestratorResult from execution.
            seed: Original seed specification.

        Returns:
            Formatted text representation.
        """
        status = "SUCCESS" if exec_result.success else "FAILED"
        lines = [
            f"Seed Execution {status}",
            "=" * 60,
            f"Seed ID: {seed.metadata.seed_id}",
            f"Session ID: {exec_result.session_id}",
            f"Execution ID: {exec_result.execution_id}",
            f"Goal: {seed.goal}",
            f"Messages Processed: {exec_result.messages_processed}",
            f"Duration: {exec_result.duration_seconds:.2f}s",
            "",
        ]

        if exec_result.summary:
            lines.append("Summary:")
            for key, value in exec_result.summary.items():
                lines.append(f"  {key}: {value}")
            lines.append("")

        if exec_result.final_message:
            lines.extend(
                [
                    "Final Message:",
                    "-" * 40,
                    exec_result.final_message[:1000],
                ]
            )
            if len(exec_result.final_message) > 1000:
                lines.append("...(truncated)")

        return "\n".join(lines)


@dataclass
class StartExecuteSeedHandler:
    """Start a seed execution asynchronously and return a job ID immediately.

    Idempotency contract (Q00/ouroboros#774):

    Callers may pass an ``idempotency_key`` argument. The handler maintains
    an in-memory ``dict[str, ExecutionMeta]`` keyed by ``idempotency_key``.
    On a second call with the same key, the handler returns the original
    execution metadata (``job_id`` / ``session_id`` / ``execution_id``) and
    does NOT enqueue a new background execution. Different keys produce
    independent executions.

    **Non-goal**: persistence across server restarts. The map TTL is the
    process lifetime; on restart the key map is empty and a duplicate
    request *can* enqueue a new execution. This is an intentional scope
    bound — auto-side state recovery on the same restart will surface
    the duplicate. See the issue for the full rationale.
    """

    execute_handler: ExecuteSeedHandler | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._execute_handler = self.execute_handler or ExecuteSeedHandler(
            event_store=self._event_store,
            agent_runtime_backend=self.agent_runtime_backend,
            opencode_mode=self.opencode_mode,
        )
        # Process-lifetime idempotency map: idempotency_key -> tool result
        # meta dict. Entries are added once on first call and reused on
        # subsequent calls with the same key.
        self._idempotency_meta: dict[str, dict[str, Any]] = {}
        # Parallel cache for plugin-dispatch entries — stores the original
        # SubagentPayload + response_shape so a retry re-emits an identical
        # ``_subagent`` envelope without triggering a second
        # subagent_dispatched event.  Keyed by idempotency_key.
        self._idempotency_plugin_payload: dict[str, dict[str, Any]] = {}
        # Per-key serialization lock so two concurrent handle() calls with
        # the same idempotency_key dedupe correctly. Without this, both
        # callers can miss the cache (entries are written *after* dispatch)
        # and each enqueues a fresh execution. ``dict.setdefault`` is
        # atomic in single-threaded asyncio so the lock-creation path is
        # race-free; the lock is held across the cache check + dispatch +
        # cache write so the second caller observes a populated cache.
        self._idempotency_locks: dict[str, asyncio.Lock] = {}

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_start_execute_seed",
            description=(
                "Start a seed execution in the background and return a job ID immediately. "
                "Use ouroboros_ac_tree_hud for live progress snapshots and "
                "ouroboros_job_result for terminal output. "
                "In plugin mode, execution is delegated to an OpenCode Task pane and "
                "job_id is None — results appear in the Task pane instead of being "
                "pollable via job_status/job_result. "
                "This is the handler for 'ooo run' commands — "
                "do NOT run 'ooo' in the shell; call this MCP tool instead."
            ),
            parameters=(
                *ExecuteSeedHandler().definition.parameters,
                MCPToolParameter(
                    name="idempotency_key",
                    type=ToolInputType.STRING,
                    description=(
                        "Optional process-local idempotency key. A second call with the same "
                        "key returns the same execution metadata and does NOT enqueue a new "
                        "execution. Map TTL is process lifetime — not persistent across "
                        "server restarts."
                    ),
                    required=False,
                ),
            ),
        )

    async def _enqueue_chained_evaluation(
        self,
        run_result: MCPToolResult,
        *,
        session_id: str | None,
        seed_content: str,
        working_dir: Path,
    ) -> MCPToolResult:
        from ouroboros.mcp.tools.evaluation_handlers import StartEvaluateHandler

        artifact = run_result.text_content or "Execution completed successfully."
        evaluation_arguments: dict[str, Any] = {
            "session_id": session_id,
            "artifact": artifact,
            "artifact_type": "code",
            "seed_content": seed_content,
            "working_dir": str(working_dir),
        }
        try:
            seed_dict = yaml.safe_load(seed_content)
            if isinstance(seed_dict, dict):
                seed = Seed.from_dict(seed_dict)
                acceptance_criteria = [
                    stripped
                    for text in ac_texts(seed.acceptance_criteria)
                    if (stripped := text.strip())
                ]
                if acceptance_criteria:
                    evaluation_arguments["acceptance_criteria"] = acceptance_criteria
            else:
                log.warning(
                    "mcp.tool.start_execute_seed.chained_evaluate.seed_not_mapping",
                    session_id=session_id,
                )
        except yaml.YAMLError as exc:
            log.warning(
                "mcp.tool.start_execute_seed.chained_evaluate.yaml_error",
                session_id=session_id,
                error=str(exc),
            )
        except (ValidationError, PydanticValidationError) as exc:
            log.warning(
                "mcp.tool.start_execute_seed.chained_evaluate.seed_validation_error",
                session_id=session_id,
                error=str(exc),
            )
        try:
            start_evaluate = StartEvaluateHandler(
                event_store=self._event_store,
                job_manager=self._job_manager,
                llm_backend=self._execute_handler.llm_backend,
                agent_runtime_backend=self.agent_runtime_backend,
                opencode_mode=self.opencode_mode,
            )
            evaluate_result = await start_evaluate.handle(evaluation_arguments)
        except Exception as exc:  # noqa: BLE001 - evaluation must never flip run success.
            error = str(exc)
            return _append_result_text(
                run_result,
                _evaluation_enqueue_failed_text(session_id, error),
                meta=_evaluation_enqueue_failed_meta(
                    run_result,
                    session_id=session_id,
                    error=error,
                ),
            )

        if evaluate_result.is_err:
            error = evaluate_result.error.message
            return _append_result_text(
                run_result,
                _evaluation_enqueue_failed_text(session_id, error),
                meta=_evaluation_enqueue_failed_meta(
                    run_result,
                    session_id=session_id,
                    error=error,
                ),
            )

        evaluation_job_id = evaluate_result.value.meta.get("job_id")
        if not isinstance(evaluation_job_id, str) or not evaluation_job_id:
            error = "StartEvaluateHandler did not return a pollable evaluation job_id"
            return _append_result_text(
                run_result,
                _evaluation_enqueue_failed_text(session_id, error),
                meta=_evaluation_enqueue_failed_meta(
                    run_result,
                    session_id=session_id,
                    error=error,
                ),
            )

        return _append_result_text(
            run_result,
            _evaluation_enqueued_text(session_id, evaluation_job_id),
            meta=_evaluation_enqueued_meta(
                run_result,
                session_id=session_id,
                evaluation_job_id=evaluation_job_id,
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        # Serialize concurrent calls with the same idempotency_key so the
        # check-cache / dispatch / write-cache sequence is atomic per key.
        # Without this, two in-flight requests can both miss the cache and
        # each enqueue a fresh execution, breaking the "second call with
        # the same key does not enqueue" contract. ``setdefault`` is
        # atomic in single-threaded asyncio (no ``await`` between the
        # ``in`` check and insert), so lock-creation is race-free.
        raw_key_outer = arguments.get("idempotency_key")
        idem_key_outer = raw_key_outer.strip() if isinstance(raw_key_outer, str) else ""
        if idem_key_outer:
            lock = self._idempotency_locks.setdefault(idem_key_outer, asyncio.Lock())
            async with lock:
                return await self._handle_inner(arguments)
        return await self._handle_inner(arguments)

    async def _handle_inner(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        # Idempotency short-circuit: if this exact key was previously
        # served, return the same metadata without enqueuing another
        # execution. The map is process-local; see class docstring.
        raw_key = arguments.get("idempotency_key")
        idempotency_key = raw_key.strip() if isinstance(raw_key, str) else ""
        if idempotency_key and idempotency_key in self._idempotency_plugin_payload:
            # Plugin-dispatch replay: re-emit an identical ``_subagent``
            # envelope using the cached payload + response_shape, but do NOT
            # emit another subagent_dispatched event (the original first call
            # already recorded it).  This preserves the public response_shape
            # contract on retries with the same idempotency_key.
            cached = self._idempotency_plugin_payload[idempotency_key]
            return build_subagent_result(
                cached["payload"],
                response_shape=dict(cached["response_shape"]),
            )
        if idempotency_key and idempotency_key in self._idempotency_meta:
            cached_meta = dict(self._idempotency_meta[idempotency_key])
            cached_session_id = (
                str(cached_meta.get("session_id")) if cached_meta.get("session_id") else None
            )
            text = (
                "Replayed prior background execution via idempotency key.\n\n"
                f"Idempotency Key: {idempotency_key}\n"
                f"Job ID: {cached_meta.get('job_id') or 'pending'}\n"
                f"Session ID: {cached_meta.get('session_id') or 'pending'}\n"
                f"Execution ID: {cached_meta.get('execution_id') or 'pending'}\n\n"
                + _run_only_verification_text(cached_session_id)
            )
            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                    is_error=False,
                    meta=cached_meta,
                )
            )

        cwd_result = ExecuteSeedHandler._resolve_dispatch_cwd_result(
            arguments.get("cwd"), tool_name="ouroboros_start_execute_seed"
        )
        if cwd_result.is_err:
            return cwd_result
        resolved_cwd = cwd_result.value
        seed_result = await ExecuteSeedHandler._resolve_seed_content(
            arguments=arguments,
            resolved_cwd=resolved_cwd,
            tool_name="ouroboros_start_execute_seed",
        )
        if seed_result.is_err:
            return seed_result
        seed_content = seed_result.value
        # Forward the resolved YAML so the inner ExecuteSeedHandler skips its
        # own path-resolution branch (the contract is now centralised here).
        arguments = {**arguments, "seed_content": seed_content}

        is_resume = bool(arguments.get("session_id"))
        successor_seed = await _prepare_conductor_successor_seed(
            arguments=arguments,
            seed_content=seed_content,
            event_store=self._event_store,
            tool_name="ouroboros_start_execute_seed",
            is_resume=is_resume,
        )
        if successor_seed.is_err:
            return Result.err(successor_seed.error)
        seed_content = successor_seed.value
        arguments = {**arguments, "seed_content": seed_content}
        try:
            execution_preferences, _raw_efficiency_mode, _raw_frugality_assurance = (
                _resolve_execution_preferences_request(arguments, is_resume=is_resume)
            )
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_start_execute_seed"))
        resumed_tracker: SessionTracker | None = None
        if is_resume:
            await self._event_store.initialize()
            session_id = arguments.get("session_id")
            assert isinstance(session_id, str) and session_id
            session_result = await SessionRepository(self._event_store).reconstruct_session(
                session_id
            )
            if session_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Session resume failed: {session_result.error.message}",
                        tool_name="ouroboros_start_execute_seed",
                    )
                )
            resumed_tracker = session_result.value
            if resumed_tracker.status in (
                SessionStatus.COMPLETED,
                SessionStatus.CANCELLED,
                SessionStatus.FAILED,
            ):
                return Result.err(
                    MCPToolError(
                        (
                            f"Session {resumed_tracker.session_id} is already "
                            f"{resumed_tracker.status.value} and cannot be resumed"
                        ),
                        tool_name="ouroboros_start_execute_seed",
                    )
                )
            try:
                execution_preferences = _persisted_execution_preferences(resumed_tracker.progress)
            except ValueError as exc:
                return Result.err(MCPToolError(str(exc), tool_name="ouroboros_start_execute_seed"))
        seed_parse = _parse_seed_yaml_for_execution_mode(
            seed_content,
            tool_name="ouroboros_start_execute_seed",
        )
        if seed_parse.is_err:
            return seed_parse
        _, execution_mode = seed_parse.value
        if not is_resume:
            mode_result = _validate_fresh_execution_mode(
                execution_mode,
                tool_name="ouroboros_start_execute_seed",
            )
            if mode_result.is_err:
                return mode_result

        # Resolve worker cap up front so plugin and background paths agree.
        try:
            max_parallel_workers = get_max_parallel_workers()
        except ConfigError as e:
            return Result.err(
                MCPToolError(
                    f"Execution handler config error: {e}",
                    tool_name="ouroboros_start_execute_seed",
                )
            )

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        # StartExecuteSeedHandler delegates to ExecuteSeedHandler internally.
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            if is_resume:
                assert resumed_tracker is not None
                plugin_mode_result = _validate_plugin_resume_tracker_acceptance_contract(
                    tracker=resumed_tracker,
                    execution_mode=execution_mode,
                    tool_name="ouroboros_start_execute_seed",
                )
            else:
                plugin_mode_result = _validate_plugin_execution_mode(
                    execution_mode,
                    tool_name="ouroboros_start_execute_seed",
                )
            if plugin_mode_result.is_err:
                return plugin_mode_result

            # Generate session_id for fresh runs BEFORE building the payload
            # so the child prompt, context, audit event, and response all
            # share the same identity.  Without this the prompt says "new"
            # while the receipt advertises an orch_* id the child never sees.
            plugin_session_id = arguments.get("session_id")
            if not plugin_session_id:
                plugin_session_id = f"orch_{uuid4().hex[:12]}"

            auto_evaluate = resolve_auto_evaluate(
                get_auto_evaluate_enabled(),
                arguments.get("auto_evaluate"),
            )
            _effective_tier, _runner_tier, delegated_model_tier = _resolve_model_tier_request(
                arguments, is_resume=is_resume
            )
            payload = build_execute_subagent(
                seed_content=seed_content,
                session_id=plugin_session_id,
                seed_path=arguments.get("seed_path"),
                cwd=arguments.get("cwd"),
                max_iterations=arguments.get("max_iterations", 10),
                skip_qa=arguments.get("skip_qa", False),
                auto_evaluate=auto_evaluate,
                model_tier=delegated_model_tier,
                efficiency_mode=execution_preferences.efficiency_mode.value,
                frugality_assurance=execution_preferences.frugality_assurance.value,
                frugality_assurance_explicit=(execution_preferences.frugality_assurance_explicit),
                max_parallel_workers=max_parallel_workers,
            )

            # Plugin mode: work runs in the OpenCode child session (Task
            # pane), NOT in a JobManager background job.  Returning a fake
            # instantly-completing job_id would break the polling contract —
            # callers would see "completed" while the child is still running.
            # Instead we return job_id=None with an explicit status so no one
            # accidentally polls a non-existent job.
            response_shape: dict[str, Any] = {
                "job_id": None,
                "session_id": plugin_session_id,
                "execution_id": None,
                "status": DELEGATED_TO_PLUGIN,
                "dispatch_mode": "plugin",
                "runtime_backend": self.agent_runtime_backend,
                "efficiency_mode": execution_preferences.efficiency_mode.value,
                "frugality_assurance": execution_preferences.frugality_assurance.value,
                **({} if is_resume else _plugin_fat_harness_downgrade_meta(execution_mode)),
                **_plugin_verification_meta(
                    plugin_session_id,
                    auto_evaluate=auto_evaluate,
                ),
            }
            # Cache for idempotent replay: a second handle() with the same
            # key must NOT re-dispatch (no second subagent_dispatched event)
            # but MUST return an identical response_shape — same contract as
            # the non-plugin path's ``_idempotency_meta`` short-circuit.
            if idempotency_key:
                self._idempotency_plugin_payload[idempotency_key] = {
                    "payload": payload,
                    "response_shape": dict(response_shape),
                }
            # The shared helper initializes the store first so the audit
            # event persists, then emits and builds the envelope.
            return await dispatch_plugin_terminal(
                self._event_store,
                session_id=plugin_session_id,
                payload=payload,
                response_shape=response_shape,
            )

        # Fall-through: real background job path (subprocess / non-opencode
        # runtimes).  No subagent payload is built here — the background job
        # re-invokes ExecuteSeedHandler.handle() via ``_runner`` below, which
        # constructs and consumes its own payload internally.  The only payload
        # consumer in this handler is the plugin branch above; an earlier
        # background-path ``build_execute_subagent`` here was orphaned by commit
        # 3c393c98 (its result was never referenced) and has been removed.
        await self._event_store.initialize()

        session_id = arguments.get("session_id")
        execution_id: str | None = None
        new_session_id: str | None = None
        if session_id:
            assert resumed_tracker is not None
            execution_id = resumed_tracker.execution_id
        else:
            execution_id = f"exec_{uuid4().hex[:12]}"
            new_session_id = f"orch_{uuid4().hex[:12]}"

        auto_evaluate_enabled = resolve_auto_evaluate(
            get_auto_evaluate_enabled(),
            arguments.get("auto_evaluate"),
        )

        # The shared pipeline owns the ``should_cancel()`` pre-work guard.
        async def _runner(_handle) -> MCPToolResult:
            result = await self._execute_handler.handle(
                arguments,
                execution_id=execution_id,
                session_id_override=new_session_id,
                synchronous=True,
            )
            if result.is_err:
                raise RuntimeError(str(result.error))
            run_result = result.value
            run_session_id = _result_session_id(
                run_result,
                session_id or new_session_id,
            )
            if auto_evaluate_enabled and run_session_id and _run_succeeded(run_result):
                return await self._enqueue_chained_evaluation(
                    run_result,
                    session_id=run_session_id,
                    seed_content=seed_content,
                    working_dir=_result_evaluation_working_dir(
                        run_result,
                        resolved_cwd,
                    ),
                )
            return run_result

        snapshot = await start_background_tool_job(
            job_manager=self._job_manager,
            event_store=self._event_store,
            job_type="execute_seed",
            intent="execute_seed",
            process_scope=f"execute_seed:{execution_id}",
            initial_message="Queued seed execution",
            links=JobLinks(
                session_id=session_id or new_session_id,
                execution_id=execution_id,
                preserve_runner_result=auto_evaluate_enabled,
            ),
            work_fn=_runner,
            cancelled_text="Seed execution cancelled before restart work began.",
            detached_tool_name="ouroboros_start_execute_seed",
            detached_arguments=arguments,
            runtime_backend=self.agent_runtime_backend,
            llm_backend=self._execute_handler.llm_backend,
            opencode_mode=self.opencode_mode,
        )

        from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend
        from ouroboros.providers.factory import resolve_llm_backend

        try:
            runtime_backend = resolve_agent_runtime_backend(
                self._execute_handler.agent_runtime_backend
            )
        except (ValueError, Exception):
            runtime_backend = "unknown"
        try:
            llm_backend = resolve_llm_backend(self._execute_handler.llm_backend)
        except (ValueError, Exception):
            llm_backend = "unknown"

        dashboard_url = await resolve_dashboard_run_url(
            snapshot.links.execution_id or execution_id, self._event_store
        )
        dashboard_line = f"Live Dashboard: {dashboard_url}\n" if dashboard_url else ""
        text = (
            f"Started background execution.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Session ID: {snapshot.links.session_id or 'pending'}\n"
            f"Execution ID: {snapshot.links.execution_id or 'pending'}\n\n"
            f"Runtime Backend: {runtime_backend}\n"
            f"LLM Backend: {llm_backend}\n"
            f"Efficiency Mode: {execution_preferences.efficiency_mode.value}\n"
            "Frugality Assurance: "
            f"{execution_preferences.frugality_assurance.value}\n"
            f"{dashboard_line}\n"
            f"{_run_only_verification_text(snapshot.links.session_id)}\n"
            "Use ouroboros_ac_tree_hud(session_id, cursor) for live progress and "
            "ouroboros_job_result(job_id) for the final output."
        )
        meta: dict[str, Any] = {
            "job_id": snapshot.job_id,
            "session_id": snapshot.links.session_id,
            "execution_id": snapshot.links.execution_id,
            **({"dashboard_url": dashboard_url} if dashboard_url else {}),
            "status": snapshot.status.value,
            "cursor": snapshot.cursor,
            "runtime_backend": runtime_backend,
            "llm_backend": llm_backend,
            "efficiency_mode": execution_preferences.efficiency_mode.value,
            "frugality_assurance": execution_preferences.frugality_assurance.value,
            "job_observer": build_job_observer_contract(
                job_id=snapshot.job_id,
                cursor=snapshot.cursor,
                session_id=snapshot.links.session_id,
                execution_id=snapshot.links.execution_id,
                follow_result_job_keys=("chained_evaluate_job_id",),
            ),
            **_run_only_verification_meta(snapshot.links.session_id),
        }
        if idempotency_key:
            self._idempotency_meta[idempotency_key] = dict(meta)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta=meta,
                structured_content=dict(meta),
            )
        )
