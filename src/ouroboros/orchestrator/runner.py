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
from collections.abc import Iterable, Mapping
from contextlib import aclosing
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, cast
from uuid import uuid4

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ouroboros.backends import backend_supports_tool_envelope, get_backend_capability
from ouroboros.config import (
    MAX_USAGE_LIMIT_PAUSE_SECONDS,
    get_llm_model_for_role,
    get_usage_limit_pause_seconds,
)
from ouroboros.core.conductor import ConductorDirective
from ouroboros.core.errors import ConfigError, OuroborosError, PersistenceError
from ouroboros.core.execution_preferences import (
    ResolvedExecutionPreferences,
    execution_preferences_from_contract,
    resolve_execution_preferences,
)
from ouroboros.core.project_identity import (
    ManagedProjectOwnershipError,
    ManagedProjectScopeError,
    ProjectIdentity,
    ProjectIdentityError,
    ProjectIdentityUnavailableError,
    active_publication_evidence_sink,
    publication_evidence_sink,
    resolve_managed_project_identity,
    resolve_project_identity,
    resolve_project_identity_for_publication,
)
from ouroboros.core.seed import AcceptanceCriterionSpec, ac_text, ac_texts
from ouroboros.core.seed_contract import SeedContract
from ouroboros.core.seed_contract_prompt import (
    render_auto_recursion_guard,
    render_seed_contract_for_execution,
)
from ouroboros.core.types import Result
from ouroboros.core.worktree import TaskWorkspace, heartbeat_lock, release_lock
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    DEFAULT_TOOLS,
    AgentMessage,
    AgentRuntime,
    ParamSupport,
    RuntimeHandle,
    resolve_worker_cwd,
)
from ouroboros.orchestrator.adaptive_concurrency import adaptive_concurrency_policy
from ouroboros.orchestrator.backend_limits import (
    BackendConcurrencyLimits,
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
from ouroboros.orchestrator.decomposition_limits import (
    DEFAULT_MAX_DECOMPOSITION_DEPTH,
    has_durable_decomposition_replay,
    validate_max_decomposition_depth,
)
from ouroboros.orchestrator.events import (
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
    _authenticate_process_local_prepared_contract,
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
    _seal_process_local_prepared_contract,
    collect_cancellation_acceptance_plan,
    collect_terminal_acceptance_plan,
    constructor_model_contract,
    request_process_local_cancellation,
    runtime_effect_capabilities_contract,
    runtime_execution_identity_contract,
    runtime_execution_proves_effective_model,
    valid_constructor_model_contract,
    valid_process_local_authority_contract,
    valid_runtime_effect_capabilities_contract,
    valid_runtime_execution_identity_contract,
)
from ouroboros.orchestrator.execution_event_replay import (
    replay_execution_events_chronologically,
)
from ouroboros.orchestrator.execution_guidance import (
    ExecutionGuidanceBundle,
    resolve_execution_guidance,
)
from ouroboros.orchestrator.execution_runtime_scope import (
    ExecutionNodeIdentity,
    build_ac_runtime_scope,
)
from ouroboros.orchestrator.execution_semantics import (
    CURRENT_EXECUTION_SEMANTICS_VERSION,
    migrated_pre_verify_shell_execution_semantics,
    pre_adaptive_execution_semantics_rejection,
    valid_execution_semantics_contract,
    valid_legacy_preflight_execution_semantics_contract,
)
from ouroboros.orchestrator.execution_strategy import ExecutionStrategy, get_strategy
from ouroboros.orchestrator.failure_taxonomy import FailureClass
from ouroboros.orchestrator.legacy_identity import (
    legacy_task_workspace_identity,
    note_legacy_identity_path,
)
from ouroboros.orchestrator.mcp_tools import (
    MCPToolProvider,
    SessionToolCatalog,
    assemble_session_tool_catalog,
    enumerate_runtime_builtin_tool_definitions,
    serialize_tool_catalog,
)
from ouroboros.orchestrator.parallel_executor_models import CoordinatorQuotaPause
from ouroboros.orchestrator.policy import (
    PolicyContext,
    PolicyDecision,
    PolicyExecutionPhase,
    PolicySessionRole,
    evaluate_capability_policy,
)
from ouroboros.orchestrator.profile_loader import ExecutionProfile, ProfileError, load_profile
from ouroboros.orchestrator.profile_strategy import ProfileBackedStrategy
from ouroboros.orchestrator.recoverable_failure import (
    is_usage_limit_pause_message,
    project_failure_metadata,
    retry_duration_seconds_from_message,
    retry_duration_seconds_from_metadata,
)
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
    ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY,
    SESSION_RUNTIME_IDENTITY_PROGRESS_KEY,
    SESSION_START_IDENTITY_PROGRESS_KEY,
    SessionRepository,
    SessionStatus,
    SessionTracker,
    runtime_resume_identity_from_payload,
)
from ouroboros.orchestrator.verify_shell import (
    capture_verify_shell_identity,
    resolve_verify_shell,
)
from ouroboros.orchestrator.workflow_state import ActivityType, coerce_ac_marker_update
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import acceptance_generation_id_for_session
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
    from ouroboros.orchestrator.route_escalation import RouteEscalationDecision
    from ouroboros.orchestrator.route_policy import RouteCandidate
    from ouroboros.orchestrator.synapse import SessionSignalHub
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)
_DIRECT_ROUTE_PAUSE_REPLAY_PAGE_SIZE = 64
_MAX_EXECUTION_STRATEGY_TOOLS = 256
_MAX_EXECUTION_STRATEGY_TEXT_CHARS = 100_000
_MAX_EXECUTION_TOOL_CATALOG_CHARS = 1_000_000
_MAX_EXECUTION_ALLOWED_TOOLS = 1024
_MAX_EXECUTION_CONTEXT_FRAGMENT_CHARS = 100_000
_MAX_EXECUTION_PROFILE_CHARS = 100_000
_MAX_EXECUTION_RUNTIME_HANDLE_CHARS = 1_000_000
_DIRECT_ROUTE_OBSERVATION_KEYS = frozenset(
    {
        "schema_version",
        "execution_id",
        "session_id",
        "root_ac_index",
        "call_site",
        "observation",
        "decision",
        "human_handoff_required",
        "final_acceptance_declared",
    }
)


def _mapping_has_exact_keys(value: object, expected: frozenset[str]) -> bool:
    """Inspect at most one key beyond a finite durable-contract schema."""

    if not isinstance(value, Mapping):
        return False
    try:
        iterator = iter(value)
    except Exception:
        return False
    seen: set[str] = set()
    for index in range(len(expected) + 1):
        try:
            key = next(iterator)
        except StopIteration:
            return len(seen) == len(expected)
        except Exception:
            return False
        if index >= len(expected) or type(key) is not str or key not in expected or key in seen:
            return False
        seen.add(key)
    return False


@dataclass(frozen=True, slots=True)
class _PersistedExecutionStrategy:
    """Effect-bearing prompt/tool strategy restored without live config reads."""

    tools: tuple[str, ...]
    system_prompt_fragment: str
    task_prompt_suffix: str
    activity_map: tuple[tuple[str, ActivityType], ...]

    def get_tools(self) -> list[str]:
        return list(self.tools)

    def get_system_prompt_fragment(self) -> str:
        return self.system_prompt_fragment

    def get_task_prompt_suffix(self) -> str:
        return self.task_prompt_suffix

    def get_activity_map(self) -> dict[str, ActivityType]:
        return dict(self.activity_map)


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
    coordinator_owner: CoordinatorQuotaPause | None = None


@dataclass(frozen=True, slots=True)
class _DirectRouteResumeState:
    """Exact nonterminal Routing D state owned by the direct runner."""

    episode_id: str
    attempt_index: int
    prior_route_ids: tuple[str, ...]
    candidate: RouteCandidate


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
    acceptance_finalizations: list[dict[str, Any]] | None = None


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
    context_pack_enabled: bool | None = None,
    resolved_context_pack_fragment: str | None = None,
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
        context_pack_enabled: Resolved context-pack mode. ``None`` preserves
            lazy config resolution for direct helper callers; runner-owned
            execution passes the durable contract value explicitly.
        resolved_context_pack_fragment: Exact context-pack text frozen before
            session publication. When provided (including the empty string),
            prompt construction never scans the mutable workspace again.

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

    context_pack_fragment = (
        resolved_context_pack_fragment
        if resolved_context_pack_fragment is not None
        else _context_pack_fragment(
            seed,
            repo_root,
            context_pack_enabled=context_pack_enabled,
        )
    )
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
    *,
    context_pack_enabled: bool | None = None,
) -> str:
    """Render the deterministic context pack fragment, or empty string.

    Root resolution happens before the config lookup so the common
    no-repo path (unit tests, greenfield seeds) never loads config and
    never touches the filesystem scanner.
    """
    root = _resolve_context_pack_root(seed, repo_root)
    if root is None:
        return ""

    if context_pack_enabled is None:
        from ouroboros.config import get_context_pack_enabled

        context_pack_enabled = get_context_pack_enabled()
    if not context_pack_enabled:
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
EXECUTION_CONTRACT_VERSION = 9
EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "foundation_a_authority",
        "execution_preferences",
        "execution_semantics",
        "execution_inputs",
        "model_routing",
        "frugality_proof",
        "guidance",
        "resume",
    }
)
PRE_ROUTE_ADMISSION_EXECUTION_CONTRACT_VERSION = 2
PRE_REQUESTED_TIER_EXECUTION_CONTRACT_VERSION = 3
PRE_EXECUTION_SEMANTICS_EXECUTION_CONTRACT_VERSION = 4
PRE_EXECUTION_INPUTS_EXECUTION_CONTRACT_VERSION = 5
PRE_RESOLVED_EFFECT_INPUTS_EXECUTION_CONTRACT_VERSION = 6
PRE_DURABLE_PAUSE_POLICY_EXECUTION_CONTRACT_VERSION = 7
PRE_RUNTIME_EFFECT_CAPABILITIES_EXECUTION_CONTRACT_VERSION = 8
FRUGALITY_PROOF_PROTOCOL_VERSION = 1
_MISSING = object()
EXECUTION_CONTRACT_PROGRESS_KEY = "execution_contract"
FORCED_EXECUTION_PERMISSION_MODE = "bypassPermissions"


