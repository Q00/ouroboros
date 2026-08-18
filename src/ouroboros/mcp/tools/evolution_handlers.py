"""Evolution-related tool handlers for MCP server.

Contains handlers for evolutionary loop operations:
- EvolveStepHandler: Run one generation of the evolutionary loop
- EvolveRewindHandler: Rewind a lineage to a specific generation
- LineageStatusHandler: Query lineage state without running a generation
- StartEvolveStepHandler: Start an evolve_step asynchronously (background job)
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import os
from pathlib import Path
import time
from typing import Any

import structlog
import yaml

from ouroboros.auto.checkpoint_commits import checkpoint_passed_ac
from ouroboros.auto.state import AutoCommitPolicy, AutoPipelineState
from ouroboros.config import get_runtime_controls_config
from ouroboros.core.conductor import (
    ConductorDirective,
    stable_payload_digest,
    validate_conductor_successor_authorization,
)
from ouroboros.core.project_paths import resolve_path_against_base, resolve_seed_project_path
from ouroboros.core.seed import Seed, ac_texts
from ouroboros.core.text import truncate_head_tail
from ouroboros.core.types import Result
from ouroboros.core.worktree import (
    TaskWorkspace,
    WorktreeError,
    is_git_repo,
    maybe_restore_task_workspace,
    release_lock,
)
from ouroboros.evaluation.verification_artifacts import build_verification_artifacts
from ouroboros.events.conductor import create_conductor_directive_attached_event
from ouroboros.evolution import loop_support
from ouroboros.evolution.frugality import (
    PROOF_EVENT,
    EvolutionFrugalityProof,
    capture_project_baseline,
)
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager
from ouroboros.mcp.tools.background import (
    BackgroundJobAcceptanceState,
    start_background_tool_job,
)
from ouroboros.mcp.tools.bridge_mixin import BridgeAwareMixin
from ouroboros.mcp.tools.evolve_start_claim import (
    PreparedEvolveClaim,
    evolve_lineage_busy_error,
)
from ouroboros.mcp.tools.job_observer import (
    append_job_observer_inline_handoff,
    build_job_observer_contract,
)
from ouroboros.mcp.tools.subagent import (
    DELEGATED_TO_PLUGIN,
    DELEGATED_TO_SUBAGENT,
    build_evolve_subagent,
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
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

_EVOLVE_HANDLER_DEFAULTS: dict[str, object] = {
    "seed_content": None,
    "execute": True,
    "benchmark_control": False,
    "parallel": True,
    "skip_qa": False,
    "project_dir": None,
    "commit_policy": None,
    "auto_session_id": None,
    "execution_id": None,
    "checkpoint_commits": [],
    "checkpoint_attempted_ac_ids": [],
    "conductor_decision_id": None,
    "predecessor_execution_id": None,
    "conductor_directive": None,
    "recover_expired_claim": False,
}


async def _benchmark_control_error(
    arguments: dict[str, Any],
    *,
    event_store: EventStore,
    lineage_id: str,
) -> str | None:
    """Fail closed unless an explicit Gen 2+ control has a clean Git baseline."""
    requested = arguments.get("benchmark_control", False)
    if type(requested) is not bool:
        return "benchmark_control must be a boolean"
    if not requested:
        return None
    if arguments.get("execute", True) is not True:
        return "benchmark_control requires execute=true"
    if arguments.get("seed_content") not in (None, ""):
        return "benchmark_control is Gen 2+ only; omit seed_content"
    project_dir = arguments.get("project_dir")
    if not isinstance(project_dir, str) or not project_dir.strip():
        return "benchmark_control requires an explicit project_dir"
    await event_store.initialize()
    events = await event_store.replay_lineage(lineage_id)
    if not any(
        event.type == "lineage.generation.completed"
        and isinstance(event.data.get("generation_number"), int)
        and event.data["generation_number"] >= 1
        for event in events
    ):
        return "benchmark_control requires an existing completed generation (Gen 2+)"
    baseline = capture_project_baseline(project_dir)
    if baseline.git_commit is None or baseline.workspace_path is None:
        return "benchmark_control requires project_dir to be an explicit Git project/worktree"
    if not baseline.clean:
        return "benchmark_control requires a clean Git baseline"
    return None


def _evolve_handler_request_key(
    arguments: dict[str, Any],
    *,
    configured_project_dir: str | None,
    fallback_cwd: Path,
    execution_policy: dict[str, Any] | None = None,
) -> str:
    """Canonicalize semantically equivalent public retries before hashing."""
    normalized = dict(arguments)
    for name, default in _EVOLVE_HANDLER_DEFAULTS.items():
        normalized.setdefault(name, default)
    # Recovery authorizes takeover of an expired owner; it does not change the
    # generation operation whose durable request identity must still match.
    normalized["recover_expired_claim"] = False
    seed_content = normalized["seed_content"]
    if isinstance(seed_content, str) and seed_content:
        try:
            normalized["seed_content"] = Seed.from_dict(yaml.safe_load(seed_content)).to_dict()
        except Exception:
            pass
    elif not seed_content:
        normalized["seed_content"] = None
    project_dir = normalized["project_dir"]
    resolved_project_dir = (
        resolve_path_against_base(project_dir, stable_base=fallback_cwd)
        if isinstance(project_dir, str) and project_dir
        else None
    )
    configured = (
        resolve_path_against_base(configured_project_dir, stable_base=fallback_cwd)
        if configured_project_dir
        else None
    )
    effective_project_dir = resolved_project_dir or configured or fallback_cwd
    normalized["project_dir"] = str(effective_project_dir)
    return stable_payload_digest(
        {
            "arguments": normalized,
            "execution_policy": execution_policy,
        }
    )


async def _resolve_conductor_directive(
    *,
    arguments: dict[str, Any],
    event_store: EventStore | None,
    target_type: str,
    target_id: str,
    tool_name: str,
) -> Result[ConductorDirective | None, MCPToolError]:
    raw_directive = arguments.get("conductor_directive")
    if raw_directive is None:
        return Result.ok(None)
    if not isinstance(raw_directive, dict):
        return Result.err(
            MCPToolError("conductor_directive must be an object", tool_name=tool_name)
        )
    decision_id = arguments.get("conductor_decision_id")
    predecessor_execution_id = arguments.get("predecessor_execution_id")
    if not isinstance(decision_id, str) or not decision_id.strip():
        return Result.err(MCPToolError("conductor_decision_id is required", tool_name=tool_name))
    if not isinstance(predecessor_execution_id, str) or not predecessor_execution_id.strip():
        return Result.err(MCPToolError("predecessor_execution_id is required", tool_name=tool_name))
    if event_store is None:
        return Result.err(
            MCPToolError(
                "EventStore is required for conductor successor audit", tool_name=tool_name
            )
        )
    try:
        directive = ConductorDirective.from_mapping(raw_directive)
        await event_store.initialize()
        decision_events = await event_store.replay("conductor_decision", decision_id.strip())
        selected = next(
            (event for event in decision_events if event.type == "conductor.decision.selected"),
            None,
        )
        if selected is None:
            raise ValueError("No selected conductor decision receipt matches conductor_decision_id")
        validate_conductor_successor_authorization(
            selected.data,
            directive=directive,
            predecessor_execution_id=predecessor_execution_id.strip(),
        )
        target_events = await event_store.replay(target_type, target_id)
        attached = next(
            (
                event
                for event in target_events
                if event.type == "conductor.directive.attached"
                and event.data.get("decision_id") == decision_id.strip()
            ),
            None,
        )
        if attached is not None:
            if attached.data.get("conductor_directive_digest") != directive.digest:
                raise ValueError("The successor target already has a different directive receipt")
        else:
            await event_store.append(
                create_conductor_directive_attached_event(
                    decision_id=decision_id.strip(),
                    target_type=target_type,
                    target_id=target_id,
                    predecessor_execution_id=predecessor_execution_id.strip(),
                    directive=directive,
                )
            )
    except (TypeError, ValueError) as exc:
        return Result.err(MCPToolError(str(exc), tool_name=tool_name))
    return Result.ok(directive)


def _resolve_verification_working_dir(
    project_dir: str | None,
    seed: Seed | None,
    *,
    stable_base: Path,
) -> Path:
    """Resolve the best project directory for post-run verification."""
    if project_dir:
        # ``project_dir`` is the explicit MCP tool argument supplied by the
        # caller — trusted source — so containment enforcement is left off.
        # Untrusted seed-encoded paths are handled by
        # ``resolve_seed_project_path`` below, which always enforces it.
        resolved = resolve_path_against_base(project_dir, stable_base=stable_base)
        if resolved is not None:
            return resolved

    resolution = resolve_seed_project_path(seed, stable_base=stable_base)
    if resolution.path is not None:
        return resolution.path
    if resolution.rejected:
        log.warning(
            "evolution_handlers.seed_project_path_rejected",
            stable_base=str(stable_base),
            reason="every seed-encoded project path escaped the stable base",
        )
    return stable_base


def _resolve_evolve_verification_working_dir(
    explicit_project_dir: str | None,
    configured_project_dir: str | None,
    generation_seed: Seed | None,
    initial_seed: Seed | None,
) -> Path:
    """Resolve the best project directory for evolve-step verification."""
    # ``explicit_project_dir`` and ``configured_project_dir`` are trusted
    # caller/config inputs (MCP arg or local config file), so the absolute
    # paths they may carry are accepted without containment enforcement.
    # Containment is enforced for seed-encoded candidates via
    # ``resolve_seed_project_path`` (called from ``_resolve_verification_working_dir``).
    cwd_base = Path.cwd().resolve()
    if explicit_project_dir:
        resolved = resolve_path_against_base(explicit_project_dir, stable_base=cwd_base)
        if resolved is not None:
            return resolved

    if configured_project_dir:
        resolved = resolve_path_against_base(configured_project_dir, stable_base=cwd_base)
        if resolved is not None:
            return resolved

    stable_base = (
        resolve_path_against_base(configured_project_dir, stable_base=cwd_base)
        or resolve_path_against_base(explicit_project_dir, stable_base=cwd_base)
        or cwd_base
    )

    for candidate_seed in (generation_seed, initial_seed):
        candidate_dir = _resolve_verification_working_dir(
            None,
            candidate_seed,
            stable_base=stable_base,
        )
        if candidate_dir != stable_base:
            return candidate_dir

    return stable_base


def _resolve_checkpoint_working_dir(
    workspace: TaskWorkspace | None,
    resolved_verification_working_dir: Path,
) -> Path:
    """Resolve the directory AC checkpoint commits must target.

    Checkpoint commits have to be created in the same filesystem root that the
    generation actually mutated. When ``maybe_restore_task_workspace`` created a
    managed lineage worktree, execution ran in ``workspace.effective_cwd`` — not
    the caller's original ``project_dir`` that
    ``_resolve_evolve_verification_working_dir`` prefers. Committing the parent
    project dir there records the AC as *attempted* with ``no_safe_changes`` (no
    diff outside the worktree), so the passed AC is never checkpointed. Prefer
    the effective worktree whenever one is active.
    """
    if workspace is not None:
        return Path(workspace.effective_cwd)
    return resolved_verification_working_dir


@dataclass
class EvolveStepHandler(BridgeAwareMixin):
    """Handler for the ouroboros_evolve_step tool.

    Runs exactly ONE generation of the evolutionary loop.
    Designed for Ralph integration: stateless between calls,
    all state reconstructed from events.

    Inherits :class:`BridgeAwareMixin` (#475) so the composition
    root's loop-injection automatically populates ``mcp_manager`` and
    ``mcp_tool_prefix`` when an MCP bridge is configured. The bridge
    is forwarded into the inner ``EvolutionaryLoop`` runner whenever it
    is available so dynamic external MCP servers reach the evolution
    pipeline without per-handler explicit wiring.
    """

    evolutionary_loop: Any | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    @property
    def TIMEOUT_SECONDS(self) -> float:
        """MCP adapter timeout for evolve_step.

        Defaults to 0 so the generation watchdog, not adapter wall-clock,
        decides whether long-running work is still productive.
        """
        return get_runtime_controls_config().mcp_tool_timeout_seconds

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evolve_step",
            description=(
                "Run exactly ONE generation of the evolutionary loop. "
                "For Gen 1: provide lineage_id and seed_content (YAML). "
                "For Gen 2+: provide lineage_id only (state reconstructed from events). "
                "Returns generation result, convergence signal, and next action "
                "(continue/converged/ontology_stable/stagnated/exhausted/failed). "
                "ontology_stable is a non-success handoff; rerun the same lineage "
                "with execute=true to perform Execute→Evaluate."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="Lineage ID to continue or new ID for Gen 1",
                    required=True,
                ),
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description=(
                        "Seed YAML text for Gen 1. Pass a YAML-formatted string "
                        "(for example, newline-delimited 'goal: ...' fields), not "
                        "JSON-shaped text or an object literal; some MCP clients may "
                        "otherwise coerce the value to an object before Ouroboros "
                        "receives it. "
                        "Omit for Gen 2+ (seed reconstructed from events)."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="execute",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Whether to run seed execution and evaluation. "
                        "True (default): full pipeline with Execute→Validate→Evaluate. "
                        "False: ontology-only evolution (fast, no execution)."
                    ),
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="benchmark_control",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Run a deliberate full-graph frugality control. Default: false. "
                        "Requires execute=true, Gen 2+, an explicit Git project_dir, "
                        "and a clean baseline; unavailable through plugin delegation."
                    ),
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="parallel",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Whether to run ACs in parallel. "
                        "True (default): parallel execution (fast, may cause import conflicts). "
                        "False: sequential execution (slower, more stable code generation)."
                    ),
                    required=False,
                    default=True,
                ),
                MCPToolParameter(
                    name="skip_qa",
                    type=ToolInputType.BOOLEAN,
                    description="Skip post-execution QA evaluation. Default: false",
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="recover_expired_claim",
                    type=ToolInputType.BOOLEAN,
                    description=(
                        "Explicitly recover an expired lineage writer claim after confirming "
                        "the prior owner process is dead. Default: false"
                    ),
                    required=False,
                    default=False,
                ),
                MCPToolParameter(
                    name="project_dir",
                    type=ToolInputType.STRING,
                    description=(
                        "Project root directory for validation (pytest collection check). "
                        "If omitted, auto-detected from execution output or CWD."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="commit_policy",
                    type=ToolInputType.STRING,
                    description="Optional checkpoint commit policy for passed ACs.",
                    required=False,
                ),
                MCPToolParameter(
                    name="auto_session_id",
                    type=ToolInputType.STRING,
                    description="Optional auto session id for checkpoint commit metadata.",
                    required=False,
                ),
                MCPToolParameter(
                    name="execution_id",
                    type=ToolInputType.STRING,
                    description="Optional execution id for checkpoint commit metadata.",
                    required=False,
                ),
                MCPToolParameter(
                    name="checkpoint_commits",
                    type=ToolInputType.ARRAY,
                    description="Existing checkpoint commit records for idempotency.",
                    required=False,
                ),
                MCPToolParameter(
                    name="checkpoint_attempted_ac_ids",
                    type=ToolInputType.ARRAY,
                    description="Acceptance criteria already considered for checkpoint commits.",
                    required=False,
                ),
                MCPToolParameter(
                    name="conductor_decision_id",
                    type=ToolInputType.STRING,
                    description="Selected conductor decision authorizing this successor generation.",
                    required=False,
                ),
                MCPToolParameter(
                    name="predecessor_execution_id",
                    type=ToolInputType.STRING,
                    description="Execution or generation that this successor follows.",
                    required=False,
                ),
                MCPToolParameter(
                    name="conductor_directive",
                    type=ToolInputType.OBJECT,
                    description="Bounded corrective context for this successor generation.",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
        *,
        on_generation_claimed: Callable[[int], Awaitable[None]] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Coalesce the complete public request before workspace acquisition."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_evolve_step",
                )
            )
        event_store = self.event_store or getattr(self.evolutionary_loop, "event_store", None)
        benchmark_control = arguments.get("benchmark_control", False)
        if not isinstance(event_store, EventStore):
            if benchmark_control is not False:
                return Result.err(
                    MCPToolError(
                        "benchmark_control requires a durable EventStore",
                        tool_name="ouroboros_evolve_step",
                    )
                )
            return await self._handle_once(dict(arguments))
        benchmark_error = await _benchmark_control_error(
            arguments,
            event_store=event_store,
            lineage_id=str(lineage_id),
        )
        if benchmark_error is not None:
            return Result.err(MCPToolError(benchmark_error, tool_name="ouroboros_evolve_step"))
        if benchmark_control is True and should_dispatch_via_plugin(
            self.agent_runtime_backend,
            self.opencode_mode,
        ):
            return Result.err(
                MCPToolError(
                    "benchmark_control requires the in-process evolve runtime",
                    tool_name="ouroboros_evolve_step",
                )
            )
        configured_project_dir = (
            self.evolutionary_loop.get_project_dir()
            if self.evolutionary_loop is not None
            and callable(getattr(self.evolutionary_loop, "get_project_dir", None))
            else None
        )
        request_key = _evolve_handler_request_key(
            arguments,
            configured_project_dir=configured_project_dir,
            fallback_cwd=Path.cwd().resolve(),
            execution_policy=loop_support.evolution_execution_policy(
                getattr(self.evolutionary_loop, "config", None),
                benchmark_control=benchmark_control is True,
            ),
        )
        recovered_generation: int | None = None
        recovered_evolve_result: Any | None = None
        if arguments.get("recover_expired_claim") is True:
            from ouroboros.evolution.step_receipt import decode_step_result
            from ouroboros.persistence import lineage_claims

            await event_store.initialize()
            stale_handler = await lineage_claims.observe(
                event_store,
                scope="evolve-handler",
                lineage_id=str(lineage_id),
            )
            if (
                stale_handler is not None
                and not stale_handler.completed
                and stale_handler.lease_expires_at_ms <= int(time.time() * 1000)
            ):
                if stale_handler.request_key != request_key:
                    return Result.err(
                        MCPToolError(
                            "Expired evolve handler must be recovered with its original request",
                            tool_name="ouroboros_evolve_step",
                        )
                    )
                recovered_generation = stale_handler.generation_number
                core_receipt = await lineage_claims.observe(
                    event_store,
                    scope="evolve-core",
                    lineage_id=str(lineage_id),
                )
                if (
                    core_receipt is not None
                    and core_receipt.completed
                    and core_receipt.generation_number != recovered_generation
                ):
                    return Result.err(
                        MCPToolError(
                            "Expired evolve handler disagrees with the durable core generation",
                            tool_name="ouroboros_evolve_step",
                        )
                    )
                if (
                    core_receipt is not None
                    and core_receipt.completed
                    and core_receipt.generation_number == recovered_generation
                ):
                    if core_receipt.result_payload is None:
                        return Result.err(
                            MCPToolError(
                                "Expired evolve handler has no durable core result to recover",
                                tool_name="ouroboros_evolve_step",
                            )
                        )
                    try:
                        recovered_evolve_result = await decode_step_result(
                            event_store,
                            core_receipt.result_payload,
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        return Result.err(
                            MCPToolError(
                                f"Failed to recover durable evolve result: {exc}",
                                tool_name="ouroboros_evolve_step",
                            )
                        )
            for scope in ("evolve-handler", "evolve-core"):
                await lineage_claims.recover_expired(
                    event_store,
                    scope=scope,
                    lineage_id=str(lineage_id),
                )
        durable_public_result = not should_dispatch_via_plugin(
            self.agent_runtime_backend,
            self.opencode_mode,
        )

        async def run_public_request() -> Result[MCPToolResult, MCPServerError]:
            if not durable_public_result:
                return await self._handle_once(dict(arguments))
            from ouroboros.mcp.tools.evolve_handler_receipt import (
                decode_evolve_handler_result,
                encode_evolve_handler_result,
            )

            async def run_bounded_handler(
                claimed_callback: Callable[[int], Awaitable[None]] | None,
            ) -> Result[MCPToolResult, MCPServerError]:
                if recovered_evolve_result is not None and claimed_callback is not None:
                    assert recovered_generation is not None
                    await claimed_callback(recovered_generation)
                    claimed_callback = None
                raw_result = await self._handle_once(
                    dict(arguments),
                    recovered_evolve_result=recovered_evolve_result,
                    on_generation_claimed=claimed_callback,
                )
                return decode_evolve_handler_result(encode_evolve_handler_result(raw_result))

            async def run_bound_handler_claim(
                _projected_generation: int,
                rebind_generation: Callable[[int], Awaitable[None]],
            ) -> Result[MCPToolResult, MCPServerError]:
                async def publish_authoritative_generation(generation_number: int) -> None:
                    await rebind_generation(generation_number)
                    if on_generation_claimed is not None:
                        await on_generation_claimed(generation_number)

                return await run_bounded_handler(publish_authoritative_generation)

            while True:
                generation_number = recovered_generation
                if generation_number is None:
                    generation_number = await loop_support.planned_evolve_generation(
                        event_store,
                        str(lineage_id),
                        execute=bool(arguments.get("execute", True)),
                    )
                try:
                    return await loop_support.run_durable_lineage_single_flight(
                        event_store,
                        str(lineage_id),
                        request_key,
                        lambda: run_bounded_handler(on_generation_claimed),
                        generation_number=generation_number,
                        encode=encode_evolve_handler_result,
                        decode=decode_evolve_handler_result,
                        scope="evolve-handler",
                        operation_with_claim=run_bound_handler_claim,
                    )
                except loop_support.LineageWinnerAdvanced:
                    continue

        if on_generation_claimed is not None:
            return await run_public_request()
        return await loop_support.run_lineage_single_flight(
            event_store,
            str(lineage_id),
            request_key,
            run_public_request,
            scope="evolve-handler",
        )

    async def _handle_once(
        self,
        arguments: dict[str, Any],
        *,
        recovered_evolve_result: Any | None = None,
        on_generation_claimed: Callable[[int], Awaitable[None]] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Run one complete evolve handler request under public ownership."""
        lineage_id = arguments.get("lineage_id")
        assert lineage_id

        directive_result = await _resolve_conductor_directive(
            arguments=arguments,
            event_store=self.event_store or getattr(self.evolutionary_loop, "event_store", None),
            target_type="lineage",
            target_id=lineage_id,
            tool_name="ouroboros_evolve_step",
        )
        if directive_result.is_err:
            return Result.err(directive_result.error)
        conductor_directive = directive_result.value

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        # Parity with qa / execute_seed / lateral / evaluate handlers. When
        # the opencode bridge plugin is active we emit a `_subagent`
        # envelope instead of running the Python evolutionary_loop inline.
        payload = build_evolve_subagent(
            lineage_id=lineage_id,
            seed_content=arguments.get("seed_content"),
            execute=arguments.get("execute", True),
            parallel=arguments.get("parallel", True),
            skip_qa=arguments.get("skip_qa", False),
            project_dir=arguments.get("project_dir"),
            conductor_directive=(
                conductor_directive.to_event_data() if conductor_directive is not None else None
            ),
            conductor_decision_id=arguments.get("conductor_decision_id"),
            predecessor_execution_id=arguments.get("predecessor_execution_id"),
        )
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            return await dispatch_plugin_terminal(
                self.event_store,
                session_id=lineage_id,
                payload=payload,
                response_shape={
                    "lineage_id": lineage_id,
                    "status": DELEGATED_TO_SUBAGENT,
                    "dispatch_mode": "plugin",
                },
            )

        # Fall-through: real in-process execution (subprocess / non-opencode runtimes).
        if self.evolutionary_loop is None:
            return Result.err(
                MCPToolError(
                    "EvolutionaryLoop not configured",
                    tool_name="ouroboros_evolve_step",
                )
            )

        # Parse seed if provided (Gen 1)
        initial_seed = None
        seed_content = arguments.get("seed_content")
        if seed_content:
            try:
                seed_dict = yaml.safe_load(seed_content)
                initial_seed = Seed.from_dict(seed_dict)
            except Exception as e:
                return Result.err(
                    MCPToolError(
                        f"Failed to parse seed_content: {e}",
                        tool_name="ouroboros_evolve_step",
                    )
                )

        execute = arguments.get("execute", True)
        parallel = arguments.get("parallel", True)
        benchmark_control = arguments.get("benchmark_control", False) is True
        project_dir = arguments.get("project_dir")
        normalized_project_dir = (
            project_dir if isinstance(project_dir, str) and project_dir else None
        )
        configured_project_dir = self.evolutionary_loop.get_project_dir()
        normalized_configured_project_dir = (
            configured_project_dir
            if isinstance(configured_project_dir, str) and configured_project_dir
            else None
        )
        effective_source_project_dir = normalized_project_dir or normalized_configured_project_dir
        workspace: TaskWorkspace | None = None
        if execute and (
            effective_source_project_dir is None or is_git_repo(effective_source_project_dir)
        ):
            try:
                workspace = maybe_restore_task_workspace(
                    lineage_id,
                    persisted=None,
                    fallback_source_cwd=effective_source_project_dir or os.getcwd(),
                    allow_untracked_evidence=True,
                )
            except WorktreeError as e:
                return Result.err(
                    MCPToolError(
                        f"Task workspace error: {e.message}",
                        tool_name="ouroboros_evolve_step",
                    )
                )

        if benchmark_control:
            effective_baseline = capture_project_baseline(
                workspace.effective_cwd if workspace else effective_source_project_dir
            )
            if (
                effective_baseline.git_commit is None
                or effective_baseline.workspace_path is None
                or not effective_baseline.clean
            ):
                if workspace is not None:
                    release_lock(workspace.lock_path)
                return Result.err(
                    MCPToolError(
                        "benchmark_control effective worktree must have a clean Git baseline",
                        tool_name="ouroboros_evolve_step",
                    )
                )

        project_dir_token = self.evolutionary_loop.set_project_dir(
            workspace.effective_cwd if workspace else effective_source_project_dir
        )
        resolved_verification_working_dir = Path.cwd()
        workspace_evidence_pending = False

        try:
            # Ensure event store is initialized before evolve_step accesses it
            # (evolve_step calls replay_lineage/append before executor/evaluator)
            await self.evolutionary_loop.event_store.initialize()
            evolve_kwargs: dict[str, Any] = {"execute": execute, "parallel": parallel}
            if benchmark_control:
                evolve_kwargs["benchmark_control"] = True
            if conductor_directive is not None:
                evolve_kwargs["conductor_directive"] = conductor_directive
            result = recovered_evolve_result
            if result is None:
                result = await self.evolutionary_loop.evolve_step(
                    lineage_id,
                    initial_seed,
                    **evolve_kwargs,
                    on_generation_claimed=on_generation_claimed,
                )
            if result.is_ok:
                step = result.value
                resolved_verification_working_dir = _resolve_checkpoint_working_dir(
                    workspace,
                    _resolve_evolve_verification_working_dir(
                        normalized_project_dir,
                        normalized_configured_project_dir,
                        getattr(step.generation_result, "seed", None),
                        initial_seed,
                    ),
                )
                workspace_evidence_pending = True
        except Exception as e:
            log.error("mcp.tool.evolve_step.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"evolve_step failed: {e}",
                    tool_name="ouroboros_evolve_step",
                )
            )
        finally:
            self.evolutionary_loop.reset_project_dir(project_dir_token)
            if workspace is not None and not workspace_evidence_pending:
                release_lock(workspace.lock_path)

        if result.is_err:
            return Result.err(
                MCPToolError(
                    str(result.error),
                    tool_name="ouroboros_evolve_step",
                )
            )

        step = result.value
        (
            checkpoint_commits,
            checkpoint_attempted_ac_ids,
            qa_attempted,
            artifact,
            reference,
        ) = await _materialize_locked_evolve_evidence(
            arguments=arguments,
            step=step,
            execute=execute,
            workspace=workspace,
            verification_working_dir=resolved_verification_working_dir,
        )
        gen = step.generation_result
        sig = step.convergence_signal

        # Format output
        text_lines = [
            f"## Generation {gen.generation_number}",
            "",
            f"**Action**: {step.action.value}",
            f"**Phase**: {gen.phase.value}",
            f"**Convergence similarity**: {sig.ontology_similarity:.2%}",
            f"**Reason**: {sig.reason}",
            *(
                [f"**Failed ACs**: {', '.join(str(i + 1) for i in sig.failed_acs)}"]
                if sig.failed_acs
                else []
            ),
            f"**Lineage**: {step.lineage.lineage_id} ({step.lineage.current_generation} generations)",
            f"**Next generation**: {step.next_generation}",
        ]
        if gen.frozen_ac_indices:
            active_labels = ", ".join(str(index + 1) for index in gen.active_ac_indices) or "none"
            frozen_labels = ", ".join(str(index + 1) for index in gen.frozen_ac_indices)
            text_lines.extend(
                [
                    f"**Active evolve nodes**: {active_labels}",
                    f"**Frozen nodes**: {frozen_labels}",
                    (
                        "**Working set**: "
                        f"{len(gen.active_ac_indices)} active / "
                        f"{len(gen.frozen_ac_indices)} frozen"
                    ),
                ]
            )
        if gen.frugality_evidence is not None:
            proof = gen.frugality_evidence.proof
            text_lines.extend(
                [
                    f"**Frugality proof**: {proof.status.value}",
                    f"**Frugality evidence**: {proof.reason}",
                ]
            )
        if workspace is not None:
            text_lines.extend(
                [
                    f"**Worktree**: {workspace.worktree_path}",
                    f"**Branch**: {workspace.branch}",
                ]
            )

        if gen.execution_output:
            text_lines.append("")
            text_lines.append("### Execution output")
            output_preview = truncate_head_tail(
                gen.execution_output,
                head=150 if gen.frozen_ac_indices else 500,
                tail=650 if gen.frozen_ac_indices else 2000,
            )
            text_lines.append(output_preview)

        if gen.evaluation_summary:
            text_lines.append("")
            text_lines.append("### Evaluation")
            es = gen.evaluation_summary
            text_lines.append(f"- **Approved**: {es.final_approved}")
            text_lines.append(f"- **Score**: {es.score}")
            text_lines.append(f"- **Drift**: {es.drift_score}")
            if es.failure_reason:
                text_lines.append(f"- **Failure**: {es.failure_reason}")
            if es.ac_results:
                text_lines.append("")
                text_lines.append("#### Per-AC Results")
                active = set(gen.active_ac_indices)
                visible_results = [
                    ac
                    for ac in es.ac_results
                    if not gen.frozen_ac_indices or ac.ac_index in active or ac.unresolved
                ]
                for ac in visible_results:
                    status = ac.authority_state.upper()
                    text_lines.append(f"- AC {ac.ac_index + 1}: [{status}] {ac.ac_content[:80]}")
                frozen_passes = sum(
                    1
                    for ac in es.ac_results
                    if ac.ac_index in gen.frozen_ac_indices and ac.authoritative_pass
                )
                if frozen_passes:
                    text_lines.append(
                        f"- {frozen_passes} frozen node(s): [PASS] boundary reverified"
                    )

        if checkpoint_commits:
            text_lines.append("")
            text_lines.append("### Checkpoint commits")
            for entry in checkpoint_commits:
                text_lines.append(f"- {entry.get('ac_id')}: {entry.get('commit')}")

        if gen.wonder_output:
            text_lines.append("")
            text_lines.append("### Wonder questions")
            for q in gen.wonder_output.questions:
                text_lines.append(f"- {q}")

        if gen.validation_output:
            text_lines.append("")
            text_lines.append("### Validation")
            text_lines.append(gen.validation_output)

        if gen.ontology_delta:
            text_lines.append("")
            text_lines.append(
                f"### Ontology delta (similarity: {gen.ontology_delta.similarity:.2%})"
            )
            for af in gen.ontology_delta.added_fields:
                text_lines.append(f"- **Added**: {af.name} ({af.field_type})")
            for rf in gen.ontology_delta.removed_fields:
                text_lines.append(f"- **Removed**: {rf}")
            for mf in gen.ontology_delta.modified_fields:
                text_lines.append(f"- **Modified**: {mf.field_name}: {mf.old_type} → {mf.new_type}")

        # Post-execution QA
        qa_meta = None
        if qa_attempted:
            from ouroboros.mcp.tools.qa import QAHandler

            assert artifact is not None
            assert reference is not None
            qa_handler = QAHandler()
            ac_lines = [f"- {ac}" for ac in ac_texts(gen.seed.acceptance_criteria)]
            quality_bar = "The execution must satisfy all acceptance criteria:\n" + "\n".join(
                ac_lines
            )
            canonical_seed_content = yaml.safe_dump(
                gen.seed.to_dict(), sort_keys=False, allow_unicode=True
            )

            qa_result = await qa_handler.handle(
                {
                    "artifact": artifact,
                    "artifact_type": "test_output",
                    "quality_bar": quality_bar,
                    "reference": reference,
                    "seed_content": canonical_seed_content,
                    "pass_threshold": 0.80,
                }
            )
            if qa_result.is_ok:
                text_lines.append("")
                text_lines.append("### QA Verdict")
                text_lines.append(qa_result.value.content[0].text)
                qa_meta = qa_result.value.meta

        meta = {
            "lineage_id": step.lineage.lineage_id,
            "generation": gen.generation_number,
            "action": step.action.value,
            "similarity": sig.ontology_similarity,
            "next_generation": step.next_generation,
            "executed": execute,
            "benchmark_control": benchmark_control,
            "qa_attempted": qa_attempted,
            "has_execution_output": gen.execution_output is not None,
            "active_ac_indices": list(gen.active_ac_indices),
            "frozen_ac_indices": list(gen.frozen_ac_indices),
            "active_node_count": len(gen.active_ac_indices),
            "frozen_node_count": len(gen.frozen_ac_indices),
            "checkpoint_commits": checkpoint_commits,
            "checkpoint_attempted_ac_ids": checkpoint_attempted_ac_ids,
        }
        if workspace is not None:
            meta["worktree_path"] = workspace.worktree_path
            meta["worktree_branch"] = workspace.branch
        if qa_meta:
            meta["qa"] = qa_meta
        if gen.frugality_evidence is not None:
            meta["frugality"] = {
                "observation": gen.frugality_evidence.observation.model_dump(mode="json"),
                "proof": gen.frugality_evidence.proof.model_dump(mode="json"),
            }

        from ouroboros.evolution.loop import StepAction

        meta["converged"] = step.action is StepAction.CONVERGED

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(text_lines)),),
                is_error=step.action.value in ("failed", "interrupted"),
                meta=meta,
            )
        )


