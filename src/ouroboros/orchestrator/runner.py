"""Orchestrator runner for executing seeds via Claude Agent SDK.

This module provides the main orchestration logic:
- OrchestratorRunner: Converts Seed → prompt, executes via adapter, tracks progress
- OrchestratorResult: Frozen dataclass with execution results

The runner integrates:
- ClaudeAgentAdapter for task execution
- SessionRepository for event-based session tracking
- Rich console for progress display
- Event emission for observability

Usage:
    runner = OrchestratorRunner(adapter, event_store)
    result = await runner.execute_seed(seed, execution_id)
    if result.is_ok:
        print(f"Success: {result.value.summary}")
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import aclosing
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from uuid import uuid4

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ouroboros.backends import backend_supports_tool_envelope
from ouroboros.config import get_llm_model_for_role
from ouroboros.core.conductor import ConductorDirective
from ouroboros.core.errors import ConfigError, OuroborosError, PersistenceError
from ouroboros.core.execution_preferences import (
    execution_preferences_from_contract,
    resolve_execution_preferences,
)
from ouroboros.core.seed import AcceptanceCriterionSpec, ac_text, ac_texts
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import (
    render_auto_recursion_guard,
    render_seed_contract_for_execution,
)
from ouroboros.core.types import Result
from ouroboros.core.worktree import TaskWorkspace, heartbeat_lock, release_lock
from ouroboros.observability.drift import DriftMeasurement
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    AgentMessage,
    AgentRuntime,
    ParamSupport,
    RuntimeHandle,
)
from ouroboros.orchestrator.backend_limits import (
    plan_fan_out_concurrency,
    resolve_backend_limits,
)
from ouroboros.orchestrator.capabilities import (
    CapabilityGraph,
    build_capability_graph,
    serialize_capability_graph,
)
from ouroboros.orchestrator.control_plane import (
    build_control_plane_state,
    serialize_control_plane_state,
)
from ouroboros.orchestrator.events import (
    create_drift_measured_event,
    create_execution_terminal_event,
    create_guidance_injected_event,
    create_mcp_tools_loaded_event,
    create_policy_capabilities_evaluated_event,
    create_progress_event,
    create_tool_called_event,
    create_workflow_progress_event,
)
from ouroboros.orchestrator.execution_authority import (
    ProcessLocalCancellationDisposition,
    _await_process_local_cleanup,
    _claim_process_local_authority_generation,
    _discard_process_local_authority_generation,
    _has_live_process_local_authority_registration,
    _has_live_process_local_authority_session,
    _live_process_local_authority_generation,
    _mint_process_local_authority_generation,
    _process_local_authority_contract,
    _ProcessLocalAuthorityGeneration,
    _register_process_local_authority_generation,
    _register_process_local_authority_terminal_finalizer,
    _release_process_local_authority_generation,
    _retire_process_local_authority_generation,
    constructor_model_contract,
    request_process_local_cancellation,
    runtime_execution_proves_effective_model,
    valid_constructor_model_contract,
    valid_process_local_authority_contract,
    valid_runtime_execution_identity_contract,
)
from ouroboros.orchestrator.execution_guidance import (
    ExecutionGuidanceBundle,
    resolve_execution_guidance,
)
from ouroboros.orchestrator.execution_runtime_scope import (
    ExecutionNodeIdentity,
    build_ac_runtime_scope,
)
from ouroboros.orchestrator.execution_strategy import ExecutionStrategy, get_strategy
from ouroboros.orchestrator.mcp_tools import (
    MCPToolProvider,
    SessionToolCatalog,
    assemble_session_tool_catalog,
    enumerate_runtime_builtin_tool_definitions,
    serialize_tool_catalog,
)
from ouroboros.orchestrator.parallel_executor import DEFAULT_MAX_DECOMPOSITION_DEPTH
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyExecutionPhase,
    PolicySessionRole,
    evaluate_capability_policy,
)
from ouroboros.orchestrator.profile_loader import ExecutionProfile, ProfileError, load_profile
from ouroboros.orchestrator.profile_strategy import ProfileBackedStrategy
from ouroboros.orchestrator.runtime_message_projection import (
    message_tool_input,
    message_tool_name,
    normalized_message_type,
    project_runtime_message,
)
from ouroboros.orchestrator.runtime_param_negotiation import (
    announce_execution_param_degradations,
    runtime_capabilities_for,
)
from ouroboros.orchestrator.session import (
    SESSION_RUNTIME_IDENTITY_PROGRESS_KEY,
    SESSION_START_IDENTITY_PROGRESS_KEY,
    SessionRepository,
    SessionStatus,
    SessionTracker,
    runtime_resume_identity_from_payload,
)
from ouroboros.orchestrator.workflow_state import coerce_ac_marker_update
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.providers import create_llm_adapter, resolve_llm_backend
from ouroboros.resilience.lateral import ThinkingPersona
from ouroboros.resilience.recovery import (
    RecoveryActionKind,
    RecoveryPlanner,
    RecoverySnapshot,
    create_recovery_applied_event,
    get_run_recovery_protocol_prompt,
)

if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.events.base import BaseEvent
    from ouroboros.mcp.client.manager import MCPClientManager
    from ouroboros.orchestrator.dependency_analyzer import DependencyAnalyzer
    from ouroboros.orchestrator.heartbeat import CancellationRequest
    from ouroboros.orchestrator.model_routing import ModelRouter
    from ouroboros.orchestrator.synapse import SessionSignalHub
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)


# =============================================================================
# Result Types
# =============================================================================


class ToolCatalogPolicyResult(NamedTuple):
    """Bundle returned by ``_evaluate_tool_catalog_policy``.

    Using a named tuple instead of a positional 4-tuple lets callers read
    fields by name and removes the refactor fragility that would come from
    re-ordering a positional unpack.
    """

    allowed_tools: list[str]
    capability_graph: CapabilityGraph
    policy_decisions: tuple[PolicyDecision, ...]
    policy_context: PolicyContext


@dataclass(frozen=True, slots=True)
class OrchestratorResult:
    """Result of orchestrator execution.

    Attributes:
        success: Whether execution completed successfully.
        session_id: Session identifier for resumption.
        execution_id: Workflow execution ID.
        summary: Execution summary dict.
        messages_processed: Total messages from agent.
        final_message: Final result message from agent.
        duration_seconds: Execution duration.
    """

    success: bool
    session_id: str
    execution_id: str
    summary: dict[str, Any] = field(default_factory=dict)
    messages_processed: int = 0
    final_message: str = ""
    duration_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class RecoverableFailurePause:
    """Structured pause decision for recoverable final runtime failures."""

    pause_kind: str
    reason: str
    resume_hint: str
    pause_seconds: int | None = None
    resume_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class _PendingLifecycleIntent:
    """Process-local lifecycle transition retained for exact-owner replay."""

    execution_id: str
    status: SessionStatus
    summary: dict[str, Any] | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    error_type: str | None = None
    messages_processed: int = 0
    cancelled_by: str = "runner"
    pause: RecoverableFailurePause | None = None


# =============================================================================
# Errors
# =============================================================================


class OrchestratorError(OuroborosError):
    """Error during orchestrator execution."""

    pass


class ExecutionCancelledError(OuroborosError):
    """Raised when an execution is cancelled via the cancellation set."""

    def __init__(self, session_id: str, reason: str = "Cancelled by user") -> None:
        self.session_id = session_id
        self.reason = reason
        super().__init__(f"Execution cancelled for session {session_id}: {reason}")


# =============================================================================
# In-memory Cancellation Registry
# =============================================================================

# Module-level requests keyed by session ID.
# The MCP cancel tool adds metadata here; the runner's execution loop checks it.
# Guarded by _cancellation_lock to prevent races between MCP cancel calls
# and the runner's message loop reading the mapping concurrently.
_cancellation_registry: dict[str, CancellationRequest] = {}
_cancellation_lock: asyncio.Lock = asyncio.Lock()


async def request_cancellation(
    session_id: str,
    *,
    reason: str = "Cancellation detected during execution",
    cancelled_by: str = "runner",
) -> None:
    """Mark a session for cancellation.

    Called by the MCP cancel tool to signal that the runner should
    stop processing the given session at its next checkpoint.

    Args:
        session_id: Session to cancel.
    """
    from ouroboros.orchestrator.heartbeat import normalize_cancellation_request

    async with _cancellation_lock:
        _cancellation_registry[session_id] = normalize_cancellation_request(
            reason=reason,
            cancelled_by=cancelled_by,
        )


async def get_cancellation_request(session_id: str) -> CancellationRequest | None:
    """Return local or cross-process metadata for a pending cancellation."""
    async with _cancellation_lock:
        request = _cancellation_registry.get(session_id)
    if request is not None:
        return request
    from ouroboros.orchestrator.heartbeat import read_cancellation_request

    return read_cancellation_request(session_id)


async def is_cancellation_requested(session_id: str) -> bool:
    """Check whether cancellation has been requested for a session.

    Args:
        session_id: Session to check.

    Returns:
        True if cancellation was requested.
    """
    return await get_cancellation_request(session_id) is not None


async def clear_cancellation(session_id: str) -> None:
    """Remove a session from the cancellation registry.

    Called after the runner has acknowledged the cancellation and
    emitted the appropriate event, so the ID doesn't linger.

    Args:
        session_id: Session to clear.
    """
    async with _cancellation_lock:
        _cancellation_registry.pop(session_id, None)
    from ouroboros.orchestrator.heartbeat import clear_cancellation_request

    clear_cancellation_request(session_id)


async def get_pending_cancellations() -> frozenset[str]:
    """Return a snapshot of all pending cancellation session IDs.

    Returns:
        Frozen set of session IDs awaiting cancellation.
    """
    async with _cancellation_lock:
        return frozenset(_cancellation_registry)


# =============================================================================
# Prompt Building
# =============================================================================


def _execution_profile_for_seed(seed: Seed) -> ExecutionProfile | None:
    """Return the execution profile matching a seed task_type, if available."""
    try:
        return load_profile(seed.task_type)
    except ProfileError:
        log.warning(
            "orchestrator.runner.execution_profile_unavailable",
            task_type=seed.task_type,
        )
        return None


def _strategy_for_seed(seed: Seed, *, fat_harness_mode: bool = False) -> ExecutionStrategy:
    """Resolve the prompt/tool strategy for the active execution mode."""
    if fat_harness_mode:
        profile = _execution_profile_for_seed(seed)
        if profile is not None:
            return ProfileBackedStrategy(profile)
    return get_strategy(seed.task_type)


def _seed_has_investment_metadata(seed: Seed) -> bool:
    """Return whether any AC requires per-criterion investment routing."""
    return any(
        isinstance(criterion, AcceptanceCriterionSpec) and criterion.investment is not None
        for criterion in seed.acceptance_criteria
    )


def build_system_prompt(
    seed: Seed,
    strategy: ExecutionStrategy | None = None,
    *,
    repo_root: str | Path | None = None,
    guidance_fragment: str = "",
) -> str:
    """Build system prompt from seed specification.

    Args:
        seed: Seed to extract system prompt from.
        strategy: Execution strategy for prompt customization.
            If None, uses strategy from seed.task_type.
        repo_root: Working directory for the run. When it (or the seed's first
            resolvable ``context_references`` path) is an existing repo, a
            deterministic context pack (stack, verify commands, layout) is
            appended so workers are not primed blind. Best-effort — a scan
            failure or a non-project directory simply omits the pack.
        guidance_fragment: Explicit project execution guidance resolved and
            provenance-checked by the runner. Empty preserves the historical
            prompt byte-for-byte.

    Returns:
        System prompt string.
    """
    from ouroboros.orchestrator.workflow_state import get_ac_tracking_prompt

    if strategy is None:
        strategy = get_strategy(seed.task_type)

    ac_tracking = get_ac_tracking_prompt()
    strategy_fragment = strategy.get_system_prompt_fragment()
    recovery_protocol = get_run_recovery_protocol_prompt()
    seed_contract = render_seed_contract_for_execution(SeedContract.from_seed(seed))
    conductor_directive = _render_conductor_directive(seed)

    prompt = f"""{strategy_fragment}

{seed_contract}

{guidance_fragment}

{ac_tracking}

{recovery_protocol}"""

    if not guidance_fragment:
        prompt = f"""{strategy_fragment}

{seed_contract}

{ac_tracking}

{recovery_protocol}"""

    if conductor_directive:
        prompt = f"{prompt}\n\n{conductor_directive}"

    context_pack_fragment = _context_pack_fragment(seed, repo_root)
    if context_pack_fragment:
        prompt = f"{prompt}\n\n{context_pack_fragment}"
    return prompt


def _render_conductor_directive(seed: Seed) -> str:
    """Render audited successor-only context without rewriting the Seed contract."""
    raw_directive = (seed.model_extra or {}).get("conductor_directive")
    if raw_directive is None:
        return ""
    if not isinstance(raw_directive, dict):
        raise ValueError("Seed conductor_directive must be a structured object")
    directive = ConductorDirective.from_mapping(raw_directive)
    reasons = (
        "\n".join(f"- {reason}" for reason in directive.rejected_reasons)
        if directive.rejected_reasons
        else "None recorded."
    )
    return f"""## Active Conductor Successor Directive
This is bounded additive context for a successor execution. The Seed above remains
the source of truth. Do not weaken or silently replace its approved direction.

Instruction: {directive.instruction}
Rejected evidence reasons:
{reasons}

Preservation contract:
- goal: {str(directive.preserve_goal).lower()}
- acceptance criteria: {str(directive.preserve_acceptance_criteria).lower()}
- constraints: {str(directive.preserve_constraints).lower()}
- non-goals: {str(directive.preserve_non_goals).lower()}

Re-check the affected implementation and verification evidence, then report the
specific correction made for this directive."""


def _resolve_context_pack_root(
    seed: Seed,
    repo_root: str | Path | None,
) -> Path | None:
    """Resolve the contained project directory the context pack may describe.

    Security contract: the pack scans this directory and, for git repos,
    cache-writes ``.ouroboros/context_pack.json`` under it, so it must never
    resolve outside the run's own contained project. ``repo_root`` is that
    project — it was already resolved and containment-checked upstream by
    ``_resolve_cli_project_dir`` (via ``resolve_seed_project_path``) — so it is
    the single trust anchor here.

    Seed-encoded ``metadata.project_dir`` / ``context_references`` are
    untrusted (LLM-generated, or imported via ``ooo publish``). They are only
    honored when they resolve *inside* ``repo_root`` under the very same
    ``resolve_seed_project_path`` containment contract the CLI uses — never as
    a way to redirect the scan (and cache write) at an arbitrary local repo.
    Any escaping candidate is rejected and we fall back to ``repo_root``
    itself. Without a trusted ``repo_root`` there is no stable base to contain
    seed paths against, so the resolver returns ``None`` (no pack) rather than
    scanning a raw seed path.
    """
    if not repo_root:
        return None
    base = Path(repo_root)
    if not base.is_dir():
        return None
    base = base.resolve()

    from ouroboros.core.project_paths import resolve_seed_project_path

    resolution = resolve_seed_project_path(seed, stable_base=base)
    candidate = resolution.path
    if candidate is not None:
        # Contained candidate (existing metadata dir, or an existing reference
        # file/dir inside ``base``). Files collapse to their parent directory.
        if candidate.is_file():
            return candidate.parent
        if candidate.is_dir():
            return candidate
    return base


def _context_pack_fragment(
    seed: Seed,
    repo_root: str | Path | None,
) -> str:
    """Render the deterministic context pack fragment, or empty string.

    Root resolution happens before the config lookup so the common
    no-repo path (unit tests, greenfield seeds) never loads config and
    never touches the filesystem scanner.
    """
    root = _resolve_context_pack_root(seed, repo_root)
    if root is None:
        return ""

    from ouroboros.config import get_context_pack_enabled

    if not get_context_pack_enabled():
        return ""

    from ouroboros.orchestrator.context_pack import build_context_pack, render_context_pack

    pack = build_context_pack(root)
    if pack is None:
        return ""
    return render_context_pack(pack)


def build_task_prompt(
    seed: Seed,
    strategy: ExecutionStrategy | None = None,
) -> str:
    """Build task prompt from seed acceptance criteria.

    Args:
        seed: Seed containing acceptance criteria.
        strategy: Execution strategy for prompt customization.
            If None, uses strategy from seed.task_type.

    Returns:
        Task prompt string.
    """
    if strategy is None:
        strategy = get_strategy(seed.task_type)

    ac_list = "\n".join(f"{i + 1}. {ac}" for i, ac in enumerate(ac_texts(seed.acceptance_criteria)))
    suffix = strategy.get_task_prompt_suffix()

    return f"""Execute the following task according to the acceptance criteria:

## Goal
{seed.goal}

## Acceptance Criteria
{ac_list}

{render_auto_recursion_guard()}