def _require_exact_execution_contract_v9(raw_contract: object) -> Mapping[str, Any]:
    """Return a current contract only when its complete top-level shape is canonical."""
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

    actual_top_level_keys = frozenset(raw_contract)
    if actual_top_level_keys != EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS:
        missing_keys = sorted(EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS - actual_top_level_keys)
        unknown_keys = sorted(
            key if isinstance(key, str) else repr(key)
            for key in actual_top_level_keys - EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS
        )
        raise OrchestratorError(
            message="Cannot resume with an invalid execution contract top-level schema",
            details={
                "contract_version": raw_version,
                "invalid": "top_level_schema",
                "missing": missing_keys,
                "unknown": unknown_keys,
            },
        )
    return raw_contract


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
        decomposition_mode: Literal["bounce_only", "off"] | None = None,
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
                Legacy config files are migrated while loading; direct preflight
                overrides are rejected instead of authorizing a provider effect.
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
        adapter_cwd = adapter.working_directory
        self._adapter_launch_cwd = adapter_cwd
        self._resolved_adapter_launch_cwd = (
            resolve_worker_cwd(adapter_cwd)
            if isinstance(adapter_cwd, str) and adapter_cwd
            else None
        )
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
        self._task_cwd = resolve_worker_cwd(task_cwd) if task_cwd else None
        self._task_workspace_value: TaskWorkspace | None = None
        self._task_workspace_lock_held = False
        self._task_workspace = task_workspace
        self._max_decomposition_depth = validate_max_decomposition_depth(max_decomposition_depth)
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
        from ouroboros.config import get_agent_reasoning_effort, get_execution_model

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
        # it). The DEFAULT sonnet fallback that execution_handlers/run.py pass to
        # create_agent_runtime is a shipped default, not a user pin; explicit
        # environment or persisted Execute-stage pins both count here.
        _model_pin = get_execution_model()
        self._model_routing_disabled = _model_routing_disabled
        self._model_pin = _model_pin
        _runtime_backend = str(getattr(adapter, "runtime_backend", "")).strip().lower()
        _model_routing_explicit = bool(
            _model_routing_env is not None and _model_routing_env.strip()
        )
        # ``None`` from get_execution_model() is not merely an absent pin for
        # Codex: it is the user's explicit "follow the model selected in Codex"
        # choice.  A tier router would turn that sentinel into an OpenAI tier
        # model and emit ``codex exec --model ...``, silently overriding Codex.
        # Keep routing dormant for that automatic Codex path.  Explicit pins,
        # explicit tier requests, and an explicit routing policy retain the
        # advanced-routing behavior.
        _codex_automatic_model_selection = (
            _runtime_backend in {"codex_cli", "codex_mcp"}
            and _model_pin is None
            and base_model_tier is None
            and not _model_routing_explicit
        )
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
        # Keep the exact economics snapshot that produced the model router. The
        # Routing B compatibility bridge rebuilds its immutable registry from
        # this snapshot at the effect boundary, so a mutable/resumed router can
        # never introduce an unconfigured model or cost.
        self._route_economics = _economics_config
        _execution_config = _config.execution
        self._run_verify_commands = _execution_config.run_verify_commands
        self._verify_command_timeout_seconds = _execution_config.verify_command_timeout_seconds
        verify_shell = resolve_verify_shell() if self._run_verify_commands else None
        self._verify_shell_identity = (
            capture_verify_shell_identity(verify_shell) if verify_shell is not None else None
        )
        self._ac_retry_attempts = _execution_config.ac_retry_attempts
        from ouroboros.config import (
            get_context_pack_enabled,
            get_cross_harness_redispatch_enabled,
        )

        self._cross_harness_redispatch_enabled = get_cross_harness_redispatch_enabled()
        self._context_pack_enabled = get_context_pack_enabled()
        self._project_guidance_ids = tuple(_execution_config.project_guidance)
        configured_decomposition_mode = (
            _execution_config.decomposition_mode
            if decomposition_mode is None
            else decomposition_mode
        )
        if configured_decomposition_mode not in {"bounce_only", "off"}:
            msg = f"Unsupported decomposition_mode: {configured_decomposition_mode!r}"
            raise ValueError(msg)
        self._decomposition_mode: Literal["bounce_only", "off"] = (
            "off" if not enable_decomposition else configured_decomposition_mode
        )
        if not _model_routing_disabled and not _codex_automatic_model_selection:
            from ouroboros.orchestrator.model_routing import build_model_router

            self._model_router = build_model_router(
                _economics_config,
                runtime_backend=getattr(adapter, "runtime_backend", None),
                pinned_model=_model_pin,
                base_tier_override=base_model_tier,
            )
        if self._model_router is None:
            # A backend without a verified tier ladder (including GJC) cannot
            # enforce an explicit tier. Do not persist an advisory-only input as
            # a resume contract field that requires a router to restore.
            self._requested_model_tier = None
        self._apply_efficiency_mode_to_router()
        self._execution_contract: dict[str, Any] | None = None
        self._execution_contract_restore_lock = RLock()
        self._process_local_authorities: dict[
            tuple[str, str], _ProcessLocalAuthorityGeneration
        ] = {}
        self._task_workspace_users: set[tuple[str, str]] = set()
        self._task_workspace_reservations: set[object] = set()
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
        # Resume restores invocation-specific routing/guidance into runner
        # fields for compatibility with the existing execution path. Serialize
        # the whole resume invocation so one session cannot observe another
        # session's restored contract between those mutations and first use.
        self._resume_lock = asyncio.Lock()

    def _apply_efficiency_mode_to_router(self) -> None:
        """Apply the public efficiency preference to the resolved tier router."""
        if self._model_router is None or self._execution_preferences.child_model_lowering_enabled:
            return
        self._model_router = replace(
            self._model_router,
            child_tier=self._model_router.base_tier,
        )

    def _authoritative_model_router(
        self,
        preferences: ResolvedExecutionPreferences,
        *,
        requested_model_tier: str | None,
    ) -> ModelRouter | None:
        """Rebuild current route policy without trusting a mutable/restored router."""

        if self._model_routing_disabled:
            return None
        from ouroboros.orchestrator.model_routing import build_model_router

        router = build_model_router(
            self._route_economics,
            runtime_backend=getattr(self._adapter, "runtime_backend", None),
            pinned_model=self._model_pin,
            base_tier_override=requested_model_tier,
        )
        if router is not None and not preferences.child_model_lowering_enabled:
            router = replace(router, child_tier=router.base_tier)
        return router

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
        bounded_escalation: bool = False,
        route_id_override: str | None = None,
        expected_route_candidate: Any | None = None,
        expected_runtime_effect_capabilities: Mapping[str, object] | None = None,
        selected_route_sink: list[Any] | None = None,
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
        from ouroboros.orchestrator.route_compat import (
            admit_compat_escalation_route,
            admit_compat_route,
            admitted_execute_model_kwargs,
            build_route_compat_projection,
            deserialize_route_compat_contract,
            validate_compat_admission,
            validate_compat_escalation_admission,
        )

        self._require_runtime_effect_capabilities(expected_runtime_effect_capabilities)
        investment_assessment = assess_investment(None)
        decision, kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=self._reasoning_effort,
            is_decomposed_child=False,
            investment_assessment=investment_assessment,
        )
        initial_model_router = self._model_router
        model_decision, legacy_model_kwargs = resolve_execute_model(
            self._adapter,
            router=initial_model_router,
            is_decomposed_child=False,
            decomposition_trustworthy=False,
        )
        route_admission = None
        admission_projection = None
        if initial_model_router is not None:
            admission_projection = build_route_compat_projection(
                self._route_economics,
                model_router=initial_model_router,
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
                effort=decision.level,
            )
            if isinstance(self._execution_contract, Mapping):
                persisted_routing = self._execution_contract.get("model_routing")
                if isinstance(persisted_routing, Mapping):
                    recognized, persisted_projection = deserialize_route_compat_contract(
                        persisted_routing.get("route_compat")
                    )
                    if recognized:
                        admission_projection = persisted_projection
            if bounded_escalation:
                route_admission = admit_compat_escalation_route(
                    admission_projection,
                    effort=decision.level,
                    route_id=route_id_override,
                )
                selected = route_admission.selected
                if expected_route_candidate is not None and selected != expected_route_candidate:
                    raise OrchestratorError(
                        message="Route admission blocked before provider dispatch",
                        details={
                            "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                            "reason": "durable successor snapshot drifted",
                            "call_site": "runner",
                        },
                    )
                model_support = getattr(
                    getattr(self._adapter, "capabilities", None),
                    "model_override_support",
                    None,
                )
                if selected is not None and admission_projection is not None:
                    from ouroboros.orchestrator.adapter import ParamSupport
                    from ouroboros.orchestrator.model_routing import (
                        MODEL_MODE_ENFORCED,
                        ModelDecision,
                    )

                    tier = next(
                        (
                            tier
                            for tier, candidate_route_id in admission_projection.tier_route_ids
                            if candidate_route_id == selected.route_id
                        ),
                        None,
                    )
                    model_decision = ModelDecision(
                        tier=tier,
                        model=selected.model,
                        mode=(
                            MODEL_MODE_ENFORCED
                            if model_support is ParamSupport.NATIVE
                            else "advised"
                        ),
                    )
                if not route_admission.admitted or not model_decision.is_enforced:
                    reason = (
                        route_admission.reason
                        if not route_admission.admitted
                        else "runtime cannot enforce the admitted model"
                    )
                    raise OrchestratorError(
                        message="Route admission blocked before provider dispatch",
                        details={
                            "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                            "reason": reason,
                            "call_site": "runner",
                        },
                    )
            else:
                route_admission = admit_compat_route(
                    admission_projection,
                    model_decision=model_decision,
                    effort=decision.level,
                )
            if not route_admission.admitted:
                raise OrchestratorError(
                    message="Route admission blocked before provider dispatch",
                    details={
                        "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                        "reason": route_admission.reason,
                        "call_site": "runner",
                    },
                )
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

        # All observability awaits are complete. Rebuild and admit from live
        # routing state now, immediately before the caller enters execute_task.
        live_model_router = self._model_router
        if (initial_model_router is None) != (live_model_router is None):
            raise OrchestratorError(
                message="Route admission became stale before provider dispatch",
                details={
                    "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                    "reason": "model routing activation changed during pre-dispatch awaits",
                    "call_site": "runner",
                },
            )
        if live_model_router is None:
            model_kwargs = legacy_model_kwargs
        else:
            if live_model_router != initial_model_router:
                raise OrchestratorError(
                    message="Route admission became stale before provider dispatch",
                    details={
                        "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                        "reason": "model routing policy changed during pre-dispatch awaits",
                        "call_site": "runner",
                    },
                )
            if not bounded_escalation:
                live_model_decision, _live_legacy_model_kwargs = resolve_execute_model(
                    self._adapter,
                    router=live_model_router,
                    is_decomposed_child=False,
                    decomposition_trustworthy=False,
                )
                if live_model_decision != model_decision:
                    raise OrchestratorError(
                        message="Route admission became stale before provider dispatch",
                        details={
                            "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                            "reason": "model routing policy changed during pre-dispatch awaits",
                            "call_site": "runner",
                        },
                    )
            live_effort_decision, live_effort_kwargs = resolve_execute_effort(
                self._adapter,
                base_effort=self._reasoning_effort,
                is_decomposed_child=False,
                investment_assessment=investment_assessment,
            )
            if live_effort_decision != decision:
                raise OrchestratorError(
                    message="Route admission became stale before provider dispatch",
                    details={
                        "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                        "reason": "effort routing policy changed during pre-dispatch awaits",
                        "call_site": "runner",
                    },
                )
            live_projection = build_route_compat_projection(
                self._route_economics,
                model_router=live_model_router,
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
                effort=live_effort_decision.level,
            )
            assert route_admission is not None
            admission_valid = (
                validate_compat_escalation_admission(
                    live_projection,
                    route_admission,
                    effort=live_effort_decision.level,
                    route_id=route_id_override,
                )
                if bounded_escalation
                else validate_compat_admission(
                    live_projection,
                    route_admission,
                    model_decision=model_decision,
                    effort=live_effort_decision.level,
                )
            )
            if not admission_valid:
                raise OrchestratorError(
                    message="Route admission became stale before provider dispatch",
                    details={
                        "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                        "reason": "live route compatibility changed",
                        "call_site": "runner",
                    },
                )
            if bounded_escalation:
                selected = route_admission.selected
                from ouroboros.orchestrator.adapter import ParamSupport

                support = getattr(
                    getattr(self._adapter, "capabilities", None),
                    "model_override_support",
                    None,
                )
                model_kwargs = (
                    {"model": selected.model}
                    if selected is not None and support is ParamSupport.NATIVE
                    else {}
                )
            else:
                model_kwargs = admitted_execute_model_kwargs(
                    route_admission,
                    model_decision=model_decision,
                    projection=live_projection,
                    effort=live_effort_decision.level,
                )
            if (
                model_decision.is_enforced
                and model_decision.model is not None
                and model_kwargs.get("model") != model_decision.model
            ):
                raise OrchestratorError(
                    message="Route admission could not authorize the provider model",
                    details={
                        "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                        "model_tier": model_decision.tier,
                        "call_site": "runner",
                    },
                )
        # Model kwargs are collapsed only after live admission above. Recheck
        # the complete runtime declaration after every observability await so
        # an unused vocabulary entry cannot drift into a later resumed effect.
        self._require_runtime_effect_capabilities(expected_runtime_effect_capabilities)
        # Callers invoke execute_task on the next statement without another await.
        if selected_route_sink is not None and route_admission is not None:
            selected = route_admission.selected
            if selected is not None:
                selected_route_sink.append(selected)
        return {**(live_effort_kwargs if live_model_router is not None else kwargs), **model_kwargs}

    def _require_runtime_effect_capabilities(
        self,
        expected: Mapping[str, object] | None,
    ) -> None:
        """Fail before provider entry when any declared runtime effect can drift."""
        if expected is None:
            return
        if not valid_runtime_effect_capabilities_contract(expected):
            raise OrchestratorError(
                message="Provider effect capability snapshot is invalid",
                details={"invalid": "runtime_effect_capabilities"},
            )
        try:
            current = runtime_effect_capabilities_contract(self._adapter)
        except (AttributeError, TypeError, ValueError) as exc:
            raise OrchestratorError(
                message="Provider effect capability snapshot is unavailable",
                details={"cause": type(exc).__name__},
            ) from exc
        if current != dict(expected):
            raise OrchestratorError(
                message="Provider effect capabilities drifted before dispatch",
                details={
                    "persisted_runtime_effect_capabilities": dict(expected),
                    "current_runtime_effect_capabilities": current,
                    "resume_blocked": "runtime_effect_capability_drift",
                },
            )

    @staticmethod
    def _classify_direct_route_failure(message: AgentMessage | None) -> Any:
        """Classify a direct final error without inventing retry permission.

        Direct execution has no leaf ``Attempt`` object, so consume explicit
        provider/verifier metadata first and recognize only unambiguous hard
        preconditions in the final error text.  Everything else remains the
        conservative evidence-missing class.
        """

        from ouroboros.orchestrator.failure_taxonomy import (
            FailureClass,
            classify_hard_precondition,
        )

        if message is None or not (message.is_final and message.is_error):
            return FailureClass.EVIDENCE_MISSING
        return (
            classify_hard_precondition(message.content, message.data)
            or FailureClass.EVIDENCE_MISSING
        )

    @staticmethod
    def _has_exact_resumable_runtime_handle(runtime_handle: RuntimeHandle | None) -> bool:
        """Return whether a pause can reconnect to an existing provider session."""

        return bool(
            runtime_handle is not None
            and runtime_handle.can_resume
            and not runtime_handle.is_terminal
        )

    async def _persist_exact_direct_pause_runtime_handle(
        self,
        *,
        session_id: str,
        runtime_handle: RuntimeHandle | None,
        messages_processed: int,
    ) -> bool:
        """Durably bind a direct PAUSED transition to provider continuity."""

        if not self._has_exact_resumable_runtime_handle(runtime_handle):
            return False
        assert runtime_handle is not None
        progress: dict[str, Any] = {
            "messages_processed": messages_processed,
            "runtime": runtime_handle.to_session_state_dict(),
            "runtime_backend": runtime_handle.backend,
        }
        if runtime_handle.backend == "claude" and runtime_handle.native_session_id:
            progress["agent_session_id"] = runtime_handle.native_session_id
        try:
            persisted = await self._session_repo.track_progress(session_id, progress)
        except Exception:
            log.exception(
                "orchestrator.runner.direct_pause_handle_persist_failed",
                session_id=session_id,
            )
            return False
        if persisted.is_err:
            log.warning(
                "orchestrator.runner.direct_pause_handle_persist_failed",
                session_id=session_id,
                error=str(persisted.error),
            )
            return False
        return True

    async def _persist_direct_route_outcome(
        self,
        *,
        execution_id: str,
        session_id: str,
        episode_id: str,
        prior_route_ids: tuple[str, ...],
        candidate: RouteCandidate,
        success: bool,
        failure_class: object | None = None,
    ) -> tuple[RouteEscalationDecision | None, tuple[str, ...]]:
        """Persist one direct provisional outcome and compute its exact successor."""

        from ouroboros.events.base import BaseEvent
        from ouroboros.orchestrator.failure_taxonomy import FailureClass
        from ouroboros.orchestrator.route_compat import (
            build_compat_escalation_registry,
            build_compat_escalation_requirements,
            build_route_compat_projection,
        )
        from ouroboros.orchestrator.route_escalation import (
            MAX_ROUTE_ATTEMPTS,
            EscalationAction,
            EscalationReason,
            RouteEscalationDecision,
            RouteObservation,
            VerifierOutcome,
            advance_route,
        )
        from ouroboros.orchestrator.route_policy import RouteRequirements

        history = (*prior_route_ids, candidate.route_id)
        if len(history) > MAX_ROUTE_ATTEMPTS or len(set(history)) != len(history):
            raise OrchestratorError(
                message="Refusing an unbounded or repeated direct route attempt",
                details={"execution_id": execution_id, "session_id": session_id},
            )
        projection = build_route_compat_projection(
            self._route_economics,
            model_router=self._model_router,
            runtime_backend=getattr(self._adapter, "runtime_backend", None),
            effort=candidate.effort,
        )
        registry = build_compat_escalation_registry(projection)
        requirements = (
            build_compat_escalation_requirements(projection, effort=candidate.effort)
            if projection is not None
            else None
        )
        live_candidate = (
            next(
                (
                    configured
                    for configured in projection.registry.candidates
                    if configured.route_id == candidate.route_id
                ),
                None,
            )
            if projection is not None
            else None
        )
        classified_failure = (
            None
            if success
            else failure_class
            if isinstance(failure_class, FailureClass)
            else FailureClass.EVIDENCE_MISSING
        )
        decision: RouteEscalationDecision | None = None
        if not success:
            if (
                projection is None
                or registry is None
                or requirements is None
                or live_candidate != candidate
            ):
                decision = RouteEscalationDecision(
                    action=EscalationAction.BLOCKED,
                    failure_class=classified_failure,
                    selected=None,
                    attempted_route_ids=history,
                    remaining_route_ids=(),
                    reason=EscalationReason.NO_ELIGIBLE_ROUTE,
                )
            else:
                decision = advance_route(
                    registry,
                    requirements,
                    current_route_id=candidate.route_id,
                    attempted_route_ids=history,
                    failure_class=classified_failure,
                )
        observation = RouteObservation.from_candidate(
            candidate,
            requirements or RouteRequirements(),
            episode_id=episode_id,
            attempt_index=len(history) - 1,
            verifier_outcome=(
                VerifierOutcome.ATTEMPT_SUCCEEDED
                if success
                else VerifierOutcome.BLOCKED
                if classified_failure is FailureClass.BLOCKED
                else VerifierOutcome.FAILED
            ),
            failure_class=classified_failure,
            escalation_reason=decision.reason if decision is not None else None,
        )
        await self._event_store.append(
            BaseEvent(
                type="execution.ac.route_observed",
                aggregate_type="execution",
                aggregate_id=execution_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": None,
                    "call_site": "runner",
                    "observation": observation.to_contract_data(),
                    "decision": decision.to_contract_data() if decision is not None else None,
                    "human_handoff_required": bool(decision is not None and decision.blocked),
                    "final_acceptance_declared": False,
                },
            )
        )
        return decision, history

    async def _direct_resume_route_id(
        self,
        *,
        execution_id: str,
        session_id: str,
    ) -> _DirectRouteResumeState | None:
        """Resume only an explicitly paused direct route; seal completed effects."""
        from ouroboros.orchestrator.route_compat import (
            build_compat_escalation_registry,
            build_compat_escalation_requirements,
            build_route_compat_projection,
        )
        from ouroboros.orchestrator.route_escalation import (
            MAX_ROUTE_ATTEMPTS,
            EscalationAction,
            RouteEscalationDecision,
            RouteObservation,
            VerifierOutcome,
            advance_route,
        )
        from ouroboros.orchestrator.route_policy import RouteCandidate, RouteRequirements

        observation_events = await self._event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.route_observed",
            limit=MAX_ROUTE_ATTEMPTS + 1,
        )
        if len(observation_events) > MAX_ROUTE_ATTEMPTS:
            raise OrchestratorError(
                message="Refusing to replay unbounded direct route observations",
                details={"execution_id": execution_id, "session_id": session_id},
            )
        direct_events = [
            event
            for event in observation_events
            if event.type == "execution.ac.route_observed"
            if event.data.get("session_id") == session_id
        ]
        if any(
            event.data.get("call_site") != "runner"
            or event.data.get("execution_id") != execution_id
            for event in direct_events
        ):
            raise OrchestratorError(
                message="Refusing to replay route evidence from another call site",
                details={"execution_id": execution_id, "session_id": session_id},
            )
        if any(
            not _mapping_has_exact_keys(event.data, _DIRECT_ROUTE_OBSERVATION_KEYS)
            for event in direct_events
        ):
            raise OrchestratorError(
                message="Refusing to replay an invalid direct route observation envelope",
                details={"execution_id": execution_id, "session_id": session_id},
            )
        expected_episode = "route:" + hashlib.sha256(f"{execution_id}\0direct".encode()).hexdigest()
        expected_pause_keys = {
            "schema_version",
            "execution_id",
            "session_id",
            "root_ac_index",
            "call_site",
            "episode_id",
            "attempt_index",
            "prior_route_ids",
            "route",
            "recoverable_pause",
            "final_acceptance_declared",
        }

        parsed_rows: list[tuple[RouteObservation, object, object]] = []
        for event in direct_events:
            data = event.data
            if (
                type(data.get("schema_version")) is not int
                or data.get("schema_version") != 1
                or data.get("final_acceptance_declared") is not False
            ):
                raise OrchestratorError(
                    message="Refusing to replay an invalid direct route observation",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            try:
                observation = RouteObservation.from_contract_data(data.get("observation"))
            except (TypeError, ValueError) as exc:
                raise OrchestratorError(
                    message="Refusing to replay an invalid direct route observation",
                    details={"execution_id": execution_id, "session_id": session_id},
                ) from exc
            if observation.episode_id != expected_episode:
                raise OrchestratorError(
                    message="Refusing to replay a direct route from another episode",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            parsed_rows.append(
                (observation, data.get("decision"), data.get("human_handoff_required"))
            )
        parsed_rows.sort(key=lambda row: row[0].attempt_index)
        route_history_is_contiguous = [row[0].attempt_index for row in parsed_rows] == list(
            range(len(parsed_rows))
        )
        prior_route_ids = tuple(row[0].route_id for row in parsed_rows)
        pause_data: dict[str, Any] | None = None
        paused_candidate: RouteCandidate | None = None
        async for pause_event in replay_execution_events_chronologically(
            self._event_store,
            execution_id=execution_id,
            event_type="execution.ac.route_paused",
            page_size=_DIRECT_ROUTE_PAUSE_REPLAY_PAGE_SIZE,
        ):
            if pause_event.type != "execution.ac.route_paused":
                continue
            superseded = pause_event.data
            if superseded.get("session_id") != session_id:
                continue
            if not route_history_is_contiguous:
                raise OrchestratorError(
                    message="Refusing to replay gapped direct route history",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            if (
                not _mapping_has_exact_keys(superseded, frozenset(expected_pause_keys))
                or superseded.get("schema_version") != 1
                or superseded.get("execution_id") != execution_id
                or superseded.get("session_id") != session_id
                or superseded.get("root_ac_index") is not None
                or superseded.get("call_site") != "runner"
                or superseded.get("episode_id") != expected_episode
                or superseded.get("recoverable_pause") is not True
                or superseded.get("final_acceptance_declared") is not False
                or type(superseded.get("attempt_index")) is not int
                or type(superseded.get("prior_route_ids")) is not list
            ):
                raise OrchestratorError(
                    message="Refusing to replay invalid superseded direct route pause state",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            superseded_index = superseded["attempt_index"]
            try:
                superseded_candidate = RouteCandidate.from_contract_data(superseded.get("route"))
            except (TypeError, ValueError) as exc:
                raise OrchestratorError(
                    message="Refusing to replay invalid superseded direct route pause state",
                    details={"execution_id": execution_id, "session_id": session_id},
                ) from exc
            superseded_prior_route_ids = tuple(superseded["prior_route_ids"])
            if superseded_index < len(parsed_rows):
                if (
                    superseded_index < 0
                    or superseded_prior_route_ids != prior_route_ids[:superseded_index]
                    or parsed_rows[superseded_index][0].route_id != superseded_candidate.route_id
                ):
                    raise OrchestratorError(
                        message="Refusing to replay an invalid consumed direct route pause",
                        details={"execution_id": execution_id, "session_id": session_id},
                    )
                continue
            if (
                superseded_index != len(parsed_rows)
                or superseded_prior_route_ids != prior_route_ids
                or len(set(prior_route_ids) | {superseded_candidate.route_id})
                != len(prior_route_ids) + 1
                or paused_candidate is not None
                and paused_candidate != superseded_candidate
            ):
                raise OrchestratorError(
                    message="Refusing to replay inconsistent direct route pause history",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            # Repeated quota windows on the same unconsumed route replace only
            # the external provider boundary. Keep the newest durable envelope
            # while validating every superseded row in bounded-memory pages.
            pause_data = superseded
            paused_candidate = superseded_candidate

        if pause_data is None or paused_candidate is None:
            if not direct_events:
                return None
            latest = max(direct_events, key=lambda event: (event.timestamp, event.id))
            data = latest.data
            try:
                observation = RouteObservation.from_contract_data(data.get("observation"))
            except (TypeError, ValueError) as exc:
                raise OrchestratorError(
                    message="Refusing to replay an invalid direct route observation",
                    details={"execution_id": execution_id, "session_id": session_id},
                ) from exc
            if observation.verifier_outcome is VerifierOutcome.ATTEMPT_SUCCEEDED:
                raise OrchestratorError(
                    message="Refusing to replay a successful direct route before Final Gate",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
            raise OrchestratorError(
                message="Refusing to replay a completed direct route; human handoff is required",
                details={"execution_id": execution_id, "session_id": session_id},
            )

        from ouroboros.orchestrator.effort_routing import (
            assess_investment,
            resolve_execute_effort,
        )

        expected_effort, _expected_effort_kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=self._reasoning_effort,
            is_decomposed_child=False,
            investment_assessment=assess_investment(None),
        )
        live_paused_projection = build_route_compat_projection(
            self._route_economics,
            model_router=self._model_router,
            runtime_backend=getattr(self._adapter, "runtime_backend", None),
            effort=expected_effort.level,
        )
        if live_paused_projection is None:
            raise OrchestratorError(
                message="Refusing to replay a paused route without a live registry",
                details={"execution_id": execution_id, "session_id": session_id},
            )
        live_paused_registry = build_compat_escalation_registry(live_paused_projection)
        live_paused_candidate = (
            next(
                (
                    candidate
                    for candidate in live_paused_registry.candidates
                    if candidate.route_id == paused_candidate.route_id
                ),
                None,
            )
            if live_paused_registry is not None
            else None
        )
        if live_paused_candidate != paused_candidate:
            raise OrchestratorError(
                message="Refusing to replay a drifted direct route pause",
                details={"execution_id": execution_id, "session_id": session_id},
            )

        if parsed_rows:
            for row_index, (observation, raw_decision, handoff_claim) in enumerate(parsed_rows):
                live_projection = build_route_compat_projection(
                    self._route_economics,
                    model_router=self._model_router,
                    runtime_backend=getattr(self._adapter, "runtime_backend", None),
                    effort=observation.effort,
                )
                escalation_registry = build_compat_escalation_registry(live_projection)
                requirements = (
                    build_compat_escalation_requirements(
                        live_projection,
                        effort=observation.effort,
                    )
                    if live_projection is not None
                    else None
                )
                try:
                    if (
                        live_projection is None
                        or escalation_registry is None
                        or requirements is None
                        or observation.failure_class is None
                        or observation.verifier_outcome is VerifierOutcome.ATTEMPT_SUCCEEDED
                    ):
                        raise ValueError("live route failure state is unavailable")
                    live_candidate = next(
                        (
                            candidate
                            for candidate in live_projection.registry.candidates
                            if candidate.route_id == observation.route_id
                        ),
                        None,
                    )
                    if live_candidate is None:
                        raise ValueError("observed route was removed")
                    expected_observation = RouteObservation.from_candidate(
                        live_candidate,
                        RouteRequirements(
                            required_capabilities=requirements.required_capabilities,
                        ),
                        episode_id=observation.episode_id,
                        attempt_index=observation.attempt_index,
                        verifier_outcome=observation.verifier_outcome,
                        failure_class=observation.failure_class,
                        escalation_reason=observation.escalation_reason,
                    )
                    decision = RouteEscalationDecision.from_contract_data(
                        raw_decision,
                        registry=escalation_registry,
                    )
                    attempted_prefix = prior_route_ids[: row_index + 1]
                    recomputed = advance_route(
                        escalation_registry,
                        requirements,
                        current_route_id=observation.route_id,
                        attempted_route_ids=attempted_prefix,
                        failure_class=observation.failure_class,
                    )
                except (TypeError, ValueError) as exc:
                    raise OrchestratorError(
                        message="Refusing to replay invalid direct route escalation state",
                        details={"execution_id": execution_id, "session_id": session_id},
                    ) from exc
                next_observation = (
                    parsed_rows[row_index + 1][0] if row_index + 1 < len(parsed_rows) else None
                )
                successor_matches = (
                    decision.selected == paused_candidate
                    if next_observation is None
                    else decision.selected is not None
                    and (
                        decision.selected.route_id,
                        decision.selected.model,
                        decision.selected.harness,
                        decision.selected.effort,
                        decision.selected.cost_units,
                        decision.selected.capabilities,
                    )
                    == (
                        next_observation.route_id,
                        next_observation.model,
                        next_observation.harness,
                        next_observation.effort,
                        next_observation.cost_units,
                        next_observation.capabilities,
                    )
                )
                if (
                    expected_observation != observation
                    or decision != recomputed
                    or decision.action is not EscalationAction.ESCALATE_ROUTE
                    or decision.selected is None
                    or not successor_matches
                    or handoff_claim is not False
                ):
                    raise OrchestratorError(
                        message="Refusing to replay a paused route outside its escalation chain",
                        details={"execution_id": execution_id, "session_id": session_id},
                    )
        else:
            from ouroboros.orchestrator.route_compat import admit_compat_escalation_route

            initial = admit_compat_escalation_route(
                live_paused_projection,
                effort=expected_effort.level,
            )
            if initial.selected != paused_candidate:
                raise OrchestratorError(
                    message="Refusing to replay a paused route that was not cheapest eligible",
                    details={"execution_id": execution_id, "session_id": session_id},
                )
        return _DirectRouteResumeState(
            episode_id=expected_episode,
            attempt_index=pause_data["attempt_index"],
            prior_route_ids=prior_route_ids,
            candidate=paused_candidate,
        )

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

    def _plan_parallel_workers(self, requested_workers: int | None = None) -> int:
        """Return the initial fan-out estimate for the connected backend.

        Backends whose underlying LLM limits are unknown — the CLI runtimes —
        start serialized. ParallelACExecutor treats this immutable pre-flight
        value as an AIMD initial window, shrinking on provider pressure and
        cautiously probing toward ``max_parallel_workers`` after sustained
        success.
        """
        limits = resolve_backend_limits(self._adapter.runtime_backend)
        requested = self._max_parallel_workers if requested_workers is None else requested_workers
        return plan_fan_out_concurrency(requested, limits)

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

    @property
    def _task_workspace(self) -> TaskWorkspace | None:
        """Return the workspace whose lock backs this runner's current effects."""
        return self._task_workspace_value

    @_task_workspace.setter
    def _task_workspace(self, workspace: TaskWorkspace | None) -> None:
        """Bind a freshly acquired workspace and reset release idempotence."""
        self._task_workspace_value = workspace
        self._task_workspace_lock_held = workspace is not None

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
        self._release_task_workspace_for_identity(
            session_id=session_id,
            execution_id=execution_id,
        )

    async def _cleanup_terminal_process_local_state(
        self,
        *,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Retire all live state, then acknowledge cancellation last."""

        async def _cleanup() -> None:
            retired = self._retire_process_local_authority(
                session_id=session_id,
                execution_id=execution_id,
            )
            if not retired:
                # A different live adapter/runner may still own this exact
                # session. Never unregister its route or release its PID
                # heartbeat merely because this cleanup observer saw a
                # terminal record. If this runner has a stale local route,
                # remove only that route while preserving the shared lease.
                if self._active_sessions.get(execution_id) == session_id:
                    self._unregister_session(
                        execution_id,
                        session_id,
                        release_liveness_lease=False,
                    )
                self._release_task_workspace_for_identity(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                from ouroboros.orchestrator.heartbeat import (
                    is_holder_alive,
                    is_owned_by_current_process,
                )

                if not _has_live_process_local_authority_session(session_id) and not (
                    is_holder_alive(session_id) and not is_owned_by_current_process(session_id)
                ):
                    await clear_cancellation(session_id)
                log.warning(
                    "orchestrator.runner.terminal_cleanup_authority_not_retired",
                    session_id=session_id,
                    execution_id=execution_id,
                )
                return
            self._unregister_session(execution_id, session_id)
            self._release_task_workspace_for_identity(
                session_id=session_id,
                execution_id=execution_id,
            )
            await clear_cancellation(session_id)

        await _await_process_local_cleanup(_cleanup())

    def _release_task_workspace_for_identity(
        self,
        *,
        session_id: str | None,
        execution_id: str | None,
    ) -> bool:
        """Release the shared workspace lock only after its last local owner exits.

        A runner may prepare more than one process-local session, while the
        managed workspace lock is runner-wide.  The lifecycle path named by
        ``session_id``/``execution_id`` is relinquishing its workspace use even
        when its authority remains registered for retry.  Any other live
        runner-local authority keeps the lock.  If the named identity was never
        registered here, a global live registration for the same session means
        this is a collision cleanup and must not release the real owner's lock.
        """
        identity = (
            (session_id, execution_id)
            if session_id is not None and execution_id is not None
            else None
        )
        owns_named_workspace = identity in self._task_workspace_users
        if identity is not None:
            self._task_workspace_users.discard(identity)
        if self._task_workspace is None or not self._task_workspace_lock_held:
            return False
        if self._task_workspace_users or self._task_workspace_reservations:
            return False
        if (
            identity is not None
            and not owns_named_workspace
            and session_id is not None
            and _has_live_process_local_authority_session(session_id)
        ):
            return False

        release_lock(self._task_workspace.lock_path)
        self._task_workspace_lock_held = False
        return True

    def _reserve_task_workspace(self, token: object | None = None) -> object | None:
        """Keep a held runner workspace alive across a pre-identity handoff."""
        if self._task_workspace is None:
            return None
        reservation = token if token is not None else object()
        self._task_workspace_reservations.add(reservation)
        return reservation

    def _release_task_workspace_reservation(self, token: object | None) -> bool:
        """Release one pre-identity reservation and the lock if it was last."""
        if token is None:
            return False
        self._task_workspace_reservations.discard(token)
        if (
            self._task_workspace is None
            or not self._task_workspace_lock_held
            or self._task_workspace_users
            or self._task_workspace_reservations
        ):
            return False
        release_lock(self._task_workspace.lock_path)
        self._task_workspace_lock_held = False
        return True

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
        self._release_task_workspace_for_identity(
            session_id=session_id,
            execution_id=execution_id,
        )

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
            await self._cleanup_terminal_process_local_state(
                session_id=session_id,
                execution_id=execution_id,
            )
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
        seed: Seed | None = None,
        execution_contract: Mapping[str, Any] | None = None,
    ) -> tuple[SessionStatus | None, Result[OrchestratorResult, OrchestratorError] | None]:
        """Persist one durable failure winner before withdrawing ownership."""
        if seed is None:
            return None, self._terminal_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                requested_status=SessionStatus.FAILED,
                cause=PersistenceError("Cannot persist FAILED without the seed acceptance plan."),
            )
        reconciled_during_exception = False
        acceptance_finalizations: list[dict[str, Any]] | None = None
        if seed is not None:
            try:
                # The exception path may run after one or more retries have
                # already emitted durable attempt judgments.  Rebuild from
                # that telemetry instead of fabricating retry-0/failed facts.
                acceptance_finalizations = await collect_terminal_acceptance_plan(
                    session_id=session_id,
                    execution_id=execution_id,
                    event_store=self._event_store,
                    terminal_status=SessionStatus.FAILED.value,
                    expected_root_indices=range(len(seed.acceptance_criteria)),
                    fallback_outcome="failed",
                )
            except (Exception, asyncio.CancelledError) as planning_error:
                if isinstance(planning_error, asyncio.CancelledError):
                    self._preserve_process_local_owner_for_retry(
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    raise
                self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
                    execution_id=execution_id,
                    status=SessionStatus.FAILED,
                    error_message=str(error),
                    error_type=type(error).__name__,
                    messages_processed=messages_processed,
                    acceptance_finalizations=None,
                )
                return None, self._terminal_persistence_pending_result(
                    session_id=session_id,
                    execution_id=execution_id,
                    requested_status=SessionStatus.FAILED,
                    cause=planning_error,
                )
        try:
            durable_status = await self._persist_session_terminal_status(
                session_id=session_id,
                execution_id=execution_id,
                requested_status=SessionStatus.FAILED,
                error_message=str(error),
                error_type=type(error).__name__,
                messages_processed=messages_processed,
                acceptance_finalizations=acceptance_finalizations,
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
                    acceptance_finalizations=acceptance_finalizations,
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
            await self._cleanup_terminal_process_local_state(
                session_id=session_id,
                execution_id=execution_id,
            )
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
        preserve_existing_tool_catalog: bool = False,
    ) -> RuntimeHandle | None:
        """Seed a runtime handle with startup metadata before execution begins."""
        backend = (
            runtime_handle.backend if runtime_handle is not None else None
        ) or self._adapter.runtime_backend
        if not backend:
            return runtime_handle

        metadata = dict(runtime_handle.metadata) if runtime_handle is not None else {}
        if tool_catalog is not None:
            serialized_catalog = serialize_tool_catalog(tool_catalog)
            existing_catalog = metadata.get("tool_catalog")
            if preserve_existing_tool_catalog and existing_catalog is not None:
                try:
                    existing_json = json.dumps(
                        existing_catalog,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                    current_json = json.dumps(
                        serialized_catalog,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                except (TypeError, ValueError) as exc:
                    raise OrchestratorError(
                        message="Cannot resume with an invalid runtime tool catalog",
                        details={"cause": type(exc).__name__},
                    ) from exc
                if existing_json != current_json:
                    raise OrchestratorError(
                        message="Cannot overwrite changed runtime tool authority on resume",
                        details={"resume_blocked": "runtime_tool_catalog_drift"},
                    )
            else:
                metadata["tool_catalog"] = serialized_catalog
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

    def _provider_cwd(self) -> str | None:
        """Return the one cwd value retained by the provider runtime."""
        cwd = self._adapter.working_directory
        if cwd == self._adapter_launch_cwd:
            return self._resolved_adapter_launch_cwd
        return resolve_worker_cwd(cwd) if isinstance(cwd, str) and cwd else None

    def _effective_cwd(self, runtime_handle: RuntimeHandle | None = None) -> str | None:
        """Return one cwd shared by publication, handles, and provider effects."""
        provider_cwd = self._provider_cwd()
        workspace_cwd = (
            resolve_worker_cwd(self._task_workspace.effective_cwd)
            if self._task_workspace is not None
            else None
        )
        if (
            self._task_cwd is not None
            and workspace_cwd is not None
            and self._task_cwd != workspace_cwd
        ):
            raise OrchestratorError(
                message="Explicit task cwd does not match the managed workspace",
                details={
                    "invalid": "runtime_cwd",
                    "selected_cwd": self._task_cwd,
                    "workspace_cwd": workspace_cwd,
                    "resume_blocked": "runtime_cwd_mismatch",
                },
            )
        selected_cwd = self._task_cwd or workspace_cwd
        handle_cwd = (
            resolve_worker_cwd(runtime_handle.cwd)
            if runtime_handle is not None and runtime_handle.cwd
            else None
        )
        if selected_cwd is not None and handle_cwd is not None and selected_cwd != handle_cwd:
            raise OrchestratorError(
                message="Runtime handle does not own the selected task cwd",
                details={
                    "invalid": "runtime_cwd",
                    "selected_cwd": selected_cwd,
                    "handle_cwd": handle_cwd,
                    "resume_blocked": "runtime_cwd_mismatch",
                },
            )
        required_cwd = selected_cwd or handle_cwd
        if required_cwd is not None and provider_cwd != required_cwd:
            raise OrchestratorError(
                message="Provider runtime does not own the selected task cwd",
                details={
                    "invalid": "runtime_cwd",
                    "selected_cwd": required_cwd,
                    "provider_cwd": provider_cwd,
                    "resume_blocked": "runtime_cwd_mismatch",
                },
            )
        return required_cwd or provider_cwd

    @staticmethod
    def _canonical_path(value: str) -> str:
        """Return a symlink-resolved absolute path without requiring existence."""
        return str(Path(value).expanduser().resolve(strict=False))

    @classmethod
    def _task_workspace_project_identity(cls, workspace: TaskWorkspace) -> ProjectIdentity:
        """Resolve a managed worktree against its durable source checkout."""
        try:
            return resolve_managed_project_identity(
                workspace.effective_cwd,
                source_root=workspace.repo_root,
                source_workspace=workspace.original_cwd,
                worktree_root=workspace.worktree_path,
            )
        except ManagedProjectScopeError as exc:
            raise OrchestratorError(
                message="Managed source and execution workspace scopes do not match",
                details={
                    "invalid": "runtime_cwd",
                    "source_cwd": exc.source_workspace,
                    "execution_cwd": exc.execution_workspace,
                    "resume_blocked": "runtime_cwd_mismatch",
                },
            ) from exc
        except ManagedProjectOwnershipError as exc:
            raise OrchestratorError(
                message="Managed worktree does not belong to its source project",
                details={
                    "invalid": "project_identity",
                    "source_identity": exc.source_identity.to_workspace_data(),
                    "execution_identity": exc.execution_identity.to_workspace_data(),
                    "resume_blocked": "project_identity_mismatch",
                },
            ) from exc

    @staticmethod
    def _project_identity_error(exc: ProjectIdentityError) -> OrchestratorError:
        """Normalize resolver failures at the orchestration lifecycle boundary."""
        details: dict[str, Any] = {"invalid": "project_identity", "cause": str(exc)}
        if isinstance(exc, ProjectIdentityUnavailableError):
            details.update(
                resume_blocked="project_identity_unavailable",
                retryable=True,
            )
        return OrchestratorError(message="Cannot resolve project identity", details=details)

    def _project_identity(self) -> ProjectIdentity | None:
        """Return the single canonical identity shared by event and contract."""
        try:
            if self._task_workspace is not None:
                self._effective_cwd()
                return self._task_workspace_project_identity(self._task_workspace)
            effective_cwd = self._effective_cwd()
            if not isinstance(effective_cwd, str) or not effective_cwd.strip():
                return None
            if active_publication_evidence_sink() is None:
                return resolve_project_identity(effective_cwd)
            identity, _evidence = resolve_project_identity_for_publication(effective_cwd)
            return identity
        except ProjectIdentityError as exc:
            raise self._project_identity_error(exc) from exc

    def _proof_workspace_identity(self) -> dict[str, str] | None:
        """Return the stable project + source-workspace identity for this run.

        Managed task worktrees have a different checkout path for every session,
        so cohort identity is anchored to their persisted source repository and
        source-relative cwd. Direct Git callers resolve the nearest checkout,
        preserve a relative workspace scope, and join provable linked worktrees
        to their primary source root. Non-Git callers retain the conservative
        canonical effective-cwd identity.
        """
        return self._resolved_proof_workspace_identity(self._project_identity())

    @staticmethod
    def _resolved_proof_workspace_identity(
        project_identity: ProjectIdentity | None,
    ) -> dict[str, str] | None:
        """Project one already-resolved identity without another resolver call."""
        return project_identity.to_workspace_data() if project_identity is not None else None

    def _legacy_proof_workspace_identity(self) -> dict[str, str] | None:
        """Reproduce the pre-Project-Map V1 nested workspace representation."""
        if self._task_workspace is not None:
            return legacy_task_workspace_identity(self._task_workspace, self._canonical_path)
        effective_cwd = self._effective_cwd()
        if not isinstance(effective_cwd, str) or not effective_cwd.strip():
            return None
        return {
            "project_root": self._canonical_path(effective_cwd),
            "workspace_path": ".",
        }

    @staticmethod
    def _project_start_identity(
        progress: Mapping[str, Any],
    ) -> tuple[bool, ProjectIdentity | None]:
        """Parse the all-or-none immutable Project Map session anchor."""
        raw_start_identity = progress.get(SESSION_START_IDENTITY_PROGRESS_KEY)
        if not isinstance(raw_start_identity, Mapping):
            return False, None
        project_keys = frozenset({"project_id", "project_root", "workspace_path"})
        present_keys = {key for key in project_keys if key in raw_start_identity}
        if not present_keys:
            return False, None
        if present_keys != project_keys:
            raise OrchestratorError(
                message="Cannot resume with a partial project identity anchor",
                details={"invalid": "project_identity_anchor"},
            )
        try:
            identity = ProjectIdentity(
                project_id=raw_start_identity["project_id"],
                project_root=raw_start_identity["project_root"],
                workspace_path=raw_start_identity["workspace_path"],
            )
        except ProjectIdentityUnavailableError as exc:
            raise OrchestratorRunner._project_identity_error(exc) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise OrchestratorError(
                message="Cannot resume with an invalid project identity anchor",
                details={"invalid": "project_identity_anchor"},
            ) from exc
        return True, identity

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
    def _execution_semantics_fingerprint(semantics_contract: Mapping[str, Any]) -> str:
        """Hash the complete scalar executor contract used by resume."""
        encoded = json.dumps(
            dict(semantics_contract),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _execution_inputs_fingerprint(inputs_contract: Mapping[str, Any]) -> str:
        """Hash the resolved prompt and complete provider tool authority."""
        encoded = json.dumps(
            dict(inputs_contract),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _build_execution_inputs_contract(self, seed: Seed | None) -> dict[str, object]:
        """Freeze every prompt/profile/session input before publication."""
        execution_profile = _execution_profile_for_seed(seed) if seed is not None else None
        strategy = (
            ProfileBackedStrategy(execution_profile)
            if self._fat_harness_mode and execution_profile is not None
            else get_strategy(seed.task_type if seed is not None else "code")
        )
        tools = strategy.get_tools()
        system_fragment = strategy.get_system_prompt_fragment()
        task_suffix = strategy.get_task_prompt_suffix()
        activity_map = strategy.get_activity_map()
        resolver = "profile_backed" if isinstance(strategy, ProfileBackedStrategy) else "registry"
        context_pack_fragment = (
            _context_pack_fragment(
                seed,
                self._effective_cwd(),
                context_pack_enabled=self._context_pack_enabled,
            )
            if seed is not None
            else ""
        )
        try:
            execution_profile_json = (
                json.dumps(
                    execution_profile.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if execution_profile is not None
                else None
            )
            inherited_runtime_handle_json = (
                json.dumps(
                    self._inherited_runtime_handle.to_persisted_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if self._inherited_runtime_handle is not None
                else None
            )
        except (OverflowError, RecursionError, TypeError, ValueError) as exc:
            raise OrchestratorError(
                message="Cannot persist non-canonical execution effect inputs",
                details={"cause": type(exc).__name__},
            ) from exc
        raw_contract: dict[str, object] = {
            "schema_version": 2,
            "strategy": {
                "schema_version": 1,
                "resolver": resolver,
                "tools": list(tools),
                "system_prompt_fragment": system_fragment,
                "task_prompt_suffix": task_suffix,
                "activity_map": [
                    [tool, activity.value] for tool, activity in sorted(activity_map.items())
                ],
            },
            "context_pack_fragment": context_pack_fragment,
            "execution_profile_json": execution_profile_json,
            "inherited_runtime_handle_json": inherited_runtime_handle_json,
            # MCP discovery is asynchronous and session-scoped, so the complete
            # catalog is sealed immediately after discovery and before the first
            # provider effect. A paused current-format session must always have
            # these fields bound.
            "allowed_tools": None,
            "tool_catalog_json": None,
            "tool_catalog_fingerprint": None,
        }
        if self._mcp_manager is None:
            _, _, static_catalog = self._assemble_strategy_base_catalog(strategy)
            policy_result = self._evaluate_tool_catalog_policy(static_catalog)
            serialized_catalog = serialize_tool_catalog(static_catalog)
            catalog_json = json.dumps(
                serialized_catalog,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            raw_contract["allowed_tools"] = list(policy_result.allowed_tools)
            raw_contract["tool_catalog_json"] = catalog_json
            raw_contract["tool_catalog_fingerprint"] = hashlib.sha256(
                catalog_json.encode("utf-8")
            ).hexdigest()
        if self._normalize_execution_inputs_contract(raw_contract, require_bound=False) is None:
            raise OrchestratorError(
                message="Cannot create an invalid execution prompt/tool contract",
                details={"invalid": "execution_inputs"},
            )
        return raw_contract

    @staticmethod
    def _normalize_execution_inputs_contract(
        value: object,
        *,
        require_bound: bool,
    ) -> dict[str, object] | None:
        """Return a bounded plain copy of the exact durable input schema."""
        input_keys = frozenset(
            {
                "schema_version",
                "strategy",
                "allowed_tools",
                "tool_catalog_json",
                "tool_catalog_fingerprint",
                "context_pack_fragment",
                "execution_profile_json",
                "inherited_runtime_handle_json",
            }
        )
        strategy_keys = frozenset(
            {
                "schema_version",
                "resolver",
                "tools",
                "system_prompt_fragment",
                "task_prompt_suffix",
                "activity_map",
            }
        )
        try:
            if not _mapping_has_exact_keys(value, input_keys):
                return None
            assert isinstance(value, Mapping)
            if value.get("schema_version") != 2:
                return None
            raw_strategy = value.get("strategy")
            if not _mapping_has_exact_keys(raw_strategy, strategy_keys):
                return None
            assert isinstance(raw_strategy, Mapping)
            if raw_strategy.get("schema_version") != 1:
                return None
            resolver = raw_strategy.get("resolver")
            tools = raw_strategy.get("tools")
            system_fragment = raw_strategy.get("system_prompt_fragment")
            task_suffix = raw_strategy.get("task_prompt_suffix")
            raw_activity_map = raw_strategy.get("activity_map")
            context_pack_fragment = value.get("context_pack_fragment")
            execution_profile_json = value.get("execution_profile_json")
            inherited_runtime_handle_json = value.get("inherited_runtime_handle_json")
            if (
                resolver not in {"registry", "profile_backed"}
                or type(tools) is not list
                or not tools
                or len(tools) > _MAX_EXECUTION_STRATEGY_TOOLS
                or any(type(tool) is not str or not tool or len(tool) > 256 for tool in tools)
                or len(set(tools)) != len(tools)
                or type(system_fragment) is not str
                or not system_fragment
                or len(system_fragment) > _MAX_EXECUTION_STRATEGY_TEXT_CHARS
                or type(task_suffix) is not str
                or not task_suffix
                or len(task_suffix) > _MAX_EXECUTION_STRATEGY_TEXT_CHARS
                or type(raw_activity_map) is not list
                or len(raw_activity_map) > _MAX_EXECUTION_STRATEGY_TOOLS
                or type(context_pack_fragment) is not str
                or len(context_pack_fragment) > _MAX_EXECUTION_CONTEXT_FRAGMENT_CHARS
            ):
                return None
            normalized_activity: list[list[str]] = []
            activity_tools: set[str] = set()
            allowed_activity_values = {activity.value for activity in ActivityType}
            for row in raw_activity_map:
                if (
                    type(row) is not list
                    or len(row) != 2
                    or type(row[0]) is not str
                    or not row[0]
                    or len(row[0]) > 256
                    or row[0] in activity_tools
                    or row[0] not in tools
                    or type(row[1]) is not str
                    or row[1] not in allowed_activity_values
                ):
                    return None
                activity_tools.add(row[0])
                normalized_activity.append([row[0], row[1]])

            allowed_tools = value.get("allowed_tools")
            catalog_json = value.get("tool_catalog_json")
            catalog_fingerprint = value.get("tool_catalog_fingerprint")
            unbound = allowed_tools is None and catalog_json is None and catalog_fingerprint is None
            if unbound:
                if require_bound:
                    return None
                normalized_allowed_tools: list[str] | None = None
            else:
                if (
                    type(allowed_tools) is not list
                    or len(allowed_tools) > _MAX_EXECUTION_ALLOWED_TOOLS
                    or any(
                        type(tool) is not str or not tool or len(tool) > 256
                        for tool in allowed_tools
                    )
                    or len(set(allowed_tools)) != len(allowed_tools)
                    or type(catalog_json) is not str
                    or len(catalog_json) > _MAX_EXECUTION_TOOL_CATALOG_CHARS
                    or type(catalog_fingerprint) is not str
                    or len(catalog_fingerprint) != 64
                    or any(char not in "0123456789abcdef" for char in catalog_fingerprint)
                    or hashlib.sha256(catalog_json.encode("utf-8")).hexdigest()
                    != catalog_fingerprint
                ):
                    return None
                decoded_catalog = json.loads(catalog_json)
                if not isinstance(decoded_catalog, list | dict):
                    return None
                canonical_catalog = json.dumps(
                    decoded_catalog,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if canonical_catalog != catalog_json:
                    return None
                normalized_allowed_tools = list(allowed_tools)

            if execution_profile_json is None:
                normalized_profile_json: str | None = None
            else:
                if (
                    type(execution_profile_json) is not str
                    or len(execution_profile_json) > _MAX_EXECUTION_PROFILE_CHARS
                ):
                    return None
                decoded_profile = json.loads(execution_profile_json)
                if not isinstance(decoded_profile, dict):
                    return None
                normalized_profile = ExecutionProfile.model_validate(decoded_profile)
                canonical_profile = json.dumps(
                    normalized_profile.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if canonical_profile != execution_profile_json:
                    return None
                normalized_profile_json = execution_profile_json

            if inherited_runtime_handle_json is None:
                normalized_runtime_handle_json: str | None = None
            else:
                if (
                    type(inherited_runtime_handle_json) is not str
                    or len(inherited_runtime_handle_json) > _MAX_EXECUTION_RUNTIME_HANDLE_CHARS
                ):
                    return None
                decoded_runtime_handle = json.loads(inherited_runtime_handle_json)
                if not isinstance(decoded_runtime_handle, dict):
                    return None
                normalized_runtime_handle = RuntimeHandle.from_dict(decoded_runtime_handle)
                if normalized_runtime_handle is None:
                    return None
                canonical_runtime_handle = json.dumps(
                    normalized_runtime_handle.to_persisted_dict(),
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                if canonical_runtime_handle != inherited_runtime_handle_json:
                    return None
                normalized_runtime_handle_json = inherited_runtime_handle_json

            return {
                "schema_version": 2,
                "strategy": {
                    "schema_version": 1,
                    "resolver": resolver,
                    "tools": list(tools),
                    "system_prompt_fragment": system_fragment,
                    "task_prompt_suffix": task_suffix,
                    "activity_map": normalized_activity,
                },
                "context_pack_fragment": context_pack_fragment,
                "execution_profile_json": normalized_profile_json,
                "inherited_runtime_handle_json": normalized_runtime_handle_json,
                "allowed_tools": normalized_allowed_tools,
                "tool_catalog_json": catalog_json,
                "tool_catalog_fingerprint": catalog_fingerprint,
            }
        except Exception:
            return None

    def _execution_inputs_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> dict[str, object]:
        """Return the validated immutable provider-input population."""
        raw_inputs = (
            execution_contract.get("execution_inputs")
            if isinstance(execution_contract, Mapping)
            else None
        )
        normalized = self._normalize_execution_inputs_contract(
            raw_inputs,
            require_bound=require_bound,
        )
        if normalized is None:
            raise OrchestratorError(
                message="Cannot execute with an invalid prompt/tool input snapshot",
                details={"invalid": "execution_inputs"},
            )
        return normalized

    def _execution_strategy_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> _PersistedExecutionStrategy:
        normalized = self._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        strategy = normalized["strategy"]
        assert isinstance(strategy, dict)
        raw_activity = strategy["activity_map"]
        assert isinstance(raw_activity, list)
        return _PersistedExecutionStrategy(
            tools=tuple(strategy["tools"]),
            system_prompt_fragment=strategy["system_prompt_fragment"],
            task_prompt_suffix=strategy["task_prompt_suffix"],
            activity_map=tuple((row[0], ActivityType(row[1])) for row in raw_activity),
        )

    def _execution_context_pack_fragment_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> str:
        """Return context text resolved before the durable session existed."""
        inputs = self._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        fragment = inputs["context_pack_fragment"]
        assert isinstance(fragment, str)
        return fragment

    def _execution_profile_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> ExecutionProfile | None:
        """Restore the complete profile without rereading mutable YAML."""
        inputs = self._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        raw_profile = inputs["execution_profile_json"]
        if raw_profile is None:
            return None
        assert isinstance(raw_profile, str)
        return ExecutionProfile.model_validate(json.loads(raw_profile))

    def _execution_inherited_runtime_handle_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
        *,
        require_bound: bool,
    ) -> RuntimeHandle | None:
        """Restore parent conversation lineage from the durable snapshot."""
        inputs = self._execution_inputs_snapshot(
            execution_contract,
            require_bound=require_bound,
        )
        raw_handle = inputs["inherited_runtime_handle_json"]
        if raw_handle is None:
            return None
        assert isinstance(raw_handle, str)
        restored = RuntimeHandle.from_dict(json.loads(raw_handle))
        if restored is None:  # pragma: no cover - exact-schema validation owns this path
            raise OrchestratorError(message="Cannot restore inherited runtime authority")
        return restored

    def _bind_execution_tool_authority(
        self,
        execution_contract: Mapping[str, Any],
        *,
        merged_tools: list[str],
        tool_catalog: SessionToolCatalog,
    ) -> tuple[dict[str, Any], bool]:
        """Seal or validate the exact allowed tools and complete catalog."""
        normalized = self._normalize_execution_inputs_contract(
            execution_contract.get("execution_inputs"),
            require_bound=False,
        )
        if normalized is None:
            raise OrchestratorError(
                message="Cannot bind an invalid prompt/tool input snapshot",
                details={"invalid": "execution_inputs"},
            )
        serialized_catalog = serialize_tool_catalog(tool_catalog)
        try:
            catalog_json = json.dumps(
                serialized_catalog,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        except (TypeError, ValueError) as exc:
            raise OrchestratorError(
                message="Cannot persist a non-canonical tool catalog",
                details={"cause": type(exc).__name__},
            ) from exc
        if len(catalog_json) > _MAX_EXECUTION_TOOL_CATALOG_CHARS:
            raise OrchestratorError(message="Cannot persist an oversized tool catalog")
        current_allowed = list(merged_tools)
        if (
            len(current_allowed) > _MAX_EXECUTION_ALLOWED_TOOLS
            or any(type(tool) is not str or not tool or len(tool) > 256 for tool in current_allowed)
            or len(set(current_allowed)) != len(current_allowed)
        ):
            raise OrchestratorError(message="Cannot persist an invalid allowed-tool catalog")

        persisted_catalog = normalized["tool_catalog_json"]
        persisted_allowed = normalized["allowed_tools"]
        if persisted_catalog is not None:
            if persisted_catalog != catalog_json or persisted_allowed != current_allowed:
                raise OrchestratorError(
                    message="Cannot resume with changed prompt/tool authority",
                    details={
                        "resume_blocked": "execution_tool_authority_drift",
                        "hint": "Restore the original tool catalog or start a new session.",
                    },
                )
            return dict(execution_contract), False

        normalized["allowed_tools"] = current_allowed
        normalized["tool_catalog_json"] = catalog_json
        normalized["tool_catalog_fingerprint"] = hashlib.sha256(
            catalog_json.encode("utf-8")
        ).hexdigest()
        bound_contract = deepcopy(dict(execution_contract))
        bound_contract["execution_inputs"] = normalized
        proof = bound_contract.get("frugality_proof")
        if not isinstance(proof, dict):
            raise OrchestratorError(message="Cannot bind an invalid proof contract")
        proof["execution_inputs_fingerprint"] = self._execution_inputs_fingerprint(normalized)
        return bound_contract, True

    def _execution_semantics_contract(self) -> dict[str, object]:
        """Return every scalar setting that can change resumed AC effects."""
        backend_limits = resolve_backend_limits(self._adapter.runtime_backend)
        effective_parallel_workers = plan_fan_out_concurrency(
            self._max_parallel_workers,
            backend_limits,
        )
        return {
            "version": CURRENT_EXECUTION_SEMANTICS_VERSION,
            "run_verify_commands": self._run_verify_commands,
            "verify_command_timeout_seconds": self._verify_command_timeout_seconds,
            "verify_shell_identity": (
                dict(self._verify_shell_identity)
                if self._verify_shell_identity is not None
                else None
            ),
            "ac_retry_attempts": self._ac_retry_attempts,
            "cross_harness_redispatch": self._cross_harness_redispatch_enabled,
            "enable_decomposition": self._enable_decomposition,
            "decomposition_mode": self._decomposition_mode,
            "max_decomposition_depth": self._max_decomposition_depth,
            "max_parallel_workers": self._max_parallel_workers,
            "effective_parallel_workers": effective_parallel_workers,
            "adaptive_concurrency_policy": adaptive_concurrency_policy(
                initial_limit=effective_parallel_workers,
                max_limit=self._max_parallel_workers,
            ),
            "fat_harness_mode": self._fat_harness_mode,
            "shadow_replay_enabled": self._shadow_replay_enabled,
            "checkpoint_store_enabled": self._checkpoint_store is not None,
            "session_signal_hub_enabled": self._session_signal_hub is not None,
            "context_pack_enabled": self._context_pack_enabled,
            "backend_limits_backend": backend_limits.backend,
            "backend_max_concurrency": backend_limits.max_concurrency,
            "backend_requests_per_minute": backend_limits.requests_per_minute,
            "backend_tokens_per_minute": backend_limits.tokens_per_minute,
            "backend_self_governs_rate_limit": bool(
                getattr(self._adapter, "self_governs_rate_limit", False)
            ),
            "usage_limit_pause_seconds": get_usage_limit_pause_seconds(),
            "runtime_effect_capabilities": runtime_effect_capabilities_contract(self._adapter),
        }

    _valid_execution_semantics_contract = staticmethod(valid_execution_semantics_contract)
    _valid_legacy_preflight_execution_semantics_contract = staticmethod(
        valid_legacy_preflight_execution_semantics_contract
    )

    def _execution_semantics_snapshot(
        self,
        execution_contract: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Return one validated immutable-input snapshot from a durable contract."""
        raw_execution_semantics = (
            execution_contract.get("execution_semantics")
            if isinstance(execution_contract, Mapping)
            else None
        )
        if not self._valid_execution_semantics_contract(raw_execution_semantics):
            raise OrchestratorError(
                message="Cannot execute with an invalid execution-semantics snapshot",
                details={"invalid": "execution_semantics"},
            )
        return dict(raw_execution_semantics)

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

    def _runtime_execution_identity_contract(
        self,
        runtime_handle: RuntimeHandle | None = None,
    ) -> dict[str, Any]:
        """Return the adapter's canonical execution identity for resume."""
        # Foundation A process-local authority intentionally does not ask
        # arbitrary runtime providers for a portable identity.  The durable
        # identity contract added for native CLI runtimes is only trustworthy
        # for Ouroboros-owned process runtimes that define the fingerprinting
        # surface; test doubles and legacy/custom adapters remain process-local.
        from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
        from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
        from ouroboros.orchestrator.goose_runtime import GooseCliRuntime
        from ouroboros.orchestrator.grok_cli_runtime import GrokCliRuntime

        trusted_runtime_types = (
            CodexCliRuntime,
            CopilotCliRuntime,
            GeminiCLIRuntime,
            GooseCliRuntime,
            GrokCliRuntime,
        )
        if type(self._adapter) not in trusted_runtime_types:
            return {"version": 1, "observed": False}
        if getattr(self._adapter, "_skills_dir", None) is not None:
            return {"version": 1, "observed": False}
        dispatcher = getattr(self._adapter, "_skill_dispatcher", None)
        if dispatcher is not None and not self._runtime_skill_dispatcher_is_portable(dispatcher):
            return {"version": 1, "observed": False}
        if runtime_handle is None:
            return dict(runtime_execution_identity_contract(self._adapter))
        provider = object.__getattribute__(self._adapter, "execution_identity_contract")
        identity = provider(runtime_handle)
        if not isinstance(identity, Mapping):
            raise ValueError("runtime execution identity contract is not a mapping")
        normalized = dict(identity)
        if not normalized:
            return {"version": 1, "observed": False}
        return {"version": 1, "observed": True, "identity": normalized}

    @staticmethod
    def _runtime_skill_dispatcher_is_portable(dispatcher: object) -> bool:
        """Return True for packaged dispatchers that expose stable identity."""
        from ouroboros.orchestrator.command_dispatcher import CodexCommandDispatcher

        owner = getattr(dispatcher, "__self__", None)
        if (
            type(owner) is not CodexCommandDispatcher
            or getattr(dispatcher, "__func__", None) is not CodexCommandDispatcher.dispatch
        ):
            return False
        stable_identity = getattr(owner, "stable_identity_contract", None)
        if not callable(stable_identity):
            return False
        try:
            identity = stable_identity()
        except Exception:
            return False
        return (
            isinstance(identity, Mapping)
            and identity.get("kind") == "ouroboros_codex_command_dispatcher_v1"
        )

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
        generation, already_claimed = _claim_process_local_authority_generation(
            session_id,
            execution_id,
            raw_contract.get("foundation_a_authority"),
            self._adapter,
        )
        if generation is not None and self._task_workspace is not None:
            self._task_workspace_users.add((session_id, execution_id))
            self._task_workspace_lock_held = True
        return generation, already_claimed

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

    def _seal_process_local_prepared_contract(
        self,
        *,
        session_id: str,
        execution_id: str,
        generation: _ProcessLocalAuthorityGeneration,
        execution_contract: Mapping[str, object],
    ) -> None:
        """Bind the successfully persisted prepare contract to its live owner."""
        _seal_process_local_prepared_contract(
            session_id,
            execution_id,
            generation,
            self._adapter,
            execution_contract,
        )

    def _authenticate_process_local_prepared_contract(
        self,
        *,
        session_id: str,
        execution_id: str,
        generation: _ProcessLocalAuthorityGeneration,
        execution_contract: object,
    ) -> dict[str, Any] | None:
        """Recover the sealed snapshot only for an exact claimed caller copy."""
        return _authenticate_process_local_prepared_contract(
            session_id,
            execution_id,
            generation,
            self._adapter,
            execution_contract,
        )

    async def _reconstruct_precreated_durable_tracker(
        self,
        tracker: SessionTracker,
    ) -> Result[SessionTracker, OrchestratorError]:
        """Return the durable lifecycle owner for one prepared execution.

        Caller-owned trackers are immutable preparation receipts, not lifecycle
        authority. Every prepared dispatch observes the event-sourced status at
        this choke point before it may claim process-local execution authority.
        """
        try:
            durable_result = await self._session_repo.reconstruct_session(tracker.session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return Result.err(
                OrchestratorError(
                    message="Cannot verify durable prepared session state",
                    details={
                        "session_id": tracker.session_id,
                        "execution_id": tracker.execution_id,
                        "cause": str(exc),
                        "resume_blocked": "precreated_state_reconstruction_pending",
                        "retryable": True,
                    },
                )
            )
        if durable_result.is_err:
            return Result.err(
                OrchestratorError(
                    message="Cannot verify durable prepared session state",
                    details={
                        "session_id": tracker.session_id,
                        "execution_id": tracker.execution_id,
                        "cause": str(durable_result.error),
                        "resume_blocked": "precreated_state_reconstruction_pending",
                        "retryable": True,
                    },
                )
            )
        durable_tracker = durable_result.value
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
                        "durable_session_id": durable_tracker.session_id,
                        "durable_execution_id": durable_tracker.execution_id,
                        "resume_blocked": "precreated_session_identity_mismatch",
                    },
                )
            )
        return Result.ok(durable_tracker)

    @staticmethod
    def _precreated_non_running_error(
        durable_tracker: SessionTracker,
    ) -> OrchestratorError | None:
        """Reject every durable lifecycle state except a fresh RUNNING owner."""
        if durable_tracker.status is SessionStatus.RUNNING:
            return None
        if durable_tracker.status is SessionStatus.PAUSED:
            return OrchestratorError(
                message=(
                    "Session is paused; resume it through resume_session instead of "
                    "reusing a prepared tracker"
                ),
                details={
                    "session_id": durable_tracker.session_id,
                    "execution_id": durable_tracker.execution_id,
                    "status": durable_tracker.status.value,
                    "resume_blocked": "session_paused_use_resume",
                },
            )
        if durable_tracker.status in {
            SessionStatus.COMPLETED,
            SessionStatus.CANCELLED,
            SessionStatus.FAILED,
        }:
            return OrchestratorError(
                message=(
                    f"Session is in terminal state {durable_tracker.status.value}, cannot execute"
                ),
                details={
                    "session_id": durable_tracker.session_id,
                    "execution_id": durable_tracker.execution_id,
                    "status": durable_tracker.status.value,
                },
            )
        return OrchestratorError(
            message=(
                "Session is not in durable running state, cannot execute "
                f"({durable_tracker.status.value})"
            ),
            details={
                "session_id": durable_tracker.session_id,
                "execution_id": durable_tracker.execution_id,
                "status": durable_tracker.status.value,
                "resume_blocked": "precreated_session_not_running",
            },
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
        self._release_task_workspace_for_identity(
            session_id=session_id,
            execution_id=execution_id,
        )

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
        if self._task_workspace is not None:
            self._task_workspace_users.add((session_id, execution_id))
            self._task_workspace_lock_held = True

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
        self._task_workspace_users.discard((session_id, execution_id))
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
        acceptance_finalizations: list[dict[str, Any]] | None = None
        try:
            acceptance_finalizations = await collect_terminal_acceptance_plan(
                session_id=session_id,
                execution_id=execution_id,
                event_store=self._event_store,
                terminal_status=SessionStatus.FAILED.value,
            )
        except (Exception, asyncio.CancelledError) as exc:
            if isinstance(exc, asyncio.CancelledError):
                raise
            last_error = exc

        if acceptance_finalizations is None:
            self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
                execution_id=execution_id,
                status=SessionStatus.FAILED,
                error_message=error.message,
                error_details=dict(error.details),
                acceptance_finalizations=None,
            )
            return OrchestratorError(
                message="Failed to collect lost process-local authority acceptance plan",
                details={
                    "session_id": session_id,
                    "execution_id": execution_id,
                    "requested_status": SessionStatus.FAILED.value,
                    "cause": str(last_error),
                    "resume_blocked": "terminal_persistence_pending",
                    "terminal_persistence_pending": True,
                },
            )

        for attempt in range(3):
            try:
                result = await self._session_repo.mark_failed_if_active(
                    session_id,
                    error.message,
                    error.details,
                    acceptance_finalizations=acceptance_finalizations,
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
                        acceptance_finalizations=acceptance_finalizations,
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
            acceptance_finalizations=acceptance_finalizations,
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
        acceptance_finalizations: list[dict[str, Any]] | None = None
        try:
            # Preparation failures happen before the executor has emitted any
            # attempt telemetry.  The collector still reconstructs a complete
            # rejecting plan from the immutable session root set, so a current
            # session cannot become terminal with missing root decisions.
            acceptance_finalizations = await collect_terminal_acceptance_plan(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                event_store=self._event_store,
                terminal_status=SessionStatus.FAILED.value,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Fail closed when the durable telemetry/root contract is unreadable:
            # omitting the plan makes current-format EventStore CAS reject the
            # terminal transition and leaves a replayable pending intent.
            log.warning(
                "orchestrator.runner.prepare_terminal_plan_unavailable",
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                error=str(exc),
            )
        for attempt in range(3):
            try:
                mark_failed_kwargs: dict[str, Any] = {}
                if acceptance_finalizations is not None:
                    mark_failed_kwargs["acceptance_finalizations"] = acceptance_finalizations
                result = await self._session_repo.mark_failed(
                    tracker.session_id,
                    message,
                    dict(details),
                    **mark_failed_kwargs,
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
                        acceptance_finalizations=acceptance_finalizations,
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
            acceptance_finalizations=acceptance_finalizations,
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
            terminal_cleanup_completed = False
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
                        await self._cleanup_terminal_process_local_state(
                            session_id=session_id,
                            execution_id=execution_id,
                        )
                        terminal_cleanup_completed = True
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
                            await self._cleanup_terminal_process_local_state(
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                            terminal_cleanup_completed = True
                        else:
                            log.warning(
                                "orchestrator.runner.create_session_cancel_terminal_pending",
                                session_id=session_id,
                                execution_id=execution_id,
                                error=terminal_mark_error,
                            )
            if not terminal_cleanup_completed:
                self._release_task_workspace_for_identity(
                    session_id=session_id,
                    execution_id=execution_id,
                )
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
        acceptance_finalizations: list[dict[str, Any]] | None = None,
    ) -> SessionStatus:
        """Persist one terminal winner and return the authoritative status.

        Session lifecycle is the durable source of truth. Execution-terminal
        events are projections and must be emitted only after this CAS has a
        winner, otherwise completion and public cancellation can produce
        contradictory terminal streams.
        """
        terminal_plan = (
            [dict(item) for item in acceptance_finalizations]
            if acceptance_finalizations is not None
            else None
        )
        intent = _PendingLifecycleIntent(
            execution_id=execution_id,
            status=requested_status,
            summary=summary,
            error_message=error_message,
            error_details=error_details,
            error_type=error_type,
            messages_processed=messages_processed,
            cancelled_by=cancelled_by,
            acceptance_finalizations=terminal_plan,
        )
        result: Any = None
        last_error: object | None = None
        for attempt in range(3):
            try:
                acceptance_kwargs = (
                    {"acceptance_finalizations": terminal_plan} if terminal_plan is not None else {}
                )
                if requested_status is SessionStatus.COMPLETED:
                    result = await self._session_repo.mark_completed(
                        session_id,
                        summary,
                        messages_processed=messages_processed,
                        **acceptance_kwargs,
                    )
                elif requested_status is SessionStatus.FAILED:
                    result = await self._session_repo.mark_failed(
                        session_id,
                        error_message or "Execution failed",
                        error_details,
                        error_type=error_type,
                        messages_processed=messages_processed,
                        **acceptance_kwargs,
                    )
                elif requested_status is SessionStatus.CANCELLED:
                    result = await self._session_repo.mark_cancelled(
                        session_id,
                        reason=error_message or "Execution cancelled",
                        cancelled_by=cancelled_by,
                        **acceptance_kwargs,
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

        replayed_acceptance_finalizations = intent.acceptance_finalizations
        if intent.status is SessionStatus.CANCELLED and replayed_acceptance_finalizations is None:
            # ``None`` means the previous owner failed before it could obtain
            # the exact telemetry snapshot.  Never replay that intent as an
            # unplanned terminal cancellation: re-collect the plan first and
            # retain it for the next retry if persistence still fails.
            try:
                replayed_acceptance_finalizations = await collect_cancellation_acceptance_plan(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    event_store=self._event_store,
                )
            except asyncio.CancelledError:
                self._preserve_process_local_owner_for_retry(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
                raise
            except Exception as exc:
                return self._cancellation_persistence_pending_result(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    cause=exc,
                    acceptance_finalizations=None,
                    cancellation_reason=intent.error_message,
                    cancelled_by=intent.cancelled_by,
                )
            self._pending_lifecycle_intents[tracker.session_id] = replace(
                intent,
                acceptance_finalizations=[dict(item) for item in replayed_acceptance_finalizations],
            )
        elif (
            intent.status in {SessionStatus.COMPLETED, SessionStatus.FAILED}
            and replayed_acceptance_finalizations is None
        ):
            # Preparation and lost-authority failures may have been interrupted
            # before the first plan snapshot was retained. Reconstruct the exact
            # plan from durable attempt telemetry before retrying the terminal CAS.
            try:
                replayed_acceptance_finalizations = await collect_terminal_acceptance_plan(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    event_store=self._event_store,
                    terminal_status=intent.status.value,
                )
            except asyncio.CancelledError:
                self._preserve_process_local_owner_for_retry(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
                raise
            except Exception as exc:
                return self._terminal_persistence_pending_result(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    requested_status=intent.status,
                    cause=exc,
                )
            self._pending_lifecycle_intents[tracker.session_id] = replace(
                intent,
                acceptance_finalizations=[dict(item) for item in replayed_acceptance_finalizations],
            )

        resolved_status: SessionStatus
        if intent.status is SessionStatus.PAUSED and intent.pause is None:
            return Result.err(
                OrchestratorError(
                    message="Pending PAUSED intent is missing its replay payload",
                    details={
                        "session_id": tracker.session_id,
                        "execution_id": tracker.execution_id,
                        "resume_blocked": "pending_lifecycle_payload_missing",
                    },
                )
            )

        try:
            if intent.status is SessionStatus.PAUSED:
                pause = cast(RecoverableFailurePause, intent.pause)
                pause_result = await self._session_repo.mark_paused(
                    tracker.session_id,
                    reason=pause.reason,
                    resume_hint=pause.resume_hint,
                    pause_seconds=pause.pause_seconds,
                    resume_after=pause.resume_after,
                    pause_kind=pause.pause_kind,
                    pause_owner=(
                        pause.coordinator_owner.owner_payload()
                        if pause.coordinator_owner is not None
                        else None
                    ),
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
                    acceptance_finalizations=replayed_acceptance_finalizations,
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
                self._release_task_workspace_for_identity(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
            else:
                await self._cleanup_terminal_process_local_state(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )

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

    @staticmethod
    def _runtime_execution_authorizes_automatic_codex_resume(_value: object) -> bool:
        """Return whether an unpinned Codex default may resume portably.

        It may not. Codex App/CLI can select a concrete model outside files that
        Ouroboros can observe. Config/profile fingerprints are still useful drift
        evidence, but they do not prove which model served the original run.
        Until Codex exposes that concrete selection, durable resume must require
        a pinned/observed model, native per-call routing, or process-local
        authority from a prepared live execution.
        """
        return False

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
        execution_inputs_contract: Mapping[str, Any] | None = None,
        project_identity: ProjectIdentity | None,
        runtime_handle: RuntimeHandle | None = None,
    ) -> dict[str, Any]:
        """Build the durable resolved inputs shared by resume and proof cohorting."""
        from ouroboros.orchestrator.model_routing import serialize_model_router
        from ouroboros.orchestrator.route_compat import (
            build_route_compat_projection,
            serialize_route_compat_contract,
        )

        guidance_bundle = self._ensure_new_run_guidance()
        execution_semantics = self._execution_semantics_contract()
        if execution_inputs_contract is None:
            execution_inputs = self._build_execution_inputs_contract(seed)
        else:
            normalized_inputs = self._normalize_execution_inputs_contract(
                execution_inputs_contract,
                require_bound=True,
            )
            if normalized_inputs is None:
                raise OrchestratorError(
                    message="Cannot preserve an invalid execution input contract",
                    details={"invalid": "execution_inputs"},
                )
            execution_inputs = normalized_inputs
        routing_contract = serialize_model_router(self._model_router)
        routing_contract["requested_model_tier"] = self._requested_model_tier
        # Effort routing is independent of model-tier routing. Persist it even
        # when the optional model router is dormant so resume cannot silently
        # adopt a different provider-effect policy.
        routing_contract["reasoning_effort"] = self._reasoning_effort
        route_projection = build_route_compat_projection(
            self._route_economics,
            model_router=self._model_router,
            runtime_backend=getattr(self._adapter, "runtime_backend", None),
            effort=self._reasoning_effort,
        )
        routing_contract["route_compat"] = serialize_route_compat_contract(route_projection)
        # This base effort reaches both direct runner dispatches and the
        # parallel executor. Persist it beside model routing so a config/env
        # change cannot silently alter ``model_reasoning_effort`` on resume.
        routing_contract["base_reasoning_effort"] = self._reasoning_effort
        routing_contract["constructor_model"] = self._constructor_model_contract()
        routing_contract["runtime_execution"] = self._runtime_execution_identity_contract(
            runtime_handle
        )
        routing_contract["runtime_backend"] = self._runtime_backend_contract()
        routing_contract["llm_backend"] = self._llm_backend_contract()
        routing_contract["permission_mode"] = self._permission_mode_contract()
        proof_contract: dict[str, Any] = {
            "protocol_version": FRUGALITY_PROOF_PROTOCOL_VERSION,
            "routing_fingerprint": self._routing_fingerprint(routing_contract),
            "execution_semantics_fingerprint": self._execution_semantics_fingerprint(
                execution_semantics
            ),
            "execution_inputs_fingerprint": self._execution_inputs_fingerprint(execution_inputs),
        }
        workspace_identity = self._resolved_proof_workspace_identity(project_identity)
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
        contract = {
            "version": EXECUTION_CONTRACT_VERSION,
            "foundation_a_authority": authority_contract,
            "execution_preferences": self._execution_preferences.to_contract_data(),
            "execution_semantics": execution_semantics,
            "execution_inputs": execution_inputs,
            "model_routing": routing_contract,
            "frugality_proof": proof_contract,
            "guidance": self._guidance_contract(guidance_bundle),
            "resume": {
                "workspace": self._resume_workspace_identity(),
            },
        }
        if frozenset(contract) != EXECUTION_CONTRACT_V9_TOP_LEVEL_KEYS:
            raise AssertionError("execution contract v9 builder emitted a non-canonical shape")
        return contract

    def _build_new_session_contract(
        self,
        *,
        seed: Seed,
        authority_generation: _ProcessLocalAuthorityGeneration,
        runtime_handle: RuntimeHandle | None = None,
    ) -> tuple[dict[str, Any], ProjectIdentity]:
        """Resolve one project identity and bind it to both publication surfaces."""
        project_identity = self._project_identity()
        if project_identity is None:
            raise OrchestratorError(
                message="Cannot start a session without a resolved project identity",
                details={"invalid": "project_identity"},
            )
        contract = self._build_execution_contract(
            seed=seed,
            authority_generation=authority_generation,
            project_identity=project_identity,
            runtime_handle=runtime_handle,
        )
        return contract, project_identity

    async def _emit_run_configuration_resolved(
        self,
        *,
        execution_id: str,
        session_id: str,
    ) -> None:
        """Persist the user-facing run configuration before any AC dispatch."""
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
                    "alternate_harness_enabled": self._cross_harness_redispatch_enabled,
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

    @staticmethod
    def _serialize_parallel_resume_plan(execution_plan: Any) -> dict[str, object]:
        """Seal the exact dependency plan needed by the parallel resume owner."""

        return {
            "schema_version": 1,
            "nodes": [
                {
                    "index": node.index,
                    "depends_on": list(node.depends_on),
                    "can_run_independently": node.can_run_independently,
                    "requires_serial_stage": node.requires_serial_stage,
                    "serialization_reasons": list(node.serialization_reasons),
                }
                for node in execution_plan.nodes
            ],
            "stages": [
                {
                    "index": stage.index,
                    "ac_indices": list(stage.ac_indices),
                    "depends_on_stages": list(stage.depends_on_stages),
                }
                for stage in execution_plan.stages
            ],
        }

    @staticmethod
    def _parallel_process_local_resume_nonce(tracker: Any) -> str | None:
        """Reuse the live Foundation A generation across parallel executors."""

        progress = getattr(tracker, "progress", None)
        raw_contract = (
            progress.get(EXECUTION_CONTRACT_PROGRESS_KEY) if isinstance(progress, Mapping) else None
        )
        authority = (
            raw_contract.get("foundation_a_authority")
            if isinstance(raw_contract, Mapping)
            else None
        )
        correlation_id = authority.get("correlation_id") if isinstance(authority, Mapping) else None
        if correlation_id is None:
            # Low-level unit callers may invoke _execute_parallel without the
            # outer run lifecycle. Production new/resume paths always carry the
            # Foundation A contract and therefore take the stable branch below.
            return None
        if (
            not isinstance(correlation_id, str)
            or len(correlation_id) != 32
            or any(char not in "0123456789abcdef" for char in correlation_id)
        ):
            raise OrchestratorError(message="Invalid process-local authority for parallel resume")
        return correlation_id

    @staticmethod
    def _deserialize_parallel_resume_plan(seed: Seed, raw: object) -> Any:
        """Restore a bounded exact plan or fail before any analyzer/provider effect."""

        from ouroboros.orchestrator.dependency_analyzer import (
            ACNode,
            ExecutionStage,
            StagedExecutionPlan,
        )

        if not isinstance(raw, Mapping) or not _mapping_has_exact_keys(
            raw, frozenset({"schema_version", "nodes", "stages"})
        ):
            raise OrchestratorError(message="Invalid persisted parallel resume plan")
        nodes_raw = raw.get("nodes")
        stages_raw = raw.get("stages")
        ac_count = len(seed.acceptance_criteria)
        if (
            raw.get("schema_version") != 1
            or type(nodes_raw) is not list
            or len(nodes_raw) != ac_count
            or type(stages_raw) is not list
            or len(stages_raw) > ac_count
            or (ac_count > 0 and not stages_raw)
        ):
            raise OrchestratorError(message="Invalid persisted parallel resume plan")
        nodes: list[ACNode] = []
        for expected_index, value in enumerate(nodes_raw):
            if not isinstance(value, Mapping) or not _mapping_has_exact_keys(
                value,
                frozenset(
                    {
                        "index",
                        "depends_on",
                        "can_run_independently",
                        "requires_serial_stage",
                        "serialization_reasons",
                    }
                ),
            ):
                raise OrchestratorError(message="Invalid persisted parallel resume plan")
            depends_on = value.get("depends_on")
            reasons = value.get("serialization_reasons")
            if (
                type(value.get("index")) is not int
                or value.get("index") != expected_index
                or type(depends_on) is not list
                or len(depends_on) > ac_count
                or any(type(index) is not int for index in depends_on)
                or len(set(depends_on)) != len(depends_on)
                or any(not 0 <= index < ac_count or index == expected_index for index in depends_on)
                or type(value.get("can_run_independently")) is not bool
                or type(value.get("requires_serial_stage")) is not bool
                or type(reasons) is not list
                or len(reasons) > ac_count
                or any(
                    type(reason) is not str or not reason or len(reason) > 256 for reason in reasons
                )
            ):
                raise OrchestratorError(message="Invalid persisted parallel resume plan")
            nodes.append(
                ACNode(
                    index=expected_index,
                    content=ac_text(seed.acceptance_criteria[expected_index]),
                    depends_on=tuple(depends_on),
                    can_run_independently=value["can_run_independently"],
                    requires_serial_stage=value["requires_serial_stage"],
                    serialization_reasons=tuple(reasons),
                )
            )
        stages: list[ExecutionStage] = []
        seen_acs: set[int] = set()
        stage_by_ac: dict[int, int] = {}
        for expected_stage, value in enumerate(stages_raw):
            if not isinstance(value, Mapping) or not _mapping_has_exact_keys(
                value,
                frozenset({"index", "ac_indices", "depends_on_stages"}),
            ):
                raise OrchestratorError(message="Invalid persisted parallel resume plan")
            ac_indices = value.get("ac_indices")
            dependencies = value.get("depends_on_stages")
            if (
                type(value.get("index")) is not int
                or value.get("index") != expected_stage
                or type(ac_indices) is not list
                or not ac_indices
                or len(ac_indices) > ac_count
                or any(type(index) is not int or not 0 <= index < ac_count for index in ac_indices)
                or len(set(ac_indices)) != len(ac_indices)
                or seen_acs.intersection(ac_indices)
                or type(dependencies) is not list
                or len(dependencies) > expected_stage
                or any(
                    type(index) is not int or not 0 <= index < expected_stage
                    for index in dependencies
                )
                or len(set(dependencies)) != len(dependencies)
            ):
                raise OrchestratorError(message="Invalid persisted parallel resume plan")
            stage = ExecutionStage(
                index=expected_stage,
                ac_indices=tuple(ac_indices),
                depends_on_stages=tuple(dependencies),
            )
            stages.append(stage)
            seen_acs.update(ac_indices)
            stage_by_ac.update(dict.fromkeys(ac_indices, expected_stage))
        if seen_acs != set(range(ac_count)):
            raise OrchestratorError(message="Invalid persisted parallel resume plan")
        for node in nodes:
            node_stage = stage_by_ac[node.index]
            stage_dependencies = set(stages[node_stage].depends_on_stages)
            if any(
                stage_by_ac[dependency] >= node_stage
                or stage_by_ac[dependency] not in stage_dependencies
                for dependency in node.depends_on
            ):
                raise OrchestratorError(message="Invalid persisted parallel resume plan")
        return StagedExecutionPlan(nodes=tuple(nodes), stages=tuple(stages))

    @staticmethod
    def _serialize_parallel_external_satisfaction(
        seed: Seed,
        values: dict[int, dict[str, Any]] | None,
    ) -> dict[str, dict[str, str | None]]:
        """Seal the partial-stage ``--skip-completed`` authority for resume."""

        serialized: dict[str, dict[str, str | None]] = {}
        for ac_index, metadata in (values or {}).items():
            if type(ac_index) is not int or not 0 <= ac_index < len(seed.acceptance_criteria):
                raise OrchestratorError(message="Invalid externally satisfied AC index")
            reason = metadata.get("reason") if isinstance(metadata, Mapping) else None
            commit = metadata.get("commit") if isinstance(metadata, Mapping) else None
            if reason is not None and not isinstance(reason, str):
                raise OrchestratorError(message="Invalid externally satisfied AC reason")
            if commit is not None and not isinstance(commit, str):
                raise OrchestratorError(message="Invalid externally satisfied AC commit")
            serialized[str(ac_index)] = {"reason": reason, "commit": commit}
        return serialized

    @staticmethod
    def _deserialize_parallel_external_satisfaction(
        seed: Seed,
        raw: object,
    ) -> dict[int, dict[str, Any]]:
        """Restore the exact skip map or fail before a provider effect."""

        if not isinstance(raw, Mapping) or len(raw) > len(seed.acceptance_criteria):
            raise OrchestratorError(message="Invalid persisted external satisfaction state")
        restored: dict[int, dict[str, Any]] = {}
        for raw_index, metadata in raw.items():
            if not isinstance(raw_index, str) or not raw_index.isdecimal():
                raise OrchestratorError(message="Invalid persisted external satisfaction state")
            ac_index = int(raw_index)
            if not 0 <= ac_index < len(seed.acceptance_criteria):
                raise OrchestratorError(message="Invalid persisted external satisfaction state")
            if not isinstance(metadata, Mapping) or not _mapping_has_exact_keys(
                metadata, frozenset({"reason", "commit"})
            ):
                raise OrchestratorError(message="Invalid persisted external satisfaction state")
            reason = metadata.get("reason")
            commit = metadata.get("commit")
            if (reason is not None and not isinstance(reason, str)) or (
                commit is not None and not isinstance(commit, str)
            ):
                raise OrchestratorError(message="Invalid persisted external satisfaction state")
            restored[ac_index] = {"reason": reason, "commit": commit}
        return restored

    def _restore_execution_contract(
        self,
        progress: Mapping[str, Any],
        *,
        seed: Seed | None = None,
        authority_generation: _ProcessLocalAuthorityGeneration | None = None,
        require_bound_execution_inputs: bool = True,
        prepared_live_execution: bool = False,
        runtime_handle: RuntimeHandle | None = None,
    ) -> bool:
        """Restore the persisted router unless this invocation explicitly overrides it.

        Returns whether a replacement contract (an explicit override or one-time
        legacy migration) should be checkpointed for subsequent resumes. A present
        malformed contract blocks resume; it is never reinterpreted as a legacy
        session or allowed to change models silently. ``prepared_live_execution``
        admits only the explicit unobservable states emitted by the new-run builder;
        the caller must already hold and match that run's sealed process-local
        contract, while the normal durable resume path remains strict.
        """
        if EXECUTION_CONTRACT_PROGRESS_KEY not in progress:
            raise OrchestratorError(
                message="Cannot resume a session without durable effect inputs",
                details={
                    "contract_version": None,
                    "resume_blocked": "execution_inputs_unavailable",
                    "hint": "Start a new session under the current execution contract.",
                },
            )
        raw_contract = progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)

        raw_version = raw_contract.get("version") if isinstance(raw_contract, Mapping) else None
        if (
            not isinstance(raw_contract, Mapping)
            or isinstance(raw_version, bool)
            or not isinstance(raw_version, int)
            or raw_version
            not in {
                PRE_ROUTE_ADMISSION_EXECUTION_CONTRACT_VERSION,
                PRE_REQUESTED_TIER_EXECUTION_CONTRACT_VERSION,
                PRE_EXECUTION_SEMANTICS_EXECUTION_CONTRACT_VERSION,
                PRE_EXECUTION_INPUTS_EXECUTION_CONTRACT_VERSION,
                PRE_RESOLVED_EFFECT_INPUTS_EXECUTION_CONTRACT_VERSION,
                PRE_DURABLE_PAUSE_POLICY_EXECUTION_CONTRACT_VERSION,
                PRE_RUNTIME_EFFECT_CAPABILITIES_EXECUTION_CONTRACT_VERSION,
                EXECUTION_CONTRACT_VERSION,
            }
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"contract_version": raw_version},
            )

        if raw_version != EXECUTION_CONTRACT_VERSION:
            # Every older version may already have dispatched provider effects,
            # but none sealed the complete v9 effect population, including the
            # runtime capability/vocabulary that builds provider-call kwargs.
            # Reconstructing any missing field from current runtime state would
            # change replay authorization.
            raise OrchestratorError(
                message="Cannot resume a session without durable effect inputs",
                details={
                    "contract_version": raw_version,
                    "resume_blocked": "execution_inputs_unavailable",
                    "hint": "Start a new session under the current execution contract.",
                },
            )

        raw_contract = _require_exact_execution_contract_v9(raw_contract)

        migrate_v2_contract = raw_version == PRE_ROUTE_ADMISSION_EXECUTION_CONTRACT_VERSION
        migrate_v3_contract = raw_version == PRE_REQUESTED_TIER_EXECUTION_CONTRACT_VERSION
        migrate_v4_contract = raw_version == PRE_EXECUTION_SEMANTICS_EXECUTION_CONTRACT_VERSION
        migrate_legacy_contract = migrate_v2_contract or migrate_v3_contract or migrate_v4_contract
        raw_proof = raw_contract.get("frugality_proof")
        raw_routing = raw_contract.get("model_routing")
        raw_resume = raw_contract.get("resume")
        raw_preferences = raw_contract.get("execution_preferences")
        preferences_migrated = "execution_preferences" not in raw_contract
        raw_execution_semantics = raw_contract.get("execution_semantics")
        raw_execution_inputs = raw_contract.get("execution_inputs")
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

        pre_adaptive_rejection = pre_adaptive_execution_semantics_rejection(
            raw_execution_semantics,
            raw_proof.get("execution_semantics_fingerprint"),
            fingerprint=self._execution_semantics_fingerprint,
        )
        if pre_adaptive_rejection is not None:
            raise OrchestratorError(
                message=pre_adaptive_rejection.message,
                details=pre_adaptive_rejection.details,
            )

        migrated_verify_shell_semantics = migrated_pre_verify_shell_execution_semantics(
            raw_execution_semantics
        )
        if migrated_verify_shell_semantics is not None:
            persisted_v4_fingerprint = raw_proof.get("execution_semantics_fingerprint")
            if not isinstance(
                persisted_v4_fingerprint, str
            ) or persisted_v4_fingerprint != self._execution_semantics_fingerprint(
                raw_execution_semantics
            ):
                raise OrchestratorError(
                    message="Cannot resume with an invalid pre-verify-shell contract",
                    details={"invalid": "execution_semantics_fingerprint"},
                )
            migrated_contract = deepcopy(dict(raw_contract))
            migrated_proof = migrated_contract["frugality_proof"]
            assert isinstance(migrated_proof, dict)
            migrated_contract["execution_semantics"] = migrated_verify_shell_semantics
            migrated_proof["execution_semantics_fingerprint"] = (
                self._execution_semantics_fingerprint(migrated_verify_shell_semantics)
            )
            raw_contract = migrated_contract
            raw_proof = migrated_proof
            raw_execution_semantics = migrated_verify_shell_semantics
            self._verify_shell_identity = None

        migrate_preflight_contract = self._valid_legacy_preflight_execution_semantics_contract(
            raw_execution_semantics
        )
        if migrate_preflight_contract:
            persisted_legacy_fingerprint = raw_proof.get("execution_semantics_fingerprint")
            if not isinstance(
                persisted_legacy_fingerprint, str
            ) or persisted_legacy_fingerprint != self._execution_semantics_fingerprint(
                raw_execution_semantics
            ):
                raise OrchestratorError(
                    message="Cannot resume with an invalid legacy preflight contract",
                    details={"invalid": "execution_semantics_fingerprint"},
                )
            migrated_contract = deepcopy(dict(raw_contract))
            migrated_semantics = migrated_contract["execution_semantics"]
            migrated_proof = migrated_contract["frugality_proof"]
            assert isinstance(migrated_semantics, dict)
            assert isinstance(migrated_proof, dict)
            migrated_semantics["decomposition_mode"] = "bounce_only"
            migrated_proof["execution_semantics_fingerprint"] = (
                self._execution_semantics_fingerprint(migrated_semantics)
            )
            raw_contract = migrated_contract
            raw_proof = migrated_proof
            raw_execution_semantics = migrated_semantics

        # Version 2 predates effort/Route B fields; version 3 predates the
        # independent requested-tier field; version 4 predates the complete
        # executor-semantics snapshot. Only those exact shapes migrate.
        # A malformed current contract must never fall through either path.
        if migrate_legacy_contract and (
            "execution_semantics" in raw_contract
            or "execution_semantics_fingerprint" in raw_proof
            or "execution_inputs" in raw_contract
            or "execution_inputs_fingerprint" in raw_proof
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": f"version {raw_version} execution semantics extension"},
            )
        if migrate_v2_contract and (
            "reasoning_effort" in raw_routing
            or "route_compat" in raw_routing
            or "requested_model_tier" in raw_routing
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "version 2 routing extension"},
            )
        if migrate_v3_contract and "requested_model_tier" in raw_routing:
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "version 3 routing extension"},
            )

        self._restore_guidance_contract(raw_contract)

        protocol_version = raw_proof.get("protocol_version")
        persisted_project_root = raw_proof.get("project_root")
        persisted_workspace_path = raw_proof.get("workspace_path")
        persisted_routing_fingerprint = raw_proof.get("routing_fingerprint")
        persisted_execution_semantics_fingerprint = raw_proof.get("execution_semantics_fingerprint")
        persisted_execution_inputs_fingerprint = raw_proof.get("execution_inputs_fingerprint")
        persisted_seed_fingerprint = raw_proof.get("seed_fingerprint")
        persisted_constructor_model = raw_routing.get("constructor_model")
        persisted_runtime_execution = raw_routing.get("runtime_execution")
        persisted_runtime_backend = raw_routing.get("runtime_backend")
        persisted_llm_backend = raw_routing.get("llm_backend")
        persisted_permission_mode = raw_routing.get("permission_mode")
        persisted_requested_model_tier = raw_routing.get("requested_model_tier")
        persisted_reasoning_effort = (
            self._reasoning_effort if migrate_v2_contract else raw_routing.get("reasoning_effort")
        )
        persisted_base_reasoning_effort = raw_routing.get("base_reasoning_effort", _MISSING)
        persisted_resume_workspace = raw_resume.get("workspace")
        base_reasoning_effort_missing = persisted_base_reasoning_effort is _MISSING
        if base_reasoning_effort_missing and not migrate_v2_contract:
            # Pre-base-effort contracts already persisted the same dispatch effort
            # under model_routing.reasoning_effort.  Treat that value as the
            # historical base effort instead of rejecting otherwise valid v9
            # checkpoints.
            persisted_base_reasoning_effort = persisted_reasoning_effort
        valid_base_reasoning_effort = (persisted_base_reasoning_effort is None) or (
            isinstance(persisted_base_reasoning_effort, str)
            and persisted_base_reasoning_effort in {"low", "medium", "high", "xhigh"}
        )
        valid_seed_fingerprint = (
            isinstance(persisted_seed_fingerprint, str)
            and len(persisted_seed_fingerprint) == 64
            and all(char in "0123456789abcdef" for char in persisted_seed_fingerprint)
        )
        normalized_execution_inputs = (
            None
            if migrate_legacy_contract
            else self._normalize_execution_inputs_contract(
                raw_execution_inputs,
                require_bound=require_bound_execution_inputs,
            )
        )
        if (
            isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or protocol_version != FRUGALITY_PROOF_PROTOCOL_VERSION
            or not (
                isinstance(persisted_project_root, str)
                and bool(persisted_project_root.strip())
                and isinstance(persisted_workspace_path, str)
                and bool(persisted_workspace_path.strip())
                or prepared_live_execution
                and persisted_project_root is None
                and persisted_workspace_path is None
            )
            or not isinstance(persisted_routing_fingerprint, str)
            or persisted_routing_fingerprint != self._routing_fingerprint(raw_routing)
            or (
                not migrate_legacy_contract
                and (
                    not self._valid_execution_semantics_contract(raw_execution_semantics)
                    or not isinstance(persisted_execution_semantics_fingerprint, str)
                    or persisted_execution_semantics_fingerprint
                    != self._execution_semantics_fingerprint(raw_execution_semantics)
                )
            )
            or (
                not migrate_legacy_contract
                and (
                    normalized_execution_inputs is None
                    or not isinstance(persisted_execution_inputs_fingerprint, str)
                    or persisted_execution_inputs_fingerprint
                    != self._execution_inputs_fingerprint(normalized_execution_inputs)
                )
            )
            or (seed is not None and not valid_seed_fingerprint)
            or not (
                self._valid_constructor_model_contract(persisted_constructor_model)
                or (prepared_live_execution and persisted_constructor_model == {"observed": False})
            )
            or not self._valid_runtime_execution_identity_contract(persisted_runtime_execution)
            or not (
                isinstance(persisted_runtime_backend, str)
                and bool(persisted_runtime_backend.strip())
                or prepared_live_execution
                and persisted_runtime_backend is None
            )
            or not (
                isinstance(persisted_llm_backend, str)
                and bool(persisted_llm_backend.strip())
                or prepared_live_execution
                and persisted_llm_backend is None
            )
            or not self._valid_permission_mode_contract(persisted_permission_mode)
            or (
                not migrate_v2_contract
                and (
                    "reasoning_effort" not in raw_routing
                    or persisted_reasoning_effort not in {None, "low", "medium", "high", "xhigh"}
                )
            )
            or (
                not (migrate_v2_contract or migrate_v3_contract)
                and (
                    "requested_model_tier" not in raw_routing
                    or persisted_requested_model_tier
                    not in {None, "frugal", "standard", "frontier"}
                )
            )
            or not (
                isinstance(persisted_resume_workspace, Mapping)
                or prepared_live_execution
                and persisted_resume_workspace is None
            )
            or not valid_base_reasoning_effort
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "proof identity"},
            )

        persisted_preferences = execution_preferences_from_contract(raw_preferences)
        if persisted_preferences is None:
            raise OrchestratorError(
                message="Cannot resume with invalid execution preferences",
                details={"invalid": "execution_preferences"},
            )
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

        if persisted_base_reasoning_effort != self._reasoning_effort:
            raise OrchestratorError(
                message="Cannot resume with a different reasoning effort",
                details={
                    "persisted_base_reasoning_effort": persisted_base_reasoning_effort,
                    "current_base_reasoning_effort": self._reasoning_effort,
                    "hint": "Start a new session after changing reasoning effort.",
                },
            )

        current_seed_fingerprint = (
            self._seed_semantics_fingerprint(seed) if seed is not None else None
        )
        persisted_workspace = (
            None
            if persisted_project_root is None and persisted_workspace_path is None
            else {
                "project_root": persisted_project_root,
                "workspace_path": persisted_workspace_path,
            }
        )
        has_project_anchor, start_project_identity = self._project_start_identity(progress)
        if has_project_anchor:
            assert start_project_identity is not None
            active_project_identity = self._project_identity()
            if (
                persisted_workspace != start_project_identity.to_workspace_data()
                or active_project_identity != start_project_identity
            ):
                raise OrchestratorError(
                    message="Cannot resume with conflicting project identity",
                    details={
                        "persisted_workspace": persisted_workspace,
                        "start_project_identity": start_project_identity.to_event_data(),
                        "current_project_identity": (
                            active_project_identity.to_event_data()
                            if active_project_identity is not None
                            else None
                        ),
                    },
                )
            active_workspace = active_project_identity.to_workspace_data()
        else:
            # Historical v9 session starts predate the additive project anchor.
            # Preserve their exact direct-cwd representation rather than
            # rewriting durable resume authority under the new resolver.
            # Transitional (#1799): this branch is a package-wide
            # project-identity support contract — see the removal criterion in
            # orchestrator/legacy_identity.py and docs/rfc/project-map-v1.md.
            # It does not bypass other resume gates. Removal is allowed only
            # after the documented identity-compatibility window ends and must
            # replace this path with a typed fail-closed rejection.
            #
            # Current prepared executions intentionally restore an anchorless
            # contract-only mapping; only a durable historical start snapshot
            # that still lacks the anchor counts as a legacy activation.
            raw_start_snapshot = progress.get(SESSION_START_IDENTITY_PROGRESS_KEY)
            if isinstance(raw_start_snapshot, Mapping):
                note_legacy_identity_path(
                    "resume_workspace_comparison",
                    prepared_live_execution=prepared_live_execution,
                )
            active_workspace = (
                self._proof_workspace_identity()
                if prepared_live_execution
                else self._legacy_proof_workspace_identity()
            )
        if active_workspace != persisted_workspace:
            raise OrchestratorError(
                message="Cannot resume from a different project workspace",
                details={
                    "persisted_workspace": persisted_workspace,
                    "current_workspace": active_workspace,
                    "hint": "Resume from the original project/workspace.",
                },
            )
        replacement_project_identity = start_project_identity
        if replacement_project_identity is None and persisted_workspace is not None:
            # A pre-anchor session owns its historical proof representation in
            # the persisted contract.  Rebuilding for a routing override must
            # carry that representation forward; recomputing under the current
            # resolver would make the following resume disagree with the
            # immutable, still-unanchored start event.
            try:
                replacement_project_identity = ProjectIdentity.from_root(
                    persisted_workspace["project_root"],
                    workspace_path=persisted_workspace["workspace_path"],
                    require_exists=True,
                )
            except ProjectIdentityError as exc:
                raise self._project_identity_error(exc) from exc
        active_resume_workspace = self._resume_workspace_identity()
        normalized_persisted_resume_workspace = (
            dict(persisted_resume_workspace)
            if isinstance(persisted_resume_workspace, Mapping)
            else None
        )
        if active_resume_workspace != normalized_persisted_resume_workspace:
            raise OrchestratorError(
                message="Cannot resume from a different execution workspace",
                details={
                    "persisted_workspace": normalized_persisted_resume_workspace,
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
        current_runtime_execution = self._runtime_execution_identity_contract(runtime_handle)
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
            not (migrate_v2_contract or migrate_v3_contract)
            and restored_router is None
            and persisted_requested_model_tier is not None
        ):
            raise OrchestratorError(
                message="Cannot resume with an invalid execution contract",
                details={"invalid": "requested_model_tier"},
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
        if migrate_v2_contract or migrate_v3_contract:
            persisted_requested_model_tier = (
                restored_router.base_tier if restored_router is not None else None
            )
        authoritative_router = self._authoritative_model_router(
            persisted_preferences,
            requested_model_tier=persisted_requested_model_tier,
        )
        if (
            not self._model_routing_override_explicit
            and restored_router is not None
            and restored_router != authoritative_router
        ):
            raise OrchestratorError(
                message="Cannot resume with a changed model-routing policy",
                details={
                    "runtime_backend": persisted_runtime_backend,
                    "hint": "Restore the current route policy or start a new session.",
                },
            )
        raw_route_compat = raw_routing.get("route_compat")
        if migrate_v2_contract:
            # The v2 router has already been compared with the router rebuilt
            # from current config/backend/preferences. Its replacement v3
            # contract below adds the independently derived Route B projection.
            pass
        elif raw_route_compat is None:
            raise OrchestratorError(
                message="Cannot resume without an explicit route compatibility contract",
                details={"invalid": "route_compat", "reason": "missing"},
            )
        else:
            from ouroboros.orchestrator.route_compat import (
                deserialize_route_compat_contract,
                validate_route_compat_projection,
            )

            route_compat_recognized, restored_projection = deserialize_route_compat_contract(
                raw_route_compat
            )
            if not route_compat_recognized:
                raise OrchestratorError(
                    message="Cannot resume with an invalid execution contract",
                    details={"invalid": "route_compat"},
                )
            if restored_router is not None and restored_projection is None:
                raise OrchestratorError(
                    message="Cannot resume without an enabled route compatibility contract",
                    details={"invalid": "route_compat", "reason": "dormant"},
                )
            if restored_router is None and restored_projection is not None:
                raise OrchestratorError(
                    message="Cannot resume with an enabled route compatibility contract",
                    details={"invalid": "route_compat", "reason": "router_dormant"},
                )
            if (
                restored_projection is not None
                and not self._model_routing_override_explicit
                and not validate_route_compat_projection(
                    restored_projection,
                    self._route_economics,
                    model_router=authoritative_router,
                    runtime_backend=persisted_runtime_backend,
                    current_effort=self._reasoning_effort,
                )
            ):
                raise OrchestratorError(
                    message="Cannot resume with a changed route compatibility catalog",
                    details={
                        "runtime_backend": persisted_runtime_backend,
                        "hint": "Restore the original route catalog or start a new session.",
                    },
                )
        if not migrate_v2_contract and persisted_reasoning_effort != self._reasoning_effort:
            raise OrchestratorError(
                message="Cannot resume with a changed reasoning-effort contract",
                details={
                    "persisted_reasoning_effort": persisted_reasoning_effort,
                    "current_reasoning_effort": self._reasoning_effort,
                    "hint": "Restore the original reasoning effort or start a new session.",
                },
            )
        constructor_model_value = persisted_constructor_model.get("model")
        effective_model_observed = self._runtime_execution_proves_effective_model(
            persisted_runtime_execution
        )
        process_local_authority = (
            prepared_live_execution
            and valid_process_local_authority_contract(raw_contract.get("foundation_a_authority"))
        )
        model_override_support = getattr(
            getattr(self._adapter, "capabilities", None),
            "model_override_support",
            ParamSupport.IGNORED,
        )
        automatic_codex_default_authorized = (
            self._runtime_execution_authorizes_automatic_codex_resume(persisted_runtime_execution)
        )
        if (
            constructor_model_value is None
            and not effective_model_observed
            and not automatic_codex_default_authorized
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
                    "automatic_codex_default_authorized": automatic_codex_default_authorized,
                    "hint": ("Pin the original runtime model/profile, or start a new session."),
                },
            )
        self._execution_preferences = persisted_preferences
        self._shadow_replay_enabled = self._resolved_shadow_replay_enabled()
        current_execution_semantics = self._execution_semantics_contract()
        if (
            not migrate_legacy_contract
            and dict(raw_execution_semantics) != current_execution_semantics
        ):
            runtime_capability_drift = raw_execution_semantics.get(
                "runtime_effect_capabilities"
            ) != current_execution_semantics.get("runtime_effect_capabilities")
            raise OrchestratorError(
                message="Cannot resume with changed execution semantics",
                details={
                    "persisted_execution_semantics": dict(raw_execution_semantics),
                    "current_execution_semantics": current_execution_semantics,
                    "resume_blocked": (
                        "runtime_effect_capability_drift"
                        if runtime_capability_drift
                        else "execution_semantics_drift"
                    ),
                    "retryable": True,
                    "hint": (
                        "Restore the original verification, retry, decomposition, and "
                        "executor settings or start a new session."
                    ),
                },
            )
        if self._model_routing_override_explicit:
            replacement = self._build_execution_contract(
                seed=seed,
                seed_fingerprint=(persisted_seed_fingerprint if valid_seed_fingerprint else None),
                authority_generation=authority_generation,
                execution_inputs_contract=normalized_execution_inputs,
                project_identity=replacement_project_identity,
                runtime_handle=runtime_handle,
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
        self._requested_model_tier = persisted_requested_model_tier
        if migrate_legacy_contract:
            replacement = self._build_execution_contract(
                seed=seed,
                seed_fingerprint=(persisted_seed_fingerprint if valid_seed_fingerprint else None),
                authority_generation=authority_generation,
                project_identity=replacement_project_identity,
                runtime_handle=runtime_handle,
            )
            if authority_generation is None:
                replacement["foundation_a_authority"] = dict(raw_contract["foundation_a_authority"])
            self._execution_contract = replacement
            return True
        if migrate_preflight_contract:
            self._execution_contract = dict(raw_contract)
            return True
        # Preserve the exact persisted proof identity alongside the restored
        # router. Recomputing it from a resumed throwaway worktree would make the
        # same execution appear to be a different experiment.
        self._execution_contract = dict(raw_contract)
        if preferences_migrated or base_reasoning_effort_missing:
            self._execution_contract["execution_preferences"] = (
                persisted_preferences.to_contract_data()
            )
            migrated_routing = dict(raw_routing)
            if base_reasoning_effort_missing:
                migrated_routing["base_reasoning_effort"] = persisted_base_reasoning_effort
            self._execution_contract["model_routing"] = migrated_routing
            migrated_proof = dict(raw_proof)
            migrated_proof["routing_fingerprint"] = self._routing_fingerprint(migrated_routing)
            self._execution_contract["frugality_proof"] = migrated_proof
            return True
        return False

    def _restore_execution_contract_snapshot(
        self,
        progress: Mapping[str, Any],
        *,
        seed: Seed | None = None,
        authority_generation: _ProcessLocalAuthorityGeneration | None = None,
        require_bound_execution_inputs: bool = True,
        prepared_live_execution: bool = False,
        runtime_handle: RuntimeHandle | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Restore and return one immutable invocation-local contract snapshot.

        The legacy restore routine updates runner configuration as a side
        effect.  Serialize that mutation and copy the resulting contract
        inside the same worker-thread critical section so a concurrent resume
        can never copy another session's authority after the await boundary.
        """
        with self._execution_contract_restore_lock:
            changed = self._restore_execution_contract(
                progress,
                seed=seed,
                authority_generation=authority_generation,
                require_bound_execution_inputs=require_bound_execution_inputs,
                prepared_live_execution=prepared_live_execution,
                runtime_handle=runtime_handle,
            )
            if not isinstance(self._execution_contract, Mapping):
                raise OrchestratorError(
                    message="Cannot resume without a restored execution contract",
                    details={"invalid": "execution_contract"},
                )
            return changed, deepcopy(dict(self._execution_contract))

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
        try:
            resolved_backend = resolve_llm_backend(backend)
            cli_path = getattr(self._adapter, "cli_path", None)
            runtime_backend = self._adapter.runtime_backend
            runtime_capability = (
                get_backend_capability(runtime_backend)
                if isinstance(runtime_backend, str)
                else None
            )
            llm_capability = get_backend_capability(resolved_backend)
            resolved_cli_path = (
                cli_path
                if (
                    isinstance(cli_path, str)
                    and cli_path
                    and runtime_capability is not None
                    and runtime_capability.cli_name is not None
                    and llm_capability is not None
                    and runtime_capability.cli_name == llm_capability.cli_name
                )
                else None
            )
            # ``allowed_tools=[]`` paired with ``max_turns=1``: see issue #781.
            llm_adapter = create_llm_adapter(
                backend=backend,
                permission_mode=self._forced_permission_mode,
                cli_path=resolved_cli_path,
                cwd=self._effective_cwd(),
                max_turns=1,
                allowed_tools=([] if backend_supports_tool_envelope(resolved_backend) else None),
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
            if (
                "runtime_event_type" not in progress
                and isinstance(runtime_event_type, str)
                and runtime_event_type
            ):
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
        if not projected.is_tool_call:
            return None
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
        """Return the shared bounded closed-vocabulary metadata projection."""
        candidates, _overflowed = project_failure_metadata(message)
        return candidates

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
            if not math.isfinite(value):
                return None
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
            if not math.isfinite(total_seconds):
                return None
        if total_seconds <= 0:
            return None
        return max(1, math.ceil(total_seconds))

    @classmethod
    def _duration_value_to_seconds(cls, value: object) -> int | None:
        """Parse a numeric or textual retry duration into seconds."""
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, int):
            if value <= 0:
                return None
            return value
        if isinstance(value, float):
            if not math.isfinite(value) or value <= 0:
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
            if not math.isfinite(numeric) or numeric <= 0:
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
        """Extract retry metadata through the provider-neutral shared parser."""

        return retry_duration_seconds_from_metadata(metadata, now=now)

    @classmethod
    def _duration_from_message(cls, message: AgentMessage, *, now: datetime) -> int | None:
        """Extract a retry duration through the same parser used for classification."""

        return retry_duration_seconds_from_message(message, now=now)

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
            kind = metadata.get("kind")
            if isinstance(kind, str) and kind.strip().lower() in _USAGE_LIMIT_RECOVERY_KINDS:
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
        default_pause_seconds: int | None = None,
    ) -> RecoverableFailurePause | None:
        """Return a pause decision for provider usage/quota window failures."""
        if not is_usage_limit_pause_message(message, now=now):
            return None

        if (
            type(default_pause_seconds) is not int
            or not 1 <= default_pause_seconds <= MAX_USAGE_LIMIT_PAUSE_SECONDS
        ):
            raise ConfigError(
                "Durable usage-limit pause policy is missing or outside its range",
                config_key="orchestrator.usage_limit_pause_hours",
                details={
                    "pause_seconds": default_pause_seconds,
                    "max_seconds": MAX_USAGE_LIMIT_PAUSE_SECONDS,
                },
            )

        pause_seconds = self._duration_from_message(message, now=now) or default_pause_seconds
        pause_seconds = max(1, pause_seconds)
        try:
            resume_after = now + timedelta(seconds=pause_seconds)
        except OverflowError:
            # Retry metadata is provider-controlled. An otherwise recognizable
            # quota boundary must remain PAUSED even when its numeric hint is
            # outside Python's datetime envelope; fall back to the validated
            # operator-configured window instead of turning pause construction
            # into exception cleanup.
            pause_seconds = default_pause_seconds
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

    def _bounded_route_runtime_active(self) -> bool:
        """Return whether this runner can authorize a Routing D provider effect."""
        return bool(
            has_durable_decomposition_replay(self._max_decomposition_depth)
            and self._model_router is not None
            and self._route_economics is not None
            and getattr(
                getattr(self._adapter, "capabilities", None),
                "model_override_support",
                ParamSupport.IGNORED,
            )
            is ParamSupport.NATIVE
        )

    @classmethod
    def _resume_retry_pause(cls, message: AgentMessage) -> RecoverableFailurePause | None:
        """Return a pause decision for recoverable resume-bootstrap failures."""
        for metadata in cls._metadata_candidates(message):
            kind = metadata.get("kind")
            if isinstance(kind, str) and kind.strip().lower() == _RESUME_RETRY_RECOVERY_KIND:
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
        default_pause_seconds: int | None = None,
    ) -> RecoverableFailurePause | None:
        """Return pause metadata when a final runtime error should stay resumable."""
        if not (message.is_final and message.is_error):
            return None

        resume_retry = self._resume_retry_pause(message)
        if resume_retry is not None:
            return resume_retry

        return self._usage_limit_pause(
            message,
            now=now or datetime.now(UTC),
            default_pause_seconds=default_pause_seconds,
        )

    def _recoverable_failure_pause_from_parallel_result(
        self,
        parallel_result: Any,
        *,
        now: datetime | None = None,
        require_all_failures_recoverable: bool = True,
        default_pause_seconds: int | None = None,
    ) -> RecoverableFailurePause | None:
        """Resolve a parallel pause under the caller's explicit ownership rule."""

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
                if require_all_failures_recoverable:
                    return None
                continue
            if not bool(getattr(ac_result, "is_failure", False)):
                if require_all_failures_recoverable or bool(getattr(ac_result, "success", False)):
                    continue

            found_failure = True
            messages = getattr(ac_result, "messages", ())
            if not isinstance(messages, tuple):
                if require_all_failures_recoverable:
                    return None
                continue

            failure_pause = None
            for message in reversed(messages):
                pause = self._recoverable_failure_pause(
                    message,
                    now=resolved_now,
                    default_pause_seconds=default_pause_seconds,
                )
                if pause is not None:
                    failure_pause = pause
                    break

            if failure_pause is None:
                if require_all_failures_recoverable:
                    return None
                continue

            selected_pause = (
                failure_pause
                if selected_pause is None
                else latest_pause(selected_pause, failure_pause)
            )

        raw_coordinator_pause = getattr(parallel_result, "recoverable_coordinator_pause", None)
        if raw_coordinator_pause is not None:
            if not isinstance(raw_coordinator_pause, CoordinatorQuotaPause):
                return None
            consequence = raw_coordinator_pause.consequence
            coordinator_pause = RecoverableFailurePause(
                pause_kind=consequence.pause_kind,
                reason=consequence.reason,
                resume_hint=consequence.resume_hint,
                pause_seconds=consequence.pause_seconds,
                resume_after=consequence.resume_after,
                coordinator_owner=raw_coordinator_pause,
            )
            found_failure = True
            selected_pause = (
                coordinator_pause
                if selected_pause is None
                else latest_pause(selected_pause, coordinator_pause)
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
            await self._cleanup_terminal_process_local_state(
                session_id=session_id,
                execution_id=execution_id,
            )
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
        raw_root_indices = tracker.progress.get(ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY)
        expected_root_indices = (
            tuple(raw_root_indices) if isinstance(raw_root_indices, (list, tuple)) else None
        )
        try:
            acceptance_finalizations = await collect_cancellation_acceptance_plan(
                session_id=session_id,
                execution_id=execution_id,
                event_store=self._event_store,
                expected_root_indices=expected_root_indices,
            )
        except Exception as exc:
            return self._cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
                cancellation_reason=reason,
                cancelled_by=cancelled_by,
            )
        cancel_result = await self._session_repo.mark_cancelled(
            session_id=session_id,
            reason=reason,
            cancelled_by=cancelled_by,
            acceptance_finalizations=acceptance_finalizations,
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

    def _assemble_strategy_base_catalog(
        self,
        strategy: ExecutionStrategy | None,
    ) -> tuple[list[str], set[str], SessionToolCatalog]:
        """Build the deterministic pre-MCP catalog used by prepare and execute."""
        base_tools = strategy.get_tools() if strategy else list(DEFAULT_TOOLS)
        inherited_mcp: set[str] = set()
        if self._inherited_tools:
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
        return base_tools, inherited_mcp, session_catalog

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
        base_tools, inherited_mcp, session_catalog = self._assemble_strategy_base_catalog(strategy)

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

    async def _handle_requested_cancellation(
        self,
        *,
        session_id: str,
        execution_id: str,
        messages_processed: int,
        start_time: datetime,
        expected_root_indices: Iterable[int] | None = None,
    ) -> Result[OrchestratorResult, OrchestratorError] | None:
        """Apply the terminal cancellation transition at an effect choke point."""

        if not await self._check_cancellation(session_id):
            return None
        return await self._handle_cancellation(
            session_id=session_id,
            execution_id=execution_id,
            messages_processed=messages_processed,
            start_time=start_time,
            expected_root_indices=expected_root_indices,
        )

    def _cancellation_persistence_pending_result(
        self,
        *,
        session_id: str,
        execution_id: str,
        cause: object,
        acceptance_finalizations: list[dict[str, Any]] | None = None,
        cancellation_reason: str | None = None,
        cancelled_by: str = "runner",
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
        if acceptance_finalizations is not None or cancellation_reason is not None:
            self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
                execution_id=execution_id,
                status=SessionStatus.CANCELLED,
                error_message=cancellation_reason or "Execution cancelled",
                cancelled_by=cancelled_by,
                acceptance_finalizations=acceptance_finalizations,
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
        expected_root_indices: Iterable[int] | None = None,
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
                expected_root_indices=expected_root_indices,
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
        expected_root_indices: Iterable[int] | None = None,
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
                    await self._cleanup_terminal_process_local_state(
                        session_id=session_id,
                        execution_id=execution_id,
                    )

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

        # Compute the complete observed-root decision set before the terminal
        # CAS.  The terminal session event carries this exact plan so a
        # cancellation cannot commit a durable winner and then lose its final
        # acceptance records to interruption.
        try:
            cancellation_acceptance_finalizations = await collect_cancellation_acceptance_plan(
                session_id=session_id,
                execution_id=execution_id,
                event_store=self._event_store,
                expected_root_indices=expected_root_indices,
            )
        except Exception as exc:
            return self._cancellation_persistence_pending_result(
                session_id=session_id,
                execution_id=execution_id,
                cause=exc,
                cancellation_reason=cancellation_reason,
                cancelled_by=cancelled_by,
            )
        try:
            cancel_result = await self._session_repo.mark_cancelled(
                session_id,
                reason=cancellation_reason,
                cancelled_by=cancelled_by,
                acceptance_finalizations=cancellation_acceptance_finalizations,
            )
        except asyncio.CancelledError:
            if (
                await self._reconcile_durable_terminal_and_cleanup(
                    session_id=session_id,
                    execution_id=execution_id,
                )
                is None
            ):
                self._pending_lifecycle_intents[session_id] = _PendingLifecycleIntent(
                    execution_id=execution_id,
                    status=SessionStatus.CANCELLED,
                    error_message=cancellation_reason,
                    cancelled_by=cancelled_by,
                    acceptance_finalizations=cancellation_acceptance_finalizations,
                )
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
                    expected_root_indices=expected_root_indices,
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
                acceptance_finalizations=cancellation_acceptance_finalizations,
                cancellation_reason=cancellation_reason,
                cancelled_by=cancelled_by,
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
                acceptance_finalizations=cancellation_acceptance_finalizations,
                cancellation_reason=cancellation_reason,
                cancelled_by=cancelled_by,
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
                    expected_root_indices=expected_root_indices,
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
                await self._cleanup_terminal_process_local_state(
                    session_id=session_id,
                    execution_id=execution_id,
                )

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

    async def _close_adapter(self) -> None:
        adapter_aclose = getattr(self._adapter, "aclose", None)
        if inspect.iscoroutinefunction(adapter_aclose):
            await adapter_aclose()

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

    def _apply_verify_command_gate(
        self, seed: Seed
    ) -> Result[SessionTracker, OrchestratorError] | None:
        """Surface — or refuse — criteria nothing can deterministically judge.

        Returns ``None`` when preparation may continue, which is every case in
        the ``warn`` stage. Only the ``block`` stage produces an error.
        """
        from ouroboros.core.seed_verify_gate import (
            render_verify_command_gate_warning,
            unverifiable_criteria,
            verify_command_gate_mode,
        )

        findings = unverifiable_criteria(seed)
        if not findings:
            return None

        mode = verify_command_gate_mode()
        indices = [finding.display_index for finding in findings]
        if mode == "block":
            return Result.err(
                OrchestratorError(
                    message=("Acceptance criteria carry no verify_command and no exemption reason"),
                    details={
                        "gate": "seed.verify_command_gate",
                        "mode": mode,
                        "unverifiable_ac_indices": indices,
                        "guidance": render_verify_command_gate_warning(findings),
                    },
                )
            )
        log.warning(
            "orchestrator.seed.verify_command_gate_warning",
            mode=mode,
            unverifiable_ac_indices=indices,
            unverifiable_ac_count=len(findings),
        )
        # Text, not markup interpolation: descriptions and commands are seed
        # text and may contain Rich tags (`[/yellow]` would raise MarkupError).
        self._console.print(Text(render_verify_command_gate_warning(findings), style="yellow"))
        return None

    async def prepare_session(
        self,
        seed: Seed,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> Result[SessionTracker, OrchestratorError]:
        """Create and persist the orchestration session before execution begins.

        This allows callers such as MCP handlers to return stable tracking IDs
        immediately and then start the actual runtime work asynchronously.

        Contract construction captures the resolver's repo-local input
        closure, and the publication boundary revalidates from that evidence
        by metadata alone when nothing observable changed — escalating to the
        full re-resolution (probe included) otherwise (#1796 L2).
        """
        with publication_evidence_sink():
            return await self._prepare_session_scoped(
                seed, execution_id=execution_id, session_id=session_id
            )

    async def _prepare_session_scoped(
        self,
        seed: Seed,
        execution_id: str | None = None,
        session_id: str | None = None,
    ) -> Result[SessionTracker, OrchestratorError]:
        # The verify-command gate runs here, at new-session preparation, so
        # sessions already in flight are never re-judged under a gate that was
        # tightened after they started.
        gate_result = self._apply_verify_command_gate(seed)
        if gate_result is not None:
            return gate_result
        exec_id = execution_id or f"exec_{uuid4().hex[:12]}"
        resolved_session_id = session_id or f"orch_{uuid4().hex[:12]}"
        self._execution_guidance = None
        # A generation belongs to this one preparation call.  Do not store it
        # in a runner-wide mutable slot: concurrent preparations must not share
        # a capability or correlation id.
        authority_generation = self._begin_process_local_authority_generation()
        if self._task_workspace is not None:
            # Reserve the runner-wide lock before the first await.  Otherwise a
            # different session can finish while contract construction is in a
            # worker thread and release the lock before this preparation has a
            # registered identity.
            self._reserve_task_workspace(authority_generation)
            self._task_workspace_lock_held = True

        def abort_process_local_preparation() -> None:
            """Dispose every authority state reachable from an aborted prepare.

            The registration may have completed before a later setup step
            raises, while an earlier failure has only an unregistered issuance.
            Both cleanup operations are idempotent and together leave no
            capability or lease behind.
            """
            identity = (resolved_session_id, exec_id)
            owns_registered_generation = (
                self._process_local_authorities.get(identity) is authority_generation
            )
            if owns_registered_generation:
                self._retire_process_local_authority(
                    session_id=resolved_session_id,
                    execution_id=exec_id,
                )
            self._discard_process_local_authority(authority_generation)
            self._task_workspace_reservations.discard(authority_generation)
            # A duplicate exact identity can fail registration while an older
            # generation still owns the same runner slot.  That failed
            # preparation acquired no authority or workspace ownership of its
            # own, so it must not retire or release the existing generation.
            if owns_registered_generation or identity not in self._task_workspace_users:
                self._release_task_workspace_for_identity(
                    session_id=resolved_session_id,
                    execution_id=exec_id,
                )

        try:
            execution_contract, project_identity = await asyncio.to_thread(
                self._build_new_session_contract,
                seed=seed,
                authority_generation=authority_generation,
                runtime_handle=self._inherited_runtime_handle,
            )
            self._execution_guidance_delivery_mode()
            create_session_kwargs: dict[str, Any] = {
                "execution_id": exec_id,
                "seed_id": seed.metadata.seed_id,
                "session_id": resolved_session_id,
                "seed_goal": seed.goal,
                "runtime_backend": getattr(self._adapter, "runtime_backend", None),
                "llm_backend": getattr(self._adapter, "llm_backend", None),
                "execution_contract": execution_contract,
                "project_identity": project_identity,
                "project_workspace": self._effective_cwd(),
            }
            if self._task_workspace is not None:
                create_session_kwargs["project_task_workspace"] = self._task_workspace
            try:
                if (
                    "acceptance_root_indices"
                    in inspect.signature(self._session_repo.create_session).parameters
                ):
                    create_session_kwargs["acceptance_root_indices"] = range(
                        len(seed.acceptance_criteria)
                    )
            except (TypeError, ValueError):
                # Legacy/mock repositories may not expose an inspectable signature;
                # the durable SessionRepository path always does.
                pass
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
        self._task_workspace_reservations.discard(authority_generation)
        self._execution_contract = execution_contract
        try:
            session_result = await self._session_repo.create_session(**create_session_kwargs)
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
                await self._cleanup_terminal_process_local_state(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
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
                await self._cleanup_terminal_process_local_state(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )

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

        try:
            self._seal_process_local_prepared_contract(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                generation=authority_generation,
                execution_contract=execution_contract,
            )
        except ValueError as exc:
            seal_details: dict[str, Any] = {
                "session_id": tracker.session_id,
                "execution_id": tracker.execution_id,
                "cause": str(exc),
                "resume_blocked": "prepared_execution_contract_unsealed",
            }
            terminal_mark_error = await self._mark_preparation_failed_best_effort(
                tracker=tracker,
                message="Failed to seal persisted initial session contract",
                details=seal_details,
            )
            if terminal_mark_error is None:
                await self._cleanup_terminal_process_local_state(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
            else:
                seal_details["terminal_mark_error"] = terminal_mark_error
                seal_details["terminal_persistence_pending"] = True
            return Result.err(
                OrchestratorError(
                    message="Failed to seal persisted initial session contract",
                    details=seal_details,
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

        durable_before_claim = await self._reconstruct_precreated_durable_tracker(tracker)
        if durable_before_claim.is_err:
            return Result.err(durable_before_claim.error)
        durable_tracker = durable_before_claim.value
        durable_status_error = self._precreated_non_running_error(durable_tracker)
        if durable_status_error is not None:
            if durable_tracker.status in {
                SessionStatus.COMPLETED,
                SessionStatus.CANCELLED,
                SessionStatus.FAILED,
            }:
                await self._cleanup_terminal_process_local_state(
                    session_id=durable_tracker.session_id,
                    execution_id=durable_tracker.execution_id,
                )
            return Result.err(durable_status_error)

        # A retained lifecycle transition outranks a still-RUNNING durable
        # snapshot. Persistence-pending means the previous provider effect
        # already happened and only its PAUSED/terminal publication remains;
        # entering normal prepared authentication would repeat that effect.
        # Use the authenticated durable tracker at the same replay choke point
        # as ``resume_session`` before any normal prepared claim or dispatch.
        pending_lifecycle = await self._retry_pending_lifecycle_intent(durable_tracker)
        if pending_lifecycle is not None:
            return pending_lifecycle

        # Preserve the historical terminal-copy recovery contract, but only
        # after durable identity and RUNNING status have been authenticated.
        # Nonterminal caller copies must still match the sealed prepared receipt.
        if tracker.status in {
            SessionStatus.COMPLETED,
            SessionStatus.CANCELLED,
            SessionStatus.FAILED,
        }:
            tracker = durable_tracker
            exec_id = tracker.execution_id

        raw_contract = tracker.progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)
        if not isinstance(raw_contract, Mapping):
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

        # This API may execute only the tracker returned by ``prepare_session``.
        # Claim its live capability first, then authenticate the caller-owned
        # contract against the snapshot sealed only after durable publication.
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

        # Close the observation-to-claim race. If another execution published
        # PAUSED or a terminal state after the first durable read, this claimed
        # generation prevents further legitimate dispatch while the second read
        # rejects the stale prepared receipt before any tool or provider effect.
        try:
            durable_after_claim = await self._reconstruct_precreated_durable_tracker(tracker)
        except asyncio.CancelledError:
            self._preserve_process_local_owner_for_retry(
                execution_id=tracker.execution_id,
                session_id=tracker.session_id,
            )
            raise
        if durable_after_claim.is_err:
            self._preserve_process_local_owner_for_retry(
                execution_id=tracker.execution_id,
                session_id=tracker.session_id,
            )
            return Result.err(durable_after_claim.error)
        durable_tracker = durable_after_claim.value
        durable_status_error = self._precreated_non_running_error(durable_tracker)
        if durable_status_error is not None:
            if durable_tracker.status in {
                SessionStatus.COMPLETED,
                SessionStatus.CANCELLED,
                SessionStatus.FAILED,
            }:
                await self._cleanup_terminal_process_local_state(
                    session_id=durable_tracker.session_id,
                    execution_id=durable_tracker.execution_id,
                )
            else:
                self._preserve_process_local_owner_for_retry(
                    execution_id=tracker.execution_id,
                    session_id=tracker.session_id,
                )
            return Result.err(durable_status_error)

        try:
            caller_contract = self._authenticate_process_local_prepared_contract(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                generation=authority_generation,
                execution_contract=raw_contract,
            )
            durable_progress = deepcopy(dict(durable_tracker.progress))
            durable_contract = self._authenticate_process_local_prepared_contract(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                generation=authority_generation,
                execution_contract=durable_progress.get(EXECUTION_CONTRACT_PROGRESS_KEY),
            )
            if caller_contract is None or durable_contract is None:
                raise OrchestratorError(
                    message=(
                        "Caller-supplied execution contract does not match the "
                        "persisted prepared contract"
                    ),
                    details={
                        "session_id": tracker.session_id,
                        "execution_id": tracker.execution_id,
                        "resume_blocked": "prepared_execution_contract_mismatch",
                    },
                )
            durable_progress[EXECUTION_CONTRACT_PROGRESS_KEY] = durable_contract
            contract_changed, validated_contract = await asyncio.to_thread(
                self._restore_execution_contract_snapshot,
                durable_progress,
                seed=seed,
                authority_generation=authority_generation,
                require_bound_execution_inputs=False,
                prepared_live_execution=True,
            )
            if contract_changed:
                raise OrchestratorError(
                    message="Prepared execution contract changed during authentication",
                    details={"resume_blocked": "prepared_execution_contract_changed"},
                )
            self._execution_guidance_delivery_mode()
        except asyncio.CancelledError:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    messages_processed=0,
                    start_time=start_time,
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            if exc.details.get("resume_blocked") == "project_identity_unavailable":
                self._preserve_process_local_owner_for_retry(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )
                return Result.err(exc)
            _, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
                error=exc,
                seed=seed,
                execution_contract=(
                    dict(raw_contract) if isinstance(raw_contract, Mapping) else None
                ),
            )
            if persistence_pending is not None:
                return persistence_pending
            return Result.err(exc)

        # Keep the immutable per-session contract local to this invocation.
        # ``self._execution_contract`` is retained for legacy helpers, but it
        # must never be the source of acceptance authority under concurrency.
        execution_contract = dict(validated_contract)
        self._execution_contract = execution_contract

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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
                )

            # Build prompts from the strategy frozen during preparation. The
            # fat-harness profile and legacy registry are effect-bearing inputs;
            # neither may be reread after the durable session is published.
            strategy = self._execution_strategy_snapshot(
                execution_contract,
                require_bound=False,
            )
            # Get merged tools (strategy tools + MCP tools if configured) and
            # authenticate the fully bound prompt/tool input contract before
            # building prompts or entering any provider path.
            merged_tools, mcp_provider, tool_catalog = await self._get_merged_tools(
                session_id=tracker.session_id,
                tool_prefix=self._mcp_tool_prefix,
                strategy=strategy,
            )
            execution_contract, inputs_changed = self._bind_execution_tool_authority(
                execution_contract,
                merged_tools=merged_tools,
                tool_catalog=tool_catalog,
            )
            if inputs_changed:
                bound_progress = {
                    EXECUTION_CONTRACT_PROGRESS_KEY: execution_contract,
                    "messages_processed": tracker.messages_processed,
                }
                persisted_inputs = await self._session_repo.track_progress(
                    tracker.session_id,
                    bound_progress,
                )
                if persisted_inputs.is_err:
                    raise OrchestratorError(
                        message="Failed to persist resolved prompt/tool authority",
                        details={
                            "session_id": tracker.session_id,
                            "cause": str(persisted_inputs.error),
                        },
                    )
                tracker = tracker.with_progress(bound_progress)
                self._execution_contract = execution_contract
            await asyncio.to_thread(
                self._restore_execution_contract_snapshot,
                {EXECUTION_CONTRACT_PROGRESS_KEY: execution_contract},
                seed=seed,
                authority_generation=authority_generation,
                prepared_live_execution=True,
            )
            execution_semantics = self._execution_semantics_snapshot(execution_contract)
            system_prompt = build_system_prompt(
                seed,
                strategy=strategy,
                repo_root=self._effective_cwd(),
                guidance_fragment=self._ensure_new_run_guidance().rendered_fragment,
                context_pack_enabled=execution_semantics["context_pack_enabled"],
                resolved_context_pack_fragment=(
                    self._execution_context_pack_fragment_snapshot(
                        execution_contract,
                        require_bound=True,
                    )
                ),
            )
            await self._record_execution_guidance_injection(
                session_id=tracker.session_id,
                execution_id=exec_id,
                injection_key="start",
            )
            task_prompt = build_task_prompt(seed, strategy=strategy)
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
                    "execution_contract": execution_contract,
                }
                if externally_satisfied_acs:
                    parallel_kwargs["externally_satisfied_acs"] = externally_satisfied_acs
                if force_sequential_levels or (
                    not parallel and (self._fat_harness_mode or has_investment_metadata)
                ):
                    parallel_kwargs["force_sequential_levels"] = True

                try:
                    return await self._execute_parallel(**parallel_kwargs)
                finally:
                    await self._close_adapter()

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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                seed=seed,
                execution_contract=execution_contract,
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
            runtime_handle_transferred_to_pause = False
            recovery_interventions_used = 0
            recovery_personas: list[str] = []
            recoverable_failure_pause: RecoverableFailurePause | None = None
            last_direct_final_message: AgentMessage | None = None
            direct_route_candidate: Any | None = None
            direct_bounded_routing = self._bounded_route_runtime_active()
            direct_terminal_blocked = False

            cancelled_result: Result[OrchestratorResult, OrchestratorError] | None = None

            async def _consume_task_stream(
                *,
                prompt: str,
                resume_handle: RuntimeHandle | None,
                status: Any,
                expected_route_candidate: Any | None = None,
            ) -> RuntimeHandle | None:
                nonlocal cancelled_result
                nonlocal final_message
                nonlocal last_completed_count
                nonlocal last_tool
                nonlocal messages_processed
                nonlocal recoverable_failure_pause
                nonlocal success
                nonlocal tracker
                nonlocal direct_route_candidate
                nonlocal last_direct_final_message
                nonlocal direct_terminal_blocked

                active_runtime_handle = resume_handle
                self._announce_param_degradations(
                    system_prompt=system_prompt,
                    tools=merged_tools,
                )
                selected_routes: list[Any] = []
                route_id_override = (
                    expected_route_candidate.route_id
                    if expected_route_candidate is not None
                    else None
                )
                effort_kwargs = await self._route_call_effort(
                    execution_id=exec_id,
                    session_id=tracker.session_id,
                    bounded_escalation=direct_bounded_routing,
                    route_id_override=route_id_override,
                    expected_route_candidate=expected_route_candidate,
                    expected_runtime_effect_capabilities=execution_semantics[
                        "runtime_effect_capabilities"
                    ],
                    selected_route_sink=selected_routes,
                )
                direct_route_candidate = selected_routes[0] if selected_routes else None
                if direct_bounded_routing:
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=tracker.session_id,
                        execution_id=exec_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )
                    if cancelled_result is not None:
                        return active_runtime_handle
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
                            cancelled_result = await self._handle_requested_cancellation(
                                session_id=tracker.session_id,
                                execution_id=exec_id,
                                messages_processed=messages_processed,
                                start_time=start_time,
                                expected_root_indices=range(len(seed.acceptance_criteria)),
                            )
                            if cancelled_result is not None:
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

                        # NOTE: periodic drift measurement used to be emitted here
                        # every PROGRESS_EMIT_INTERVAL messages, but the two inputs
                        # it needs are not tracked anywhere in this loop. Passing
                        # empty lists pinned constraint_drift to 0.0 (dropping 30%
                        # of the weighted score) and ontology_drift to 1.0 (a fixed
                        # +0.2 penalty), so combined_drift was always
                        # goal_drift * 0.5 + 0.2 and is_acceptable (<= 0.3) was
                        # effectively always False. Emitting nothing is preferable
                        # to persisting a measurement we know is wrong; re-enable
                        # only once constraint violations and ontology concepts are
                        # actually tracked for the message being measured.

                        # Handle final message
                        if message.is_final:
                            last_direct_final_message = message
                            final_message = message.content
                            success = not message.is_error
                            recoverable_failure_pause = self._recoverable_failure_pause(
                                message,
                                now=datetime.now(UTC),
                                default_pause_seconds=execution_semantics[
                                    "usage_limit_pause_seconds"
                                ],
                            )

                if direct_bounded_routing and cancelled_result is None:
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=tracker.session_id,
                        execution_id=exec_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )

                if (
                    recoverable_failure_pause is not None
                    and direct_bounded_routing
                    and not await self._persist_exact_direct_pause_runtime_handle(
                        session_id=tracker.session_id,
                        runtime_handle=active_runtime_handle,
                        messages_processed=messages_processed,
                    )
                ):
                    # A quota signal without provider continuity cannot authorize
                    # PAUSED: retrying it would be a second fresh provider effect.
                    recoverable_failure_pause = None
                    direct_terminal_blocked = True
                    success = False
                    final_message = (
                        f"{final_message}\nRecoverable provider pause rejected: no exact "
                        "resumable handle is available; human handoff required."
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
                    self._execution_inherited_runtime_handle_snapshot(
                        execution_contract,
                        require_bound=True,
                    ),
                    tool_catalog=tool_catalog,
                )
                direct_route_history: tuple[str, ...] = ()
                direct_route_override: Any | None = None
                direct_prompt = task_prompt
                while True:
                    runtime_handle = await _consume_task_stream(
                        prompt=direct_prompt,
                        resume_handle=runtime_handle,
                        status=status,
                        expected_route_candidate=direct_route_override,
                    )
                    if cancelled_result is not None:
                        # Cancellation owns the terminal transition.  It is not a
                        # classified route failure and must never authorize a
                        # successor provider effect or route observation.
                        break
                    if recoverable_failure_pause is not None:
                        # Usage/quota pauses retain the current provider session
                        # for resume.  Persisting a terminal BLOCKED observation
                        # would seal the very continuation advertised by PAUSED.
                        if direct_bounded_routing and direct_route_candidate is not None:
                            from ouroboros.events.base import BaseEvent

                            pause_episode = (
                                "route:" + hashlib.sha256(f"{exec_id}\0direct".encode()).hexdigest()
                            )
                            await self._event_store.append(
                                BaseEvent(
                                    type="execution.ac.route_paused",
                                    aggregate_type="execution",
                                    aggregate_id=exec_id,
                                    data={
                                        "schema_version": 1,
                                        "execution_id": exec_id,
                                        "session_id": tracker.session_id,
                                        "root_ac_index": None,
                                        "call_site": "runner",
                                        "episode_id": pause_episode,
                                        "attempt_index": len(direct_route_history),
                                        "prior_route_ids": list(direct_route_history),
                                        "route": direct_route_candidate.to_contract_data(),
                                        "recoverable_pause": True,
                                        "final_acceptance_declared": False,
                                    },
                                )
                            )
                        break
                    if not direct_bounded_routing or direct_route_candidate is None:
                        break

                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=tracker.session_id,
                        execution_id=exec_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )
                    if cancelled_result is not None:
                        break
                    episode_digest = hashlib.sha256(f"{exec_id}\0direct".encode()).hexdigest()
                    decision, direct_route_history = await self._persist_direct_route_outcome(
                        execution_id=exec_id,
                        session_id=tracker.session_id,
                        episode_id=f"route:{episode_digest}",
                        prior_route_ids=direct_route_history,
                        candidate=direct_route_candidate,
                        success=success,
                        failure_class=self._classify_direct_route_failure(last_direct_final_message)
                        if not direct_terminal_blocked
                        else FailureClass.BLOCKED,
                    )
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=tracker.session_id,
                        execution_id=exec_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )
                    if cancelled_result is not None:
                        break
                    if success or decision is None or decision.blocked:
                        if decision is not None and decision.blocked:
                            direct_terminal_blocked = True
                            final_message = (
                                f"{final_message}\nRoute escalation stopped: "
                                f"{decision.reason.value}; human handoff required."
                            )
                        break
                    assert decision.selected is not None
                    direct_route_override = decision.selected
                    direct_prompt = (
                        task_prompt
                        + "\n\nThe prior implementation route failed. Continue in a fresh "
                        "session and satisfy the same Seed contracts."
                    )
                    # A route change never resumes the previous provider session.
                    await self._terminate_runtime_handle(
                        runtime_handle,
                        session_id=tracker.session_id,
                        context="bounded_route_escalation",
                    )
                    runtime_handle = None
                    success = False
                    recoverable_failure_pause = None

                # Same-session recovery is limited to the sequential runner.
                # Parallel execution owns per-AC retry semantics, and resume_session
                # is already a recovery workflow.
                if (
                    cancelled_result is None
                    and not success
                    and recoverable_failure_pause is None
                    and runtime_handle is not None
                    and not direct_bounded_routing
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
            acceptance_finalizations: list[dict[str, Any]] | None = None
            if success:
                completion_summary = {
                    "final_message": final_message[:500],
                    "messages_processed": messages_processed,
                    **self._task_summary(),
                }
                acceptance_finalizations = self._build_terminal_acceptance_finalizations(
                    seed=seed,
                    parallel_result=None,
                    execution_id=exec_id,
                    session_id=tracker.session_id,
                    terminal_status=SessionStatus.COMPLETED.value,
                    accepted_root_indices=set(range(len(seed.acceptance_criteria))),
                    default_outcome="succeeded",
                    execution_contract=execution_contract,
                )
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    requested_status=SessionStatus.COMPLETED,
                    summary=completion_summary,
                    messages_processed=messages_processed,
                    acceptance_finalizations=acceptance_finalizations,
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
                    # The lifecycle helper explicitly retained process-local
                    # pause ownership. Terminating here would destroy the exact
                    # provider boundary that its retry must publish.
                    runtime_handle_transferred_to_pause = True
                    return pause_pending
                assert pause_status is not None
                if pause_status is SessionStatus.PAUSED:
                    runtime_handle_transferred_to_pause = True
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
                acceptance_finalizations = self._build_terminal_acceptance_finalizations(
                    seed=seed,
                    parallel_result=None,
                    execution_id=exec_id,
                    session_id=tracker.session_id,
                    terminal_status=SessionStatus.FAILED.value,
                    default_outcome="blocked" if direct_terminal_blocked else "failed",
                    execution_contract=execution_contract,
                )
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=tracker.session_id,
                    execution_id=exec_id,
                    requested_status=SessionStatus.FAILED,
                    error_message=final_message,
                    messages_processed=messages_processed,
                    acceptance_finalizations=acceptance_finalizations,
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

            if terminal_status != "paused":
                await self._cleanup_terminal_process_local_state(
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
                    release_liveness_lease=False,
                )
                self._release_task_workspace_for_identity(
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                )

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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                seed=seed,
                execution_contract=execution_contract,
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
            if not runtime_handle_transferred_to_pause:
                await self._terminate_runtime_handle(
                    runtime_handle,
                    session_id=tracker.session_id,
                    context="execute",
                )
                await self._close_adapter()

    async def _execute_parallel(
        self,
        seed: Seed,
        exec_id: str,
        tracker: Any,
        merged_tools: list[str],
        tool_catalog: SessionToolCatalog,
        system_prompt: str,
        start_time: datetime,
        execution_contract: Mapping[str, Any] | None = None,
        externally_satisfied_acs: dict[int, dict[str, Any]] | None = None,
        force_sequential_levels: bool = False,
        resume_execution_plan: Any | None = None,
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
            ParallelExecutionCancelled,
            render_parallel_completion_message,
            render_parallel_verification_report,
        )

        log.info(
            "orchestrator.runner.parallel_mode_enabled",
            execution_id=exec_id,
            session_id=tracker.session_id,
            ac_count=len(seed.acceptance_criteria),
        )

        # Consume one immutable scalar snapshot for the whole invocation. The
        # public new/resume paths pass the exact durable contract; low-level
        # callers without one retain their current constructor semantics.
        contract_source = execution_contract
        if contract_source is None and isinstance(tracker.progress, Mapping):
            tracker_contract = tracker.progress.get(EXECUTION_CONTRACT_PROGRESS_KEY)
            contract_source = tracker_contract if isinstance(tracker_contract, Mapping) else None
        if contract_source is None:
            execution_semantics = self._execution_semantics_contract()
        else:
            execution_semantics = self._execution_semantics_snapshot(contract_source)
        current_execution_semantics = self._execution_semantics_contract()
        if execution_semantics != current_execution_semantics:
            raise OrchestratorError(
                message="Cannot execute after execution semantics drifted",
                details={
                    "persisted_execution_semantics": execution_semantics,
                    "current_execution_semantics": current_execution_semantics,
                },
            )
        max_decomposition_depth = execution_semantics["max_decomposition_depth"]
        max_parallel_workers = execution_semantics["max_parallel_workers"]
        effective_workers = execution_semantics["effective_parallel_workers"]
        resolved_backend_limits = BackendConcurrencyLimits(
            backend=execution_semantics["backend_limits_backend"],
            max_concurrency=execution_semantics["backend_max_concurrency"],
            requests_per_minute=execution_semantics["backend_requests_per_minute"],
            tokens_per_minute=execution_semantics["backend_tokens_per_minute"],
        )
        parallel_bounded_routing = bool(
            has_durable_decomposition_replay(max_decomposition_depth)
            and self._model_router is not None
            and self._route_economics is not None
            and getattr(
                getattr(self._adapter, "capabilities", None),
                "model_override_support",
                ParamSupport.IGNORED,
            )
            is ParamSupport.NATIVE
        )

        # Capture Routing D effect capability once at the dispatch choke point.
        # A durable parallel owner is itself sufficient replay evidence: it is
        # persisted before the first route event, so absence of those events
        # cannot authorize a fallthrough to the legacy executor after a crash.
        persisted_parallel_owner = tracker.progress.get("routing_resume_owner") == "parallel"
        if persisted_parallel_owner and not parallel_bounded_routing:
            self._preserve_process_local_owner_for_retry(
                session_id=tracker.session_id,
                execution_id=exec_id,
            )
            return Result.err(
                OrchestratorError(
                    message="Persisted parallel Routing D owner cannot enforce its route",
                    details={
                        "session_id": tracker.session_id,
                        "execution_id": exec_id,
                        "resume_blocked": "routing_enforcement_unavailable",
                        "routing_resume_owner": "parallel",
                        "retryable": True,
                    },
                )
            )

        # A paused parallel owner must reuse its already-durable plan.  Running
        # dependency analysis again would be a provider effect before Routing D
        # replay has validated its judgment/observation chain.
        if resume_execution_plan is not None:
            execution_plan = resume_execution_plan
        elif force_sequential_levels:
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

        if resume_execution_plan is None:
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

        if contract_source is None:
            execution_profile = _execution_profile_for_seed(seed)
            inherited_runtime_handle = self._inherited_runtime_handle
        else:
            execution_profile = self._execution_profile_snapshot(
                contract_source,
                require_bound=True,
            )
            inherited_runtime_handle = self._execution_inherited_runtime_handle_snapshot(
                contract_source,
                require_bound=True,
            )

        # Start from pre-flight, then probe toward the worker budget after sustained success.
        if effective_workers < max_parallel_workers:
            self._console.print(
                f"[yellow]Initial fan-out set to {effective_workers} worker(s) for backend "
                f"'{self._adapter.runtime_backend}' (adaptive ceiling "
                f"{max_parallel_workers}).[/yellow]"
            )
            log.info(
                "orchestrator.runner.fan_out_initialized",
                runtime_backend=self._adapter.runtime_backend,
                requested_workers=max_parallel_workers,
                effective_workers=effective_workers,
            )

        # Execute in parallel. Reuse the base effort resolved once in __init__
        # (self._reasoning_effort) so a single runner instance has one consistent
        # effort source across its direct paths and the parallel executor.
        # Capture the activation snapshot immediately before construction so
        # the executor and the later owner publication use the same decision.
        parallel_executor = ParallelACExecutor(
            adapter=self._adapter,
            event_store=self._event_store,
            console=self._console,
            enable_decomposition=execution_semantics["enable_decomposition"],
            decomposition_mode=execution_semantics["decomposition_mode"],
            max_concurrent=effective_workers,
            adaptive_max_concurrent=max_parallel_workers,
            max_decomposition_depth=max_decomposition_depth,
            inherited_runtime_handle=inherited_runtime_handle,
            task_cwd=self._effective_cwd(),
            checkpoint_store=self._checkpoint_store,
            execution_profile=execution_profile,
            fat_harness_mode=execution_semantics["fat_harness_mode"],
            reasoning_effort=self._reasoning_effort,
            # Legacy model selection predates Routing D and remains active when
            # durable route ownership is unavailable (for example, configured
            # decomposition depths above four). ParallelACExecutor separately
            # gates only bounded escalation/replay with its durable-depth flag.
            model_router=self._model_router,
            route_economics=self._route_economics,
            run_verify_commands=execution_semantics["run_verify_commands"],
            verify_command_timeout_seconds=execution_semantics["verify_command_timeout_seconds"],
            verify_shell_identity=cast(
                Mapping[str, object] | None,
                execution_semantics["verify_shell_identity"],
            ),
            ac_retry_attempts=execution_semantics["ac_retry_attempts"],
            cross_harness_redispatch=execution_semantics["cross_harness_redispatch"],
            shadow_replay_enabled=execution_semantics["shadow_replay_enabled"],
            session_signal_hub=self._session_signal_hub,
            process_local_resume_nonce=self._parallel_process_local_resume_nonce(tracker),
            resolved_backend_limits=resolved_backend_limits,
            resolved_self_governs_rate_limit=execution_semantics["backend_self_governs_rate_limit"],
            expected_runtime_effect_capabilities=execution_semantics["runtime_effect_capabilities"],
            usage_limit_pause_seconds=execution_semantics["usage_limit_pause_seconds"],
        )

        raw_published_pause_owner = tracker.progress.get("pause_owner")
        if (
            tracker.status is SessionStatus.PAUSED
            and raw_published_pause_owner is not None
            and not isinstance(raw_published_pause_owner, Mapping)
        ):
            raise OrchestratorError(
                message="Persisted coordinator pause owner is malformed",
                details={
                    "session_id": tracker.session_id,
                    "execution_id": exec_id,
                    "resume_blocked": "coordinator_pause_owner_invalid",
                },
            )
        published_coordinator_pause_owner = (
            dict(raw_published_pause_owner)
            if tracker.status is SessionStatus.PAUSED
            and isinstance(raw_published_pause_owner, Mapping)
            else None
        )

        # Check for cancellation before starting parallel execution
        if await self._check_cancellation(tracker.session_id):
            return await self._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=exec_id,
                messages_processed=0,
                start_time=start_time,
                expected_root_indices=range(len(seed.acceptance_criteria)),
            )

        if parallel_bounded_routing:
            # Publish the parallel effect owner before Routing D can append a
            # route judgment, observation, pause, or enter a provider boundary.
            # Legacy parallel execution has no complete durable stage replay
            # owner, so it must not publish this stronger resume claim.
            resume_owner_progress = {
                "routing_resume_owner": "parallel",
                "routing_parallel_force_sequential": force_sequential_levels,
                "routing_parallel_plan": self._serialize_parallel_resume_plan(execution_plan),
                "routing_parallel_externally_satisfied_acs": (
                    self._serialize_parallel_external_satisfaction(
                        seed,
                        externally_satisfied_acs,
                    )
                ),
            }
            owner_result = await self._session_repo.track_progress(
                tracker.session_id,
                resume_owner_progress,
            )
            if owner_result.is_err:
                return Result.err(
                    OrchestratorError(
                        message="Failed to persist the parallel Routing D resume owner",
                        details={
                            "session_id": tracker.session_id,
                            "execution_id": exec_id,
                            "cause": str(owner_result.error),
                        },
                    )
                )
            tracker = tracker.with_progress(resume_owner_progress)

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
                published_coordinator_pause_owner=published_coordinator_pause_owner,
            )
        except ParallelExecutionCancelled as cancelled:
            return await self._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=exec_id,
                messages_processed=cancelled.messages_processed,
                start_time=start_time,
                expected_root_indices=range(len(seed.acceptance_criteria)),
            )

        # Check for cancellation after parallel execution
        if await self._check_cancellation(tracker.session_id):
            return await self._handle_cancellation(
                session_id=tracker.session_id,
                execution_id=exec_id,
                messages_processed=parallel_result.total_messages,
                start_time=start_time,
                expected_root_indices=range(len(seed.acceptance_criteria)),
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
                require_all_failures_recoverable=not bool(
                    getattr(parallel_result, "recoverable_route_pause", False)
                    or getattr(parallel_result, "recoverable_coordinator_pause", False)
                ),
                default_pause_seconds=execution_semantics["usage_limit_pause_seconds"],
            )

        final_message = render_parallel_completion_message(
            parallel_result,
            len(seed.acceptance_criteria),
        )
        verification_report = render_parallel_verification_report(
            parallel_result,
            len(seed.acceptance_criteria),
            max_decomposition_depth=max_decomposition_depth,
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
            "max_decomposition_depth": max_decomposition_depth,
            "max_parallel_workers": max_parallel_workers,
            "effective_parallel_workers": effective_workers,
            "verification_report": verification_report,
            **self._task_summary(),
        }

        durable_terminal_status: SessionStatus | None = None
        acceptance_finalizations: list[dict[str, Any]] | None = None
        if success:
            acceptance_finalizations = self._build_terminal_acceptance_finalizations(
                seed=seed,
                parallel_result=parallel_result,
                execution_id=exec_id,
                session_id=tracker.session_id,
                terminal_status=SessionStatus.COMPLETED.value,
                execution_contract=execution_contract,
            )
            durable_terminal_status = await self._persist_session_terminal_status(
                session_id=tracker.session_id,
                execution_id=exec_id,
                requested_status=SessionStatus.COMPLETED,
                summary=execution_summary,
                messages_processed=parallel_result.total_messages,
                acceptance_finalizations=acceptance_finalizations,
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
                pause_owner=(
                    recoverable_failure_pause.coordinator_owner.owner_payload()
                    if recoverable_failure_pause.coordinator_owner is not None
                    else None
                ),
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
            acceptance_finalizations = self._build_terminal_acceptance_finalizations(
                seed=seed,
                parallel_result=parallel_result,
                execution_id=exec_id,
                session_id=tracker.session_id,
                terminal_status=SessionStatus.FAILED.value,
                execution_contract=execution_contract,
            )
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
                acceptance_finalizations=acceptance_finalizations,
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

        if terminal_status != "paused":
            await self._cleanup_terminal_process_local_state(
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
                release_liveness_lease=False,
            )
            self._release_task_workspace_for_identity(
                session_id=tracker.session_id,
                execution_id=tracker.execution_id,
            )

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

    def _build_terminal_acceptance_finalizations(
        self,
        *,
        seed: Seed,
        parallel_result: Any,
        execution_id: str,
        session_id: str,
        terminal_status: str,
        accepted_root_indices: set[int] | None = None,
        default_outcome: str | None = None,
        execution_contract: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Build the complete root decision set before terminal CAS."""
        if terminal_status == SessionStatus.PAUSED.value:
            return []
        del execution_contract
        acceptance_generation_id = acceptance_generation_id_for_session(
            session_id,
            execution_id,
        )

        results_by_index: dict[int, Any] = {}
        for result in getattr(parallel_result, "results", ()):
            ac_index = getattr(result, "ac_index", None)
            if isinstance(ac_index, bool) or not isinstance(ac_index, int) or ac_index < 0:
                continue
            if ac_index in results_by_index:
                raise PersistenceError(
                    "Final acceptance received duplicate root AC results.",
                    operation="runner.emit_terminal_acceptance_finalized",
                    details={"root_ac_index": ac_index, "execution_id": execution_id},
                )
            results_by_index[ac_index] = result

        finalizations: list[dict[str, Any]] = []
        for root_ac_index in range(len(seed.acceptance_criteria)):
            result = results_by_index.get(root_ac_index)
            outcome = getattr(getattr(result, "outcome", None), "value", None)
            if outcome is None and default_outcome is not None:
                outcome = default_outcome
            if not isinstance(outcome, str) or not outcome.strip():
                outcome = (
                    "blocked"
                    if result is None
                    else ("failed" if not result.success else "succeeded")
                )
            retry_attempt = getattr(result, "retry_attempt", 0) if result is not None else 0
            if (
                isinstance(retry_attempt, bool)
                or not isinstance(retry_attempt, int)
                or retry_attempt < 0
            ):
                retry_attempt = 0
            accepted = bool(
                terminal_status == SessionStatus.COMPLETED.value
                and (
                    root_ac_index in accepted_root_indices
                    if accepted_root_indices is not None
                    else result is not None and outcome in {"succeeded", "satisfied_externally"}
                )
            )
            disposition = (
                "accepted"
                if accepted
                else (
                    "cancelled"
                    if terminal_status == SessionStatus.CANCELLED.value
                    else (
                        "rejected" if outcome in {"succeeded", "satisfied_externally"} else outcome
                    )
                )
            )
            finalizations.append(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "acceptance_generation_id": acceptance_generation_id,
                    "root_ac_index": root_ac_index,
                    "final_retry_attempt": retry_attempt,
                    "accepted": accepted,
                    "disposition": disposition,
                    "outcome": outcome.strip(),
                    "terminal_status": terminal_status,
                }
            )
        return finalizations

    async def resume_session(
        self,
        session_id: str,
        seed: Seed,
    ) -> Result[OrchestratorResult, OrchestratorError]:
        """Serialize resume invocations that temporarily restore runner state."""
        async with self._resume_lock:
            return await self._resume_session_impl(session_id, seed)

    async def _resume_session_impl(
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

        # Reconstruct session. A transient read failure is not evidence that
        # the retained process-local owner is lost; preserve it as a typed,
        # retryable lifecycle result so a background wrapper cannot append a
        # generic FAILED event or evict the owner/store.
        try:
            session_result = await self._session_repo.reconstruct_session(session_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to reconstruct session: {exc}",
                    details={
                        "session_id": session_id,
                        "cause": str(exc),
                        "resume_blocked": "process_local_reconstruction_pending",
                        "retryable": True,
                    },
                )
            )

        if session_result.is_err:
            return Result.err(
                OrchestratorError(
                    message=f"Failed to reconstruct session: {session_result.error}",
                    details={
                        "session_id": session_id,
                        "cause": str(session_result.error),
                        "resume_blocked": "process_local_reconstruction_pending",
                        "retryable": True,
                    },
                )
            )

        tracker = session_result.value
        parallel_resume_owner = tracker.progress.get("routing_resume_owner") == "parallel"

        # Check if session can be resumed
        if tracker.status in (
            SessionStatus.COMPLETED,
            SessionStatus.CANCELLED,
            SessionStatus.FAILED,
        ):
            self._pending_lifecycle_intents.pop(session_id, None)
            await self._cleanup_terminal_process_local_state(
                session_id=session_id,
                execution_id=tracker.execution_id,
            )
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
            if error.details.get("terminal_persistence_pending") is not True:
                await clear_cancellation(session_id)
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
            if error.details.get("terminal_persistence_pending") is not True:
                await clear_cancellation(session_id)
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
                        expected_root_indices=range(len(seed.acceptance_criteria)),
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
                self._release_task_workspace_for_identity(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                )
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
                self._release_task_workspace_for_identity(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                )
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

        if self._fat_harness_mode and not parallel_resume_owner:
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

        if _seed_has_investment_metadata(seed) and not parallel_resume_owner:
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
            runtime_handle = self._deserialize_runtime_handle(tracker.progress)
            runtime_handle = self._force_runtime_handle_permission(runtime_handle)
            self._validate_runtime_handle_backend(runtime_handle)
            self._validate_bound_runtime_resume_identity(tracker.progress, runtime_handle)
            execution_contract_changed, execution_contract = await asyncio.to_thread(
                self._restore_execution_contract_snapshot,
                tracker.progress,
                seed=seed,
                authority_generation=authority_generation,
                runtime_handle=runtime_handle,
            )
            execution_semantics = self._execution_semantics_snapshot(execution_contract)
            self._execution_guidance_delivery_mode()
        except asyncio.CancelledError:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=tracker.messages_processed,
                    start_time=tracker.start_time,
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
                )
            )
            if cancellation_result is not None:
                return cancellation_result
            if exc.details.get("resume_blocked") == "project_identity_unavailable":
                self._preserve_process_local_owner_for_retry(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                )
                return Result.err(exc)
            _, persistence_pending = await self._persist_failure_and_cleanup(
                session_id=session_id,
                execution_id=tracker.execution_id,
                error=exc,
                seed=seed,
                execution_contract=(
                    dict(raw_contract) if isinstance(raw_contract, Mapping) else None
                ),
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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
                )

            if execution_contract_changed:
                contract_progress = {
                    EXECUTION_CONTRACT_PROGRESS_KEY: execution_contract,
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

            # Reuse the exact strategy that produced the original prompt/tool
            # boundary. Profile files and the task-type registry are mutable
            # current configuration and therefore are not resume authority.
            strategy = self._execution_strategy_snapshot(
                execution_contract,
                require_bound=True,
            )
            system_prompt = build_system_prompt(
                seed,
                strategy=strategy,
                repo_root=self._effective_cwd(),
                guidance_fragment=self._ensure_new_run_guidance().rendered_fragment,
                context_pack_enabled=execution_semantics["context_pack_enabled"],
                resolved_context_pack_fragment=(
                    self._execution_context_pack_fragment_snapshot(
                        execution_contract,
                        require_bound=True,
                    )
                ),
            )
            await self._record_execution_guidance_injection(
                session_id=session_id,
                execution_id=tracker.execution_id,
                injection_key=f"resume:{tracker.messages_processed}",
            )
            resume_prompt = f"""Continue executing the task from where you left off.

{build_task_prompt(seed, strategy=strategy)}

Note: This is a resumed session. Please continue from where execution was interrupted.
"""
            self._validate_resume_handle_execution_identity(runtime_handle)
            if self._task_workspace is not None and "workspace" not in tracker.progress:
                await self._persist_session_progress(
                    session_id,
                    {"workspace": self._task_workspace.to_progress_dict()},
                )

            # Discover handlers for the persisted base strategy, then require
            # exact equality with the complete original catalog before any
            # provider resume effect.
            merged_tools, mcp_provider, tool_catalog = await self._get_merged_tools(
                session_id=session_id,
                tool_prefix=self._mcp_tool_prefix,
                strategy=strategy,
            )
            execution_contract, inputs_changed = self._bind_execution_tool_authority(
                execution_contract,
                merged_tools=merged_tools,
                tool_catalog=tool_catalog,
            )
            if inputs_changed:
                bound_progress = {
                    EXECUTION_CONTRACT_PROGRESS_KEY: execution_contract,
                    "messages_processed": tracker.messages_processed,
                }
                persisted_inputs = await self._session_repo.track_progress(
                    session_id,
                    bound_progress,
                )
                if persisted_inputs.is_err:
                    raise OrchestratorError(
                        message="Failed to persist migrated prompt/tool authority",
                        details={"session_id": session_id, "cause": str(persisted_inputs.error)},
                    )
                tracker = tracker.with_progress(bound_progress)
                self._execution_contract = execution_contract
            runtime_handle = self._seed_runtime_handle(
                runtime_handle,
                tool_catalog=tool_catalog,
                preserve_existing_tool_catalog=True,
            )

            start_time = datetime.now(UTC)
            messages_processed = tracker.messages_processed
            final_message = ""
            success = False
            recoverable_resume_failure: RecoverableFailurePause | None = None

            # Create workflow state tracker for progress display
            from ouroboros.orchestrator.workflow_state import WorkflowStateTracker

            state_tracker = WorkflowStateTracker(
                acceptance_criteria=list(seed.acceptance_criteria),
                goal=seed.goal,
                session_id=session_id,
                activity_map=strategy.get_activity_map(),
            )
            await self._replay_workflow_state(session_id, state_tracker)

            if parallel_resume_owner:
                force_sequential = tracker.progress.get(
                    "routing_parallel_force_sequential",
                    False,
                )
                if type(force_sequential) is not bool:
                    raise OrchestratorError(
                        message="Invalid persisted parallel Routing D resume owner state",
                        details={
                            "session_id": session_id,
                            "execution_id": tracker.execution_id,
                        },
                    )
                resume_execution_plan = self._deserialize_parallel_resume_plan(
                    seed,
                    tracker.progress.get("routing_parallel_plan"),
                )
                resume_externally_satisfied_acs = self._deserialize_parallel_external_satisfaction(
                    seed,
                    tracker.progress.get("routing_parallel_externally_satisfied_acs"),
                )
                try:
                    return await self._execute_parallel(
                        seed=seed,
                        exec_id=tracker.execution_id,
                        tracker=tracker,
                        merged_tools=merged_tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        start_time=start_time,
                        execution_contract=execution_contract,
                        externally_satisfied_acs=resume_externally_satisfied_acs,
                        force_sequential_levels=force_sequential,
                        resume_execution_plan=resume_execution_plan,
                    )
                finally:
                    await self._close_adapter()
        except asyncio.CancelledError:
            cancellation_result = (
                await self._drain_requested_cancellation_before_pre_execution_cleanup(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=tracker.messages_processed,
                    start_time=tracker.start_time,
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                seed=seed,
                execution_contract=execution_contract,
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
            runtime_handle_transferred_to_pause = False
            cancelled_result: Result[OrchestratorResult, OrchestratorError] | None = None
            resume_route_state: _DirectRouteResumeState | None = None
            last_resume_final_message: AgentMessage | None = None
            resume_terminal_blocked = False

            with Status(
                f"[bold cyan]Resuming: {seed.goal[:50]}...[/]",
                console=self._console,
                spinner="dots",
            ) as status:
                self._announce_param_degradations(
                    system_prompt=system_prompt,
                    tools=merged_tools,
                )
                resume_route_state = await self._direct_resume_route_id(
                    execution_id=tracker.execution_id,
                    session_id=session_id,
                )
                if resume_route_state is not None and not self._has_exact_resumable_runtime_handle(
                    runtime_handle
                ):
                    raise OrchestratorError(
                        message=(
                            "Refusing to replay a paused direct route without its exact "
                            "resumable provider handle"
                        ),
                        details={
                            "session_id": session_id,
                            "execution_id": tracker.execution_id,
                            "resume_blocked": "provider_handle_unavailable",
                            "human_handoff_required": True,
                        },
                    )
                effort_kwargs = await self._route_call_effort(
                    execution_id=tracker.execution_id,
                    session_id=session_id,
                    bounded_escalation=resume_route_state is not None,
                    route_id_override=(
                        resume_route_state.candidate.route_id
                        if resume_route_state is not None
                        else None
                    ),
                    expected_route_candidate=(
                        resume_route_state.candidate if resume_route_state is not None else None
                    ),
                    expected_runtime_effect_capabilities=execution_semantics[
                        "runtime_effect_capabilities"
                    ],
                )
                if resume_route_state is not None:
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=session_id,
                        execution_id=tracker.execution_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )
                    if cancelled_result is not None:
                        return cancelled_result
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
                            cancelled_result = await self._handle_requested_cancellation(
                                session_id=session_id,
                                execution_id=tracker.execution_id,
                                messages_processed=messages_processed,
                                start_time=start_time,
                                expected_root_indices=range(len(seed.acceptance_criteria)),
                            )
                            if cancelled_result is not None:
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
                            last_resume_final_message = message
                            final_message = message.content
                            success = not message.is_error
                            recoverable_resume_failure = self._recoverable_failure_pause(
                                message,
                                now=datetime.now(UTC),
                                default_pause_seconds=execution_semantics[
                                    "usage_limit_pause_seconds"
                                ],
                            )

                if (
                    recoverable_resume_failure is not None
                    and resume_route_state is not None
                    and not await self._persist_exact_direct_pause_runtime_handle(
                        session_id=session_id,
                        runtime_handle=live_runtime_handle,
                        messages_processed=messages_processed,
                    )
                ):
                    recoverable_resume_failure = None
                    resume_terminal_blocked = True
                    success = False
                    final_message = (
                        f"{final_message}\nRecoverable provider pause rejected: no exact "
                        "resumable handle is available; human handoff required."
                    )

                if resume_route_state is not None and cancelled_result is None:
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=session_id,
                        execution_id=tracker.execution_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )

            if (
                resume_route_state is not None
                and cancelled_result is None
                and recoverable_resume_failure is None
            ):
                cancelled_result = await self._handle_requested_cancellation(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                    expected_root_indices=range(len(seed.acceptance_criteria)),
                )
            if (
                resume_route_state is not None
                and cancelled_result is None
                and recoverable_resume_failure is None
            ):
                decision, route_history = await self._persist_direct_route_outcome(
                    execution_id=tracker.execution_id,
                    session_id=session_id,
                    episode_id=resume_route_state.episode_id,
                    prior_route_ids=resume_route_state.prior_route_ids,
                    candidate=resume_route_state.candidate,
                    success=success,
                    failure_class=(
                        FailureClass.BLOCKED
                        if resume_terminal_blocked
                        else self._classify_direct_route_failure(last_resume_final_message)
                    ),
                )
                cancelled_result = await self._handle_requested_cancellation(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    messages_processed=messages_processed,
                    start_time=start_time,
                    expected_root_indices=range(len(seed.acceptance_criteria)),
                )
                while not success and decision is not None and not decision.blocked:
                    if cancelled_result is not None:
                        break
                    assert decision.selected is not None
                    await self._terminate_runtime_handle(
                        live_runtime_handle,
                        session_id=session_id,
                        context="bounded_route_resume_escalation",
                    )
                    live_runtime_handle = None
                    successor = decision.selected
                    successor_kwargs = await self._route_call_effort(
                        execution_id=tracker.execution_id,
                        session_id=session_id,
                        bounded_escalation=True,
                        route_id_override=successor.route_id,
                        expected_route_candidate=successor,
                        expected_runtime_effect_capabilities=execution_semantics[
                            "runtime_effect_capabilities"
                        ],
                    )
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=session_id,
                        execution_id=tracker.execution_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )
                    if cancelled_result is not None:
                        break
                    final_message = ""
                    last_resume_final_message = None
                    recoverable_resume_failure = None
                    async with (
                        aclosing(
                            self._adapter.execute_task(  # type: ignore[type-var]
                                prompt=(
                                    build_task_prompt(seed, strategy=strategy)
                                    + "\n\nThe resumed route failed. Continue in a fresh "
                                    "session and satisfy the same Seed contracts."
                                ),
                                tools=merged_tools,
                                system_prompt=system_prompt,
                                resume_handle=None,
                                **successor_kwargs,
                            )
                        ) as successor_stream
                    ):
                        async for message in successor_stream:
                            messages_processed += 1
                            if messages_processed % CANCELLATION_CHECK_INTERVAL == 0:
                                cancelled_result = await self._handle_requested_cancellation(
                                    session_id=session_id,
                                    execution_id=tracker.execution_id,
                                    messages_processed=messages_processed,
                                    start_time=start_time,
                                    expected_root_indices=range(len(seed.acceptance_criteria)),
                                )
                                if cancelled_result is not None:
                                    break
                            tracker = await self._update_and_persist_progress(
                                tracker,
                                message,
                                messages_processed,
                                session_id,
                            )
                            if message.resume_handle is not None:
                                live_runtime_handle = message.resume_handle
                            state_tracker.process_runtime_message(message)
                            if message.is_final:
                                last_resume_final_message = message
                                final_message = message.content
                                success = not message.is_error
                                recoverable_resume_failure = self._recoverable_failure_pause(
                                    message,
                                    now=datetime.now(UTC),
                                    default_pause_seconds=execution_semantics[
                                        "usage_limit_pause_seconds"
                                    ],
                                )
                    if cancelled_result is None:
                        cancelled_result = await self._handle_requested_cancellation(
                            session_id=session_id,
                            execution_id=tracker.execution_id,
                            messages_processed=messages_processed,
                            start_time=start_time,
                            expected_root_indices=range(len(seed.acceptance_criteria)),
                        )
                    if cancelled_result is not None:
                        break
                    if recoverable_resume_failure is not None:
                        if not await self._persist_exact_direct_pause_runtime_handle(
                            session_id=session_id,
                            runtime_handle=live_runtime_handle,
                            messages_processed=messages_processed,
                        ):
                            recoverable_resume_failure = None
                            resume_terminal_blocked = True
                            success = False
                            final_message = (
                                f"{final_message}\nRecoverable provider pause rejected: no exact "
                                "resumable handle is available; human handoff required."
                            )
                    if recoverable_resume_failure is not None:
                        from ouroboros.events.base import BaseEvent

                        await self._event_store.append(
                            BaseEvent(
                                type="execution.ac.route_paused",
                                aggregate_type="execution",
                                aggregate_id=tracker.execution_id,
                                data={
                                    "schema_version": 1,
                                    "execution_id": tracker.execution_id,
                                    "session_id": session_id,
                                    "root_ac_index": None,
                                    "call_site": "runner",
                                    "episode_id": resume_route_state.episode_id,
                                    "attempt_index": len(route_history),
                                    "prior_route_ids": list(route_history),
                                    "route": successor.to_contract_data(),
                                    "recoverable_pause": True,
                                    "final_acceptance_declared": False,
                                },
                            )
                        )
                        break
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=session_id,
                        execution_id=tracker.execution_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )
                    if cancelled_result is not None:
                        break
                    decision, route_history = await self._persist_direct_route_outcome(
                        execution_id=tracker.execution_id,
                        session_id=session_id,
                        episode_id=resume_route_state.episode_id,
                        prior_route_ids=route_history,
                        candidate=successor,
                        success=success,
                        failure_class=self._classify_direct_route_failure(
                            last_resume_final_message
                        ),
                    )
                    cancelled_result = await self._handle_requested_cancellation(
                        session_id=session_id,
                        execution_id=tracker.execution_id,
                        messages_processed=messages_processed,
                        start_time=start_time,
                        expected_root_indices=range(len(seed.acceptance_criteria)),
                    )
                if not success and decision is not None and decision.blocked:
                    resume_terminal_blocked = True
                    final_message = (
                        f"{final_message}\nRoute escalation stopped: "
                        f"{decision.reason.value}; human handoff required."
                    )

            if cancelled_result is not None:
                return cancelled_result

            duration = (datetime.now(UTC) - start_time).total_seconds()

            durable_terminal_status: SessionStatus | None = None
            acceptance_finalizations: list[dict[str, Any]] | None = None
            if success:
                acceptance_finalizations = self._build_terminal_acceptance_finalizations(
                    seed=seed,
                    parallel_result=None,
                    execution_id=tracker.execution_id,
                    session_id=session_id,
                    terminal_status=SessionStatus.COMPLETED.value,
                    accepted_root_indices=set(range(len(seed.acceptance_criteria))),
                    default_outcome="succeeded",
                    execution_contract=execution_contract,
                )
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    requested_status=SessionStatus.COMPLETED,
                    summary={
                        "messages_processed": messages_processed,
                        **self._task_summary(),
                    },
                    messages_processed=messages_processed,
                    acceptance_finalizations=acceptance_finalizations,
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
                    runtime_handle_transferred_to_pause = True
                    return pause_pending
                assert pause_status is not None
                if pause_status is SessionStatus.PAUSED:
                    runtime_handle_transferred_to_pause = True
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
                acceptance_finalizations = self._build_terminal_acceptance_finalizations(
                    seed=seed,
                    parallel_result=None,
                    execution_id=tracker.execution_id,
                    session_id=session_id,
                    terminal_status=SessionStatus.FAILED.value,
                    default_outcome="blocked" if resume_terminal_blocked else "failed",
                    execution_contract=execution_contract,
                )
                durable_terminal_status = await self._persist_session_terminal_status(
                    session_id=session_id,
                    execution_id=tracker.execution_id,
                    requested_status=SessionStatus.FAILED,
                    error_message=final_message,
                    messages_processed=messages_processed,
                    acceptance_finalizations=acceptance_finalizations,
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

            async def _cleanup_resumed_owner() -> None:
                # A paused owner has not acknowledged a cancellation that may
                # have arrived after its final execution checkpoint. Preserve
                # that marker so the next resume terminalizes before effects.
                if terminal_status != "paused":
                    await self._cleanup_terminal_process_local_state(
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
                        release_liveness_lease=False,
                    )
                    self._release_task_workspace_for_identity(
                        session_id=tracker.session_id,
                        execution_id=tracker.execution_id,
                    )

            await _await_process_local_cleanup(_cleanup_resumed_owner())

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
                    expected_root_indices=range(len(seed.acceptance_criteria)),
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
                seed=seed,
                execution_contract=execution_contract,
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
            if not runtime_handle_transferred_to_pause:
                await self._terminate_runtime_handle(
                    live_runtime_handle,
                    session_id=session_id,
                    context="resume",
                )
                await self._close_adapter()


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