async def _materialize_locked_evolve_evidence(
    *,
    arguments: dict[str, Any],
    step: Any,
    execute: bool,
    workspace: TaskWorkspace | None,
    verification_working_dir: Path,
) -> tuple[list[dict[str, Any]], list[str], bool, str | None, str | None]:
    """Materialize every filesystem-dependent receipt before releasing the workspace.

    The generation Seed and its verification artifact must describe one filesystem
    epoch.  A managed task worktree can be reused by the next generation as soon as
    its durable lock is released, so checkpointing and mechanical verification run
    while that lock is still held.  QA runs later from the returned strings and does
    not need to retain the lock across provider latency.
    """
    try:
        gen = step.generation_result
        checkpoint_commits, checkpoint_attempted_ac_ids = _checkpoint_passed_generation_acs(
            arguments,
            gen.evaluation_summary,
            verification_working_dir,
        )
        skip_qa = arguments.get("skip_qa", False)
        qa_attempted = step.action.value in ("continue", "converged") and execute and not skip_qa
        if not qa_attempted:
            return (
                checkpoint_commits,
                checkpoint_attempted_ac_ids,
                False,
                None,
                None,
            )

        execution_artifact = gen.execution_output or (
            f"Generation {gen.generation_number}: action={step.action.value}; "
            f"reason={step.convergence_signal.reason}"
        )
        try:
            verification = await build_verification_artifacts(
                f"{step.lineage.lineage_id}-gen-{gen.generation_number}",
                execution_artifact,
                verification_working_dir,
            )
            artifact = verification.artifact
            reference = verification.reference
        except Exception as exc:
            artifact = execution_artifact
            reference = f"Verification artifact generation failed: {exc}"
        return (
            checkpoint_commits,
            checkpoint_attempted_ac_ids,
            True,
            artifact,
            reference,
        )
    finally:
        if workspace is not None:
            release_lock(workspace.lock_path)