{suffix}
"""


# =============================================================================
# Runner
# =============================================================================


# Progress event emission interval (every N messages)
PROGRESS_EMIT_INTERVAL = 10

# Session progress persistence interval (every N messages)
SESSION_PROGRESS_PERSIST_INTERVAL = 10

# Cancellation check interval (every N messages)
CANCELLATION_CHECK_INTERVAL = 5

# Frugality proof is a multi-run experiment, but its run-end consumer must not
# scan or mix the whole event database. Session-start events provide the stable
# seed_id -> execution_id ownership map; inspect a bounded recent window and at
# most this many same-seed executions.
FRUGALITY_PROOF_SESSION_LOOKBACK = 1000
FRUGALITY_PROOF_MAX_COHORT_RUNS = 50
EXECUTION_CONTRACT_VERSION = 2
FRUGALITY_PROOF_PROTOCOL_VERSION = 1
EXECUTION_CONTRACT_PROGRESS_KEY = "execution_contract"
FORCED_EXECUTION_PERMISSION_MODE = "bypassPermissions"

_LONG_RETRY_AFTER_SECONDS = 60 * 60
_DURATION_PATTERN = re.compile(
    r"\b(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b",
    re.IGNORECASE,
)
_USAGE_LIMIT_RECOVERY_KINDS = frozenset(
    {
        "usage_limit",
        "usage_quota",
        "quota_limit",
        "quota_window",
        "quota_exceeded",
        "quota_exhausted",
        "usage_limit_pause",
    }
)
_RESUME_RETRY_RECOVERY_KIND = "resume_retry"
_USAGE_LIMIT_TEXT_PATTERNS = (
    re.compile(
        r"\b(?:usage|quota|credit|request)\s+"
        r"(?:limit|quota|cap|window|allowance)\b.{0,80}"
        r"\b(?:hit|reached|exceeded|exhausted|depleted)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:hit|reached|exceeded|exhausted|depleted)\b.{0,80}"
        r"\b(?:usage|quota|credit|request)\s+"
        r"(?:limit|quota|cap|window|allowance)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:quota|allowance)\s+(?:exceeded|exhausted|depleted)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:usage\s+limit|quota\s+window|rate\s+limit\s+window)"
        r"\s+(?:hit|reached|exceeded|exhausted|depleted)\b",
        re.IGNORECASE,
    ),
)
_USAGE_LIMIT_WINDOW_CONTEXT_PATTERN = re.compile(
    r"\b(?:usage|quota|allowance|rate|request)\s+"
    r"(?:limit|quota|cap|window|allowance)\b.{0,120}"
    r"\b(?:reached|exceeded|exhausted|depleted|hit|reset|resets|available|renews)\b"
    r"|\b(?:reached|exceeded|exhausted|depleted|hit|reset|resets|available|renews)\b"
    r".{0,120}\b(?:usage|quota|allowance|rate|request)\s+"
    r"(?:limit|quota|cap|window|allowance)\b",
    re.IGNORECASE,
)


class OrchestratorRunner:
    """Main orchestration runner for executing seeds via Claude Agent.

    Converts Seed specifications to agent prompts, executes via adapter,
    tracks progress through event emission, and displays status via Rich.

    Optionally integrates with external MCP servers via MCPClientManager
    to provide additional tools to the Claude Agent during execution.
    """

    def __init__(
        self,
        adapter: AgentRuntime,
        event_store: EventStore,
        console: Console | None = None,
        mcp_manager: MCPClientManager | None = None,
        mcp_tool_prefix: str = "",
        debug: bool = False,
        enable_decomposition: bool = True,
        decomposition_mode: Literal["preflight", "bounce_only", "off"] | None = None,
        inherited_runtime_handle: RuntimeHandle | None = None,
        inherited_tools: list[str] | None = None,
        task_cwd: str | None = None,
        task_workspace: TaskWorkspace | None = None,
        checkpoint_store: CheckpointStore | None = None,
        max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
        max_parallel_workers: int = 3,
        fat_harness_mode: bool = False,
        base_model_tier: str | None = None,
        efficiency_mode: str | None = None,
        frugality_assurance: str | None = None,
        session_signal_hub: SessionSignalHub | None = None,
    ) -> None:
        """Initialize orchestrator runner.

        Args:
            adapter: Agent runtime for task execution.
            event_store: Event store for persistence.
            console: Rich console for output. Uses default if not provided.
            mcp_manager: Optional MCP client manager for external tool integration.
                        When provided, tools from connected MCP servers will be
                        made available to the Claude Agent during execution.
            mcp_tool_prefix: Optional prefix to add to MCP tool names to avoid
                           conflicts (e.g., "mcp_" makes "read" become "mcp_read").
            debug: Enable verbose logging output. When False, only Live display shown.
            enable_decomposition: Enable AC decomposition into Sub-ACs.
            decomposition_mode: Optional decomposition mode override. When omitted,
                the runner uses ``execution.decomposition_mode`` from config.
                ``enable_decomposition=False`` forces the effective mode to ``off``.
            inherited_runtime_handle: Optional parent Claude runtime handle for
                        delegated child executions that should fork a session.
            inherited_tools: Optional effective tool set inherited from a
                        delegating parent session.
            task_cwd: Explicit working directory override for task execution metadata.
            task_workspace: Managed task workspace metadata for persistence and cleanup.
            checkpoint_store: Optional checkpoint store for execution state persistence
                        and recovery. When provided, enables per-level state snapshots.
            max_decomposition_depth: Maximum recursive AC decomposition depth.
            max_parallel_workers: Maximum concurrent AC workers for parallel execution.
            fat_harness_mode: Enforce profile typed-evidence validation plus
                verifier PASS at atomic AC acceptance. Public entrypoints that
                can support the gate (for example CLI `ooo run`) pass this
                explicitly; the low-level constructor default stays False so
                direct runner/resume callers are not silently converted to a
                stricter contract they cannot satisfy.
            base_model_tier: Force the top-level model-routing tier instead of
                deriving it from the config default. Threaded by the MCP
                ``execute_seed`` handler from its ``model_tier`` tool arg
                (small/medium/large → frugal/standard/frontier); the CLI passes
                nothing so routing derives its own base tier.
            efficiency_mode: ``adaptive`` allows decomposed-child tier lowering;
                ``quality_first`` keeps children at the parent starting tier.
            frugality_assurance: ``off``, ``observe``, or explicit ``strict``.
                Strict is the only preference that can authorize an otherwise
                eligible shadow baseline.
            session_signal_hub: Optional shared Synapse registry used to deliver
                bounded signals to exact active AC attempts.
        """
        self._adapter = adapter
        self._forced_permission_mode = self._force_adapter_permission_mode(adapter)
        self._event_store = event_store
        self._checkpoint_store = checkpoint_store
        self._console = console or Console()
        self._session_repo = SessionRepository(event_store)
        self._mcp_manager: MCPClientManager | None = mcp_manager
        self._mcp_tool_prefix = mcp_tool_prefix
        self._debug = debug
        self._enable_decomposition = enable_decomposition
        self._inherited_runtime_handle = self._force_runtime_handle_permission(
            inherited_runtime_handle
        )
        self._inherited_tools = list(inherited_tools) if inherited_tools else None
        self._task_cwd = task_cwd
        self._task_workspace = task_workspace
        self._max_decomposition_depth = max(0, max_decomposition_depth)
        self._max_parallel_workers = max(1, max_parallel_workers)
        self._fat_harness_mode = fat_harness_mode
        self._session_signal_hub = session_signal_hub
        self._execution_preferences_override_explicit = (
            efficiency_mode is not None or frugality_assurance is not None
        )
        self._execution_preferences = resolve_execution_preferences(
            efficiency_mode,
            frugality_assurance,
        )
        self._requested_model_tier = base_model_tier
        # Effort-first investment dial (RFC #1405): base level for the runner's own
        # direct execution paths (single-AC / resume), which call execute_task
        # without going through ParallelACExecutor. Resolved once; None ⇒ dormant.
        from ouroboros.config import get_agent_reasoning_effort

        self._reasoning_effort = get_agent_reasoning_effort()
        # Model-tier investment router (the frugality sibling of reasoning_effort),
        # built once so a single runner instance routes every unit consistently.
        # Global escape hatch: routing is on by default, so honor an explicit kill
        # switch (a custom-proxy codex user may need to disable it entirely).
        self._model_router: ModelRouter | None = None
        _model_routing_env = os.environ.get("OUROBOROS_MODEL_TIER_ROUTING")
        _model_routing_disabled = (_model_routing_env or "").strip().lower() in {
            "0",
            "off",
            "false",
        }
        # An explicit user model pin disables routing (routing must never override
        # it). The DEFAULT sonnet pin that execution_handlers/run.py pass to
        # create_agent_runtime is a SHIPPED default, not a user pin — only the env
        # var counts here.
        _model_pin_env = os.environ.get("OUROBOROS_EXECUTION_MODEL")
        _model_pin = _model_pin_env.strip() or None if _model_pin_env else None
        # Resume normally restores the run's persisted resolved router. These are
        # the existing user-facing controls that explicitly request a different
        # contract for this invocation, so only they may replace it.
        self._model_routing_override_explicit = bool(
            base_model_tier is not None
            or _model_pin is not None
            or (_model_routing_env is not None and _model_routing_env.strip())
        )
        # Verify-by-default execution knobs (PR-V). Start from the shipped config
        # so direct/test construction in a fresh HOME still gets the real defaults
        # (including the model-tier ladder), then replace it with the user's config
        # when one exists. A missing/malformed config must not silently disable
        # routing by leaving ``self._model_router`` at None.
        from ouroboros.config import get_default_config, load_config

        _shipped_config = get_default_config()
        _config = _shipped_config
        try:
            _config = load_config()
        except Exception:  # pragma: no cover - defensive config fallback
            pass
        # A valid partial/older config is materialized as ``tiers={}`` by the
        # Pydantic default. Treat only that empty mapping as "not configured" so
        # routing keeps the shipped ladder; any non-empty user ladder remains the
        # exact source of truth (including intentionally sparse/custom tiers).
        _economics_config = _config.economics
        if not _economics_config.tiers:
            _economics_config = _economics_config.model_copy(
                update={"tiers": _shipped_config.economics.tiers}
            )
        _execution_config = _config.execution
        self._run_verify_commands = _execution_config.run_verify_commands
        self._verify_command_timeout_seconds = _execution_config.verify_command_timeout_seconds
        self._ac_retry_attempts = _execution_config.ac_retry_attempts
        self._project_guidance_ids = tuple(_execution_config.project_guidance)
        self._decomposition_mode: Literal["preflight", "bounce_only", "off"] = (
            "off"
            if not enable_decomposition
            else (
                _execution_config.decomposition_mode
                if decomposition_mode is None
                else decomposition_mode
            )
        )
        if not _model_routing_disabled:
            from ouroboros.orchestrator.model_routing import build_model_router

            self._model_router = build_model_router(
                _economics_config,
                runtime_backend=getattr(adapter, "runtime_backend", None),
                pinned_model=_model_pin,
                base_tier_override=base_model_tier,
            )
        self._apply_efficiency_mode_to_router()
        self._execution_contract: dict[str, Any] | None = None
        self._process_local_authorities: dict[
            tuple[str, str], _ProcessLocalAuthorityGeneration
        ] = {}
        self._pending_lifecycle_intents: dict[str, _PendingLifecycleIntent] = {}
        self._execution_guidance: ExecutionGuidanceBundle | None = None
        # Opt-in shadow-replay baseline harness (frugality-proof AC5). Read ONCE
        # here next to the router build and threaded to the parallel executor.
        # Default OFF. Enabling the flag only arms the experiment's eligibility
        # checks. Current live decompositions have no deterministic MECE trust
        # attestation, and bundled runtimes have no complete replay-isolation
        # attestation, so production leaves are quarantined before baseline model
        # dispatch. A future fully-attested experiment may incur the extra cost.
        from ouroboros.orchestrator.shadow_replay import shadow_replay_enabled_from_env

        self._shadow_replay_requested = shadow_replay_enabled_from_env()
        self._shadow_replay_enabled = self._resolved_shadow_replay_enabled()
        if self._shadow_replay_requested and not self._shadow_replay_enabled:
            log.warning(
                "orchestrator.runner.shadow_replay_not_authorized",
                frugality_assurance=self._execution_preferences.frugality_assurance.value,
                explicit=self._execution_preferences.frugality_assurance_explicit,
                note="Shadow replay requires explicitly requested strict assurance.",
            )
        elif self._shadow_replay_enabled:
            log.warning(
                "orchestrator.runner.shadow_replay_enabled",
                note=(
                    "OUROBOROS_SHADOW_REPLAY is ON — the experiment harness is "
                    "ARMED. Current live decompositions have no deterministic MECE "
                    "attestation, and bundled runtimes have no complete replay-"
                    "isolation attestation, so baseline dispatch is quarantined and "
                    "no shadow baseline is emitted until both contracts are met."
                ),
            )
            self._console.print(
                "[bold yellow]⚠ Shadow-replay experiment ARMED "
                "(OUROBOROS_SHADOW_REPLAY). Live decompositions and bundled runtimes "
                "currently lack the required MECE/isolation attestations, so baseline "
                "model dispatch is skipped and no shadow baseline is emitted.[/bold yellow]"
            )
        self._announced_param_degradations: set[tuple[str, str]] = set()
        # Track active session for external cancellation by execution_id
        self._active_sessions: dict[str, str] = {}  # execution_id -> session_id

    def _apply_efficiency_mode_to_router(self) -> None:
        """Apply the public efficiency preference to the resolved tier router."""
        if self._model_router is None or self._execution_preferences.child_model_lowering_enabled:
            return
        self._model_router = replace(
            self._model_router,
            child_tier=self._model_router.base_tier,
        )

    def _resolved_shadow_replay_enabled(self) -> bool:
        """Gate the expensive proof harness on explicit strict authorization."""
        return bool(
            getattr(self, "_shadow_replay_requested", False)
            and self._execution_preferences.strict_baseline_authorized
        )

    def _announce_param_degradations(
        self,
        *,
        system_prompt: str | None,
        tools: list[str] | None,
    ) -> None:
        """Surface requested execution params this runtime will degrade."""
        announce_execution_param_degradations(
            self._adapter,
            system_prompt=system_prompt,
            tools=tools,
            announced=self._announced_param_degradations,
            console=self._console,
            log_event="orchestrator.runner.param_degraded",
        )

    def _execution_guidance_delivery_mode(self) -> str:
        bundle = self._ensure_new_run_guidance()
        support = runtime_capabilities_for(self._adapter).system_prompt_support
        if bundle.refs and support is ParamSupport.IGNORED:
            raise OrchestratorError(
                message="Runtime cannot deliver declared project execution guidance",
                details={
                    "runtime_backend": self._runtime_backend_contract(),
                    "system_prompt_support": support.value,
                    "guidance_ids": [ref.guidance_id for ref in bundle.refs],
                },
            )
        return support.value

    async def _record_execution_guidance_injection(
        self,
        *,
        session_id: str,
        execution_id: str,
        injection_key: str = "start",
    ) -> None:
        bundle = self._ensure_new_run_guidance()
        if not bundle.refs:
            return
        try:
            prior_events = await self._event_store.replay("session", session_id)
        except Exception as exc:
            raise OrchestratorError(
                message="Failed to replay declared project guidance provenance",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(exc),
                },
            ) from exc
        if isinstance(prior_events, list | tuple) and any(
            event.type == "orchestrator.guidance.injected"
            and event.data.get("execution_id") == execution_id
            and event.data.get("fragment_hash") == bundle.rendered_fragment_hash
            and event.data.get("injection_key") == injection_key
            for event in prior_events
        ):
            return
        event = create_guidance_injected_event(
            session_id=session_id,
            execution_id=execution_id,
            guidance_refs=[ref.to_metadata() for ref in bundle.refs],
            fragment_hash=bundle.rendered_fragment_hash,
            fragment_size_bytes=bundle.rendered_fragment_size_bytes,
            delivery_mode=self._execution_guidance_delivery_mode(),
            injection_key=injection_key,
        )
        try:
            await self._event_store.append(event)
        except Exception as exc:
            raise OrchestratorError(
                message="Failed to persist declared project guidance provenance",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(exc),
                },
            ) from exc

    async def _route_call_effort(
        self,
        *,
        execution_id: str | None,
        session_id: str | None,
    ) -> dict[str, str]:
        """Lay the runner's own execute_task paths on BOTH investment contracts.

        These legacy direct paths do not go through ParallelACExecutor, so without
        this they would silently skip effort AND model-tier routing. Seeds carrying
        AC investment metadata are routed through the AC executor instead, and
        resume fails closed until it can restore per-AC authority. Returns the
        merged execute_task kwargs (empty unless the runtime enforces the respective
        parameter).

        It records ``execution.ac.investment_assessed`` plus the applicable
        ``execution.ac.effort_routed`` and ``execution.ac.model_routed`` events for
        OBSERVABILITY — so
        a direct run's routing is visible in the event stream exactly like the
        parallel path's. These events are deliberately NOT a frugality-proof
        contribution: a direct run is a single top-level unit
        (``is_decomposed_child=False``) with no per-AC decomposition, so the
        payload carries no ``ac_id``. The deterministic proof excludes it on both
        counts — ``assemble_triads`` skips ``ac_id``-less events, and
        ``counts_in_proof`` only admits decomposed children — because the
        hypothesis is about children running at lower effort, which a top-level
        direct call has nothing to say about. ``call_site="runner"`` marks the
        origin so the two emission paths are distinguishable in the stream.
        """
        from ouroboros.orchestrator.effort_routing import assess_investment, resolve_execute_effort
        from ouroboros.orchestrator.model_routing import resolve_execute_model

        investment_assessment = assess_investment(None)
        decision, kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=self._reasoning_effort,
            is_decomposed_child=False,
            investment_assessment=investment_assessment,
        )
        model_decision, model_kwargs = resolve_execute_model(
            self._adapter,
            router=self._model_router,
            is_decomposed_child=False,
            decomposition_trustworthy=False,
        )
        # Merge the model override; kwargs carry a parameter ONLY for runtimes that
        # enforce it, so an advised runtime is never handed one.
        kwargs = {**kwargs, **model_kwargs}
        from ouroboros.events.base import BaseEvent

        try:
            await self._event_store.append(
                BaseEvent(
                    type="execution.ac.investment_assessed",
                    aggregate_type="execution",
                    aggregate_id=execution_id or session_id or "",
                    data={
                        "execution_id": execution_id,
                        "session_id": session_id,
                        "is_decomposed_child": False,
                        **investment_assessment.to_event_data(),
                        "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                        "call_site": "runner",
                    },
                )
            )
        except Exception as exc:
            log.warning(
                "orchestrator.runner.investment_assessed.persist_failed",
                error=str(exc),
            )
        if decision.level is not None:
            # Observability-only: this event must never make runtime dispatch/resume
            # depend on event-store health. _route_call_effort runs BEFORE
            # execute_task on the direct and resume paths, so a raw append would turn
            # a degraded/locked store into a dispatch failure. Degrade to a warning
            # instead — matching how the parallel executor treats the same telemetry.
            try:
                await self._event_store.append(
                    BaseEvent(
                        type="execution.ac.effort_routed",
                        aggregate_type="execution",
                        aggregate_id=execution_id or session_id or "",
                        data={
                            "execution_id": execution_id,
                            "session_id": session_id,
                            "is_decomposed_child": False,
                            "effort_level": decision.level,
                            "effort_mode": decision.mode,
                            "base_reasoning_effort": self._reasoning_effort,
                            "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                            "investment_assessment": investment_assessment.to_event_data(),
                            "call_site": "runner",
                        },
                    )
                )
            except Exception as exc:
                log.warning(
                    "orchestrator.runner.effort_routed.persist_failed",
                    error=str(exc),
                    effort_level=decision.level,
                    effort_mode=decision.mode,
                )
        if model_decision.model is not None:
            # Same observe-only contract as the effort event above: a degraded
            # event store must degrade to a warning, never fail dispatch/resume.
            try:
                await self._event_store.append(
                    BaseEvent(
                        type="execution.ac.model_routed",
                        aggregate_type="execution",
                        aggregate_id=execution_id or session_id or "",
                        data={
                            "execution_id": execution_id,
                            "session_id": session_id,
                            "is_decomposed_child": False,
                            "decomposition_trustworthy": False,
                            "child_downgrade_authorized": False,
                            "model_tier": model_decision.tier,
                            "model": model_decision.model,
                            "model_mode": model_decision.mode,
                            "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                            "call_site": "runner",
                        },
                    )
                )
            except Exception as exc:
                log.warning(
                    "orchestrator.runner.model_routed.persist_failed",
                    error=str(exc),
                    model_tier=model_decision.tier,
                    model_mode=model_decision.mode,
                )
        return kwargs

    async def _evaluate_frugality_proof(self, execution_id: str) -> None:
        """Run the deterministic frugality proof over a bounded same-seed cohort.

        Best-effort, run-end telemetry: session-start events identify the current
        execution's ``seed_id`` and the most recent executions of that same seed.
        It queries only that bounded cohort, assembles frugality triads, and emits an
        ``execution.frugality_proof.evaluated`` event plus one console line with the
        verdict. This is what makes ``min_runs >= 3`` reachable without mixing a
        different seed/project's evidence into the proof. When the session-start
        ownership event is unavailable it safely falls back to the current execution
        only, which remains insufficient until enough attributable runs exist.

        Grounding uses the live producer's explicit fail-closed policy (accepted
        child -> no regression; rejected child -> conservative regression), while
        the shadow replay supplies only the paired token baseline. Any failure
        degrades to a warning; the proof never fails the run.
        """
        from ouroboros.events.base import BaseEvent
        from ouroboros.orchestrator.frugality_proof import assemble_triads, evaluate_proof

        try:
            seed_id, cohort_execution_ids = await self._frugality_proof_cohort(execution_id)
            events = []
            for cohort_execution_id in cohort_execution_ids:
                events.extend(
                    await self._event_store.query_execution_related_events(
                        cohort_execution_id,
                        limit=None,
                    )
                )
            rows = assemble_triads(events)
            verdict = evaluate_proof(rows)
            await self._event_store.append(
                BaseEvent(
                    type="execution.frugality_proof.evaluated",
                    aggregate_type="execution",
                    aggregate_id=execution_id,
                    data={
                        "execution_id": execution_id,
                        "seed_id": seed_id,
                        "cohort_execution_ids": list(cohort_execution_ids),
                        "status": verdict.status.value,
                        "counted_rows": verdict.counted_rows,
                        "runs": verdict.runs,
                        "token_reduction_pct": verdict.token_reduction_pct,
                        "grounding_regressions": verdict.grounding_regressions,
                        "reason": verdict.reason,
                        "thresholds": dict(verdict.thresholds),
                    },
                )
            )
            self._console.print(f"Frugality proof: {verdict.status.value} — {verdict.reason}")
        except Exception as exc:
            log.warning(
                "orchestrator.runner.frugality_proof.eval_failed",
                execution_id=execution_id,
                error=str(exc),
            )

    async def _report_frugality_retrospective(
        self,
        *,
        execution_id: str,
        session_id: str,
        terminal_status: str,
    ) -> bool:
        """Best-effort execution-finalized evidence reporting.

        The reporter itself returns before querying on ``paused``. Keeping this
        wrapper best-effort preserves the observability-only contract: persistence
        or projection failures never change execution success, routing, or retry
        behavior.
        """
        from ouroboros.observability.frugality_retrospective import (
            report_frugality_retrospective,
        )

        try:
            return await report_frugality_retrospective(
                self._event_store,
                execution_id=execution_id,
                session_id=session_id,
                terminal_status=terminal_status,
            )
        except Exception as exc:
            log.warning(
                "orchestrator.runner.frugality_retrospective.report_failed",
                execution_id=execution_id,
                session_id=session_id,
                terminal_status=terminal_status,
                error=str(exc),
            )
            return False

    async def _frugality_proof_cohort(
        self,
        execution_id: str,
    ) -> tuple[str | None, tuple[str, ...]]:
        """Return recent executions with the exact same proof protocol identity.

        ``orchestrator.session.started`` is the authoritative ownership record for
        ``seed_id``, canonical project/workspace, protocol version, and resolved
        routing fingerprint. EventStore returns newest first, so selected prior
        runs are the most recent comparable experiment runs. Any missing legacy
        metadata falls back to current-only rather than mixing a global DB cohort.
        """
        query_events = getattr(self._event_store, "query_events", None)
        if not callable(query_events):
            return None, (execution_id,)

        session_starts = await query_events(
            event_type="orchestrator.session.started",
            limit=FRUGALITY_PROOF_SESSION_LOOKBACK,
        )
        if not isinstance(session_starts, (list, tuple)):
            return None, (execution_id,)
        current_identity: tuple[str, str, str, int, str, str, str] | None = None
        for event in session_starts:
            data = getattr(event, "data", None)
            if not isinstance(data, Mapping) or data.get("execution_id") != execution_id:
                continue
            current_identity = self._proof_cohort_identity(data)
            break
        if current_identity is None:
            return None, (execution_id,)

        current_seed_id = current_identity[0]
        # An explicit resume override can intentionally replace the persisted
        # start contract. That execution now contains mixed regimes, so it must
        # never borrow prior runs for a proof verdict.
        if self._execution_contract is not None:
            active_identity = self._proof_cohort_identity(
                {
                    "seed_id": current_seed_id,
                    EXECUTION_CONTRACT_PROGRESS_KEY: self._execution_contract,
                }
            )
            if active_identity != current_identity:
                return current_seed_id, (execution_id,)

        cohort: list[str] = [execution_id]
        seen = {execution_id}
        for event in session_starts:
            data = getattr(event, "data", None)
            if not isinstance(data, Mapping):
                continue
            if self._proof_cohort_identity(data) != current_identity:
                continue
            candidate = data.get("execution_id")
            if not isinstance(candidate, str) or not candidate.strip():
                continue
            normalized = candidate.strip()
            if normalized in seen:
                continue
            cohort.append(normalized)
            seen.add(normalized)
            if len(cohort) >= FRUGALITY_PROOF_MAX_COHORT_RUNS:
                break
        return current_seed_id, tuple(cohort)

    def _plan_parallel_workers(self) -> int:
        """Return the effective fan-out worker count for the connected backend.

        Ouroboros caps delivery fan-out to the connected backend's known
        concurrency limit so it does not stampede the LLM's rate/quota window
        (R3). Backends whose underlying LLM limits are unknown — the CLI
        runtimes — serialize by default and are raised only via
        ``OUROBOROS_MAX_CONCURRENCY``.
        """
        limits = resolve_backend_limits(self._adapter.runtime_backend)
        return plan_fan_out_concurrency(self._max_parallel_workers, limits)

    @property
    def mcp_manager(self) -> MCPClientManager | None:
        """Return the MCP client manager if configured.

        Returns:
            The MCPClientManager instance or None if not configured.
        """
        return self._mcp_manager

    @property
    def session_repo(self) -> SessionRepository:
        """Return the session repository.

        Returns:
            The SessionRepository instance for session management.
        """
        return self._session_repo

    @property
    def active_sessions(self) -> dict[str, str]:
        """Return a copy of currently active execution_id -> session_id mappings.

        Returns:
            Dict mapping execution IDs to session IDs for in-flight executions.
        """
        return dict(self._active_sessions)

    def _register_session(self, execution_id: str, session_id: str) -> None:
        """Register an active session for cancellation tracking.

        Called at the start of execution to enable in-flight cancellation.
        Also writes a heartbeat file so the orphan detector knows this
        session is alive (runtime-agnostic mechanism).

        Args:
            execution_id: Execution ID for external lookup.
            session_id: Session ID for internal tracking.
        """
        from ouroboros.orchestrator.heartbeat import acquire as acquire_lock

        acquire_lock(session_id)
        # Do not publish an in-memory cancellation route before its liveness
        # lease exists. A failed claim must not leave a routable, unowned
        # session behind.
        self._active_sessions[execution_id] = session_id

    def _unregister_session(
        self,
        execution_id: str,
        session_id: str,
        *,
        release_liveness_lease: bool = True,
    ) -> None:
        """Unregister a session after execution completes.

        Called at the end of execution (success, failure, or cancellation)
        to clean up tracking state and normally remove the heartbeat file. A
        deliberately paused process-local session keeps its liveness lease:
        the claim is released so its original owner can resume it, while other
        runners must still recognize that the live capability has not crashed.

        Args:
            execution_id: Execution ID to remove.
            session_id: Session ID to remove.
            release_liveness_lease: Whether this lifecycle path is terminal and
                may remove the PID liveness lease.
        """
        from ouroboros.orchestrator.heartbeat import (
            release_if_owned_by_current_process as release_lock,
        )

        self._active_sessions.pop(execution_id, None)
        if release_liveness_lease:
            release_lock(session_id)

    def _cleanup_pre_execution_state(
        self,
        execution_id: str | None,
        session_id: str | None,
        *,
        session_registered: bool,
        retire_authority: bool = True,
    ) -> None:
        """Release pre-loop runner state after an aborted execution path.

        Use this only when the caller has either observed a durable terminal
        result or proved that no usable authority exists. Retryable persistence
        and raw-cancellation paths use
        :meth:`_preserve_process_local_owner_for_retry` instead.
        """
        if retire_authority and execution_id is not None and session_id is not None:
            self._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
        if session_registered and execution_id is not None and session_id is not None:
            self._unregister_session(execution_id, session_id)
        if self._task_workspace is not None:
            release_lock(self._task_workspace.lock_path)

    def _preserve_process_local_owner_for_retry(
        self,
        *,
        execution_id: str,
        session_id: str,
    ) -> None:
        """Release an exiting coroutine's effects without retiring authority.

        A durable RUNNING session remains truthful only while its exact local
        generation and liveness lease remain available.  Persistence failures
        and raw task cancellation therefore release the exclusive claim,
        active route, and worktree lock, but keep the registration resumable by
        the same retained owner.
        """
        self._release_process_local_authority(
            session_id=session_id,
            execution_id=execution_id,
        )
        self._unregister_session(
            execution_id,
            session_id,
            release_liveness_lease=False,
        )
        if self._task_workspace is not None:
            release_lock(self._task_workspace.lock_path)

    def _terminal_persistence_pending_result(
        self,
        *,
        session_id: str,
        execution_id: str,
        requested_status: SessionStatus,
        cause: object,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Return a typed retryable result while preserving the live owner."""
        self._preserve_process_local_owner_for_retry(
            session_id=session_id,
            execution_id=execution_id,
        )
        return Result.err(
            OrchestratorError(
                message=(
                    f"Failed to persist terminal status {requested_status.value}; "
                    "process-local authority remains live"
                ),
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "requested_status": requested_status.value,
                    "cause": str(cause),
                    "resume_blocked": "terminal_persistence_pending",
                    "terminal_persistence_pending": True,
                },
            )
        )

    def _pause_persistence_pending_result(
        self,
        *,
        session_id: str,
        execution_id: str,
        cause: object,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Return a typed pause failure without publishing a false PAUSED state."""
        self._preserve_process_local_owner_for_retry(
            session_id=session_id,
            execution_id=execution_id,
        )
        return Result.err(
            OrchestratorError(
                message="Failed to persist paused session state; process-local authority remains live",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "requested_status": SessionStatus.PAUSED.value,
                    "cause": str(cause),
                    "resume_blocked": "pause_persistence_pending",
                    "pause_persistence_pending": True,
                },
            )
        )

    async def _resolve_pause_publication(
        self,
        *,
        session_id: str,
        execution_id: str,
        pause_result: Result[bool, PersistenceError],
        pause: RecoverableFailurePause,
    ) -> tuple[
        SessionStatus | None,
        Result[OrchestratorResult, OrchestratorError] | None,
    ]:
        """Resolve conditional PAUSED publication against a terminal winner.

        ``PAUSED`` means the pause append won. An explicit terminal status
        means the pause lost and callers must project/clean up that durable
        winner. An unreadable winner is retryable and preserves the exact
        process-local owner rather than guessing at lifecycle state.
        """
        if pause_result.is_err:
            self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
                execution_id=execution_id,
                status=SessionStatus.PAUSED,
                error_message=pause.reason,
                pause=pause,
            )
            return None, self._pause_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=pause_result.error,
            )
        if pause_result.value:
            self._pending_lifecycle_intents.pop(session_id, None)
            return SessionStatus.PAUSED, None

        try:
            reconstructed = await self._session_repo.reconstruct_session(session_id)
        except Exception as exc:
            return None, self._pause_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
            )
        if reconstructed.is_ok and reconstructed.value.status in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }:
            self._pending_lifecycle_intents.pop(session_id, None)
            return reconstructed.value.status, None

        cause: object = (
            reconstructed.error
            if reconstructed.is_err
            else PersistenceError(
                "Conditional pause lost but no durable terminal winner could be reconstructed",
                details={"session_id": session_id},
            )
        )
        self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
            execution_id=execution_id,
            status=SessionStatus.PAUSED,
            error_message=pause.reason,
            pause=pause,
        )
        return None, self._pause_persistence_pending_result(
            session_id=session_id,
            execution_id=execution_id,
            cause=cause,
        )

    async def _project_execution_outcome(
        self,
        *,
        execution_id: str,
        session_id: str,
        terminal_status: str,
        terminal_event: BaseEvent,
    ) -> None:
        """Run auxiliary outcome projections without invalidating durable PAUSED."""
        try:
            await self._event_store.append(terminal_event)
            await self._evaluate_frugality_proof(execution_id)
            if terminal_status in {"completed", "failed", "cancelled"}:
                await self._report_frugality_retrospective(
                    execution_id=execution_id,
                    session_id=session_id,
                    terminal_status=terminal_status,
                )
        except Exception:
            if terminal_status != SessionStatus.PAUSED.value:
                raise
            log.exception(
                "orchestrator.runner.paused_auxiliary_projection_failed",
                execution_id=execution_id,
                session_id=session_id,
            )

    @staticmethod
    def _requested_terminal_status_from_error(error: BaseException) -> SessionStatus | None:
        """Recover the original terminal intent from a typed persistence error."""
        if not isinstance(error, OrchestratorError):
            return None
        details = error.details
        if details.get("terminal_persistence_pending") is not True:
            return None
        requested_status = details.get("requested_status")
        try:
            status = SessionStatus(requested_status)
        except (TypeError, ValueError):
            return None
        if status not in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }:
            return None
        return status

    def _terminal_persistence_pending_from_error(
        self,
        *,
        session_id: str,
        execution_id: str,
        error: BaseException,
    ) -> Result[OrchestratorResult, OrchestratorError] | None:
        """Preserve ownership instead of changing a failed terminal intent."""
        requested_status = self._requested_terminal_status_from_error(error)
        if requested_status is None:
            return None
        return self._terminal_persistence_pending_result(
            session_id=session_id,
            execution_id=execution_id,
            requested_status=requested_status,
            cause=error,
        )

    async def _reconcile_durable_terminal_and_cleanup(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> SessionStatus | None:
        """Return and fully reconcile a reconstructed durable terminal winner.

        Cancellation can arrive after the session CAS commits but before the
        execution-stream projection or ordinary cleanup finishes.  In that
        window preserving the live generation would contradict the durable
        terminal source of truth, so interruption paths reconcile first.
        """

        async def _reconcile() -> SessionStatus | None:
            try:
                reconstructed = await self._session_repo.reconstruct_session(session_id)
            except Exception:
                return None
            if reconstructed.is_err or reconstructed.value.status not in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }:
                return None
            durable_status = reconstructed.value.status
            self._pending_lifecycle_intents.pop(session_id, None)
            self._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            self._unregister_session(execution_id, session_id)
            await clear_cancellation(session_id)
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)
            return durable_status

        return await _await_process_local_cleanup(_reconcile())

    async def _cleanup_if_durable_terminal(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> bool:
        """Retire a claimed owner only after reconstructing a terminal winner."""
        return (
            await self._reconcile_durable_terminal_and_cleanup(
                session_id=session_id,
                execution_id=execution_id,
            )
            is not None
        )

    async def _persist_failure_and_cleanup(
        self,
        *,
        session_id: str,
        execution_id: str,
        error: BaseException,
        messages_processed: int = 0,
    ) -> tuple[SessionStatus | None, Result[OrchestratorResult, OrchestratorError] | None]:
        """Persist one durable failure winner before withdrawing ownership."""
        reconciled_during_exception = False
        try:
            durable_status = await self._persist_session_terminal_status(
                session_id=session_id,
                execution_id=execution_id,
                requested_status=SessionStatus.FAILED,
                error_message=str(error),
                error_type=type(error).__name__,
                messages_processed=messages_processed,
            )
        except (Exception, asyncio.CancelledError) as persistence_error:
            durable_status = await self._reconcile_durable_terminal_and_cleanup(
                session_id=session_id,
                execution_id=execution_id,
            )
            if durable_status is not None:
                reconciled_during_exception = True
                if isinstance(persistence_error, asyncio.CancelledError):
                    raise
            else:
                self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
                    execution_id=execution_id,
                    status=SessionStatus.FAILED,
                    error_message=str(error),
                    error_type=type(error).__name__,
                    messages_processed=messages_processed,
                )
                if isinstance(persistence_error, asyncio.CancelledError):
                    self._preserve_process_local_owner_for_retry(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    raise
                return None, self._terminal_persistence_pending_result(
                    session_id=session_id,
                    execution_id=execution_id,
                    requested_status=SessionStatus.FAILED,
                    cause=persistence_error,
                )

        if durable_status is None:
            return None, self._terminal_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                requested_status=SessionStatus.FAILED,
                cause=error,
            )

        if not reconciled_during_exception:
            self._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            self._unregister_session(execution_id, session_id)
            await clear_cancellation(session_id)
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)
        try:
            await self._event_store.append(
                create_execution_terminal_event(
                    execution_id=execution_id,
                    session_id=session_id,
                    status=durable_status.value,
                    error_message=(
                        str(error) if durable_status is not SessionStatus.COMPLETED else None
                    ),
                    messages_processed=messages_processed,
                )
            )
        except Exception:
            log.warning(
                "orchestrator.runner.failure_projection_failed",
                session_id=session_id,
                execution_id=execution_id,
                durable_status=durable_status.value,
            )
        return durable_status, None

    def _deserialize_runtime_handle(self, progress: dict[str, Any]) -> RuntimeHandle | None:
        """Deserialize runtime resume state from session progress."""
        runtime_payload = progress.get("runtime")
        try:
            runtime_handle = RuntimeHandle.from_dict(runtime_payload)
        except ValueError as exc:
            log.warning(
                "orchestrator.runner.runtime_handle_deserialize_failed",
                error=str(exc),
                runtime_keys=sorted(runtime_payload) if isinstance(runtime_payload, dict) else None,
            )
            runtime_handle = None
        if runtime_handle is not None:
            return runtime_handle

        legacy_session_id = progress.get("agent_session_id")
        if isinstance(legacy_session_id, str) and legacy_session_id:
            # Legacy sessions predate multi-runtime; infer backend from context
            legacy_backend = progress.get("runtime_backend", "claude")
            if not isinstance(legacy_backend, str):
                legacy_backend = "claude"
            return RuntimeHandle(backend=legacy_backend, native_session_id=legacy_session_id)

        return None

    def _implementation_policy_context(
        self,
        *,
        runtime_backend: str | None = None,
    ) -> PolicyContext:
        """Return the policy context used for implementation tool catalogs."""
        return PolicyContext(
            runtime_backend=runtime_backend or self._adapter.runtime_backend,
            session_role=PolicySessionRole.IMPLEMENTATION,
            execution_phase=PolicyExecutionPhase.IMPLEMENTATION,
        )

    def _evaluate_tool_catalog_policy(
        self,
        tool_catalog: SessionToolCatalog,
        *,
        runtime_backend: str | None = None,
    ) -> ToolCatalogPolicyResult:
        """Evaluate the implementation policy for a normalized tool catalog."""
        capability_graph = build_capability_graph(tool_catalog)
        policy_context = self._implementation_policy_context(runtime_backend=runtime_backend)
        policy_decisions = evaluate_capability_policy(capability_graph, policy_context)
        allowed_tools = [
            decision.name
            for decision in policy_decisions
            if decision.visible and decision.executable
        ]
        return ToolCatalogPolicyResult(
            allowed_tools=allowed_tools,
            capability_graph=capability_graph,
            policy_decisions=policy_decisions,
            policy_context=policy_context,
        )

    async def _emit_policy_capabilities_evaluated_event(
        self,
        session_id: str,
        capability_graph: CapabilityGraph,
        policy_decisions: tuple[PolicyDecision, ...],
        policy_context: PolicyContext,
    ) -> None:
        """Persist capability policy decisions for audit/debuggability.

        Best-effort: the audit record is auxiliary to the orchestration
        path, not a prerequisite for it.  An event-store failure here
        must never take down interview/evaluation/execution — we log
        the failure and continue, so that observability degradation
        never becomes an availability incident.
        """
        try:
            await self._event_store.append(
                create_policy_capabilities_evaluated_event(
                    session_id=session_id,
                    graph=capability_graph,
                    decisions=policy_decisions,
                    context=policy_context,
                )
            )
        except Exception as exc:
            log.warning(
                "orchestrator.runner.policy_audit_emit_failed",
                session_id=session_id,
                capability_count=len(capability_graph.capabilities),
                error=str(exc),
                error_type=type(exc).__name__,
            )

    def _seed_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        tool_catalog: SessionToolCatalog | None = None,
    ) -> RuntimeHandle | None:
        """Seed a runtime handle with startup metadata before execution begins."""
        backend = (
            runtime_handle.backend if runtime_handle is not None else None
        ) or self._adapter.runtime_backend
        if not backend:
            return runtime_handle

        metadata = dict(runtime_handle.metadata) if runtime_handle is not None else {}
        if tool_catalog is not None:
            metadata["tool_catalog"] = serialize_tool_catalog(tool_catalog)
            policy_result = self._evaluate_tool_catalog_policy(
                tool_catalog,
                runtime_backend=backend,
            )
            metadata["capability_graph"] = serialize_capability_graph(
                policy_result.capability_graph
            )
            metadata["control_plane"] = serialize_control_plane_state(
                build_control_plane_state(
                    policy_result.capability_graph,
                    policy_result.policy_decisions,
                )
            )

        cwd = self._effective_cwd(runtime_handle)
        approval_mode = self._forced_permission_mode

        if runtime_handle is not None:
            return replace(
                runtime_handle,
                backend=backend,
                kind=runtime_handle.kind or "agent_runtime",
                cwd=(
                    runtime_handle.cwd
                    if runtime_handle.cwd
                    else cwd
                    if isinstance(cwd, str) and cwd
                    else None
                ),
                approval_mode=approval_mode,
                updated_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )

        return RuntimeHandle(
            backend=backend,
            kind="agent_runtime",
            cwd=cwd if isinstance(cwd, str) and cwd else None,
            approval_mode=approval_mode
            if isinstance(approval_mode, str) and approval_mode
            else None,
            updated_at=datetime.now(UTC).isoformat(),
            metadata=metadata,
        )

    def _task_summary(self) -> dict[str, Any]:
        """Return summary metadata for the active task workspace."""
        if self._task_workspace is None:
            return {}
        return {
            "worktree_path": self._task_workspace.worktree_path,
            "worktree_branch": self._task_workspace.branch,
            "task_cwd": self._task_workspace.effective_cwd,
        }

    def _effective_cwd(self, runtime_handle: RuntimeHandle | None = None) -> str | None:
        """Resolve the effective cwd for persisted runtime metadata."""
        if self._task_cwd:
            return self._task_cwd
        if self._task_workspace is not None:
            return self._task_workspace.effective_cwd
        if runtime_handle is not None and runtime_handle.cwd:
            return runtime_handle.cwd
        cwd = self._adapter.working_directory
        return cwd if isinstance(cwd, str) and cwd else None

    @staticmethod
    def _canonical_path(value: str) -> str:
        """Return a symlink-resolved absolute path without requiring existence."""
        return str(Path(value).expanduser().resolve(strict=False))

    @classmethod
    def _task_workspace_identity(cls, workspace: TaskWorkspace) -> dict[str, str]:
        """Return the stable source identity encoded by a managed workspace."""
        project_root = cls._canonical_path(workspace.repo_root)
        original_cwd = cls._canonical_path(workspace.original_cwd)
        try:
            relative_workspace = Path(original_cwd).relative_to(project_root)
            workspace_path = relative_workspace.as_posix() or "."
        except ValueError:
            # Corrupted/legacy workspace metadata must not collapse onto a
            # broad identity. Keep the canonical absolute source cwd instead.
            workspace_path = original_cwd
        return {
            "project_root": project_root,
            "workspace_path": workspace_path,
        }

    def _proof_workspace_identity(self) -> dict[str, str] | None:
        """Return the stable project + source-workspace identity for this run.

        Managed task worktrees have a different checkout path for every session,
        so cohort identity is anchored to their persisted source repository and
        source-relative cwd. Non-worktree callers use their canonical effective
        cwd as a conservative project/workspace identity; this may split cohorts
        launched from different subdirectories, but can never mix projects.
        """
        if self._task_workspace is not None:
            return self._task_workspace_identity(self._task_workspace)

        effective_cwd = self._effective_cwd()
        if not isinstance(effective_cwd, str) or not effective_cwd.strip():
            return None
        canonical_cwd = self._canonical_path(effective_cwd)
        return {
            "project_root": canonical_cwd,
            "workspace_path": ".",
        }

    @classmethod
    def _task_resume_workspace_identity(cls, workspace: TaskWorkspace) -> dict[str, str]:
        """Return the exact managed checkout identity required for safe resume."""
        return {
            "mode": "task_workspace",
            "durable_id": workspace.durable_id,
            "repo_root": cls._canonical_path(workspace.repo_root),
            "worktree_path": cls._canonical_path(workspace.worktree_path),
            "effective_cwd": cls._canonical_path(workspace.effective_cwd),
            "branch": workspace.branch,
        }

    def _resume_workspace_identity(self) -> dict[str, str] | None:
        """Return session-specific checkout identity, unlike stable proof cohorting."""
        if self._task_workspace is not None:
            return self._task_resume_workspace_identity(self._task_workspace)
        effective_cwd = self._effective_cwd()
        if not isinstance(effective_cwd, str) or not effective_cwd.strip():
            return None
        return {
            "mode": "direct",
            "effective_cwd": self._canonical_path(effective_cwd),
        }

    @staticmethod
    def _routing_fingerprint(routing_contract: Mapping[str, Any]) -> str:
        """Hash a resolved routing contract into a stable cohort key."""
        encoded = json.dumps(
            dict(routing_contract),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _seed_semantics_fingerprint(seed: Seed) -> str:
        """Hash executable Seed semantics while excluding volatile identity fields."""
        payload = seed.to_dict()
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            metadata = dict(metadata)
            for key in ("seed_id", "created_at", "interview_id", "parent_seed_id"):
                metadata.pop(key, None)
            payload["metadata"] = metadata
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _constructor_model_contract(self) -> dict[str, Any]:
        """Return the runtime's normalized constructor-model pin contract.

        Every bundled runtime stores its constructor ``model`` argument in
        ``_model``. Read it statically so permissive mocks/custom ``__getattr__``
        implementations cannot fabricate a value, then apply the runtime's own
        statically declared ``_normalize_model`` hook when one exists. CLI
        runtimes use sentinels such as ``default`` or ``current`` to mean "no
        model pin"; persisting those raw strings as concrete pins would let an
        unpinned, routing-disabled resume bypass the effective-model guard.
        ``observed=False`` remains a truthful compatibility state for third-party
        runtimes that expose no constructor model at all; current-format resume
        then fails closed because the effective pin cannot be verified.
        """
        return constructor_model_contract(self._adapter)

    @staticmethod
    def _valid_constructor_model_contract(value: object) -> bool:
        """Return whether a persisted constructor-model contract is canonical."""
        return valid_constructor_model_contract(value)

    def _runtime_execution_identity_contract(self) -> dict[str, Any]:
        """Return no cross-process identity for a legacy dynamic runtime.

        Foundation A intentionally does not execute a runtime-owned identity
        provider here.  That provider may itself resolve mutable helpers,
        profile files, handler caches, or launcher state.  A live
        process-local generation below controls same-process resume instead.
        """
        return {"version": 1, "observed": False}

    @staticmethod
    def _valid_runtime_execution_identity_contract(value: object) -> bool:
        """Return whether a persisted backend execution identity is canonical."""
        return valid_runtime_execution_identity_contract(value)

    @staticmethod
    def _runtime_execution_proves_effective_model(value: object) -> bool:
        """Return whether a backend identity observed a concrete model/profile."""
        return runtime_execution_proves_effective_model(value)

    @staticmethod
    def _begin_process_local_authority_generation() -> _ProcessLocalAuthorityGeneration:
        """Mint one fresh live-only authority generation for a new session.

        The generation is deliberately returned to the caller rather than kept
        in a mutable runner-wide slot.  A single runner can prepare several
        sessions concurrently, and each preparation must retain its exact
        generation through contract creation and registry binding.
        """
        return _mint_process_local_authority_generation()

    @staticmethod
    def _process_local_authority_contract(
        generation: _ProcessLocalAuthorityGeneration,
    ) -> dict[str, object]:
        """Build evidence-only scope data for one issued live generation."""
        return _process_local_authority_contract(generation)

    def _has_live_process_local_authority(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> bool:
        """Check live authority before restoring runtime-controlled state."""
        if not isinstance(raw_contract, Mapping):
            return False
        authority = raw_contract.get("foundation_a_authority")
        return (
            _live_process_local_authority_generation(
                session_id,
                execution_id,
                authority,
                self._adapter,
            )
            is not None
        )

    def _has_live_process_local_authority_registration(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> bool:
        """See a local capability without granting another adapter its use.

        This distinction lets an observer reject safely when another live
        adapter owns the generation, instead of turning a valid paused or
        transitioning session into a false crash recovery.
        """
        if not isinstance(raw_contract, Mapping):
            return False
        return _has_live_process_local_authority_registration(
            session_id,
            execution_id,
            raw_contract.get("foundation_a_authority"),
        )

    def _process_local_authority_held_elsewhere(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> bool:
        """Return whether another live owner retains this process-local session.

        A different adapter must never receive the opaque capability, but it
        must also never turn a valid owner into a false crash-recovery path.
        The in-process registration covers a foreign adapter in this PID; the
        PID lease covers a holder in another process.  The exact adapter's own
        capability is intentionally excluded so normal non-paused validation
        continues to report the session state rather than an ownership error.
        """
        if self._has_live_process_local_authority(session_id, execution_id, raw_contract):
            return False
        if self._has_live_process_local_authority_registration(
            session_id,
            execution_id,
            raw_contract,
        ):
            return True
        from ouroboros.orchestrator.heartbeat import is_holder_alive

        return is_holder_alive(session_id)

    def _live_process_local_authority_generation(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> _ProcessLocalAuthorityGeneration | None:
        """Return the registry-issued generation for an already-live session."""
        if not isinstance(raw_contract, Mapping):
            return None
        return _live_process_local_authority_generation(
            session_id,
            execution_id,
            raw_contract.get("foundation_a_authority"),
            self._adapter,
        )

    def _claim_process_local_authority_generation(
        self,
        session_id: str,
        execution_id: str,
        raw_contract: object,
    ) -> tuple[_ProcessLocalAuthorityGeneration | None, bool]:
        """Claim a live capability before any effectful session work begins."""
        if not isinstance(raw_contract, Mapping):
            return None, False
        return _claim_process_local_authority_generation(
            session_id,
            execution_id,
            raw_contract.get("foundation_a_authority"),
            self._adapter,
        )

    def _release_process_local_authority(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Make a deliberately paused session resumable in this process again."""
        _release_process_local_authority_generation(
            session_id,
            execution_id,
            self._adapter,
        )

    def _cleanup_process_local_authority_after_external_terminal(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Drop runner-local references after another surface wrote terminal state.

        The registry has already invalidated the opaque capability.  This
        callback intentionally cleans only runner-local bookkeeping; the
        registry owns the liveness-lease release so it remains one operation.
        """
        self._process_local_authorities.pop((session_id, execution_id), None)
        self._active_sessions.pop(execution_id, None)
        if self._task_workspace is not None:
            release_lock(self._task_workspace.lock_path)

    def _register_process_local_authority(
        self,
        *,
        session_id: str,
        execution_id: str,
        execution_contract: Mapping[str, object],
        generation: _ProcessLocalAuthorityGeneration,
    ) -> None:
        """Bind the persisted correlation record to its live process capability."""
        authority = execution_contract.get("foundation_a_authority")
        if (
            not valid_process_local_authority_contract(authority)
            or authority.get("correlation_id") != generation.correlation_id
        ):
            raise OrchestratorError(
                message="Cannot create an invalid process-local execution authority",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "invalid": "foundation_a_authority",
                },
            )
        from ouroboros.orchestrator.heartbeat import acquire as acquire_session_lock

        try:
            _register_process_local_authority_generation(
                session_id,
                execution_id,
                generation,
                self._adapter,
            )
        except ValueError as exc:
            raise OrchestratorError(
                message="Cannot register process-local execution authority",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(exc),
                },
            ) from exc
        self._process_local_authorities[(session_id, execution_id)] = generation
        if not _register_process_local_authority_terminal_finalizer(
            session_id,
            execution_id,
            authority,
            self._adapter,
            ("runner", id(self)),
            lambda: self._cleanup_process_local_authority_after_external_terminal(
                session_id=session_id,
                execution_id=execution_id,
            ),
        ):
            self._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            raise OrchestratorError(
                message="Cannot register process-local terminal cleanup",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                },
            )
        # Establish the cross-process liveness record as soon as a session owns
        # a live capability, before a detached caller can observe its persisted
        # RUNNING tracker.  It is a lease/liveness marker, never authority.
        try:
            acquire_session_lock(session_id)
        except OSError as exc:
            # A registry entry without its liveness lease would let another
            # process misclassify a durable RUNNING tracker as crashed. Undo
            # the exact binding before returning the setup failure.
            self._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            raise OrchestratorError(
                message="Cannot establish process-local execution liveness lease",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(exc),
                },
            ) from exc

    def _retire_process_local_authority(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> bool:
        """Discard a terminal session's capability only when this adapter owns it."""
        retired = _retire_process_local_authority_generation(
            session_id,
            execution_id,
            self._adapter,
        )
        if retired:
            from ouroboros.orchestrator.heartbeat import (
                release_if_owned_by_current_process as release_session_lock,
            )

            release_session_lock(session_id)
        self._process_local_authorities.pop((session_id, execution_id), None)
        return retired

    def _discard_process_local_authority(
        self,
        generation: _ProcessLocalAuthorityGeneration,
    ) -> None:
        """Discard an unregistered capability after failed preparation."""
        _discard_process_local_authority_generation(generation)

    @staticmethod
    def _process_local_resume_unavailable_error(
        session_id: str,
        execution_id: str,
    ) -> OrchestratorError:
        """Return the explicit non-fallback outcome for a lost live generation."""
        return OrchestratorError(
            message=(
                "Cannot resume this process-local execution after its live authority "
                "generation is unavailable; start a new attempt."
            ),
            details={
                "session_id": session_id,
                "execution_id": execution_id,
                "resume_blocked": "process_local_resume_unavailable",
            },
        )

    @staticmethod
    def _process_local_execution_in_progress_error(
        session_id: str,
        execution_id: str,
    ) -> OrchestratorError:
        """Return the non-terminal outcome for a concurrent same-process claim."""
        return OrchestratorError(
            message="This process-local execution is already active in this process.",
            details={
                "session_id": session_id,
                "execution_id": execution_id,
                "resume_blocked": "process_local_execution_in_progress",
            },
        )

    @staticmethod
    def _process_local_authority_held_elsewhere_error(
        session_id: str,
        execution_id: str,
    ) -> OrchestratorError:
        """Reject an observer without revoking another adapter's live binding."""
        return OrchestratorError(
            message=(
                "This process-local execution is retained by another live runtime "
                "adapter or process."
            ),
            details={
                "session_id": session_id,
                "execution_id": execution_id,
                "resume_blocked": "process_local_authority_held_elsewhere",
            },
        )

    async def _mark_process_local_resume_unavailable(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> OrchestratorError:
        """Terminally record a lost local capability without trying a fallback."""
        error = self._process_local_resume_unavailable_error(session_id, execution_id)
        last_error: object | None = None
        for attempt in range(3):
            try:
                result = await self._session_repo.mark_failed_if_active(
                    session_id,
                    error.message,
                    error.details,
                )
            except (Exception, asyncio.CancelledError) as exc:
                durable_status = await self._reconcile_durable_terminal_and_cleanup(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                if durable_status is not None:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return error
                if isinstance(exc, asyncio.CancelledError):
                    self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
                        execution_id=execution_id,
                        status=SessionStatus.FAILED,
                        error_message=error.message,
                        error_details=dict(error.details),
                    )
                    self._preserve_process_local_owner_for_retry(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    raise
                last_error = exc
            else:
                if result.is_ok:
                    if not result.value:
                        log.info(
                            "orchestrator.runner.process_local_resume_terminal_already_persisted",
                            session_id=session_id,
                            execution_id=execution_id,
                        )
                    self._retire_process_local_authority(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    await clear_cancellation(session_id)
                    return error
                last_error = result.error
            log.warning(
                "orchestrator.runner.process_local_resume_terminal_mark_retry",
                session_id=session_id,
                execution_id=execution_id,
                attempt=attempt + 1,
                error=str(last_error),
            )
            if attempt < 2:
                await asyncio.sleep(0.05 * (2**attempt))
        self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
            execution_id=execution_id,
            status=SessionStatus.FAILED,
            error_message=error.message,
            error_details=dict(error.details),
        )
        return OrchestratorError(
            message="Failed to persist lost process-local authority terminal state",
            details={
                "session_id": session_id,
                "execution_id": execution_id,
                "requested_status": SessionStatus.FAILED.value,
                "cause": str(last_error),
                "resume_blocked": "terminal_persistence_pending",
                "terminal_persistence_pending": True,
            },
        )

    async def _mark_preparation_failed_best_effort(
        self,
        *,
        tracker: SessionTracker,
        message: str,
        details: Mapping[str, Any],
    ) -> str | None:
        """Record a post-start preparation failure without masking cleanup.

        ``create_session`` has already written a RUNNING durable tracker by
        the time initial progress is persisted.  If that second write fails,
        retry the terminal write briefly. If persistence remains unavailable,
        the caller preserves the process-local capability and lease instead of
        manufacturing an unowned durable RUNNING session.
        """
        last_error: object | None = None
        for attempt in range(3):
            try:
                result = await self._session_repo.mark_failed(
                    tracker.session_id,
                    message,
                    dict(details),
                )
            except (Exception, asyncio.CancelledError) as exc:
                durable_status = await self._reconcile_durable_terminal_and_cleanup(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
                if durable_status is not None:
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    return None
                if isinstance(exc, asyncio.CancelledError):
                    self._pending_lifecycle_intents[tracker.session_id] = _PendingLifecycleIntent(
                        execution_id=tracker.execution_id,
                        status=SessionStatus.FAILED,
                        error_message=message,
                        error_details=dict(details),
                        messages_processed=tracker.messages_processed,
                    )
                    self._preserve_process_local_owner_for_retry(
                        session_id=tracker.session_id,
                        execution_id=tracker.execution_id,
                    )
                    raise
                last_error = exc
            else:
                if result.is_ok:
                    self._pending_lifecycle_intents.pop(tracker.session_id, None)
                    return None
                last_error = result.error
            log.warning(
                "orchestrator.runner.prepare_terminal_mark_retry",
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                attempt=attempt + 1,
                error=str(last_error),
            )
            if attempt < 2:
                await asyncio.sleep(0.05 * (2**attempt))
        self._pending_lifecycle_intents[tracker.session_id] = _PendingLifecycleIntent(
            execution_id=tracker.execution_id,
            status=SessionStatus.FAILED,
            error_message=message,
            error_details=dict(details),
            messages_processed=tracker.messages_processed,
        )
        return str(last_error)

    async def _reconcile_session_publication_interruption(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> bool:
        """Resolve an interrupted or failed durable session-start append.

        ``create_session`` may commit ``session.started`` and still be
        interrupted before returning its tracker.  Never retire the early
        process-local owner from the exception alone: reconstruct under
        shielding, terminalize a proven active publication, or retain the
        exact owner when persistence cannot be established.
        """

        async def _reconcile() -> bool:
            try:
                reconstructed = await self._session_repo.reconstruct_session(session_id)
            except Exception:
                reconstructed = None

            if reconstructed is not None and reconstructed.is_err:
                reconstruction_message = getattr(
                    reconstructed.error,
                    "message",
                    str(reconstructed.error),
                )
                if reconstruction_message.startswith(("No events found", "No start event found")):
                    self._retire_process_local_authority(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
            elif reconstructed is not None and reconstructed.is_ok:
                tracker = reconstructed.value
                if tracker.session_id == session_id and tracker.execution_id == execution_id:
                    if tracker.status in {
                        SessionStatus.COMPLETED,
                        SessionStatus.FAILED,
                        SessionStatus.CANCELLED,
                    }:
                        self._retire_process_local_authority(
                            session_id=session_id,
                            execution_id=execution_id,
                        )
                        await clear_cancellation(session_id)
                    else:
                        terminal_mark_error = await self._mark_preparation_failed_best_effort(
                            tracker=tracker,
                            message="Session preparation was cancelled after durable publication",
                            details={
                                "session_id": session_id,
                                "execution_id": execution_id,
                                "cause": "CancelledError",
                            },
                        )
                        if terminal_mark_error is None:
                            self._retire_process_local_authority(
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                            await clear_cancellation(session_id)
                        else:
                            log.warning(
                                "orchestrator.runner.create_session_cancel_terminal_pending",
                                session_id=session_id,
                                execution_id=execution_id,
                                error=terminal_mark_error,
                            )
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)
            return (session_id, execution_id) in self._process_local_authorities

        return bool(await _await_process_local_cleanup(_reconcile()))

    async def _persist_session_terminal_status(
        self,
        *,
        session_id: str,
        execution_id: str,
        requested_status: SessionStatus,
        summary: dict[str, Any] | None = None,
        error_message: str | None = None,
        error_details: dict[str, Any] | None = None,
        error_type: str | None = None,
        messages_processed: int = 0,
        cancelled_by: str = "runner",
    ) -> SessionStatus:
        """Persist one terminal winner and return the authoritative status.

        Session lifecycle is the durable source of truth. Execution-terminal
        events are projections and must be emitted only after this CAS has a
        winner, otherwise completion and public cancellation can produce
        contradictory terminal streams.
        """
        intent = _PendingLifecycleIntent(
            execution_id=execution_id,
            status=requested_status,
            summary=summary,
            error_message=error_message,
            error_details=error_details,
            error_type=error_type,
            messages_processed=messages_processed,
            cancelled_by=cancelled_by,
        )
        result: Any = None
        last_error: object | None = None
        for attempt in range(3):
            try:
                if requested_status is SessionStatus.COMPLETED:
                    result = await self._session_repo.mark_completed(
                        session_id,
                        summary,
                        messages_processed=messages_processed,
                    )
                elif requested_status is SessionStatus.FAILED:
                    result = await self._session_repo.mark_failed(
                        session_id,
                        error_message or "Execution failed",
                        error_details,
                        error_type=error_type,
                        messages_processed=messages_processed,
                    )
                elif requested_status is SessionStatus.CANCELLED:
                    result = await self._session_repo.mark_cancelled(
                        session_id,
                        reason=error_message or "Execution cancelled",
                        cancelled_by=cancelled_by,
                    )
                else:
                    raise ValueError(
                        f"Unsupported terminal session status: {requested_status.value}"
                    )
            except Exception as exc:
                last_error = exc
            else:
                if result.is_ok:
                    break
                last_error = result.error
            log.warning(
                "orchestrator.runner.terminal_persistence_retry",
                session_id=session_id,
                requested_status=requested_status.value,
                attempt=attempt + 1,
                error=str(last_error),
            )
            if attempt < 2:
                await asyncio.sleep(0.05 * (2**attempt))
        else:
            self._pending_lifecycle_intents[session_id] = intent
            raise OrchestratorError(
                message=f"Failed to persist terminal session status: {requested_status.value}",
                details={
                    "session_id": session_id,
                    "requested_status": requested_status.value,
                    "cause": str(last_error),
                    "resume_blocked": "terminal_persistence_pending",
                    "terminal_persistence_pending": True,
                },
            )

        if result.value is not False:
            self._pending_lifecycle_intents.pop(session_id, None)
            return requested_status

        try:
            winner = await self._session_repo.reconstruct_session(session_id)
        except Exception as exc:
            self._pending_lifecycle_intents[session_id] = intent
            raise OrchestratorError(
                message="Terminal session transition lost its CAS without a readable winner",
                details={
                    "session_id": session_id,
                    "requested_status": requested_status.value,
                    "cause": str(exc),
                    "resume_blocked": "terminal_persistence_pending",
                    "terminal_persistence_pending": True,
                },
            ) from exc
        terminal_statuses = {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }
        if winner.is_ok and winner.value.status in terminal_statuses:
            self._pending_lifecycle_intents.pop(session_id, None)
            log.info(
                "orchestrator.runner.terminal_transition_preserved",
                session_id=session_id,
                requested_status=requested_status.value,
                durable_status=winner.value.status.value,
            )
            return winner.value.status
        self._pending_lifecycle_intents[session_id] = intent
        raise OrchestratorError(
            message="Terminal session transition lost its CAS without a readable winner",
            details={
                "session_id": session_id,
                "requested_status": requested_status.value,
                **({"cause": str(winner.error)} if winner.is_err else {}),
                "resume_blocked": "terminal_persistence_pending",
                "terminal_persistence_pending": True,
            },
        )

    async def _retry_pending_lifecycle_intent(
        self,
        tracker: SessionTracker,
    ) -> Result[OrchestratorResult, OrchestratorError] | None:
        """Replay an exact owner's retained terminal or pause transition.

        Persistence-pending is not a normal RUNNING resume. The retained
        process-local runner must first reclaim its generation and retry the
        original transition with its original payload. Only after that intent
        is durably resolved may ownership be released or retired.
        """
        intent = self._pending_lifecycle_intents.get(tracker.session_id)
        if intent is None:
            return None
        if intent.execution_id != tracker.execution_id:
            return Result.err(
                OrchestratorError(
                    message="Pending lifecycle intent does not match the durable execution",
                    details={
                        "session_id": tracker.session_id,
                        "execution_id": tracker.execution_id,
                        "pending_execution_id": intent.execution_id,
                        "resume_blocked": "pending_lifecycle_identity_mismatch",
                    },
                )
            )

        raw_contract = tracker.progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)
        if not isinstance(raw_contract, Mapping):
            return Result.err(
                self._process_local_resume_unavailable_error(
                    tracker.session_id,
                    tracker.execution_id,
                )
            )
        generation, already_claimed = self._claim_process_local_authority_generation(
            tracker.session_id,
            tracker.execution_id,
            raw_contract,
        )
        if already_claimed:
            return Result.err(
                self._process_local_execution_in_progress_error(
                    tracker.session_id,
                    tracker.execution_id,
                )
            )
        if generation is None:
            if self._process_local_authority_held_elsewhere(
                tracker.session_id,
                tracker.execution_id,
                raw_contract,
            ):
                return Result.err(
                    self._process_local_authority_held_elsewhere_error(
                        tracker.session_id,
                        tracker.execution_id,
                    )
                )
            return Result.err(
                self._process_local_resume_unavailable_error(
                    tracker.session_id,
                    tracker.execution_id,
                )
            )

        try:
            self._register_session(tracker.execution_id, tracker.session_id)
        except Exception as exc:
            if intent.status is SessionStatus.PAUSED:
                return self._pause_persistence_pending_result(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    cause=exc,
                )
            return self._terminal_persistence_pending_result(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                requested_status=intent.status,
                cause=exc,
            )

        resolved_status: SessionStatus
        try:
            if intent.status is SessionStatus.PAUSED:
                pause = intent.pause
                if pause is None:
                    raise OrchestratorError(
                        message="Pending PAUSED intent is missing its replay payload",
                        details={
                            "session_id": tracker.session_id,
                            "execution_id": tracker.execution_id,
                            "resume_blocked": "pending_lifecycle_payload_missing",
                        },
                    )
                pause_result = await self._session_repo.mark_paused(
                    tracker.session_id,
                    reason=pause.reason,
                    resume_hint=pause.resume_hint,
                    pause_seconds=pause.pause_seconds,
                    resume_after=pause.resume_after,
                    pause_kind=pause.pause_kind,
                )
                resolved, pending = await self._resolve_pause_publication(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    pause_result=pause_result,
                    pause=pause,
                )
                if pending is not None:
                    return pending
                assert resolved is not None
                resolved_status = resolved
            else:
                resolved_status = await self._persist_session_terminal_status(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    requested_status=intent.status,
                    summary=intent.summary,
                    error_message=intent.error_message,
                    error_details=intent.error_details,
                    error_type=intent.error_type,
                    messages_processed=intent.messages_processed,
                    cancelled_by=intent.cancelled_by,
                )
        except asyncio.CancelledError:
            if (
                await self._reconcile_durable_terminal_and_cleanup(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
                is None
            ):
                self._preserve_process_local_owner_for_retry(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
            raise
        except BaseException as exc:
            pending = self._terminal_persistence_pending_from_error(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                error=exc,
            )
            if pending is not None:
                return pending
            if intent.status is SessionStatus.PAUSED:
                return self._pause_persistence_pending_result(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    cause=exc,
                )
            return self._terminal_persistence_pending_result(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                requested_status=intent.status,
                cause=exc,
            )

        pause = intent.pause if resolved_status is SessionStatus.PAUSED else None
        final_message = (
            pause.reason
            if pause is not None
            else (
                intent.error_message
                or f"Pending {resolved_status.value} lifecycle transition persisted"
            )
        )
        terminal_event = create_execution_terminal_event(
            execution_id=tracker.execution_id,
            session_id=tracker.session_id,
            status=resolved_status.value,
            summary=intent.summary if resolved_status is SessionStatus.COMPLETED else None,
            error_message=(
                final_message
                if resolved_status not in {SessionStatus.COMPLETED, SessionStatus.PAUSED}
                else None
            ),
            messages_processed=intent.messages_processed,
            pause_seconds=pause.pause_seconds if pause is not None else None,
            resume_after=pause.resume_after if pause is not None else None,
            pause_kind=pause.pause_kind if pause is not None else None,
            resume_hint=pause.resume_hint if pause is not None else None,
        )
        try:
            await self._project_execution_outcome(
                execution_id=tracker.execution_id,
                session_id=tracker.session_id,
                terminal_status=resolved_status.value,
                terminal_event=terminal_event,
            )
        except Exception:
            log.exception(
                "orchestrator.runner.pending_lifecycle_projection_failed",
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                durable_status=resolved_status.value,
            )
        finally:
            if resolved_status is SessionStatus.PAUSED:
                self._release_process_local_authority(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
                self._unregister_session(
                    tracker.execution_id,
                    tracker.session_id,
                    release_liveness_lease=False,
                )
            else:
                self._retire_process_local_authority(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
                self._unregister_session(tracker.execution_id, tracker.session_id)
                await clear_cancellation(tracker.session_id)
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)

        self._pending_lifecycle_intents.pop(tracker.session_id, None)
        return Result.ok(
            OrchestratorResult(
                success=resolved_status is SessionStatus.COMPLETED,
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                summary={
                    **(intent.summary or {}),
                    "replayed_pending_lifecycle": resolved_status.value,
                },
                messages_processed=intent.messages_processed,
                final_message=final_message,
                duration_seconds=0.0,
            )
        )

    def _validate_resume_handle_execution_identity(
        self,
        runtime_handle: RuntimeHandle | None,
    ) -> None:
        """Reject persisted handle selectors that were not part of the start contract."""
        raw_contract = self._execution_contract
        if not isinstance(raw_contract, Mapping):
            raise OrchestratorError(
                message="Cannot resume without a restored execution contract",
                details={"invalid": "execution_contract"},
            )
        raw_routing = raw_contract.get("model_routing")
        raw_runtime_execution = (
            raw_routing.get("runtime_execution") if isinstance(raw_routing, Mapping) else None
        )
        raw_identity = (
            raw_runtime_execution.get("identity")
            if isinstance(raw_runtime_execution, Mapping)
            and raw_runtime_execution.get("observed") is True
            else None
        )
        if not isinstance(raw_identity, Mapping):
            return
        persisted_selector = raw_identity.get("resume_handle_selector")
        if persisted_selector is None:
            # Only runtimes that explicitly persist a root-handle selector
            # contract participate in this check. Codex does; CLI subclasses
            # that merely inherit its process machinery do not.
            return

        provider_descriptor = inspect.getattr_static(
            type(self._adapter),
            "resume_handle_execution_identity_contract",
            None,
        )
        if provider_descriptor is None:
            raise OrchestratorError(
                message="Cannot validate the persisted runtime resume selector",
                details={"runtime_backend": self._runtime_backend_contract()},
            )

        provider = object.__getattribute__(
            self._adapter,
            "resume_handle_execution_identity_contract",
        )
        try:
            current_selector = provider(runtime_handle)
        except Exception as exc:
            raise OrchestratorError(
                message="Cannot resume with invalid runtime selector metadata",
                details={"cause": str(exc)},
            ) from exc
        if persisted_selector != current_selector:
            raise OrchestratorError(
                message="Cannot resume with a different runtime handle selector",
                details={
                    "persisted_selector": persisted_selector,
                    "current_selector": current_selector,
                    "hint": "Restore the original runtime handle metadata or start a new session.",
                },
            )

    @staticmethod
    def _validate_bound_runtime_resume_identity(
        progress: Mapping[str, Any],
        runtime_handle: RuntimeHandle | None,
    ) -> None:
        """Bind resume to the first stable backend session id in event history."""
        persisted_identity = progress.get(SESSION_RUNTIME_IDENTITY_PROGRESS_KEY)
        if persisted_identity is None:
            return
        if (
            not isinstance(persisted_identity, Mapping)
            or persisted_identity.get("status") != "bound"
        ):
            raise OrchestratorError(
                message="Cannot resume with conflicting runtime session identity",
                details={"persisted_runtime_identity": persisted_identity},
            )
        current_identity = runtime_resume_identity_from_payload(
            runtime_handle.to_persisted_dict() if runtime_handle is not None else None
        )
        if current_identity != dict(persisted_identity):
            raise OrchestratorError(
                message="Cannot resume a different backend session",
                details={
                    "persisted_runtime_identity": dict(persisted_identity),
                    "current_runtime_identity": current_identity,
                    "hint": "Restore the original runtime session id or start a new session.",
                },
            )

    def _validate_runtime_handle_backend(
        self,
        runtime_handle: RuntimeHandle | None,
    ) -> None:
        """Require every persisted handle to belong to the contracted runtime."""
        if runtime_handle is None:
            return
        expected_backend = self._runtime_backend_contract()
        if runtime_handle.backend != expected_backend:
            raise OrchestratorError(
                message="Cannot resume with a runtime handle from a different backend",
                details={
                    "persisted_handle_backend": runtime_handle.backend,
                    "execution_runtime_backend": expected_backend,
                    "hint": "Restore the original runtime handle or start a new session.",
                },
            )

    @staticmethod
    def _force_adapter_permission_mode(adapter: AgentRuntime) -> str:
        """Force the runtime's native equivalent of bypassPermissions."""
        normalized = FORCED_EXECUTION_PERMISSION_MODE
        resolver_descriptor = inspect.getattr_static(
            type(adapter),
            "_resolve_permission_mode",
            None,
        )
        if resolver_descriptor is not None:
            resolver = object.__getattribute__(adapter, "_resolve_permission_mode")
            resolved = resolver(FORCED_EXECUTION_PERMISSION_MODE)
            if not isinstance(resolved, str) or not resolved.strip():
                raise ValueError("Runtime returned an invalid bypass permission mode")
            normalized = resolved.strip()
        try:
            object.__setattr__(adapter, "_permission_mode", normalized)
        except Exception as exc:
            raise ValueError("Runtime permission mode cannot be forced to bypass") from exc
        return normalized

    def _force_runtime_handle_permission(
        self,
        runtime_handle: RuntimeHandle | None,
    ) -> RuntimeHandle | None:
        """Overwrite persisted approval state with the mandatory bypass mode."""
        if runtime_handle is None:
            return None
        return replace(runtime_handle, approval_mode=self._forced_permission_mode)

    def _runtime_backend_contract(self) -> str | None:
        """Return the concrete runtime backend that owns this resumable run."""
        runtime_backend = getattr(self._adapter, "runtime_backend", None)
        if not isinstance(runtime_backend, str) or not runtime_backend.strip():
            return None
        return runtime_backend.strip()

    def _llm_backend_contract(self) -> str | None:
        """Return the LLM backend used by analysis and runtime-adjacent calls."""
        llm_backend = getattr(self._adapter, "llm_backend", None)
        if not isinstance(llm_backend, str) or not llm_backend.strip():
            return None
        return llm_backend.strip()

    def _permission_mode_contract(self) -> dict[str, Any]:
        """Return the normalized runtime authority level used for this run."""
        permission_mode = self._forced_permission_mode
        if not isinstance(permission_mode, str) or not permission_mode.strip():
            return {"observed": False}
        return {"observed": True, "mode": permission_mode.strip()}

    @staticmethod
    def _valid_permission_mode_contract(value: object) -> bool:
        if not isinstance(value, Mapping) or value.get("observed") is not True:
            return False
        mode = value.get("mode")
        return set(value) == {"observed", "mode"} and isinstance(mode, str) and bool(mode.strip())

    def _guidance_root(self, guidance_ids: tuple[str, ...]) -> Path:
        """Return the project root used for declared execution guidance."""
        effective_cwd = self._effective_cwd()
        if effective_cwd:
            return Path(effective_cwd)
        if not guidance_ids:
            return Path(".")
        raise OrchestratorError(
            message="Cannot resolve project guidance without an execution working directory",
            details={"guidance_ids": list(guidance_ids)},
        )

    def _resolve_guidance_bundle(
        self,
        guidance_ids: tuple[str, ...],
        *,
        expected_metadata: Mapping[str, Any] | None = None,
    ) -> ExecutionGuidanceBundle:
        """Resolve declared guidance and optionally enforce persisted identity."""
        try:
            bundle = resolve_execution_guidance(self._guidance_root(guidance_ids), guidance_ids)
        except ConfigError as exc:
            raise OrchestratorError(
                message="Cannot resolve declared project execution guidance",
                details={"cause": exc.message, **exc.details},
            ) from exc

        if expected_metadata is not None and bundle.to_metadata() != dict(expected_metadata):
            raise OrchestratorError(
                message="Cannot resume because declared project guidance changed",
                details={
                    "persisted_guidance": dict(expected_metadata),
                    "current_guidance": bundle.to_metadata(),
                    "hint": "Restore the original GUIDANCE.md files or start a new session.",
                },
            )
        return bundle

    @staticmethod
    def _guidance_contract(bundle: ExecutionGuidanceBundle) -> dict[str, Any]:
        return {
            "mode": "declared" if bundle.refs else "disabled",
            "provenance_scope": "ouroboros_declared_guidance_only",
            **bundle.to_metadata(),
        }

    def _ensure_new_run_guidance(self) -> ExecutionGuidanceBundle:
        if self._execution_guidance is None:
            self._execution_guidance = self._resolve_guidance_bundle(self._project_guidance_ids)
        return self._execution_guidance

    def _restore_guidance_contract(self, raw_contract: Mapping[str, Any]) -> None:
        """Restore persisted guidance refs without consulting the current allowlist."""
        raw_guidance = raw_contract.get("guidance")
        if raw_guidance is None:
            self._execution_guidance = self._resolve_guidance_bundle(())
            return
        if not isinstance(raw_guidance, Mapping):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "guidance"},
            )

        mode = raw_guidance.get("mode")
        provenance_scope = raw_guidance.get("provenance_scope")
        items = raw_guidance.get("items")
        if (
            mode not in {"disabled", "declared"}
            or provenance_scope != "ouroboros_declared_guidance_only"
            or not isinstance(items, list)
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "guidance metadata"},
            )

        guidance_ids: list[str] = []
        for item in items:
            if not isinstance(item, Mapping):
                raise OrchestratorError(
                    message="Cannot resume with an invalid execution contract",
                    details={"invalid": "guidance item"},
                )
            guidance_id = item.get("id")
            if not isinstance(guidance_id, str) or not guidance_id.strip():
                raise OrchestratorError(
                    message="Cannot resume with an invalid execution contract",
                    details={"invalid": "guidance id"},
                )
            guidance_ids.append(guidance_id.strip())
        if (mode == "disabled") != (not guidance_ids):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "guidance mode"},
            )

        expected_metadata = {
            key: value
            for key, value in raw_guidance.items()
            if key not in {"mode", "provenance_scope"}
        }
        self._execution_guidance = self._resolve_guidance_bundle(
            tuple(guidance_ids),
            expected_metadata=expected_metadata,
        )

    def _build_execution_contract(
        self,
        *,
        seed: Seed | None = None,
        seed_fingerprint: str | None = None,
        authority_generation: _ProcessLocalAuthorityGeneration | None = None,
    ) -> dict[str, Any]:
        """Build the durable resolved inputs shared by resume and proof cohorting."""
        from ouroboros.orchestrator.model_routing import serialize_model_router

        guidance_bundle = self._ensure_new_run_guidance()
        routing_contract = serialize_model_router(self._model_router)
        routing_contract["constructor_model"] = self._constructor_model_contract()
        routing_contract["runtime_execution"] = self._runtime_execution_identity_contract()
        routing_contract["runtime_backend"] = self._runtime_backend_contract()
        routing_contract["llm_backend"] = self._llm_backend_contract()
        routing_contract["permission_mode"] = self._permission_mode_contract()
        proof_contract: dict[str, Any] = {
            "protocol_version": FRUGALITY_PROOF_PROTOCOL_VERSION,
            "routing_fingerprint": self._routing_fingerprint(routing_contract),
        }
        workspace_identity = self._proof_workspace_identity()
        if workspace_identity is not None:
            proof_contract.update(workspace_identity)
        resolved_seed_fingerprint = seed_fingerprint
        if resolved_seed_fingerprint is None and seed is not None:
            resolved_seed_fingerprint = self._seed_semantics_fingerprint(seed)
        if resolved_seed_fingerprint is not None:
            proof_contract["seed_fingerprint"] = resolved_seed_fingerprint
        if authority_generation is None:
            # Diagnostics and contract-validation callers need attribution
            # shape, not a live capability. Do not mint a registry issuance
            # here: there is no session lifecycle that could register and
            # retire it. The random correlation remains evidence-only, and
            # cannot be claimed or registered without an explicit generation.
            authority_contract: dict[str, object] = {
                "version": 1,
                "scope": "process_local",
                "correlation_id": uuid4().hex,
            }
        else:
            authority_contract = self._process_local_authority_contract(authority_generation)
        return {
            "version": EXECUTION_CONTRACT_VERSION,
            "foundation_a_authority": authority_contract,
            "execution_preferences": self._execution_preferences.to_contract_data(),
            "model_routing": routing_contract,
            "frugality_proof": proof_contract,
            "guidance": self._guidance_contract(guidance_bundle),
            "resume": {
                "workspace": self._resume_workspace_identity(),
            },
        }

    async def _emit_run_configuration_resolved(
        self,
        *,
        execution_id: str,
        session_id: str,
    ) -> None:
        """Persist the user-facing run configuration before any AC dispatch."""
        from ouroboros.config import get_cross_harness_redispatch_enabled
        from ouroboros.events.base import BaseEvent

        starting_tier = self._model_router.base_tier if self._model_router else None
        starting_model = (
            self._model_router.tier_models.get(starting_tier)
            if self._model_router is not None and starting_tier is not None
            else None
        )
        await self._event_store.append(
            BaseEvent(
                type="execution.run.configuration_resolved",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "efficiency_mode": self._execution_preferences.efficiency_mode.value,
                    "frugality_assurance": (self._execution_preferences.frugality_assurance.value),
                    "primary_runtime_backend": getattr(self._adapter, "runtime_backend", "unknown"),
                    "primary_harness_label": type(self._adapter).__name__[:80],
                    "model_routing_enabled": self._model_router is not None,
                    "requested_model_tier": self._requested_model_tier,
                    "starting_model_tier": starting_tier,
                    "starting_model": starting_model,
                    "progressive_escalation_enabled": self._model_router is not None,
                    "alternate_harness_enabled": get_cross_harness_redispatch_enabled(),
                    "strict_baseline_authorized": (
                        self._execution_preferences.strict_baseline_authorized
                    ),
                    "shadow_replay_enabled": self._shadow_replay_enabled,
                },
            )
        )

    async def _emit_execution_plan_created(
        self,
        *,
        seed: Seed,
        execution_id: str,
        session_id: str,
        execution_plan: Any,
    ) -> None:
        """Persist one bounded whole-run plan before the first level starts."""
        from ouroboros.events.base import BaseEvent

        levels: list[dict[str, Any]] = []
        for stage in execution_plan.stages:
            indices = [
                index for index in stage.ac_indices if 0 <= index < len(seed.acceptance_criteria)
            ]
            levels.append(
                {
                    "level": stage.stage_number,
                    "ac_indices": indices,
                    "semantic_ac_keys": [
                        seed.acceptance_criteria[index].semantic_ac_key for index in indices
                    ],
                    "ac_summaries": [
                        " ".join(ac_text(seed.acceptance_criteria[index]).split())[:160]
                        for index in indices
                    ],
                    "depends_on_levels": [dependency + 1 for dependency in stage.depends_on_stages],
                }
            )
        first = levels[0] if levels else None
        await self._event_store.append(
            BaseEvent(
                type="execution.plan.created",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "total_acs": len(seed.acceptance_criteria),
                    "total_levels": execution_plan.total_stages,
                    "parallelizable": execution_plan.is_parallelizable,
                    "levels": levels,
                    "first_level": first["level"] if first is not None else None,
                    "first_ac_indices": first["ac_indices"] if first is not None else [],
                },
            )
        )

    def _validate_legacy_resume_identity(
        self,
        progress: Mapping[str, Any],
        *,
        seed: Seed | None,
    ) -> None:
        """Validate every recoverable identity field before legacy migration.

        Legacy sessions predate the versioned execution contract, but their
        authoritative start event already records the seed id/goal and runtime
        backend. ``SessionRepository`` exposes that snapshot under
        :data:`SESSION_START_IDENTITY_PROGRESS_KEY`; accepting a mismatched
        current invocation would permanently bless the wrong seed/backend when
        the migration checkpoint is written.
        """

        raw_start_identity = progress.get(SESSION_START_IDENTITY_PROGRESS_KEY)
        if raw_start_identity is not None and not isinstance(raw_start_identity, Mapping):
            raise OrchestratorError(
                message="Cannot migrate a legacy session with invalid start identity",
                details={"invalid": SESSION_START_IDENTITY_PROGRESS_KEY},
            )
        start_identity = raw_start_identity if isinstance(raw_start_identity, Mapping) else {}

        if "seed_id" in start_identity:
            persisted_seed_id = start_identity.get("seed_id")
            current_seed_id = seed.metadata.seed_id if seed is not None else None
            if (
                not isinstance(persisted_seed_id, str)
                or not persisted_seed_id.strip()
                or current_seed_id != persisted_seed_id
            ):
                raise OrchestratorError(
                    message="Cannot resume a legacy session with a different Seed identity",
                    details={
                        "persisted_seed_id": persisted_seed_id,
                        "current_seed_id": current_seed_id,
                        "hint": "Resume with the original Seed, or start a new session.",
                    },
                )

        if "seed_goal" in start_identity:
            persisted_seed_goal = start_identity.get("seed_goal")
            current_seed_goal = seed.goal if seed is not None else None
            if (
                not isinstance(persisted_seed_goal, str)
                or not persisted_seed_goal.strip()
                or current_seed_goal != persisted_seed_goal
            ):
                raise OrchestratorError(
                    message="Cannot resume a legacy session with a modified Seed goal",
                    details={
                        "persisted_seed_goal": persisted_seed_goal,
                        "current_seed_goal": current_seed_goal,
                        "hint": "Resume with the original Seed, or start a new session.",
                    },
                )

        persisted_runtime_backend: object | None = None
        if "runtime_backend" in start_identity:
            persisted_runtime_backend = start_identity.get("runtime_backend")
        elif "runtime_backend" in progress:
            # Older start events may lack the backend while runtime progress
            # still carries the backend that owns the resumable handle.
            persisted_runtime_backend = progress.get("runtime_backend")
        if persisted_runtime_backend is not None:
            current_runtime_backend = self._runtime_backend_contract()
            if (
                not isinstance(persisted_runtime_backend, str)
                or not persisted_runtime_backend.strip()
                or current_runtime_backend != persisted_runtime_backend
            ):
                raise OrchestratorError(
                    message="Cannot resume a legacy session with a different runtime backend",
                    details={
                        "persisted_runtime_backend": persisted_runtime_backend,
                        "current_runtime_backend": current_runtime_backend,
                        "hint": "Resume with the original runtime, or start a new session.",
                    },
                )

        if "llm_backend" in start_identity:
            persisted_llm_backend = start_identity.get("llm_backend")
            current_llm_backend = getattr(self._adapter, "llm_backend", None)
            if (
                not isinstance(persisted_llm_backend, str)
                or not persisted_llm_backend.strip()
                or current_llm_backend != persisted_llm_backend
            ):
                raise OrchestratorError(
                    message="Cannot resume a legacy session with a different LLM backend",
                    details={
                        "persisted_llm_backend": persisted_llm_backend,
                        "current_llm_backend": current_llm_backend,
                        "hint": "Resume with the original backend, or start a new session.",
                    },
                )

        if "workspace" in progress:
            persisted_task_workspace = TaskWorkspace.from_progress_dict(progress.get("workspace"))
            if persisted_task_workspace is None:
                raise OrchestratorError(
                    message="Cannot migrate a legacy session with invalid workspace identity",
                    details={"invalid": "workspace"},
                )
            persisted_workspace = self._task_resume_workspace_identity(persisted_task_workspace)
            active_workspace = self._resume_workspace_identity()
            if active_workspace != persisted_workspace:
                raise OrchestratorError(
                    message="Cannot resume a legacy session from a different project workspace",
                    details={
                        "persisted_workspace": persisted_workspace,
                        "current_workspace": active_workspace,
                        "hint": "Resume from the original project/workspace.",
                    },
                )
        else:
            runtime_progress = progress.get("runtime")
            if isinstance(runtime_progress, Mapping) and "cwd" in runtime_progress:
                persisted_cwd = runtime_progress.get("cwd")
                if persisted_cwd is not None:
                    current_cwd = self._effective_cwd()
                    if (
                        not isinstance(persisted_cwd, str)
                        or not persisted_cwd.strip()
                        or not isinstance(current_cwd, str)
                        or self._canonical_path(current_cwd) != self._canonical_path(persisted_cwd)
                    ):
                        raise OrchestratorError(
                            message=(
                                "Cannot resume a legacy session from a different project workspace"
                            ),
                            details={
                                "persisted_workspace": persisted_cwd,
                                "current_workspace": current_cwd,
                                "hint": "Resume from the original project/workspace.",
                            },
                        )

    def _restore_execution_contract(
        self,
        progress: Mapping[str, Any],
        *,
        seed: Seed | None = None,
        authority_generation: _ProcessLocalAuthorityGeneration | None = None,
    ) -> bool:
        """Restore the persisted router unless this invocation explicitly overrides it.

        Returns whether a replacement contract (an explicit override or one-time
        legacy migration) should be checkpointed for subsequent resumes. A present
        malformed contract blocks resume; it is never reinterpreted as a legacy
        session or allowed to change models silently.
        """
        if EXECUTION_CONTRACT_PROGRESS_KEY not in progress:
            self._validate_legacy_resume_identity(progress, seed=seed)
            self._execution_guidance = self._resolve_guidance_bundle(())
            self._execution_contract = self._build_execution_contract(
                seed=seed,
                authority_generation=authority_generation,
            )
            # One unavoidable recomputation migrates a legacy session. Persist the
            # resolved contract now so every later resume restores this exact policy
            # instead of drifting again with each environment/config change.
            return True
        raw_contract = progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)

        raw_version = raw_contract.get("version") if isinstance(raw_contract, Mapping) else None
        if (
            not isinstance(raw_contract, Mapping)
            or isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version != EXECUTION_CONTRACT_VERSION
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"contract_version": raw_version},
            )

        raw_proof = raw_contract.get("frugality_proof")
        raw_routing = raw_contract.get("model_routing")
        raw_resume = raw_contract.get("resume")
        raw_preferences = raw_contract.get("execution_preferences")
        raw_authority = raw_contract.get("foundation_a_authority")
        if (
            not isinstance(raw_proof, Mapping)
            or not isinstance(raw_routing, Mapping)
            or not isinstance(raw_resume, Mapping)
            or not valid_process_local_authority_contract(raw_authority)
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={
                    "missing": "frugality_proof, model_routing, resume, or foundation_a_authority"
                },
            )

        self._restore_guidance_contract(raw_contract)

        protocol_version = raw_proof.get("protocol_version")
        persisted_project_root = raw_proof.get("project_root")
        persisted_workspace_path = raw_proof.get("workspace_path")
        persisted_routing_fingerprint = raw_proof.get("routing_fingerprint")
        persisted_seed_fingerprint = raw_proof.get("seed_fingerprint")
        persisted_constructor_model = raw_routing.get("constructor_model")
        persisted_runtime_execution = raw_routing.get("runtime_execution")
        persisted_runtime_backend = raw_routing.get("runtime_backend")
        persisted_llm_backend = raw_routing.get("llm_backend")
        persisted_permission_mode = raw_routing.get("permission_mode")
        persisted_resume_workspace = raw_resume.get("workspace")
        valid_seed_fingerprint = (
            isinstance(persisted_seed_fingerprint, str)
            and len(persisted_seed_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in persisted_seed_fingerprint)
        )
        if (
            isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or protocol_version != FRUGALITY_PROOF_PROTOCOL_VERSION
            or not isinstance(persisted_project_root, str)
            or not persisted_project_root.strip()
            or not isinstance(persisted_workspace_path, str)
            or not persisted_workspace_path.strip()
            or not isinstance(persisted_routing_fingerprint, str)
            or persisted_routing_fingerprint != self._routing_fingerprint(raw_routing)
            or (seed is not None and not valid_seed_fingerprint)
            or not self._valid_constructor_model_contract(persisted_constructor_model)
            or not self._valid_runtime_execution_identity_contract(persisted_runtime_execution)
            or not isinstance(persisted_runtime_backend, str)
            or not persisted_runtime_backend.strip()
            or not isinstance(persisted_llm_backend, str)
            or not persisted_llm_backend.strip()
            or not self._valid_permission_mode_contract(persisted_permission_mode)
            or not isinstance(persisted_resume_workspace, Mapping)
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "proof identity"},
            )

        persisted_preferences = execution_preferences_from_contract(raw_preferences)
        preferences_migrated = persisted_preferences is None and raw_preferences is None
        if persisted_preferences is None:
            if not preferences_migrated:
                raise OrchestratorError(
                    message="Cannot resume with invalid execution preferences",
                    details={"invalid": "execution_preferences"},
                )
            persisted_preferences = resolve_execution_preferences(None, None)
        if (
            self._execution_preferences_override_explicit
            and self._execution_preferences != persisted_preferences
        ):
            raise OrchestratorError(
                message="Cannot change efficiency or frugality preferences on resume",
                details={
                    "persisted_preferences": persisted_preferences.to_contract_data(),
                    "requested_preferences": self._execution_preferences.to_contract_data(),
                    "hint": "Start a new successor execution for an intentional change.",
                },
            )

        current_seed_fingerprint = (
            self._seed_semantics_fingerprint(seed) if seed is not None else None
        )
        active_workspace = self._proof_workspace_identity()
        persisted_workspace = {
            "project_root": persisted_project_root,
            "workspace_path": persisted_workspace_path,
        }
        if active_workspace != persisted_workspace:
            raise OrchestratorError(
                message="Cannot resume from a different project workspace",
                details={
                    "persisted_workspace": persisted_workspace,
                    "current_workspace": active_workspace,
                    "hint": "Resume from the original project/workspace.",
                },
            )
        active_resume_workspace = self._resume_workspace_identity()
        if active_resume_workspace != dict(persisted_resume_workspace):
            raise OrchestratorError(
                message="Cannot resume from a different execution workspace",
                details={
                    "persisted_workspace": dict(persisted_resume_workspace),
                    "current_workspace": active_resume_workspace,
                    "hint": "Resume from the exact original worktree and branch.",
                },
            )
        current_runtime_backend = self._runtime_backend_contract()
        if current_runtime_backend != persisted_runtime_backend:
            raise OrchestratorError(
                message="Cannot resume with a different runtime backend",
                details={
                    "persisted_runtime_backend": persisted_runtime_backend,
                    "current_runtime_backend": current_runtime_backend,
                    "hint": "Resume with the original runtime, or start a new session.",
                },
            )
        current_llm_backend = self._llm_backend_contract()
        if current_llm_backend != persisted_llm_backend:
            raise OrchestratorError(
                message="Cannot resume with a different LLM backend",
                details={
                    "persisted_llm_backend": persisted_llm_backend,
                    "current_llm_backend": current_llm_backend,
                    "hint": "Restore the original LLM backend or start a new session.",
                },
            )
        current_permission_mode = self._permission_mode_contract()
        if current_permission_mode != persisted_permission_mode:
            raise OrchestratorError(
                message="Cannot resume with a different permission mode",
                details={
                    "persisted_permission_mode": dict(persisted_permission_mode),
                    "current_permission_mode": current_permission_mode,
                    "hint": "Restore the original permission mode or start a new session.",
                },
            )
        if (
            valid_seed_fingerprint
            and current_seed_fingerprint is not None
            and persisted_seed_fingerprint != current_seed_fingerprint
        ):
            raise OrchestratorError(
                message="Cannot resume with a modified Seed",
                details={
                    "persisted_seed_fingerprint": persisted_seed_fingerprint,
                    "current_seed_fingerprint": current_seed_fingerprint,
                    "hint": "Start a new session for changed goals, constraints, or ACs.",
                },
            )
        current_constructor_model = self._constructor_model_contract()
        if persisted_constructor_model != current_constructor_model:
            raise OrchestratorError(
                message="Cannot resume with a different constructor model",
                details={
                    "persisted_constructor_model": dict(persisted_constructor_model),
                    "current_constructor_model": current_constructor_model,
                    "hint": (
                        "Resume with the original runtime model, or start a new session "
                        "for an intentional model change."
                    ),
                },
            )
        current_runtime_execution = self._runtime_execution_identity_contract()
        if persisted_runtime_execution != current_runtime_execution:
            raise OrchestratorError(
                message="Cannot resume with a different runtime execution profile",
                details={
                    "persisted_runtime_execution": dict(persisted_runtime_execution),
                    "current_runtime_execution": current_runtime_execution,
                    "hint": (
                        "Restore the original runtime/model profile, or start a new "
                        "session for an intentional execution-profile change."
                    ),
                },
            )

        from ouroboros.orchestrator.model_routing import deserialize_model_router

        recognized, restored_router = deserialize_model_router(raw_routing)
        if not recognized:
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "model_routing"},
            )

        if (
            restored_router is not None
            and persisted_runtime_backend != restored_router.runtime_backend
        ):
            raise OrchestratorError(
                message="Cannot resume with an inconsistent runtime backend contract",
                details={
                    "persisted_runtime_backend": restored_router.runtime_backend,
                    "execution_runtime_backend": persisted_runtime_backend,
                },
            )
        constructor_model_value = persisted_constructor_model.get("model")
        effective_model_observed = self._runtime_execution_proves_effective_model(
            persisted_runtime_execution
        )
        process_local_authority = valid_process_local_authority_contract(
            raw_contract.get("foundation_a_authority")
        )
        model_override_support = getattr(
            getattr(self._adapter, "capabilities", None),
            "model_override_support",
            ParamSupport.IGNORED,
        )
        if (
            constructor_model_value is None
            and not effective_model_observed
            and not (restored_router is not None and model_override_support is ParamSupport.NATIVE)
            and not process_local_authority
        ):
            raise OrchestratorError(
                message="Cannot resume because the effective runtime model is unverifiable",
                details={
                    "runtime_backend": persisted_runtime_backend,
                    "constructor_model": None,
                    "effective_model_observed": False,
                    "model_routing_enforced": (
                        restored_router is not None
                        and model_override_support is ParamSupport.NATIVE
                    ),
                    "hint": ("Pin the original runtime model/profile, or start a new session."),
                },
            )
        self._execution_preferences = persisted_preferences
        self._shadow_replay_enabled = self._resolved_shadow_replay_enabled()
        if self._model_routing_override_explicit:
            replacement = self._build_execution_contract(
                seed=seed,
                seed_fingerprint=(persisted_seed_fingerprint if valid_seed_fingerprint else None),
                authority_generation=authority_generation,
            )
            # Only the public resume path reaches this branch with a live,
            # registry-issued generation.  Preserve the persisted diagnostics
            # in direct contract-validation calls so those calls cannot mint a
            # replacement correlation id by accident.
            if authority_generation is None:
                replacement["foundation_a_authority"] = dict(raw_contract["foundation_a_authority"])
            self._execution_contract = replacement
            return self._execution_contract != raw_contract

        self._model_router = restored_router
        # Preserve the exact persisted proof identity alongside the restored
        # router. Recomputing it from a resumed throwaway worktree would make the
        # same execution appear to be a different experiment.
        self._execution_contract = dict(raw_contract)
        if preferences_migrated:
            self._execution_contract["execution_preferences"] = (
                persisted_preferences.to_contract_data()
            )
            return True
        return False

    @staticmethod
    def _proof_cohort_identity(
        event_data: Mapping[str, Any],
    ) -> tuple[str, str, str, int, str, str, str] | None:
        """Reject cross-run proof cohorts for Foundation A process-local runs."""
        raw_contract = event_data.get(EXECUTION_CONTRACT_PROGRESS_KEY)
        if not isinstance(raw_contract, Mapping):
            return None
        raw_version = raw_contract.get("version")
        if (
            isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version != EXECUTION_CONTRACT_VERSION
        ):
            return None
        # Foundation A's current correlation record is diagnostic attribution
        # only.  It must not form a replay, idempotency, trust, or cross-run
        # proof cohort key.  A future portable authority needs its own reviewed
        # consumer rule rather than falling through this legacy proof path.
        return None

    def _build_dependency_analyzer(self) -> DependencyAnalyzer:
        """Create a dependency analyzer wired to the active LLM backend when available.

        Legacy ``AgentRuntime`` implementations (custom runtimes, test mocks)
        predating the ``llm_backend`` Protocol addition in v0.28.6 may not
        define the property. We probe it via ``getattr`` and degrade to a
        structured-only ``DependencyAnalyzer`` when the attribute is absent,
        preserving pre-v0.28.6 behavior for downstream Protocol implementers.
        """
        from ouroboros.orchestrator.dependency_analyzer import DependencyAnalyzer

        # Legacy-compat: adapters predating the llm_backend Protocol addition
        # (v0.28.6) lack this attribute. Fall back to structured-only analysis
        # rather than raising AttributeError.
        _llm_backend_sentinel = object()
        llm_backend = getattr(self._adapter, "llm_backend", _llm_backend_sentinel)
        if llm_backend is _llm_backend_sentinel:
            log.info(
                "orchestrator.runner.dependency_analyzer.legacy_adapter_without_llm_backend",
                adapter_type=type(self._adapter).__name__,
            )
            return DependencyAnalyzer()

        backend = (
            llm_backend
            if isinstance(llm_backend, str) and llm_backend
            else (self._adapter.runtime_backend)
        )
        cli_path = getattr(self._adapter, "cli_path", None)
        resolved_cli_path = cli_path if isinstance(cli_path, str) and cli_path else None
        try:
            # ``allowed_tools=[]`` paired with ``max_turns=1``: see issue #781.
            llm_adapter = create_llm_adapter(
                backend=backend,
                permission_mode=self._forced_permission_mode,
                cli_path=resolved_cli_path,
                cwd=self._effective_cwd(),
                max_turns=1,
                allowed_tools=(
                    [] if backend_supports_tool_envelope(resolve_llm_backend(backend)) else None
                ),
            )
        except (RuntimeError, ImportError, ConnectionError, OSError, ValueError) as exc:
            log.warning(
                "orchestrator.runner.dependency_analysis_llm_unavailable",
                backend=backend,
                error=str(exc),
            )
            return DependencyAnalyzer()

        return DependencyAnalyzer(
            llm_adapter=llm_adapter,
            model=get_llm_model_for_role("dependency_analysis", backend=backend),
        )

    def _normalized_message_type(self, message: AgentMessage) -> str:
        """Collapse runtime-specific message details into shared progress categories."""
        return normalized_message_type(message)

    def _message_tool_name(self, message: AgentMessage) -> str | None:
        """Resolve the tool name from either the message envelope or message data."""
        return message_tool_name(message)

    def _message_tool_input(self, message: AgentMessage) -> dict[str, Any]:
        """Return structured tool input when present."""
        return message_tool_input(message)

    def _message_tool_input_preview(self, message: AgentMessage) -> str | None:
        """Build a compact preview string for persisted tool-call events."""
        tool_input = self._message_tool_input(message)
        if not tool_input:
            return None

        parts: list[str] = []
        for key, value in tool_input.items():
            rendered = str(value).strip()
            if rendered:
                parts.append(f"{key}: {rendered}")
        preview = ", ".join(parts)
        return preview[:100] if preview else None

    def _serialize_runtime_message_metadata(self, message: AgentMessage) -> dict[str, Any]:
        """Serialize shared runtime metadata for persisted progress/audit events."""
        projected = project_runtime_message(message)
        return dict(projected.runtime_metadata)

    def _build_progress_update(
        self,
        message: AgentMessage,
        messages_processed: int,
    ) -> dict[str, Any]:
        """Build a normalized progress payload for session persistence."""
        projected = project_runtime_message(message)
        message_type = projected.message_type
        progress: dict[str, Any] = {
            "last_message_type": message_type,
            "messages_processed": messages_processed,
            "content_preview": projected.content[:200],
        }

        runtime_handle = message.resume_handle
        progress.update(projected.runtime_metadata)

        if runtime_handle is not None:
            progress["runtime"] = runtime_handle.to_session_state_dict()
            progress["runtime_backend"] = runtime_handle.backend
            runtime_event_type = runtime_handle.metadata.get("runtime_event_type")
            if isinstance(runtime_event_type, str) and runtime_event_type:
                progress["runtime_event_type"] = runtime_event_type
            if runtime_handle.backend == "claude" and runtime_handle.native_session_id:
                progress["agent_session_id"] = runtime_handle.native_session_id
        if self._task_workspace is not None:
            progress["workspace"] = self._task_workspace.to_progress_dict()

        return progress

    def _build_progress_event(
        self,
        session_id: str,
        message: AgentMessage,
        *,
        step: int | None = None,
    ):
        """Create an enriched progress event from a normalized runtime message."""
        projected = project_runtime_message(message)
        message_type = projected.message_type
        tool_name = projected.tool_name
        event = create_progress_event(
            session_id=session_id,
            message_type=message_type,
            content_preview=projected.content,
            step=step,
            tool_name=tool_name if message_type in {"tool", "tool_result"} else None,
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
            "progress": {
                "last_message_type": message_type,
                "last_content_preview": projected.content[:200],
            },
        }
        runtime = event_data.get("runtime")
        if isinstance(runtime, dict):
            event_data["progress"]["runtime"] = runtime
        runtime_event_type = event_data.get("runtime_event_type")
        if isinstance(runtime_event_type, str) and runtime_event_type:
            event_data["progress"]["runtime_event_type"] = runtime_event_type
        thinking = event_data.get("thinking")
        if isinstance(thinking, str) and thinking:
            event_data["progress"]["thinking"] = thinking
        ac_tracking = coerce_ac_marker_update(event_data.get("ac_tracking"))
        if not ac_tracking.is_empty:
            event_data["progress"]["ac_tracking"] = ac_tracking.to_dict()
        return event.model_copy(update={"data": event_data})

    def _build_tool_called_event(
        self,
        session_id: str,
        message: AgentMessage,
    ):
        """Create an enriched tool-called event from a normalized runtime message."""
        projected = project_runtime_message(message)
        tool_name = projected.tool_name
        if tool_name is None:
            return None
        event = create_tool_called_event(
            session_id=session_id,
            tool_name=tool_name,
            tool_input_preview=self._message_tool_input_preview(message),
        )
        event_data = {
            **event.data,
            **projected.runtime_metadata,
        }
        return event.model_copy(update={"data": event_data})

    @staticmethod
    def _with_execution_node_identity(
        acceptance_criteria: list[dict[str, Any]],
        *,
        execution_id: str,
    ) -> list[dict[str, Any]]:
        """Attach canonical node identity to top-level workflow progress items."""
        enriched: list[dict[str, Any]] = []
        for order, raw_ac in enumerate(acceptance_criteria):
            ac = dict(raw_ac)
            raw_index = ac.get("index")
            ac_index = raw_index - 1 if isinstance(raw_index, int) and raw_index > 0 else order
            node_identity = ExecutionNodeIdentity.root(
                execution_context_id=execution_id,
                ac_index=ac_index,
            )
            runtime_scope = build_ac_runtime_scope(
                ac_index,
                execution_context_id=execution_id,
                node_id=node_identity.node_id,
                node_path=node_identity.path,
            )
            enriched.append(
                {
                    **node_identity.to_event_metadata(),
                    **ac,
                    "ac_id": ac.get("ac_id") or runtime_scope.aggregate_id,
                }
            )
        return enriched

    @staticmethod
    def _metadata_candidates(message: AgentMessage) -> tuple[Mapping[str, Any], ...]:
        """Return structured metadata maps attached to a runtime message."""
        candidates: list[Mapping[str, Any]] = []
        seen: set[int] = set()

        def add(value: object) -> None:
            if not isinstance(value, Mapping):
                return
            identity = id(value)
            if identity in seen:
                return
            seen.add(identity)
            candidates.append(value)
            for key in ("meta", "mcp_meta", "metadata", "error", "details", "response"):
                nested = value.get(key)
                if isinstance(nested, Mapping):
                    add(nested)

        add(message.data)
        return tuple(candidates)

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        """Parse an ISO timestamp defensively."""
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _duration_text_to_seconds(text: str) -> int | None:
        """Parse retry-window duration tokens from text into total seconds."""
        total_seconds = 0.0
        for match in _DURATION_PATTERN.finditer(text):
            value = float(match.group("value"))
            unit = match.group("unit").lower()
            if unit.startswith("d"):
                seconds = value * 24 * 60 * 60
            elif unit.startswith("h"):
                seconds = value * 60 * 60
            elif unit.startswith("m"):
                seconds = value * 60
            else:
                seconds = value
            total_seconds += seconds
        if total_seconds <= 0:
            return None
        return max(1, math.ceil(total_seconds))

    @classmethod
    def _duration_value_to_seconds(cls, value: object) -> int | None:
        """Parse a numeric or textual retry duration into seconds."""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int | float):
            if value <= 0:
                return None
            return max(1, math.ceil(value))
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            try:
                numeric = float(stripped)
            except ValueError:
                return cls._duration_text_to_seconds(stripped)
            if numeric <= 0:
                return None
            return max(1, math.ceil(numeric))
        return None

    @classmethod
    def _duration_from_metadata(
        cls,
        metadata: Mapping[str, Any],
        *,
        now: datetime,
    ) -> int | None:
        """Extract retry/pause duration from structured runtime metadata."""
        for key in (
            "pause_seconds",
            "retry_after_seconds",
            "retryAfterSeconds",
            "reset_after_seconds",
            "resetAfterSeconds",
        ):
            parsed = cls._duration_value_to_seconds(metadata.get(key))
            if parsed is not None:
                return parsed

        for key in ("retry_after_ms", "retryAfterMs", "reset_after_ms", "resetAfterMs"):
            parsed = cls._duration_value_to_seconds(metadata.get(key))
            if parsed is not None:
                return max(1, math.ceil(parsed / 1000))

        for key in ("retry_after", "retryAfter", "reset_after", "resetAfter"):
            value = metadata.get(key)
            parsed_datetime = cls._parse_datetime(value)
            if parsed_datetime is not None:
                seconds = math.ceil((parsed_datetime - now).total_seconds())
                if seconds > 0:
                    return seconds
            parsed_duration = cls._duration_value_to_seconds(value)
            if parsed_duration is not None:
                return parsed_duration

        for key in ("resume_after", "resumeAfter", "reset_at", "resetAt"):
            parsed_datetime = cls._parse_datetime(metadata.get(key))
            if parsed_datetime is not None:
                seconds = math.ceil((parsed_datetime - now).total_seconds())
                if seconds > 0:
                    return seconds

        return None

    @classmethod
    def _duration_from_message(cls, message: AgentMessage, *, now: datetime) -> int | None:
        """Extract a retry/pause duration from metadata, then final error text."""
        for metadata in cls._metadata_candidates(message):
            duration = cls._duration_from_metadata(metadata, now=now)
            if duration is not None:
                return duration

        return cls._duration_text_to_seconds(message.content)

    @staticmethod
    def _metadata_has_runtime_error_shape(metadata: Mapping[str, Any]) -> bool:
        """Return True when metadata looks like provider/runtime error data."""
        runtime_keys = {
            "error_type",
            "error_code",
            "code",
            "status",
            "status_code",
            "http_status",
            "provider",
            "recoverable",
            "is_retriable",
            "retriable",
            "retry_after",
            "retry_after_seconds",
            "retryAfter",
            "retryAfterSeconds",
            "resume_after",
            "reset_at",
            "reset_after",
        }
        return any(key in metadata for key in runtime_keys)

    @classmethod
    def _message_has_runtime_error_shape(cls, message: AgentMessage) -> bool:
        """Return True when any attached metadata looks runtime-owned."""
        return any(
            cls._metadata_has_runtime_error_shape(metadata)
            for metadata in cls._metadata_candidates(message)
        )

    @staticmethod
    def _metadata_text(metadata: Mapping[str, Any]) -> str:
        """Flatten common structured error fields for quota classification."""
        values: list[str] = []
        for key in (
            "error_type",
            "error_code",
            "code",
            "type",
            "reason",
            "message",
            "status",
            "provider",
        ):
            value = metadata.get(key)
            if isinstance(value, str):
                values.append(value)
        return " ".join(values).lower()

    @staticmethod
    def _is_usage_limit_text(text: str, *, has_runtime_error_shape: bool) -> bool:
        """Classify provider usage/quota window messages with conservative text rules."""
        normalized = " ".join(text.lower().split())
        if not normalized:
            return False
        if not has_runtime_error_shape:
            return False

        has_quota_phrase = any(
            pattern.search(normalized) is not None for pattern in _USAGE_LIMIT_TEXT_PATTERNS
        )
        duration_seconds = OrchestratorRunner._duration_text_to_seconds(normalized)
        has_long_retry_window = (
            duration_seconds is not None
            and duration_seconds >= _LONG_RETRY_AFTER_SECONDS
            and re.search(
                r"\b(?:try again|retry|come back|available|reset|resets|window)\b",
                normalized,
            )
            is not None
        )
        mentions_limit_window = _USAGE_LIMIT_WINDOW_CONTEXT_PATTERN.search(normalized) is not None

        if has_quota_phrase and (has_runtime_error_shape or duration_seconds is not None):
            return True
        return bool(has_long_retry_window and mentions_limit_window)

    @classmethod
    def _usage_limit_failure_from_metadata(
        cls,
        message: AgentMessage,
        *,
        now: datetime,
    ) -> bool:
        """Return True when structured metadata identifies a quota-window failure."""
        for metadata in cls._metadata_candidates(message):
            recovery = metadata.get("recovery")
            if isinstance(recovery, Mapping):
                kind = str(recovery.get("kind", "")).strip().lower()
                if kind in _USAGE_LIMIT_RECOVERY_KINDS:
                    return True

            if metadata.get("usage_limit") is True or metadata.get("quota_exhausted") is True:
                return True

            metadata_text = cls._metadata_text(metadata)
            duration = cls._duration_from_metadata(metadata, now=now)
            if duration is not None and duration >= _LONG_RETRY_AFTER_SECONDS:
                if re.search(r"\b(?:usage|quota|allowance|limit|window)\b", metadata_text):
                    return True

            if metadata_text and cls._is_usage_limit_text(
                metadata_text,
                has_runtime_error_shape=True,
            ):
                return True

        return False

    @staticmethod
    def _format_pause_duration(seconds: int) -> str:
        """Return a compact human-readable duration for pause hints."""
        if seconds % (24 * 60 * 60) == 0:
            days = seconds // (24 * 60 * 60)
            return f"{days} day{'s' if days != 1 else ''}"
        if seconds % (60 * 60) == 0:
            hours = seconds // (60 * 60)
            return f"{hours} hour{'s' if hours != 1 else ''}"
        if seconds % 60 == 0:
            minutes = seconds // 60
            return f"{minutes} minute{'s' if minutes != 1 else ''}"
        return f"{seconds} second{'s' if seconds != 1 else ''}"

    def _usage_limit_pause(
        self,
        message: AgentMessage,
        *,
        now: datetime,
    ) -> RecoverableFailurePause | None:
        """Return a pause decision for provider usage/quota window failures."""
        has_runtime_error_shape = self._message_has_runtime_error_shape(message)
        is_usage_limit = self._usage_limit_failure_from_metadata(
            message,
            now=now,
        ) or self._is_usage_limit_text(
            message.content,
            has_runtime_error_shape=has_runtime_error_shape,
        )
        if not is_usage_limit:
            return None

        from ouroboros.config import get_usage_limit_pause_seconds

        default_pause_seconds = get_usage_limit_pause_seconds()

        pause_seconds = self._duration_from_message(message, now=now) or default_pause_seconds
        pause_seconds = max(1, pause_seconds)
        resume_after = now + timedelta(seconds=pause_seconds)
        duration_display = self._format_pause_duration(pause_seconds)
        return RecoverableFailurePause(
            pause_kind="usage_limit",
            reason=message.content,
            pause_seconds=pause_seconds,
            resume_after=resume_after,
            resume_hint=(
                "Provider usage/quota window reached. "
                f"Resume after {resume_after.isoformat()} "
                f"(wait at least {duration_display})."
            ),
        )

    @classmethod
    def _resume_retry_pause(cls, message: AgentMessage) -> RecoverableFailurePause | None:
        """Return a pause decision for recoverable resume-bootstrap failures."""
        for metadata in cls._metadata_candidates(message):
            recovery = metadata.get("recovery")
            if not isinstance(recovery, Mapping):
                continue
            kind = str(recovery.get("kind", "")).strip().lower()
            if kind == _RESUME_RETRY_RECOVERY_KIND:
                return RecoverableFailurePause(
                    pause_kind=_RESUME_RETRY_RECOVERY_KIND,
                    reason=message.content,
                    resume_hint=(
                        "Retry the same --resume session after fixing the runtime/tooling issue."
                    ),
                )
        return None

    def _recoverable_failure_pause(
        self,
        message: AgentMessage,
        *,
        now: datetime | None = None,
    ) -> RecoverableFailurePause | None:
        """Return pause metadata when a final runtime error should stay resumable."""
        if not (message.is_final and message.is_error):
            return None

        resume_retry = self._resume_retry_pause(message)
        if resume_retry is not None:
            return resume_retry

        return self._usage_limit_pause(message, now=now or datetime.now(UTC))

    def _is_recoverable_resume_failure(self, message: AgentMessage) -> bool:
        """Return True when a final error should leave the session resumable."""
        return self._recoverable_failure_pause(message) is not None

    def _recoverable_failure_pause_from_parallel_result(
        self,
        parallel_result: Any,
        *,
        now: datetime | None = None,
    ) -> RecoverableFailurePause | None:
        """Return a pause only when every executed failure is recoverable."""

        def iter_leaf_ac_results(results: tuple[Any, ...]) -> Any:
            for result in results:
                sub_results = getattr(result, "sub_results", ())
                if isinstance(sub_results, tuple) and sub_results:
                    yield from iter_leaf_ac_results(sub_results)
                else:
                    yield result

        def latest_pause(
            current: RecoverableFailurePause,
            candidate: RecoverableFailurePause,
        ) -> RecoverableFailurePause:
            current_resume_after = current.resume_after or datetime.min.replace(tzinfo=UTC)
            candidate_resume_after = candidate.resume_after or datetime.min.replace(tzinfo=UTC)
            if candidate_resume_after > current_resume_after:
                return candidate
            if candidate_resume_after == current_resume_after and (candidate.pause_seconds or 0) > (
                current.pause_seconds or 0
            ):
                return candidate
            return current

        resolved_now = now or datetime.now(UTC)
        results = getattr(parallel_result, "results", ())
        if not isinstance(results, tuple):
            return None

        selected_pause: RecoverableFailurePause | None = None
        found_failure = False

        for ac_result in iter_leaf_ac_results(results):
            if bool(getattr(ac_result, "is_invalid", False)):
                return None
            if not bool(getattr(ac_result, "is_failure", False)):
                continue

            found_failure = True
            messages = getattr(ac_result, "messages", ())
            if not isinstance(messages, tuple):
                return None

            failure_pause = None
            for message in reversed(messages):
                pause = self._recoverable_failure_pause(message, now=resolved_now)
                if pause is not None:
                    failure_pause = pause
                    break

            if failure_pause is None:
                return None

            selected_pause = (
                failure_pause
                if selected_pause is None
                else latest_pause(selected_pause, failure_pause)
            )

        if not found_failure:
            return None

        return selected_pause

    async def _terminate_runtime_handle(
        self,
        runtime_handle: RuntimeHandle | None,
        *,
        session_id: str,
        context: str,
    ) -> None:
        """Best-effort live runtime termination for handles that remain controllable."""
        if runtime_handle is None or not runtime_handle.can_terminate:
            return

        try:
            terminated = await runtime_handle.terminate()
        except Exception as exc:
            log.warning(
                "orchestrator.runner.runtime_handle_terminate_failed",
                session_id=session_id,
                context=context,
                backend=runtime_handle.backend,
                error=str(exc),
            )
            return

        if terminated:
            log.info(
                "orchestrator.runner.runtime_handle_terminated",
                session_id=session_id,
                context=context,
                backend=runtime_handle.backend,
            )

    def _should_emit_progress_event(
        self,
        message: AgentMessage,
        messages_processed: int,
    ) -> bool:
        """Determine whether a message should emit a persisted progress event."""
        projected = project_runtime_message(message)
        runtime_backend = message.resume_handle.backend if message.resume_handle else None
        return (
            message.is_final
            or messages_processed % PROGRESS_EMIT_INTERVAL == 0
            or projected.is_tool_call
            or projected.thinking is not None
            or message.type == "system"
            or runtime_backend == "opencode"
            or projected.is_tool_result
        )

    async def _update_and_persist_progress(
        self,
        tracker: SessionTracker,
        message: AgentMessage,
        messages_processed: int,
        session_id: str,
    ) -> SessionTracker:
        """Update tracker progress and persist when needed.

        Persists on: final message, every N messages, or runtime handle change.
        Returns updated tracker.
        """
        previous_runtime = tracker.progress.get("runtime")
        progress_update = self._build_progress_update(message, messages_processed)
        tracker = tracker.with_progress(progress_update)

        # Compare runtime dicts ignoring the volatile updated_at field
        def _stable_runtime(rt: Any) -> Any:
            if isinstance(rt, dict):
                return {k: v for k, v in rt.items() if k != "updated_at"}
            return rt

        should_persist = (
            message.is_final
            or messages_processed % SESSION_PROGRESS_PERSIST_INTERVAL == 0
            or _stable_runtime(progress_update.get("runtime")) != _stable_runtime(previous_runtime)
        )
        if should_persist:
            await self._persist_session_progress(session_id, progress_update)
        return tracker

    async def _persist_session_progress(
        self,
        session_id: str,
        progress: dict[str, Any],
    ) -> None:
        """Persist session progress without interrupting execution on failure."""
        if self._task_workspace is not None:
            heartbeat_lock(self._task_workspace.lock_path)
        result = await self._session_repo.track_progress(session_id, progress)
        if result.is_err:
            log.warning(
                "orchestrator.runner.progress_persist_failed",
                session_id=session_id,
                error=str(result.error),
            )

    async def _replay_workflow_state(
        self,
        session_id: str,
        state_tracker: Any,
    ) -> None:
        """Replay persisted session progress events into workflow state."""
        try:
            events = await self._event_store.replay("session", session_id)
        except Exception as e:
            log.warning(
                "orchestrator.runner.workflow_state_replay_failed",
                session_id=session_id,
                error=str(e),
            )
            return

        state_tracker.replay_progress_events(events)

    async def cancel_execution(
        self,
        execution_id: str,
        reason: str = "Cancelled by user",
        cancelled_by: str = "user",
    ) -> Result[dict[str, Any], OrchestratorError]:
        """Cancel a running execution gracefully.

        This is the shared cancellation entry point used by both the MCP tool
        and CLI command. It signals the in-flight execution to stop at the
        next message boundary and updates the session status to CANCELLED.

        If the execution is actively running in this runner instance, adds
        the session to the cancellation registry so the message loop exits
        gracefully. If the execution is not found in-flight (e.g., orphaned
        or stuck), marks the session as cancelled directly via the repository.

        Args:
            execution_id: Execution ID to cancel.
            reason: Human-readable cancellation reason.
            cancelled_by: Who/what initiated cancellation ("user", "auto_cleanup").

        Returns:
            Result with cancellation details on success, or error.
        """
        session_id = self._active_sessions.get(execution_id)

        if session_id is not None:
            # In-flight cancellation: signal via the cancellation registry
            await request_cancellation(
                session_id,
                reason=reason,
                cancelled_by=cancelled_by,
            )
            log.info(
                "orchestrator.runner.cancellation_requested",
                execution_id=execution_id,
                session_id=session_id,
                reason=reason,
                cancelled_by=cancelled_by,
                in_flight=True,
            )
            # The message loop will detect this and call _handle_cancellation
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "cancellation_requested",
                    "in_flight": True,
                    "reason": reason,
                }
            )

        # Not in-flight: cancel directly via session repository
        return await self._cancel_session_directly(
            execution_id=execution_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

    async def _cancel_session_directly(
        self,
        execution_id: str,
        reason: str,
        cancelled_by: str,
    ) -> Result[dict[str, Any], OrchestratorError]:
        """Cancel a session directly via the repository (not in-flight).

        Used for orphaned/stuck executions that are no longer actively
        running in this process. Looks up the session_id from the event
        store and marks it as cancelled.

        Args:
            execution_id: Execution ID being cancelled.
            reason: Human-readable cancellation reason.
            cancelled_by: Who/what initiated cancellation.

        Returns:
            Result with cancellation details on success, or error.
        """
        session_id: str | None = None
        # Try to find session_id from event store
        try:
            events = await self._event_store.get_all_sessions()
            for event in events:
                if (
                    event.type == "orchestrator.session.started"
                    and event.data.get("execution_id") == execution_id
                ):
                    session_id = event.aggregate_id
                    break
        except Exception as e:
            log.warning(
                "orchestrator.runner.session_lookup_failed",
                execution_id=execution_id,
                error=str(e),
            )

        if session_id is None:
            return Result.err(
                OrchestratorError(
                    message=f"No session found for execution {execution_id}",
                    details={"execution_id": execution_id},
                )
            )

        tracker_result = await self._session_repo.reconstruct_session(session_id)
        if tracker_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to reconstruct session for cancellation: {tracker_result.error}",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            )
        tracker = tracker_result.value
        if tracker.status in {
            SessionStatus.COMPLETED,
            SessionStatus.FAILED,
            SessionStatus.CANCELLED,
        }:
            self._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            await clear_cancellation(session_id)
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "already_terminal",
                    "terminal_status": tracker.status.value,
                    "reason": reason,
                }
            )

        process_local = await request_process_local_cancellation(
            tracker,
            self._session_repo,
            reason=reason,
            cancelled_by=cancelled_by,
        )
        if process_local is not None:
            if (
                process_local.disposition
                == ProcessLocalCancellationDisposition.CANCELLATION_REQUESTED
            ):
                return Result.ok(
                    {
                        "execution_id": execution_id,
                        "session_id": session_id,
                        "status": "cancellation_requested",
                        "in_flight": True,
                        "reason": reason,
                    }
                )
            if process_local.disposition == ProcessLocalCancellationDisposition.HELD_ELSEWHERE:
                return Result.err(
                    self._process_local_authority_held_elsewhere_error(session_id, execution_id)
                )
            if process_local.disposition == ProcessLocalCancellationDisposition.PERSISTENCE_PENDING:
                return Result.err(
                    OrchestratorError(
                        message="Failed to persist cancellation; retained process-local owner must retry",
                        details={
                            "execution_id": execution_id,
                            "session_id": session_id,
                            "resume_blocked": "cancellation_persistence_pending",
                            "cause": str(process_local.error),
                        },
                    )
                )
            if process_local.disposition == ProcessLocalCancellationDisposition.ALREADY_TERMINAL:
                return Result.ok(
                    {
                        "execution_id": execution_id,
                        "session_id": session_id,
                        "status": "already_terminal",
                        "reason": reason,
                    }
                )

            await self._report_frugality_retrospective(
                execution_id=execution_id,
                session_id=session_id,
                terminal_status="cancelled",
            )
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "cancelled",
                    "in_flight": False,
                    "reason": reason,
                }
            )

        # Historical sessions have no live Foundation A capability to coordinate.
        cancel_result = await self._session_repo.mark_cancelled(
            session_id=session_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

        if cancel_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to cancel session: {cancel_result.error}",
                    details={
                        "execution_id": execution_id,
                        "session_id": session_id,
                    },
                )
            )
        if cancel_result.value is False:
            terminal_result = await self._session_repo.reconstruct_session(session_id)
            terminal_status = (
                terminal_result.value.status
                if terminal_result.is_ok
                and terminal_result.value.status
                in {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
                else None
            )
            return Result.ok(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "status": "already_terminal",
                    **(
                        {"terminal_status": terminal_status.value}
                        if terminal_status is not None
                        else {}
                    ),
                    "reason": reason,
                }
            )

        await self._report_frugality_retrospective(
            execution_id=execution_id,
            session_id=session_id,
            terminal_status="cancelled",
        )

        log.info(
            "orchestrator.runner.session_cancelled_directly",
            execution_id=execution_id,
            session_id=session_id,
            reason=reason,
            cancelled_by=cancelled_by,
        )

        return Result.ok(
            {
                "execution_id": execution_id,
                "session_id": session_id,
                "status": "cancelled",
                "in_flight": False,
                "reason": reason,
            }
        )

    async def _get_merged_tools(
        self,
        session_id: str,
        tool_prefix: str = "",
        strategy: ExecutionStrategy | None = None,
    ) -> tuple[list[str], MCPToolProvider | None, SessionToolCatalog]:
        """Get merged tool list from strategy tools and MCP tools.

        Uses strategy.get_tools() as the base tool set (falls back to
        DEFAULT_TOOLS when no strategy is provided). If MCP manager is
        configured, discovers tools from connected servers and merges them.

        Args:
            session_id: Current session ID for event emission.
            tool_prefix: Optional prefix for MCP tool names.
            strategy: Execution strategy providing base tool set.

        Returns:
            Tuple of (merged tool names list, MCPToolProvider or None, session catalog).
        """
        # Start with strategy tools (or DEFAULT_TOOLS as fallback)
        base_tools = strategy.get_tools() if strategy else list(DEFAULT_TOOLS)
        inherited_mcp: set[str] = set()
        if self._inherited_tools:
            # Separate inherited tools into two buckets:
            #
            # 1. **Builtins** (Read, Edit, Bash, …) → added to ``base_tools``
            #    so they receive real catalog entries with handlers.
            #
            # 2. **Bridge / MCP tools** → stored as ``inherited_capabilities``
            #    on the session catalog.  They are *not* added to
            #    ``base_tools`` because that would synthesize phantom catalog
            #    entries (definitions with no backing handler).  When
            #    ``self._mcp_manager`` is set, ``MCPToolProvider.get_tools()``
            #    below discovers them with real server connections.  When the
            #    manager is absent the names are still preserved so the
            #    delegated-session capability contract is not silently lost.
            known_builtins = {d.name for d in enumerate_runtime_builtin_tool_definitions()}
            for tool_name in self._inherited_tools:
                if tool_name in known_builtins and tool_name not in base_tools:
                    base_tools.append(tool_name)
                elif tool_name not in known_builtins:
                    inherited_mcp.add(tool_name)
                    log.info(
                        "orchestrator.runner.inherited_mcp_capability_preserved",
                        tool=tool_name,
                        has_mcp_manager=self._mcp_manager is not None,
                    )
        session_catalog = assemble_session_tool_catalog(base_tools)
        if inherited_mcp:
            session_catalog = replace(
                session_catalog,
                inherited_capabilities=frozenset(inherited_mcp),
            )

        # Defer the pre-discovery policy evaluation.  Previously we computed
        # it unconditionally and threw it away whenever MCP discovery
        # succeeded.  Now we only evaluate once per path, so the
        # post-discovery success case does not double-compute.
        if self._mcp_manager is None:
            policy_result = self._evaluate_tool_catalog_policy(session_catalog)
            await self._emit_policy_capabilities_evaluated_event(
                session_id,
                policy_result.capability_graph,
                policy_result.policy_decisions,
                policy_result.policy_context,
            )
            return policy_result.allowed_tools, None, session_catalog

        # Create provider and get MCP tools
        provider = MCPToolProvider(
            self._mcp_manager,
            tool_prefix=tool_prefix,
        )

        try:
            mcp_tools = await provider.get_tools(builtin_tools=base_tools)
        except Exception as e:
            log.warning(
                "orchestrator.runner.mcp_tools_load_failed",
                session_id=session_id,
                error=str(e),
            )
            policy_result = self._evaluate_tool_catalog_policy(session_catalog)
            await self._emit_policy_capabilities_evaluated_event(
                session_id,
                policy_result.capability_graph,
                policy_result.policy_decisions,
                policy_result.policy_context,
            )
            return policy_result.allowed_tools, None, session_catalog

        if not mcp_tools:
            log.info(
                "orchestrator.runner.no_mcp_tools_available",
                session_id=session_id,
            )
            policy_result = self._evaluate_tool_catalog_policy(session_catalog)
            await self._emit_policy_capabilities_evaluated_event(
                session_id,
                policy_result.capability_graph,
                policy_result.policy_decisions,
                policy_result.policy_context,
            )
            return policy_result.allowed_tools, provider, session_catalog

        session_catalog = provider.session_catalog
        # Preserve inherited MCP capabilities after discovery replaces the
        # catalog.  The provider builds a fresh catalog from live connections
        # which does not know about the parent's capability grant.
        if inherited_mcp:
            session_catalog = replace(
                session_catalog,
                inherited_capabilities=frozenset(inherited_mcp),
            )
        policy_result = self._evaluate_tool_catalog_policy(session_catalog)
        merged_tools = policy_result.allowed_tools
        await self._emit_policy_capabilities_evaluated_event(
            session_id,
            policy_result.capability_graph,
            policy_result.policy_decisions,
            policy_result.policy_context,
        )
        mcp_tool_names = [t.name for t in mcp_tools]

        # Log conflicts
        for conflict in provider.conflicts:
            log.warning(
                "orchestrator.runner.tool_conflict",
                tool_name=conflict.tool_name,
                source=conflict.source,
                shadowed_by=conflict.shadowed_by,
                resolution=conflict.resolution,
            )

        # Emit MCP tools loaded event
        server_names = tuple({t.server_name for t in mcp_tools})
        mcp_event = create_mcp_tools_loaded_event(
            session_id=session_id,
            tool_count=len(mcp_tools),
            server_names=server_names,
            conflict_count=len(provider.conflicts),
            tool_names=mcp_tool_names,
        )
        await self._event_store.append(mcp_event)

        log.info(
            "orchestrator.runner.mcp_tools_loaded",
            session_id=session_id,
            mcp_tool_count=len(mcp_tools),
            total_tools=len(merged_tools),
            servers=server_names,
        )

        return merged_tools, provider, session_catalog

    async def _check_cancellation(self, session_id: str) -> bool:
        """Check for cancellation via in-memory registry and event store.

        First checks the in-memory cancellation registry (fast path) which is
        populated by the MCP cancel tool. Falls back to querying the event store
        for ``orchestrator.session.cancelled`` events so that cancellations
        persisted by the CLI or other processes are also detected.

        Args:
            session_id: Session ID to check for cancellation.

        Returns:
            True if cancellation was requested, False otherwise.
        """
        # Fast path: check the in-memory cancellation set first.
        # This is O(1) and requires no I/O.
        if await is_cancellation_requested(session_id):
            return True

        # Slow path: check event store for externally-persisted cancellation
        try:
            events = await self._event_store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
                limit=1,
            )
            return len(events) > 0
        except Exception:
            # Graceful degradation: if event store query fails,
            # don't interrupt execution — just log and continue
            log.warning(
                "orchestrator.runner.cancellation_check_failed",
                session_id=session_id,
            )
            return False

    async def _check_startup_cancellation(self, session_id: str) -> bool:
        """Check cancellation before normal message-loop checkpoints exist."""
        if await is_cancellation_requested(session_id):
            return True
        try:
            events = await self._event_store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
                limit=1,
            )
            return len(events) > 0
        except Exception:
            log.warning(
                "orchestrator.runner.startup_cancellation_check_failed",
                session_id=session_id,
            )
            return False

    def _cancellation_persistence_pending_result(
        self,
        *,
        session_id: str,
        execution_id: str,
        cause: object,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Leave a failed cancellation write retryable without stranding a claim.

        The live registration and heartbeat remain as a truthful same-process
        owner, and the cancellation request remains set.  The effectful claim,
        active route, and worktree lock belong to the coroutine that is now
        exiting, so they must be released for a retained owner to retry the
        durable terminal write on a later resume request.
        """
        self._preserve_process_local_owner_for_retry(
            session_id=session_id,
            execution_id=execution_id,
        )
        return Result.err(
            OrchestratorError(
                message="Failed to persist cancellation; process-local authority remains live",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "cause": str(cause),
                    "resume_blocked": "cancellation_persistence_pending",
                    "cancellation_persistence_pending": True,
                },
            )
        )

    async def _drain_requested_cancellation_before_pre_execution_cleanup(
        self,
        *,
        session_id: str,
        execution_id: str,
        messages_processed: int,
        start_time: datetime,
    ) -> Result[OrchestratorResult, OrchestratorError] | None:
        """Persist a published cancellation before abandoning a claimed setup.

        A public cancellation can arrive after a resume/new-run claims the
        generation but before the runner has registered its normal active
        route.  Raw task cancellation in that window must not retire the
        capability and leave a durable ``PAUSED`` tracker that a later resume
        reclassifies as lost authority.  Run the normal cancellation lifecycle
        in a shielded child task so a repeat caller cancellation cannot skip
        the durable write or its retryable-pending cleanup.

        Returns ``None`` when no cooperative cancellation is pending; otherwise
        it returns the normal cancellation result.  A second raw cancellation
        is re-raised after the child has drained its lifecycle work.
        """
        if not await is_cancellation_requested(session_id):
            return None

        task = asyncio.create_task(
            self._handle_cancellation(
                session_id=session_id,
                execution_id=execution_id,
                messages_processed=messages_processed,
                start_time=start_time,
            )
        )
        repeated_cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                repeated_cancellation = repeated_cancellation or exc

        try:
            result = task.result()
        except asyncio.CancelledError as exc:
            repeated_cancellation = repeated_cancellation or exc
            result = self._cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause="cancellation lifecycle task cancelled",
            )
        except Exception as exc:  # pragma: no cover - defensive task boundary
            log.exception(
                "orchestrator.runner.pre_execution_cancellation_drain_failed",
                session_id=session_id,
                execution_id=execution_id,
            )
            result = self._cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
            )

        if repeated_cancellation is not None:
            raise repeated_cancellation
        return result

    async def _handle_cancellation(
        self,
        session_id: str,
        execution_id: str,
        messages_processed: int,
        start_time: datetime,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Handle a detected cancellation by marking the session and returning a result.

        Args:
            session_id: Session that was cancelled.
            execution_id: Execution ID for the result.
            messages_processed: Number of messages processed before cancellation.
            start_time: When execution started.

        Returns:
            Result containing OrchestratorResult with success=False and cancellation info.
        """
        duration = (datetime.now(UTC) - start_time).total_seconds()

        log.info(
            "orchestrator.runner.execution_cancelled",
            session_id=session_id,
            execution_id=execution_id,
            messages_processed=messages_processed,
            duration_seconds=duration,
        )
        cancellation_request = await get_cancellation_request(session_id)
        cancellation_reason = (
            cancellation_request.reason
            if cancellation_request is not None
            else "Cancellation detected during execution"
        )
        cancelled_by = (
            cancellation_request.cancelled_by if cancellation_request is not None else "runner"
        )

        # Determine and durably publish the terminal state *before* withdrawing
        # the process-local capability or its heartbeat.  A RUNNING tracker
        # with neither liveness signal is indistinguishable from a crashed
        # owner to another process, so releasing first can cause a concurrent
        # observer to terminalize this deliberate cancellation as lost
        # authority.  It would also make a persistence failure report a
        # cancellation that the durable session never recorded.
        session_result = await self._session_repo.reconstruct_session(session_id)
        _terminal = {SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED}
        # An unreadable snapshot cannot prove that a terminal state already
        # exists.  Continue through ``mark_cancelled`` while retaining the live
        # owner; that append is the authoritative terminal write and preserves
        # the legacy best-effort reconstruction posture without ever releasing
        # a RUNNING session first.
        session_already_terminal = session_result.is_ok and session_result.value.status in _terminal
        if session_already_terminal:
            terminal_status = session_result.value.status
            final_message = f"Execution already {terminal_status.value}"
            summary = {"terminal_status": terminal_status.value, **self._task_summary()}
            if terminal_status == SessionStatus.CANCELLED:
                summary["cancelled"] = True
            try:
                execution_terminal_events = await self._event_store.query_events(
                    aggregate_id=execution_id,
                    event_type="execution.terminal",
                    limit=1,
                )
            except Exception:
                execution_terminal_events = []

            async def _reconcile_existing_terminal_owner() -> None:
                try:
                    await clear_cancellation(session_id)
                    if not execution_terminal_events:
                        await self._event_store.append(
                            create_execution_terminal_event(
                                execution_id=execution_id,
                                session_id=session_id,
                                status=terminal_status.value,
                                summary=(
                                    summary if terminal_status == SessionStatus.COMPLETED else None
                                ),
                                error_message=(
                                    final_message
                                    if terminal_status != SessionStatus.COMPLETED
                                    else None
                                ),
                                messages_processed=messages_processed,
                            )
                        )
                finally:
                    self._retire_process_local_authority(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    self._unregister_session(execution_id, session_id)
                    if self._task_workspace is not None:
                        release_lock(self._task_workspace.lock_path)

            await _await_process_local_cleanup(_reconcile_existing_terminal_owner())
            await self._report_frugality_retrospective(
                execution_id=execution_id,
                session_id=session_id,
                terminal_status=terminal_status.value,
            )
            return Result.ok(
                OrchestratorResult(
                    success=terminal_status == SessionStatus.COMPLETED,
                    session_id=session_id,
                    execution_id=execution_id,
                    summary=summary,
                    messages_processed=messages_processed,
                    final_message=final_message,
                    duration_seconds=duration,
                )
            )

        try:
            cancel_result = await self._session_repo.mark_cancelled(
                session_id,
                reason=cancellation_reason,
                cancelled_by=cancelled_by,
            )
        except asyncio.CancelledError:
            if (
                await self._reconcile_durable_terminal_and_cleanup(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                is None
            ):
                self._preserve_process_local_owner_for_retry(
                    session_id=session_id,
                    execution_id=execution_id,
                )
            raise
        except Exception as exc:
            durable_status = await self._reconcile_durable_terminal_and_cleanup(
                session_id=session_id,
                execution_id=execution_id,
            )
            if durable_status is not None:
                return await self._handle_cancellation(
                    session_id=session_id,
                    execution_id=execution_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                )
            log.warning(
                "orchestrator.runner.mark_cancelled_raised",
                session_id=session_id,
                error=str(exc),
            )
            return self._cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
            )
        if cancel_result is not None and cancel_result.is_err:
            log.warning(
                "orchestrator.runner.mark_cancelled_failed",
                session_id=session_id,
                error=str(cancel_result.error),
            )
            return self._cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=cancel_result.error,
            )
        if cancel_result is not None and cancel_result.value is False:
            # The session became terminal after the initial reconstruction but
            # before this owner won its cancellation transition. Re-enter once
            # so the authoritative terminal branch mirrors the real winner and
            # tears down this process-local owner without reporting cancellation.
            winner = await self._session_repo.reconstruct_session(session_id)
            if winner.is_ok and winner.value.status in _terminal:
                return await self._handle_cancellation(
                    session_id=session_id,
                    execution_id=execution_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                )
            return self._cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=PersistenceError(
                    "Terminal cancellation lost its CAS but the durable winner could not be read"
                ),
            )

        # The session is now terminal. Drain the complete reconciliation in a
        # shielded child task: repeated caller cancellation may not interrupt
        # marker acknowledgement, projection, or live-owner teardown after the
        # durable CAS has committed.
        async def _reconcile_cancelled_owner() -> None:
            try:
                await clear_cancellation(session_id)
                await self._event_store.append(
                    create_execution_terminal_event(
                        execution_id=execution_id,
                        session_id=session_id,
                        status="cancelled",
                        error_message=cancellation_reason,
                        messages_processed=messages_processed,
                    )
                )
            finally:
                self._retire_process_local_authority(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                self._unregister_session(execution_id, session_id)
                if self._task_workspace is not None:
                    release_lock(self._task_workspace.lock_path)

        await _await_process_local_cleanup(_reconcile_cancelled_owner())
        await self._report_frugality_retrospective(
            execution_id=execution_id,
            session_id=session_id,
            terminal_status="cancelled",
        )

        # Display cancellation notice
        self._console.print(
            Panel(
                Text("Execution cancelled by external request", style="yellow"),
                title="[yellow]Execution Cancelled[/yellow]",
                border_style="yellow",
            )
        )

        return Result.ok(
            OrchestratorResult(
                success=False,
                session_id=session_id,
                execution_id=execution_id,
                summary={"cancelled": True, **self._task_summary()},
                messages_processed=messages_processed,
                final_message="Execution cancelled by external request",
                duration_seconds=duration,
            )
        )

    async def execute_seed(
        self,
        seed: Seed,
        execution_id: str | None = None,
        session_id: str | None = None,
        parallel: bool = True,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
        force_sequential_levels: bool = False,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Execute seed via Claude Agent.

        This is the main entry point for orchestrator execution.
        It converts the seed to prompts, executes via the adapter,
        and tracks progress through events.

        Args:
            seed: Seed specification to execute.
            execution_id: Optional execution ID. Generated if not provided.
            session_id: Optional session ID to preallocate for external tracking.
            parallel: Enable parallel AC execution. When True, independent ACs
                     run concurrently. Default: True (parallel execution).
            externally_satisfied_acs: Top-level ACs already satisfied by the
                current working tree and therefore skipped for re-execution.
            force_sequential_levels: Preserve --sequential ordering while still
                using the AC executor, primarily for temporary fat-harness opt-in.

        Returns:
            Result containing OrchestratorResult on success.
        """
        session_result = await self.prepare_session(
            seed,
            execution_id=execution_id,
            session_id=session_id,
        )
        if session_result.is_err:
            return Result.err(session_result.error)

        execute_kwargs: dict[str, Any] = {
            "seed": seed,
            "tracker": session_result.value,
            "parallel": parallel,
        }
        if externally_satisfied_acs:
            execute_kwargs["externally_satisfied_acs"] = externally_satisfied_acs
        if force_sequential_levels:
            execute_kwargs["force_sequential_levels"] = True

        return await self.execute_precreated_session(**execute_kwargs)

    async def prepare_session(
        self,
        seed: Seed,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> Result[SessionTracker, OrchestratorError]:
        """Create and persist the orchestration session before execution begins.

        This allows callers such as MCP handlers to return stable tracking IDs
        immediately and then start the actual runtime work asynchronously.
        """
        exec_id = execution_id or f"exec_{uuid4().hex[:12]}"
        resolved_session_id = session_id or f"orch_{uuid4().hex[:12]}"
        self._execution_guidance = None
        # A generation belongs to this one preparation call.  Do not store it
        # in a runner-wide mutable slot: concurrent preparations must not share
        # a capability or correlation id.
        authority_generation = self._begin_process_local_authority_generation()
        workspace_had_existing_owner = bool(self._process_local_authorities)

        def abort_process_local_preparation() -> None:
            """Dispose every authority state reachable from an aborted prepare.

            The registration may have completed before a later setup step
            raises, while an earlier failure has only an unregistered issuance.
            Both cleanup operations are idempotent and together leave no
            capability or lease behind.
            """
            self._retire_process_local_authority(
                session_id=resolved_session_id,
                execution_id=exec_id,
            )
            self._discard_process_local_authority(authority_generation)
            # The workspace lock is runner-wide, while this authority
            # generation belongs only to the current preparation. A rejected
            # second preparation must not release exclusion still owned by an
            # earlier live session. The dynamic check also covers concurrent
            # preparations that both began before either registered.
            workspace_has_other_owner = any(
                identity != (resolved_session_id, exec_id)
                for identity in self._process_local_authorities
            )
            workspace_has_live_session_owner = _has_live_process_local_authority_session(
                resolved_session_id
            )
            if (
                self._task_workspace is not None
                and not workspace_had_existing_owner
                and not workspace_has_other_owner
                and not workspace_has_live_session_owner
            ):
                release_lock(self._task_workspace.lock_path)

        try:
            execution_contract = await asyncio.to_thread(
                self._build_execution_contract,
                seed=seed,
                authority_generation=authority_generation,
            )
            self._execution_guidance_delivery_mode()
            # Establish the exact capability and PID liveness lease before any
            # durable RUNNING tracker can be reconstructed by an observer. The
            # resolved session id is allocated locally for that purpose rather
            # than delegated to SessionRepository after its start event is written.
            self._register_process_local_authority(
                session_id=resolved_session_id,
                execution_id=exec_id,
                execution_contract=execution_contract,
                generation=authority_generation,
            )
        except OrchestratorError as exc:
            abort_process_local_preparation()
            return Result.err(exc)
        except asyncio.CancelledError:
            abort_process_local_preparation()
            raise
        except Exception as exc:
            abort_process_local_preparation()
            log.exception(
                "orchestrator.runner.prepare_authority_failed",
                execution_id=exec_id,
                session_id=resolved_session_id,
            )
            return Result.err(
                OrchestratorError(
                    message="Failed to prepare process-local execution authority",
                    details={
                        "execution_id": exec_id,
                        "session_id": resolved_session_id,
                        "cause": type(exc).__name__,
                    },
                )
            )
        except BaseException:
            abort_process_local_preparation()
            raise
        self._execution_contract = execution_contract

        try:
            session_result = await self._session_repo.create_session(
                execution_id=exec_id,
                seed_id=seed.metadata.seed_id,
                session_id=resolved_session_id,
                seed_goal=seed.goal,
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
                llm_backend=getattr(self._adapter, "llm_backend", None),
                execution_contract=execution_contract,
            )
        except asyncio.CancelledError:
            await self._reconcile_session_publication_interruption(
                session_id=resolved_session_id,
                execution_id=exec_id,
            )
            raise
        except Exception as exc:
            retained_owner = await self._reconcile_session_publication_interruption(
                session_id=resolved_session_id,
                execution_id=exec_id,
            )
            return Result.err(
                OrchestratorError(
                    message=f"Failed to create session: {exc}",
                    details={
                        "execution_id": exec_id,
                        "session_id": resolved_session_id,
                        **(
                            {
                                "resume_blocked": "terminal_persistence_pending",
                                "terminal_persistence_pending": True,
                            }
                            if retained_owner
                            else {}
                        ),
                    },
                )
            )

        if session_result.is_err:
            persistence_details = getattr(session_result.error, "details", {})
            if (
                isinstance(persistence_details, Mapping)
                and persistence_details.get("session_start_conflict") is True
            ):
                abort_process_local_preparation()
                return Result.err(
                    OrchestratorError(
                        message="Session ID already belongs to an immutable execution",
                        details={
                            "execution_id": exec_id,
                            "session_id": resolved_session_id,
                            "resume_blocked": "session_id_conflict",
                            "session_id_conflict": True,
                        },
                    )
                )
            retained_owner = await self._reconcile_session_publication_interruption(
                session_id=resolved_session_id,
                execution_id=exec_id,
            )
            return Result.err(
                OrchestratorError(
                    message=f"Failed to create session: {session_result.error}",
                    details={
                        "execution_id": exec_id,
                        "session_id": resolved_session_id,
                        **(
                            {
                                "resume_blocked": "terminal_persistence_pending",
                                "terminal_persistence_pending": True,
                            }
                            if retained_owner
                            else {}
                        ),
                    },
                )
            )

        tracker = session_result.value
        if tracker.session_id != resolved_session_id or tracker.execution_id != exec_id:
            # The registration and its early lease were established for the
            # caller-supplied durable identity before ``create_session`` wrote
            # ``session.started``.  Accepting a repository response for a
            # different identity would attach that capability to a tracker that
            # was never protected during publication.  This is a repository
            # contract violation, not a reason to mutate or terminalize the
            # unrelated returned tracker.
            retained_owner = await self._reconcile_session_publication_interruption(
                session_id=resolved_session_id,
                execution_id=exec_id,
            )
            return Result.err(
                OrchestratorError(
                    message="Session repository returned an unexpected session identity",
                    details={
                        "expected_session_id": resolved_session_id,
                        "expected_execution_id": exec_id,
                        "returned_session_id": tracker.session_id,
                        "returned_execution_id": tracker.execution_id,
                        **(
                            {
                                "resume_blocked": "terminal_persistence_pending",
                                "terminal_persistence_pending": True,
                            }
                            if retained_owner
                            else {}
                        ),
                    },
                )
            )
        initial_progress: dict[str, Any] = {
            "fat_harness_mode": self._fat_harness_mode,
            "messages_processed": 0,
            EXECUTION_CONTRACT_PROGRESS_KEY: execution_contract,
        }
        if self._task_workspace is not None:
            initial_progress["workspace"] = self._task_workspace.to_progress_dict()
        try:
            progress_result = await self._session_repo.track_progress(
                tracker.session_id,
                initial_progress,
            )
        except asyncio.CancelledError:
            await self._reconcile_session_publication_interruption(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )
            raise
        except Exception as exc:
            progress_exception_details: dict[str, Any] = {
                "session_id": tracker.session_id,
                "execution_id": tracker.execution_id,
                "fat_harness_mode": self._fat_harness_mode,
                "cause": str(exc),
            }
            terminal_mark_error = await self._mark_preparation_failed_best_effort(
                tracker=tracker,
                message="Failed to persist initial session contract",
                details=progress_exception_details,
            )
            if terminal_mark_error is None:
                self._retire_process_local_authority(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)
            if terminal_mark_error is not None:
                progress_exception_details["terminal_mark_error"] = terminal_mark_error
                progress_exception_details["resume_blocked"] = "terminal_persistence_pending"
                progress_exception_details["terminal_persistence_pending"] = True
            return Result.err(
                OrchestratorError(
                    message="Failed to persist initial session contract",
                    details=progress_exception_details,
                )
            )
        if progress_result.is_err:
            progress_result_details: dict[str, Any] = {
                "session_id": tracker.session_id,
                "execution_id": tracker.execution_id,
                "fat_harness_mode": self._fat_harness_mode,
                "cause": str(progress_result.error),
            }
            terminal_mark_error = await self._mark_preparation_failed_best_effort(
                tracker=tracker,
                message="Failed to persist initial session contract",
                details=progress_result_details,
            )
            if terminal_mark_error is None:
                self._retire_process_local_authority(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)

            if terminal_mark_error is not None:
                progress_result_details["terminal_mark_error"] = terminal_mark_error
                progress_result_details["resume_blocked"] = "terminal_persistence_pending"
                progress_result_details["terminal_persistence_pending"] = True
            return Result.err(
                OrchestratorError(
                    message="Failed to persist initial session contract",
                    details=progress_result_details,
                )
            )

        return Result.ok(tracker.with_progress(initial_progress))

    async def execute_precreated_session(
        self,
        seed: Seed,
        tracker: SessionTracker,
        parallel: bool = True,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
        force_sequential_levels: bool = False,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Execute a seed using an already-persisted orchestrator session."""
        exec_id = tracker.execution_id
        start_time = datetime.now(UTC)

        # Control console logging based on debug mode
        from ouroboros.observability.logging import set_console_logging

        set_console_logging(self._debug)

        if tracker.status in (
            SessionStatus.COMPLETED,
            SessionStatus.CANCELLED,
            SessionStatus.FAILED,
        ):
            durable_tracker_result = await self._session_repo.reconstruct_session(
                tracker.session_id
            )
            if durable_tracker_result.is_err:
                return Result.err(
                    OrchestratorError(
                        message="Cannot verify caller-supplied terminal session state",
                        details={
                            "session_id": tracker.session_id,
                            "execution_id": tracker.execution_id,
                            "cause": str(durable_tracker_result.error),
                            "resume_blocked": "terminal_state_unverified",
                        },
                    )
                )
            durable_tracker = durable_tracker_result.value
            if (
                durable_tracker.session_id != tracker.session_id
                or durable_tracker.execution_id != tracker.execution_id
            ):
                return Result.err(
                    OrchestratorError(
                        message="Durable session identity does not match the supplied tracker",
                        details={
                            "session_id": tracker.session_id,
                            "execution_id": tracker.execution_id,
                        },
                    )
                )
            if durable_tracker.status in {
                SessionStatus.COMPLETED,
                SessionStatus.CANCELLED,
                SessionStatus.FAILED,
            }:
                self._cleanup_pre_execution_state(
                    durable_tracker.execution_id,
                    durable_tracker.session_id,
                    session_registered=False,
                )
                await clear_cancellation(durable_tracker.session_id)
                return Result.err(
                    OrchestratorError(
                        message=(
                            "Session is in terminal state "
                            f"{durable_tracker.status.value}, cannot execute"
                        ),
                        details={
                            "session_id": durable_tracker.session_id,
                            "status": durable_tracker.status.value,
                        },
                    )
                )
            tracker = durable_tracker
            exec_id = tracker.execution_id

        raw_contract = tracker.progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)

        # This API may execute only the tracker returned by ``prepare_session``.
        # A legacy/reconstructed tracker with no contract is not a new-session
        # shortcut: it has no Foundation A generation and must fail closed before
        # prompts, tool setup, or any runtime-owned provider are consulted.
        authority_generation, authority_claimed = self._claim_process_local_authority_generation(
            tracker.session_id,
            exec_id,
            raw_contract,
        )
        if authority_claimed:
            return Result.err(
                self._process_local_execution_in_progress_error(
                    tracker.session_id,
                    tracker.execution_id,
                )
            )
        if authority_generation is None:
            if self._process_local_authority_held_elsewhere(
                tracker.session_id,
                tracker.execution_id,
                raw_contract,
            ):
                return Result.err(
                    self._process_local_authority_held_elsewhere_error(
                        tracker.session_id,
                        tracker.execution_id,
                    )
                )
            self._cleanup_pre_execution_state(
                tracker.execution_id,
                tracker.session_id,
                session_registered=False,
            )
            return Result.err(
                self._process_local_resume_unavailable_error(
                    tracker.session_id,
                    tracker.execution_id,
                )
            )
        try:
            await asyncio.to_thread(self._restore_guidance_contract, raw_contract)
            self._execution_guidance_delivery_mode()
        except asyncio.CancelledError:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    messages_processed=0,
                    start_time=start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            self._preserve_process_local_owner_for_retry(
                execution_id=tracker.execution_id,
                session_id=tracker.session_id,
            )
            raise
        except OrchestratorError as exc:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    messages_processed=0,
                    start_time=start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            _, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                error=exc,
            )
            if persistence_pending is not None:
                return persistence_pending
            return Result.err(exc)
        self._execution_contract = dict(raw_contract)

        log.info(
            "orchestrator.runner.execute_started",
            execution_id=exec_id,
            session_id=tracker.session_id,
            seed_id=seed.metadata.seed_id,
            goal=seed.goal[:100],
        )
        try:
            # Register session for cancellation tracking
            self._register_session(exec_id, tracker.session_id)
            if await self._check_startup_cancellation(tracker.session_id):
                return await self._handle_cancellation(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    messages_processed=0,
                    start_time=start_time,
                )

            # Build prompts with strategy. The fat-harness default path must use
            # the profile-backed prompt contract so leaf agents are told to emit
            # schema-valid evidence before the acceptance gate parses it.
            strategy = _strategy_for_seed(seed, fat_harness_mode=self._fat_harness_mode)
            system_prompt = build_system_prompt(
                seed,
                strategy=strategy,
                repo_root=self._effective_cwd(),
                guidance_fragment=self._ensure_new_run_guidance().rendered_fragment,
            )
            await self._record_execution_guidance_injection(
                session_id=tracker.session_id,
                execution_id=exec_id,
                injection_key="start",
            )
            task_prompt = build_task_prompt(seed, strategy=strategy)

            # Get merged tools (strategy tools + MCP tools if configured)
            merged_tools, mcp_provider, tool_catalog = await self._get_merged_tools(
                session_id=tracker.session_id,
                tool_prefix=self._mcp_tool_prefix,
                strategy=strategy,
            )
            await self._emit_run_configuration_resolved(
                execution_id=exec_id,
                session_id=tracker.session_id,
            )

            # Execute with progress display
            messages_processed = 0
            final_message = ""
            success = False

            # Create workflow state tracker for progress display
            from ouroboros.orchestrator.workflow_state import WorkflowStateTracker

            state_tracker = WorkflowStateTracker(
                acceptance_criteria=list(seed.acceptance_criteria),
                goal=seed.goal,
                session_id=tracker.session_id,
                activity_map=strategy.get_activity_map(),
            )

            # Check for fat-harness / parallel execution mode. Fat-harness
            # uses the AC executor even for single-AC or --sequential runs so
            # the evidence gate is never silently bypassed. Investment metadata
            # likewise requires per-AC dispatch so direct whole-seed execution
            # cannot discard difficulty/stakes authority.
            has_investment_metadata = _seed_has_investment_metadata(seed)
            if (
                self._fat_harness_mode
                or force_sequential_levels
                or has_investment_metadata
                or (parallel and len(seed.acceptance_criteria) > 1)
            ):
                parallel_kwargs: dict[str, Any] = {
                    "seed": seed,
                    "exec_id": exec_id,
                    "tracker": tracker,
                    "merged_tools": merged_tools,
                    "tool_catalog": tool_catalog,
                    "system_prompt": system_prompt,
                    "start_time": start_time,
                }
                if externally_satisfied_acs:
                    parallel_kwargs["externally_satisfied_acs"] = externally_satisfied_acs
                if force_sequential_levels or (
                    not parallel and (self._fat_harness_mode or has_investment_metadata)
                ):
                    parallel_kwargs["force_sequential_levels"] = True

                return await self._execute_parallel(**parallel_kwargs)

            from ouroboros.orchestrator.dependency_analyzer import (
                ACNode,
                DependencyGraph,
            )

            direct_graph = DependencyGraph(
                nodes=tuple(
                    ACNode(index=index, content=ac_text(criterion), depends_on=())
                    for index, criterion in enumerate(seed.acceptance_criteria)
                ),
                execution_levels=(tuple(range(len(seed.acceptance_criteria))),)
                if seed.acceptance_criteria
                else (),
            )
            await self._emit_execution_plan_created(
                seed=seed,
                execution_id=exec_id,
                session_id=tracker.session_id,
                execution_plan=direct_graph.to_execution_plan(),
            )
        except asyncio.CancelledError:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    messages_processed=0,
                    start_time=start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            if await self._cleanup_if_durable_terminal(
                session_id=tracker.session_id,
                execution_id=exec_id,
            ):
                raise
            self._preserve_process_local_owner_for_retry(
                execution_id=exec_id,
                session_id=tracker.session_id,
            )
            raise
        except Exception as e:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    messages_processed=0,
                    start_time=start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            terminal_persistence_pending = self._terminal_persistence_pending_from_error(
                session_id=tracker.session_id,
                execution_id=exec_id,
                error=e,
            )
            if terminal_persistence_pending is not None:
                return terminal_persistence_pending
            log.exception(
                "orchestrator.runner.execute_setup_failed",
                execution_id=exec_id,
                error=str(e),
            )
            _, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=tracker.session_id,
                execution_id=exec_id,
                error=e,
            )
            if persistence_pending is not None:
                return persistence_pending
            return Result.err(
                OrchestratorError(
                    message=f"Orchestrator execution failed: {e}",
                    details={"execution_id": exec_id},
                )
            )

        try:
            # Use simple status spinner with log-style output for changes
            from rich.status import Status

            last_tool: str | None = None
            last_completed_count = 0
            runtime_handle: RuntimeHandle | None = None
            recovery_interventions_used = 0
            recovery_personas: list[str] = []
            recoverable_failure_pause: RecoverableFailurePause | None = None

            cancelled_result: Result[OrchestratorResult, OrchestratorError] | None = None

            async def _consume_task_stream(
                *,
                prompt: str,
                resume_handle: RuntimeHandle | None,
                status: Any,
            ) -> RuntimeHandle | None:
                nonlocal cancelled_result
                nonlocal final_message
                nonlocal last_completed_count
                nonlocal last_tool
                nonlocal messages_processed
                nonlocal recoverable_failure_pause
                nonlocal success
                nonlocal tracker

                active_runtime_handle = resume_handle
                self._announce_param_degradations(
                    system_prompt=system_prompt,
                    tools=merged_tools,
                )
                effort_kwargs = await self._route_call_effort(
                    execution_id=exec_id,
                    session_id=tracker.session_id,
                )
                async with aclosing(
                    self._adapter.execute_task(  # type: ignore[type-var]
                        prompt=prompt,
                        tools=merged_tools,
                        system_prompt=system_prompt,
                        resume_handle=active_runtime_handle,
                        **effort_kwargs,
                    )
                ) as message_stream:
                    async for message in message_stream:
                        messages_processed += 1
                        projected = project_runtime_message(message)

                        # Check for cancellation periodically
                        if messages_processed % CANCELLATION_CHECK_INTERVAL == 0:
                            if await self._check_cancellation(tracker.session_id):
                                cancelled_result = await self._handle_cancellation(
                                    session_id=tracker.session_id,
                                    execution_id=exec_id,
                                    messages_processed=messages_processed,
                                    start_time=start_time,
                                )
                                break

                        tracker = await self._update_and_persist_progress(
                            tracker,
                            message,
                            messages_processed,
                            tracker.session_id,
                        )
                        if message.resume_handle is not None:
                            active_runtime_handle = message.resume_handle

                        # Update workflow state tracker
                        state_tracker.process_runtime_message(message)

                        # Print log-style output for tool calls and agent messages
                        if projected.tool_name and projected.tool_name != last_tool:
                            status.stop()
                            self._console.print(f"  [yellow]🔧 {projected.tool_name}[/yellow]")
                            status.start()
                            last_tool = projected.tool_name
                        elif (
                            projected.message_type == "assistant"
                            and projected.content
                            and not projected.tool_name
                        ):
                            # Show agent thinking/reasoning
                            content = projected.content.strip()
                            status.stop()
                            self._console.print(f"  [dim]💭 {content}[/dim]")
                            status.start()

                        # Print when AC is completed
                        current_completed = state_tracker.state.completed_count
                        if current_completed > last_completed_count:
                            status.stop()
                            self._console.print(
                                f"  [green]✓ AC {current_completed} completed[/green]"
                            )
                            status.start()
                            last_completed_count = current_completed

                        # Update status with current activity
                        ac_progress = f"{state_tracker.state.completed_count}/{state_tracker.state.total_count}"
                        tool_info = f" | {projected.tool_name}" if projected.tool_name else ""
                        status.update(
                            f"[bold cyan]AC {ac_progress}{tool_info} | {messages_processed} msgs[/]"
                        )

                        # Emit workflow progress event for TUI
                        # Use exec_id defined at start of function (not execution_id param)
                        progress_data = state_tracker.state.to_tui_message_data(
                            execution_id=exec_id
                        )
                        workflow_event = create_workflow_progress_event(
                            execution_id=exec_id,
                            session_id=tracker.session_id,
                            acceptance_criteria=self._with_execution_node_identity(
                                progress_data["acceptance_criteria"],
                                execution_id=exec_id,
                            ),
                            completed_count=progress_data["completed_count"],
                            total_count=progress_data["total_count"],
                            current_ac_index=progress_data["current_ac_index"],
                            current_phase=progress_data["current_phase"],
                            activity=progress_data["activity"],
                            activity_detail=progress_data["activity_detail"],
                            elapsed_display=progress_data["elapsed_display"],
                            estimated_remaining=progress_data["estimated_remaining"],
                            messages_count=progress_data["messages_count"],
                            tool_calls_count=progress_data["tool_calls_count"],
                            estimated_tokens=progress_data["estimated_tokens"],
                            estimated_cost_usd=progress_data["estimated_cost_usd"],
                            last_update=progress_data.get("last_update"),
                        )
                        await self._event_store.append(workflow_event)

                        tool_event = self._build_tool_called_event(tracker.session_id, message)
                        if tool_event is not None:
                            await self._event_store.append(tool_event)

                        if self._should_emit_progress_event(message, messages_processed):
                            progress_event = self._build_progress_event(
                                tracker.session_id,
                                message,
                                step=messages_processed,
                            )
                            await self._event_store.append(progress_event)

                        # Measure and emit drift periodically
                        if messages_processed % PROGRESS_EMIT_INTERVAL == 0:
                            # Measure and emit drift
                            drift_measurement = DriftMeasurement()
                            drift_metrics = drift_measurement.measure(
                                current_output=message.content,
                                constraint_violations=[],  # TODO: track violations
                                current_concepts=[],  # TODO: extract concepts
                                seed=seed,
                            )
                            drift_event = create_drift_measured_event(
                                execution_id=exec_id,
                                goal_drift=drift_metrics.goal_drift,
                                constraint_drift=drift_metrics.constraint_drift,
                                ontology_drift=drift_metrics.ontology_drift,
                                combined_drift=drift_metrics.combined_drift,
                                is_acceptable=drift_metrics.is_acceptable,
                            )
                            await self._event_store.append(drift_event)

                        # Handle final message
                        if message.is_final:
                            final_message = message.content
                            success = not message.is_error
                            recoverable_failure_pause = self._recoverable_failure_pause(
                                message,
                                now=datetime.now(UTC),
                            )

                return active_runtime_handle

            def _build_recovery_snapshot() -> RecoverySnapshot:
                unfinished = [
                    f"{ac.index}. {ac.content}"
                    for ac in state_tracker.state.acceptance_criteria
                    if ac.status.value != "completed"
                ]
                unfinished_text = "\n".join(unfinished[:5]) or "None"
                problem_context = (
                    f"Goal: {seed.goal}\n"
                    f"Unfinished acceptance criteria:\n{unfinished_text}\n\n"
                    f"Previous final message:\n{final_message[:1000]}"
                )
                current_approach = (
                    "The first run attempted the seed normally and ended without "
                    "satisfying the workflow. Continue from the current repository "
                    "state, but avoid repeating the same failed path."
                )
                return RecoverySnapshot(
                    problem_context=problem_context,
                    current_approach=current_approach,
                    messages_processed=messages_processed,
                    completed_count=state_tracker.state.completed_count,
                    total_count=state_tracker.state.total_count,
                    final_error=final_message,
                    used_personas=tuple(ThinkingPersona(persona) for persona in recovery_personas),
                    interventions_used=recovery_interventions_used,
                )

            with Status(
                f"[bold cyan]Executing: {seed.goal[:50]}...[/]",
                console=self._console,
                spinner="dots",
            ) as status:
                runtime_handle = self._seed_runtime_handle(
                    self._inherited_runtime_handle, tool_catalog=tool_catalog
                )
                runtime_handle = await _consume_task_stream(
                    prompt=task_prompt,
                    resume_handle=runtime_handle,
                    status=status,
                )

                # Same-session recovery is limited to the sequential runner.
                # Parallel execution owns per-AC retry semantics, and resume_session
                # is already a recovery workflow.
                if (
                    cancelled_result is None
                    and not success
                    and recoverable_failure_pause is None
                    and runtime_handle is not None
                ):
                    planner = RecoveryPlanner()
                    recovery_action = planner.plan(_build_recovery_snapshot())
                    if (
                        recovery_action.kind == RecoveryActionKind.INJECT_LATERAL_DIRECTIVE
                        and recovery_action.directive
                        and recovery_action.persona is not None
                    ):
                        recovery_interventions_used += 1
                        recovery_personas.append(recovery_action.persona.value)
                        await self._event_store.append(
                            create_recovery_applied_event(
                                execution_id=exec_id,
                                session_id=tracker.session_id,
                                seed_id=seed.metadata.seed_id,
                                action=recovery_action,
                                messages_processed=messages_processed,
                                completed_count=state_tracker.state.completed_count,
                                total_count=state_tracker.state.total_count,
                            )
                        )
                        status.stop()
                        self._console.print(
                            "[yellow]Recovery: "
                            f"{recovery_action.pattern.value if recovery_action.pattern else 'unknown'} "
                            f"-> {recovery_action.persona.value}[/yellow]"
                        )
                        status.start()
                        runtime_handle = await _consume_task_stream(
                            prompt=recovery_action.directive,
                            resume_handle=runtime_handle,
                            status=status,
                        )

            # If cancelled, return the cancellation result now that the
            # generator has been properly closed via aclosing.
            if cancelled_result is not None:
                return cancelled_result

            # Calculate duration
            duration = (datetime.now(UTC) - start_time).total_seconds()

            durable_terminal_status: SessionStatus | None = None
            completion_summary: dict[str, Any] | None = None
            if success:
                completion_summary = {
                    "final_message": final_message[:500],
                    "messages_processed": messages_processed,
                    **self._task_summary(),
                }
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    requested_status=SessionStatus.COMPLETED,
                    summary=completion_summary,
                    messages_processed=messages_processed,
                )
                success = durable_terminal_status is SessionStatus.COMPLETED
                if not success:
                    final_message = (
                        "Execution result was not persisted because the session was already "
                        f"{durable_terminal_status.value}."
                    )

                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="green" if success else "yellow"),
                        title=(
                            "[green]Execution Completed[/green]"
                            if success
                            else f"[yellow]Execution {durable_terminal_status.value.title()}[/yellow]"
                        ),
                        border_style="green" if success else "yellow",
                    )
                )
            elif recoverable_failure_pause is not None:
                pause_result = await self._session_repo.mark_paused(
                    tracker.session_id,
                    reason=recoverable_failure_pause.reason,
                    resume_hint=recoverable_failure_pause.resume_hint,
                    pause_seconds=recoverable_failure_pause.pause_seconds,
                    resume_after=recoverable_failure_pause.resume_after,
                    pause_kind=recoverable_failure_pause.pause_kind,
                )
                pause_status, pause_pending = await self._resolve_pause_publication(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    pause_result=pause_result,
                    pause=recoverable_failure_pause,
                )
                if pause_pending is not None:
                    return pause_pending
                assert pause_status is not None
                if pause_status is SessionStatus.PAUSED:
                    self._console.print(
                        Panel(
                            Text(final_message[:1000], style="yellow"),
                            title="[yellow]Execution Paused[/yellow]",
                            border_style="yellow",
                        )
                    )
                else:
                    durable_terminal_status = pause_status
                    recoverable_failure_pause = None
                    success = pause_status is SessionStatus.COMPLETED
                    final_message = (
                        "Execution pause was not persisted because the session was already "
                        f"{pause_status.value}."
                    )
            else:
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    requested_status=SessionStatus.FAILED,
                    error_message=final_message,
                    messages_processed=messages_processed,
                )
                success = durable_terminal_status is SessionStatus.COMPLETED
                if durable_terminal_status is not SessionStatus.FAILED:
                    final_message = (
                        "Execution failure was not persisted because the session was already "
                        f"{durable_terminal_status.value}."
                    )

                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="green" if success else "red"),
                        title=(
                            "[green]Execution Completed[/green]"
                            if success
                            else f"[red]Execution {durable_terminal_status.value.title()}[/red]"
                        ),
                        border_style="green" if success else "red",
                    )
                )

            # Mirror terminal state into the execution event stream so
            # single-stream consumers (TUI) detect completion without
            # polling the separate session aggregate.
            terminal_status = (
                "paused"
                if recoverable_failure_pause is not None
                else (
                    durable_terminal_status.value
                    if durable_terminal_status is not None
                    else SessionStatus.FAILED.value
                )
            )
            terminal_event = create_execution_terminal_event(
                execution_id=exec_id,
                session_id=tracker.session_id,
                status=terminal_status,
                summary=completion_summary
                if terminal_status == SessionStatus.COMPLETED.value
                else None,
                error_message=(
                    final_message
                    if terminal_status
                    not in {SessionStatus.COMPLETED.value, SessionStatus.PAUSED.value}
                    else None
                ),
                messages_processed=messages_processed,
                pause_seconds=(
                    recoverable_failure_pause.pause_seconds
                    if recoverable_failure_pause is not None
                    else None
                ),
                resume_after=(
                    recoverable_failure_pause.resume_after
                    if recoverable_failure_pause is not None
                    else None
                ),
                pause_kind=(
                    recoverable_failure_pause.pause_kind
                    if recoverable_failure_pause is not None
                    else None
                ),
                resume_hint=(
                    recoverable_failure_pause.resume_hint
                    if recoverable_failure_pause is not None
                    else None
                ),
            )
            await self._project_execution_outcome(
                execution_id=exec_id,
                session_id=tracker.session_id,
                terminal_status=terminal_status,
                terminal_event=terminal_event,
            )

            log.info(
                "orchestrator.runner.execute_completed",
                execution_id=exec_id,
                session_id=tracker.session_id,
                success=success,
                messages_processed=messages_processed,
                duration_seconds=duration,
            )

            # Clean up session tracking
            if terminal_status != "paused":
                self._retire_process_local_authority(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                )
            else:
                self._release_process_local_authority(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                )
            self._unregister_session(
                exec_id,
                tracker.session_id,
                release_liveness_lease=terminal_status != "paused",
            )
            if terminal_status != "paused":
                await clear_cancellation(tracker.session_id)
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)

            return Result.ok(
                OrchestratorResult(
                    success=success,
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    summary={
                        "goal": seed.goal,
                        "acceptance_criteria_count": len(seed.acceptance_criteria),
                        **self._task_summary(),
                    },
                    messages_processed=messages_processed,
                    final_message=final_message,
                    duration_seconds=duration,
                )
            )

        except asyncio.CancelledError:
            if await is_cancellation_requested(tracker.session_id):
                return await self._handle_cancellation(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                )
            if await self._cleanup_if_durable_terminal(
                session_id=tracker.session_id,
                execution_id=exec_id,
            ):
                raise
            self._preserve_process_local_owner_for_retry(
                session_id=tracker.session_id,
                execution_id=exec_id,
            )
            raise
        except Exception as e:
            log.exception(
                "orchestrator.runner.execute_failed",
                execution_id=exec_id,
                error=str(e),
            )

            terminal_persistence_pending = self._terminal_persistence_pending_from_error(
                session_id=tracker.session_id,
                execution_id=exec_id,
                error=e,
            )
            if terminal_persistence_pending is not None:
                return terminal_persistence_pending
            durable_terminal_status, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=tracker.session_id,
                execution_id=exec_id,
                error=e,
                messages_processed=messages_processed,
            )
            if persistence_pending is not None:
                return persistence_pending
            assert durable_terminal_status is not None
            await self._report_frugality_retrospective(
                execution_id=exec_id,
                session_id=tracker.session_id,
                terminal_status=durable_terminal_status.value,
            )

            return Result.err(
                OrchestratorError(
                    message=f"Orchestrator execution failed: {e}",
                    details={
                        "execution_id": exec_id,
                        "session_id": tracker.session_id,
                        "messages_processed": messages_processed,
                    },
                )
            )
        finally:
            await self._terminate_runtime_handle(
                runtime_handle,
                session_id=tracker.session_id,
                context="execute",
            )

    async def _execute_parallel(
        self,
        seed: Seed,
        exec_id: str,
        tracker: Any,
        merged_tools: list[str],
        tool_catalog: SessionToolCatalog,
        system_prompt: str,
        start_time: datetime,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
        force_sequential_levels: bool = False,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Execute seed with parallel AC execution.

        Analyzes AC dependencies using LLM, then executes independent ACs
        in parallel. ACs with dependencies execute after their dependencies complete.

        Args:
            seed: Seed specification to execute.
            exec_id: Execution ID.
            tracker: Session tracker.
            merged_tools: Available tools.
            system_prompt: System prompt for agents.
            start_time: Execution start time.
            externally_satisfied_acs: Top-level ACs already satisfied by the
                current working tree and therefore skipped for re-execution.
            force_sequential_levels: Preserve --sequential ordering while still
                using the AC executor, primarily for temporary fat-harness opt-in.

        Returns:
            Result containing OrchestratorResult on success.
        """
        from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
        from ouroboros.orchestrator.parallel_executor import (
            ParallelACExecutor,
            render_parallel_completion_message,
            render_parallel_verification_report,
        )

        log.info(
            "orchestrator.runner.parallel_mode_enabled",
            execution_id=exec_id,
            session_id=tracker.session_id,
            ac_count=len(seed.acceptance_criteria),
        )

        # Analyze dependencies
        if force_sequential_levels:
            self._console.print("\n[cyan]Preparing sequential AC execution plan...[/cyan]")
            dependency_graph = DependencyGraph(
                nodes=tuple(
                    ACNode(index=i, content=ac_text(ac), depends_on=tuple(range(i)))
                    for i, ac in enumerate(seed.acceptance_criteria)
                ),
                execution_levels=tuple((i,) for i in range(len(seed.acceptance_criteria))),
            )
        else:
            self._console.print("\n[cyan]Analyzing AC dependencies...[/cyan]")

            analyzer = self._build_dependency_analyzer()
            dep_result = await analyzer.analyze(seed.acceptance_criteria)

            if dep_result.is_err:
                log.warning(
                    "orchestrator.runner.dependency_analysis_failed",
                    execution_id=exec_id,
                    error=str(dep_result.error),
                )
                # Fallback: run all ACs in a single parallel level
                all_indices = tuple(range(len(seed.acceptance_criteria)))
                dependency_graph = DependencyGraph(
                    nodes=tuple(
                        ACNode(index=i, content=ac_text(ac), depends_on=())
                        for i, ac in enumerate(seed.acceptance_criteria)
                    ),
                    execution_levels=(all_indices,) if all_indices else (),
                )
            else:
                dependency_graph = dep_result.value

        execution_plan = dependency_graph.to_execution_plan()

        await self._emit_execution_plan_created(
            seed=seed,
            execution_id=exec_id,
            session_id=tracker.session_id,
            execution_plan=execution_plan,
        )

        # Log execution plan
        log.info(
            "orchestrator.runner.execution_plan",
            execution_id=exec_id,
            total_levels=execution_plan.total_stages,
            levels=execution_plan.execution_levels,
            parallelizable=execution_plan.is_parallelizable,
        )

        self._console.print(
            f"[green]Execution plan: {execution_plan.total_stages} stages, "
            f"parallelizable: {execution_plan.is_parallelizable}[/green]"
        )
        for stage in execution_plan.stages:
            self._console.print(
                f"  Stage {stage.stage_number}: ACs {[idx + 1 for idx in stage.ac_indices]}"
            )

        execution_profile = _execution_profile_for_seed(seed)

        # Cap fan-out to the connected backend's concurrency constraints so a
        # parallel dispatch never stampedes the LLM's rate/quota window (R3).
        effective_workers = self._plan_parallel_workers()
        if effective_workers < self._max_parallel_workers:
            self._console.print(
                f"[yellow]Fan-out capped to {effective_workers} worker(s) for backend "
                f"'{self._adapter.runtime_backend}' (requested {self._max_parallel_workers}). "
                f"Override with OUROBOROS_MAX_CONCURRENCY.[/yellow]"
            )
            log.info(
                "orchestrator.runner.fan_out_capped",
                runtime_backend=self._adapter.runtime_backend,
                requested_workers=self._max_parallel_workers,
                effective_workers=effective_workers,
            )

        # Execute in parallel. Reuse the base effort resolved once in __init__
        # (self._reasoning_effort) so a single runner instance has one consistent
        # effort source across its direct paths and the parallel executor.
        parallel_executor = ParallelACExecutor(
            adapter=self._adapter,
            event_store=self._event_store,
            console=self._console,
            enable_decomposition=self._enable_decomposition,
            decomposition_mode=self._decomposition_mode,
            max_concurrent=effective_workers,
            max_decomposition_depth=self._max_decomposition_depth,
            inherited_runtime_handle=self._inherited_runtime_handle,
            task_cwd=self._effective_cwd(),
            checkpoint_store=self._checkpoint_store,
            execution_profile=execution_profile,
            fat_harness_mode=self._fat_harness_mode,
            reasoning_effort=self._reasoning_effort,
            model_router=self._model_router,
            run_verify_commands=self._run_verify_commands,
            verify_command_timeout_seconds=self._verify_command_timeout_seconds,
            ac_retry_attempts=self._ac_retry_attempts,
            shadow_replay_enabled=self._shadow_replay_enabled,
            session_signal_hub=self._session_signal_hub,
        )

        # Check for cancellation before starting parallel execution
        if await self._check_cancellation(tracker.session_id):
            return await self._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=exec_id,
                messages_processed=0,
                start_time=start_time,
            )

        try:
            parallel_result = await parallel_executor.execute_parallel(
                seed=seed,
                execution_plan=execution_plan,
                session_id=tracker.session_id,
                execution_id=exec_id,
                tools=merged_tools,
                tool_catalog=tool_catalog.tools,
                system_prompt=system_prompt,
                externally_satisfied_acs=externally_satisfied_acs,
            )
        finally:
            # Release any warm worker-pool sessions the runtime holds (e.g. the
            # codex-mcp persistent connection pool). The non-parallel path closes
            # per-turn handles, but the parallel path otherwise leaves the pool to
            # its idle TTL — a process-leak window after every run. Guard on
            # ``iscoroutinefunction`` so this is a no-op for runtimes without a
            # real async ``aclose`` (and so MagicMock test adapters, whose
            # attribute access auto-creates a non-awaitable child, are skipped).
            adapter_aclose = getattr(self._adapter, "aclose", None)
            if inspect.iscoroutinefunction(adapter_aclose):
                await adapter_aclose()

        # Check for cancellation after parallel execution
        if await self._check_cancellation(tracker.session_id):
            return await self._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=exec_id,
                messages_processed=parallel_result.total_messages,
                start_time=start_time,
            )

        # Calculate duration
        duration = (datetime.now(UTC) - start_time).total_seconds()

        # Determine overall success
        success = parallel_result.all_succeeded
        recoverable_failure_pause = None
        if not success:
            recoverable_failure_pause = self._recoverable_failure_pause_from_parallel_result(
                parallel_result,
                now=datetime.now(UTC),
            )

        final_message = render_parallel_completion_message(
            parallel_result,
            len(seed.acceptance_criteria),
        )
        verification_report = render_parallel_verification_report(
            parallel_result,
            len(seed.acceptance_criteria),
            max_decomposition_depth=self._max_decomposition_depth,
        )
        execution_summary = {
            "goal": seed.goal,
            "acceptance_criteria_count": len(seed.acceptance_criteria),
            "parallel_execution": True,
            "success_count": parallel_result.success_count,
            "externally_satisfied_count": parallel_result.externally_satisfied_count,
            "satisfied_count": (
                parallel_result.success_count + parallel_result.externally_satisfied_count
            ),
            "failure_count": parallel_result.failure_count,
            "blocked_count": parallel_result.blocked_count,
            "invalid_count": parallel_result.invalid_count,
            "skipped_count": parallel_result.skipped_count,
            "total_levels": execution_plan.total_stages,
            "max_decomposition_depth": self._max_decomposition_depth,
            "max_parallel_workers": self._max_parallel_workers,
            "effective_parallel_workers": effective_workers,
            "verification_report": verification_report,
            **self._task_summary(),
        }

        durable_terminal_status: SessionStatus | None = None
        if success:
            durable_terminal_status = await self._persist_session_terminal_status(
                session_id=tracker.session_id,
                execution_id=exec_id,
                requested_status=SessionStatus.COMPLETED,
                summary=execution_summary,
                messages_processed=parallel_result.total_messages,
            )
            success = durable_terminal_status is SessionStatus.COMPLETED
            if not success:
                final_message = (
                    "Parallel result was not persisted because the session was already "
                    f"{durable_terminal_status.value}."
                )

            self._console.print(
                Panel(
                    Text(final_message, style="green" if success else "yellow"),
                    title=(
                        "[green]Parallel Execution Completed[/green]"
                        if success
                        else f"[yellow]Parallel Execution {durable_terminal_status.value.title()}[/yellow]"
                    ),
                    border_style="green" if success else "yellow",
                )
            )
        elif recoverable_failure_pause is not None:
            pause_result = await self._session_repo.mark_paused(
                tracker.session_id,
                reason=recoverable_failure_pause.reason,
                resume_hint=recoverable_failure_pause.resume_hint,
                pause_seconds=recoverable_failure_pause.pause_seconds,
                resume_after=recoverable_failure_pause.resume_after,
                pause_kind=recoverable_failure_pause.pause_kind,
            )
            pause_status, pause_pending = await self._resolve_pause_publication(
                session_id=tracker.session_id,
                execution_id=exec_id,
                pause_result=pause_result,
                pause=recoverable_failure_pause,
            )
            if pause_pending is not None:
                return pause_pending
            assert pause_status is not None
            if pause_status is SessionStatus.PAUSED:
                self._console.print(
                    Panel(
                        Text(final_message, style="yellow"),
                        title="[yellow]Parallel Execution Paused[/yellow]",
                        border_style="yellow",
                    )
                )
            else:
                durable_terminal_status = pause_status
                recoverable_failure_pause = None
                success = pause_status is SessionStatus.COMPLETED
                final_message = (
                    "Parallel pause was not persisted because the session was already "
                    f"{pause_status.value}."
                )
        else:
            durable_terminal_status = await self._persist_session_terminal_status(
                session_id=tracker.session_id,
                execution_id=exec_id,
                requested_status=SessionStatus.FAILED,
                error_message=(
                    "Partial failure: "
                    f"{parallel_result.failure_count} failed, "
                    f"{parallel_result.blocked_count} blocked, "
                    f"{parallel_result.invalid_count} invalid"
                ),
                messages_processed=parallel_result.total_messages,
            )
            success = durable_terminal_status is SessionStatus.COMPLETED
            if durable_terminal_status is not SessionStatus.FAILED:
                final_message = (
                    "Parallel failure was not persisted because the session was already "
                    f"{durable_terminal_status.value}."
                )

            self._console.print(
                Panel(
                    Text(final_message, style="green" if success else "yellow"),
                    title=(
                        "[green]Parallel Execution Completed[/green]"
                        if success
                        else f"[yellow]Parallel Execution {durable_terminal_status.value.title()}[/yellow]"
                    ),
                    border_style="green" if success else "yellow",
                )
            )

        terminal_status = (
            "paused"
            if recoverable_failure_pause is not None
            else (
                durable_terminal_status.value
                if durable_terminal_status is not None
                else SessionStatus.FAILED.value
            )
        )
        terminal_event = create_execution_terminal_event(
            execution_id=exec_id,
            session_id=tracker.session_id,
            status=terminal_status,
            summary=(
                execution_summary if terminal_status == SessionStatus.COMPLETED.value else None
            ),
            error_message=(
                final_message
                if terminal_status
                not in {SessionStatus.COMPLETED.value, SessionStatus.PAUSED.value}
                else None
            ),
            messages_processed=parallel_result.total_messages,
            pause_seconds=(
                recoverable_failure_pause.pause_seconds
                if recoverable_failure_pause is not None
                else None
            ),
            resume_after=(
                recoverable_failure_pause.resume_after
                if recoverable_failure_pause is not None
                else None
            ),
            pause_kind=(
                recoverable_failure_pause.pause_kind
                if recoverable_failure_pause is not None
                else None
            ),
            resume_hint=(
                recoverable_failure_pause.resume_hint
                if recoverable_failure_pause is not None
                else None
            ),
        )
        await self._project_execution_outcome(
            execution_id=exec_id,
            session_id=tracker.session_id,
            terminal_status=terminal_status,
            terminal_event=terminal_event,
        )

        log.info(
            "orchestrator.runner.parallel_completed",
            execution_id=exec_id,
            session_id=tracker.session_id,
            success=success,
            success_count=parallel_result.success_count,
            failure_count=parallel_result.failure_count,
            blocked_count=parallel_result.blocked_count,
            invalid_count=parallel_result.invalid_count,
            skipped_count=parallel_result.skipped_count,
            total_messages=parallel_result.total_messages,
            duration_seconds=duration,
        )

        # Clean up session tracking
        if terminal_status != "paused":
            self._retire_process_local_authority(
                session_id=tracker.session_id,
                execution_id=exec_id,
            )
        else:
            self._release_process_local_authority(
                session_id=tracker.session_id,
                execution_id=exec_id,
            )
        self._unregister_session(
            exec_id,
            tracker.session_id,
            release_liveness_lease=terminal_status != "paused",
        )
        if terminal_status != "paused":
            await clear_cancellation(tracker.session_id)
        if self._task_workspace is not None:
            release_lock(self._task_workspace.lock_path)

        return Result.ok(
            OrchestratorResult(
                success=success,
                session_id=tracker.session_id,
                execution_id=exec_id,
                summary=execution_summary,
                messages_processed=parallel_result.total_messages,
                final_message=final_message,
                duration_seconds=duration,
            )
        )

    async def resume_session(
        self,
        session_id: str,
        seed: Seed,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Resume a paused or failed session.

        Reconstructs session state from events and continues execution.

        Args:
            session_id: Session to resume.
            seed: Original seed (needed for prompt building).

        Returns:
            Result containing OrchestratorResult on success.
        """
        # Control console logging based on debug mode
        from ouroboros.observability.logging import set_console_logging

        set_console_logging(self._debug)

        log.info(
            "orchestrator.runner.resume_started",
            session_id=session_id,
        )

        # Reconstruct session
        session_result = await self._session_repo.reconstruct_session(session_id)

        if session_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to reconstruct session: {session_result.error}",
                    details={"session_id": session_id},
                )
            )

        tracker = session_result.value

        # Check if session can be resumed
        if tracker.status in (
            SessionStatus.COMPLETED,
            SessionStatus.CANCELLED,
            SessionStatus.FAILED,
        ):
            self._pending_lifecycle_intents.pop(session_id, None)
            self._cleanup_pre_execution_state(
                tracker.execution_id,
                session_id,
                session_registered=False,
            )
            await clear_cancellation(session_id)
            return Result.err(
                OrchestratorError(
                    message=f"Session is in terminal state {tracker.status.value}, cannot resume",
                    details={"session_id": session_id, "status": tracker.status.value},
                )
            )

        pending_lifecycle = await self._retry_pending_lifecycle_intent(tracker)
        if pending_lifecycle is not None:
            return pending_lifecycle

        raw_contract = tracker.progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)
        if not isinstance(raw_contract, Mapping):
            error = await self._mark_process_local_resume_unavailable(
                session_id=session_id,
                execution_id=tracker.execution_id,
            )
            self._cleanup_pre_execution_state(
                tracker.execution_id,
                session_id,
                session_registered=False,
                retire_authority=False,
            )
            return Result.err(error)

        # Persisted process-local authority is arbitrated before any policy
        # derived from the current runner or seed. A current policy gate must
        # never mask that the exact paused owner has disappeared.
        authority_generation, authority_claimed = self._claim_process_local_authority_generation(
            session_id,
            tracker.execution_id,
            raw_contract,
        )
        if authority_claimed:
            return Result.err(
                self._process_local_execution_in_progress_error(
                    session_id,
                    tracker.execution_id,
                )
            )
        if authority_generation is None:
            if self._process_local_authority_held_elsewhere(
                session_id,
                tracker.execution_id,
                raw_contract,
            ):
                return Result.err(
                    self._process_local_authority_held_elsewhere_error(
                        session_id,
                        tracker.execution_id,
                    )
                )
            error = await self._mark_process_local_resume_unavailable(
                session_id=session_id,
                execution_id=tracker.execution_id,
            )
            self._cleanup_pre_execution_state(
                tracker.execution_id,
                session_id,
                session_registered=False,
                retire_authority=False,
            )
            return Result.err(error)

        # A RUNNING tracker with a current Foundation A contract belongs to an
        # active process while its early liveness lease is held.  If the lease
        # is gone and this process has no live registry capability, the prior
        # owner has crashed or exited and Foundation A must terminally reject it
        # rather than leave a restartable-looking RUNNING session stranded.
        if tracker.status != SessionStatus.PAUSED:
            # A previous cancellation attempt may have failed its durable
            # write after the worker stopped. Its request remains live so this
            # exact owner can retry terminalization before rejecting RUNNING.
            try:
                self._register_session(tracker.execution_id, session_id)
                if await self._check_startup_cancellation(session_id):
                    return await self._handle_cancellation(
                        session_id=session_id,
                        execution_id=tracker.execution_id,
                        messages_processed=tracker.messages_processed,
                        start_time=datetime.now(UTC),
                    )
            except asyncio.CancelledError:
                self._release_process_local_authority(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                )
                self._unregister_session(
                    tracker.execution_id,
                    session_id,
                    release_liveness_lease=False,
                )
                if self._task_workspace is not None:
                    release_lock(self._task_workspace.lock_path)
                raise
            except Exception as exc:
                self._release_process_local_authority(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                )
                self._unregister_session(
                    tracker.execution_id,
                    session_id,
                    release_liveness_lease=False,
                )
                if self._task_workspace is not None:
                    release_lock(self._task_workspace.lock_path)
                return Result.err(
                    OrchestratorError(
                        message=f"Failed to inspect live process-local session: {exc}",
                        details={
                            "session_id": session_id,
                            "execution_id": tracker.execution_id,
                        },
                    )
                )
            self._release_process_local_authority(
                session_id=session_id,
                execution_id=tracker.execution_id,
            )
            self._unregister_session(
                tracker.execution_id,
                session_id,
                release_liveness_lease=False,
            )
            return Result.err(
                OrchestratorError(
                    message=f"Session is not paused and cannot resume ({tracker.status.value})",
                    details={
                        "session_id": session_id,
                        "status": tracker.status.value,
                        "resume_blocked": "session_not_paused",
                    },
                )
            )

        if self._fat_harness_mode:
            self._release_process_local_authority(
                session_id=session_id,
                execution_id=tracker.execution_id,
            )
            return Result.err(
                OrchestratorError(
                    message=(
                        "Resume is blocked because this resume path cannot enforce "
                        "typed evidence plus verifier PASS; restart the "
                        "run so each AC goes through the fat-harness acceptance gate."
                    ),
                    details={
                        "session_id": session_id,
                        "execution_id": tracker.execution_id,
                        "fat_harness_mode": True,
                        "resume_blocked": "typed_evidence_gate_required",
                    },
                )
            )

        if _seed_has_investment_metadata(seed):
            self._release_process_local_authority(
                session_id=session_id,
                execution_id=tracker.execution_id,
            )
            return Result.err(
                OrchestratorError(
                    message=(
                        "Resume is blocked because this resume path cannot preserve "
                        "per-AC investment authority; restart the run so each AC goes "
                        "through the investment-aware AC executor."
                    ),
                    details={
                        "session_id": session_id,
                        "execution_id": tracker.execution_id,
                        "investment_metadata_present": True,
                        "resume_blocked": "investment_authority_required",
                    },
                )
            )

        try:
            execution_contract_changed = await asyncio.to_thread(
                self._restore_execution_contract,
                tracker.progress,
                seed=seed,
                authority_generation=authority_generation,
            )
            self._execution_guidance_delivery_mode()
        except asyncio.CancelledError:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=tracker.messages_processed,
                    start_time=tracker.start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            if await self._cleanup_if_durable_terminal(
                session_id=session_id,
                execution_id=tracker.execution_id,
            ):
                raise
            self._preserve_process_local_owner_for_retry(
                execution_id=tracker.execution_id,
                session_id=session_id,
            )
            raise
        except OrchestratorError as exc:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=tracker.messages_processed,
                    start_time=tracker.start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            _, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=session_id,
                execution_id=tracker.execution_id,
                error=exc,
            )
            if persistence_pending is not None:
                return persistence_pending
            return Result.err(exc)
        try:
            # Register session for cancellation tracking
            self._register_session(tracker.execution_id, session_id)

            # A public cancellation may have reserved this previously-paused
            # owner between resume arbitration and registration.  Honor that
            # request before restoring handles, assembling tools, or invoking
            # any runtime effect.
            if await self._check_startup_cancellation(session_id):
                return await self._handle_cancellation(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=tracker.messages_processed,
                    start_time=datetime.now(UTC),
                )

            if execution_contract_changed and self._execution_contract is not None:
                contract_progress = {
                    EXECUTION_CONTRACT_PROGRESS_KEY: self._execution_contract,
                    "messages_processed": tracker.messages_processed,
                }
                persisted_contract = await self._session_repo.track_progress(
                    session_id,
                    contract_progress,
                )
                if persisted_contract.is_err:
                    raise OrchestratorError(
                        message="Failed to persist explicit resume routing override",
                        details={
                            "session_id": session_id,
                            "cause": str(persisted_contract.error),
                        },
                    )
                tracker = tracker.with_progress(contract_progress)

            self._console.print(
                f"[cyan]Resuming session {session_id}[/cyan]\n"
                f"[dim]Previously processed: {tracker.messages_processed} messages[/dim]"
            )

            # Build resume prompt
            system_prompt = build_system_prompt(
                seed,
                repo_root=self._effective_cwd(),
                guidance_fragment=self._ensure_new_run_guidance().rendered_fragment,
            )
            await self._record_execution_guidance_injection(
                session_id=session_id,
                execution_id=tracker.execution_id,
                injection_key=f"resume:{tracker.messages_processed}",
            )
            resume_prompt = f"""Continue executing the task from where you left off.

{build_task_prompt(seed)}

Note: This is a resumed session. Please continue from where execution was interrupted.
"""
            # Get runtime resume state if stored
            runtime_handle = self._deserialize_runtime_handle(tracker.progress)
            runtime_handle = self._force_runtime_handle_permission(runtime_handle)
            self._validate_runtime_handle_backend(runtime_handle)
            self._validate_bound_runtime_resume_identity(tracker.progress, runtime_handle)
            self._validate_resume_handle_execution_identity(runtime_handle)
            if self._task_workspace is not None and "workspace" not in tracker.progress:
                await self._persist_session_progress(
                    session_id,
                    {"workspace": self._task_workspace.to_progress_dict()},
                )

            # Get merged tools (DEFAULT_TOOLS + MCP tools if configured)
            merged_tools, mcp_provider, tool_catalog = await self._get_merged_tools(
                session_id=session_id,
                tool_prefix=self._mcp_tool_prefix,
            )
            runtime_handle = self._seed_runtime_handle(runtime_handle, tool_catalog=tool_catalog)

            start_time = datetime.now(UTC)
            messages_processed = tracker.messages_processed
            final_message = ""
            success = False
            recoverable_resume_failure: RecoverableFailurePause | None = None

            # Create workflow state tracker for progress display
            from ouroboros.orchestrator.workflow_state import WorkflowStateTracker

            resume_strategy = get_strategy(seed.task_type)
            state_tracker = WorkflowStateTracker(
                acceptance_criteria=list(seed.acceptance_criteria),
                goal=seed.goal,
                session_id=session_id,
                activity_map=resume_strategy.get_activity_map(),
            )
            await self._replay_workflow_state(session_id, state_tracker)
        except asyncio.CancelledError:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=tracker.messages_processed,
                    start_time=tracker.start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            self._preserve_process_local_owner_for_retry(
                execution_id=tracker.execution_id,
                session_id=session_id,
            )
            raise
        except Exception as e:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=tracker.messages_processed,
                    start_time=tracker.start_time,
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            terminal_persistence_pending = self._terminal_persistence_pending_from_error(
                session_id=session_id,
                execution_id=tracker.execution_id,
                error=e,
            )
            if terminal_persistence_pending is not None:
                return terminal_persistence_pending
            log.exception(
                "orchestrator.runner.resume_setup_failed",
                session_id=session_id,
                error=str(e),
            )
            _, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=session_id,
                execution_id=tracker.execution_id,
                error=e,
                messages_processed=tracker.messages_processed,
            )
            if persistence_pending is not None:
                return persistence_pending
            return Result.err(
                OrchestratorError(
                    message=f"Session resume failed: {e}",
                    details={"session_id": session_id},
                )
            )

        try:
            # Use simple status spinner with log-style output for changes
            from rich.status import Status

            last_tool: str | None = None
            last_completed_count = state_tracker.state.completed_count
            live_runtime_handle = runtime_handle
            cancelled_result: Result[OrchestratorResult, OrchestratorError] | None = None

            with Status(
                f"[bold cyan]Resuming: {seed.goal[:50]}...[/]",
                console=self._console,
                spinner="dots",
            ) as status:
                self._announce_param_degradations(
                    system_prompt=system_prompt,
                    tools=merged_tools,
                )
                effort_kwargs = await self._route_call_effort(
                    execution_id=None,
                    session_id=session_id,
                )
                async with aclosing(
                    self._adapter.execute_task(  # type: ignore[type-var]
                        prompt=resume_prompt,
                        tools=merged_tools,
                        system_prompt=system_prompt,
                        resume_handle=runtime_handle,
                        **effort_kwargs,
                    )
                ) as message_stream:
                    async for message in message_stream:
                        messages_processed += 1
                        projected = project_runtime_message(message)

                        # Check for cancellation periodically
                        if messages_processed % CANCELLATION_CHECK_INTERVAL == 0:
                            if await self._check_cancellation(session_id):
                                cancelled_result = await self._handle_cancellation(
                                    session_id=session_id,
                                    execution_id=tracker.execution_id,
                                    messages_processed=messages_processed,
                                    start_time=start_time,
                                )
                                break

                        tracker = await self._update_and_persist_progress(
                            tracker,
                            message,
                            messages_processed,
                            session_id,
                        )
                        if message.resume_handle is not None:
                            live_runtime_handle = message.resume_handle

                        # Update workflow state tracker
                        state_tracker.process_runtime_message(message)

                        # Print log-style output for tool calls and agent messages
                        if projected.tool_name and projected.tool_name != last_tool:
                            status.stop()
                            self._console.print(f"  [yellow]🔧 {projected.tool_name}[/yellow]")
                            status.start()
                            last_tool = projected.tool_name
                        elif (
                            projected.message_type == "assistant"
                            and projected.content
                            and not projected.tool_name
                        ):
                            # Show agent thinking/reasoning
                            content = projected.content.strip()
                            status.stop()
                            self._console.print(f"  [dim]💭 {content}[/dim]")
                            status.start()

                        # Print when AC is completed
                        current_completed = state_tracker.state.completed_count
                        if current_completed > last_completed_count:
                            status.stop()
                            self._console.print(
                                f"  [green]✓ AC {current_completed} completed[/green]"
                            )
                            status.start()
                            last_completed_count = current_completed

                        # Update status with current activity
                        ac_progress = f"{state_tracker.state.completed_count}/{state_tracker.state.total_count}"
                        tool_info = f" | {projected.tool_name}" if projected.tool_name else ""
                        status.update(
                            f"[bold cyan]AC {ac_progress}{tool_info} | {messages_processed} msgs[/]"
                        )

                        # Emit workflow progress event for TUI
                        progress_data = state_tracker.state.to_tui_message_data(
                            execution_id=session_id  # Use session_id as execution_id for resume
                        )
                        workflow_event = create_workflow_progress_event(
                            execution_id=session_id,
                            session_id=session_id,
                            acceptance_criteria=self._with_execution_node_identity(
                                progress_data["acceptance_criteria"],
                                execution_id=session_id,
                            ),
                            completed_count=progress_data["completed_count"],
                            total_count=progress_data["total_count"],
                            current_ac_index=progress_data["current_ac_index"],
                            current_phase=progress_data["current_phase"],
                            activity=progress_data["activity"],
                            activity_detail=progress_data["activity_detail"],
                            elapsed_display=progress_data["elapsed_display"],
                            estimated_remaining=progress_data["estimated_remaining"],
                            messages_count=progress_data["messages_count"],
                            tool_calls_count=progress_data["tool_calls_count"],
                            estimated_tokens=progress_data["estimated_tokens"],
                            estimated_cost_usd=progress_data["estimated_cost_usd"],
                            last_update=progress_data.get("last_update"),
                        )
                        await self._event_store.append(workflow_event)

                        tool_event = self._build_tool_called_event(session_id, message)
                        if tool_event is not None:
                            await self._event_store.append(tool_event)

                        if self._should_emit_progress_event(message, messages_processed):
                            progress_event = self._build_progress_event(
                                session_id,
                                message,
                                step=messages_processed,
                            )
                            await self._event_store.append(progress_event)

                        if message.is_final:
                            final_message = message.content
                            success = not message.is_error
                            recoverable_resume_failure = self._recoverable_failure_pause(
                                message,
                                now=datetime.now(UTC),
                            )

            if cancelled_result is not None:
                return cancelled_result

            duration = (datetime.now(UTC) - start_time).total_seconds()

            durable_terminal_status: SessionStatus | None = None
            if success:
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    requested_status=SessionStatus.COMPLETED,
                    summary={
                        "messages_processed": messages_processed,
                        **self._task_summary(),
                    },
                    messages_processed=messages_processed,
                )
                success = durable_terminal_status is SessionStatus.COMPLETED
                if not success:
                    final_message = (
                        "Resumed execution result was not persisted because the session was already "
                        f"{durable_terminal_status.value}."
                    )
                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="green" if success else "yellow"),
                        title=(
                            "[green]Resumed Execution Completed[/green]"
                            if success
                            else f"[yellow]Resumed Execution {durable_terminal_status.value.title()}[/yellow]"
                        ),
                        border_style="green" if success else "yellow",
                    )
                )
            elif recoverable_resume_failure is not None:
                pause_result = await self._session_repo.mark_paused(
                    session_id,
                    reason=recoverable_resume_failure.reason,
                    resume_hint=recoverable_resume_failure.resume_hint,
                    pause_seconds=recoverable_resume_failure.pause_seconds,
                    resume_after=recoverable_resume_failure.resume_after,
                    pause_kind=recoverable_resume_failure.pause_kind,
                )
                pause_status, pause_pending = await self._resolve_pause_publication(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    pause_result=pause_result,
                    pause=recoverable_resume_failure,
                )
                if pause_pending is not None:
                    return pause_pending
                assert pause_status is not None
                if pause_status is SessionStatus.PAUSED:
                    self._console.print(
                        Panel(
                            Text(final_message[:1000], style="yellow"),
                            title="[yellow]Resumed Execution Paused[/yellow]",
                            border_style="yellow",
                        )
                    )
                else:
                    durable_terminal_status = pause_status
                    recoverable_resume_failure = None
                    success = pause_status is SessionStatus.COMPLETED
                    final_message = (
                        "Resumed execution pause was not persisted because the session was already "
                        f"{pause_status.value}."
                    )
            else:
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    requested_status=SessionStatus.FAILED,
                    error_message=final_message,
                    messages_processed=messages_processed,
                )
                success = durable_terminal_status is SessionStatus.COMPLETED
                if durable_terminal_status is not SessionStatus.FAILED:
                    final_message = (
                        "Resumed execution failure was not persisted because the session was already "
                        f"{durable_terminal_status.value}."
                    )
                self._console.print(
                    Panel(
                        Text(final_message[:1000], style="green" if success else "red"),
                        title=(
                            "[green]Resumed Execution Completed[/green]"
                            if success
                            else f"[red]Resumed Execution {durable_terminal_status.value.title()}[/red]"
                        ),
                        border_style="green" if success else "red",
                    )
                )

            # Mirror terminal state into execution stream for TUI.
            terminal_status = (
                "paused"
                if recoverable_resume_failure is not None
                else (
                    durable_terminal_status.value
                    if durable_terminal_status is not None
                    else SessionStatus.FAILED.value
                )
            )
            terminal_event = create_execution_terminal_event(
                execution_id=tracker.execution_id,
                session_id=session_id,
                status=terminal_status,
                error_message=(
                    final_message
                    if terminal_status
                    not in {SessionStatus.COMPLETED.value, SessionStatus.PAUSED.value}
                    else None
                ),
                messages_processed=messages_processed,
                pause_seconds=(
                    recoverable_resume_failure.pause_seconds
                    if recoverable_resume_failure is not None
                    else None
                ),
                resume_after=(
                    recoverable_resume_failure.resume_after
                    if recoverable_resume_failure is not None
                    else None
                ),
                pause_kind=(
                    recoverable_resume_failure.pause_kind
                    if recoverable_resume_failure is not None
                    else None
                ),
                resume_hint=(
                    recoverable_resume_failure.resume_hint
                    if recoverable_resume_failure is not None
                    else None
                ),
            )
            await self._project_execution_outcome(
                execution_id=tracker.execution_id,
                session_id=session_id,
                terminal_status=terminal_status,
                terminal_event=terminal_event,
            )

            log.info(
                "orchestrator.runner.resume_completed",
                session_id=session_id,
                success=success,
                messages_processed=messages_processed,
                duration_seconds=duration,
            )

            # A paused owner has not acknowledged a cancellation that may have
            # arrived after its final execution checkpoint. Preserve that
            # marker so the next resume entry point terminalizes before effects.
            if terminal_status != "paused":
                await clear_cancellation(session_id)

            # Clean up session tracking
            if terminal_status != "paused":
                self._retire_process_local_authority(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                )
            else:
                self._release_process_local_authority(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                )
            self._unregister_session(
                tracker.execution_id,
                session_id,
                release_liveness_lease=terminal_status != "paused",
            )
            if self._task_workspace is not None:
                release_lock(self._task_workspace.lock_path)

            return Result.ok(
                OrchestratorResult(
                    success=success,
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    summary={"resumed": True, **self._task_summary()},
                    messages_processed=messages_processed,
                    final_message=final_message,
                    duration_seconds=duration,
                )
            )

        except asyncio.CancelledError:
            if await is_cancellation_requested(session_id):
                return await self._handle_cancellation(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                )
            if await self._cleanup_if_durable_terminal(
                session_id=session_id,
                execution_id=tracker.execution_id,
            ):
                raise
            self._preserve_process_local_owner_for_retry(
                session_id=session_id,
                execution_id=tracker.execution_id,
            )
            raise
        except Exception as e:
            log.exception(
                "orchestrator.runner.resume_failed",
                session_id=session_id,
                error=str(e),
            )

            terminal_persistence_pending = self._terminal_persistence_pending_from_error(
                session_id=session_id,
                execution_id=tracker.execution_id,
                error=e,
            )
            if terminal_persistence_pending is not None:
                return terminal_persistence_pending
            durable_terminal_status, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=session_id,
                execution_id=tracker.execution_id,
                error=e,
                messages_processed=messages_processed,
            )
            if persistence_pending is not None:
                return persistence_pending
            assert durable_terminal_status is not None
            await self._report_frugality_retrospective(
                execution_id=tracker.execution_id,
                session_id=session_id,
                terminal_status=durable_terminal_status.value,
            )

            return Result.err(
                OrchestratorError(
                    message=f"Session resume failed: {e}",
                    details={"session_id": session_id},
                )
            )
        finally:
            await self._terminate_runtime_handle(
                live_runtime_handle,
                session_id=session_id,
                context="resume",
            )


__all__ = [
    "ExecutionCancelledError",
    "OrchestratorError",
    "OrchestratorResult",
    "OrchestratorRunner",
    "build_system_prompt",
    "build_task_prompt",
    "clear_cancellation",
    "get_cancellation_request",
    "get_pending_cancellations",
    "is_cancellation_requested",
    "request_cancellation",
]
