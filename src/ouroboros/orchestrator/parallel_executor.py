"""Parallel AC execution orchestrator with Sub-AC decomposition.

Executes acceptance criteria in parallel groups based on dependency analysis.
Complex ACs are decomposed into Sub-ACs and executed in parallel.

Features:
- Parallel execution within dependency levels
- Claude-driven decomposition of complex ACs into Sub-ACs
- Parallel execution of Sub-ACs (each in separate Claude session)
- Event emission for TUI progress tracking

Example:
    executor = ParallelACExecutor(adapter, event_store, console)
    result = await executor.execute_parallel(
        seed=seed,
        execution_plan=graph.to_execution_plan(),
        session_id="sess_123",
        tools=["Read", "Write", "Bash"],
        system_prompt="You are an agent...",
    )

    if result.all_succeeded:
        print(f"All {result.success_count} ACs completed!")
    else:
        print(f"Partial: {result.success_count} succeeded, {result.failure_count} failed")
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import anyio
from rich.console import Console

from ouroboros.core.seed import AcceptanceCriterionSpec, ac_text
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeHandle,
)
from ouroboros.orchestrator.atomic_prompt_builder import (
    AtomicPromptBuilder,
    _build_success_contract_block,  # noqa: F401  (re-exported for tests/back-compat)
)
from ouroboros.orchestrator.backend_limits import resolve_backend_limits
from ouroboros.orchestrator.context_governor import SiblingStatus, compose_context
from ouroboros.orchestrator.coordinator import CoordinatorReview, LevelCoordinator
from ouroboros.orchestrator.decomposition_params import (
    build_decomposition_system_prompt,
    build_decomposition_user_prompt,
    params_from_profile,
)
from ouroboros.orchestrator.effort_routing import resolve_execute_effort
from ouroboros.orchestrator.events import create_ac_stall_detected_event
from ouroboros.orchestrator.evidence.ac_classification import (  # noqa: F401
    _CODE_IMPLEMENTATION_ACTION_RE,
    _CODE_MUTATION_ACTION_RE,
    _CODE_WORK_SIGNAL_RE,
    _DOC_ONLY_ACTION_RE,
    _DOC_ONLY_TARGET_RE,
    _DOCS_TEST_REFERENCE_RE,
    _EXISTING_VALIDATION_RE,
    _NO_MUTATION_VALIDATION_RE,
    _TEST_MUTATION_WORK_RE,
    _TEST_WORK_RE,
    _VALIDATION_ONLY_ACTION_RE,
    _VALIDATION_ONLY_TEST_SIGNAL_RE,
    _effective_evidence_schema_for_ac,
    _has_mixed_code_and_documentation_work,
    _has_mixed_test_and_documentation_work,
    _has_mixed_validation_and_documentation_work,
    _is_documentation_only_ac,
    _is_validation_only_ac,
    _out_of_scope_evidence_fields_for_ac,
    _out_of_scope_evidence_values_for_ac,
    _profile_with_evidence_schema,
    _scoped_evidence_record_for_ac,
)
from ouroboros.orchestrator.evidence.claims import (  # noqa: F401
    _bash_command_mutates_file_reference,
    _file_claim_matches_runtime_path,
    _file_reference_pattern,
    _runtime_command_value_to_text,
    _runtime_message_command_values,
    _runtime_message_file_path_values,
    _runtime_message_file_proof_text,
    _runtime_message_has_following_success,
    _runtime_message_has_success_evidence,
    _runtime_message_has_success_signal,
    _runtime_message_search_text,
    _runtime_message_supports_command_claim,
    _runtime_message_supports_file_reference,
    _runtime_messages_have_masked_test_command_form,
    _runtime_messages_support_claim,
    _runtime_messages_support_command_claim,
    _runtime_messages_support_file_claim,
    _runtime_support_messages_for_field,
    _text_supports_file_mutation_reference,
    _workspace_relative_file_claim,
)
from ouroboros.orchestrator.evidence.common import (  # noqa: F401
    _MAX_LEAF_RESULT_CHARS,
    _flatten_evidence_values,
    _normalize_command,
    _normalize_exact_command,
    _normalized_evidence_text,
    _truncate_text,
)
from ouroboros.orchestrator.evidence.formatting import (  # noqa: F401
    _build_governed_parent_summary,
    _extract_leaf_evidence_lines,
    _render_ac_section,
    _subtask_event_label,
)
from ouroboros.orchestrator.evidence.runtime_metadata import (  # noqa: F401
    _AC_RUNTIME_OWNERSHIP_METADATA_KEYS,
    _AC_RUNTIME_RESUME_METADATA_KEYS,
    _AC_RUNTIME_SCOPE_METADATA_KEYS,
    _NON_REUSABLE_RUNTIME_EVENT_TYPES,
    _REUSABLE_RUNTIME_EVENT_TYPES,
    _SIBLING_HEADLINE_CHARS,
    _STALL_SENTINEL,
    HEARTBEAT_INTERVAL_SECONDS,
    MAX_STALL_RETRIES,
    STALL_TIMEOUT_SECONDS,
    _SiblingACRef,
)
from ouroboros.orchestrator.evidence.shell_parsing import (  # noqa: F401
    _OUTPUT_FILTER_COMMANDS,
    _TRAILING_REDIRECT_RE,
    _has_gradle_or_maven_test_skip,
    _has_trailing_output_filter_pipeline,
    _is_env_assignment,
    _is_pipefail_parts,
    _is_pipefail_preamble,
    _is_safe_test_command_preamble,
    _looks_like_test_command,
    _looks_like_unittest_command,
    _normalized_command_claim_aliases,
    _normalized_shell_words_text,
    _output_filter_pipeline_is_pipefail_protected,
    _runtime_command_evidence_aliases,
    _segments_after_safe_shell_preamble,
    _segments_after_safe_shell_preamble_with_pipefail,
    _shell_command_body,
    _single_command_after_safe_shell_preamble,
    _single_exact_command_after_safe_shell_preamble,
    _strip_command_output_plumbing,
    _strip_env_prefix,
    _test_command_invocation,
    _test_command_invocation_allowing_output_plumbing,
    _test_invocation_from_prefix,
    _test_invocation_from_shell_body,
    _unittest_command_invocation,
    _uses_pipefail,
)
from ouroboros.orchestrator.evidence.system import (  # noqa: F401
    _MEMORY_CHECK_INTERVAL_SECONDS,
    _MEMORY_WAIT_MAX_SECONDS,
    _MIN_FREE_MEMORY_GB,
    _get_available_memory_gb,
)
from ouroboros.orchestrator.evidence.test_detection import (  # noqa: F401
    _claim_contains_command_success_summary,
    _claim_summary_matches_runtime_chunk,
    _is_tool_result_message,
    _message_contains_test_success,
    _runtime_message_test_proof_text,
    _runtime_messages_have_masked_test_command_for_test_claim,
    _runtime_messages_support_test_claim,
    _successful_runtime_test_commands,
    _test_claim_file_part,
    _test_command_targets_claim,
    _text_contains_test_success,
    _text_contains_unittest_success,
)
from ouroboros.orchestrator.evidence.typed_evidence import (  # noqa: F401
    _add_runtime_command_evidence,
    _complete_sibling_acs_from_evidence,
    _criterion_inline_code_values,
    _criterion_is_exact_command_pass_ac,
    _criterion_is_exact_command_run_ac,
    _criterion_is_exact_file_presence_ac,
    _criterion_satisfied_by_evidence,
    _evidence_values_from_result,
    _typed_evidence_is_usable_for_sibling_reconciliation,
    _typed_file_evidence_proves_current_existence,
)
from ouroboros.orchestrator.evidence.verification import (
    _verify_atomic_evidence_against_runtime_messages,
)
from ouroboros.orchestrator.evidence_schema import (
    EvidenceError,
    EvidenceRecord,
    ProfileEvidenceConfigError,
    ValidationResult,
    extract_evidence,
    validate_evidence,
)
from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    ExecutionNodeIdentity,
    build_ac_runtime_identity,
)
from ouroboros.orchestrator.leaf_dispatcher import (
    LeafDispatcher,
    LeafDispatchState,
)
from ouroboros.orchestrator.level_context import (
    LevelContext,
    deserialize_level_contexts,
    extract_level_context,
    serialize_level_contexts,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
    StageExecutionOutcome,
)
from ouroboros.orchestrator.profile_loader import ExecutionProfile
from ouroboros.orchestrator.rate_limit import (
    RateLimitBackoff,
    RateLimitGate,
    build_rate_limit_gate,
    estimate_runtime_request_tokens,
)
from ouroboros.orchestrator.runtime_param_negotiation import (
    announce_execution_param_degradations,
)
from ouroboros.orchestrator.verifier import (
    Verifier,
    VerifierContractError,
    VerifierVerdict,
    verifier_operational_failure_verdict,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.mcp.types import MCPToolDefinition
    from ouroboros.orchestrator.adapter import AgentRuntime
    from ouroboros.orchestrator.dependency_analyzer import (
        DependencyGraph,
        StagedExecutionPlan,
    )
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)

# Decomposition constants
# Depth >= max_decomposition_depth forces atomic execution as a soft safety net.
DEFAULT_MAX_DECOMPOSITION_DEPTH = 2
MAX_DECOMPOSITION_DEPTH = DEFAULT_MAX_DECOMPOSITION_DEPTH
MIN_SUB_ACS = 2
MAX_SUB_ACS = 5
DECOMPOSITION_TIMEOUT_SECONDS = 60.0
_IMPLEMENTATION_SESSION_KIND = "implementation_session"
_VERIFY_OUTPUT_TAIL_CHARS = 2000  # How much verify-command output to attach


@dataclass(frozen=True)
class _VerifyGateOutcome:
    """Outcome of the orchestrator-run AC success-contract gate (PR-V V1)."""

    passed: bool
    reason: str | None
    output_tail: str
    missing_artifacts: tuple[str, ...] = ()


def _missing_expected_artifacts(artifacts: tuple[str, ...], cwd: str) -> tuple[str, ...]:
    """Return the expected artifacts absent relative to ``cwd``.

    Each entry must resolve to an existing file or directory under ``cwd``.
    Absolute paths and ``..`` escapes are rejected — treated as missing with the
    escape named — so a contract cannot be satisfied by files outside the run
    workspace.
    """
    root = Path(cwd).resolve()
    missing: list[str] = []
    for artifact in artifacts:
        candidate = (root / artifact).resolve()
        if not candidate.is_relative_to(root):
            missing.append(f"{artifact} (escapes workspace)")
            continue
        if not candidate.exists():
            missing.append(artifact)
    return tuple(missing)


def _collect_decomposition_depth_warning_paths(
    result: ACExecutionResult,
    *,
    index_path: tuple[int, ...],
) -> list[str]:
    """Collect dotted AC paths that hit the soft decomposition depth safety net."""
    warning_paths: list[str] = []
    if result.decomposition_depth_warning:
        warning_paths.append(".".join(str(i) for i in index_path))

    for idx, sub_result in enumerate(result.sub_results, start=1):
        warning_paths.extend(
            _collect_decomposition_depth_warning_paths(
                sub_result,
                index_path=index_path + (idx,),
            )
        )
    return warning_paths


def _safe_backend_outcome_weights() -> dict[str, float]:
    """Per-backend outcome weights for the picker tie-break (PR-X X4), never raising.

    The flywheel is a read-only SQLite scan; any failure collapses to no weights
    so a failed AC's cross-harness redispatch is never blocked by it.
    """
    try:
        from ouroboros.orchestrator.backend_outcomes import outcome_weights

        return outcome_weights()
    except Exception:
        return {}


def render_parallel_verification_report(
    parallel_result: ParallelExecutionResult,
    total_acceptance_criteria: int,
    *,
    max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
) -> str:
    """Build the canonical QA artifact for parallel execution results."""
    total_satisfied = parallel_result.success_count + parallel_result.externally_satisfied_count
    lines = [
        "Parallel Execution Verification Report",
        f"Success: {total_satisfied}/{total_acceptance_criteria}",
    ]
    if parallel_result.externally_satisfied_count > 0:
        lines.append(f"Externally Satisfied: {parallel_result.externally_satisfied_count}")
    if parallel_result.failure_count > 0:
        lines.append(f"Failed: {parallel_result.failure_count}")
    if parallel_result.skipped_count > 0:
        lines.append(f"Skipped: {parallel_result.skipped_count}")

    warning_paths: list[str] = []
    for user_facing_idx, result in enumerate(parallel_result.results, start=1):
        warning_paths.extend(
            _collect_decomposition_depth_warning_paths(
                result,
                index_path=(user_facing_idx,),
            )
        )

    if warning_paths:
        feedback_metadata = {
            "feedback_metadata": [
                {
                    "code": "decomposition_depth_warning",
                    "severity": "warning",
                    "message": (
                        "Recursive decomposition reached the soft depth safety net; "
                        "affected leaves were forced to atomic execution."
                    ),
                    "source": "parallel_executor",
                    "details": {
                        "max_depth": max_decomposition_depth,
                        "affected_count": len(warning_paths),
                        "affected_ac_paths": warning_paths,
                    },
                }
            ]
        }
        lines.append("")
        lines.append("## Feedback Metadata")
        lines.append(f"Feedback Metadata JSON: {json.dumps(feedback_metadata, sort_keys=True)}")

    lines.append("")
    lines.append("## Task Results")
    for result in parallel_result.results:
        lines.append("")
        lines.extend(
            _render_ac_section(
                result,
                index_path=(result.ac_index + 1,),
                heading_level=3,
            )
        )
    return "\n".join(lines)


def render_parallel_completion_message(
    parallel_result: ParallelExecutionResult,
    total_acceptance_criteria: int,
) -> str:
    """Build a concise operator-facing completion summary."""
    total_satisfied = parallel_result.success_count + parallel_result.externally_satisfied_count
    lines = [
        "Parallel Execution Complete",
        f"Success: {total_satisfied}/{total_acceptance_criteria}",
    ]
    if parallel_result.externally_satisfied_count > 0:
        lines.append(f"Externally Satisfied: {parallel_result.externally_satisfied_count}")
    if parallel_result.failure_count > 0:
        lines.append(f"Failed: {parallel_result.failure_count}")
    if parallel_result.skipped_count > 0:
        lines.append(f"Skipped: {parallel_result.skipped_count}")

    lines.append("")
    lines.append("Task Status:")
    for result in parallel_result.results:
        if result.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY:
            status = "COMPLETED"
            suffix = " (externally satisfied)"
        else:
            status = "COMPLETED" if result.success else "FAILED"
            suffix = f" ({len(result.sub_results)} subtasks)" if result.is_decomposed else ""
        lines.append(f"- Task {result.ac_index + 1}: [{status}] {result.ac_content}{suffix}")
    return "\n".join(lines)


# =============================================================================
# Parallel Executor
# =============================================================================


class ParallelACExecutor:
    """Executes ACs in parallel based on dependency graph."""

    def __init__(
        self,
        adapter: AgentRuntime,
        event_store: EventStore,
        console: Console | None = None,
        enable_decomposition: bool = True,
        max_concurrent: int = 3,
        max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
        checkpoint_store: Any | None = None,
        inherited_runtime_handle: RuntimeHandle | None = None,
        task_cwd: str | None = None,
        execution_profile: ExecutionProfile | None = None,
        fat_harness_mode: bool = False,
        atomic_verifier: Verifier | None = None,
        reasoning_effort: str | None = None,
        run_verify_commands: bool = True,
        verify_command_timeout_seconds: int = 600,
        ac_retry_attempts: int = 0,
        cross_harness_redispatch: bool | None = None,
    ):
        """Initialize executor.

        Args:
            adapter: Agent runtime for execution.
            event_store: Event store for progress tracking.
            console: Rich console for output.
            enable_decomposition: Enable Claude to decompose complex ACs.
            max_concurrent: Maximum number of concurrent AC executions.
            max_decomposition_depth: Maximum recursive decomposition depth.
            checkpoint_store: Optional CheckpointStore for state recovery (RC3).
            inherited_runtime_handle: Optional parent Claude runtime handle for
                        delegated child executions.
            task_cwd: Explicit working directory override for task execution metadata.
            execution_profile: Optional profile that makes decomposition split along
                profile axis/min_unit instead of the legacy generic prompt.
            fat_harness_mode: Enforce profile typed evidence plus a verifier
                PASS at atomic AC acceptance.
            atomic_verifier: Optional verifier callable for the separate
                atomic evidence PASS gate. Defaults to the harness-owned
                structural verifier.
            run_verify_commands: When True (default), the orchestrator checks
                an AC's success contract itself before accepting the AC: all
                ``spec.expected_artifacts`` must exist under the run workspace
                and ``spec.verify_command`` must exit 0 (plus any
                ``output_assertion``).
            verify_command_timeout_seconds: Timeout for an AC verify command.
            ac_retry_attempts: How many times a failed AC is re-dispatched
                before it is marked FAILED (excludes stall retries). The
                low-level constructor default is 0 so direct/test callers keep
                today's single-dispatch behavior; real run paths (CLI `ooo run`
                via the runner) pass the config value (default 2).
        """
        self._adapter = adapter
        self._event_store = event_store
        self._console = console or Console()
        self._enable_decomposition = enable_decomposition
        self._max_decomposition_depth = max(0, max_decomposition_depth)
        self._inherited_runtime_handle = inherited_runtime_handle
        self._task_cwd = task_cwd
        self._execution_profile = execution_profile
        self._fat_harness_mode = fat_harness_mode
        self._run_verify_commands = run_verify_commands
        self._verify_command_timeout_seconds = max(1, verify_command_timeout_seconds)
        self._ac_retry_attempts = max(0, ac_retry_attempts)
        # Effort-first investment dial (RFC #1405). Base level for full-strength
        # units; decomposed children run one notch lower. ``None`` leaves effort
        # routing dormant (execute_task receives no level → no behavior change),
        # so laying the executor on the capability contract is safe by default.
        self._reasoning_effort = reasoning_effort
        self._atomic_verifier = atomic_verifier
        self._coordinator = LevelCoordinator(
            adapter,
            inherited_runtime_handle=inherited_runtime_handle,
            task_cwd=task_cwd,
        )
        self._semaphore = anyio.Semaphore(max_concurrent)
        self._ac_runtime_handle_manager = ACRuntimeHandleManager(
            adapter,
            event_store,
            task_cwd=task_cwd,
        )
        self._ac_runtime_handles = self._ac_runtime_handle_manager.runtime_handles
        self._event_emitter = ExecutionEventEmitter(
            event_store,
            safe_emit_event=self._safe_emit_event,
        )
        self._checkpoint_store = checkpoint_store
        self._execution_counters_lock = asyncio.Lock()
        self._dispatch_rate_gate = self._build_dispatch_rate_gate(adapter)
        # Param degradations already surfaced this run, keyed by (param, support),
        # so the operator is told once rather than on every dispatch.
        self._announced_param_degradations: set[tuple[str, str]] = set()
        # Cross-harness recovery (PR-X X1): when a terminally failing AC is
        # eligible, redispatch it once onto a different installed runtime before
        # marking it FAILED. ``None`` reads the config flag; the throwaway
        # alternate-runtime executor passes ``False`` as a recursion guard.
        if cross_harness_redispatch is None:
            from ouroboros.config import get_cross_harness_redispatch_enabled

            self._cross_harness_redispatch_enabled = get_cross_harness_redispatch_enabled()
        else:
            self._cross_harness_redispatch_enabled = cross_harness_redispatch
        # AC identities that have already consumed their one alt-harness redispatch.
        self._alt_harness_redispatched_acs: set[str] = set()

    @staticmethod
    def _build_dispatch_rate_gate(adapter: AgentRuntime) -> RateLimitGate:
        """Build the shared dispatch rate gate for non-self-governing backends.

        Ouroboros — not the runtime — paces delivery within the backend's
        declared RPM/TPM budget. Native adapters that already run their own
        shared bucket (Claude) advertise ``self_governs_rate_limit`` and are left
        alone so they are never double-limited. Every other backend gets a gate
        that stays dormant until an RPM/TPM is configured for it (registry,
        ``~/.ouroboros/backend_limits.yaml``, or ``OUROBOROS_<BACKEND>_RPM/TPM``),
        so the default behavior is unchanged.
        """
        backend_attr = getattr(adapter, "runtime_backend", "")
        backend = backend_attr if isinstance(backend_attr, str) and backend_attr else "unknown"

        if getattr(adapter, "self_governs_rate_limit", False):
            return build_rate_limit_gate(backend, request_limit=None, token_limit=None)

        limits = resolve_backend_limits(backend)
        return build_rate_limit_gate(
            backend,
            request_limit=limits.requests_per_minute,
            token_limit=limits.tokens_per_minute,
        )

    async def _await_dispatch_rate_budget(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
    ) -> None:
        """Wait for shared rate-limit headroom before dispatching a runtime call.

        No-op when the gate is dormant (the default for backends with no
        configured RPM/TPM). When active, paces dispatch across all concurrent
        workers (they share this executor's single gate instance) and logs each
        backoff for observability.
        """
        if not self._dispatch_rate_gate.enabled:
            return

        estimated_tokens = estimate_runtime_request_tokens(prompt, system_prompt=system_prompt)

        def _log_backoff(backoff: RateLimitBackoff) -> None:
            log.info(
                "orchestrator.parallel_executor.rate_limit_backoff",
                runtime_backend=backoff.snapshot.runtime_backend,
                forced=backoff.forced,
                wait_seconds=backoff.wait_seconds,
                total_waited=backoff.total_waited,
                requests_in_window=backoff.snapshot.requests_in_window,
                request_limit=backoff.snapshot.request_limit,
                tokens_in_window=backoff.snapshot.tokens_in_window,
                token_limit=backoff.snapshot.token_limit,
            )

        await self._dispatch_rate_gate.acquire(estimated_tokens, on_backoff=_log_backoff)

    def _announce_param_degradations(
        self,
        *,
        system_prompt: str | None,
        tools: list[str] | None,
    ) -> None:
        """Surface (once per run) execution params the runtime won't honor natively.

        Observability only — nothing here changes what is passed to the runtime.
        It makes previously silent degradation (e.g. a CLI runtime folding the
        system prompt into the user message) visible in logs and the console.
        """
        announce_execution_param_degradations(
            self._adapter,
            system_prompt=system_prompt,
            tools=tools,
            announced=self._announced_param_degradations,
            console=self._console,
            log_event="orchestrator.parallel_executor.param_degraded",
        )

    def _flush_console(self) -> None:
        """Flush console output to ensure progress is visible immediately."""
        if hasattr(self._console, "file") and hasattr(self._console.file, "flush"):
            try:
                self._console.file.flush()
            except (OSError, ValueError):
                pass

    async def _safe_emit_event(self, event: Any, max_retries: int = 3) -> bool:
        """Emit event with retry on failure (RC5).

        Retries with exponential backoff to handle transient DB lock errors.
        On permanent failure, logs error AND prints a console warning so the
        operator is aware of event persistence degradation.

        Args:
            event: BaseEvent to persist.
            max_retries: Maximum number of attempts.

        Returns:
            True if event was written, False if all retries failed.
        """
        for attempt in range(max_retries):
            try:
                await self._event_store.append(event)
                return True
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = min(1.0 * (2**attempt), 5.0)
                    log.warning(
                        "parallel_executor.event_write.retry",
                        event_type=event.type,
                        attempt=attempt + 1,
                        error=str(e),
                    )
                    await anyio.sleep(wait)
                else:
                    log.error(
                        "parallel_executor.event_write.failed",
                        event_type=event.type,
                        attempts=max_retries,
                        error=str(e),
                    )
                    self._console.print(
                        f"  [yellow]Event persistence degraded: "
                        f"{event.type} dropped after {max_retries} retries[/yellow]"
                    )
        return False

    @staticmethod
    def _build_expected_ac_runtime_metadata(
        runtime_scope: Any,
        *,
        ac_index: int,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        node_identity: ExecutionNodeIdentity | None,
        retry_attempt: int,
    ) -> dict[str, Any]:
        return ACRuntimeHandleManager._build_expected_ac_runtime_metadata(
            runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )

    @staticmethod
    def _metadata_value_matches_expected_scope(
        key: str,
        observed_value: Any,
        expected_metadata: dict[str, Any],
    ) -> bool:
        return ACRuntimeHandleManager._metadata_value_matches_expected_scope(
            key,
            observed_value,
            expected_metadata,
        )

    @staticmethod
    def _runtime_handle_claims_foreign_ac_scope(
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        is_sub_ac: bool,
    ) -> bool:
        return ACRuntimeHandleManager._runtime_handle_claims_foreign_ac_scope(
            runtime_handle,
            expected_metadata=expected_metadata,
            is_sub_ac=is_sub_ac,
        )

    @classmethod
    def _runtime_handle_matches_ac_scope_for_resume(
        cls,
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        is_sub_ac: bool,
    ) -> bool:
        return ACRuntimeHandleManager._runtime_handle_matches_ac_scope_for_resume(
            runtime_handle,
            expected_metadata=expected_metadata,
            is_sub_ac=is_sub_ac,
        )

    @staticmethod
    def _bind_runtime_handle_to_ac_scope(
        runtime_handle: RuntimeHandle | None,
        *,
        expected_metadata: dict[str, Any],
        scrub_resume_state: bool = False,
    ) -> RuntimeHandle | None:
        return ACRuntimeHandleManager._bind_runtime_handle_to_ac_scope(
            runtime_handle,
            expected_metadata=expected_metadata,
            scrub_resume_state=scrub_resume_state,
        )

    def _normalize_ac_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_scope: Any,
        ac_index: int,
        is_sub_ac: bool,
        parent_ac_index: int | None,
        sub_ac_index: int | None,
        node_identity: ExecutionNodeIdentity | None,
        retry_attempt: int,
        source: str,
        require_resume_scope_match: bool,
    ) -> RuntimeHandle | None:
        return self._ac_runtime_handle_manager._normalize_ac_runtime_handle(
            runtime_handle,
            runtime_scope=runtime_scope,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            source=source,
            require_resume_scope_match=require_resume_scope_match,
        )

    def _build_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
        tool_catalog: tuple[MCPToolDefinition, ...] | None = None,
    ) -> RuntimeHandle | None:
        return self._ac_runtime_handle_manager._build_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            tool_catalog=tool_catalog,
        )

    async def _load_persisted_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
    ) -> RuntimeHandle | None:
        return await self._ac_runtime_handle_manager._load_persisted_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )

    def _remember_ac_runtime_handle(
        self,
        ac_index: int,
        runtime_handle: RuntimeHandle | None,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
    ) -> RuntimeHandle | None:
        return self._ac_runtime_handle_manager._remember_ac_runtime_handle(
            ac_index,
            runtime_handle,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )

    def _forget_ac_runtime_handle(
        self,
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
    ) -> None:
        self._ac_runtime_handle_manager._forget_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )

    async def _terminate_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_scope_id: str,
    ) -> None:
        await self._ac_runtime_handle_manager._terminate_runtime_handle(
            runtime_handle,
            runtime_scope_id=runtime_scope_id,
        )

    @staticmethod
    def _resolve_ac_runtime_identity(
        ac_index: int,
        *,
        execution_context_id: str | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_attempt: int = 0,
    ) -> ACRuntimeIdentity:
        return ACRuntimeHandleManager._resolve_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )

    @staticmethod
    def _event_matches_ac_runtime_identity(
        event_data: dict[str, Any],
        runtime_identity: ACRuntimeIdentity,
    ) -> bool:
        return ACRuntimeHandleManager._event_matches_ac_runtime_identity(
            event_data,
            runtime_identity,
        )

    @staticmethod
    def _default_turn_id(
        runtime_identity: ACRuntimeIdentity,
        turn_number: int,
    ) -> str:
        return ACRuntimeHandleManager._default_turn_id(runtime_identity, turn_number)

    @staticmethod
    def _runtime_turn_number(runtime_handle: RuntimeHandle | None) -> int:
        return ACRuntimeHandleManager._runtime_turn_number(runtime_handle)

    @classmethod
    def _runtime_turn_id(
        cls,
        runtime_handle: RuntimeHandle | None,
        *,
        runtime_identity: ACRuntimeIdentity,
    ) -> str:
        return ACRuntimeHandleManager._runtime_turn_id(
            runtime_handle,
            runtime_identity=runtime_identity,
        )

    @staticmethod
    def _runtime_recovery_discontinuity(
        runtime_handle: RuntimeHandle | None,
    ) -> dict[str, Any] | None:
        return ACRuntimeHandleManager._runtime_recovery_discontinuity(runtime_handle)

    @classmethod
    def _runtime_handle_same_session(
        cls,
        previous_handle: RuntimeHandle | None,
        current_handle: RuntimeHandle | None,
    ) -> bool:
        return ACRuntimeHandleManager._runtime_handle_same_session(
            previous_handle,
            current_handle,
        )

    @classmethod
    def _build_recovery_discontinuity(
        cls,
        *,
        previous_handle: RuntimeHandle | None,
        current_handle: RuntimeHandle,
        runtime_identity: ACRuntimeIdentity,
    ) -> dict[str, Any] | None:
        return ACRuntimeHandleManager._build_recovery_discontinuity(
            previous_handle=previous_handle,
            current_handle=current_handle,
            runtime_identity=runtime_identity,
        )

    @classmethod
    def _augment_ac_runtime_handle(
        cls,
        runtime_handle: RuntimeHandle,
        *,
        runtime_identity: ACRuntimeIdentity,
        previous_handle: RuntimeHandle | None,
    ) -> RuntimeHandle:
        return ACRuntimeHandleManager._augment_ac_runtime_handle(
            runtime_handle,
            runtime_identity=runtime_identity,
            previous_handle=previous_handle,
        )

    @staticmethod
    def _with_native_session_id(
        runtime_handle: RuntimeHandle | None,
        native_session_id: str | None,
    ) -> RuntimeHandle | None:
        return ACRuntimeHandleManager._with_native_session_id(runtime_handle, native_session_id)

    @staticmethod
    def _is_resumable_runtime_handle(runtime_handle: RuntimeHandle | None) -> bool:
        return ACRuntimeHandleManager._is_resumable_runtime_handle(runtime_handle)

    @staticmethod
    def _runtime_resume_session_id(runtime_handle: RuntimeHandle | None) -> str | None:
        return ACRuntimeHandleManager._runtime_resume_session_id(runtime_handle)

    async def _emit_ac_runtime_event(
        self,
        *,
        event_type: str,
        runtime_identity: ACRuntimeIdentity,
        ac_content: str,
        runtime_handle: RuntimeHandle | None,
        execution_id: str | None = None,
        session_id: str | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        error: str | None = None,
    ) -> None:
        await self._ac_runtime_handle_manager._emit_ac_runtime_event(
            event_type=event_type,
            runtime_identity=runtime_identity,
            ac_content=ac_content,
            runtime_handle=runtime_handle,
            execution_id=execution_id,
            session_id=session_id,
            result_summary=result_summary,
            success=success,
            error=error,
        )

    @staticmethod
    def _coerce_ac_indices(raw_indices: Any) -> tuple[int, ...]:
        """Normalize a stage or batch AC index payload into an ordered tuple."""
        if raw_indices is None:
            return ()
        if isinstance(raw_indices, int):
            return (raw_indices,)

        indices: list[int] = []
        for candidate in raw_indices:
            if isinstance(candidate, int):
                indices.append(candidate)
        return tuple(indices)

    def _get_stage_batches(self, stage: Any) -> tuple[tuple[int, ...], ...]:
        """Return normalized batch AC groupings for a stage."""
        raw_batches = getattr(stage, "batches", None)
        if raw_batches:
            batches = tuple(
                batch_indices
                for batch_indices in (
                    self._coerce_ac_indices(getattr(batch, "ac_indices", batch))
                    for batch in raw_batches
                )
                if batch_indices
            )
            if batches:
                return batches

        ac_indices = self._coerce_ac_indices(getattr(stage, "ac_indices", ()))
        return (ac_indices,) if ac_indices else ()

    def _get_stage_ac_indices(self, stage: Any) -> tuple[int, ...]:
        """Return the ordered AC indices covered by a stage."""
        ac_indices = self._coerce_ac_indices(getattr(stage, "ac_indices", ()))
        if ac_indices:
            return ac_indices

        ordered_indices: list[int] = []
        seen_indices: set[int] = set()
        for batch in self._get_stage_batches(stage):
            for ac_index in batch:
                if ac_index in seen_indices:
                    continue
                seen_indices.add(ac_index)
                ordered_indices.append(ac_index)
        return tuple(ordered_indices)

    async def _execute_ac_batch(
        self,
        *,
        seed: Seed,
        batch_indices: list[int],
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        ac_retry_attempts: dict[int, int],
        execution_counters: dict[str, int] | None = None,
        retry_prompts: dict[int, str] | None = None,
        same_runtime_budget_exhausted: bool = True,
    ) -> list[ACExecutionResult | BaseException]:
        """Execute one batch of stage-ready ACs using the shared worker pool.

        ``same_runtime_budget_exhausted`` is forwarded to every AC in the batch:
        it is ``True`` only on the batch attempt that spends the AC's configured
        same-runtime retry budget, gating cross-harness redispatch (PR-X X1) so
        it never pre-empts those retries.
        """
        batch_results: list[ACExecutionResult | BaseException] = [None] * len(batch_indices)
        sibling_acs: list[_SiblingACRef] = (
            [(i, ac_text(seed.acceptance_criteria[i])) for i in batch_indices]
            if len(batch_indices) > 1
            else []
        )

        async def _run_ac(idx: int, ac_idx: int) -> None:
            async with self._semaphore:
                try:
                    ac_criterion = seed.acceptance_criteria[ac_idx]
                    batch_results[idx] = await self._execute_single_ac(
                        ac_index=ac_idx,
                        ac_content=ac_text(ac_criterion),
                        session_id=session_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        seed_goal=seed.goal,
                        depth=0,
                        execution_id=execution_id,
                        level_contexts=level_contexts,
                        sibling_acs=sibling_acs,
                        retry_attempt=ac_retry_attempts[ac_idx],
                        execution_counters=execution_counters,
                        retry_prompt_extra=(retry_prompts or {}).get(ac_idx, ""),
                        same_runtime_budget_exhausted=same_runtime_budget_exhausted,
                        ac_spec=(
                            ac_criterion
                            if isinstance(ac_criterion, AcceptanceCriterionSpec)
                            else None
                        ),
                    )
                except BaseException as e:
                    # Never suppress anyio Cancelled — doing so breaks
                    # the task group's cancel-scope propagation and can
                    # cause the entire group to hang indefinitely.
                    if isinstance(e, anyio.get_cancelled_exc_class()):
                        raise
                    batch_results[idx] = e

        # Cross-AC concurrency is governed by the LevelCoordinator's
        # file-conflict guard, not by session-level tool catalog presence.
        # Tool-call-level serialization (same runtime session cannot invoke
        # ISOLATED_SESSION_REQUIRED capabilities concurrently) is enforced by
        # the provider runtime, which is the correct layer: the batch
        # scheduler does not know which ACs will actually invoke which tools.
        async with anyio.create_task_group() as tg:
            for idx, ac_idx in enumerate(batch_indices):
                tg.start_soon(_run_ac, idx, ac_idx)

        return batch_results

    async def execute_parallel(
        self,
        seed: Seed,
        *,
        session_id: str,
        execution_id: str,
        tools: list[str],
        system_prompt: str,
        tool_catalog: tuple[MCPToolDefinition, ...] | None = None,
        dependency_graph: DependencyGraph | None = None,
        execution_plan: StagedExecutionPlan | None = None,
        reconciled_level_contexts: list[LevelContext] | None = None,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
    ) -> ParallelExecutionResult:
        """Execute ACs according to a staged execution plan.

        Args:
            seed: Seed specification.
            execution_plan: Staged execution plan defining serial stages.
            session_id: Parent session ID for tracking.
            execution_id: Execution ID for event tracking.
            tools: Tools available to agents.
            system_prompt: System prompt for agents.
            dependency_graph: Legacy fallback used to derive ``execution_plan``.
            reconciled_level_contexts: Existing post-reconcile stage contexts
                from a previous execution attempt. Reopened ACs receive these
                as prompt context so they continue from the current shared
                workspace state instead of the original failed-attempt state.
            externally_satisfied_acs: Top-level ACs already satisfied by the
                current working tree and therefore skipped for re-execution.

        Returns:
            ParallelExecutionResult with outcomes for all ACs.
        """
        if execution_plan is None:
            if dependency_graph is None:
                msg = "execution_plan is required when dependency_graph is not provided"
                raise ValueError(msg)
            execution_plan = dependency_graph.to_execution_plan()

        start_time = datetime.now(UTC)
        all_results: list[ACExecutionResult] = []
        failed_indices: set[int] = set()
        blocked_indices: set[int] = set()
        stage_results: list[ParallelExecutionStageResult] = []
        level_contexts = list(reconciled_level_contexts or [])

        total_levels = execution_plan.total_stages
        total_acs = len(seed.acceptance_criteria)
        external_completed = externally_satisfied_acs or {}
        execution_counters = {
            "messages_count": 0,
            "tool_calls_count": 0,
        }

        # Track AC statuses for TUI updates
        ac_statuses: dict[int, str] = dict.fromkeys(range(total_acs), "pending")
        ac_retry_attempts: dict[int, int] = dict.fromkeys(range(total_acs), 0)
        completed_count = 0
        resume_from_level = 0

        # RC3: Attempt to recover from checkpoint
        if self._checkpoint_store:
            try:
                seed_id = getattr(seed, "id", session_id)
                load_result = self._checkpoint_store.load(seed_id)
                if hasattr(load_result, "is_ok") and load_result.is_ok and load_result.value:
                    cp = load_result.value
                    if cp.phase == "parallel_execution":
                        resume_from_level = cp.state.get("completed_levels", 0)
                        for idx, status in cp.state.get("ac_statuses", {}).items():
                            ac_statuses[int(idx)] = status
                        for idx in cp.state.get("failed_indices", []):
                            failed_indices.add(int(idx))
                        completed_count = cp.state.get("completed_count", 0)
                        # Restore level contexts so subsequent levels
                        # have access to completed levels' output
                        saved_contexts = cp.state.get("level_contexts", [])
                        if saved_contexts:
                            level_contexts = deserialize_level_contexts(saved_contexts)
                        log.info(
                            "parallel_executor.recovery.resuming",
                            from_level=resume_from_level,
                            seed_id=seed_id,
                            restored_contexts=len(level_contexts),
                        )
                        # Reconstruct all_results for completed/failed/skipped ACs.
                        for prev_stage in execution_plan.stages[:resume_from_level]:
                            for ac_idx in self._get_stage_ac_indices(prev_stage):
                                if ac_idx >= total_acs:
                                    continue
                                status = ac_statuses.get(ac_idx, "pending")
                                is_completed = status == "completed"
                                is_skipped = status == "skipped"
                                all_results.append(
                                    ACExecutionResult(
                                        ac_index=ac_idx,
                                        ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                                        success=is_completed,
                                        final_message=(
                                            "[Restored from checkpoint]" if is_completed else ""
                                        ),
                                        error=(
                                            "Skipped: dependency failed"
                                            if is_skipped
                                            else None
                                            if is_completed
                                            else "Failed (restored from checkpoint)"
                                        ),
                                        retry_attempt=ac_retry_attempts.get(ac_idx, 0),
                                    )
                                )
                        self._console.print(
                            f"[cyan]Resuming from level {resume_from_level + 1} "
                            f"(checkpoint recovered, "
                            f"{len(level_contexts)} level context(s) restored)[/cyan]"
                        )
            except Exception as e:
                log.warning(
                    "parallel_executor.recovery.failed",
                    error=str(e),
                )

        # Validation: check all AC indices are present in dependency graph
        expected_indices = set(range(total_acs))
        actual_indices = {
            idx for stage in execution_plan.stages for idx in self._get_stage_ac_indices(stage)
        }
        missing_indices = expected_indices - actual_indices
        extra_indices = actual_indices - expected_indices

        if missing_indices:
            log.warning(
                "parallel_executor.missing_ac_indices",
                session_id=session_id,
                missing=sorted(missing_indices),
            )
            # Add missing ACs to results as errors
            for idx in sorted(missing_indices):
                all_results.append(
                    ACExecutionResult(
                        ac_index=idx,
                        ac_content=ac_text(seed.acceptance_criteria[idx]),
                        success=False,
                        error="Not included in dependency graph",
                        retry_attempt=ac_retry_attempts[idx],
                        outcome=ACExecutionOutcome.INVALID,
                    )
                )

        if extra_indices:
            log.error(
                "parallel_executor.invalid_ac_indices",
                session_id=session_id,
                extra=sorted(extra_indices),
                max_valid=total_acs - 1,
            )
            # Invalid indices will be skipped in the execution loop below

        dependency_edges = [
            {"ac_index": idx, "depends_on": deps}
            for idx in range(total_acs)
            if (deps := tuple(execution_plan.get_dependencies(idx)))
        ]
        log.info(
            "parallel_executor.execution.started",
            session_id=session_id,
            total_acs=total_acs,
            total_levels=total_levels,
            levels=execution_plan.execution_levels,
        )
        log.info(
            "parallel_executor.dependency_graph",
            session_id=session_id,
            execution_id=execution_id,
            total_acs=total_acs,
            dependency_edges=dependency_edges,
        )

        # Emit initial progress for TUI
        await self._emit_workflow_progress(
            session_id=session_id,
            execution_id=execution_id,
            seed=seed,
            ac_statuses=ac_statuses,
            ac_retry_attempts=ac_retry_attempts,
            executing_indices=[],
            completed_count=completed_count,
            current_level=resume_from_level + 1,
            total_levels=total_levels,
            activity="Starting parallel execution",
            messages_count=execution_counters["messages_count"],
            tool_calls_count=execution_counters["tool_calls_count"],
        )

        # RC2+RC4: Shared state for resilient progress emitter
        progress_state: dict[str, int] = {
            "current_level": resume_from_level + 1,
            "total_levels": total_levels,
        }

        # Execute groups sequentially, but ACs within each group in parallel.
        # The resilient progress emitter runs as a sibling background task
        # and is automatically cancelled when the execution loop finishes.
        async with anyio.create_task_group() as outer_tg:
            outer_tg.start_soon(
                self._resilient_progress_emitter,
                session_id,
                execution_id,
                seed,
                ac_statuses,
                progress_state,
            )

            for stage in execution_plan.stages:
                level_idx = stage.index
                level = self._get_stage_ac_indices(stage)
                stage_batches = self._get_stage_batches(stage)
                level_num = level_idx + 1

                # RC3: Skip already-completed levels on recovery
                if level_idx < resume_from_level:
                    log.info(
                        "parallel_executor.recovery.skipping_level",
                        level=level_num,
                    )
                    continue

                # Update shared progress state for background emitter
                progress_state["current_level"] = level_num

                # Check for blocked ACs (dependencies failed or were blocked upstream)
                executable: list[int] = []
                blocked: list[int] = []
                externally_satisfied: list[int] = []
                stage_ac_results: list[ACExecutionResult] = []

                for ac_idx in level:
                    # Skip invalid indices
                    if ac_idx < 0 or ac_idx >= total_acs:
                        continue

                    # Always validate dependencies first — even externally
                    # satisfied ACs must be blocked if their upstream
                    # dependencies failed, because the "satisfied" state may
                    # be stale relative to the current execution.
                    deps = execution_plan.get_dependencies(ac_idx)
                    if any(dep in failed_indices or dep in blocked_indices for dep in deps):
                        blocked.append(ac_idx)
                    elif ac_idx in external_completed:
                        externally_satisfied.append(ac_idx)
                    else:
                        executable.append(ac_idx)

                level_success = 0
                level_failed = 0

                for ac_idx in externally_satisfied:
                    metadata = external_completed.get(ac_idx, {})
                    reason = metadata.get("reason")
                    commit = metadata.get("commit")

                    # PR-V V4: --skip-completed trusts working-tree state. When the
                    # AC carries a success contract (verify_command OR expected
                    # artifacts), prove it with the gate before skipping; on gate
                    # failure, execute the AC normally instead.
                    spec = seed.acceptance_criteria[ac_idx]
                    verification_status = "assumed"
                    if (
                        self._run_verify_commands
                        and isinstance(spec, AcceptanceCriterionSpec)
                        and (spec.verify_command or spec.expected_artifacts)
                    ):
                        cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
                        gate = await self._run_ac_verify_gate(spec=spec, cwd=cwd)
                        if not gate.passed:
                            executable.append(ac_idx)
                            log.info(
                                "parallel_executor.ac.skip_completed_gate_failed",
                                session_id=session_id,
                                ac_index=ac_idx,
                                reason=gate.reason,
                            )
                            continue
                        verification_status = "verified"

                    notes: list[str] = [
                        "Skipped via --skip-completed; existing working tree state is treated as satisfied."
                    ]
                    if isinstance(reason, str) and reason.strip():
                        notes.append(f"Reason: {reason.strip()}")
                    if isinstance(commit, str) and commit.strip():
                        notes.append(f"Commit: {commit.strip()}")
                    notes.append(f"verification_status={verification_status}")

                    satisfied_result = ACExecutionResult(
                        ac_index=ac_idx,
                        ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                        success=True,
                        final_message="\n".join(notes),
                        retry_attempt=ac_retry_attempts[ac_idx],
                        outcome=ACExecutionOutcome.SATISFIED_EXTERNALLY,
                    )
                    all_results.append(satisfied_result)
                    stage_ac_results.append(satisfied_result)
                    ac_statuses[ac_idx] = "completed"
                    completed_count += 1
                    level_success += 1
                    log.info(
                        "parallel_executor.ac.satisfied_externally",
                        session_id=session_id,
                        ac_index=ac_idx,
                        reason=reason,
                        commit=commit,
                    )

                # Add blocked results
                for ac_idx in blocked:
                    blocked_result = ACExecutionResult(
                        ac_index=ac_idx,
                        ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                        success=False,
                        error="Skipped: dependency failed",
                        retry_attempt=ac_retry_attempts[ac_idx],
                        outcome=ACExecutionOutcome.BLOCKED,
                    )
                    all_results.append(blocked_result)
                    stage_ac_results.append(blocked_result)
                    blocked_indices.add(ac_idx)
                    ac_statuses[ac_idx] = "skipped"
                    log.info(
                        "parallel_executor.ac.skipped",
                        session_id=session_id,
                        ac_index=ac_idx,
                        reason="dependency_failed",
                    )

                if not executable:
                    stage_started = bool(externally_satisfied)
                    stage_result = ParallelExecutionStageResult(
                        stage_index=level_idx,
                        ac_indices=tuple(level),
                        results=tuple(sorted(stage_ac_results, key=lambda result: result.ac_index)),
                        started=stage_started,
                    )
                    stage_results.append(stage_result)
                    await self._emit_level_completed(
                        session_id=session_id,
                        level=level_num,
                        success_count=stage_result.success_count,
                        failure_count=stage_result.failure_count,
                        blocked_count=stage_result.blocked_count,
                        started=stage_started,
                        outcome=stage_result.outcome.value,
                    )
                    continue

                # Mark ACs as executing
                for ac_idx in executable:
                    ac_statuses[ac_idx] = "executing"

                self._console.print(
                    f"\n[cyan]Level {level_num}/{total_levels}: "
                    f"Executing ACs {[idx + 1 for idx in executable]} in parallel[/cyan]"
                )
                self._flush_console()

                # Emit level started event
                await self._emit_level_started(
                    session_id=session_id,
                    level=level_num,
                    ac_indices=executable,
                    total_levels=total_levels,
                )

                # Capture current contexts for this level's closure
                current_contexts = list(level_contexts)

                for batch_index, batch in enumerate(stage_batches, start=1):
                    batch_executable = [ac_idx for ac_idx in batch if ac_idx in executable]
                    if not batch_executable:
                        continue

                    for ac_idx in batch_executable:
                        ac_statuses[ac_idx] = "executing"

                    if len(stage_batches) > 1:
                        self._console.print(
                            f"  [cyan]Batch {batch_index}/{len(stage_batches)}: "
                            f"ACs {[idx + 1 for idx in batch_executable]}[/cyan]"
                        )
                        self._flush_console()

                    await self._emit_workflow_progress(
                        session_id=session_id,
                        execution_id=execution_id,
                        seed=seed,
                        ac_statuses=ac_statuses,
                        ac_retry_attempts=ac_retry_attempts,
                        executing_indices=batch_executable,
                        completed_count=completed_count,
                        current_level=level_num,
                        total_levels=total_levels,
                        activity="Executing",
                        messages_count=execution_counters["messages_count"],
                        tool_calls_count=execution_counters["tool_calls_count"],
                    )

                    batch_results = await self._run_batch_with_verify_and_retry(
                        seed=seed,
                        batch_executable=batch_executable,
                        session_id=session_id,
                        execution_id=execution_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        level_contexts=current_contexts,
                        ac_retry_attempts=ac_retry_attempts,
                        execution_counters=execution_counters,
                    )

                    for ac_idx, result in zip(batch_executable, batch_results, strict=False):
                        if isinstance(result, BaseException):
                            # Exception during execution
                            error_msg = str(result)
                            ac_result = ACExecutionResult(
                                ac_index=ac_idx,
                                ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                                success=False,
                                error=error_msg,
                                retry_attempt=ac_retry_attempts[ac_idx],
                                outcome=ACExecutionOutcome.FAILED,
                            )
                            failed_indices.add(ac_idx)
                            level_failed += 1
                            ac_statuses[ac_idx] = "failed"

                            log.error(
                                "parallel_executor.ac.exception",
                                session_id=session_id,
                                ac_index=ac_idx,
                                error=error_msg,
                            )
                        elif (
                            isinstance(result, ACExecutionResult)
                            and result.error == _STALL_SENTINEL
                        ):
                            # Stalled AC — treat as permanent failure at batch level
                            ac_id = f"ac_{ac_idx}"
                            await self._safe_emit_event(
                                create_ac_stall_detected_event(
                                    session_id=session_id,
                                    ac_index=ac_idx,
                                    ac_id=ac_id,
                                    silent_seconds=STALL_TIMEOUT_SECONDS,
                                    attempt=1,
                                    max_attempts=1,
                                    action="abandon",
                                )
                            )
                            ac_result = ACExecutionResult(
                                ac_index=ac_idx,
                                ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                                success=False,
                                error=(f"Stalled (no activity for {STALL_TIMEOUT_SECONDS:.0f}s)"),
                                retry_attempt=ac_retry_attempts[ac_idx],
                                outcome=ACExecutionOutcome.FAILED,
                            )
                            failed_indices.add(ac_idx)
                            level_failed += 1
                            ac_statuses[ac_idx] = "failed"
                            log.error(
                                "parallel_executor.ac.stall_abandoned",
                                session_id=session_id,
                                ac_index=ac_idx,
                            )
                        else:
                            ac_result = result
                            if ac_result.success:
                                level_success += 1
                                ac_statuses[ac_idx] = "completed"
                                completed_count += 1
                            elif ac_result.is_blocked:
                                blocked_indices.add(ac_idx)
                                ac_statuses[ac_idx] = "skipped"
                            else:
                                failed_indices.add(ac_idx)
                                level_failed += 1
                                ac_statuses[ac_idx] = "failed"

                        all_results.append(ac_result)
                        stage_ac_results.append(ac_result)

                flip_gated_out = await self._compute_sibling_flip_gated_out(
                    seed=seed,
                    level_results=stage_ac_results,
                    session_id=session_id,
                    execution_id=execution_id,
                )
                (
                    completed_count,
                    level_success,
                    level_failed,
                    stage_ac_results,
                ) = _complete_sibling_acs_from_evidence(
                    level_results=stage_ac_results,
                    ac_statuses=ac_statuses,
                    failed_indices=failed_indices,
                    completed_count=completed_count,
                    level_success=level_success,
                    level_failed=level_failed,
                    flip_gated_out=flip_gated_out,
                )

                reconciled_by_index = {result.ac_index: result for result in stage_ac_results}
                all_results = [
                    reconciled_by_index.get(result.ac_index, result) for result in all_results
                ]

                stage_result = ParallelExecutionStageResult(
                    stage_index=level_idx,
                    ac_indices=tuple(level),
                    results=tuple(sorted(stage_ac_results, key=lambda result: result.ac_index)),
                    started=True,
                )

                # Emit level completed event
                await self._emit_level_completed(
                    session_id=session_id,
                    level=level_num,
                    success_count=level_success,
                    failure_count=level_failed,
                    blocked_count=stage_result.blocked_count,
                    started=True,
                    outcome=stage_result.outcome.value,
                )

                # Emit progress after level completes
                await self._emit_workflow_progress(
                    session_id=session_id,
                    execution_id=execution_id,
                    seed=seed,
                    ac_statuses=ac_statuses,
                    ac_retry_attempts=ac_retry_attempts,
                    executing_indices=[],
                    completed_count=completed_count,
                    current_level=level_num,
                    total_levels=total_levels,
                    activity=f"Level {level_num} complete",
                    messages_count=execution_counters["messages_count"],
                    tool_calls_count=execution_counters["tool_calls_count"],
                )

                self._console.print(
                    f"[green]Level {level_num} complete: "
                    f"{level_success} succeeded, {level_failed} failed[/green]"
                )
                self._flush_console()

                # Extract context from this level for next level's ACs
                if executable and level_success > 0:
                    level_ac_data = [
                        (r.ac_index, r.ac_content, r.success, r.messages, r.final_message)
                        for r in stage_ac_results
                        if r.ac_index in executable
                    ]
                    # workspace_root is required: fall back through
                    # adapter working directory, then process cwd. Never None.
                    workspace_root = (
                        self._task_cwd or self._adapter.working_directory or os.getcwd()
                    )
                    level_ctx = extract_level_context(
                        level_ac_data,
                        level_num,
                        workspace_root=workspace_root,
                    )

                    # Coordinator: detect and resolve file conflicts (Approach A)
                    level_ac_results = [r for r in stage_ac_results if r.ac_index in executable]
                    conflicts = self._coordinator.detect_file_conflicts(level_ac_results)

                    if conflicts:
                        self._console.print(
                            f"  [yellow]Coordinator: {len(conflicts)} file conflict(s) detected, "
                            f"starting review...[/yellow]"
                        )
                        await self._emit_coordinator_started(
                            execution_id=execution_id,
                            session_id=session_id,
                            level=level_num,
                            conflicts=conflicts,
                        )
                        review = await self._coordinator.run_review(
                            execution_id=execution_id,
                            conflicts=conflicts,
                            level_context=level_ctx,
                            level_number=level_num,
                        )
                        await self._emit_coordinator_runtime_events(
                            execution_id=execution_id,
                            session_id=session_id,
                            review=review,
                        )
                        await self._emit_coordinator_completed(
                            execution_id=execution_id,
                            session_id=session_id,
                            review=review,
                        )
                        # Attach review to the level context
                        level_ctx = LevelContext(
                            level_number=level_ctx.level_number,
                            completed_acs=level_ctx.completed_acs,
                            coordinator_review=review,
                        )
                        stage_result = replace(stage_result, coordinator_review=review)
                        self._console.print(
                            f"  [green]Coordinator review complete: "
                            f"{len(review.fixes_applied)} fix(es), "
                            f"{len(review.warnings_for_next_level)} warning(s)[/green]"
                        )

                    level_contexts.append(level_ctx)
                stage_results.append(stage_result)

                # RC3: Save checkpoint after each level completion
                if self._checkpoint_store:
                    try:
                        from ouroboros.persistence.checkpoint import CheckpointData

                        seed_id = getattr(seed, "id", session_id)
                        checkpoint = CheckpointData.create(
                            seed_id=seed_id,
                            phase="parallel_execution",
                            state={
                                "session_id": session_id,
                                "execution_id": execution_id,
                                "completed_levels": level_idx + 1,
                                "ac_statuses": {str(k): v for k, v in ac_statuses.items()},
                                "failed_indices": sorted(failed_indices),
                                "completed_count": completed_count,
                                "level_contexts": serialize_level_contexts(level_contexts),
                            },
                        )
                        save_result = self._checkpoint_store.save(checkpoint)
                        if hasattr(save_result, "is_ok") and save_result.is_ok:
                            log.info(
                                "parallel_executor.checkpoint.saved",
                                level=level_num,
                                seed_id=seed_id,
                            )
                        else:
                            err_msg = (
                                str(save_result.error)
                                if hasattr(save_result, "error")
                                else "unknown error"
                            )
                            log.warning(
                                "parallel_executor.checkpoint.save_failed",
                                level=level_num,
                                seed_id=seed_id,
                                error=err_msg,
                            )
                            self._console.print(
                                f"  [yellow]Checkpoint save failed for level "
                                f"{level_num}: {err_msg}[/yellow]"
                            )
                    except Exception as e:
                        log.warning(
                            "parallel_executor.checkpoint.save_failed",
                            level=level_num,
                            error=str(e),
                        )

            # All levels done — cancel the background progress emitter
            outer_tg.cancel_scope.cancel()

        # Aggregate results - sort by AC index for consistent ordering
        sorted_results = sorted(all_results, key=lambda r: r.ac_index)
        total_duration = (datetime.now(UTC) - start_time).total_seconds()
        success_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.SUCCEEDED)
        externally_satisfied_count = sum(
            1 for r in sorted_results if r.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY
        )
        failure_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.FAILED)
        blocked_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.BLOCKED)
        invalid_count = sum(1 for r in sorted_results if r.outcome == ACExecutionOutcome.INVALID)
        skipped_count = blocked_count + invalid_count
        total_messages = execution_counters["messages_count"]

        log.info(
            "parallel_executor.execution.completed",
            session_id=session_id,
            success_count=success_count,
            externally_satisfied_count=externally_satisfied_count,
            failure_count=failure_count,
            blocked_count=blocked_count,
            invalid_count=invalid_count,
            skipped_count=skipped_count,
            total_messages=total_messages,
            duration_seconds=total_duration,
        )

        return ParallelExecutionResult(
            results=tuple(sorted_results),
            success_count=success_count,
            failure_count=failure_count,
            externally_satisfied_count=externally_satisfied_count,
            skipped_count=skipped_count,
            blocked_count=blocked_count,
            invalid_count=invalid_count,
            stages=tuple(stage_results),
            reconciled_level_contexts=tuple(level_contexts),
            total_messages=total_messages,
            total_duration_seconds=total_duration,
        )

    async def _execute_single_ac(
        self,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        seed_goal: str,
        depth: int = 0,
        execution_id: str = "",
        level_contexts: list[LevelContext] | None = None,
        sibling_acs: list[_SiblingACRef] | None = None,
        retry_attempt: int = 0,
        execution_counters: dict[str, int] | None = None,
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        retry_prompt_extra: str = "",
        same_runtime_budget_exhausted: bool = True,
        ac_spec: AcceptanceCriterionSpec | None = None,
    ) -> ACExecutionResult:
        """Execute a single AC via the sole recursive AC execution entry point.

        Flow:
        1. Ask Claude to analyze if AC needs decomposition
        2. If decomposable → get Sub-ACs → execute in parallel
        3. If atomic → execute directly

        Args:
            ac_index: 0-based AC index.
            ac_content: AC description.
            session_id: Parent session ID.
            tools: Tools for the agent.
            system_prompt: System prompt.
            seed_goal: Overall goal from seed.
            depth: Current depth in decomposition tree.
            execution_id: Execution ID for event tracking.
            level_contexts: Context from previously completed levels.
            sibling_acs: Descriptions of ACs running in parallel at this level.
            same_runtime_budget_exhausted: Whether this call is the AC's final
                same-runtime attempt. Cross-harness redispatch (PR-X X1) is only
                consulted when this is ``True`` — i.e. the same-runtime recovery
                budget (batch-level ``ac_retry_attempts`` retries, plus this
                call's stall retries) is spent — so the alternate harness never
                pre-empts the configured same-runtime retries. The batch layer
                sets it; direct/sub-AC callers default to ``True``.
            ac_spec: The top-level AC's structured spec, when it carries a success
                contract, so the atomic leaf prompt can surface it. Only the batch
                layer passes it for top-level ACs; sub-AC recursion leaves it
                ``None`` (a decomposed child has no spec-level contract of its own).

        Returns:
            ACExecutionResult for this AC.
        """
        start_time = datetime.now(UTC)
        execution_context_id = execution_id or session_id
        if node_identity is None:
            node_identity = ExecutionNodeIdentity.root(
                execution_context_id=execution_context_id,
                ac_index=ac_index,
            )

        log.info(
            "parallel_executor.ac.started",
            parent_session_id=session_id,
            ac_index=ac_index,
            node_id=node_identity.node_id,
            display_path=node_identity.display_path,
            depth=depth,
        )

        # Try decomposition if enabled and not too deep
        if self._enable_decomposition and depth < self._max_decomposition_depth:
            display_label = (
                f"AC {node_identity.display_path}"
                if node_identity.depth == 0
                else f"Sub-AC {node_identity.display_path}"
            )
            self._console.print(f"  [dim]{display_label}: Analyzing complexity...[/dim]")
            self._flush_console()
            sub_acs = await self._try_decompose_ac(
                ac_content=ac_content,
                ac_index=ac_index,
                seed_goal=seed_goal,
                tools=tools,
                system_prompt=system_prompt,
                node_identity=node_identity,
            )

            if sub_acs and len(sub_acs) >= MIN_SUB_ACS:
                # Decomposition successful - execute Sub-ACs in parallel
                self._console.print(
                    f"  [cyan]{display_label} → Decomposed into {len(sub_acs)} Sub-ACs (parallel)[/cyan]"
                )
                self._flush_console()

                # Emit decomposition event for TUI
                for i, sub_ac in enumerate(sub_acs):
                    child_node_identity = node_identity.child(i)
                    await self._emit_subtask_event(
                        execution_id=execution_id,
                        ac_index=ac_index,
                        sub_task_index=i + 1,
                        sub_task_content=sub_ac,
                        status="pending",
                        node_identity=child_node_identity,
                    )

                # Execute Sub-ACs sequentially (memory optimization) while
                # re-entering this same method for both composite and atomic children.
                self._console.print(
                    f"    [green]Starting {len(sub_acs)} Sub-ACs sequentially...[/green]"
                )

                sub_results: list[ACExecutionResult | BaseException] = [None] * len(sub_acs)
                sub_depth = depth + 1

                for idx, sub_ac in enumerate(sub_acs):
                    try:
                        child_node_identity = node_identity.child(idx)
                        child_is_sub_ac = child_node_identity.depth > 0
                        legacy_parent_ac_index = (
                            node_identity.root_ac_index if child_node_identity.depth == 1 else None
                        )
                        legacy_sub_ac_index = idx if child_node_identity.depth == 1 else None
                        await self._emit_subtask_event(
                            execution_id=execution_id,
                            ac_index=ac_index,
                            sub_task_index=idx + 1,
                            sub_task_content=sub_ac,
                            status="executing",
                            node_identity=child_node_identity,
                        )

                        sub_results[idx] = await self._execute_single_ac(
                            ac_index=ac_index * 100 + idx,
                            ac_content=sub_ac,
                            session_id=session_id,
                            tools=tools,
                            tool_catalog=tool_catalog,
                            system_prompt=system_prompt,
                            seed_goal=seed_goal,
                            depth=sub_depth,
                            execution_id=execution_id,
                            level_contexts=level_contexts,
                            retry_attempt=retry_attempt,
                            execution_counters=execution_counters,
                            is_sub_ac=child_is_sub_ac,
                            parent_ac_index=legacy_parent_ac_index,
                            sub_ac_index=legacy_sub_ac_index,
                            node_identity=child_node_identity,
                        )
                    except BaseException as e:
                        if isinstance(e, anyio.get_cancelled_exc_class()):
                            raise
                        sub_results[idx] = e

                # Convert exceptions and None sentinels to failed results
                final_sub_results: list[ACExecutionResult] = []
                for i, result in enumerate(sub_results):
                    if isinstance(result, BaseException) or result is None:
                        final_sub_results.append(
                            ACExecutionResult(
                                ac_index=ac_index * 100 + i,
                                ac_content=sub_acs[i],
                                success=False,
                                error=str(result)
                                if isinstance(result, BaseException)
                                else "Task cancelled or produced no result",
                                retry_attempt=retry_attempt,
                                depth=sub_depth,
                            )
                        )
                    else:
                        final_sub_results.append(result)

                success_count = sum(1 for result in final_sub_results if result.success)
                self._console.print(
                    f"    [{'green' if success_count == len(sub_acs) else 'yellow'}]"
                    f"Sub-ACs completed: {success_count}/{len(sub_acs)} succeeded[/]"
                )

                # Update TUI with final statuses
                for i, result in enumerate(final_sub_results):
                    status = "completed" if result.success else "failed"
                    child_node_identity = node_identity.child(i)
                    await self._emit_subtask_event(
                        execution_id=execution_id,
                        ac_index=ac_index,
                        sub_task_index=i + 1,
                        sub_task_content=sub_acs[i],
                        status=status,
                        node_identity=child_node_identity,
                    )

                duration = (datetime.now(UTC) - start_time).total_seconds()
                all_success = all(result.success for result in final_sub_results)

                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=all_success,
                    messages=(),
                    final_message="\n".join(
                        _render_ac_section(
                            ACExecutionResult(
                                ac_index=ac_index,
                                ac_content=ac_content,
                                success=all_success,
                                messages=(),
                                duration_seconds=duration,
                                is_decomposed=True,
                                sub_results=tuple(final_sub_results),
                                depth=depth,
                            ),
                            index_path=(ac_index + 1,),
                            heading_level=3,
                            include_header=False,
                        )
                    ),
                    duration_seconds=duration,
                    retry_attempt=retry_attempt,
                    is_decomposed=True,
                    sub_results=tuple(final_sub_results),
                    depth=depth,
                )

        # Depth-limit canary: execution is forced atomic once the soft recursion
        # safety net is reached, so downstream stages can detect decomposition pressure.
        decomposition_depth_warning = (
            self._enable_decomposition and depth >= self._max_decomposition_depth
        )

        # Stall recovery belongs to atomic leaves only. Once this method decides
        # to execute atomically, it can retry the leaf without re-running the
        # decomposition/dispatch branch above.
        atomic_retry_attempt = retry_attempt
        max_attempts = retry_attempt + MAX_STALL_RETRIES + 1
        # Stable re-run bundle for a possible cross-harness redispatch (PR-X X1):
        # every param except retry_attempt is fixed across the atomic loop, so it
        # can be replayed verbatim on an alternative runtime.
        alt_rerun_kwargs: dict[str, Any] = {
            "ac_index": ac_index,
            "ac_content": ac_content,
            "session_id": session_id,
            "tools": tools,
            "tool_catalog": tool_catalog,
            "system_prompt": system_prompt,
            "seed_goal": seed_goal,
            "depth": depth,
            "execution_id": execution_id,
            "level_contexts": level_contexts,
            "sibling_acs": sibling_acs,
            "execution_counters": execution_counters,
            "is_sub_ac": is_sub_ac,
            "parent_ac_index": parent_ac_index,
            "sub_ac_index": sub_ac_index,
            "node_identity": node_identity,
            "ac_spec": ac_spec,
        }
        while True:
            atomic_result = await self._execute_atomic_ac(
                ac_index=ac_index,
                ac_content=ac_content,
                session_id=session_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                seed_goal=seed_goal,
                depth=depth,
                start_time=start_time,
                execution_id=execution_id,
                level_contexts=level_contexts,
                sibling_acs=sibling_acs,
                retry_attempt=atomic_retry_attempt,
                execution_counters=execution_counters,
                retry_prompt_extra=retry_prompt_extra,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                ac_spec=ac_spec,
            )
            if atomic_result.error != _STALL_SENTINEL:
                if not atomic_result.success and same_runtime_budget_exhausted:
                    # Non-stall terminal failure (e.g. fabrication, exhausted
                    # transient 429/529) on the FINAL same-runtime attempt: try
                    # one cross-harness redispatch. Earlier attempts fall through
                    # so the configured same-runtime retries run first.
                    alt_result = await self._maybe_redispatch_alt_harness(
                        result=atomic_result,
                        execution_context_id=execution_context_id,
                        rerun_kwargs=alt_rerun_kwargs,
                        atomic_retry_attempt=atomic_retry_attempt,
                        stall_retries_exhausted=False,
                    )
                    if alt_result is not None:
                        atomic_result = alt_result
                if decomposition_depth_warning:
                    return replace(atomic_result, decomposition_depth_warning=True)
                return atomic_result

            runtime_identity = build_ac_runtime_identity(
                ac_index,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                retry_attempt=atomic_retry_attempt,
            )
            should_retry = atomic_retry_attempt - retry_attempt < MAX_STALL_RETRIES
            stall_event = create_ac_stall_detected_event(
                session_id=session_id,
                ac_index=ac_index,
                ac_id=runtime_identity.ac_id,
                silent_seconds=STALL_TIMEOUT_SECONDS,
                attempt=runtime_identity.attempt_number,
                max_attempts=max_attempts,
                action="restart" if should_retry else "abandon",
            )
            if node_identity is not None:
                stall_event.data.update(node_identity.to_event_metadata())
            await self._safe_emit_event(stall_event)

            if not should_retry:
                log.error(
                    "parallel_executor.ac.stall_abandoned",
                    session_id=session_id,
                    ac_index=ac_index,
                    depth=depth,
                    retry_attempt=atomic_retry_attempt,
                )
                failed_result = replace(
                    atomic_result,
                    error=f"Stalled (no activity for {STALL_TIMEOUT_SECONDS:.0f}s)",
                )
                # An abandoned stall is re-dispatched by the batch-level
                # same-runtime retry loop (its error is no longer the stall
                # sentinel), so only try a cross-harness redispatch once that
                # budget is also spent — i.e. this is the final same-runtime
                # attempt — before the AC is finally marked FAILED.
                if same_runtime_budget_exhausted:
                    alt_result = await self._maybe_redispatch_alt_harness(
                        result=failed_result,
                        execution_context_id=execution_context_id,
                        rerun_kwargs=alt_rerun_kwargs,
                        atomic_retry_attempt=atomic_retry_attempt,
                        stall_retries_exhausted=True,
                    )
                    if alt_result is not None:
                        failed_result = alt_result
                if decomposition_depth_warning:
                    return replace(failed_result, decomposition_depth_warning=True)
                return failed_result

            atomic_retry_attempt += 1

    async def _maybe_redispatch_alt_harness(
        self,
        *,
        result: ACExecutionResult,
        execution_context_id: str,
        rerun_kwargs: dict[str, Any],
        atomic_retry_attempt: int,
        stall_retries_exhausted: bool,
    ) -> ACExecutionResult | None:
        """Cross-harness recovery hook (PR-X X1) — narrow shell over the module.

        Consults :func:`decide_alt_harness_redispatch`; on a positive decision,
        re-runs the SAME AC once on a different runtime (fresh worker session),
        capped at one alt-harness redispatch per AC. Returns the alternative's
        result whether it succeeds or fails, so a failed alternate attempt is
        surfaced as the authoritative outcome (never silently discarded); only a
        negative decision or an infrastructure error returns ``None`` so the
        original failure path is untouched.
        """
        if not self._cross_harness_redispatch_enabled:
            return None

        from ouroboros.orchestrator.cross_harness_redispatch import (
            decide_alt_harness_redispatch,
            looks_transient_exhausted,
        )
        from ouroboros.orchestrator.failure_taxonomy import FailureClass

        from_backend = getattr(self._adapter, "runtime_backend", None)
        runtime_identity = build_ac_runtime_identity(
            rerun_kwargs["ac_index"],
            execution_context_id=execution_context_id,
            is_sub_ac=rerun_kwargs["is_sub_ac"],
            parent_ac_index=rerun_kwargs["parent_ac_index"],
            sub_ac_index=rerun_kwargs["sub_ac_index"],
            node_identity=rerun_kwargs["node_identity"],
            retry_attempt=atomic_retry_attempt,
        )
        ac_key = runtime_identity.ac_id or f"{execution_context_id}:{rerun_kwargs['ac_index']}"

        failure: FailureClass | None = None
        verdict = result.atomic_verifier_verdict
        if verdict is not None and verdict.failure_class:
            try:
                failure = FailureClass(verdict.failure_class)
            except ValueError:
                failure = None
        # The stall-abandon site carries no verifier verdict, but the condition
        # itself is a STALL — name it so the policy can route it.
        if failure is None and stall_retries_exhausted:
            failure = FailureClass.STALL

        decision = decide_alt_harness_redispatch(
            enabled=True,
            from_backend=from_backend,
            failure=failure,
            already_redispatched=ac_key in self._alt_harness_redispatched_acs,
            stall_retries_exhausted=stall_retries_exhausted,
            transient_exhausted=looks_transient_exhausted(result.error),
            exclude={from_backend} if from_backend else None,
            weights=_safe_backend_outcome_weights(),
        )
        if not decision.should_redispatch or decision.to_backend is None:
            return None

        # Consume the one-per-AC cap up front so a re-run that itself fails does
        # not trigger a second harness hop.
        self._alt_harness_redispatched_acs.add(ac_key)
        try:
            alt_result = await self._run_single_ac_on_backend(
                decision.to_backend,
                rerun_kwargs=rerun_kwargs,
                retry_attempt=atomic_retry_attempt + 1,
                decision=decision,
                runtime_identity=runtime_identity,
                failure_class=failure.value if failure is not None else None,
            )
        except Exception as exc:  # never make a failure worse
            log.warning(
                "parallel_executor.alt_harness_redispatch_failed",
                to_backend=decision.to_backend,
                ac_index=rerun_kwargs["ac_index"],
                error=str(exc),
            )
            return None
        if alt_result is None:
            return None
        # Surface the alternate attempt as the authoritative outcome regardless of
        # its success: the alternate backend ran in the SAME workspace and may
        # have left edits, so on failure the caller must report the alternate's
        # (failed) result — not the original same-runtime failure — so the
        # backend that last touched the workspace is honestly represented.
        return self._annotate_alt_harness_result(
            alt_result,
            decision=decision,
            from_backend=from_backend,
        )

    @staticmethod
    def _annotate_alt_harness_result(
        result: ACExecutionResult,
        *,
        decision: Any,
        from_backend: str | None,
    ) -> ACExecutionResult:
        """Make an alternate-harness attempt self-describing for honest reporting.

        On a successful alternate the result already carries the alt backend's
        session/runtime handle, so it is returned unchanged (the win is the win).
        On a FAILED alternate the alternate backend ran in the SAME workspace and
        may have left edits, so the returned failure names the from→to backends
        and flags the possible workspace mutation in its ``error`` — the field
        downstream FAILED classification and the human-facing report read — so
        the final result never describes only the original same-runtime failure
        while a different backend was the last thing to touch the workspace.
        """
        if result.success:
            return result
        to_backend = getattr(decision, "to_backend", None)
        alt_note = (
            f"Cross-harness redispatch to '{to_backend}' (from '{from_backend}') also FAILED; "
            f"the alternate backend ran in the shared workspace and may have modified it."
        )
        base_error = result.error or "alternate-harness attempt failed"
        combined_error = f"{base_error}\n[alt-harness] {alt_note}"
        return replace(result, error=combined_error)

    async def _run_single_ac_on_backend(
        self,
        backend: str,
        *,
        rerun_kwargs: dict[str, Any],
        retry_attempt: int,
        decision: Any,
        runtime_identity: ACRuntimeIdentity,
        failure_class: str | None,
    ) -> ACExecutionResult | None:
        """Build a throwaway runtime for ``backend`` and replay one AC on it.

        Emits the observable from→to redispatch event, then runs the AC through a
        fresh, decomposition-disabled executor whose own cross-harness redispatch
        is turned off (recursion guard).
        """
        from ouroboros.orchestrator.cross_harness_redispatch import (
            create_alt_harness_redispatch_event,
        )
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        cwd = self._task_cwd or self._adapter.working_directory
        alt_adapter = create_agent_runtime(backend=backend, cwd=cwd)

        event = create_alt_harness_redispatch_event(
            session_id=rerun_kwargs["session_id"],
            ac_index=rerun_kwargs["ac_index"],
            ac_id=runtime_identity.ac_id,
            execution_id=rerun_kwargs["execution_id"] or None,
            decision=decision,
            redispatch_index=1,
            failure_class=failure_class,
        )
        await self._safe_emit_event(event)
        log.info(
            "parallel_executor.alt_harness_redispatch",
            from_backend=decision.from_backend,
            to_backend=backend,
            ac_index=rerun_kwargs["ac_index"],
        )

        alt_executor = ParallelACExecutor(
            alt_adapter,
            self._event_store,
            console=self._console,
            enable_decomposition=False,
            max_concurrent=1,
            checkpoint_store=self._checkpoint_store,
            task_cwd=self._task_cwd,
            execution_profile=self._execution_profile,
            fat_harness_mode=self._fat_harness_mode,
            atomic_verifier=self._atomic_verifier,
            reasoning_effort=self._reasoning_effort,
            cross_harness_redispatch=False,
        )
        return await alt_executor._execute_single_ac(**rerun_kwargs, retry_attempt=retry_attempt)

    async def _try_decompose_ac(
        self,
        ac_content: str,
        ac_index: int,
        seed_goal: str,
        tools: list[str],
        system_prompt: str,
        node_identity: ExecutionNodeIdentity | None = None,
    ) -> list[str] | None:
        """Ask Claude to decompose AC into Sub-ACs if complex.

        Returns:
            List of Sub-AC descriptions, or None if AC is atomic.
        """
        ac_label = (
            f"AC #{node_identity.display_path}"
            if node_identity is not None
            else f"AC #{ac_index + 1}"
        )
        decomposition_system_prompt = (
            "You are a task decomposition expert. Analyze tasks and break them down if needed."
        )
        min_sub_acs = MIN_SUB_ACS
        max_sub_acs = MAX_SUB_ACS
        profile_metadata = self._decomposition_profile_metadata()
        if self._execution_profile is not None:
            params = params_from_profile(
                self._execution_profile,
                min_branching=MIN_SUB_ACS,
            )
            min_sub_acs = params.min_branching
            max_sub_acs = params.max_branching
            decomposition_system_prompt = build_decomposition_system_prompt(params)
            decompose_prompt = build_decomposition_user_prompt(
                params,
                ac_label=ac_label,
                ac_content=ac_content,
                seed_goal=seed_goal,
            )
        else:
            decompose_prompt = f"""Analyze this acceptance criterion and determine if it should be decomposed.

## Goal Context
{seed_goal}

## Acceptance Criterion ({ac_label})
{ac_content}

## Instructions
Default to ATOMIC. Each Sub-AC you create becomes a separate agent session with
its own full context, so decomposing has a real token cost — only split when it
clearly pays for itself.

Decompose into {MIN_SUB_ACS}-{MAX_SUB_ACS} Sub-ACs ONLY if this AC bundles
multiple independently *valuable* outcomes that would each be verified
differently. Needing several steps, or touching several files, is NOT by itself a
reason to decompose — the executor handles multi-step work within one unit.

If the AC is a single focused outcome (the common case), respond with: ATOMIC

If decomposing, respond with ONLY a JSON array of Sub-AC descriptions:
["Sub-AC 1: description", "Sub-AC 2: description", ...]

Each Sub-AC should be:
- Independently executable
- Specific and focused
- Part of achieving the parent AC
- Targeting distinct files or distinct sections within shared files (avoid overlap)

Respond with either "ATOMIC" or the JSON array only, nothing else.
"""

        self._announce_param_degradations(
            system_prompt=decomposition_system_prompt,
            tools=[],
        )
        # Pace this backend request within the shared budget before starting the
        # decomposition timeout, so rate-limit waiting never eats into it.
        await self._await_dispatch_rate_budget(
            prompt=decompose_prompt,
            system_prompt=decomposition_system_prompt,
        )

        try:
            response_text = ""
            # NOTE: Do NOT use `break` or `aclosing` with the SDK generator.
            # The SDK uses anyio cancel scopes internally. If the generator
            # is closed via aclose() (from break or aclosing), the cancel scope
            # cleanup creates background asyncio Tasks that cancel other
            # running tasks. Let the generator complete naturally instead.
            async with asyncio.timeout(DECOMPOSITION_TIMEOUT_SECONDS):
                async for message in self._adapter.execute_task(
                    prompt=decompose_prompt,
                    tools=[],  # No tools for decomposition analysis
                    system_prompt=decomposition_system_prompt,
                    resume_handle=self._inherited_runtime_handle,
                ):
                    if message.content:
                        # Some runtimes (notably Goose stream-json) emit assistant text
                        # as token/delta chunks.  The decomposition parser needs the
                        # full response, so accumulate chunks for Goose while preserving
                        # the previous last-message behavior for runtimes that emit
                        # complete assistant messages.
                        if getattr(self._adapter, "runtime_backend", "") == "goose":
                            if message.type not in {"assistant", "result"}:
                                continue
                            if message.is_final:
                                response_text = message.content
                            else:
                                response_text += message.content
                        else:
                            response_text = message.content

            # Parse response.
            #
            # Check for an explicit Sub-AC JSON array FIRST, before the ATOMIC
            # verdict. A bare ``"ATOMIC" in text`` substring match (the previous
            # behavior) mis-reads a legitimate split such as
            # ``"NOT ATOMIC, decompose into: [...]"`` — or an array element that
            # merely mentions the word "atomic" — as a verdict of atomic. By
            # parsing the array first and only then accepting an ATOMIC verdict
            # that the response *starts with*, both false-atomic and false-split
            # directions are closed. Anything we cannot parse fails closed to
            # atomic (no split): an erroneous split multiplies token cost across
            # the whole subtree, so atomic is the frugal default.
            response_text = response_text.strip()

            json_match = re.search(r"\[.*\]", response_text, re.DOTALL)
            if json_match:
                try:
                    sub_acs = json.loads(json_match.group())
                except json.JSONDecodeError:
                    sub_acs = None
                if (
                    isinstance(sub_acs, list)
                    and all(isinstance(s, str) for s in sub_acs)
                    and min_sub_acs <= len(sub_acs) <= max_sub_acs
                ):
                    log.info(
                        "parallel_executor.decomposition.success",
                        ac_index=ac_index,
                        sub_ac_count=len(sub_acs),
                        **profile_metadata,
                    )
                    return sub_acs

            if response_text.upper().startswith("ATOMIC"):
                log.info(
                    "parallel_executor.decomposition.atomic",
                    ac_index=ac_index,
                    **profile_metadata,
                )
                return None

            log.warning(
                "parallel_executor.decomposition.unparseable_defaulting_atomic",
                ac_index=ac_index,
                response_preview=response_text[:100],
                **profile_metadata,
            )
            return None

        except TimeoutError:
            log.warning(
                "parallel_executor.decomposition.timeout",
                ac_index=ac_index,
                timeout_seconds=DECOMPOSITION_TIMEOUT_SECONDS,
                **profile_metadata,
            )
            return None
        except Exception as e:
            log.warning(
                "parallel_executor.decomposition.error",
                ac_index=ac_index,
                error=str(e),
                **profile_metadata,
            )
            return None

    @staticmethod
    def _format_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
        """Format tool name with input detail for console output."""
        detail = ""
        if tool_name in ("Read", "Write", "Edit"):
            detail = tool_input.get("file_path", "")
        elif tool_name == "Bash":
            detail = tool_input.get("command", "")
        elif tool_name in ("Glob", "Grep"):
            detail = tool_input.get("pattern", "")
        elif tool_name.startswith("mcp__"):
            for v in tool_input.values():
                if v:
                    detail = str(v)[:50]
                    break
        if detail and len(detail) > 60:
            detail = detail[:57] + "..."
        return f"{tool_name}: {detail}" if detail else tool_name

    async def _wait_for_memory(self, label: str) -> None:
        """Block until system has enough free memory to spawn a subprocess."""
        requires_memory_gate = getattr(self._adapter, "_requires_memory_gate", None)
        if not isinstance(requires_memory_gate, bool):
            requires_memory_gate = False
        if not requires_memory_gate:
            return

        elapsed = 0.0
        while elapsed < _MEMORY_WAIT_MAX_SECONDS:
            available_gb = _get_available_memory_gb()
            if available_gb is None or available_gb >= _MIN_FREE_MEMORY_GB:
                return
            log.warning(
                "memory_pressure.waiting",
                available_gb=round(available_gb, 2),
                label=label,
            )
            await asyncio.sleep(_MEMORY_CHECK_INTERVAL_SECONDS)
            elapsed += _MEMORY_CHECK_INTERVAL_SECONDS
        log.warning("memory_pressure.timeout", label=label)

    def _decomposition_profile_metadata(self) -> dict[str, Any]:
        """Return audit metadata for profile-aware decomposition decisions.

        The metadata is intentionally descriptive only. It lets projections,
        tests, and reviewers prove which profile shaped decomposition without
        changing dispatch behavior or the CLI fat-harness default path.
        """
        profile = self._execution_profile
        if profile is None:
            return {"decomposition_profile": None}
        return {
            "decomposition_profile": {
                "profile": profile.profile,
                "axis": profile.axis,
                "min_unit": profile.min_unit,
                "cut_signal": profile.cut_signal,
                "max_branching": profile.max_branching,
            }
        }

    def _build_atomic_dispatch_context(
        self,
        *,
        ac_index: int,
        ac_content: str,
        label: str,
        level_contexts: list[LevelContext] | None,
        sibling_acs: list[_SiblingACRef] | None,
    ) -> tuple[str, dict[str, Any] | None]:
        """Build the task section for an atomic leaf dispatch.

        Legacy execution keeps its historical prompt shape.  When an
        ExecutionProfile is active, route parent/sibling/AC context through
        the #830 H6 context governor so profile-backed leaves receive bounded,
        deterministic context without flipping any evidence/verifier default.
        """
        if self._execution_profile is None:
            return f"## Your Task ({label})\n{ac_content}", None

        sibling_statuses: list[SiblingStatus] = []
        if sibling_acs and len(sibling_acs) > 1:
            for sibling_index, sibling_ac in sibling_acs:
                if sibling_index == ac_index:
                    continue
                sibling_id = f"sibling-{len(sibling_statuses) + 1}"
                headline = " ".join(sibling_ac.split())
                if len(headline) > _SIBLING_HEADLINE_CHARS:
                    headline = headline[:_SIBLING_HEADLINE_CHARS]
                sibling_statuses.append(
                    SiblingStatus(
                        sibling_id=sibling_id,
                        accepted=None,
                        headline=headline,
                    )
                )

        try:
            composed = compose_context(
                ac=ac_content,
                parent_summary=_build_governed_parent_summary(level_contexts),
                siblings=sibling_statuses,
            )
        except ValueError as exc:
            # This C.3 slice wires the governor into profile-backed dispatch
            # without making budget failures an acceptance/default gate yet.
            # Preserve execution by falling back to the legacy prompt shape and
            # emit auditable metadata so later enforcement work can quantify
            # how often the hard governor would have rejected a leaf.
            return f"## Your Task ({label})\n{ac_content}", {
                "context_governed": False,
                "context_acceptance_enforced": False,
                "context_default_flipped": False,
                "context_governance_error": str(exc),
                "context_fallback": "legacy_prompt",
            }
        rendered = composed.render()
        audit = {
            "context_governed": True,
            "context_acceptance_enforced": False,
            "context_default_flipped": False,
            "context_rendered_chars": len(rendered),
            "context_truncated": composed.truncated,
            "context_sibling_status_count": len(composed.sibling_lines),
            "context_parent_summary_present": bool(composed.parent_summary),
        }
        return f"## Governed Dispatch Context ({label})\n{rendered}", audit

    async def _emit_atomic_context_governed_event(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str,
        session_id: str | None,
        ac_content: str,
        context_audit: dict[str, Any] | None,
    ) -> None:
        """Persist observe-only context-governor metadata for profile-backed leaves."""
        if self._execution_profile is None or context_audit is None:
            return

        await self._event_emitter.emit_atomic_context_governed(
            runtime_identity=runtime_identity,
            execution_id=execution_id,
            session_id=session_id,
            ac_content=ac_content,
            profile=self._execution_profile.profile,
            decomposition_profile_metadata=self._decomposition_profile_metadata(),
            context_audit=context_audit,
        )

    @staticmethod
    def _runtime_event_metadata(message: AgentMessage) -> dict[str, Any]:
        """Serialize shared runtime/tool metadata for execution-scoped events."""
        return ExecutionEventEmitter.runtime_event_metadata(message)

    @staticmethod
    def _message_tool_input_preview(tool_input: dict[str, Any]) -> str | None:
        """Build a compact preview string for shared session tool-call events."""
        return ExecutionEventEmitter.message_tool_input_preview(tool_input)

    @staticmethod
    def _should_emit_session_progress_event(
        message: AgentMessage,
        *,
        projected: Any,
        messages_processed: int,
    ) -> bool:
        """Reuse the shared progress-emission policy for AC session messages."""
        runtime_backend = message.resume_handle.backend if message.resume_handle else None
        return (
            message.is_final
            or messages_processed % 10 == 0
            or projected.is_tool_call
            or projected.thinking is not None
            or message.type == "system"
            or runtime_backend == "opencode"
            or projected.is_tool_result
        )

    def _build_session_progress_event(
        self,
        session_id: str,
        message: AgentMessage,
        *,
        projected: Any,
    ):
        """Create a shared session progress event from an AC runtime message."""
        return self._event_emitter.build_session_progress_event(
            session_id,
            message,
            projected=projected,
        )

    def _build_session_tool_called_event(
        self,
        session_id: str,
        *,
        projected: Any,
    ):
        """Create a shared session tool-call event from an AC runtime message."""
        return self._event_emitter.build_session_tool_called_event(
            session_id,
            projected=projected,
        )

    @staticmethod
    def _coordinator_aggregate_id(execution_id: str, level: int) -> str:
        """Build a deterministic level-scoped aggregate ID for coordinator work."""
        return ExecutionEventEmitter.coordinator_aggregate_id(execution_id, level)

    async def _emit_coordinator_started(
        self,
        execution_id: str,
        session_id: str,
        level: int,
        conflicts: list[Any],
    ) -> None:
        """Emit a level-scoped event when coordinator reconciliation starts."""
        await self._event_emitter.emit_coordinator_started(
            execution_id,
            session_id,
            level,
            conflicts,
        )

    async def _emit_coordinator_runtime_events(
        self,
        execution_id: str,
        session_id: str,
        review: CoordinatorReview,
    ) -> None:
        """Persist normalized coordinator runtime audit events at level scope."""
        await self._event_emitter.emit_coordinator_runtime_events(
            execution_id,
            session_id,
            review,
            format_tool_detail=self._format_tool_detail,
        )

    async def _emit_coordinator_completed(
        self,
        execution_id: str,
        session_id: str,
        review: CoordinatorReview,
    ) -> None:
        """Persist the coordinator reconciliation result as a level-scoped artifact."""
        await self._event_emitter.emit_coordinator_completed(
            execution_id,
            session_id,
            review,
        )

    async def _execute_atomic_ac(
        self,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        system_prompt: str,
        seed_goal: str,
        depth: int,
        start_time: datetime,
        execution_id: str = "",
        is_sub_ac: bool = False,
        parent_ac_index: int | None = None,
        sub_ac_index: int | None = None,
        node_identity: ExecutionNodeIdentity | None = None,
        level_contexts: list[LevelContext] | None = None,
        sibling_acs: list[_SiblingACRef] | None = None,
        retry_attempt: int = 0,
        tool_catalog: tuple[MCPToolDefinition, ...] | None = None,
        execution_counters: dict[str, int] | None = None,
        retry_prompt_extra: str = "",
        ac_spec: AcceptanceCriterionSpec | None = None,
    ) -> ACExecutionResult:
        """Execute an atomic AC directly via Claude Agent.

        Returns:
            ACExecutionResult for this AC.
        """
        ac_session_id: str | None = None

        # Build prompt (label/indent, governed task section, success contract,
        # retry/parallel-awareness sections, cwd scan, completion contract).
        prompt_bundle = AtomicPromptBuilder(self).build(
            ac_index=ac_index,
            ac_content=ac_content,
            seed_goal=seed_goal,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            level_contexts=level_contexts,
            sibling_acs=sibling_acs,
            retry_attempt=retry_attempt,
            retry_prompt_extra=retry_prompt_extra,
            ac_spec=ac_spec,
        )
        prompt = prompt_bundle.prompt
        label = prompt_bundle.label
        indent = prompt_bundle.indent
        context_governance_audit = prompt_bundle.context_governance_audit

        messages: list[AgentMessage] = []
        final_message = ""
        success = False
        clear_cached_runtime_handle = False
        execution_context_id = execution_id or session_id
        persisted_runtime_handle = await self._load_persisted_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        if persisted_runtime_handle is not None:
            self._remember_ac_runtime_handle(
                ac_index,
                persisted_runtime_handle,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                retry_attempt=retry_attempt,
            )
        runtime_handle = self._build_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            tool_catalog=tool_catalog,
        )
        runtime_identity = build_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        await self._emit_atomic_context_governed_event(
            runtime_identity=runtime_identity,
            execution_id=execution_context_id,
            session_id=session_id,
            ac_content=ac_content,
            context_audit=context_governance_audit,
        )
        await self._wait_for_memory(label)
        self._announce_param_degradations(system_prompt=system_prompt, tools=tools)
        # Pace delivery within the backend's shared rate budget (dormant unless
        # an RPM/TPM is configured for this backend) before the stall-scoped run.
        await self._await_dispatch_rate_budget(prompt=prompt, system_prompt=system_prompt)

        # Lay the executor on the capability contract: decide the effort level for
        # this unit (a decomposed child inherits the parent tier unchanged; a hard AC
        # on its second-or-later retry is raised one notch) and classify how the
        # chosen runtime will honor it from its declared capability — enforced via a
        # native knob, or advised. The level is passed to execute_task; an advised
        # runtime ignores it. Dormant by default (base effort None → level None).
        effort_decision, execute_effort_kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=self._reasoning_effort,
            is_decomposed_child=is_sub_ac,
            retry_attempt=retry_attempt,
        )
        if effort_decision.level is not None:
            log.debug(
                "orchestrator.executor.effort_routed",
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                effort_level=effort_decision.level,
                effort_mode=effort_decision.mode,
                backend=getattr(self._adapter, "runtime_backend", None),
            )
            # Record the routing decision as a first-class, queryable event so the
            # frugality proof can join per-AC (effort_level x effort_mode) against
            # token attribution and the TraceGuard verdict. Only ``enforced`` rows
            # count toward the deterministic proof; advised rows are recorded but
            # excluded — which is exactly the distinction effort_mode carries here.
            #
            # This is auxiliary proof telemetry, not a runtime dependency: route it
            # through ``_safe_emit_event`` so a degraded event store degrades to a
            # warning (matching the adjacent observe-only executor events) instead of
            # aborting the AC before runtime dispatch. ``execution_context_id``
            # (execution_id or session_id) keeps the payload scope aligned with the
            # aggregate id even on direct/fallback callers that pass no execution_id.
            await self._event_emitter.emit_effort_routed(
                runtime_identity=runtime_identity,
                execution_id=execution_context_id,
                session_id=session_id,
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                effort_level=effort_decision.level,
                effort_mode=effort_decision.mode,
                base_reasoning_effort=self._reasoning_effort,
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
            )
        # execute_effort_kwargs (from resolve_execute_effort) carries
        # reasoning_effort ONLY for runtimes that enforce it; advised runtimes that
        # do not accept the parameter are never handed it.

        # Runtime dispatch + streaming/heartbeat consumption. The dispatcher owns
        # the stall-scoped CancelScope and the per-message loop; it mutates
        # ``dispatch_state`` in place (including on the exception path) so the
        # ``except``/``finally`` below observe the latest runtime handle, session
        # id, and partial message list. Created before the ``try`` so it is always
        # bound for the ``except``/``finally``.
        dispatch_state = LeafDispatchState(messages=messages, runtime_handle=runtime_handle)
        try:
            await LeafDispatcher(self).stream(
                state=dispatch_state,
                prompt=prompt,
                tools=tools,
                system_prompt=system_prompt,
                execute_effort_kwargs=execute_effort_kwargs,
                runtime_identity=runtime_identity,
                execution_context_id=execution_context_id,
                session_id=session_id,
                ac_index=ac_index,
                ac_content=ac_content,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                retry_attempt=retry_attempt,
                label=label,
                indent=indent,
                execution_counters=execution_counters,
            )
            runtime_handle = dispatch_state.runtime_handle
            ac_session_id = dispatch_state.ac_session_id
            final_message = dispatch_state.final_message
            success = dispatch_state.success

            # Check if stall was detected (CancelScope ate the Cancelled)
            if dispatch_state.stalled:
                duration = (datetime.now(UTC) - start_time).total_seconds()
                log.warning(
                    "parallel_executor.ac.stall_detected",
                    ac_index=ac_index,
                    depth=depth,
                    silent_seconds=STALL_TIMEOUT_SECONDS,
                    message_count=dispatch_state.message_count,
                )
                clear_cached_runtime_handle = True
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    messages=tuple(messages),
                    error=_STALL_SENTINEL,
                    duration_seconds=duration,
                    session_id=ac_session_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                )

            self._remember_ac_runtime_handle(
                ac_index,
                runtime_handle,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                retry_attempt=retry_attempt,
            )

            duration = (datetime.now(UTC) - start_time).total_seconds()

            typed_evidence, typed_validation, typed_error = self._observe_atomic_typed_evidence(
                ac_content=ac_content,
                final_message=final_message,
                success=success,
            )
            verifier_verdict = self._run_atomic_verifier_pass(
                ac_content=ac_content,
                final_message=final_message,
                success=success,
                messages=tuple(messages),
                typed_evidence=typed_evidence,
                typed_validation=typed_validation,
                ac_spec=ac_spec,
            )
            fat_harness_error = self._fat_harness_acceptance_error(
                runtime_success=success,
                typed_evidence=typed_evidence,
                typed_validation=typed_validation,
                typed_error=typed_error,
                verifier_verdict=verifier_verdict,
            )
            result_final_message = final_message
            if fat_harness_error is not None:
                success = False
                log.warning(
                    "parallel_executor.ac.verifier_rejected",
                    session_id=session_id,
                    execution_id=execution_id,
                    ac_index=ac_index,
                    depth=depth,
                    reason=fat_harness_error,
                    typed_evidence_present=typed_evidence is not None,
                    typed_evidence_valid=(
                        typed_validation.ok if typed_validation is not None else False
                    ),
                    verifier_ran=verifier_verdict is not None,
                    verifier_passed=(
                        verifier_verdict.passed if verifier_verdict is not None else False
                    ),
                    verifier_reasons=(
                        list(verifier_verdict.reasons) if verifier_verdict is not None else []
                    ),
                    verifier_failure_class=(
                        verifier_verdict.failure_class if verifier_verdict is not None else None
                    ),
                    verifier_status=(
                        verifier_verdict.status.value if verifier_verdict is not None else None
                    ),
                    retry_admission=(
                        verifier_verdict.retry_admission.value
                        if verifier_verdict is not None
                        else None
                    ),
                    verifier_evidence_used=(
                        list(verifier_verdict.evidence_used) if verifier_verdict is not None else []
                    ),
                )
                result_final_message = (
                    f"{fat_harness_error}\n\nRuntime final message:\n{final_message}"
                    if final_message
                    else fat_harness_error
                )
            await self._emit_atomic_typed_evidence_event(
                runtime_identity=runtime_identity,
                execution_id=execution_context_id,
                session_id=ac_session_id,
                ac_content=ac_content,
                typed_evidence=typed_evidence,
                typed_validation=typed_validation,
                typed_error=typed_error,
                verifier_verdict=verifier_verdict,
                enforcement_error=fat_harness_error,
            )
            await self._emit_ac_runtime_event(
                event_type=(
                    "execution.session.completed" if success else "execution.session.failed"
                ),
                runtime_identity=runtime_identity,
                ac_content=ac_content,
                runtime_handle=runtime_handle,
                execution_id=execution_context_id,
                session_id=ac_session_id,
                result_summary=result_final_message or None,
                success=success,
                error=(
                    None
                    if success
                    else fat_harness_error or final_message or "Implementation session failed"
                ),
            )
            clear_cached_runtime_handle = True
            result_typed_evidence = typed_evidence
            if success and self._execution_profile is not None and typed_evidence is not None:
                result_typed_evidence = _scoped_evidence_record_for_ac(
                    self._execution_profile,
                    ac_content,
                    typed_evidence,
                )

            log.info(
                "parallel_executor.ac.completed",
                ac_index=ac_index,
                depth=depth,
                success=success,
                is_sub_ac=is_sub_ac,
                duration_seconds=duration,
            )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=success,
                messages=tuple(messages),
                final_message=result_final_message,
                duration_seconds=duration,
                session_id=ac_session_id,
                retry_attempt=retry_attempt,
                depth=depth,
                runtime_handle=runtime_handle,
                typed_evidence=result_typed_evidence,
                typed_evidence_validation=typed_validation,
                typed_evidence_error=typed_error,
                atomic_verifier_verdict=verifier_verdict,
                error=fat_harness_error,
            )

        except Exception as e:
            duration = (datetime.now(UTC) - start_time).total_seconds()

            self._remember_ac_runtime_handle(
                ac_index,
                dispatch_state.runtime_handle,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                retry_attempt=retry_attempt,
            )
            await self._emit_ac_runtime_event(
                event_type="execution.session.failed",
                runtime_identity=runtime_identity,
                ac_content=ac_content,
                runtime_handle=dispatch_state.runtime_handle,
                execution_id=execution_context_id,
                session_id=dispatch_state.ac_session_id,
                success=False,
                error=str(e),
            )
            clear_cached_runtime_handle = True

            log.exception(
                "parallel_executor.ac.failed",
                ac_index=ac_index,
                depth=depth,
                error=str(e),
            )

            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                messages=tuple(messages),
                error=str(e),
                duration_seconds=duration,
                session_id=dispatch_state.ac_session_id,
                retry_attempt=retry_attempt,
                depth=depth,
                runtime_handle=dispatch_state.runtime_handle,
            )
        finally:
            if clear_cached_runtime_handle:
                await self._terminate_runtime_handle(
                    dispatch_state.runtime_handle,
                    runtime_scope_id=runtime_identity.session_scope_id,
                )
                self._forget_ac_runtime_handle(
                    ac_index,
                    execution_context_id=execution_context_id,
                    is_sub_ac=is_sub_ac,
                    parent_ac_index=parent_ac_index,
                    sub_ac_index=sub_ac_index,
                    node_identity=node_identity,
                    retry_attempt=retry_attempt,
                )

    def _observe_atomic_typed_evidence(
        self,
        *,
        ac_content: str,
        final_message: str,
        success: bool,
    ) -> tuple[EvidenceRecord | None, ValidationResult | None, str | None]:
        """Parse and validate typed evidence at the atomic AC acceptance boundary.

        In observe-only mode this only records whether a successful atomic
        leaf emitted profile-shaped evidence. In fat-harness mode, the caller
        subsequently requires both this validation result and a separate
        verifier PASS before accepting the AC.
        """
        if not success or self._execution_profile is None:
            return None, None, None

        try:
            record = extract_evidence(final_message)
            effective_schema = _effective_evidence_schema_for_ac(
                self._execution_profile,
                ac_content,
            )
            validation = validate_evidence(
                _profile_with_evidence_schema(self._execution_profile, effective_schema),
                record,
            )
        except ProfileEvidenceConfigError:
            raise
        except EvidenceError as exc:
            return None, None, str(exc)
        return record, validation, None

    async def _run_ac_verify_gate(
        self, *, spec: AcceptanceCriterionSpec, cwd: str
    ) -> _VerifyGateOutcome:
        """Judge an AC's success contract: expected artifacts + verify command.

        The orchestrator — not the worker — checks the contract so a failing
        check cannot be self-reported away. All ``expected_artifacts`` must
        exist under ``cwd`` (checked first — it is cheap — and every missing
        entry is reported in one failure). ``verify_command``, when set, must
        then exit 0 and, when ``output_assertion`` is set, print that substring
        in the combined output.
        """
        import contextlib

        missing_artifacts = _missing_expected_artifacts(spec.expected_artifacts, cwd)
        if missing_artifacts:
            return _VerifyGateOutcome(
                passed=False,
                reason="expected_artifacts missing: " + ", ".join(missing_artifacts),
                output_tail="",
                missing_artifacts=missing_artifacts,
            )

        command = spec.verify_command
        if not command:
            return _VerifyGateOutcome(passed=True, reason=None, output_tail="")
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:  # pragma: no cover - spawn failure is environmental
            return _VerifyGateOutcome(
                passed=False,
                reason=f"verify_command could not start: {exc}",
                output_tail="",
            )
        try:
            stdout_bytes, _ = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._verify_command_timeout_seconds,
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return _VerifyGateOutcome(
                passed=False,
                reason=(f"verify_command timed out after {self._verify_command_timeout_seconds}s"),
                output_tail="",
            )

        combined = (stdout_bytes or b"").decode("utf-8", errors="replace")
        tail = combined[-_VERIFY_OUTPUT_TAIL_CHARS:]
        returncode = proc.returncode
        if returncode != 0:
            return _VerifyGateOutcome(
                passed=False,
                reason=f"verify_command exited with status {returncode}",
                output_tail=tail,
            )
        if spec.output_assertion and spec.output_assertion not in combined:
            return _VerifyGateOutcome(
                passed=False,
                reason=(
                    f"output_assertion {spec.output_assertion!r} not found in verify_command output"
                ),
                output_tail=tail,
            )
        return _VerifyGateOutcome(passed=True, reason=None, output_tail=tail)

    async def _apply_verify_gate(
        self,
        *,
        seed: Seed,
        ac_index: int,
        result: ACExecutionResult,
        session_id: str,
        execution_id: str,
    ) -> ACExecutionResult:
        """Gate a successful AC on its success contract (PR-V V1).

        The contract gate applies when the spec carries a ``verify_command`` OR
        non-empty ``expected_artifacts``. Contract-less ACs and ACs that already
        failed are returned untouched, so contract-less behavior — and the
        single fat-harness failure event for an already-failed AC — is
        preserved (no double-fail for one root cause).
        """
        if not self._run_verify_commands or not result.success:
            return result
        if ac_index < 0 or ac_index >= len(seed.acceptance_criteria):
            return result
        spec = seed.acceptance_criteria[ac_index]
        if not isinstance(spec, AcceptanceCriterionSpec) or not (
            spec.verify_command or spec.expected_artifacts
        ):
            return result

        cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
        outcome = await self._run_ac_verify_gate(spec=spec, cwd=cwd)
        if outcome.passed:
            return result

        from ouroboros.events.base import BaseEvent
        from ouroboros.orchestrator.failure_taxonomy import FailureClass

        reason = f"Verify gate failed: {outcome.reason}"
        detail = reason
        if outcome.output_tail:
            detail = f"{reason}\n--- verify_command output (tail) ---\n{outcome.output_tail}"
        verdict = VerifierVerdict(
            passed=False,
            reasons=(reason,),
            failure_class=FailureClass.EVIDENCE_MISSING.value,
        )
        await self._safe_emit_event(
            BaseEvent(
                type="execution.verify.failed",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "ac_index": ac_index,
                    "ac_content": ac_text(spec),
                    "verify_command": spec.verify_command,
                    "expected_artifacts": list(spec.expected_artifacts),
                    "missing_artifacts": list(outcome.missing_artifacts),
                    "reason": outcome.reason,
                    "failure_class": FailureClass.EVIDENCE_MISSING.value,
                    "output_tail": outcome.output_tail,
                },
            )
        )
        log.warning(
            "parallel_executor.ac.verify_gate_failed",
            session_id=session_id,
            ac_index=ac_index,
            reason=outcome.reason,
        )
        return replace(
            result,
            success=False,
            error=detail,
            final_message=detail,
            outcome=ACExecutionOutcome.FAILED,
            atomic_verifier_verdict=verdict,
        )

    async def _compute_sibling_flip_gated_out(
        self,
        *,
        seed: Seed,
        level_results: list[ACExecutionResult],
        session_id: str,
        execution_id: str,
    ) -> frozenset[int]:
        """Gate sibling-evidence flips for FAILED contract ACs (PR-V V4).

        A FAILED AC whose spec carries a success contract (``verify_command``
        OR non-empty ``expected_artifacts``) may only be flipped to satisfied by
        sibling evidence if its own contract passes the orchestrator gate now.
        ACs without a contract are never gated out.
        """
        if not self._run_verify_commands:
            return frozenset()
        gated_out: set[int] = set()
        cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
        for result in level_results:
            if result.success or result.outcome != ACExecutionOutcome.FAILED:
                continue
            ac_idx = result.ac_index
            if ac_idx < 0 or ac_idx >= len(seed.acceptance_criteria):
                continue
            spec = seed.acceptance_criteria[ac_idx]
            if not isinstance(spec, AcceptanceCriterionSpec) or not (
                spec.verify_command or spec.expected_artifacts
            ):
                continue
            outcome = await self._run_ac_verify_gate(spec=spec, cwd=cwd)
            if not outcome.passed:
                gated_out.add(ac_idx)
        return frozenset(gated_out)

    def _failure_class_for_result(self, result: ACExecutionResult) -> str | None:
        """Best-effort failure taxonomy label for a failed AC result."""
        verdict = result.atomic_verifier_verdict
        if verdict is not None and verdict.failure_class:
            return verdict.failure_class
        if result.error == _STALL_SENTINEL:
            from ouroboros.orchestrator.failure_taxonomy import FailureClass

            return FailureClass.STALL.value
        return None

    def _is_retryable_failure(self, result: ACExecutionResult | BaseException) -> bool:
        """Whether a batch result is a non-stall, non-blocked AC failure (PR-V V3)."""
        if not isinstance(result, ACExecutionResult):
            return False
        if result.success or result.is_blocked:
            return False
        # Stall retries are handled separately by the atomic leaf loop.
        return result.error != _STALL_SENTINEL

    def _build_ac_retry_prompt(
        self,
        *,
        result: ACExecutionResult,
        ac_content: str,
        is_final_attempt: bool,
    ) -> str:
        """Build the enriched retry prompt section for a re-dispatched AC (PR-V V3/V4)."""
        parts: list[str] = []
        failure_class = self._failure_class_for_result(result)
        if failure_class:
            parts.append(f"### Prior failure classification\n{failure_class}")
        last_error = result.error or result.final_message or ""
        if last_error and last_error != _STALL_SENTINEL:
            parts.append("### Last error (tail)\n" + last_error[-500:])
        if is_final_attempt:
            from ouroboros.resilience.lateral import (
                build_lateral_change_of_approach_directive,
            )

            parts.append(
                build_lateral_change_of_approach_directive(
                    problem_context=ac_content,
                    current_approach=(
                        "The previous attempts failed as described above; the same "
                        "approach is not working."
                    ),
                    failed_attempts=(failure_class,) if failure_class else (),
                )
            )
        return "\n\n".join(parts)

    async def _run_batch_with_verify_and_retry(
        self,
        *,
        seed: Seed,
        batch_executable: list[int],
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        ac_retry_attempts: dict[int, int],
        execution_counters: dict[str, int] | None,
    ) -> list[ACExecutionResult | BaseException]:
        """Dispatch a batch, apply the V1 verify gate, and retry failures (PR-V V1/V3/V4).

        Contract-less ACs with the verify gate off/absent and zero configured
        retries reduce to a single ``_execute_ac_batch`` call plus the identity
        gate, so today's behavior is preserved.
        """
        results = await self._execute_ac_batch(
            seed=seed,
            batch_indices=batch_executable,
            session_id=session_id,
            execution_id=execution_id,
            tools=tools,
            tool_catalog=tool_catalog,
            system_prompt=system_prompt,
            level_contexts=level_contexts,
            ac_retry_attempts=ac_retry_attempts,
            execution_counters=execution_counters,
            # The initial attempt is the AC's final same-runtime attempt only
            # when no same-runtime retries are configured; otherwise defer
            # cross-harness redispatch until the V3 loop below is spent.
            same_runtime_budget_exhausted=self._ac_retry_attempts <= 0,
        )
        # V1 gate on freshly-successful ACs.
        for position, ac_idx in enumerate(batch_executable):
            result = results[position]
            if isinstance(result, ACExecutionResult):
                results[position] = await self._apply_verify_gate(
                    seed=seed,
                    ac_index=ac_idx,
                    result=result,
                    session_id=session_id,
                    execution_id=execution_id,
                )

        if self._ac_retry_attempts <= 0:
            return results

        # V3 retry loop: re-dispatch non-stall failures up to the configured
        # attempts. Kill criterion: stop early when the failure class repeats.
        position_by_idx = {ac_idx: position for position, ac_idx in enumerate(batch_executable)}
        pending = {
            ac_idx
            for position, ac_idx in enumerate(batch_executable)
            if self._is_retryable_failure(results[position])
        }
        last_failure_class = {
            ac_idx: self._failure_class_for_result(results[position_by_idx[ac_idx]])
            for ac_idx in pending
        }

        while pending:
            retry_idxs = [
                ac_idx for ac_idx in pending if ac_retry_attempts[ac_idx] < self._ac_retry_attempts
            ]
            if not retry_idxs:
                break

            retry_prompts: dict[int, str] = {}
            for ac_idx in retry_idxs:
                ac_retry_attempts[ac_idx] += 1
                is_final = ac_retry_attempts[ac_idx] >= self._ac_retry_attempts
                prior = results[position_by_idx[ac_idx]]
                if isinstance(prior, ACExecutionResult):
                    retry_prompts[ac_idx] = self._build_ac_retry_prompt(
                        result=prior,
                        ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                        is_final_attempt=is_final,
                    )

            # Pending ACs advance their retry counter in lockstep, so the batch
            # is on its final same-runtime attempt exactly when every retried AC
            # has reached the configured cap. Only then may cross-harness
            # redispatch run inside the workers.
            retry_batch_final = all(
                ac_retry_attempts[ac_idx] >= self._ac_retry_attempts for ac_idx in retry_idxs
            )
            retry_results = await self._execute_ac_batch(
                seed=seed,
                batch_indices=retry_idxs,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                ac_retry_attempts=ac_retry_attempts,
                execution_counters=execution_counters,
                retry_prompts=retry_prompts,
                same_runtime_budget_exhausted=retry_batch_final,
            )

            for retry_position, ac_idx in enumerate(retry_idxs):
                gated = retry_results[retry_position]
                if isinstance(gated, ACExecutionResult):
                    gated = await self._apply_verify_gate(
                        seed=seed,
                        ac_index=ac_idx,
                        result=gated,
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                results[position_by_idx[ac_idx]] = gated

                if not self._is_retryable_failure(gated):
                    pending.discard(ac_idx)
                    continue
                new_class = (
                    self._failure_class_for_result(gated)
                    if isinstance(gated, ACExecutionResult)
                    else None
                )
                if (
                    new_class is not None
                    and last_failure_class.get(ac_idx) is not None
                    and new_class == last_failure_class[ac_idx]
                ):
                    # Identical failure class on every attempt: stop early
                    # rather than burning the last attempt.
                    log.info(
                        "parallel_executor.ac.retry_early_stop",
                        session_id=session_id,
                        ac_index=ac_idx,
                        failure_class=new_class,
                    )
                    # The same-runtime path has given up before the retry cap, so
                    # its recovery budget is effectively spent — the alt-harness
                    # boundary. When this dispatch was not already the final
                    # attempt (``retry_batch_final``), its workers never got the
                    # cross-harness hook, so open it here for the (eligible) AC.
                    if not retry_batch_final and isinstance(gated, ACExecutionResult):
                        alt = await self._maybe_redispatch_alt_harness_for_batch_ac(
                            seed=seed,
                            ac_idx=ac_idx,
                            result=gated,
                            session_id=session_id,
                            execution_id=execution_id,
                            tools=tools,
                            tool_catalog=tool_catalog,
                            system_prompt=system_prompt,
                            level_contexts=level_contexts,
                            execution_counters=execution_counters,
                            retry_attempt=ac_retry_attempts[ac_idx],
                        )
                        if isinstance(alt, ACExecutionResult):
                            # The alternate ran via _execute_single_ac, which has
                            # no seed-level success contract — apply the same V1
                            # verify gate the same-runtime results get, so an
                            # alternate 'success' with a failing verify_command or
                            # missing expected artifact is not accepted as success.
                            results[position_by_idx[ac_idx]] = await self._apply_verify_gate(
                                seed=seed,
                                ac_index=ac_idx,
                                result=alt,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                    pending.discard(ac_idx)
                    continue
                last_failure_class[ac_idx] = new_class
                if ac_retry_attempts[ac_idx] >= self._ac_retry_attempts:
                    pending.discard(ac_idx)

        return results

    async def _maybe_redispatch_alt_harness_for_batch_ac(
        self,
        *,
        seed: Seed,
        ac_idx: int,
        result: ACExecutionResult,
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        execution_counters: dict[str, int] | None,
        retry_attempt: int,
    ) -> ACExecutionResult | None:
        """Give a terminally-failing top-level batch AC one cross-harness redispatch.

        Used at the retry loop's early-stop boundary (repeated failure class),
        where the same-runtime recovery has given up before the retry counter cap
        and the workers therefore never reached the in-worker alt-harness hook.
        Rebuilds the top-level re-run bundle and defers to the shared
        :meth:`_maybe_redispatch_alt_harness`, so the alternate-harness decision,
        the one-per-AC cap, and the failed-alt surfacing all stay in one place.
        """
        execution_context_id = execution_id or session_id
        rerun_kwargs: dict[str, Any] = {
            "ac_index": ac_idx,
            "ac_content": ac_text(seed.acceptance_criteria[ac_idx]),
            "session_id": session_id,
            "tools": tools,
            "tool_catalog": tool_catalog,
            "system_prompt": system_prompt,
            "seed_goal": seed.goal,
            "depth": 0,
            "execution_id": execution_id,
            "level_contexts": level_contexts,
            "sibling_acs": [],
            "execution_counters": execution_counters,
            "is_sub_ac": False,
            "parent_ac_index": None,
            "sub_ac_index": None,
            "node_identity": None,
        }
        return await self._maybe_redispatch_alt_harness(
            result=result,
            execution_context_id=execution_context_id,
            rerun_kwargs=rerun_kwargs,
            atomic_retry_attempt=retry_attempt,
            stall_retries_exhausted=False,
        )

    def _fat_harness_acceptance_error(
        self,
        *,
        runtime_success: bool,
        typed_evidence: EvidenceRecord | None,
        typed_validation: ValidationResult | None,
        typed_error: str | None,
        verifier_verdict: VerifierVerdict | None,
    ) -> str | None:
        """Return the fat-harness rejection reason for an atomic leaf."""
        if not self._fat_harness_mode or not runtime_success:
            return None
        if self._execution_profile is None:
            return "Fat-harness mode requires a loaded execution profile."
        if typed_evidence is None:
            return typed_error or "Fat-harness mode requires typed evidence."
        if typed_validation is None:
            return "Fat-harness mode could not validate typed evidence."
        if typed_validation.ok:
            if verifier_verdict is None:
                return "Fat-harness mode requires verifier PASS before atomic acceptance."
            if verifier_verdict.passed:
                return None
            detail = "; ".join(verifier_verdict.reasons) or "verifier rejected atomic evidence"
            return f"Fat-harness verifier failed ({detail})."

        reasons: list[str] = []
        if typed_validation.missing_fields:
            reasons.append("missing fields: " + ", ".join(typed_validation.missing_fields))
        if typed_validation.rejected_by:
            reasons.append("rejected by: " + ", ".join(typed_validation.rejected_by))
        if typed_validation.blocker is not None:
            reasons.append("blocker: " + typed_validation.blocker.summary())
        detail = "; ".join(reasons) if reasons else "profile evidence validation failed"
        return f"Fat-harness typed evidence validation failed ({detail})."

    def _run_atomic_verifier_pass(
        self,
        *,
        ac_content: str,
        final_message: str,
        success: bool,
        messages: tuple[AgentMessage, ...],
        typed_evidence: EvidenceRecord | None,
        typed_validation: ValidationResult | None,
        ac_spec: AcceptanceCriterionSpec | None = None,
    ) -> VerifierVerdict | None:
        """Run the separate verifier pass once typed evidence is schema-valid."""
        if (
            not success
            or not self._fat_harness_mode
            or self._execution_profile is None
            or typed_evidence is None
            or typed_validation is None
            or not typed_validation.ok
        ):
            return None

        verifier = self._atomic_verifier
        try:
            effective_schema = _effective_evidence_schema_for_ac(
                self._execution_profile, ac_content
            )
            effective_profile = _profile_with_evidence_schema(
                self._execution_profile, effective_schema
            )
            scoped_evidence = _scoped_evidence_record_for_ac(
                self._execution_profile,
                ac_content,
                typed_evidence,
            )
            verdict = (
                verifier(
                    profile=effective_profile,
                    ac=ac_content,
                    leaf_output=final_message,
                    record=scoped_evidence,
                )
                if verifier is not None
                else self._verify_atomic_evidence_against_runtime_messages(
                    messages=messages,
                    typed_evidence=scoped_evidence,
                    ac_content=ac_content,
                    verify_command=(
                        ac_spec.verify_command
                        if isinstance(ac_spec, AcceptanceCriterionSpec)
                        else None
                    ),
                )
            )
        except VerifierContractError:
            raise
        except Exception as exc:
            verdict = verifier_operational_failure_verdict(exc)
        if not isinstance(verdict, VerifierVerdict):
            msg = f"Atomic verifier returned {type(verdict).__name__}, expected VerifierVerdict."
            raise VerifierContractError(msg)
        return verdict

    def _verify_atomic_evidence_against_runtime_messages(
        self,
        *,
        messages: tuple[AgentMessage, ...],
        typed_evidence: EvidenceRecord,
        ac_content: str,
        verify_command: str | None = None,
    ) -> VerifierVerdict:
        return _verify_atomic_evidence_against_runtime_messages(
            messages=messages,
            typed_evidence=typed_evidence,
            ac_content=ac_content,
            execution_profile=self._execution_profile,
            task_cwd=self._task_cwd,
            adapter_working_directory=self._adapter.working_directory,
            verify_command=verify_command,
        )

    async def _emit_atomic_typed_evidence_event(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str,
        session_id: str | None,
        ac_content: str,
        typed_evidence: EvidenceRecord | None,
        typed_validation: ValidationResult | None,
        typed_error: str | None,
        verifier_verdict: VerifierVerdict | None = None,
        enforcement_error: str | None = None,
    ) -> None:
        """Persist typed-evidence metadata for atomic AC completion."""
        if self._execution_profile is None:
            return

        data: dict[str, Any] = {
            **runtime_identity.to_metadata(),
            **self._decomposition_profile_metadata(),
            "execution_id": execution_id,
            "session_id": session_id,
            "acceptance_criterion": ac_content,
            "profile": self._execution_profile.profile,
            "required_fields": list(
                _effective_evidence_schema_for_ac(self._execution_profile, ac_content).required
            ),
            "observe_only": not self._fat_harness_mode,
            "enforced": self._fat_harness_mode,
            "fat_harness_mode": self._fat_harness_mode,
            "enforcement_error": enforcement_error,
            "typed_evidence_present": typed_evidence is not None,
            "typed_evidence_valid": typed_validation.ok if typed_validation is not None else False,
            "typed_evidence_error": typed_error,
            "verifier_ran": verifier_verdict is not None,
            "verifier_passed": verifier_verdict.passed if verifier_verdict is not None else False,
        }
        if verifier_verdict is not None:
            data["verifier_reasons"] = list(verifier_verdict.reasons)
            data["verifier_failure_class"] = verifier_verdict.failure_class
            data["verifier_status"] = verifier_verdict.status.value
            data["retry_admission"] = verifier_verdict.retry_admission.value
            data["verifier_evidence_used"] = list(verifier_verdict.evidence_used)
        if typed_evidence is not None:
            data["typed_evidence_fields"] = sorted(typed_evidence.data)
            data["ignored_out_of_scope_evidence_fields"] = list(
                _out_of_scope_evidence_fields_for_ac(
                    self._execution_profile,
                    ac_content,
                    typed_evidence,
                )
            )
            data["ignored_out_of_scope_evidence"] = _out_of_scope_evidence_values_for_ac(
                self._execution_profile,
                ac_content,
                typed_evidence,
            )
        if typed_validation is not None:
            data["missing_fields"] = list(typed_validation.missing_fields)
            data["rejected_by"] = list(typed_validation.rejected_by)
            data["blocker"] = (
                typed_validation.blocker.summary() if typed_validation.blocker is not None else None
            )

        await self._event_emitter.emit_atomic_typed_evidence_observed(
            runtime_identity=runtime_identity,
            data=data,
        )

    async def _emit_subtask_event(
        self,
        execution_id: str,
        ac_index: int,
        sub_task_index: int,
        sub_task_content: str,
        status: str,
        node_identity: ExecutionNodeIdentity | None = None,
    ) -> None:
        """Emit sub-task event for TUI tree updates.

        ``ac_index`` arrives 0-based from the executor loop but the TUI
        tree keys AC nodes as ``ac_{1-based}``, so we convert here.
        """
        label = _subtask_event_label(sub_task_content)
        await self._event_emitter.emit_subtask_event(
            execution_id,
            ac_index,
            sub_task_index,
            sub_task_content,
            status,
            node_identity,
            label=label,
        )

    async def _emit_level_started(
        self,
        session_id: str,
        level: int,
        ac_indices: list[int],
        total_levels: int,
    ) -> None:
        """Emit event when a parallel level starts."""
        await self._event_emitter.emit_level_started(
            session_id,
            level,
            ac_indices,
            total_levels,
            decomposition_profile_metadata=self._decomposition_profile_metadata(),
        )

    async def _emit_level_completed(
        self,
        session_id: str,
        level: int,
        success_count: int,
        failure_count: int,
        blocked_count: int = 0,
        started: bool = True,
        outcome: str | None = None,
    ) -> None:
        """Emit event when a parallel level completes."""
        await self._event_emitter.emit_level_completed(
            session_id,
            level,
            success_count,
            failure_count,
            blocked_count=blocked_count,
            started=started,
            outcome=outcome,
        )

    async def _resilient_progress_emitter(
        self,
        session_id: str,
        execution_id: str,
        seed: Seed,
        ac_statuses: dict[int, str],
        progress_state: dict[str, int],
        interval: float = 15.0,
        max_consecutive_errors: int = 5,
    ) -> None:
        """Periodically emit workflow progress with error resilience (RC2 + RC4).

        Runs as a background task inside a task group. Terminates when:
        - All ACs are in terminal state (RC4: no stale monitoring)
        - Consecutive errors exceed threshold (RC2: graceful degradation)
        - Task group cancel scope triggers (execution loop finished)

        Args:
            session_id: Session ID.
            execution_id: Execution ID.
            seed: Seed specification.
            ac_statuses: Shared dict of AC statuses (mutated externally).
            progress_state: Shared dict with ``current_level`` and ``total_levels``
                keys, mutated by the main execution loop.
            interval: Seconds between emissions.
            max_consecutive_errors: Stop after this many consecutive failures.
        """
        consecutive_errors = 0
        terminal_states = {"completed", "failed", "skipped"}

        while True:
            await anyio.sleep(interval)

            # RC4: Stop when all ACs are done
            if all(s in terminal_states for s in ac_statuses.values()):
                log.info("parallel_executor.progress_emitter.all_done")
                return

            try:
                await self._emit_workflow_progress(
                    session_id=session_id,
                    execution_id=execution_id,
                    seed=seed,
                    ac_statuses=ac_statuses,
                    ac_retry_attempts=None,
                    executing_indices=[i for i, s in ac_statuses.items() if s == "executing"],
                    completed_count=sum(1 for s in ac_statuses.values() if s == "completed"),
                    current_level=progress_state.get("current_level", 0),
                    total_levels=progress_state.get("total_levels", 0),
                    activity="Monitoring",
                )
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                wait = min(2.0**consecutive_errors, 30.0)
                log.warning(
                    "parallel_executor.progress_emitter.error",
                    error=str(e),
                    consecutive_errors=consecutive_errors,
                )
                if consecutive_errors >= max_consecutive_errors:
                    log.error(
                        "parallel_executor.progress_emitter.giving_up",
                        consecutive_errors=consecutive_errors,
                    )
                    return
                await anyio.sleep(wait)

    async def _emit_workflow_progress(
        self,
        session_id: str,
        execution_id: str,
        seed: Seed,
        ac_statuses: dict[int, str],
        ac_retry_attempts: dict[int, int] | None,
        executing_indices: list[int],
        completed_count: int,
        current_level: int,
        total_levels: int,
        activity: str = "Executing",
        messages_count: int = 0,
        tool_calls_count: int = 0,
    ) -> None:
        """Emit workflow progress event for TUI updates.

        Args:
            session_id: Session ID.
            execution_id: Execution ID.
            seed: Seed specification.
            ac_statuses: Dict mapping AC index to status string.
            ac_retry_attempts: Dict mapping AC index to reopen retry count.
            executing_indices: Currently executing AC indices.
            completed_count: Number of completed ACs.
            current_level: Current execution level.
            total_levels: Total execution levels.
            activity: Current activity description.
        """
        await self._event_emitter.emit_workflow_progress(
            session_id,
            execution_id,
            seed,
            ac_statuses,
            ac_retry_attempts,
            executing_indices,
            completed_count,
            current_level,
            total_levels,
            activity=activity,
            messages_count=messages_count,
            tool_calls_count=tool_calls_count,
        )


__all__ = [
    "ACExecutionOutcome",
    "ACExecutionResult",
    "ParallelExecutionStageResult",
    "StageExecutionOutcome",
    "ParallelExecutionResult",
    "ParallelACExecutor",
]