def _checkpoint_passed_generation_acs(
    arguments: dict[str, Any],
    evaluation_summary: Any,
    repo_cwd: Path,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Create checkpoint commits for passed ACs when requested."""
    raw_existing = arguments.get("checkpoint_commits") or []
    raw_attempted = arguments.get("checkpoint_attempted_ac_ids") or []
    existing = [item for item in raw_existing if isinstance(item, dict)]
    attempted = [item for item in raw_attempted if isinstance(item, str)]
    if evaluation_summary is None or not getattr(evaluation_summary, "ac_results", None):
        return existing, attempted
    raw_policy = arguments.get("commit_policy")
    if not isinstance(raw_policy, str) or not raw_policy.strip():
        return existing, attempted
    auto_session_id = arguments.get("auto_session_id")
    if not isinstance(auto_session_id, str) or not auto_session_id.strip():
        return existing, attempted

    try:
        commit_policy = AutoCommitPolicy(raw_policy)
    except ValueError:
        return existing, attempted

    state = AutoPipelineState(goal="Ralph checkpoint", cwd=str(repo_cwd))
    state.auto_session_id = auto_session_id
    execution_id = arguments.get("execution_id")
    if isinstance(execution_id, str) and execution_id.strip():
        state.execution_id = execution_id
    state.commit_policy = commit_policy
    state.checkpoint_commits = list(existing)
    state.checkpoint_attempted_ac_ids = list(attempted)
    for ac in evaluation_summary.ac_results:
        if not bool(getattr(ac, "authoritative_pass", False)):
            continue
        ac_id = f"AC-{int(getattr(ac, 'ac_index', 0)) + 1}"
        ac_text = str(getattr(ac, "ac_content", ac_id))
        try:
            checkpoint_passed_ac(state, repo_cwd=repo_cwd, ac_id=ac_id, ac_text=ac_text)
        except RuntimeError as exc:
            log.warning("mcp.tool.evolve_step.checkpoint_commit_failed", error=str(exc))
    return state.checkpoint_commits, state.checkpoint_attempted_ac_ids


@dataclass
class EvolveRewindHandler:
    """Handler for the ouroboros_evolve_rewind tool.

    Rewinds an evolutionary lineage to a specific generation.
    Delegates to EvolutionaryLoop.rewind_to().
    """

    evolutionary_loop: Any | None = field(default=None, repr=False)

    TIMEOUT_SECONDS: int = 0  # No timeout

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_evolve_rewind",
            description=(
                "Rewind an evolutionary lineage to a specific generation. "
                "Truncates all generations after the target and emits a "
                "lineage.rewound event. The lineage can then continue evolving "
                "from the rewind point."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to rewind",
                    required=True,
                ),
                MCPToolParameter(
                    name="to_generation",
                    type=ToolInputType.INTEGER,
                    description="Generation number to rewind to (inclusive)",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a rewind request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        to_generation = arguments.get("to_generation")
        if to_generation is None:
            return Result.err(
                MCPToolError(
                    "to_generation is required",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if self.evolutionary_loop is None:
            return Result.err(
                MCPToolError(
                    "EvolutionaryLoop not configured",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        try:
            await self.evolutionary_loop.event_store.initialize()
            events = await self.evolutionary_loop.event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to replay lineage: {e}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        from ouroboros.evolution.projector import LineageProjector

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage: {lineage_id}",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        # Validate generation is in range
        if to_generation < 1 or to_generation > lineage.current_generation:
            return Result.err(
                MCPToolError(
                    f"Generation {to_generation} out of range [1, {lineage.current_generation}]",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        if to_generation == lineage.current_generation:
            return Result.err(
                MCPToolError(
                    f"Already at generation {to_generation}, nothing to rewind",
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        from_gen = lineage.current_generation
        result = await self.evolutionary_loop.rewind_to(lineage, to_generation)

        if result.is_err:
            return Result.err(
                MCPToolError(
                    str(result.error),
                    tool_name="ouroboros_evolve_rewind",
                )
            )

        committed = result.value
        rewound_lineage = committed.lineage

        # Get seed_json from the target generation if available
        target_gen = None
        for g in rewound_lineage.generations:
            if g.generation_number == to_generation:
                target_gen = g
                break

        seed_info = ""
        if target_gen and target_gen.seed_json:
            seed_info = f"\n\n### Target generation seed\n```yaml\n{target_gen.seed_json}\n```"

        text = (
            f"## Rewind Complete\n\n"
            f"**Lineage**: {lineage_id}\n"
            f"**From generation**: {committed.from_generation}\n"
            f"**To generation**: {committed.to_generation}\n"
            f"**Status**: {rewound_lineage.status.value}\n"
            f"**Git tag**: `ooo/{lineage_id}/gen_{to_generation}`\n\n"
            f"Generations {to_generation + 1}–{from_gen} have been truncated.\n"
            f"Run `ralph.sh --lineage-id {lineage_id}` to resume evolution."
            f"{seed_info}"
        )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "lineage_id": lineage_id,
                    "from_generation": committed.from_generation,
                    "to_generation": committed.to_generation,
                    "rewind_event_id": committed.rewind_event_id,
                    "rewind_occurred_at": committed.rewind_occurred_at.isoformat().replace(
                        "+00:00", "Z"
                    ),
                },
            )
        )


@dataclass
class LineageStatusHandler:
    """Handler for the ouroboros_lineage_status tool.

    Queries the current state of an evolutionary lineage
    without running a generation.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize event store."""
        self._event_store = self.event_store or EventStore()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_lineage_status",
            description=(
                "Query the current state of an evolutionary lineage. "
                "Returns generation count, status, ontology evolution, "
                "and convergence progress."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to query",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a lineage status request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_lineage_status",
                )
            )

        await self._ensure_initialized()

        try:
            events = await self._event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        from ouroboros.evolution.projector import LineageProjector

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage from events: {lineage_id}",
                    tool_name="ouroboros_lineage_status",
                )
            )

        text_lines = [
            f"## Lineage: {lineage.lineage_id}",
            "",
            f"**Status**: {lineage.status.value}",
            f"**Goal**: {lineage.goal}",
            f"**Generations**: {lineage.current_generation}",
            f"**Created**: {lineage.created_at.isoformat()}",
        ]
        latest_frugality = next(
            (event for event in reversed(events) if event.type == PROOF_EVENT),
            None,
        )
        frugality_proof = None
        if latest_frugality is not None:
            try:
                frugality_proof = EvolutionFrugalityProof.model_validate(
                    latest_frugality.data.get("proof")
                )
            except (TypeError, ValueError):
                frugality_proof = None
        if frugality_proof is not None:
            text_lines.extend(
                [
                    f"**Frugality proof**: {frugality_proof.status.value}",
                    f"**Frugality evidence**: {frugality_proof.reason}",
                ]
            )

        # Ontology summary
        if lineage.current_ontology:
            text_lines.append("")
            text_lines.append(f"### Current Ontology: {lineage.current_ontology.name}")
            for f in lineage.current_ontology.fields:
                required = " (required)" if f.required else ""
                text_lines.append(f"- **{f.name}**: {f.field_type}{required}")

        # Generation history
        if lineage.generations:
            text_lines.append("")
            text_lines.append("### Generation History")
            for gen in lineage.generations:
                status = (
                    "passed"
                    if gen.evaluation_summary and gen.evaluation_summary.final_approved
                    else "pending"
                )
                error_part = ""
                if gen.failure_error:
                    error_part = f" | {gen.failure_error[:60]}"
                text_lines.append(
                    f"- Gen {gen.generation_number}: {gen.phase.value} | {status}{error_part}"
                )

        # Rewind history
        if lineage.rewind_history:
            text_lines.append("")
            text_lines.append("### Rewind History")
            for rr in lineage.rewind_history:
                ts = rr.rewound_at
                time_str = (
                    ts.strftime("%Y-%m-%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
                )
                text_lines.append(
                    f"- \u21a9 Rewound Gen {rr.from_generation} \u2192 "
                    f"Gen {rr.to_generation} ({time_str})"
                )
                for dg in rr.discarded_generations:
                    score_part = ""
                    if dg.evaluation_summary and dg.evaluation_summary.score is not None:
                        score_part = f" | score={dg.evaluation_summary.score:.2f}"
                    error_part = ""
                    if dg.failure_error:
                        error_part = f" | {dg.failure_error[:60]}"
                    text_lines.append(
                        f"  - Gen {dg.generation_number}: {dg.phase.value}{score_part}{error_part}"
                    )

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="\n".join(text_lines)),),
                is_error=False,
                meta={
                    "lineage_id": lineage.lineage_id,
                    "status": lineage.status.value,
                    "generations": lineage.current_generation,
                    "goal": lineage.goal,
                    "frugality": (
                        frugality_proof.model_dump(mode="json")
                        if frugality_proof is not None
                        else None
                    ),
                },
            )
        )


@dataclass
class StartEvolveStepHandler:
    """Start one evolve_step generation asynchronously."""

    evolve_handler: EvolveStepHandler | None = field(default=None, repr=False)
    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)
    agent_runtime_backend: str | None = field(default=None, repr=False)
    opencode_mode: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)
        self._evolve_handler = self.evolve_handler or EvolveStepHandler()

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_start_evolve_step",
            description=(
                "Start one evolve_step generation in the background and return a job ID "
                "immediately for later status checks. "
                "In plugin mode, evolution is delegated to an OpenCode Task pane and "
                "job_id is None — results appear in the Task pane instead of being "
                "pollable via job_status/job_result."
            ),
            parameters=EvolveStepHandler().definition.parameters,
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_start_evolve_step",
                )
            )
        benchmark_control = arguments.get("benchmark_control", False)
        if type(benchmark_control) is not bool:
            return Result.err(
                MCPToolError(
                    "benchmark_control must be a boolean",
                    tool_name="ouroboros_start_evolve_step",
                )
            )
        if benchmark_control and should_dispatch_via_plugin(
            self.agent_runtime_backend,
            self.opencode_mode,
        ):
            return Result.err(
                MCPToolError(
                    "benchmark_control requires the in-process evolve runtime",
                    tool_name="ouroboros_start_evolve_step",
                )
            )

        directive_result = await _resolve_conductor_directive(
            arguments=arguments,
            event_store=self._event_store,
            target_type="lineage",
            target_id=lineage_id,
            tool_name="ouroboros_start_evolve_step",
        )
        if directive_result.is_err:
            return Result.err(directive_result.error)
        conductor_directive = directive_result.value

        # --- Subagent dispatch: gate on runtime + opencode_mode ---
        # Own-gate parity with StartExecuteSeedHandler. Plugin mode is
        # terminal: return envelope, skip background-job enqueue entirely.
        payload = build_evolve_subagent(
            lineage_id=lineage_id,
            seed_content=arguments.get("seed_content"),
            execute=arguments.get("execute", True),
            parallel=arguments.get("parallel", True),
            skip_qa=arguments.get("skip_qa", False),
            project_dir=arguments.get("project_dir"),
            conductor_directive=(
                conductor_directive.to_event_data() if conductor_directive is not None else None
            ),
            conductor_decision_id=arguments.get("conductor_decision_id"),
            predecessor_execution_id=arguments.get("predecessor_execution_id"),
        )
        if should_dispatch_via_plugin(self.agent_runtime_backend, self.opencode_mode):
            # Plugin mode: work runs in the OpenCode child session (Task
            # pane), NOT in a JobManager background job.  See
            # StartExecuteSeedHandler for rationale — no fake job_id. The
            # shared helper initializes the store first so the audit event
            # persists.
            return await dispatch_plugin_terminal(
                self._event_store,
                session_id=lineage_id,
                payload=payload,
                response_shape={
                    "job_id": None,
                    "lineage_id": lineage_id,
                    "status": DELEGATED_TO_PLUGIN,
                    "dispatch_mode": "plugin",
                },
            )

        # Fall-through: real background job path. The shared pipeline owns the
        # ``should_cancel()`` pre-work guard, so the runner only does the work.
        await self._event_store.initialize()
        if isinstance(self._event_store, EventStore):
            from ouroboros.persistence import lineage_claims

            existing_claim = await lineage_claims.observe(
                self._event_store,
                scope="evolve-handler",
                lineage_id=str(lineage_id),
            )
            if (
                existing_claim is not None
                and not existing_claim.completed
                and existing_claim.lease_expires_at_ms > int(time.time() * 1000)
            ):
                return Result.err(evolve_lineage_busy_error(str(lineage_id)))
        supports_claimed_handoff = (
            isinstance(self._event_store, EventStore)
            and getattr(self._evolve_handler, "evolutionary_loop", None) is not None
        )
        prepared_claim = PreparedEvolveClaim(
            self._evolve_handler,
            arguments,
            str(lineage_id),
        )

        execution_id = None
        if not supports_claimed_handoff:
            replay_lineage = getattr(self._event_store, "replay_lineage", None)
            generation_number = (
                await loop_support.planned_evolve_generation(
                    self._event_store,
                    str(lineage_id),
                    execute=bool(arguments.get("execute", True)),
                )
                if callable(replay_lineage)
                else 1
            )
            execution_id = loop_support.generation_execution_id(
                str(lineage_id),
                generation_number,
            )

        async def _runner(_handle) -> MCPToolResult:
            result = await self._evolve_handler.handle(arguments)
            if result.is_err:
                raise RuntimeError(str(result.error))
            return result.value

        background_acceptance = BackgroundJobAcceptanceState()

        async def _abort_unaccepted_claim(error: BaseException) -> None:
            if not background_acceptance.may_have_accepted(error):
                await prepared_claim.abort_on_failure(error)

        try:
            snapshot = await start_background_tool_job(
                job_manager=self._job_manager,
                event_store=self._event_store,
                job_type="evolve_step",
                intent="evolve_step",
                process_scope=f"evolve_step:{lineage_id}",
                initial_message=f"Queued evolve_step for {lineage_id}",
                links=JobLinks(lineage_id=lineage_id, execution_id=execution_id),
                work_fn=_runner,
                cancelled_text="evolve_step cancelled before restart work began.",
                detached_tool_name="ouroboros_start_evolve_step",
                detached_arguments=arguments,
                runtime_backend=self.agent_runtime_backend,
                opencode_mode=self.opencode_mode,
                prepare_inline=prepared_claim.prepare if supports_claimed_handoff else None,
                on_cancel_before_work=prepared_claim.abort if supports_claimed_handoff else None,
                on_enqueue_failure=(_abort_unaccepted_claim if supports_claimed_handoff else None),
                acceptance_state=background_acceptance,
            )
        except MCPToolError as exc:
            return Result.err(exc)

        observer = build_job_observer_contract(
            job_id=snapshot.job_id,
            cursor=snapshot.cursor,
            execution_id=snapshot.links.execution_id,
        )
        text = (
            f"Started background evolve_step.\n\n"
            f"Job ID: {snapshot.job_id}\n"
            f"Lineage ID: {lineage_id}\n"
            f"Execution ID: {snapshot.links.execution_id}\n\n"
            "Use ouroboros_job_status, ouroboros_job_wait, or ouroboros_job_result to monitor it."
        )
        text = append_job_observer_inline_handoff(text, observer)
        meta = {
            "job_id": snapshot.job_id,
            "lineage_id": lineage_id,
            "execution_id": snapshot.links.execution_id,
            "status": snapshot.status.value,
            "cursor": snapshot.cursor,
            "job_observer": observer,
        }
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta=meta,
                structured_content=dict(meta),
            )
        )
