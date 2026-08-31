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
from collections.abc import Awaitable, Callable, Mapping
import contextlib
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import wraps
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import time
from typing import TYPE_CHECKING, Any, Literal, NamedTuple
from uuid import uuid4
from weakref import ref

import anyio
from rich.console import Console

from ouroboros import telemetry as usage_telemetry
from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    ac_text,
    derive_semantic_ac_key,
)
from ouroboros.core.session_signal import SessionSignalMode
from ouroboros.events.session_signal import (
    create_session_signal_applied_event,
    create_session_signal_completed_event,
    create_session_signal_delivery_uncertain_event,
    create_session_signal_rejected_event,
)
from ouroboros.evolution.provider_usage import tracked_agent_task

# Import the harness submodules directly, NOT the ``ouroboros.harness`` package
# aggregate: ``harness.__init__`` pulls in ``deliver_routing`` which imports from
# ``ouroboros.orchestrator``, so importing the aggregate here would re-enter a
# partially-initialized ``harness`` during ``orchestrator`` package import. The
# concrete submodules below import nothing from ``orchestrator``, breaking the cycle.
from ouroboros.harness.claim_term_guard import strict_deterministic_claim_term_guard
from ouroboros.harness.deliver_gate import (
    DeliverEvidenceClaim,
    DeliverEvidenceFact,
    _journal_entry_proves_command_artifact,
    evaluate_deliver_claim,
    load_ac_evidence_manifest,
)
from ouroboros.harness.journal import EvidenceEntry, EvidenceManifest
from ouroboros.harness.traceguard_validator import validate_evidence_claims
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator import (
    adaptive_concurrency,
    provider_admission,
    provider_effect_scope,
    retry_hints,
)
from ouroboros.orchestrator.ac_execution_capsule import (
    UnmaterializableSuccessContractError,
    bind_capsule_to_runtime_handle,
    build_ac_dispatch_authority_scope,
    build_ac_dispatch_request_digest,
    compile_ac_execution_capsule,
)
from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    RuntimeHandle,
    resolve_worker_cwd,
)
from ouroboros.orchestrator.atomic_prompt_builder import (
    AtomicPromptBuilder,
    _build_success_contract_block,  # noqa: F401  (re-exported for tests/back-compat)
)
from ouroboros.orchestrator.backend_limits import (
    BackendConcurrencyLimits,
    resolve_backend_limits,
)
from ouroboros.orchestrator.backend_outcomes import outcome_weights as _safe_backend_outcome_weights
from ouroboros.orchestrator.context_governor import SiblingStatus, compose_context
from ouroboros.orchestrator.coordinator import (
    CoordinatorReview,
    FileConflict,
    LevelCoordinator,
    validate_coordinator_started_payload,
)
from ouroboros.orchestrator.coordinator_quota import (
    normalize_published_coordinator_pause_owner,
    resolve_replayed_coordinator_quota_pause,
    resolve_usage_limit_pause_seconds,
    restore_checkpointed_coordinator_quota,
)
from ouroboros.orchestrator.decomposition_limits import (
    DEFAULT_MAX_DECOMPOSITION_DEPTH,
    MAX_DECOMPOSITION_CHILDREN,
    MAX_DECOMPOSITION_DEPTH,  # noqa: F401  (re-exported for tests/back-compat)
    MAX_DECOMPOSITION_REPLAY_NODES,
    MIN_DECOMPOSITION_CHILDREN,
    has_durable_decomposition_replay,
    validate_max_decomposition_depth,
)
from ouroboros.orchestrator.decomposition_params import (
    build_decomposition_system_prompt,
    params_from_profile,
)
from ouroboros.orchestrator.decomposition_policy import (
    MAX_EVIDENCE_REF_CHARS,
    MAX_EVIDENCE_REF_COUNT,
    MAX_REASON_CHARS,
    MAX_TRACE_SUMMARY_CHARS,
    BounceCause,
    DecompositionDecisionRecord,
    DecompositionDisposition,
    DecompositionProposal,
    DecompositionSource,
    DecompositionTraceSummary,
    SemanticAttestationStatus,
    StructuralCheckStatus,
    legacy_unverified_split_decision,
    parse_decomposition_proposal,
    redact_and_truncate_text,
    summarize_decomposition_trace,
    validate_decomposition_proposal,
)
from ouroboros.orchestrator.effort_routing import assess_investment, resolve_execute_effort
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
    _shell_command_body_from_argv,
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
    _text_proves_test_execution_success,
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
from ouroboros.orchestrator.execution_authority import (
    ExecutionAuthorityContract,
    ExecutionAuthorityLiveBinding,
    canonical_workspace_authority,
    runtime_effect_capabilities_contract,
    valid_runtime_effect_capabilities_contract,
)
from ouroboros.orchestrator.execution_event_emitter import ExecutionEventEmitter
from ouroboros.orchestrator.execution_event_replay import (
    replay_execution_events_chronologically,
)
from ouroboros.orchestrator.execution_runtime_scope import (
    ACRuntimeIdentity,
    ExecutionNodeIdentity,
    build_ac_runtime_identity,
    build_level_coordinator_runtime_scope,
)
from ouroboros.orchestrator.frugality_evidence import (
    harvest_token_spend as _harvest_token_spend,
)
from ouroboros.orchestrator.frugality_evidence import (
    observed_effective_model as _observed_effective_model,
)
from ouroboros.orchestrator.leaf_dispatcher import (
    LeafDispatcher,
    LeafDispatchState,
)
from ouroboros.orchestrator.level_context import (
    ACContextSummary,
    LevelContext,
    build_context_prompt,
    deserialize_level_contexts,
    extract_level_context,
    serialize_level_contexts,
)
from ouroboros.orchestrator.mcp_tools import serialize_tool_catalog
from ouroboros.orchestrator.model_routing import (
    MODEL_MODE_ENFORCED,
    MODEL_TIER_LADDER,
    ModelDecision,
    decide_model,
    resolve_execute_model,
    serialize_model_router,
    tier_from_profile_hint,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    CoordinatorQuotaPause,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
    StageExecutionOutcome,
    collect_decomposition_depth_warning_paths,
)
from ouroboros.orchestrator.profile_loader import ExecutionProfile, SuggestedModelTier
from ouroboros.orchestrator.rate_limit import (
    RateLimitBackoff,
    RateLimitGate,
    SharedRateLimitBucket,
    build_rate_limit_gate,
    estimate_runtime_request_tokens,
)
from ouroboros.orchestrator.recoverable_failure import is_usage_limit_pause_message
from ouroboros.orchestrator.route_compat import (
    RouteCompatProjection,
    admit_compat_escalation_route,
    admit_compat_route,
    admitted_execute_model_kwargs,
    build_compat_escalation_registry,
    build_compat_escalation_requirements,
    build_route_compat_projection,
    serialize_route_compat_contract,
    validate_compat_admission,
    validate_compat_escalation_admission,
)
from ouroboros.orchestrator.route_escalation import (
    MAX_EPISODE_ID_CHARS,
    MAX_ROUTE_ATTEMPTS,
    EscalationAction,
    EscalationReason,
    RouteEscalationDecision,
    RouteObservation,
    advance_route,
)
from ouroboros.orchestrator.route_escalation import (
    VerifierOutcome as RouteVerifierOutcome,
)
from ouroboros.orchestrator.route_policy import MAX_ROUTE_ID_CHARS, RouteCandidate
from ouroboros.orchestrator.runtime_param_negotiation import (
    announce_execution_param_degradations,
)
from ouroboros.orchestrator.session_signal_followup import (
    CompletedProviderTurn,
    _bounded_session_signal_runtime_reply,
    _is_session_signal_application_acknowledgement,
    abort_unentered_follow_up,
    claim_follow_up_delivery,
)
from ouroboros.orchestrator.shadow_replay import isolated_workspace, run_shadow_replay
from ouroboros.orchestrator.synapse import (
    SessionSignalTarget,
    render_after_turn_signal_prompt,
    render_inform_signal_prompt,
    target_ended_rejection_event,
)
from ouroboros.orchestrator.verifier import (
    RetryAdmission,
    Verifier,
    VerifierContractError,
    VerifierStatus,
    VerifierVerdict,
    verifier_operational_failure_verdict,
)
from ouroboros.orchestrator.verify_command_runner import run_with_shell
from ouroboros.orchestrator.verify_gate_outcome import (
    _VERIFY_OUTPUT_TAIL_CHARS,
    _deserialize_verify_gate_outcome,
    _mapping_has_exact_keys,
    _missing_artifacts_cause,
    _missing_expected_artifacts,
    _revalidate_cached_verify_gate_outcome,
    _serialize_verify_gate_outcome,
    _VerifyGateOutcome,
)
from ouroboros.orchestrator.verify_quarantine import (
    quarantine_unverifiable_result,
    render_unverifiable_summary,
)
from ouroboros.orchestrator.verify_shell import (
    capture_verify_shell_identity,
    resolve_verify_shell,
    sanitized_verify_environment,
    verify_shell_path_from_identity,
    verify_shell_unavailable_reason,
)
from ouroboros.orchestrator.workspace_evidence_paths import (
    is_untracked_top_level_evidence_path,
    load_tracked_workspace_paths,
)


class _AutoVerifyShell:
    """Constructor sentinel for low-level callers without durable semantics."""


_AUTO_VERIFY_SHELL = _AutoVerifyShell()

_PARALLEL_PAUSE_REPLAY_PAGE_SIZE = 64
_has_usage_limit_pause = adaptive_concurrency.has_usage_limit_pause
_PROVIDER_OBSERVATION_SINK: ContextVar[
    Callable[[adaptive_concurrency.ConcurrencyObservation], None] | None
] = ContextVar("ouroboros_provider_observation_sink", default=None)
_PARALLEL_ROUTE_OBSERVATION_KEYS = frozenset(
    {
        "schema_version",
        "execution_id",
        "session_id",
        "root_ac_index",
        "semantic_ac_key",
        "call_site",
        "observation",
        "decision",
        "provisional_result",
        "human_handoff_required",
        "final_acceptance_declared",
    }
)


def _composite_completion_event_sentinel(root_ac_count: int) -> int:
    """Return the max-plus-one population admitted by the root Seed."""

    if type(root_ac_count) is not int or root_ac_count < 0:
        raise ValueError("composite completion root population is invalid")
    # A completed composite is terminal and can be produced at most once for
    # each admitted root AC.  Replay and pre-dispatch detection must derive
    # their query sentinel from that producer population instead of imposing a
    # smaller execution-wide constant on otherwise valid Seeds.
    return root_ac_count + 1


def _decomposition_decision_event_sentinel(root_ac_count: int) -> int:
    """Return the max-plus-one durable decision population for one Seed.

    A historical PREFLIGHT atomic decision may transition once to the live
    BOUNCE decision after migration. No other finalized decision can change.
    """

    if type(root_ac_count) is not int or root_ac_count < 0:
        raise ValueError("decomposition decision root population is invalid")
    node_population = root_ac_count * (MAX_DECOMPOSITION_REPLAY_NODES + 1)
    return node_population * 2 + 1


_DECOMPOSITION_DECISION_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "node_id",
        "source",
        "disposition",
        "cause",
        "reasons",
        "evidence_refs",
        "children",
        "structural_status",
        "semantic_status",
        "repair_count",
        "trustworthy",
        "compromise_reason",
    }
)
_DECOMPOSITION_DECISION_EVENT_KEYS = frozenset(
    {
        "identity_model",
        "schema_version",
        "node_id",
        "parent_node_id",
        "legacy_node_id",
        "legacy_parent_node_id",
        "legacy_node_aliases",
        "legacy_parent_node_aliases",
        "root_ac_index",
        "root_ac_number",
        "path",
        "display_path",
        "depth",
        "ordinal",
        "node_kind",
        "execution_id",
        "session_id",
        "mode",
        "child_count",
        "source",
        "disposition",
        "cause",
        "reasons",
        "evidence_refs",
        "children",
        "structural_status",
        "semantic_status",
        "repair_count",
        "trustworthy",
        "compromise_reason",
    }
)
_BOUNCE_CLASSIFIED_EVENT_KEYS = frozenset(
    {
        "identity_model",
        "schema_version",
        "node_id",
        "parent_node_id",
        "legacy_node_id",
        "legacy_parent_node_id",
        "legacy_node_aliases",
        "legacy_parent_node_aliases",
        "root_ac_index",
        "root_ac_number",
        "path",
        "display_path",
        "depth",
        "ordinal",
        "node_kind",
        "execution_id",
        "session_id",
        "cause",
        "rationale",
        "failure_class",
        "retry_admission",
        "evidence_refs",
        "trace_summary",
    }
)


_PARALLEL_ROUTE_PAUSE_KEYS = frozenset(
    {
        "schema_version",
        "execution_id",
        "session_id",
        "root_ac_index",
        "semantic_ac_key",
        "call_site",
        "episode_id",
        "attempt_index",
        "prior_route_ids",
        "route",
        "resume_state",
        "recoverable_pause",
        "final_acceptance_declared",
    }
)
_PARALLEL_ROUTE_PAUSE_RESUME_KEYS = frozenset(
    {
        "retry_attempt",
        "retry_prompt_extra",
        "sibling_acs",
        "route_id_override",
        "expected_route_candidate",
        "runtime_scope_id",
        "dispatch_id",
        "capsule_fingerprint",
    }
)
_PARALLEL_UNCERTAIN_HANDOFF_KEYS = frozenset(
    {
        "schema_version",
        "execution_id",
        "session_id",
        "root_ac_index",
        "semantic_ac_key",
        "call_site",
        "reason",
        "human_handoff_required",
        "final_acceptance_declared",
    }
)
_PARALLEL_COMPOSITE_COMPLETION_KEYS = frozenset(
    {
        "schema_version",
        "execution_id",
        "session_id",
        "root_ac_index",
        "semantic_ac_key",
        "call_site",
        "result",
        "decomposition_decision",
        "decomposition_fingerprint",
        "final_acceptance_declared",
    }
)
_PARALLEL_COMPOSITE_PAUSE_KEYS = frozenset(
    {
        "schema_version",
        "execution_id",
        "session_id",
        "root_ac_index",
        "semantic_ac_key",
        "call_site",
        "frames",
        "paused_leaf",
        "recoverable_pause",
        "final_acceptance_declared",
    }
)
_PARALLEL_COMPOSITE_PAUSE_FRAME_KEYS = frozenset(
    {
        "completed_children",
        "paused_child_index",
        "paused_child_node_id",
        "paused_child_ac_index",
        "paused_child_content",
        "paused_child_retry_attempt",
        "decomposition_decision",
        "decomposition_fingerprint",
    }
)
_PARALLEL_COMPOSITE_PAUSE_LEAF_KEYS = frozenset(
    {
        "node_id",
        "ac_index",
        "ac_content",
        "retry_attempt",
        "runtime_scope_id",
        "dispatch_id",
        "capsule_fingerprint",
    }
)
_DECOMPOSITION_ATTESTATION_KEYS = frozenset(
    {
        "coverage_established",
        "non_overlap_established",
        "simpler_units_established",
    }
)
_BOUNCE_CLASSIFICATION_KEYS = frozenset({"cause", "has_remaining_scope"})
_PARALLEL_ROUTE_JUDGMENT_KEYS = frozenset(
    {
        "execution_id",
        "session_id",
        "root_ac_index",
        "ac_index",
        "retry_attempt",
        "attempt_number",
        "success",
        "outcome",
        "is_decomposed",
        "is_decomposed_child",
        "route_contract_version",
        "route_episode_id",
        "route_attempt_index",
        "route_id",
        "call_site",
    }
)


@dataclass(frozen=True, slots=True)
class _PartialCompositeResumeState:
    """Exact durable prefix and provider boundary for one paused split root."""

    decision: DecompositionDecisionRecord
    completed_children: tuple[ACExecutionResult, ...]
    paused_child_index: int
    paused_child_ac_index: int
    paused_child_content: str
    paused_child_retry_attempt: int
    paused_runtime_scope_id: str | None
    paused_dispatch_id: str | None
    paused_capsule_fingerprint: str | None


@dataclass(frozen=True, slots=True)
class _DurableBounceReplayState:
    """Validated TOO_BIG phase persisted before decomposition provider effects."""

    cause: BounceCause
    rationale: str
    failure_class: str | None
    retry_admission: str | None
    evidence_refs: tuple[str, ...]
    trace_summary: str
    event_key: tuple[datetime, str] | None


@dataclass(frozen=True, slots=True)
class _ParallelRouteResumeState:
    """Exact capsule-bearing inputs for one paused top-level route attempt."""

    candidate: RouteCandidate
    retry_attempt: int
    retry_prompt_extra: str
    sibling_acs: tuple[_SiblingACRef, ...]
    route_id_override: str | None
    expected_route_candidate: RouteCandidate | None
    runtime_scope_id: str
    dispatch_id: str
    capsule_fingerprint: str


if TYPE_CHECKING:
    from ouroboros.core.seed import Seed
    from ouroboros.mcp.types import MCPToolDefinition
    from ouroboros.orchestrator.adapter import AgentRuntime
    from ouroboros.orchestrator.dependency_analyzer import (
        DependencyGraph,
        StagedExecutionPlan,
    )
    from ouroboros.orchestrator.model_routing import ModelRouter
    from ouroboros.orchestrator.synapse import SessionSignalHub
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)


class ParallelExecutionCancelled(RuntimeError):
    """Signal a cooperative cancellation before another parallel effect."""

    def __init__(self, session_id: str, messages_processed: int) -> None:
        self.session_id = session_id
        self.messages_processed = messages_processed
        super().__init__(f"Parallel execution cancelled for session {session_id}")


@dataclass(frozen=True, slots=True)
class _FoundationAClosedRoots:
    """Original direct roots held by the executor constructor default."""

    leaf_dispatcher_type: type[LeafDispatcher]
    leaf_dispatcher_stream_root: Callable[..., Awaitable[None]]
    leaf_dispatcher_stream_code: object
    level_coordinator_type: type[LevelCoordinator]
    level_coordinator_review_root: object
    level_coordinator_review_code: object
    rate_gate_factory: Callable[..., RateLimitGate]
    rate_gate_type: type[RateLimitGate]
    rate_gate_acquire_root: Callable[..., Awaitable[None]]
    rate_gate_acquire_code: object
    rate_gate_sleep: object
    rate_gate_sleep_code: object | None
    rate_gate_bucket_type: type[SharedRateLimitBucket]
    rate_gate_bucket_time: object
    rate_gate_bucket_enabled_root: object
    rate_gate_bucket_enabled_code: object | None
    rate_gate_bucket_acquire_root: object
    rate_gate_bucket_acquire_code: object | None
    rate_gate_bucket_force_reserve_root: object
    rate_gate_bucket_force_reserve_code: object | None
    rate_gate_bucket_helper_roots: tuple[tuple[str, object | None, object | None], ...]
    transcript_verifier: Callable[..., VerifierVerdict]
    transcript_verifier_code: object


class _FoundationAInternalEntryRoots(NamedTuple):
    """Closed entry functions used by the executor's own orchestration path."""

    executor_type: type[object]
    execute_single_ac_root: Callable[..., Any]
    execute_single_ac_code: object
    execute_atomic_ac_root: Callable[..., Any]
    execute_atomic_ac_code: object
    await_dispatch_rate_budget_root: Callable[..., Any]
    await_dispatch_rate_budget_code: object
    dispatch_decomposition_prompt_root: Callable[..., Any]
    dispatch_decomposition_prompt_code: object
    run_atomic_verifier_pass_root: Callable[..., Any]
    run_atomic_verifier_pass_code: object
    run_ac_verify_gate_root: Callable[..., Any]
    run_ac_verify_gate_code: object


# These names document the closed components. ``ParallelACExecutor.__init__``
# receives a value-based default built from them below, so changing a mutable
# module global after import cannot replace the roots it binds.
_FOUNDATION_A_LEAF_DISPATCHER_TYPE = LeafDispatcher
_FOUNDATION_A_LEAF_DISPATCHER_STREAM_ROOT = LeafDispatcher.stream
_FOUNDATION_A_LEAF_DISPATCHER_STREAM_CODE = LeafDispatcher.stream.__code__
_FOUNDATION_A_LEVEL_COORDINATOR_TYPE = LevelCoordinator
_FOUNDATION_A_LEVEL_COORDINATOR_REVIEW_ROOT = LevelCoordinator.run_review
_FOUNDATION_A_LEVEL_COORDINATOR_REVIEW_CODE = LevelCoordinator.run_review.__code__
_FOUNDATION_A_RATE_GATE_FACTORY = build_rate_limit_gate
_FOUNDATION_A_RATE_GATE_TYPE = RateLimitGate
_FOUNDATION_A_RATE_GATE_ACQUIRE_ROOT = RateLimitGate.acquire
_FOUNDATION_A_RATE_GATE_ACQUIRE_CODE = RateLimitGate.acquire.__code__
_FOUNDATION_A_RATE_GATE_SLEEP = asyncio.sleep
_FOUNDATION_A_RATE_GATE_SLEEP_CODE = asyncio.sleep.__code__
_FOUNDATION_A_RATE_GATE_BUCKET_TYPE = SharedRateLimitBucket
_FOUNDATION_A_RATE_GATE_BUCKET_TIME = time.monotonic
_FOUNDATION_A_RATE_GATE_BUCKET_ENABLED_ROOT = SharedRateLimitBucket.enabled.fget
_FOUNDATION_A_RATE_GATE_BUCKET_ENABLED_CODE = (
    _FOUNDATION_A_RATE_GATE_BUCKET_ENABLED_ROOT.__code__
    if _FOUNDATION_A_RATE_GATE_BUCKET_ENABLED_ROOT is not None
    else None
)
_FOUNDATION_A_RATE_GATE_BUCKET_ACQUIRE_ROOT = SharedRateLimitBucket.acquire
_FOUNDATION_A_RATE_GATE_BUCKET_ACQUIRE_CODE = SharedRateLimitBucket.acquire.__code__
_FOUNDATION_A_RATE_GATE_BUCKET_FORCE_RESERVE_ROOT = SharedRateLimitBucket.force_reserve
_FOUNDATION_A_RATE_GATE_BUCKET_FORCE_RESERVE_CODE = SharedRateLimitBucket.force_reserve.__code__
_FOUNDATION_A_RATE_GATE_BUCKET_HELPER_ROOTS = (
    ("_prune", SharedRateLimitBucket._prune, SharedRateLimitBucket._prune.__code__),
    (
        "_tokens_in_window",
        SharedRateLimitBucket._tokens_in_window,
        SharedRateLimitBucket._tokens_in_window.__code__,
    ),
    ("_snapshot", SharedRateLimitBucket._snapshot, SharedRateLimitBucket._snapshot.__code__),
    (
        "_request_wait_seconds",
        SharedRateLimitBucket._request_wait_seconds,
        SharedRateLimitBucket._request_wait_seconds.__code__,
    ),
    (
        "_token_wait_seconds",
        SharedRateLimitBucket._token_wait_seconds,
        SharedRateLimitBucket._token_wait_seconds.__code__,
    ),
)
_FOUNDATION_A_TRANSCRIPT_VERIFIER = _verify_atomic_evidence_against_runtime_messages
_FOUNDATION_A_TRANSCRIPT_VERIFIER_CODE = _verify_atomic_evidence_against_runtime_messages.__code__
_FOUNDATION_A_CLOSED_ROOTS = _FoundationAClosedRoots(
    leaf_dispatcher_type=_FOUNDATION_A_LEAF_DISPATCHER_TYPE,
    leaf_dispatcher_stream_root=_FOUNDATION_A_LEAF_DISPATCHER_STREAM_ROOT,
    leaf_dispatcher_stream_code=_FOUNDATION_A_LEAF_DISPATCHER_STREAM_CODE,
    level_coordinator_type=_FOUNDATION_A_LEVEL_COORDINATOR_TYPE,
    level_coordinator_review_root=_FOUNDATION_A_LEVEL_COORDINATOR_REVIEW_ROOT,
    level_coordinator_review_code=_FOUNDATION_A_LEVEL_COORDINATOR_REVIEW_CODE,
    rate_gate_factory=_FOUNDATION_A_RATE_GATE_FACTORY,
    rate_gate_type=_FOUNDATION_A_RATE_GATE_TYPE,
    rate_gate_acquire_root=_FOUNDATION_A_RATE_GATE_ACQUIRE_ROOT,
    rate_gate_acquire_code=_FOUNDATION_A_RATE_GATE_ACQUIRE_CODE,
    rate_gate_sleep=_FOUNDATION_A_RATE_GATE_SLEEP,
    rate_gate_sleep_code=_FOUNDATION_A_RATE_GATE_SLEEP_CODE,
    rate_gate_bucket_type=_FOUNDATION_A_RATE_GATE_BUCKET_TYPE,
    rate_gate_bucket_time=_FOUNDATION_A_RATE_GATE_BUCKET_TIME,
    rate_gate_bucket_enabled_root=_FOUNDATION_A_RATE_GATE_BUCKET_ENABLED_ROOT,
    rate_gate_bucket_enabled_code=_FOUNDATION_A_RATE_GATE_BUCKET_ENABLED_CODE,
    rate_gate_bucket_acquire_root=_FOUNDATION_A_RATE_GATE_BUCKET_ACQUIRE_ROOT,
    rate_gate_bucket_acquire_code=_FOUNDATION_A_RATE_GATE_BUCKET_ACQUIRE_CODE,
    rate_gate_bucket_force_reserve_root=_FOUNDATION_A_RATE_GATE_BUCKET_FORCE_RESERVE_ROOT,
    rate_gate_bucket_force_reserve_code=_FOUNDATION_A_RATE_GATE_BUCKET_FORCE_RESERVE_CODE,
    rate_gate_bucket_helper_roots=_FOUNDATION_A_RATE_GATE_BUCKET_HELPER_ROOTS,
    transcript_verifier=_FOUNDATION_A_TRANSCRIPT_VERIFIER,
    transcript_verifier_code=_FOUNDATION_A_TRANSCRIPT_VERIFIER_CODE,
)


def _bind_foundation_a_roots(
    roots: _FoundationAClosedRoots,
) -> Callable[[Callable[..., None]], Callable[..., None]]:
    """Bind closed roots in a closure instead of a caller-controlled kwarg."""

    # Extract every root while this decorator is applied at module import. Do
    # not retain the dataclass bundle itself: ``frozen=True`` is not a Python
    # memory-integrity boundary and ``object.__setattr__`` could otherwise
    # poison the bundle before a later executor is constructed.
    leaf_dispatcher_type = roots.leaf_dispatcher_type
    leaf_dispatcher_stream_root = roots.leaf_dispatcher_stream_root
    leaf_dispatcher_stream_code = roots.leaf_dispatcher_stream_code
    level_coordinator_type = roots.level_coordinator_type
    level_coordinator_review_root = roots.level_coordinator_review_root
    level_coordinator_review_code = roots.level_coordinator_review_code
    rate_gate_factory = roots.rate_gate_factory
    rate_gate_type = roots.rate_gate_type
    rate_gate_acquire_root = roots.rate_gate_acquire_root
    rate_gate_acquire_code = roots.rate_gate_acquire_code
    rate_gate_sleep = roots.rate_gate_sleep
    rate_gate_sleep_code = roots.rate_gate_sleep_code
    rate_gate_bucket_type = roots.rate_gate_bucket_type
    rate_gate_bucket_time = roots.rate_gate_bucket_time
    rate_gate_bucket_enabled_root = roots.rate_gate_bucket_enabled_root
    rate_gate_bucket_enabled_code = roots.rate_gate_bucket_enabled_code
    rate_gate_bucket_acquire_root = roots.rate_gate_bucket_acquire_root
    rate_gate_bucket_acquire_code = roots.rate_gate_bucket_acquire_code
    rate_gate_bucket_force_reserve_root = roots.rate_gate_bucket_force_reserve_root
    rate_gate_bucket_force_reserve_code = roots.rate_gate_bucket_force_reserve_code
    rate_gate_bucket_helper_roots = roots.rate_gate_bucket_helper_roots
    transcript_verifier = roots.transcript_verifier
    transcript_verifier_code = roots.transcript_verifier_code

    def decorate(initializer: Callable[..., None]) -> Callable[..., None]:
        @wraps(initializer)
        def bound(self: object, *args: object, **kwargs: object) -> None:
            initializer(
                self,
                *args,
                **kwargs,
                _foundation_a_roots=_FoundationAClosedRoots(
                    leaf_dispatcher_type=leaf_dispatcher_type,
                    leaf_dispatcher_stream_root=leaf_dispatcher_stream_root,
                    leaf_dispatcher_stream_code=leaf_dispatcher_stream_code,
                    level_coordinator_type=level_coordinator_type,
                    level_coordinator_review_root=level_coordinator_review_root,
                    level_coordinator_review_code=level_coordinator_review_code,
                    rate_gate_factory=rate_gate_factory,
                    rate_gate_type=rate_gate_type,
                    rate_gate_acquire_root=rate_gate_acquire_root,
                    rate_gate_acquire_code=rate_gate_acquire_code,
                    rate_gate_sleep=rate_gate_sleep,
                    rate_gate_sleep_code=rate_gate_sleep_code,
                    rate_gate_bucket_type=rate_gate_bucket_type,
                    rate_gate_bucket_time=rate_gate_bucket_time,
                    rate_gate_bucket_enabled_root=rate_gate_bucket_enabled_root,
                    rate_gate_bucket_enabled_code=rate_gate_bucket_enabled_code,
                    rate_gate_bucket_acquire_root=rate_gate_bucket_acquire_root,
                    rate_gate_bucket_acquire_code=rate_gate_bucket_acquire_code,
                    rate_gate_bucket_force_reserve_root=rate_gate_bucket_force_reserve_root,
                    rate_gate_bucket_force_reserve_code=rate_gate_bucket_force_reserve_code,
                    rate_gate_bucket_helper_roots=rate_gate_bucket_helper_roots,
                    transcript_verifier=transcript_verifier,
                    transcript_verifier_code=transcript_verifier_code,
                ),
            )

        return bound

    return decorate


_FOUNDATION_A_ENTRY_EXECUTE_SINGLE_AC = 0
_FOUNDATION_A_ENTRY_EXECUTE_ATOMIC_AC = 1
_FOUNDATION_A_ENTRY_AWAIT_DISPATCH_RATE_BUDGET = 2
_FOUNDATION_A_ENTRY_DISPATCH_DECOMPOSITION_PROMPT = 3
_FOUNDATION_A_ENTRY_RUN_ATOMIC_VERIFIER_PASS = 4
_FOUNDATION_A_ENTRY_RUN_AC_VERIFY_GATE = 5


def _foundation_a_internal_entry_root_specs(
    roots: _FoundationAInternalEntryRoots,
) -> tuple[tuple[str, Callable[..., Any], object], ...]:
    """Return the small, versioned set of internal effect-entry roots."""

    return (
        ("_execute_single_ac", roots.execute_single_ac_root, roots.execute_single_ac_code),
        ("_execute_atomic_ac", roots.execute_atomic_ac_root, roots.execute_atomic_ac_code),
        (
            "_await_dispatch_rate_budget",
            roots.await_dispatch_rate_budget_root,
            roots.await_dispatch_rate_budget_code,
        ),
        (
            "_dispatch_decomposition_prompt",
            roots.dispatch_decomposition_prompt_root,
            roots.dispatch_decomposition_prompt_code,
        ),
        (
            "_run_atomic_verifier_pass",
            roots.run_atomic_verifier_pass_root,
            roots.run_atomic_verifier_pass_code,
        ),
        (
            "_run_ac_verify_gate",
            roots.run_ac_verify_gate_root,
            roots.run_ac_verify_gate_code,
        ),
    )


def _capture_foundation_a_internal_entry_roots(
    executor_type: type[object],
) -> _FoundationAInternalEntryRoots:
    """Capture finite direct entry functions from one concrete executor type."""

    try:
        execute_single_ac_root = type.__getattribute__(executor_type, "_execute_single_ac")
        execute_atomic_ac_root = type.__getattribute__(executor_type, "_execute_atomic_ac")
        await_dispatch_rate_budget_root = type.__getattribute__(
            executor_type,
            "_await_dispatch_rate_budget",
        )
        dispatch_decomposition_prompt_root = type.__getattribute__(
            executor_type,
            "_dispatch_decomposition_prompt",
        )
        run_atomic_verifier_pass_root = type.__getattribute__(
            executor_type,
            "_run_atomic_verifier_pass",
        )
        run_ac_verify_gate_root = type.__getattribute__(executor_type, "_run_ac_verify_gate")
        return _FoundationAInternalEntryRoots(
            executor_type=executor_type,
            execute_single_ac_root=execute_single_ac_root,
            execute_single_ac_code=object.__getattribute__(execute_single_ac_root, "__code__"),
            execute_atomic_ac_root=execute_atomic_ac_root,
            execute_atomic_ac_code=object.__getattribute__(execute_atomic_ac_root, "__code__"),
            await_dispatch_rate_budget_root=await_dispatch_rate_budget_root,
            await_dispatch_rate_budget_code=object.__getattribute__(
                await_dispatch_rate_budget_root,
                "__code__",
            ),
            dispatch_decomposition_prompt_root=dispatch_decomposition_prompt_root,
            dispatch_decomposition_prompt_code=object.__getattribute__(
                dispatch_decomposition_prompt_root,
                "__code__",
            ),
            run_atomic_verifier_pass_root=run_atomic_verifier_pass_root,
            run_atomic_verifier_pass_code=object.__getattribute__(
                run_atomic_verifier_pass_root,
                "__code__",
            ),
            run_ac_verify_gate_root=run_ac_verify_gate_root,
            run_ac_verify_gate_code=object.__getattribute__(run_ac_verify_gate_root, "__code__"),
        )
    except (AttributeError, TypeError) as exc:
        raise ValueError("execution authority internal entry roots are not bindable") from exc


def _foundation_a_internal_entry_roots_are_closed(
    roots: _FoundationAInternalEntryRoots,
    expected_roots: _FoundationAInternalEntryRoots,
) -> bool:
    """Return whether construction found the import-time executor entry set."""

    return roots.executor_type is expected_roots.executor_type and all(
        root is expected_root and code is expected_code
        for (_, root, code), (_, expected_root, expected_code) in zip(
            _foundation_a_internal_entry_root_specs(roots),
            _foundation_a_internal_entry_root_specs(expected_roots),
            strict=True,
        )
    )


def _foundation_a_internal_entry_roots_are_current(
    executor: object,
    roots: _FoundationAInternalEntryRoots,
) -> bool:
    """Check only direct implementation roots, never arbitrary callable state."""

    try:
        if type(executor) is not roots.executor_type:
            return False
        instance_values = object.__getattribute__(executor, "__dict__")
        executor_type = type(executor)
        for name, expected_root, expected_code in _foundation_a_internal_entry_root_specs(roots):
            if name in instance_values:
                return False
            current_root = type.__getattribute__(executor_type, name)
            if current_root is not expected_root:
                return False
            if object.__getattribute__(current_root, "__code__") is not expected_code:
                return False
    except (AttributeError, KeyError, TypeError):
        return False
    return True


def _make_foundation_a_internal_entry_invokers(
    executor: object,
    roots: _FoundationAInternalEntryRoots,
) -> tuple[Callable[..., Any], ...]:
    """Close direct function calls over the constructor's finite root snapshot."""

    executor_ref = ref(executor)
    execute_single_ac_root = roots.execute_single_ac_root
    execute_atomic_ac_root = roots.execute_atomic_ac_root
    await_dispatch_rate_budget_root = roots.await_dispatch_rate_budget_root
    dispatch_decomposition_prompt_root = roots.dispatch_decomposition_prompt_root
    run_atomic_verifier_pass_root = roots.run_atomic_verifier_pass_root
    run_ac_verify_gate_root = roots.run_ac_verify_gate_root

    def _require_captured_executor(current_executor: object) -> None:
        if executor_ref() is not current_executor:
            raise ValueError("execution authority entry root is unavailable")

    def execute_single_ac(current_executor: object, **kwargs: object) -> Any:
        _require_captured_executor(current_executor)
        return execute_single_ac_root(current_executor, **kwargs)

    def execute_atomic_ac(current_executor: object, **kwargs: object) -> Any:
        _require_captured_executor(current_executor)
        return execute_atomic_ac_root(current_executor, **kwargs)

    def await_dispatch_rate_budget(current_executor: object, **kwargs: object) -> Any:
        _require_captured_executor(current_executor)
        return await_dispatch_rate_budget_root(current_executor, **kwargs)

    def dispatch_decomposition_prompt(current_executor: object, **kwargs: object) -> Any:
        _require_captured_executor(current_executor)
        return dispatch_decomposition_prompt_root(current_executor, **kwargs)

    def run_atomic_verifier_pass(current_executor: object, **kwargs: object) -> Any:
        _require_captured_executor(current_executor)
        return run_atomic_verifier_pass_root(current_executor, **kwargs)

    def run_ac_verify_gate(current_executor: object, **kwargs: object) -> Any:
        _require_captured_executor(current_executor)
        return run_ac_verify_gate_root(current_executor, **kwargs)

    return (
        execute_single_ac,
        execute_atomic_ac,
        await_dispatch_rate_budget,
        dispatch_decomposition_prompt,
        run_atomic_verifier_pass,
        run_ac_verify_gate,
    )


def _bind_foundation_a_internal_entry_roots(executor_type: type[Any]) -> type[Any]:
    """Capture original internal entry functions after the class body is complete."""

    expected_roots = _capture_foundation_a_internal_entry_roots(executor_type)
    initializer = executor_type.__init__

    @wraps(initializer)
    def bound(self: object, *args: object, **kwargs: object) -> None:
        if (
            "_foundation_a_internal_entry_roots" in kwargs
            or "_foundation_a_internal_entry_roots_are_closed" in kwargs
        ):
            raise TypeError("_foundation_a_internal_entry_roots is constructor-bound")
        roots = _capture_foundation_a_internal_entry_roots(type(self))
        initializer(
            self,
            *args,
            **kwargs,
            _foundation_a_internal_entry_roots=roots,
            _foundation_a_internal_entry_roots_are_closed=(
                _foundation_a_internal_entry_roots_are_closed(roots, expected_roots)
            ),
        )

    executor_type.__init__ = bound
    return executor_type


def _make_execution_authority_guard(
    executor: object,
    *,
    binding: ExecutionAuthorityLiveBinding,
    workspace_builder: Callable[[], str | None],
    policy_builder: Callable[[], dict[str, object]],
    internal_entry_roots: _FoundationAInternalEntryRoots,
) -> Callable[[object], None]:
    """Capture Foundation A roots outside mutable executor instance fields."""

    executor_ref = ref(executor)
    binding_ref = ref(binding)
    binding_is_intact = ExecutionAuthorityLiveBinding.is_intact
    binding_is_intact_code = binding_is_intact.__code__
    workspace_builder_root = workspace_builder.__func__
    workspace_builder_code = workspace_builder_root.__code__
    policy_builder_root = policy_builder.__func__
    policy_builder_code = policy_builder_root.__code__
    internal_entry_roots_are_current = _foundation_a_internal_entry_roots_are_current
    internal_entry_roots_are_current_code = internal_entry_roots_are_current.__code__

    def guard(current_executor: object) -> None:
        captured_executor = executor_ref()
        captured_binding = binding_ref()
        if captured_executor is not current_executor or captured_binding is None:
            raise ValueError("execution authority guard is unavailable")
        authority_verifier = captured_binding.verifier
        adapter = captured_binding.adapter
        dispatcher_type = captured_binding.dispatcher_type
        dispatcher = captured_binding.dispatcher
        transcript_verifier = captured_binding.transcript_verifier
        rate_gate = captured_binding.rate_gate
        dispatcher_stream_callable = captured_binding.dispatcher_stream_callable
        rate_gate_acquire_callable = captured_binding.rate_gate_acquire_callable
        coordinator = captured_binding.coordinator
        coordinator_review_callable = captured_binding.coordinator_review_callable
        session_signal_hub = captured_binding.session_signal_hub
        get_attribute = object.__getattribute__
        if (
            get_attribute(current_executor, "_execution_authority_live_binding")
            is not captured_binding
            or get_attribute(current_executor, "_authority_verifier") is not authority_verifier
            or get_attribute(current_executor, "_atomic_verifier") is not authority_verifier
            or get_attribute(current_executor, "_adapter") is not adapter
            or get_attribute(current_executor, "_authority_leaf_dispatcher_type")
            is not dispatcher_type
            or get_attribute(current_executor, "_authority_leaf_dispatcher") is not dispatcher
            or get_attribute(current_executor, "_authority_transcript_verifier")
            is not transcript_verifier
            or get_attribute(current_executor, "_dispatch_rate_gate") is not rate_gate
            or get_attribute(current_executor, "_authority_leaf_dispatcher_stream")
            is not dispatcher_stream_callable
            or get_attribute(current_executor, "_authority_rate_gate_acquire_root")
            is not rate_gate_acquire_callable
            or get_attribute(current_executor, "_coordinator") is not coordinator
            or get_attribute(current_executor, "_authority_coordinator_review")
            is not coordinator_review_callable
            or get_attribute(current_executor, "_session_signal_hub") is not session_signal_hub
        ):
            raise ValueError("execution authority drifted before effect")
        if (
            binding_is_intact.__code__ is not binding_is_intact_code
            or workspace_builder_root.__code__ is not workspace_builder_code
            or policy_builder_root.__code__ is not policy_builder_code
            or internal_entry_roots_are_current.__code__
            is not internal_entry_roots_are_current_code
        ):
            raise ValueError("execution authority drifted before effect")
        if not internal_entry_roots_are_current(current_executor, internal_entry_roots):
            raise ValueError("execution authority drifted before effect")
        if not binding_is_intact(
            captured_binding,
            executor=current_executor,
            adapter=adapter,
            verifier=authority_verifier,
            dispatcher_type=dispatcher_type,
            dispatcher=dispatcher,
            dispatcher_executor=current_executor,
            transcript_verifier=transcript_verifier,
            rate_gate=rate_gate,
            workspace=workspace_builder_root(current_executor),
            execution_policy=policy_builder_root(current_executor),
            session_signal_hub=session_signal_hub,
            dispatcher_stream_callable=dispatcher_stream_callable,
            rate_gate_acquire_callable=rate_gate_acquire_callable,
            coordinator=coordinator,
            coordinator_review_callable=coordinator_review_callable,
        ):
            raise ValueError("execution authority drifted before effect")

    return guard


def _make_execution_authority_registry() -> tuple[
    Callable[[object, Callable[[object], None], tuple[Callable[..., Any], ...]], None],
    Callable[[object], None],
    Callable[..., Any],
]:
    """Keep guards and direct invokers in closure-only process state.

    This is an integrity boundary for normal collaborators, not a Python
    sandbox against code that deliberately introspects and mutates private
    module closures in its own process.
    """

    # ``WeakKeyDictionary`` delegates identity to user-defined ``__hash__`` and
    # ``__eq__``. Foundation A deliberately supports process-local executor
    # subclasses, including unhashable and equality-overriding ones, so keep a
    # private identity-keyed table instead. Every lookup verifies the weak
    # referent before use, which also makes delayed cleanup and ``id`` reuse
    # safe. Values retain only the guard/invokers; those retain weak executor
    # references and therefore cannot keep an executor alive.
    states: dict[
        int,
        tuple[
            ref[object],
            Callable[[object], None],
            tuple[Callable[..., Any], ...],
        ],
    ] = {}

    def _lookup(
        executor: object,
    ) -> tuple[Callable[[object], None], tuple[Callable[..., Any], ...]]:
        executor_id = id(executor)
        state = states.get(executor_id)
        if state is None:
            raise ValueError("execution authority guard is unavailable")
        executor_ref, guard, invokers = state
        if executor_ref() is not executor:
            # The stale entry may await a weakref callback, or its integer key
            # may have been reused. Never dispatch through either case.
            if executor_ref() is None:
                states.pop(executor_id, None)
            raise ValueError("execution authority guard is unavailable")
        return guard, invokers

    def register(
        executor: object,
        guard: Callable[[object], None],
        invokers: tuple[Callable[..., Any], ...],
    ) -> None:
        executor_id = id(executor)

        def cleanup(executor_ref: ref[object]) -> None:
            state = states.get(executor_id)
            if state is not None and state[0] is executor_ref:
                states.pop(executor_id, None)

        executor_ref = ref(executor, cleanup)
        states[executor_id] = (executor_ref, guard, invokers)

    def invoke_guard(executor: object) -> None:
        guard, _ = _lookup(executor)
        guard(executor)

    def invoke_entry(executor: object, entry_index: int, **kwargs: object) -> Any:
        try:
            guard, invokers = _lookup(executor)
            invoker = invokers[entry_index]
        except IndexError as exc:
            raise ValueError("execution authority entry root is unavailable") from exc
        guard(executor)
        return invoker(executor, **kwargs)

    return register, invoke_guard, invoke_entry


(
    _register_execution_authority_state,
    _invoke_execution_authority_guard,
    _invoke_execution_authority_entry,
) = _make_execution_authority_registry()


# -- Frugality-proof producer helpers ----------------------------------------
# Token keys the deliver-verdict claim surface may carry a handle under. Mirrors
# the vocabulary traceguard_validator._CHUNK_ID_KEYS accepts, so a leaf-emitted
# structured fact is not misread as "no evidence handle".
_DELIVER_CLAIM_SURFACE_KEYS: tuple[str, ...] = (
    "evidence_claims",
    "observed_facts",
    "retained_facts",
)
_DELIVER_FACT_ID_KEYS: tuple[str, ...] = ("fact_id",)
_DELIVER_EVIDENCE_HANDLE_KEYS: tuple[str, ...] = (
    "evidence_handle",
    "chunk_id",
    "evidence",
    "chunk",
)
_STANDARD_DELIVER_EVIDENCE_FIELDS: tuple[str, ...] = (
    "files_touched",
    "commands_run",
    "tests_passed",
)
_FILE_MUTATION_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})


def _first_nonblank_str(entry: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _structured_deliver_facts(
    typed_evidence: EvidenceRecord | None,
) -> list[DeliverEvidenceFact]:
    """Extract genuinely-present ``(fact_id, evidence_handle)`` claim facts.

    Reads only an EXPLICIT structured claim array the leaf emitted (one of
    :data:`_DELIVER_CLAIM_SURFACE_KEYS`, each item a mapping bearing a non-blank
    ``fact_id`` and a non-blank evidence handle). Returns ``[]`` when the evidence
    carries no such surface — the common non-fat-harness case — so the caller
    SKIPs rather than fabricating facts from prose, which would reward-hack the
    very proof the deliver gate exists to keep honest.
    """
    if typed_evidence is None:
        return []
    data = getattr(typed_evidence, "data", None)
    if not isinstance(data, Mapping):
        return []
    facts: list[DeliverEvidenceFact] = []
    seen: set[str] = set()
    for surface_key in _DELIVER_CLAIM_SURFACE_KEYS:
        entries = data.get(surface_key)
        if not isinstance(entries, (list, tuple)):
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            fact_id = _first_nonblank_str(entry, _DELIVER_FACT_ID_KEYS)
            handle = _first_nonblank_str(entry, _DELIVER_EVIDENCE_HANDLE_KEYS)
            if fact_id is None or handle is None or fact_id in seen:
                continue
            seen.add(fact_id)
            statement = entry.get("statement")
            facts.append(
                DeliverEvidenceFact(
                    fact_id=fact_id,
                    evidence_handle=handle,
                    statement=statement if isinstance(statement, str) else "",
                )
            )
    return facts


def _standard_deliver_facts(
    typed_evidence: EvidenceRecord,
    manifest: EvidenceManifest,
    *,
    task_cwd: str | None,
    verifier_passed: bool,
) -> list[DeliverEvidenceFact] | None:
    """Bind default-profile evidence to exact accepted-leaf tool journal rows.

    ``None`` means the record exposes none of the standard code-profile fields,
    allowing the caller to fall back to an explicit structured claim surface.
    A list (including an empty list) means the standard surface was present and
    therefore takes priority over arbitrary ``observed_facts``.

    Every scalar becomes a fact. Exact one-entry matches receive that journal
    handle; missing or ambiguous matches receive a guaranteed-absent handle so
    TraceGuard emits a deterministic rejection. File paths must be relative and
    contained in ``task_cwd``. ``tests_passed`` additionally requires both a
    harness verifier PASS and exact membership in ``commands_run``.
    """
    data = typed_evidence.data
    if not any(field in data for field in _STANDARD_DELIVER_EVIDENCE_FIELDS):
        return None

    commands = frozenset(_string_evidence_values(data.get("commands_run")))
    facts: list[DeliverEvidenceFact] = []
    seen: set[tuple[str, str]] = set()
    for field in _STANDARD_DELIVER_EVIDENCE_FIELDS:
        raw_values = data.get(field)
        values = _string_evidence_values(raw_values)
        if raw_values is not None and not values:
            values = ("<invalid-or-empty-evidence>",)
        for index, raw_value in enumerate(values):
            normalized = raw_value.strip()
            if field == "files_touched":
                normalized_path = _contained_workspace_relative_path(normalized, task_cwd)
                match_value = normalized_path or normalized
                eligible = normalized_path is not None
            else:
                match_value = normalized
                eligible = bool(normalized) and "\n" not in normalized and "\r" not in normalized
            if field == "tests_passed":
                eligible = eligible and verifier_passed and normalized in commands

            dedupe_key = (field, match_value)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            matches = (
                _matching_journal_entries(
                    manifest, field=field, value=match_value, task_cwd=task_cwd
                )
                if eligible
                else ()
            )
            if len(matches) == 1:
                handle = matches[0].handle
            elif len(matches) > 1:
                handle = f"ambiguous:{field}:{index}"
            else:
                handle = f"missing:{field}:{index}"
            statement_value = _structured_literal(match_value)
            if statement_value is None:
                handle = f"missing:{field}:{index}"
                statement_value = "invalid"
            facts.append(
                DeliverEvidenceFact(
                    fact_id=f"typed:{field}:{index}",
                    evidence_handle=handle,
                    statement=f"typed_evidence {field}={statement_value}",
                )
            )
    return facts


def _string_evidence_values(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _contained_workspace_relative_path(value: str, task_cwd: str | None) -> str | None:
    if not value or task_cwd is None:
        return None
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return None
    try:
        root = Path(task_cwd).expanduser().resolve(strict=False)
        candidate = (root / path).resolve(strict=False)
        normalized = candidate.relative_to(root).as_posix()
    except (OSError, ValueError):
        return None
    return normalized if normalized not in {"", "."} else None


def _matching_journal_entries(
    manifest: EvidenceManifest,
    *,
    field: str,
    value: str,
    task_cwd: str | None = None,
) -> tuple[EvidenceEntry, ...]:
    matches: list[EvidenceEntry] = []
    for entry in manifest.entries:
        if entry.ok is not True or not isinstance(entry.payload, Mapping):
            continue
        payload = entry.payload
        tool_name = payload.get("tool_name")
        if field == "files_touched":
            if tool_name == "Bash" and _journal_entry_proves_command_artifact(
                entry, relative_path=value, task_cwd=task_cwd
            ):
                matches.append(entry)
                continue
            if tool_name not in _FILE_MUTATION_TOOLS:
                continue
            observed = payload.get("workspace_relative_path")
        else:
            if tool_name != "Bash":
                continue
            if field == "tests_passed":
                if not _looks_like_test_command(value):
                    continue
                result_text = _journal_result_text(payload)
                if not _text_proves_test_execution_success(result_text):
                    continue
            observed_commands = _journal_command_values(payload)
            if any(_commands_are_strictly_equivalent(value, item) for item in observed_commands):
                matches.append(entry)
            continue
        if isinstance(observed, str) and observed.strip() == value:
            matches.append(entry)
    return tuple(matches)


_JOURNAL_COMMAND_KEYS: tuple[str, ...] = ("command", "cmd", "command_line")


def _journal_result_text(payload: Mapping[str, object]) -> str:
    """Return runtime-produced result text attached to one journal entry."""
    parts: list[str] = []
    for key in ("result_preview", "output", "stdout", "stderr", "tool_result_text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    tool_result = payload.get("tool_result")
    if isinstance(tool_result, Mapping):
        for key in ("text_content", "content", "output", "stdout", "stderr"):
            value = tool_result.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(dict.fromkeys(parts))


def _journal_command_values(payload: Mapping[str, object]) -> tuple[object, ...]:
    """Extract structured command values from one journal manifest payload.

    Accepted-tool projection keeps the original ``tool_input`` only as bounded
    JSON in ``args_preview`` for adapters that use ``cmd`` / ``command_line`` or
    argv-list values. Preserve those structures until signature generation so
    argument boundaries cannot be flattened into an unsafe string comparison.
    """
    values: list[object] = []

    def append_from(container: Mapping[str, object]) -> None:
        for key in _JOURNAL_COMMAND_KEYS:
            candidate = container.get(key)
            if isinstance(candidate, str) and candidate.strip():
                values.append(candidate.strip())
            elif isinstance(candidate, (list, tuple)) and candidate:
                values.append(tuple(str(part) for part in candidate))

    append_from(payload)
    preview = payload.get("args_preview")
    if isinstance(preview, str) and preview.strip():
        try:
            decoded = json.loads(preview)
        except (json.JSONDecodeError, TypeError, ValueError):
            # Legacy normalizer rows store the raw command directly.
            values.append(preview.strip())
        else:
            if isinstance(decoded, Mapping):
                append_from(decoded)
            elif isinstance(decoded, str) and decoded.strip():
                values.append(decoded.strip())
            else:
                # JSON scalars and unsupported containers may be legacy raw
                # command spellings (for example ``true``). Preserve the
                # preview rather than silently dropping exact evidence.
                values.append(preview.strip())
    if len(values) > 1:
        anchor = values[0]
        if any(
            not _commands_are_strictly_equivalent(anchor, candidate) for candidate in values[1:]
        ):
            return ()
    return tuple(values)


def _commands_are_strictly_equivalent(claim: str, observed: object) -> bool:
    """Compare command evidence using case-sensitive argv signatures.

    Quoting differences that preserve one argv token are equivalent, while case
    changes and flattened token boundaries are not. Safe shell wrappers and
    setup-only preambles may expose their single inner command. Output-only
    plumbing follows the verifier's existing masking guard.
    """
    claim_signatures = set(_strict_command_signatures(claim))
    observed_signatures = set(_strict_command_signatures(observed))
    return bool(claim_signatures.intersection(observed_signatures))


def _strict_command_signatures(command: object) -> tuple[tuple[str, ...], ...]:
    if isinstance(command, (list, tuple)):
        signature = tuple(str(part) for part in command)
        if not signature:
            return ()
        argv_signatures = [("argv", *signature)]
        body = _shell_command_body_from_argv(signature)
        if body is not None:
            argv_signatures.extend(_strict_command_signatures(body))
        return tuple(dict.fromkeys(argv_signatures))
    if not isinstance(command, str) or not command.strip():
        return ()

    raw = command.strip()
    candidates: list[tuple[str, bool]] = [(raw, False)]
    body = _shell_command_body(raw)
    if body is not None:
        candidates.append((body, _output_filter_pipeline_is_pipefail_protected(body)))
        inner = tuple(_segments_after_safe_shell_preamble(body))
        if len(inner) == 1:
            candidates.append((inner[0], _output_filter_pipeline_is_pipefail_protected(body)))

    signatures: list[tuple[str, ...]] = []
    for candidate, pipefail_protected in candidates:
        variants = [candidate.strip()]
        stripped = _strip_command_output_plumbing(candidate)
        if stripped and stripped != candidate.strip():
            unsafe_test_filter = (
                _has_trailing_output_filter_pipeline(candidate)
                and _looks_like_test_command(stripped)
                and not pipefail_protected
            )
            try:
                stripped_parts = shlex.split(stripped)
            except ValueError:
                stripped_parts = []
            if not unsafe_test_filter and stripped_parts:
                variants.append(stripped)
        for variant in variants:
            raw_signature = ("raw", variant)
            if raw_signature not in signatures:
                signatures.append(raw_signature)
            if not _shell_text_is_argv_safe(variant):
                continue
            try:
                signature = ("argv", *shlex.split(variant))
            except ValueError:
                continue
            if len(signature) > 1 and signature not in signatures:
                signatures.append(signature)
    return tuple(signatures)


_SHELL_ACTIVE_UNQUOTED = frozenset("$*?[]><;|&`(){}~#!^\n\r")
_SHELL_ACTIVE_DOUBLE_QUOTED = frozenset("$`")
_SHELL_RESERVED_WORDS = frozenset(
    {
        "case",
        "coproc",
        "do",
        "done",
        "elif",
        "else",
        "esac",
        "fi",
        "for",
        "function",
        "if",
        "in",
        "select",
        "then",
        "time",
        "until",
        "while",
    }
)


def _shell_text_is_argv_safe(command: str) -> bool:
    """Return whether quote removal cannot change this command's shell effects.

    ``shlex.split`` is an argv oracle only when every shell-active token is
    inert. Single quotes make those tokens literal; double quotes still permit
    parameter and command substitution. Unsafe strings retain only their exact
    raw signature, so ``echo $HOME`` cannot equal ``echo '$HOME'`` and quoted
    redirections/globs cannot equal active ones.
    """
    quote: str | None = None
    escaped = False
    for char in command:
        if escaped:
            escaped = False
            continue
        if quote == "'":
            if char == "'":
                quote = None
            continue
        if char == "\\":
            escaped = True
            continue
        if quote == '"':
            if char == '"':
                quote = None
            elif char in _SHELL_ACTIVE_DOUBLE_QUOTED:
                return False
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char in _SHELL_ACTIVE_UNQUOTED:
            return False
    if quote is not None or escaped:
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    return parts[0] not in _SHELL_RESERVED_WORDS and not _is_env_assignment(parts[0])


def _structured_literal(value: str) -> str | None:
    """Quote a scalar for the strict key=value claim-term grammar."""
    if not value or "\n" in value or "\r" in value:
        return None
    for quote in ("`", '"', "'"):
        if quote not in value:
            return f"{quote}{value}{quote}"
    return None


# Decomposition constants
# A bounce at max_decomposition_depth escalates and records the unsplit compromise.
MIN_SUB_ACS = MIN_DECOMPOSITION_CHILDREN
MAX_SUB_ACS = MAX_DECOMPOSITION_CHILDREN
DECOMPOSITION_TIMEOUT_SECONDS = 60.0
_IMPLEMENTATION_SESSION_KIND = "implementation_session"
_WORKSPACE_FINGERPRINT_IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
)
_WORKSPACE_FINGERPRINT_IGNORED_REGULAR_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})
_ROUTE_SUCCESS_CONTEXT_CHARS = 200
_ROUTE_SUCCESS_PUBLIC_API_CHARS = 500
_DURABLE_CONFLICT_PATH_CHARS = 32_768
_COMPOSITE_RESULT_TEXT_CHARS = 4_000
# This replay envelope is derived from the same public live-depth contract used
# by CLI, Seed, runner, and executor admission.  Do not hand-tune it separately.
_COMPOSITE_RESULT_MAX_NODES = MAX_DECOMPOSITION_REPLAY_NODES
_COMPOSITE_RESULT_MAX_DEPTH = 8


def _checkpoint_result_retry_attempts(
    results: list[ACExecutionResult],
) -> dict[str, int]:
    """Persist each result's actual attempt identity, not scheduler counters."""
    return {
        str(result.ac_index): result.retry_attempt
        for result in results
        if isinstance(result.retry_attempt, int)
        and not isinstance(result.retry_attempt, bool)
        and result.retry_attempt >= 0
    }


def _checkpoint_verify_gate_outcomes(
    results: list[ACExecutionResult],
) -> dict[str, dict[str, object]]:
    """Persist all available per-result verify evidence for crash replay."""
    serialized: dict[str, dict[str, object]] = {}
    for result in results:
        outcome = _serialize_verify_gate_outcome(result.verify_gate_outcome)
        if outcome is not None:
            serialized[str(result.ac_index)] = outcome
    return serialized


class _BatchInterruptedForRecoverablePause(RuntimeError):
    """Internal marker for an AC stopped before a shared-quota provider effect."""


class _BatchEnteredAtRecoverablePause(RuntimeError):
    """Internal marker for an AC cancelled after crossing a provider effect."""


def _canonical_result_context(
    result: ACExecutionResult,
    *,
    workspace_root: str,
) -> ACContextSummary:
    """Build the exact bounded projection consumed by downstream levels."""

    if result.context_summary is not None:
        summary = result.context_summary
    else:
        projected = extract_level_context(
            [
                (
                    result.ac_index,
                    result.ac_content,
                    result.success,
                    result.messages,
                    result.final_message,
                )
            ],
            0,
            workspace_root=workspace_root,
        )
        summary = projected.completed_acs[0]
    if (
        summary.ac_index != result.ac_index
        or summary.ac_content != result.ac_content
        or summary.success is not result.success
    ):
        raise RuntimeError("durable context projection contradicts its AC result")
    return summary


def _serialize_context_summary(summary: ACContextSummary) -> dict[str, object]:
    """Serialize one canonical level-context projection with finite bounds."""

    tools = summary.tools_used
    files = summary.files_modified
    if (
        type(summary.ac_index) is not int
        or summary.ac_index < 0
        or not isinstance(summary.ac_content, str)
        or type(summary.success) is not bool
        or not isinstance(tools, tuple)
        or tuple(sorted(set(tools))) != tools
        or any(not isinstance(tool, str) or not tool for tool in tools)
        or not isinstance(files, tuple)
        or tuple(sorted(set(files))) != files
        or any(
            not isinstance(path, str) or not path or len(path) > _DURABLE_CONFLICT_PATH_CHARS
            for path in files
        )
        or not isinstance(summary.key_output, str)
        or len(summary.key_output) > _ROUTE_SUCCESS_CONTEXT_CHARS
        or not isinstance(summary.public_api, str)
        or len(summary.public_api) > _ROUTE_SUCCESS_PUBLIC_API_CHARS
    ):
        raise RuntimeError("canonical route context exceeds its durable bounds")
    return {
        "ac_index": summary.ac_index,
        "ac_content": summary.ac_content,
        "success": summary.success,
        "tools_used": list(tools),
        "files_modified": list(files),
        "key_output": summary.key_output,
        "public_api": summary.public_api,
    }


def _deserialize_context_summary(
    value: object,
    *,
    ac_index: int,
    ac_content: str,
    success: bool,
) -> ACContextSummary:
    """Strictly restore a canonical context projection."""

    expected = frozenset(
        {
            "ac_index",
            "ac_content",
            "success",
            "tools_used",
            "files_modified",
            "key_output",
            "public_api",
        }
    )
    if not _mapping_has_exact_keys(value, expected):
        raise RuntimeError("durable route context has an invalid schema")
    assert isinstance(value, Mapping)
    raw_tools = value.get("tools_used")
    raw_files = value.get("files_modified")
    key_output = value.get("key_output")
    public_api = value.get("public_api")
    if (
        value.get("ac_index") != ac_index
        or type(value.get("ac_index")) is not int
        or value.get("ac_content") != ac_content
        or value.get("success") is not success
        or not isinstance(raw_tools, list)
        or not all(isinstance(tool, str) and bool(tool) for tool in raw_tools)
        or raw_tools != sorted(set(raw_tools))
        or not isinstance(raw_files, list)
        or not all(
            isinstance(path, str) and 0 < len(path) <= _DURABLE_CONFLICT_PATH_CHARS
            for path in raw_files
        )
        or raw_files != sorted(set(raw_files))
        or not isinstance(key_output, str)
        or len(key_output) > _ROUTE_SUCCESS_CONTEXT_CHARS
        or not isinstance(public_api, str)
        or len(public_api) > _ROUTE_SUCCESS_PUBLIC_API_CHARS
    ):
        raise RuntimeError("durable route context is malformed or crossed AC identity")
    return ACContextSummary(
        ac_index=ac_index,
        ac_content=ac_content,
        success=success,
        tools_used=tuple(raw_tools),
        files_modified=tuple(raw_files),
        key_output=key_output,
        public_api=public_api,
    )


def _collect_result_conflict_files(result: ACExecutionResult) -> tuple[str, ...]:
    """Project one result node's exact local file set for recursive replay."""

    if result.conflict_files is not None:
        files = result.conflict_files
    else:
        collected: set[str] = set()
        for message in result.messages:
            if message.tool_name not in {"Write", "Edit"}:
                continue
            tool_input = message.data.get("tool_input")
            if not isinstance(tool_input, Mapping):
                continue
            file_path = tool_input.get("file_path")
            if isinstance(file_path, str) and file_path:
                collected.add(file_path)
        files = tuple(sorted(collected))
    if (
        not isinstance(files, tuple)
        or tuple(sorted(set(files))) != files
        or any(
            not isinstance(path, str) or not path or len(path) > _DURABLE_CONFLICT_PATH_CHARS
            for path in files
        )
    ):
        raise RuntimeError("durable conflict projection exceeds its bounds")
    return files


def _deserialize_conflict_files(value: object) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not all(
            isinstance(path, str) and 0 < len(path) <= _DURABLE_CONFLICT_PATH_CHARS
            for path in value
        )
        or value != sorted(set(value))
    ):
        raise RuntimeError("durable conflict projection is malformed")
    return tuple(value)


def _serialize_provisional_route_success(
    result: ACExecutionResult,
    *,
    workspace_root: str,
) -> dict[str, object]:
    """Persist the canonical context required to settle a resumed success."""

    if (
        not math.isfinite(result.duration_seconds)
        or result.duration_seconds < 0
        or type(result.retry_attempt) is not int
        or result.retry_attempt < 0
        or (result.session_id is not None and not isinstance(result.session_id, str))
    ):
        raise RuntimeError("provisional route success cannot seal malformed result context")
    summary = _canonical_result_context(result, workspace_root=workspace_root)
    return {
        "schema_version": 2,
        "context_summary": _serialize_context_summary(summary),
        "conflict_files": list(_collect_result_conflict_files(result)),
        "duration_seconds": result.duration_seconds,
        "session_id": result.session_id,
        "retry_attempt": result.retry_attempt,
        "verify_gate_outcome": _serialize_verify_gate_outcome(result.verify_gate_outcome),
    }


def _deserialize_provisional_route_success(
    value: object,
    *,
    ac_index: int,
    ac_content: str,
    route_candidate: RouteCandidate,
) -> ACExecutionResult:
    """Restore a sealed provisional success or fail closed on malformed state."""
    expected_keys = frozenset(
        {
            "schema_version",
            "context_summary",
            "conflict_files",
            "duration_seconds",
            "session_id",
            "retry_attempt",
            "verify_gate_outcome",
        }
    )
    if (
        not _mapping_has_exact_keys(value, expected_keys)
        or not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
    ):
        raise RuntimeError("provisional route success has invalid durable context")
    duration_seconds = value.get("duration_seconds")
    session_id = value.get("session_id")
    retry_attempt = value.get("retry_attempt")
    if (
        not isinstance(duration_seconds, int | float)
        or isinstance(duration_seconds, bool)
        or not math.isfinite(duration_seconds)
        or duration_seconds < 0
        or (session_id is not None and not isinstance(session_id, str))
        or type(retry_attempt) is not int
        or retry_attempt < 0
    ):
        raise RuntimeError("provisional route success has malformed bounded context")
    summary = _deserialize_context_summary(
        value.get("context_summary"),
        ac_index=ac_index,
        ac_content=ac_content,
        success=True,
    )
    conflict_files = _deserialize_conflict_files(value.get("conflict_files"))
    raw_verify = value.get("verify_gate_outcome")
    verify_outcome = _deserialize_verify_gate_outcome(raw_verify)
    if raw_verify is not None and verify_outcome is None:
        raise RuntimeError("provisional route success has malformed verify evidence")
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=True,
        final_message=summary.key_output,
        duration_seconds=float(duration_seconds),
        session_id=session_id,
        retry_attempt=retry_attempt,
        outcome=ACExecutionOutcome.SUCCEEDED,
        verify_gate_outcome=verify_outcome,
        context_summary=summary,
        conflict_files=conflict_files,
        route_candidate=route_candidate,
    )


def _canonical_decomposition_decision(
    value: object,
) -> tuple[DecompositionDecisionRecord, dict[str, object], str]:
    """Strictly parse and fingerprint a bounded decomposition decision."""

    parsed = DecompositionDecisionRecord.from_dict(value)
    if parsed is None:
        raise RuntimeError("composite completion has an invalid decomposition decision")
    canonical = parsed.to_dict()
    if value != canonical:
        raise RuntimeError("composite completion has a non-canonical decomposition decision")
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 20_000:
        raise RuntimeError("composite completion decomposition decision exceeds its bound")
    return parsed, canonical, hashlib.sha256(encoded).hexdigest()


def _serialize_composite_result_tree(
    result: ACExecutionResult,
    *,
    node_budget: list[int],
    workspace_root: str,
) -> dict[str, object]:
    """Seal the bounded child-result tree used by reports and depth warnings."""

    node_budget[0] -= 1
    if node_budget[0] < 0:
        raise RuntimeError("composite completion result tree exceeds its node bound")
    if (
        type(result.ac_index) is not int
        or result.ac_index < 0
        or not isinstance(result.ac_content, str)
        or type(result.success) is not bool
        or not math.isfinite(result.duration_seconds)
        or result.duration_seconds < 0
        or type(result.retry_attempt) is not int
        or result.retry_attempt < 0
        or type(result.depth) is not int
        or not 0 <= result.depth <= _COMPOSITE_RESULT_MAX_DEPTH
        or type(result.decomposition_depth_warning) is not bool
        or (result.session_id is not None and not isinstance(result.session_id, str))
        or (result.error is not None and not isinstance(result.error, str))
        or not isinstance(result.final_message, str)
    ):
        raise RuntimeError("composite completion result tree is malformed")
    outcome = result.outcome
    if outcome not in {
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.BLOCKED,
    } or result.success is not (outcome is ACExecutionOutcome.SUCCEEDED):
        raise RuntimeError("composite completion result tree has contradictory semantics")
    if result.is_decomposed is not bool(result.sub_results):
        raise RuntimeError("composite completion result tree lost its child structure")
    decision_data: dict[str, object] | None = None
    if result.decomposition_decision is not None:
        decision, decision_data, _fingerprint = _canonical_decomposition_decision(
            result.decomposition_decision.to_dict()
        )
        if result.is_decomposed and decision.disposition is not DecompositionDisposition.SPLIT:
            raise RuntimeError("composite child result has a non-split decomposition decision")
        if result.is_decomposed and (
            len(decision.children) != len(result.sub_results)
            or tuple(child.description for child in decision.children)
            != tuple(child.ac_content for child in result.sub_results)
        ):
            raise RuntimeError("composite child result drifted from its decomposition decision")
    elif result.is_decomposed:
        raise RuntimeError("composite child result lost its decomposition decision")
    summary = _canonical_result_context(result, workspace_root=workspace_root)
    return {
        "schema_version": 2,
        "ac_index": result.ac_index,
        "ac_content": result.ac_content,
        "success": result.success,
        "final_message_tail": result.final_message[-_COMPOSITE_RESULT_TEXT_CHARS:],
        "error": result.error,
        "duration_seconds": result.duration_seconds,
        "session_id": result.session_id,
        "retry_attempt": result.retry_attempt,
        "is_decomposed": result.is_decomposed,
        "depth": result.depth,
        "decomposition_depth_warning": result.decomposition_depth_warning,
        "outcome": outcome.value,
        "decomposition_decision": decision_data,
        "verify_gate_outcome": _serialize_verify_gate_outcome(result.verify_gate_outcome),
        "context_summary": _serialize_context_summary(summary),
        "conflict_files": list(_collect_result_conflict_files(result)),
        "sub_results": [
            _serialize_composite_result_tree(
                child,
                node_budget=node_budget,
                workspace_root=workspace_root,
            )
            for child in result.sub_results
        ],
    }


def _deserialize_composite_result_tree(
    value: object,
    *,
    node_budget: list[int],
) -> ACExecutionResult:
    """Strictly restore a bounded child-result tree."""

    node_budget[0] -= 1
    if node_budget[0] < 0:
        raise RuntimeError("composite completion result tree exceeds its node bound")
    expected = frozenset(
        {
            "schema_version",
            "ac_index",
            "ac_content",
            "success",
            "final_message_tail",
            "error",
            "duration_seconds",
            "session_id",
            "retry_attempt",
            "is_decomposed",
            "depth",
            "decomposition_depth_warning",
            "outcome",
            "decomposition_decision",
            "verify_gate_outcome",
            "context_summary",
            "conflict_files",
            "sub_results",
        }
    )
    if (
        not _mapping_has_exact_keys(value, expected)
        or not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 2
    ):
        raise RuntimeError("composite completion result tree has an invalid schema")
    ac_index = value.get("ac_index")
    ac_content = value.get("ac_content")
    success = value.get("success")
    final_message = value.get("final_message_tail")
    error = value.get("error")
    duration_seconds = value.get("duration_seconds")
    session_id = value.get("session_id")
    retry_attempt = value.get("retry_attempt")
    is_decomposed = value.get("is_decomposed")
    depth = value.get("depth")
    depth_warning = value.get("decomposition_depth_warning")
    raw_children = value.get("sub_results")
    try:
        outcome = ACExecutionOutcome(value.get("outcome"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("composite completion result tree has an invalid outcome") from exc
    if (
        type(ac_index) is not int
        or ac_index < 0
        or not isinstance(ac_content, str)
        or type(success) is not bool
        or not isinstance(final_message, str)
        or len(final_message) > _COMPOSITE_RESULT_TEXT_CHARS
        or (error is not None and not isinstance(error, str))
        or not isinstance(duration_seconds, int | float)
        or isinstance(duration_seconds, bool)
        or not math.isfinite(duration_seconds)
        or duration_seconds < 0
        or (session_id is not None and not isinstance(session_id, str))
        or type(retry_attempt) is not int
        or retry_attempt < 0
        or type(is_decomposed) is not bool
        or type(depth) is not int
        or not 0 <= depth <= _COMPOSITE_RESULT_MAX_DEPTH
        or type(depth_warning) is not bool
        or not isinstance(raw_children, list)
        or len(raw_children) > _COMPOSITE_RESULT_MAX_NODES
        or outcome
        not in {
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.BLOCKED,
        }
        or success is not (outcome is ACExecutionOutcome.SUCCEEDED)
        or is_decomposed is not bool(raw_children)
    ):
        raise RuntimeError("composite completion result tree is malformed")
    raw_decision = value.get("decomposition_decision")
    decision: DecompositionDecisionRecord | None = None
    if raw_decision is not None:
        decision, _decision_data, _fingerprint = _canonical_decomposition_decision(raw_decision)
        if is_decomposed and decision.disposition is not DecompositionDisposition.SPLIT:
            raise RuntimeError("composite child result has a non-split decomposition decision")
    elif is_decomposed:
        raise RuntimeError("composite child result lost its decomposition decision")
    children = tuple(
        _deserialize_composite_result_tree(child, node_budget=node_budget) for child in raw_children
    )
    if (
        is_decomposed
        and decision is not None
        and (
            len(decision.children) != len(children)
            or tuple(child.description for child in decision.children)
            != tuple(child.ac_content for child in children)
        )
    ):
        raise RuntimeError("composite child result drifted from its decomposition decision")
    summary = _deserialize_context_summary(
        value.get("context_summary"),
        ac_index=ac_index,
        ac_content=ac_content,
        success=success,
    )
    conflict_files = _deserialize_conflict_files(value.get("conflict_files"))
    raw_verify = value.get("verify_gate_outcome")
    verify_outcome = _deserialize_verify_gate_outcome(raw_verify)
    if raw_verify is not None and verify_outcome is None:
        raise RuntimeError("composite completion result tree has malformed verify evidence")
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=success,
        final_message=final_message,
        error=error,
        duration_seconds=float(duration_seconds),
        session_id=session_id,
        retry_attempt=retry_attempt,
        is_decomposed=is_decomposed,
        sub_results=children,
        depth=depth,
        decomposition_depth_warning=depth_warning,
        outcome=outcome,
        decomposition_decision=decision,
        verify_gate_outcome=verify_outcome,
        context_summary=summary,
        conflict_files=conflict_files,
    )


def _serialize_composite_completion_result(
    result: ACExecutionResult,
    *,
    workspace_root: str,
) -> tuple[dict[str, object], dict[str, object], str]:
    """Seal a completed legacy composite without retaining child transcripts."""

    decision = result.decomposition_decision
    if not result.is_decomposed or decision is None:
        raise RuntimeError("composite completion requires its decomposition decision")
    parsed, decision_data, fingerprint = _canonical_decomposition_decision(decision.to_dict())
    if parsed.disposition is not DecompositionDisposition.SPLIT:
        raise RuntimeError("composite completion requires a split decomposition decision")
    if len(parsed.children) != len(result.sub_results) or tuple(
        child.description for child in parsed.children
    ) != tuple(child.ac_content for child in result.sub_results):
        raise RuntimeError("composite completion drifted from its decomposition decision")
    outcome = result.outcome
    if outcome not in {
        ACExecutionOutcome.SUCCEEDED,
        ACExecutionOutcome.FAILED,
        ACExecutionOutcome.BLOCKED,
    }:
        raise RuntimeError("composite completion has an invalid terminal outcome")
    if result.success is not (outcome is ACExecutionOutcome.SUCCEEDED):
        raise RuntimeError("composite completion has contradictory success semantics")
    if (
        not math.isfinite(result.duration_seconds)
        or result.duration_seconds < 0
        or type(result.retry_attempt) is not int
        or result.retry_attempt < 0
        or (result.session_id is not None and not isinstance(result.session_id, str))
        or (result.error is not None and not isinstance(result.error, str))
    ):
        raise RuntimeError("composite completion cannot seal malformed result context")
    summary = _canonical_result_context(result, workspace_root=workspace_root)
    node_budget = [_COMPOSITE_RESULT_MAX_NODES]
    sub_results = [
        _serialize_composite_result_tree(
            child,
            node_budget=node_budget,
            workspace_root=workspace_root,
        )
        for child in result.sub_results
    ]
    return (
        {
            "schema_version": 1,
            "success": result.success,
            "outcome": outcome.value,
            "error": result.error,
            "duration_seconds": result.duration_seconds,
            "session_id": result.session_id,
            "retry_attempt": result.retry_attempt,
            "verify_gate_outcome": _serialize_verify_gate_outcome(result.verify_gate_outcome),
            "context_summary": _serialize_context_summary(summary),
            "conflict_files": list(_collect_result_conflict_files(result)),
            "sub_results": sub_results,
        },
        decision_data,
        fingerprint,
    )


def _deserialize_composite_completion_result(
    value: object,
    *,
    ac_index: int,
    ac_content: str,
    decomposition_decision: DecompositionDecisionRecord,
) -> ACExecutionResult:
    """Restore a terminal composite projection without replaying child effects."""

    expected = frozenset(
        {
            "schema_version",
            "success",
            "outcome",
            "error",
            "duration_seconds",
            "session_id",
            "retry_attempt",
            "verify_gate_outcome",
            "context_summary",
            "conflict_files",
            "sub_results",
        }
    )
    if (
        not _mapping_has_exact_keys(value, expected)
        or not isinstance(value, Mapping)
        or type(value.get("schema_version")) is not int
        or value.get("schema_version") != 1
    ):
        raise RuntimeError("composite completion has an invalid result schema")
    success = value.get("success")
    raw_outcome = value.get("outcome")
    error = value.get("error")
    duration_seconds = value.get("duration_seconds")
    session_id = value.get("session_id")
    retry_attempt = value.get("retry_attempt")
    try:
        outcome = ACExecutionOutcome(raw_outcome)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("composite completion has an invalid outcome") from exc
    if (
        type(success) is not bool
        or outcome
        not in {
            ACExecutionOutcome.SUCCEEDED,
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.BLOCKED,
        }
        or success is not (outcome is ACExecutionOutcome.SUCCEEDED)
        or (error is not None and not isinstance(error, str))
        or not isinstance(duration_seconds, int | float)
        or isinstance(duration_seconds, bool)
        or not math.isfinite(duration_seconds)
        or duration_seconds < 0
        or (session_id is not None and not isinstance(session_id, str))
        or type(retry_attempt) is not int
        or retry_attempt < 0
    ):
        raise RuntimeError("composite completion has malformed result semantics")
    summary = _deserialize_context_summary(
        value.get("context_summary"),
        ac_index=ac_index,
        ac_content=ac_content,
        success=success,
    )
    conflict_files = _deserialize_conflict_files(value.get("conflict_files"))
    raw_sub_results = value.get("sub_results")
    if not isinstance(raw_sub_results, list) or not raw_sub_results:
        raise RuntimeError("composite completion lost its child result projection")
    node_budget = [_COMPOSITE_RESULT_MAX_NODES]
    sub_results = tuple(
        _deserialize_composite_result_tree(child, node_budget=node_budget)
        for child in raw_sub_results
    )
    if len(decomposition_decision.children) != len(sub_results) or tuple(
        child.description for child in decomposition_decision.children
    ) != tuple(child.ac_content for child in sub_results):
        raise RuntimeError("composite completion drifted from its decomposition decision")
    raw_verify = value.get("verify_gate_outcome")
    verify_outcome = _deserialize_verify_gate_outcome(raw_verify)
    if raw_verify is not None and verify_outcome is None:
        raise RuntimeError("composite completion has malformed verify evidence")
    return ACExecutionResult(
        ac_index=ac_index,
        ac_content=ac_content,
        success=success,
        final_message=summary.key_output,
        error=error,
        duration_seconds=float(duration_seconds),
        session_id=session_id,
        retry_attempt=retry_attempt,
        is_decomposed=True,
        sub_results=sub_results,
        outcome=outcome,
        verify_gate_outcome=verify_outcome,
        decomposition_decision=decomposition_decision,
        context_summary=summary,
        conflict_files=conflict_files,
    )


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
    if parallel_result.blocked_count > 0:
        lines.append(f"Blocked: {parallel_result.blocked_count}")
    if parallel_result.invalid_count > 0:
        lines.append(f"Invalid: {parallel_result.invalid_count}")
    unverifiable_indices = [
        result.ac_index + 1
        for result in parallel_result.results
        if (
            isinstance(result.verify_gate_outcome, _VerifyGateOutcome)
            and result.verify_gate_outcome.environment_unverifiable
        )
        or (
            result.atomic_verifier_verdict is not None
            and result.atomic_verifier_verdict.failure_class == "TRANSCRIPT_MISSING_INFRASTRUCTURE"
        )
    ]
    if unverifiable_indices:
        lines.append(render_unverifiable_summary(unverifiable_indices))

    warning_paths: list[str] = []
    for user_facing_idx, result in enumerate(parallel_result.results, start=1):
        warning_paths.extend(
            collect_decomposition_depth_warning_paths(
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
    if parallel_result.blocked_count > 0:
        lines.append(f"Blocked: {parallel_result.blocked_count}")
    if parallel_result.invalid_count > 0:
        lines.append(f"Invalid: {parallel_result.invalid_count}")

    lines.append("")
    lines.append("Task Status:")
    for result in parallel_result.results:
        if result.outcome == ACExecutionOutcome.SATISFIED_EXTERNALLY:
            status = "COMPLETED"
            suffix = " (externally satisfied)"
        elif result.outcome == ACExecutionOutcome.BLOCKED:
            status = "BLOCKED"
            suffix = f" — {result.error}" if result.error else ""
        elif result.outcome == ACExecutionOutcome.INVALID:
            status = "INVALID"
            suffix = f" — {result.error}" if result.error else ""
        else:
            status = "COMPLETED" if result.success else "FAILED"
            suffix = f" ({len(result.sub_results)} subtasks)" if result.is_decomposed else ""
            if not result.success and result.error:
                suffix += f" — {result.error}"
        lines.append(f"- Task {result.ac_index + 1}: [{status}] {result.ac_content}{suffix}")
    return "\n".join(lines)


# =============================================================================
# Parallel Executor
# =============================================================================


@_bind_foundation_a_internal_entry_roots
class ParallelACExecutor:
    """Executes ACs in parallel based on dependency graph."""

    @_bind_foundation_a_roots(_FOUNDATION_A_CLOSED_ROOTS)
    def __init__(
        self,
        adapter: AgentRuntime,
        event_store: EventStore,
        console: Console | None = None,
        enable_decomposition: bool = True,
        decomposition_mode: Literal["bounce_only", "off"] = "bounce_only",
        max_concurrent: int = 3,
        adaptive_max_concurrent: int | None = None,
        max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
        checkpoint_store: Any | None = None,
        inherited_runtime_handle: RuntimeHandle | None = None,
        task_cwd: str | None = None,
        execution_profile: ExecutionProfile | None = None,
        fat_harness_mode: bool = False,
        atomic_verifier: Verifier | None = None,
        reasoning_effort: str | None = None,
        model_router: ModelRouter | None = None,
        route_economics: Any | None = None,
        run_verify_commands: bool = True,
        verify_command_timeout_seconds: int = 600,
        verify_shell_identity: Mapping[str, object] | None | _AutoVerifyShell = _AUTO_VERIFY_SHELL,
        ac_retry_attempts: int = 0,
        cross_harness_redispatch: bool | None = None,
        shadow_replay_enabled: bool = False,
        session_signal_hub: SessionSignalHub | None = None,
        process_local_resume_nonce: str | None = None,
        resolved_backend_limits: BackendConcurrencyLimits | None = None,
        resolved_self_governs_rate_limit: bool | None = None,
        expected_runtime_effect_capabilities: Mapping[str, object] | None = None,
        usage_limit_pause_seconds: int | None = None,
        _foundation_a_roots: _FoundationAClosedRoots = _FOUNDATION_A_CLOSED_ROOTS,
        _foundation_a_internal_entry_roots: _FoundationAInternalEntryRoots | None = None,
        _foundation_a_internal_entry_roots_are_closed: bool = False,
    ):
        """Initialize executor.

        Args:
            adapter: Agent runtime for execution.
            event_store: Event store for progress tracking.
            console: Rich console for output.
            enable_decomposition: Enable Claude to decompose complex ACs.
            decomposition_mode: Whether decomposition runs only after a
                classified bounce or not at all. Historical ``preflight``
                records remain readable, but no live constructor can authorize
                a new pre-execution decomposition effect.
            max_concurrent: Initial number of concurrent AC executions.
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
            verify_shell_identity: Canonical Bash path and content digest sealed
                by the runner. ``None`` keeps verification unavailable for this
                execution; the private auto sentinel is only for low-level tests.
            ac_retry_attempts: How many times a failed AC is re-dispatched
                before it is marked FAILED (excludes stall retries). The
                low-level constructor default is 0 so direct/test callers keep
                today's single-dispatch behavior; real run paths (CLI `ooo run`
                via the runner) pass the config value (default 2).
            route_economics: Optional economics snapshot used to project live
                model/effort decisions into the Routing B Admission Kernel. The
                bridge stays dormant for low-level callers that omit it.
            resolved_backend_limits: Optional immutable fan-out/rate snapshot.
                Runner-owned execution passes its durable contract value so a
                resume never rereads changed backend-limit configuration.
            resolved_self_governs_rate_limit: Optional immutable adapter pacing
                mode paired with ``resolved_backend_limits``.
            expected_runtime_effect_capabilities: Complete durable runtime
                declaration that must still match at every provider entry.
        """
        if _foundation_a_internal_entry_roots is None:
            raise ValueError("execution authority internal entry roots are unavailable")
        internal_entry_roots = _foundation_a_internal_entry_roots

        self._adapter = adapter
        if expected_runtime_effect_capabilities is not None:
            if not valid_runtime_effect_capabilities_contract(expected_runtime_effect_capabilities):
                raise ValueError("invalid expected runtime effect capabilities")
            if runtime_effect_capabilities_contract(adapter) != dict(
                expected_runtime_effect_capabilities
            ):
                raise ValueError("runtime effect capabilities drifted before executor creation")
        self._expected_runtime_effect_capabilities = (
            deepcopy(dict(expected_runtime_effect_capabilities))
            if expected_runtime_effect_capabilities is not None
            else None
        )
        self._event_store = event_store
        self._console = console or Console()
        self._usage_limit_pause_seconds = resolve_usage_limit_pause_seconds(
            usage_limit_pause_seconds
        )
        if decomposition_mode not in {"bounce_only", "off"}:
            msg = f"Unsupported decomposition_mode: {decomposition_mode!r}"
            raise ValueError(msg)
        self._decomposition_mode: Literal["bounce_only", "off"] = (
            "off" if not enable_decomposition else decomposition_mode
        )
        self._enable_decomposition = self._decomposition_mode != "off"
        self._max_decomposition_depth = validate_max_decomposition_depth(max_decomposition_depth)
        self._durable_decomposition_replay_enabled = has_durable_decomposition_replay(
            self._max_decomposition_depth
        )
        self._max_concurrent = max_concurrent
        approval_mode = getattr(adapter, "permission_mode", None)
        self._inherited_runtime_handle = (
            replace(inherited_runtime_handle, approval_mode=approval_mode.strip())
            if inherited_runtime_handle is not None
            and isinstance(approval_mode, str)
            and approval_mode.strip()
            else inherited_runtime_handle
        )
        self._task_cwd = resolve_worker_cwd(task_cwd) if task_cwd else None
        self._execution_profile = execution_profile
        self._fat_harness_mode = fat_harness_mode
        self._run_verify_commands = run_verify_commands
        self._verify_command_timeout_seconds = max(1, verify_command_timeout_seconds)
        if isinstance(verify_shell_identity, _AutoVerifyShell):
            verify_shell = resolve_verify_shell()
            self._verify_shell_identity = (
                capture_verify_shell_identity(verify_shell) if verify_shell is not None else None
            )
        elif verify_shell_identity is None:
            self._verify_shell_identity = None
        elif isinstance(verify_shell_identity, Mapping):
            self._verify_shell_identity = deepcopy(dict(verify_shell_identity))
        else:
            raise ValueError("verify_shell_identity must be a mapping or None")
        self._ac_retry_attempts = max(0, ac_retry_attempts)
        # Effort-first investment dial (RFC #1405). AC investment metadata may
        # impose a floor or authorize one lower notch; decomposition alone never
        # lowers effort. ``None`` leaves effort routing dormant.
        self._reasoning_effort = reasoning_effort
        # Model-tier investment dial (the frugality sibling of reasoning_effort).
        # The router maps a per-unit tier decision to a backend-executable model id;
        # ``None`` leaves model routing dormant (execute_task receives no model
        # override → byte-identical to today's behavior), so laying the executor on
        # the model capability contract is safe by default.
        self._model_router = model_router
        # Routing B compatibility is explicit at this constructor seam. The live
        # runner supplies the resolved economics; direct/test callers retain the
        # historical dispatch path until they opt into the bridge.
        self._route_economics = route_economics
        self._bounded_route_escalation_enabled = (
            self._durable_decomposition_replay_enabled
            and route_economics is not None
            and model_router is not None
            and getattr(
                getattr(adapter, "capabilities", None),
                "model_override_support",
                ParamSupport.IGNORED,
            )
            is ParamSupport.NATIVE
        )
        # Opt-in shadow-replay baseline harness (frugality-proof AC5). Default OFF:
        # replaying a decomposed child at the parent tier doubles token cost, so
        # this is an experiment lever, never a production default. When on, a
        # successful decomposed child is re-executed in an isolated workspace to
        # measure its parent-tier baseline spend. See ``shadow_replay`` module.
        self._shadow_replay_enabled = shadow_replay_enabled
        self._session_signal_hub = session_signal_hub
        self._atomic_verifier = atomic_verifier
        # These exact objects are the finite effect-owner roots Foundation A
        # can bind at runtime. Callable internals remain explicitly volatile;
        # custom verifiers never become portable through graph inspection.
        self._authority_verifier = atomic_verifier
        self._authority_leaf_dispatcher_type = _foundation_a_roots.leaf_dispatcher_type
        # Construct the leaf once through closed primitive roots, then call the
        # captured stream implementation directly. A later class ``__init__``
        # or instance ``stream`` hook cannot replace the immediate dispatcher
        # effect after the authority check.
        self._authority_leaf_dispatcher = object.__new__(self._authority_leaf_dispatcher_type)
        object.__setattr__(self._authority_leaf_dispatcher, "_executor", self)
        self._authority_leaf_dispatcher_stream = _foundation_a_roots.leaf_dispatcher_stream_root
        self._authority_transcript_verifier = _foundation_a_roots.transcript_verifier
        self._coordinator = _foundation_a_roots.level_coordinator_type(
            adapter,
            inherited_runtime_handle=self._inherited_runtime_handle,
            task_cwd=self._task_cwd,
            reasoning_effort=self._reasoning_effort,
        )
        self._authority_coordinator = self._coordinator
        self._authority_coordinator_review = self._coordinator.run_review
        self._adaptive_concurrency = adaptive_concurrency.AdaptiveConcurrencyController(
            initial_limit=max_concurrent,
            max_limit=adaptive_max_concurrent,
        )
        if process_local_resume_nonce is not None and (
            len(process_local_resume_nonce) != 32
            or any(char not in "0123456789abcdef" for char in process_local_resume_nonce)
        ):
            raise ValueError("process-local resume nonce must be 32 lowercase hex characters")
        self._process_local_resume_nonce = process_local_resume_nonce or uuid4().hex
        self._ac_runtime_handle_manager = ACRuntimeHandleManager(
            adapter,
            event_store,
            task_cwd=self._task_cwd,
            process_local_resume_nonce=self._process_local_resume_nonce,
        )
        self._ac_runtime_handles = self._ac_runtime_handle_manager.runtime_handles
        self._event_emitter = ExecutionEventEmitter(
            event_store,
            safe_emit_event=self._safe_emit_event,
        )
        self._checkpoint_store = checkpoint_store
        self._decomposition_decisions: dict[str, DecompositionDecisionRecord] = {}
        self._event_owned_decomposition_decisions: dict[str, DecompositionDecisionRecord] = {}
        self._pending_bounce_decompositions: dict[str, _DurableBounceReplayState] = {}
        self._partial_composite_resumes: dict[str, _PartialCompositeResumeState] = {}
        self._parallel_route_resumes: dict[int, _ParallelRouteResumeState] = {}
        self._execution_counters_lock = asyncio.Lock()
        self._resolved_backend_limits = resolved_backend_limits or resolve_backend_limits(
            getattr(adapter, "runtime_backend", None)
        )
        self._resolved_self_governs_rate_limit = (
            bool(getattr(adapter, "self_governs_rate_limit", False))
            if resolved_self_governs_rate_limit is None
            else resolved_self_governs_rate_limit
        )
        self._dispatch_rate_gate = self._build_dispatch_rate_gate(
            adapter,
            limits=self._resolved_backend_limits,
            self_governs_rate_limit=self._resolved_self_governs_rate_limit,
            rate_gate_factory=_foundation_a_roots.rate_gate_factory,
        )
        self._authority_rate_gate_acquire_root = _foundation_a_roots.rate_gate_acquire_root
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
        self._alt_harness_status_by_root: dict[int, str] = {}
        self._recovery_exhausted_emitted: set[tuple[str, int]] = set()

        workspace_builder = object.__getattribute__(
            self,
            "_execution_authority_workspace",
        )
        policy_builder = object.__getattribute__(
            self,
            "_execution_authority_policy",
        )
        binding = ExecutionAuthorityLiveBinding.capture(
            executor=self,
            adapter=adapter,
            verifier=atomic_verifier,
            dispatcher_type=self._authority_leaf_dispatcher_type,
            dispatcher=self._authority_leaf_dispatcher,
            dispatcher_executor=self,
            transcript_verifier=self._authority_transcript_verifier,
            rate_gate=self._dispatch_rate_gate,
            workspace=workspace_builder(),
            execution_policy=policy_builder(),
            session_signal_hub=self._session_signal_hub,
            dispatcher_stream_callable=self._authority_leaf_dispatcher_stream,
            rate_gate_acquire_callable=self._authority_rate_gate_acquire_root,
            coordinator=self._authority_coordinator,
            coordinator_review_callable=self._authority_coordinator_review,
            expected_dispatcher_type=_foundation_a_roots.leaf_dispatcher_type,
            expected_dispatcher_stream_root=_foundation_a_roots.leaf_dispatcher_stream_root,
            expected_dispatcher_stream_code=_foundation_a_roots.leaf_dispatcher_stream_code,
            expected_transcript_verifier=_foundation_a_roots.transcript_verifier,
            expected_transcript_verifier_code=_foundation_a_roots.transcript_verifier_code,
            expected_rate_gate_acquire_root=_foundation_a_roots.rate_gate_acquire_root,
            expected_rate_gate_acquire_code=_foundation_a_roots.rate_gate_acquire_code,
            expected_rate_gate_type=_foundation_a_roots.rate_gate_type,
            expected_rate_gate_sleep=_foundation_a_roots.rate_gate_sleep,
            expected_rate_gate_sleep_code=_foundation_a_roots.rate_gate_sleep_code,
            expected_rate_gate_bucket_type=_foundation_a_roots.rate_gate_bucket_type,
            expected_rate_gate_bucket_time=_foundation_a_roots.rate_gate_bucket_time,
            expected_rate_gate_bucket_enabled_root=(
                _foundation_a_roots.rate_gate_bucket_enabled_root
            ),
            expected_rate_gate_bucket_enabled_code=(
                _foundation_a_roots.rate_gate_bucket_enabled_code
            ),
            expected_rate_gate_bucket_acquire_root=(
                _foundation_a_roots.rate_gate_bucket_acquire_root
            ),
            expected_rate_gate_bucket_acquire_code=(
                _foundation_a_roots.rate_gate_bucket_acquire_code
            ),
            expected_rate_gate_bucket_force_reserve_root=(
                _foundation_a_roots.rate_gate_bucket_force_reserve_root
            ),
            expected_rate_gate_bucket_force_reserve_code=(
                _foundation_a_roots.rate_gate_bucket_force_reserve_code
            ),
            expected_rate_gate_bucket_helper_roots=(
                _foundation_a_roots.rate_gate_bucket_helper_roots
            ),
            expected_coordinator_type=_foundation_a_roots.level_coordinator_type,
            expected_coordinator_review_root=_foundation_a_roots.level_coordinator_review_root,
            expected_coordinator_review_code=_foundation_a_roots.level_coordinator_review_code,
            force_runtime_process_local=(
                not _foundation_a_internal_entry_roots_are_closed
                or self._session_signal_hub is not None
            ),
            runtime_instance_nonce=self._process_local_resume_nonce,
        )
        self._execution_authority_live_binding = binding
        self._execution_authority = binding.contract
        internal_entry_invokers = _make_foundation_a_internal_entry_invokers(
            self,
            internal_entry_roots,
        )
        _register_execution_authority_state(
            self,
            _make_execution_authority_guard(
                self,
                binding=binding,
                workspace_builder=workspace_builder,
                policy_builder=policy_builder,
                internal_entry_roots=internal_entry_roots,
            ),
            internal_entry_invokers,
        )

    def _profile_suggested_tier(self) -> str | None:
        """Return the profile's explicit model-tier floor, if it has one."""

        if (
            self._execution_profile is None
            or self._execution_profile.suggested_model_tier is SuggestedModelTier.MEDIUM
        ):
            return None
        return tier_from_profile_hint(self._execution_profile.suggested_model_tier.value)

    def _build_route_compat_projection(
        self,
        *,
        model_router: ModelRouter | None,
        effort: str | None,
    ) -> RouteCompatProjection | None:
        """Build one projection with every public starting-tier floor applied."""

        projection = build_route_compat_projection(
            self._route_economics,
            model_router=model_router,
            runtime_backend=getattr(self._adapter, "runtime_backend", None),
            effort=effort,
        )
        suggested_tier = self._profile_suggested_tier()
        if projection is None or suggested_tier is None:
            return projection
        effective_floor = MODEL_TIER_LADDER[
            max(
                MODEL_TIER_LADDER.index(projection.base_tier),
                MODEL_TIER_LADDER.index(suggested_tier),
            )
        ]
        return replace(projection, base_tier=effective_floor)

    @property
    def execution_authority(self) -> ExecutionAuthorityContract:
        """Return the immutable authority snapshot used by this executor."""
        return self._execution_authority

    def _execution_authority_workspace(self) -> str | None:
        """Return the finite effect-workspace root for Foundation A."""
        get_attribute = object.__getattribute__
        task_cwd = get_attribute(self, "_task_cwd")
        adapter = get_attribute(self, "_adapter")
        workspace = task_cwd or getattr(adapter, "working_directory", None)
        return workspace if isinstance(workspace, str) else None

    def _execution_authority_policy(self) -> dict[str, object]:
        """Return only static executor policy; attempt inputs stay excluded."""
        get_attribute = object.__getattribute__
        adapter = get_attribute(self, "_adapter")
        coordinator = get_attribute(self, "_coordinator")
        backend = getattr(adapter, "runtime_backend", None)
        limits = get_attribute(self, "_resolved_backend_limits")
        coordinator_effort = getattr(coordinator, "_reasoning_effort", None)
        return {
            "version": 2,
            "decomposition_mode": get_attribute(self, "_decomposition_mode"),
            "max_decomposition_depth": get_attribute(self, "_max_decomposition_depth"),
            "max_concurrent": get_attribute(self, "_max_concurrent"),
            "adaptive_concurrency": get_attribute(self, "_adaptive_concurrency").policy,
            "execution_profile": (
                get_attribute(self, "_execution_profile").model_dump(mode="json")
                if get_attribute(self, "_execution_profile") is not None
                else None
            ),
            "fat_harness_mode": get_attribute(self, "_fat_harness_mode"),
            "run_verify_commands": get_attribute(self, "_run_verify_commands"),
            "verify_command_timeout_seconds": get_attribute(
                self,
                "_verify_command_timeout_seconds",
            ),
            "ac_retry_attempts": get_attribute(self, "_ac_retry_attempts"),
            "reasoning_effort": get_attribute(self, "_reasoning_effort"),
            "coordinator_reasoning_effort": coordinator_effort,
            "model_routing": serialize_model_router(get_attribute(self, "_model_router")),
            "cross_harness_redispatch": get_attribute(
                self,
                "_cross_harness_redispatch_enabled",
            ),
            "shadow_replay_enabled": get_attribute(self, "_shadow_replay_enabled"),
            "session_signal_hub_enabled": get_attribute(self, "_session_signal_hub") is not None,
            "dispatch_rate": {
                "algorithm": "rate-limit-gate/v1",
                "backend": backend if isinstance(backend, str) else None,
                "self_governs_rate_limit": get_attribute(
                    self,
                    "_resolved_self_governs_rate_limit",
                ),
                "requests_per_minute": limits.requests_per_minute,
                "tokens_per_minute": limits.tokens_per_minute,
            },
        }

    def _require_execution_authority_intact(self) -> None:
        """Fail closed when an enumerated live effect-owner root drifts."""
        _invoke_execution_authority_guard(self)

    @staticmethod
    def _build_dispatch_rate_gate(
        adapter: AgentRuntime,
        *,
        limits: BackendConcurrencyLimits | None = None,
        self_governs_rate_limit: bool | None = None,
        rate_gate_factory: Callable[
            ..., RateLimitGate
        ] = _FOUNDATION_A_CLOSED_ROOTS.rate_gate_factory,
    ) -> RateLimitGate:
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

        if self_governs_rate_limit is None:
            self_governs_rate_limit = bool(getattr(adapter, "self_governs_rate_limit", False))
        if self_governs_rate_limit:
            return rate_gate_factory(
                backend,
                request_limit=None,
                token_limit=None,
            )

        limits = limits or resolve_backend_limits(backend)
        return rate_gate_factory(
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
        _invoke_execution_authority_guard(self)
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

        await self._authority_rate_gate_acquire_root(
            self._dispatch_rate_gate,
            estimated_tokens,
            on_backoff=_log_backoff,
        )
        # Rate admission can suspend. Revalidate before returning to a caller
        # that will dispatch to the runtime immediately afterwards.
        _invoke_execution_authority_guard(self)

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
        expected_capsule_fingerprint: str | None = None,
        expected_process_local_resume_nonce: str | None = None,
    ) -> RuntimeHandle | None:
        return await self._ac_runtime_handle_manager._load_persisted_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            expected_capsule_fingerprint=expected_capsule_fingerprint,
            expected_process_local_resume_nonce=expected_process_local_resume_nonce,
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
        orchestrator_session_id: str | None = None,
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
            orchestrator_session_id=orchestrator_session_id,
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
        route_overrides: dict[int, RouteCandidate] | None = None,
        route_resume_states: Mapping[int, _ParallelRouteResumeState] | None = None,
        batch_sibling_indices: list[int] | None = None,
        same_runtime_budget_exhausted: bool = True,
        force_legacy_routing: bool = False,
    ) -> list[ACExecutionResult | BaseException]:
        """Execute one batch of stage-ready ACs using the shared worker pool.

        ``same_runtime_budget_exhausted`` is forwarded to every AC in the batch:
        it is ``True`` only on the batch attempt that spends the AC's configured
        same-runtime retry budget, gating cross-harness redispatch (PR-X X1) so
        it never pre-empts those retries.
        """
        batch_results: list[ACExecutionResult | BaseException | None] = [None] * len(batch_indices)
        cancel_on_recoverable_pause = bool(
            self._bounded_route_escalation_enabled and not force_legacy_routing
        )
        recoverable_pause_detected = anyio.Event()
        sibling_cancel_scopes: dict[int, anyio.CancelScope] = {}
        admission_sequence = provider_admission.AdmissionSequence(len(batch_indices))
        provider_effects = provider_effect_scope.BatchProviderEffects(
            recoverable_pause_detected,
            admission_sequence,
            _BatchInterruptedForRecoverablePause,
        )
        sibling_indices = batch_sibling_indices or batch_indices
        sibling_acs: list[_SiblingACRef] = (
            [(i, ac_text(seed.acceptance_criteria[i])) for i in sibling_indices]
            if len(sibling_indices) > 1
            else []
        )

        async def _run_ac(idx: int, ac_idx: int) -> None:
            def _observe_batch_provider(
                observation: adaptive_concurrency.ConcurrencyObservation,
            ) -> None:
                if (
                    not cancel_on_recoverable_pause
                    or observation.kind
                    is not adaptive_concurrency.BackendPressureKind.QUOTA_EXHAUSTION
                ):
                    return
                recoverable_pause_detected.set()
                for sibling_idx, scope in tuple(sibling_cancel_scopes.items()):
                    if sibling_idx != idx and provider_effects.should_cancel(sibling_idx):
                        scope.cancel()

            admission_token = admission_sequence.bind(idx)
            observation_sink_token = _PROVIDER_OBSERVATION_SINK.set(
                _observe_batch_provider if cancel_on_recoverable_pause else None
            )
            provider_effect_tokens = provider_effects.bind(idx)
            with anyio.CancelScope() as sibling_scope:
                sibling_cancel_scopes[idx] = sibling_scope
                try:
                    if cancel_on_recoverable_pause and recoverable_pause_detected.is_set():
                        batch_results[idx] = _BatchInterruptedForRecoverablePause(
                            "batch stopped at a recoverable provider quota boundary"
                        )
                        return
                    ac_criterion = seed.acceptance_criteria[ac_idx]
                    resume_state = (route_resume_states or {}).get(ac_idx)
                    expected_route = (
                        resume_state.expected_route_candidate
                        if resume_state is not None
                        else (route_overrides or {}).get(ac_idx)
                    )
                    route_id_override = (
                        resume_state.route_id_override
                        if resume_state is not None
                        else expected_route.route_id
                        if expected_route is not None
                        else None
                    )
                    attempt_siblings = (
                        list(resume_state.sibling_acs) if resume_state is not None else sibling_acs
                    )
                    result = await _invoke_execution_authority_entry(
                        self,
                        _FOUNDATION_A_ENTRY_EXECUTE_SINGLE_AC,
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
                        sibling_acs=attempt_siblings,
                        retry_attempt=ac_retry_attempts[ac_idx],
                        execution_counters=execution_counters,
                        retry_prompt_extra=(
                            resume_state.retry_prompt_extra
                            if resume_state is not None
                            else (retry_prompts or {}).get(ac_idx, "")
                        ),
                        route_id_override=route_id_override,
                        expected_route_candidate=expected_route,
                        force_legacy_routing=force_legacy_routing,
                        same_runtime_budget_exhausted=same_runtime_budget_exhausted,
                        expected_resume_dispatch_id=(
                            resume_state.dispatch_id if resume_state is not None else None
                        ),
                        expected_resume_capsule_fingerprint=(
                            resume_state.capsule_fingerprint if resume_state is not None else None
                        ),
                        expected_resume_runtime_scope_id=(
                            resume_state.runtime_scope_id if resume_state is not None else None
                        ),
                        ac_spec=(
                            ac_criterion
                            if isinstance(ac_criterion, AcceptanceCriterionSpec)
                            else None
                        ),
                        investment_spec=(
                            ac_criterion.investment
                            if isinstance(ac_criterion, AcceptanceCriterionSpec)
                            else None
                        ),
                    )
                    batch_results[idx] = result
                    if cancel_on_recoverable_pause and _has_usage_limit_pause(result):
                        recoverable_pause_detected.set()
                        for sibling_idx, scope in tuple(sibling_cancel_scopes.items()):
                            if sibling_idx != idx and provider_effects.should_cancel(sibling_idx):
                                scope.cancel()
                except BaseException as e:
                    if isinstance(e, anyio.get_cancelled_exc_class()):
                        if (
                            cancel_on_recoverable_pause
                            and sibling_scope.cancel_called
                            and recoverable_pause_detected.is_set()
                        ):
                            marker_type = (
                                _BatchEnteredAtRecoverablePause
                                if provider_effects.is_active(idx)
                                else _BatchInterruptedForRecoverablePause
                            )
                            batch_results[idx] = marker_type(
                                "batch stopped at a recoverable provider quota boundary"
                            )
                            return
                        # External cancellation still owns the task group and
                        # must never be converted into a recoverable pause.
                        raise
                    batch_results[idx] = e
                finally:
                    admission_sequence.finished(idx, admission_token)
                    sibling_cancel_scopes.pop(idx, None)
                    _PROVIDER_OBSERVATION_SINK.reset(observation_sink_token)
                    provider_effects.finish(idx, provider_effect_tokens)

        # Cross-AC concurrency is governed by the LevelCoordinator's
        # file-conflict guard, not by session-level tool catalog presence.
        # Tool-call-level serialization (same runtime session cannot invoke
        # ISOLATED_SESSION_REQUIRED capabilities concurrently) is enforced by
        # the provider runtime, which is the correct layer: the batch
        # scheduler does not know which ACs will actually invoke which tools.
        async with anyio.create_task_group() as tg:
            for idx, ac_idx in enumerate(batch_indices):
                tg.start_soon(_run_ac, idx, ac_idx)

        if any(result is None for result in batch_results):
            raise RuntimeError("parallel AC batch exited without materializing every result")
        return [result for result in batch_results if result is not None]

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
        published_coordinator_pause_owner: Mapping[str, object] | None = None,
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
        published_pause_owner = normalize_published_coordinator_pause_owner(
            published_coordinator_pause_owner
        )

        start_time = datetime.now(UTC)
        all_results: list[ACExecutionResult] = []
        failed_indices: set[int] = set()
        blocked_indices: set[int] = set()
        stage_results: list[ParallelExecutionStageResult] = []
        level_contexts = list(reconciled_level_contexts or [])
        # A coordinator is an effectful writer.  Acceptance evidence collected
        # before that writer runs is not authoritative until the settled
        # workspace has been checked again at the final boundary.
        coordinator_mutated_workspace = False
        post_coordinator_revalidation_required = False
        post_coordinator_revalidated = False
        post_coordinator_revalidation_workspace_digest: str | None = None

        total_levels = execution_plan.total_stages
        total_acs = len(seed.acceptance_criteria)
        external_completed = externally_satisfied_acs or {}
        execution_counters = {
            "messages_count": 0,
            "tool_calls_count": 0,
        }

        ac_statuses: dict[int, str] = dict.fromkeys(range(total_acs), "pending")
        ac_retry_attempts: dict[int, int] = dict.fromkeys(range(total_acs), 0)
        completed_count = 0
        resume_from_level = 0
        recoverable_route_pause = False
        recoverable_coordinator_pause: CoordinatorQuotaPause | None = None

        # Restore the durable bounce phase before its consuming finalized event.
        # Both precede checkpoints, route projections, and every provider entry.
        if self._decomposition_mode == "bounce_only":
            await self._restore_bounce_classifications(
                seed=seed,
                execution_id=execution_id,
                session_id=session_id,
            )
        await self._restore_finalized_decomposition_decisions(
            seed=seed,
            execution_id=execution_id,
            session_id=session_id,
        )

        if self._checkpoint_store:
            try:
                seed_id = getattr(seed, "id", session_id)
                load_result = self._checkpoint_store.load(seed_id)
                if hasattr(load_result, "is_ok") and load_result.is_ok and load_result.value:
                    cp = load_result.value
                    checkpoint_state = cp.state if isinstance(cp.state, Mapping) else {}
                    current_workspace_identity = canonical_workspace_authority(
                        self._task_cwd
                        or getattr(self._adapter, "working_directory", None)
                        or os.getcwd()
                    )
                    checkpoint_matches_invocation = (
                        cp.seed_id == seed_id
                        and cp.phase == "parallel_execution"
                        and checkpoint_state.get("session_id") == session_id
                        and checkpoint_state.get("execution_id") == execution_id
                        and checkpoint_state.get("workspace_identity") == current_workspace_identity
                    )
                    if checkpoint_matches_invocation:
                        coordinator_mutated_workspace = bool(
                            checkpoint_state.get("coordinator_mutated_workspace", False)
                        )
                        post_coordinator_revalidation_required = bool(
                            checkpoint_state.get("post_coordinator_revalidation_required", False)
                        )
                        post_coordinator_revalidated = bool(
                            checkpoint_state.get("post_coordinator_revalidated", False)
                        )
                        raw_final_digest = checkpoint_state.get(
                            "final_workspace_digest",
                            checkpoint_state.get("post_coordinator_revalidation_workspace_digest"),
                        )
                        if isinstance(raw_final_digest, str) and raw_final_digest:
                            post_coordinator_revalidation_workspace_digest = raw_final_digest
                        if post_coordinator_revalidation_required:
                            current_digest = self._workspace_content_digest(
                                self._task_cwd
                                or getattr(self._adapter, "working_directory", None)
                                or os.getcwd()
                            )
                            if (
                                not post_coordinator_revalidated
                                or current_digest is None
                                or current_digest != post_coordinator_revalidation_workspace_digest
                            ):
                                coordinator_mutated_workspace = True
                                post_coordinator_revalidated = False
                        resume_from_level = cp.state.get("completed_levels", 0)
                        for idx, status in cp.state.get("ac_statuses", {}).items():
                            ac_statuses[int(idx)] = status
                        for idx, retry_attempt in checkpoint_state.get(
                            "ac_retry_attempts", {}
                        ).items():
                            if (
                                isinstance(retry_attempt, int)
                                and not isinstance(retry_attempt, bool)
                                and retry_attempt >= 0
                            ):
                                ac_retry_attempts[int(idx)] = retry_attempt
                        raw_result_retry_attempts = checkpoint_state.get(
                            "result_retry_attempts", {}
                        )
                        raw_verify_gate_outcomes = checkpoint_state.get("verify_gate_outcomes", {})
                        for idx in cp.state.get("failed_indices", []):
                            failed_indices.add(int(idx))
                        for idx in checkpoint_state.get("blocked_indices", []):
                            blocked_indices.add(int(idx))
                        completed_count = cp.state.get("completed_count", 0)
                        saved_contexts = cp.state.get("level_contexts", [])
                        if saved_contexts:
                            level_contexts = deserialize_level_contexts(saved_contexts)
                            restored_quota = await restore_checkpointed_coordinator_quota(
                                event_store=self._event_store,
                                level_contexts=level_contexts,
                                execution_id=execution_id,
                                session_id=session_id,
                                published_owner=published_pause_owner,
                            )
                            recoverable_coordinator_pause, published_pause_owner = restored_quota
                        self._restore_checkpoint_decomposition_decisions(
                            checkpoint_state.get("decomposition_decisions", {}),
                            root_ac_count=total_acs,
                        )
                        log.info(
                            "parallel_executor.recovery.resuming",
                            from_level=resume_from_level,
                            seed_id=seed_id,
                            restored_contexts=len(level_contexts),
                        )
                        restored_outcomes = checkpoint_state.get(
                            "revalidated_ac_outcomes",
                            checkpoint_state.get("ac_outcomes", {}),
                        )
                        for prev_stage in execution_plan.stages[:resume_from_level]:
                            for ac_idx in self._get_stage_ac_indices(prev_stage):
                                if ac_idx >= total_acs:
                                    continue
                                status = ac_statuses.get(ac_idx, "pending")
                                raw_outcome = (
                                    restored_outcomes.get(str(ac_idx))
                                    if isinstance(restored_outcomes, Mapping)
                                    else None
                                )
                                outcome = (
                                    raw_outcome
                                    if isinstance(raw_outcome, str)
                                    and raw_outcome
                                    in {
                                        "succeeded",
                                        "satisfied_externally",
                                        "failed",
                                        "blocked",
                                        "invalid",
                                    }
                                    else (
                                        "succeeded"
                                        if status == "completed"
                                        else "blocked"
                                        if status == "skipped"
                                        else "failed"
                                    )
                                )
                                is_completed = outcome in {"succeeded", "satisfied_externally"}
                                is_skipped = status == "skipped"
                                raw_result_retry_attempt = (
                                    raw_result_retry_attempts.get(str(ac_idx))
                                    if isinstance(raw_result_retry_attempts, Mapping)
                                    else None
                                )
                                restored_retry_attempt = (
                                    raw_result_retry_attempt
                                    if isinstance(raw_result_retry_attempt, int)
                                    and not isinstance(raw_result_retry_attempt, bool)
                                    and raw_result_retry_attempt >= 0
                                    else ac_retry_attempts.get(ac_idx, 0)
                                )
                                raw_verify_gate = (
                                    raw_verify_gate_outcomes.get(str(ac_idx))
                                    if isinstance(raw_verify_gate_outcomes, Mapping)
                                    else None
                                )
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
                                            if outcome == "blocked" or is_skipped
                                            else None
                                            if is_completed
                                            else "Failed (restored from checkpoint)"
                                        ),
                                        retry_attempt=restored_retry_attempt,
                                        outcome=ACExecutionOutcome(outcome),
                                        verify_gate_outcome=_deserialize_verify_gate_outcome(
                                            raw_verify_gate
                                        ),
                                    )
                                )
                        self._console.print(
                            f"[cyan]Resuming from level {resume_from_level + 1} "
                            f"(checkpoint recovered, "
                            f"{len(level_contexts)} level context(s) restored)[/cyan]"
                        )
                    elif cp.phase == "parallel_execution":
                        log.info(
                            "parallel_executor.recovery.checkpoint_identity_mismatch",
                            seed_id=seed_id,
                            session_id=session_id,
                            execution_id=execution_id,
                        )
            except RuntimeError:
                # A matching checkpoint is durable execution authority.  Once
                # recognized, malformed state must stop before provider entry;
                # treating it as a cache miss would redispatch completed work.
                raise
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
                if recoverable_coordinator_pause is not None:
                    break
                level_idx = stage.index
                level = self._get_stage_ac_indices(stage)
                stage_batches = self._get_stage_batches(stage)
                level_num = level_idx + 1

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
                    # AC carries a success contract, prove it with the gate before skipping; on gate
                    # failure, execute the AC normally instead.
                    spec = seed.acceptance_criteria[ac_idx]
                    verification_status = "assumed"
                    gate: _VerifyGateOutcome | None = None
                    if (
                        self._run_verify_commands
                        and isinstance(spec, AcceptanceCriterionSpec)
                        and spec.has_success_contract
                    ):
                        cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
                        gate = await _invoke_execution_authority_entry(
                            self,
                            _FOUNDATION_A_ENTRY_RUN_AC_VERIFY_GATE,
                            spec=spec,
                            cwd=cwd,
                        )
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
                        verify_gate_outcome=gate,
                    )
                    all_results.append(satisfied_result)
                    stage_ac_results.append(satisfied_result)
                    await self._emit_ac_attempt_judged(
                        result=satisfied_result,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                    )
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
                    await self._emit_ac_attempt_judged(
                        result=blocked_result,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                    )
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

                    batch_route_pause = self._bounded_route_escalation_enabled and any(
                        isinstance(result, ACExecutionResult) and _has_usage_limit_pause(result)
                        for result in batch_results
                    )

                    for ac_idx, result in zip(batch_executable, batch_results, strict=False):
                        if isinstance(result, _BatchInterruptedForRecoverablePause):
                            if not batch_route_pause:
                                raise RuntimeError(
                                    "parallel AC batch interrupted without a recoverable pause"
                                )
                            # This sibling crossed no completed provider boundary
                            # after the shared quota signal. Keep it pending for
                            # the exact-route resume owner; interruption is not a
                            # judgment, failure, or completed stage result.
                            ac_statuses[ac_idx] = "pending"
                            continue
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
                            await self._emit_ac_attempt_judged(
                                result=ac_result,
                                root_ac_index=ac_idx,
                                session_id=session_id,
                                execution_id=execution_id,
                            )

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
                            await self._emit_ac_attempt_judged(
                                result=ac_result,
                                root_ac_index=ac_idx,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
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

                    if batch_route_pause:
                        # A quota window belongs to the complete execution plan,
                        # not merely this route loop.  Do not start another batch
                        # or stage, and do not run sibling/coordinator effects.
                        recoverable_route_pause = True
                        break

                if recoverable_route_pause:
                    # In particular, do not emit level_completed or checkpoint
                    # this incomplete stage as completed.  Durable route events
                    # below are the replay authority for the interrupted round.
                    break

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
                    f"{stage_result.success_count} succeeded, "
                    f"{stage_result.failure_count} failed, "
                    f"{stage_result.blocked_count} blocked, "
                    f"{stage_result.invalid_count} invalid[/green]"
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
                    sealed_contexts = {
                        result.ac_index: result.context_summary
                        for result in stage_ac_results
                        if result.ac_index in executable and result.context_summary is not None
                    }
                    if sealed_contexts:
                        level_ctx = replace(
                            level_ctx,
                            completed_acs=tuple(
                                sealed_contexts.get(summary.ac_index, summary)
                                for summary in level_ctx.completed_acs
                            ),
                        )

                    # Coordinator: detect and resolve file conflicts (Approach A)
                    level_ac_results = [r for r in stage_ac_results if r.ac_index in executable]
                    conflicts = self._coordinator.detect_file_conflicts(level_ac_results)
                    restored_review = (
                        await self._restore_completed_coordinator_review(
                            execution_id=execution_id,
                            session_id=session_id,
                            level=level_num,
                            conflicts=conflicts,
                        )
                        if conflicts
                        else None
                    )

                    if conflicts:
                        if restored_review is None:
                            self._console.print(
                                f"  [yellow]Coordinator: {len(conflicts)} file conflict(s) "
                                f"detected, starting review...[/yellow]"
                            )
                            await self._emit_coordinator_started(
                                execution_id=execution_id,
                                session_id=session_id,
                                level=level_num,
                                conflicts=conflicts,
                            )
                            _invoke_execution_authority_guard(self)
                            workspace_root = (
                                self._task_cwd or self._adapter.working_directory or os.getcwd()
                            )
                            workspace_before = self._workspace_content_digest(workspace_root)
                            review: CoordinatorReview | None = None
                            async with self._adaptive_concurrency.slot() as permit_epoch:
                                _invoke_execution_authority_guard(self)
                                provider_effect_scope.enter()
                                try:
                                    review = await self._authority_coordinator_review(
                                        execution_id=execution_id,
                                        conflicts=conflicts,
                                        level_context=level_ctx,
                                        level_number=level_num,
                                    )
                                finally:
                                    try:
                                        with anyio.CancelScope(shield=True):
                                            await adaptive_concurrency.observe_provider_messages(
                                                self._adaptive_concurrency,
                                                () if review is None else review.messages,
                                                permit_epoch,
                                                None,
                                                provider_completed=review is not None,
                                            )
                                    finally:
                                        if review is not None:
                                            provider_effect_scope.complete()
                            if review is None:
                                raise RuntimeError(
                                    "coordinator provider completed without a review artifact"
                                )
                            review = review.with_recoverable_quota_state(
                                now=datetime.now(UTC),
                                default_pause_seconds=self._usage_limit_pause_seconds,
                            )
                            workspace_after = self._workspace_content_digest(workspace_root)
                            workspace_changed = (
                                workspace_before is None
                                or workspace_after is None
                                or workspace_before != workspace_after
                            )
                            coordinator_mutated_workspace = (
                                coordinator_mutated_workspace
                                or workspace_changed
                                or self._coordinator_review_may_mutate_workspace(review)
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
                        else:
                            review = restored_review
                            # The completed provider had Edit/Bash authority and
                            # its pre-effect workspace digest is not replayable.
                            # Conservatively revalidate the settled workspace.
                            coordinator_mutated_workspace = True
                            self._console.print(
                                f"  [cyan]Coordinator review restored for level "
                                f"{level_num}; provider effect not repeated.[/cyan]"
                            )
                        (
                            recoverable_coordinator_pause,
                            published_pause_owner,
                        ) = await resolve_replayed_coordinator_quota_pause(
                            event_store=self._event_store,
                            review=review,
                            execution_id=execution_id,
                            session_id=session_id,
                            level_number=level_num,
                            coordinator_aggregate_id=self._coordinator_aggregate_id(
                                execution_id, level_num
                            ),
                            restored=restored_review is not None,
                            published_owner=published_pause_owner,
                        )
                        if coordinator_mutated_workspace:
                            post_coordinator_revalidation_required = True
                            post_coordinator_revalidated = False
                            post_coordinator_revalidation_workspace_digest = None
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
                                "workspace_identity": canonical_workspace_authority(
                                    self._task_cwd
                                    or getattr(self._adapter, "working_directory", None)
                                    or os.getcwd()
                                ),
                                "coordinator_mutated_workspace": coordinator_mutated_workspace,
                                "post_coordinator_revalidation_required": (
                                    post_coordinator_revalidation_required
                                ),
                                "post_coordinator_revalidated": post_coordinator_revalidated,
                                "post_coordinator_revalidation_workspace_digest": (
                                    post_coordinator_revalidation_workspace_digest
                                ),
                                "final_workspace_digest": (
                                    post_coordinator_revalidation_workspace_digest
                                ),
                                "completed_levels": level_idx + 1,
                                "ac_statuses": {str(k): v for k, v in ac_statuses.items()},
                                "ac_retry_attempts": {
                                    str(k): v for k, v in ac_retry_attempts.items()
                                },
                                "result_retry_attempts": _checkpoint_result_retry_attempts(
                                    all_results
                                ),
                                "verify_gate_outcomes": _checkpoint_verify_gate_outcomes(
                                    all_results
                                ),
                                "ac_outcomes": {
                                    str(result.ac_index): (
                                        result.outcome.value
                                        if result.outcome is not None
                                        else ("succeeded" if result.success else "failed")
                                    )
                                    for result in all_results
                                },
                                "failed_indices": sorted(failed_indices),
                                "blocked_indices": sorted(blocked_indices),
                                "completed_count": completed_count,
                                "level_contexts": serialize_level_contexts(level_contexts),
                                "decomposition_decisions": {
                                    node_id: record.to_dict()
                                    for node_id, record in self._decomposition_decisions.items()
                                },
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

                if recoverable_coordinator_pause:
                    # The completed coordinator artifact remains the replay
                    # owner, but no later stage may cross a provider boundary
                    # until the runner durably publishes and resumes the quota.
                    break

            # All levels done — cancel the background progress emitter
            outer_tg.cancel_scope.cancel()

        needs_post_coordinator_revalidation = (
            coordinator_mutated_workspace and not post_coordinator_revalidated
        ) or (post_coordinator_revalidation_required and not post_coordinator_revalidated)
        if needs_post_coordinator_revalidation:
            # The coordinator may have edited files after a worker's verify
            # gate passed.  Reconcile every success contract against the
            # settled workspace; arbitrary verify_command contracts are not
            # replayed because no deterministic external-effect boundary exists.
            all_results = await self._revalidate_results_after_coordinator(
                seed=seed,
                results=all_results,
                session_id=session_id,
                execution_id=execution_id,
            )
            post_coordinator_revalidation_required = True
            post_coordinator_revalidated = True
            post_coordinator_revalidation_workspace_digest = self._workspace_content_digest(
                self._task_cwd or self._adapter.working_directory or os.getcwd()
            )
            if post_coordinator_revalidation_workspace_digest is None:
                # The final evidence must be bound to a readable workspace. A
                # successful command without a final digest is not durable
                # acceptance evidence.
                final_digest_error = (
                    "Final workspace digest unavailable after coordinator revalidation."
                )
                all_results = [
                    replace(
                        result,
                        success=False,
                        error=final_digest_error,
                        final_message=final_digest_error,
                        outcome=ACExecutionOutcome.FAILED,
                    )
                    if result.success
                    and result.outcome
                    in {
                        ACExecutionOutcome.SUCCEEDED,
                        ACExecutionOutcome.SATISFIED_EXTERNALLY,
                    }
                    else result
                    for result in all_results
                ]
            by_index = {result.ac_index: result for result in all_results}
            stage_results = [
                replace(
                    stage,
                    results=tuple(
                        by_index.get(result.ac_index, result) for result in stage.results
                    ),
                )
                for stage in stage_results
            ]

            # Persist the post-coordinator verdict after, not before, the
            # revalidation.  A crash after the old checkpoint write therefore
            # resumes through this same boundary instead of reviving stale ACs.
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
                            "workspace_identity": canonical_workspace_authority(
                                self._task_cwd
                                or getattr(self._adapter, "working_directory", None)
                                or os.getcwd()
                            ),
                            "coordinator_mutated_workspace": coordinator_mutated_workspace,
                            "post_coordinator_revalidation_required": (
                                post_coordinator_revalidation_required
                            ),
                            "post_coordinator_revalidated": post_coordinator_revalidated,
                            "post_coordinator_revalidation_workspace_digest": (
                                post_coordinator_revalidation_workspace_digest
                            ),
                            "final_workspace_digest": post_coordinator_revalidation_workspace_digest,
                            "completed_levels": total_levels,
                            "ac_statuses": {str(k): v for k, v in ac_statuses.items()},
                            "ac_retry_attempts": {str(k): v for k, v in ac_retry_attempts.items()},
                            "result_retry_attempts": _checkpoint_result_retry_attempts(all_results),
                            "verify_gate_outcomes": _checkpoint_verify_gate_outcomes(all_results),
                            "ac_outcomes": {
                                str(result.ac_index): (
                                    result.outcome.value
                                    if result.outcome is not None
                                    else ("succeeded" if result.success else "failed")
                                )
                                for result in all_results
                            },
                            "revalidated_ac_outcomes": {
                                str(result.ac_index): (
                                    result.outcome.value
                                    if result.outcome is not None
                                    else ("succeeded" if result.success else "failed")
                                )
                                for result in all_results
                            },
                            "failed_indices": sorted(failed_indices),
                            "blocked_indices": sorted(blocked_indices),
                            "completed_count": completed_count,
                            "level_contexts": serialize_level_contexts(level_contexts),
                            "decomposition_decisions": {
                                node_id: record.to_dict()
                                for node_id, record in self._decomposition_decisions.items()
                            },
                        },
                    )
                    save_result = self._checkpoint_store.save(checkpoint)
                    if not getattr(save_result, "is_ok", False):
                        log.warning(
                            "parallel_executor.final_revalidation_checkpoint_save_failed",
                            seed_id=seed_id,
                            error=str(getattr(save_result, "error", "unknown error")),
                        )
                except Exception as exc:
                    log.warning(
                        "parallel_executor.final_revalidation_checkpoint_save_failed",
                        error=str(exc),
                    )

        # All ACs have now finished (including any coordinator reconciliation).
        # Reconcile verify evidence against the settled shared workspace before
        # the runner constructs the terminal acceptance plan.
        all_results = await self._settle_verify_gate_results(
            seed=seed,
            results=all_results,
            session_id=session_id,
            execution_id=execution_id,
            coordinator_revalidated=post_coordinator_revalidated,
        )
        settled_by_index = {result.ac_index: result for result in all_results}
        stage_results = [
            replace(
                stage,
                results=tuple(
                    settled_by_index.get(result.ac_index, result) for result in stage.results
                ),
            )
            for stage in stage_results
        ]

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
            recoverable_route_pause=recoverable_route_pause,
            recoverable_coordinator_pause=recoverable_coordinator_pause,
        )

    @staticmethod
    def _coordinator_review_may_mutate_workspace(review: Any) -> bool:
        """Return whether a coordinator review could have changed the workspace.

        Coordinator sessions are allowed to use ``Edit`` and ``Bash``.  The
        structured review summary is not treated as proof of a mutation: a
        model can report a proposed fix without applying one.  The caller also
        compares workspace digests around the review, so direct writes remain
        observable even when a runtime omits tool messages.
        """
        if review is None:
            return False
        for message in getattr(review, "messages", ()) or ():
            if getattr(message, "tool_name", None) in {"Write", "Edit", "Bash"}:
                return True
        return False

    @staticmethod
    def _workspace_content_digest(
        cwd: str,
        *,
        expected_artifacts: tuple[str, ...] = (),
    ) -> str | None:
        """Hash acceptance-relevant workspace state for mutation checks.

        Runtime/cache paths are excluded unless they overlap an explicitly
        declared expected artifact. Bytecode suffix exclusions apply only to
        regular files: same-named directories and symlinks remain observable.
        Read failures return ``None`` so the caller fails closed instead of
        trusting evidence it could not compare.
        """
        try:
            root = Path(cwd).expanduser().resolve(strict=False)
            if not root.is_dir():
                return hashlib.sha256(
                    f"missing-workspace\0{root}".encode("utf-8", "surrogateescape")
                ).hexdigest()

            declared_paths: set[Path] = set()
            for artifact in expected_artifacts:
                # Keep both the lexical path and its resolved in-workspace target.
                # The former binds symlink artifacts themselves; the latter keeps
                # an artifact reached through an in-workspace symlink observable.
                candidate = Path(os.path.abspath(root / artifact))
                for declared in (candidate, candidate.resolve(strict=False)):
                    if declared.is_relative_to(root):
                        declared_paths.add(declared.relative_to(root))

            def is_declared_contract_path(relative: Path) -> bool:
                return any(
                    relative == declared
                    or relative in declared.parents
                    or declared in relative.parents
                    for declared in declared_paths
                )

            tracked_paths = load_tracked_workspace_paths(root)

            digest = hashlib.sha256()
            paths = sorted(root.rglob("*"), key=lambda path: path.as_posix())
            for path in paths:
                relative = path.relative_to(root)
                declared_contract_path = is_declared_contract_path(relative)
                if (
                    is_untracked_top_level_evidence_path(
                        relative,
                        tracked_paths=tracked_paths,
                        is_directory=path.is_dir() and not path.is_symlink(),
                    )
                    or any(
                        part in _WORKSPACE_FINGERPRINT_IGNORED_DIRECTORIES
                        for part in relative.parts
                    )
                ) and not declared_contract_path:
                    continue
                try:
                    stat = path.lstat()
                    if path.is_symlink():
                        digest.update(b"L\0")
                        digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
                        digest.update(b"\0")
                        digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
                    elif path.is_dir():
                        # Empty-directory creation/removal is observable workspace
                        # state.  Hash a directory marker as well as its mode so a
                        # coordinator cannot evade revalidation by only changing
                        # the tree shape.
                        digest.update(b"D\0")
                        digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
                        digest.update(b"\0")
                        digest.update(str(stat.st_mode).encode("ascii"))
                    elif path.is_file():
                        if (
                            path.suffix in _WORKSPACE_FINGERPRINT_IGNORED_REGULAR_FILE_SUFFIXES
                            and not declared_contract_path
                        ):
                            continue
                        digest.update(b"F\0")
                        digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
                        digest.update(b"\0")
                        digest.update(str(stat.st_mode).encode("ascii"))
                        digest.update(b"\0")
                        with path.open("rb") as handle:
                            while chunk := handle.read(1024 * 1024):
                                digest.update(chunk)
                except (OSError, ValueError):
                    return None
            return digest.hexdigest()
        except (OSError, ValueError):
            return None

    async def _revalidate_results_after_coordinator(
        self,
        *,
        seed: Seed,
        results: list[ACExecutionResult],
        session_id: str,
        execution_id: str,
    ) -> list[ACExecutionResult]:
        """Bind successful ACs to the workspace settled by the coordinator.

        A cached command result is intentionally not replayed here.  The
        coordinator is an effectful writer and can invalidate a previously
        passing command after the worker-level gate, while an unrestricted
        shell replay could duplicate external effects.  Command-bearing ACs
        therefore fail closed; artifact-only contracts are checked against the
        settled workspace and description-only ACs fail closed because no
        independent post-mutation contract exists.
        """
        from ouroboros.events.base import BaseEvent

        cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
        revalidated: list[ACExecutionResult] = []
        for result in results:
            if not result.success or result.outcome not in {
                ACExecutionOutcome.SUCCEEDED,
                ACExecutionOutcome.SATISFIED_EXTERNALLY,
            }:
                revalidated.append(result)
                continue

            spec = (
                seed.acceptance_criteria[result.ac_index]
                if 0 <= result.ac_index < len(seed.acceptance_criteria)
                else None
            )
            has_contract = isinstance(spec, AcceptanceCriterionSpec) and spec.has_success_contract
            if not self._run_verify_commands or not has_contract:
                reason = (
                    "Final workspace changed during coordinator reconciliation; "
                    "the AC has no deterministic post-coordinator success contract."
                )
                await self._safe_emit_event(
                    BaseEvent(
                        type="execution.verify.failed",
                        aggregate_type="execution",
                        aggregate_id=execution_id or session_id,
                        data={
                            "session_id": session_id,
                            "execution_id": execution_id,
                            "ac_index": result.ac_index,
                            "reason": reason,
                            "failure_class": "evidence_missing",
                            "final_workspace_revalidation": True,
                            "verify_cause": "workspace_mutated",
                        },
                    )
                )
                usage_telemetry.capture_ac_verify_failed(cause="workspace_mutated")
                revalidated.append(
                    replace(
                        result,
                        success=False,
                        error=reason,
                        final_message=reason,
                        outcome=ACExecutionOutcome.FAILED,
                    )
                )
                continue

            if spec.verify_command:
                # A verify command is an arbitrary shell contract and may have
                # external effects that the workspace digest cannot observe.
                # Never replay it after a coordinator mutation; fail closed at
                # this boundary instead of executing an effectful command twice.
                reason = (
                    "Final acceptance rejected because the coordinator changed the "
                    "workspace and replaying verify_command is not permitted."
                )
                await self._safe_emit_event(
                    BaseEvent(
                        type="execution.verify.failed",
                        aggregate_type="execution",
                        aggregate_id=execution_id or session_id,
                        data={
                            "session_id": session_id,
                            "execution_id": execution_id,
                            "ac_index": result.ac_index,
                            "verify_command": spec.verify_command,
                            "expected_artifacts": list(spec.expected_artifacts),
                            "reason": reason,
                            "failure_class": "evidence_missing",
                            "final_workspace_revalidation": True,
                            "verify_replay_blocked": True,
                            "verify_cause": "workspace_mutated",
                        },
                    )
                )
                usage_telemetry.capture_ac_verify_failed(cause="workspace_mutated")
                revalidated.append(
                    replace(
                        result,
                        success=False,
                        error=reason,
                        final_message=reason,
                        outcome=ACExecutionOutcome.FAILED,
                    )
                )
                continue

            missing_artifacts = _missing_expected_artifacts(spec.expected_artifacts, cwd)
            if missing_artifacts:
                reason = "Final expected_artifacts missing: " + ", ".join(missing_artifacts)
                # This branch owns the rejection (settlement skips
                # already-failed results), so it must record the same durable
                # diagnostics and closed-cause analytics as every other
                # deterministic gate rejection.
                verify_cause = _missing_artifacts_cause(missing_artifacts, cwd)
                await self._safe_emit_event(
                    BaseEvent(
                        type="execution.verify.failed",
                        aggregate_type="execution",
                        aggregate_id=execution_id or session_id,
                        data={
                            "session_id": session_id,
                            "execution_id": execution_id,
                            "ac_index": result.ac_index,
                            "expected_artifacts": list(spec.expected_artifacts),
                            "missing_artifacts": list(missing_artifacts),
                            "reason": reason,
                            "failure_class": "evidence_missing",
                            "final_workspace_revalidation": True,
                            "verify_cause": verify_cause,
                        },
                    )
                )
                usage_telemetry.capture_ac_verify_failed(cause=verify_cause)
                revalidated.append(
                    replace(
                        result,
                        success=False,
                        error=reason,
                        final_message=reason,
                        outcome=ACExecutionOutcome.FAILED,
                    )
                )
                continue

            revalidated.append(
                replace(
                    result,
                    verify_gate_outcome=_VerifyGateOutcome(
                        passed=True,
                        reason=None,
                        output_tail="",
                        workspace_digest=self._workspace_content_digest(
                            cwd,
                            expected_artifacts=spec.expected_artifacts,
                        ),
                    ),
                )
            )

        return revalidated

    async def _settle_verify_gate_results(
        self,
        *,
        seed: Seed,
        results: list[ACExecutionResult],
        session_id: str,
        execution_id: str,
        coordinator_revalidated: bool = False,
    ) -> list[ACExecutionResult]:
        """Fail closed when final shared-workspace evidence is no longer valid.

        Verify gates run as each AC completes, while later ACs can still touch
        the same workspace.  Before terminal acceptance, re-check every
        successful contract's artifact leg and cached workspace identity. A
        stale command result is rejected rather than replayed because an
        unrestricted shell command may have effects outside the workspace.
        Invalidate the complete success set when any verify command was
        observed mutating the workspace.
        """
        from ouroboros.events.base import BaseEvent
        from ouroboros.orchestrator.failure_taxonomy import FailureClass

        if not self._run_verify_commands:
            return results

        successful_contracts: dict[int, AcceptanceCriterionSpec] = {}
        verify_mutated_workspace = False
        for result in results:
            outcome = result.verify_gate_outcome
            verify_mutated_workspace = verify_mutated_workspace or bool(
                isinstance(outcome, _VerifyGateOutcome) and outcome.workspace_mutated
            )
            if not result.success or not (0 <= result.ac_index < len(seed.acceptance_criteria)):
                continue
            spec = seed.acceptance_criteria[result.ac_index]
            if isinstance(spec, AcceptanceCriterionSpec) and spec.has_success_contract:
                successful_contracts[result.ac_index] = spec

        if not successful_contracts and not verify_mutated_workspace:
            return results

        cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
        settled: list[ACExecutionResult] = []
        # (reason, outcome, verify_cause): every settlement branch names its
        # own machine-readable cause at classification time — a shared
        # fallback guess here would misreport e.g. an unavailable digest as
        # concurrent workspace mutation. `None` folds to `unknown` downstream.
        individual_failures: dict[int, tuple[str, _VerifyGateOutcome | None, str | None]] = {}

        for result in results:
            if not result.success:
                settled.append(result)
                continue

            spec = successful_contracts.get(result.ac_index)
            if spec is None:
                # A mutating final verifier invalidates all successes, including
                # description-only ACs.  Defer the replacement until every
                # final verifier has been inspected.
                settled.append(result)
                continue

            if verify_mutated_workspace:
                # Once one final verifier has changed (or made unreadable) the
                # workspace, do not execute any additional arbitrary commands.
                # The complete success set will be invalidated below.
                settled.append(result)
                continue

            outcome = result.verify_gate_outcome
            if not isinstance(outcome, _VerifyGateOutcome):
                individual_failures[result.ac_index] = (
                    "Final verify gate evidence is unavailable for acceptance.",
                    None,
                    None,
                )
                settled.append(result)
                continue

            if not outcome.passed and not outcome.environment_unverifiable:
                individual_failures[result.ac_index] = (
                    f"Final workspace verify gate failed: {outcome.reason}",
                    outcome,
                    outcome.cause,
                )
                settled.append(result)
                continue

            missing_artifacts = _missing_expected_artifacts(spec.expected_artifacts, cwd)
            final_digest = self._workspace_content_digest(
                cwd,
                expected_artifacts=spec.expected_artifacts,
            )
            if final_digest is None:
                individual_failures[result.ac_index] = (
                    "Final workspace digest unavailable for acceptance evidence.",
                    outcome,
                    None,
                )
                settled.append(result)
                continue
            if missing_artifacts:
                individual_failures[result.ac_index] = (
                    "Final expected_artifacts missing: " + ", ".join(missing_artifacts),
                    outcome,
                    _missing_artifacts_cause(missing_artifacts, cwd),
                )
                settled.append(result)
                continue

            if spec.verify_command and not outcome.environment_unverifiable:
                if coordinator_revalidated:
                    # Coordinator revalidation deliberately refuses to replay
                    # arbitrary shell contracts.  A command-bearing success
                    # that survived that boundary is therefore not admissible.
                    individual_failures[result.ac_index] = (
                        "Final acceptance rejected because coordinator revalidation "
                        "did not replay verify_command.",
                        outcome,
                        "workspace_mutated",
                    )
                    settled.append(result)
                    continue

                cached_digest = outcome.workspace_digest
                if cached_digest is None:
                    individual_failures[result.ac_index] = (
                        "Final verify gate workspace digest unavailable for acceptance.",
                        outcome,
                        None,
                    )
                    settled.append(result)
                    continue

                if cached_digest != final_digest:
                    # A later worker changed the workspace after this command
                    # passed.  Do not replay an unrestricted shell contract:
                    # effects outside the workspace (DB/API/deployment writes)
                    # cannot be detected by the digest.  The stale evidence is
                    # therefore rejected at the final boundary.
                    individual_failures[result.ac_index] = (
                        "Final acceptance rejected because the workspace changed "
                        "after verify_command completed; replaying verify_command "
                        "is not permitted.",
                        outcome,
                        "workspace_mutated",
                    )
                    settled.append(result)
                    continue

            settled.append(result)

        if verify_mutated_workspace:
            # A verifier is an observation boundary, not another writer.  If
            # any final verifier changed the shared workspace (or its digest
            # became unreadable), every success observed before or after that
            # command is stale.  Fail closed for the complete finalization set.
            mutation_reason = (
                "Final acceptance rejected because a verify_command mutated the "
                "workspace or its digest could not be revalidated."
            )
            individual_failures = {
                result.ac_index: (
                    mutation_reason,
                    result.verify_gate_outcome
                    if isinstance(result.verify_gate_outcome, _VerifyGateOutcome)
                    else None,
                    "workspace_mutated",
                )
                for result in settled
                if result.success
            }

        finalized: list[ACExecutionResult] = []
        for result in settled:
            failure = individual_failures.get(result.ac_index)
            if failure is None:
                finalized.append(result)
                continue
            reason, outcome, verify_cause = failure
            spec = successful_contracts.get(result.ac_index)
            missing_artifacts = (
                list(outcome.missing_artifacts) if isinstance(outcome, _VerifyGateOutcome) else []
            )
            await self._safe_emit_event(
                BaseEvent(
                    type="execution.verify.failed",
                    aggregate_type="execution",
                    aggregate_id=execution_id or session_id,
                    data={
                        "session_id": session_id,
                        "execution_id": execution_id,
                        "ac_index": result.ac_index,
                        "ac_content": result.ac_content,
                        "verify_command": spec.verify_command if spec is not None else None,
                        "expected_artifacts": (
                            list(spec.expected_artifacts) if spec is not None else []
                        ),
                        "missing_artifacts": missing_artifacts,
                        "reason": reason,
                        "failure_class": FailureClass.EVIDENCE_MISSING.value,
                        "final_workspace_revalidation": True,
                        "verify_cause": verify_cause,
                    },
                )
            )
            usage_telemetry.capture_ac_verify_failed(cause=verify_cause)
            finalized.append(
                replace(
                    result,
                    success=False,
                    error=reason,
                    final_message=reason,
                    outcome=ACExecutionOutcome.FAILED,
                    atomic_verifier_verdict=VerifierVerdict(
                        passed=False,
                        reasons=(reason,),
                        failure_class=FailureClass.EVIDENCE_MISSING.value,
                    ),
                )
            )
        return finalized

    def _coerce_decomposition_decision(
        self,
        value: object,
        *,
        node_identity: ExecutionNodeIdentity,
        source: DecompositionSource,
        cause: BounceCause | None = None,
    ) -> DecompositionDecisionRecord:
        """Normalize production and legacy/mocked decomposition results."""
        if isinstance(value, DecompositionDecisionRecord):
            if value.node_id != node_identity.node_id or value.source is not source:
                return DecompositionDecisionRecord(
                    node_id=node_identity.node_id,
                    source=source,
                    disposition=DecompositionDisposition.UNKNOWN,
                    cause=cause,
                    reasons=("decomposition_decision_identity_mismatch",),
                )
            if cause is not None and value.cause is not cause:
                return DecompositionDecisionRecord(
                    node_id=node_identity.node_id,
                    source=source,
                    disposition=DecompositionDisposition.UNKNOWN,
                    cause=cause,
                    reasons=("decomposition_decision_cause_mismatch",),
                )
            return value
        if isinstance(value, list) and all(isinstance(item, str) for item in value):
            if not MIN_SUB_ACS <= len(value) <= MAX_SUB_ACS:
                return DecompositionDecisionRecord(
                    node_id=node_identity.node_id,
                    source=source,
                    disposition=DecompositionDisposition.UNKNOWN,
                    cause=cause,
                    reasons=("legacy_split_child_count_invalid",),
                )
            return legacy_unverified_split_decision(
                node_id=node_identity.node_id,
                source=source,
                child_descriptions=value,
                cause=cause,
                reasons=("legacy_unverified_split",),
            )
        if value is None:
            return DecompositionDecisionRecord(
                node_id=node_identity.node_id,
                source=source,
                disposition=DecompositionDisposition.ATOMIC,
                cause=cause,
                reasons=("legacy_atomic_result",),
            )
        return DecompositionDecisionRecord(
            node_id=node_identity.node_id,
            source=source,
            disposition=DecompositionDisposition.UNKNOWN,
            cause=cause,
            reasons=("unsupported_decomposition_result",),
        )

    async def _finalize_decomposition_decision(
        self,
        *,
        decision: DecompositionDecisionRecord,
        node_identity: ExecutionNodeIdentity,
        execution_id: str,
        session_id: str,
    ) -> DecompositionDecisionRecord:
        """Persist event-owned authority, then publish its runtime projection."""
        if decision.node_id != node_identity.node_id:
            decision = DecompositionDecisionRecord(
                node_id=node_identity.node_id,
                source=decision.source,
                disposition=DecompositionDisposition.UNKNOWN,
                cause=decision.cause,
                reasons=("decomposition_decision_identity_mismatch",),
            )
        previous = self._event_owned_decomposition_decisions.get(node_identity.node_id)
        cached = self._decomposition_decisions.get(node_identity.node_id)
        if cached is not None and cached != previous:
            raise RuntimeError("decomposition cache lacks matching finalized-event authority")
        if (
            previous is not None
            and previous != decision
            and not self._is_valid_decision_transition(
                previous,
                decision,
            )
        ):
            raise RuntimeError("a finalized decomposition decision cannot change")
        if previous == decision:
            self._decomposition_decisions[node_identity.node_id] = decision
            return decision
        self._require_pending_bounce_for_finalized_decision(decision)
        await self._event_emitter.emit_decomposition_decision_finalized(
            execution_id=execution_id,
            session_id=session_id,
            mode=self._decomposition_mode,
            node_identity=node_identity,
            decision=decision,
        )
        if decision.source is DecompositionSource.BOUNCE:
            self._pending_bounce_decompositions.pop(decision.node_id)
        self._event_owned_decomposition_decisions[node_identity.node_id] = decision
        self._decomposition_decisions[node_identity.node_id] = decision
        return decision

    @staticmethod
    def _is_valid_decision_transition(
        previous: DecompositionDecisionRecord,
        current: DecompositionDecisionRecord,
    ) -> bool:
        """Admit only the one historical-to-live migration transition."""

        return bool(
            previous.node_id == current.node_id
            and previous.source is DecompositionSource.PREFLIGHT
            and previous.disposition is not DecompositionDisposition.SPLIT
            and current.source is DecompositionSource.BOUNCE
        )

    def _require_pending_bounce_for_finalized_decision(
        self,
        decision: DecompositionDecisionRecord,
        *,
        finalized_event_key: tuple[datetime, str] | None = None,
    ) -> None:
        """Bind every BOUNCE decision to its earlier durable TOO_BIG trigger."""

        if decision.source is not DecompositionSource.BOUNCE:
            return
        pending = self._pending_bounce_decompositions.get(decision.node_id)
        if (
            decision.cause is not BounceCause.TOO_BIG
            or pending is None
            or pending.cause is not BounceCause.TOO_BIG
            or decision.evidence_refs != pending.evidence_refs
            or (
                finalized_event_key is not None
                and pending.event_key is not None
                and pending.event_key >= finalized_event_key
            )
        ):
            raise RuntimeError("BOUNCE decision lacks matching prior TOO_BIG authority")

    def _publish_event_owned_decomposition_decision(
        self,
        decision: DecompositionDecisionRecord,
    ) -> None:
        """Publish authority restored from a canonical finalized event."""

        previous = self._event_owned_decomposition_decisions.get(decision.node_id)
        cached = self._decomposition_decisions.get(decision.node_id)
        if previous is not None and previous != decision:
            raise RuntimeError("durable decomposition decisions conflict")
        if cached is not None and cached != decision:
            raise RuntimeError("decomposition cache conflicts with finalized-event authority")
        self._event_owned_decomposition_decisions[decision.node_id] = decision
        self._decomposition_decisions[decision.node_id] = decision

    def _confirm_replayed_decomposition_decision(
        self,
        decision: DecompositionDecisionRecord,
    ) -> None:
        """Confirm a cache projection without allowing it to mint authority."""

        authority = self._event_owned_decomposition_decisions.get(decision.node_id)
        if authority is None:
            raise RuntimeError("decomposition cache lacks finalized-event authority")
        if authority != decision:
            raise RuntimeError("decomposition cache conflicts with finalized-event authority")
        self._decomposition_decisions[decision.node_id] = authority

    async def _restore_bounce_classifications(
        self,
        *,
        seed: Seed,
        execution_id: str,
        session_id: str,
    ) -> None:
        """Restore canonical durable TOO_BIG phases before any resumed effect."""

        async for event in replay_execution_events_chronologically(
            self._event_store,
            execution_id=execution_id,
            event_type="execution.decomposition.bounce_classified",
            page_size=_PARALLEL_PAUSE_REPLAY_PAGE_SIZE,
        ):
            if event.type != "execution.decomposition.bounce_classified":
                continue
            data = event.data
            if not _mapping_has_exact_keys(data, _BOUNCE_CLASSIFIED_EVENT_KEYS):
                raise RuntimeError("bounce classification replay has an invalid event envelope")
            if data.get("session_id") != session_id:
                continue
            raw_path = data.get("path")
            root_ac_index = data.get("root_ac_index")
            if (
                data.get("execution_id") != execution_id
                or data.get("identity_model") != "execution_node_v1"
                or type(root_ac_index) is not int
                or not 0 <= root_ac_index < len(seed.acceptance_criteria)
                or type(raw_path) is not list
                or not raw_path
                or len(raw_path) > self._max_decomposition_depth + 1
                or any(type(ordinal) is not int or ordinal < 0 for ordinal in raw_path)
                or raw_path[0] != root_ac_index
                or any(ordinal >= MAX_DECOMPOSITION_CHILDREN for ordinal in raw_path[1:])
            ):
                raise RuntimeError("bounce classification replay has invalid execution identity")
            node_identity = ExecutionNodeIdentity.root(
                execution_context_id=execution_id or session_id,
                ac_index=root_ac_index,
            )
            for ordinal in raw_path[1:]:
                node_identity = node_identity.child(ordinal)
            try:
                cause = BounceCause(data.get("cause"))
                raw_evidence_refs = data.get("evidence_refs")
                if type(raw_evidence_refs) is not list:
                    raise ValueError
                trace = DecompositionTraceSummary(
                    summary=data.get("trace_summary"),
                    evidence_refs=tuple(raw_evidence_refs),
                )
            except (TypeError, ValueError):
                raise RuntimeError(
                    "bounce classification replay has malformed bounded evidence"
                ) from None
            rationale = data.get("rationale")
            failure_class = data.get("failure_class")
            retry_admission = data.get("retry_admission")
            if (
                type(rationale) is not str
                or not rationale
                or rationale != redact_and_truncate_text(rationale, max_chars=MAX_REASON_CHARS)
                or trace.summary != data.get("trace_summary")
                or list(trace.evidence_refs) != raw_evidence_refs
                or len(trace.evidence_refs) > MAX_EVIDENCE_REF_COUNT
                or any(len(ref) > MAX_EVIDENCE_REF_CHARS for ref in trace.evidence_refs)
                or len(trace.summary) > MAX_TRACE_SUMMARY_CHARS
                or (
                    failure_class is not None
                    and (type(failure_class) is not str or len(failure_class) > MAX_REASON_CHARS)
                )
                or (
                    retry_admission is not None
                    and (
                        type(retry_admission) is not str or len(retry_admission) > MAX_REASON_CHARS
                    )
                )
            ):
                raise RuntimeError(
                    "bounce classification replay has non-canonical bounded evidence"
                )
            expected_payload = {
                **node_identity.to_event_metadata(),
                "execution_id": execution_id,
                "session_id": session_id,
                "cause": cause.value,
                "rationale": rationale,
                "failure_class": failure_class,
                "retry_admission": retry_admission,
                "evidence_refs": list(trace.evidence_refs),
                "trace_summary": trace.summary,
            }
            if data != expected_payload:
                raise RuntimeError("bounce classification replay is not canonical")
            if cause is not BounceCause.TOO_BIG:
                continue
            if node_identity.node_id in self._pending_bounce_decompositions:
                raise RuntimeError(
                    "bounce classification replay duplicated a pending TOO_BIG phase"
                )
            self._pending_bounce_decompositions[node_identity.node_id] = _DurableBounceReplayState(
                cause=cause,
                rationale=rationale,
                failure_class=failure_class,
                retry_admission=retry_admission,
                evidence_refs=trace.evidence_refs,
                trace_summary=trace.summary,
                event_key=(event.timestamp, event.id),
            )

    async def _restore_finalized_decomposition_decisions(
        self,
        *,
        seed: Seed,
        execution_id: str,
        session_id: str,
    ) -> None:
        """Replay the mandatory decision event before any resumed effect.

        Checkpoints and composite projections are caches of this authority, not
        substitutes for it.  Historical PREFLIGHT decisions remain consumable,
        while the live constructor still exposes no preflight producer path.
        """

        event_limit = _decomposition_decision_event_sentinel(len(seed.acceptance_criteria))
        raw_events = await self._event_store.query_execution_related_events(
            execution_id,
            event_type="execution.decomposition.decision_finalized",
            limit=event_limit,
        )
        try:
            events = list(raw_events)
        except TypeError as exc:
            raise RuntimeError(
                "decomposition decision replay returned a non-iterable page"
            ) from exc
        if len(events) >= event_limit:
            raise RuntimeError("decomposition decision replay exceeds the admitted node population")
        seen: dict[str, DecompositionDecisionRecord] = {}
        for event in sorted(events, key=lambda item: (item.timestamp, item.id)):
            if event.type != "execution.decomposition.decision_finalized":
                continue
            data = event.data
            if not _mapping_has_exact_keys(data, _DECOMPOSITION_DECISION_EVENT_KEYS):
                raise RuntimeError("decomposition decision replay has an invalid event envelope")
            if data.get("session_id") != session_id:
                continue
            raw_path = data.get("path")
            root_ac_index = data.get("root_ac_index")
            if (
                data.get("execution_id") != execution_id
                or data.get("identity_model") != "execution_node_v1"
                or type(root_ac_index) is not int
                or not 0 <= root_ac_index < len(seed.acceptance_criteria)
                or type(raw_path) is not list
                or not raw_path
                or len(raw_path) > self._max_decomposition_depth + 1
                or any(type(ordinal) is not int or ordinal < 0 for ordinal in raw_path)
                or raw_path[0] != root_ac_index
                or any(ordinal >= MAX_DECOMPOSITION_CHILDREN for ordinal in raw_path[1:])
            ):
                raise RuntimeError("decomposition decision replay has invalid execution identity")
            node_identity = ExecutionNodeIdentity.root(
                execution_context_id=execution_id or session_id,
                ac_index=root_ac_index,
            )
            for ordinal in raw_path[1:]:
                node_identity = node_identity.child(ordinal)
            raw_decision = {key: data[key] for key in _DECOMPOSITION_DECISION_RECORD_KEYS}
            decision = DecompositionDecisionRecord.from_dict(raw_decision)
            mode = data.get("mode")
            expected_source = (
                DecompositionSource.PREFLIGHT
                if mode == "preflight"
                else DecompositionSource.BOUNCE
                if mode == "bounce_only"
                else None
            )
            if (
                decision is None
                or decision.node_id != node_identity.node_id
                or decision.source is not expected_source
                or data.get("child_count") != len(decision.children)
                or (
                    decision.disposition is DecompositionDisposition.SPLIT
                    and node_identity.depth >= self._max_decomposition_depth
                )
            ):
                raise RuntimeError("decomposition decision replay has invalid decision authority")
            expected_payload = {
                **node_identity.to_event_metadata(),
                **decision.to_dict(),
                "execution_id": execution_id,
                "session_id": session_id,
                "mode": mode,
                "child_count": len(decision.children),
            }
            if data != expected_payload:
                raise RuntimeError("decomposition decision replay is not canonical")
            previous = seen.get(decision.node_id)
            if previous is not None:
                if not self._is_valid_decision_transition(previous, decision):
                    raise RuntimeError("decomposition decision replay has an invalid transition")
            self._require_pending_bounce_for_finalized_decision(
                decision,
                finalized_event_key=(event.timestamp, event.id),
            )
            if decision.source is DecompositionSource.BOUNCE:
                self._pending_bounce_decompositions.pop(decision.node_id)
            if previous is not None:
                self._event_owned_decomposition_decisions[decision.node_id] = decision
                self._decomposition_decisions[decision.node_id] = decision
            else:
                self._publish_event_owned_decomposition_decision(decision)
            seen[decision.node_id] = decision

    def _restore_checkpoint_decomposition_decisions(
        self,
        raw_decisions: object,
        *,
        root_ac_count: int,
    ) -> None:
        """Strictly merge the checkpoint cache into event-owned replay state."""

        if type(raw_decisions) is not dict:
            raise RuntimeError("checkpoint decomposition decisions are malformed")
        max_population = root_ac_count * (MAX_DECOMPOSITION_REPLAY_NODES + 1)
        if len(raw_decisions) > max_population:
            raise RuntimeError("checkpoint decomposition decisions exceed their bound")
        for raw_node_id, raw_record in raw_decisions.items():
            if type(raw_node_id) is not str:
                raise RuntimeError("checkpoint decomposition decision id is malformed")
            restored = DecompositionDecisionRecord.from_dict(raw_record)
            if (
                restored is None
                or restored.node_id != raw_node_id
                or restored.to_dict() != raw_record
            ):
                raise RuntimeError("checkpoint decomposition decision is non-canonical")
            try:
                self._confirm_replayed_decomposition_decision(restored)
            except RuntimeError as exc:
                raise RuntimeError(f"checkpoint {exc}") from exc

    async def _execute_decomposition_children(
        self,
        *,
        decision: DecompositionDecisionRecord,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        seed_goal: str,
        depth: int,
        execution_id: str,
        level_contexts: list[LevelContext] | None,
        retry_attempt: int,
        execution_counters: dict[str, int] | None,
        node_identity: ExecutionNodeIdentity,
        start_time: datetime,
        semantic_ac_key: str,
        investment_spec: InvestmentSpec | None = None,
    ) -> ACExecutionResult:
        """Dispatch one finalized split through the shared recursive child path."""
        sub_acs = [child.description for child in decision.children]
        resume_state = self._partial_composite_resumes.pop(node_identity.node_id, None)
        if resume_state is not None and (
            resume_state.decision != decision
            or resume_state.paused_child_index >= len(sub_acs)
            or len(resume_state.completed_children) != resume_state.paused_child_index
            or tuple(child.ac_content for child in resume_state.completed_children)
            != tuple(sub_acs[: resume_state.paused_child_index])
            or resume_state.paused_child_content != sub_acs[resume_state.paused_child_index]
            or resume_state.paused_child_ac_index
            != ac_index * 100 + resume_state.paused_child_index
            or resume_state.paused_child_retry_attempt != retry_attempt
        ):
            raise RuntimeError("partial composite resume state drifted from its split plan")
        display_label = (
            f"AC {node_identity.display_path}"
            if node_identity.depth == 0
            else f"Sub-AC {node_identity.display_path}"
        )
        self._console.print(
            f"  [cyan]{display_label} → Decomposed into {len(sub_acs)} Sub-ACs (parallel)[/cyan]"
        )
        self._flush_console()
        first_pending_child = resume_state.paused_child_index if resume_state is not None else 0
        for idx, sub_ac in enumerate(sub_acs[first_pending_child:], start=first_pending_child):
            await self._emit_subtask_event(
                execution_id=execution_id,
                ac_index=ac_index,
                sub_task_index=idx + 1,
                sub_task_content=sub_ac,
                status="pending",
                node_identity=node_identity.child(idx),
            )

        self._console.print(f"    [green]Starting {len(sub_acs)} Sub-ACs sequentially...[/green]")
        sub_results: list[ACExecutionResult | BaseException | None] = [None] * len(sub_acs)
        if resume_state is not None:
            sub_results[:first_pending_child] = list(resume_state.completed_children)
        sub_depth = depth + 1
        paused_at: int | None = None
        for idx, sub_ac in enumerate(sub_acs[first_pending_child:], start=first_pending_child):
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
                sub_results[idx] = await _invoke_execution_authority_entry(
                    self,
                    _FOUNDATION_A_ENTRY_EXECUTE_SINGLE_AC,
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
                    investment_spec=investment_spec,
                    decomposition_trustworthy=decision.trustworthy,
                    semantic_ac_key=semantic_ac_key,
                    expected_resume_dispatch_id=(
                        resume_state.paused_dispatch_id
                        if resume_state is not None and idx == first_pending_child
                        else None
                    ),
                    expected_resume_capsule_fingerprint=(
                        resume_state.paused_capsule_fingerprint
                        if resume_state is not None and idx == first_pending_child
                        else None
                    ),
                    expected_resume_runtime_scope_id=(
                        resume_state.paused_runtime_scope_id
                        if resume_state is not None and idx == first_pending_child
                        else None
                    ),
                )
                if isinstance(sub_results[idx], ACExecutionResult) and _has_usage_limit_pause(
                    sub_results[idx]
                ):
                    paused_at = idx
                    break
            except BaseException as exc:
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise
                sub_results[idx] = exc

        materialized_results = sub_results if paused_at is None else sub_results[: paused_at + 1]
        final_sub_results: list[ACExecutionResult] = []
        for idx, result in enumerate(materialized_results):
            if isinstance(result, BaseException) or result is None:
                final_sub_results.append(
                    ACExecutionResult(
                        ac_index=ac_index * 100 + idx,
                        ac_content=sub_acs[idx],
                        success=False,
                        error=(
                            str(result)
                            if isinstance(result, BaseException)
                            else "Task cancelled or produced no result"
                        ),
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
        for idx, result in enumerate(final_sub_results):
            await self._emit_subtask_event(
                execution_id=execution_id,
                ac_index=ac_index,
                sub_task_index=idx + 1,
                sub_task_content=sub_acs[idx],
                status=(
                    "paused"
                    if _has_usage_limit_pause(result)
                    else "completed"
                    if result.success
                    else "failed"
                ),
                node_identity=node_identity.child(idx),
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
            decomposition_decision=decision,
        )

    def _build_decomposition_trace_summary(
        self,
        *,
        result: ACExecutionResult,
        ac_spec: AcceptanceCriterionSpec | None,
    ) -> DecompositionTraceSummary:
        """Project one failed attempt into typed counts and enums only."""
        from ouroboros.orchestrator.failure_taxonomy import FailureClass

        verdict = result.atomic_verifier_verdict
        attempted_tool_count = sum(
            1
            for message in result.messages
            if isinstance(message.tool_name, str) and bool(message.tool_name.strip())
        )
        evidence_field_count = (
            len(result.typed_evidence.data)
            if result.typed_evidence is not None and type(result.typed_evidence.data) is dict
            else 0
        )
        verified_artifact_count = 0
        remaining_artifact_count = 0
        if ac_spec is not None and ac_spec.expected_artifacts:
            cwd = Path(self._task_cwd or self._adapter.working_directory or os.getcwd())
            for artifact in ac_spec.expected_artifacts[:8]:
                target = Path(artifact)
                if not target.is_absolute():
                    target = cwd / target
                if target.exists():
                    verified_artifact_count += 1
                else:
                    remaining_artifact_count += 1

        try:
            failure_class = (
                FailureClass(verdict.failure_class).value
                if verdict is not None and verdict.failure_class is not None
                else "UNKNOWN"
            )
        except ValueError:
            failure_class = "UNKNOWN"
        retry_admission = (
            verdict.retry_admission.value
            if verdict is not None and isinstance(verdict.retry_admission, RetryAdmission)
            else "UNKNOWN"
        )
        lines = [
            f"attempt_message_count={len(result.messages)}",
            f"attempted_tool_count={attempted_tool_count}",
            f"typed_evidence_present={result.typed_evidence is not None}",
            f"evidence_field_count={evidence_field_count}",
            f"verified_artifact_count={verified_artifact_count}",
            f"remaining_artifact_count={remaining_artifact_count}",
            f"failure_class={failure_class}",
            f"retry_admission={retry_admission}",
            f"verifier_reason_count={len(verdict.reasons) if verdict is not None else 0}",
            f"failure_detail_present={bool(result.error or result.final_message)}",
        ]
        if ac_spec is not None:
            lines.append(f"verify_command_present={bool(ac_spec.verify_command)}")
            lines.append(f"output_assertion_present={bool(ac_spec.output_assertion)}")
        return summarize_decomposition_trace("\n".join(lines))

    async def _dispatch_decomposition_prompt(
        self,
        *,
        prompt: str,
        system_prompt: str,
        independent_session: bool = False,
    ) -> str:
        """Run one bounded, tracked, tool-free decomposition-policy request."""
        self._announce_param_degradations(system_prompt=system_prompt, tools=[])
        _invoke_execution_authority_guard(self)
        await _invoke_execution_authority_entry(
            self,
            _FOUNDATION_A_ENTRY_AWAIT_DISPATCH_RATE_BUDGET,
            prompt=prompt,
            system_prompt=system_prompt,
        )
        _invoke_execution_authority_guard(self)
        response_text = ""
        feedback_messages: list[AgentMessage] = []
        await provider_admission.wait()
        async with self._adaptive_concurrency.slot() as permit_epoch:
            _invoke_execution_authority_guard(self)
            provider_effect_scope.enter()
            provider_completed = False
            try:
                async with asyncio.timeout(DECOMPOSITION_TIMEOUT_SECONDS):
                    async for message in tracked_agent_task(
                        self._adapter,
                        role="executor_decomposition_policy",
                        prompt=prompt,
                        tools=[],
                        system_prompt=system_prompt,
                        resume_handle=(
                            None if independent_session else self._inherited_runtime_handle
                        ),
                    ):
                        if message.is_final:
                            feedback_messages.append(message)
                            del feedback_messages[:-8]
                        if not message.content:
                            continue
                        if getattr(self._adapter, "runtime_backend", "") == "goose":
                            if message.type not in {"assistant", "result"}:
                                continue
                            if message.is_final:
                                response_text = message.content
                            else:
                                response_text += message.content
                        else:
                            response_text = message.content
                provider_completed = True
            finally:
                # Cancellation/timeout must not erase already-observed pressure.
                try:
                    with anyio.CancelScope(shield=True):
                        await adaptive_concurrency.observe_provider_messages(
                            self._adaptive_concurrency,
                            feedback_messages,
                            permit_epoch,
                            None,
                            provider_completed=provider_completed,
                            on_observation=_PROVIDER_OBSERVATION_SINK.get(),
                        )
                finally:
                    if provider_completed:
                        provider_effect_scope.complete()
        return response_text.strip()

    async def _request_bounce_classification(
        self,
        *,
        trace: DecompositionTraceSummary,
    ) -> tuple[BounceCause, bool]:
        """Ask for typed cause metadata without admitting classifier prose."""
        prompt = (
            "Classify this failed execution attempt for recovery. Use only the bounded "
            "attempt evidence below. Do not infer complexity from task length or wording. "
            "Return ONLY JSON with cause and has_remaining_scope. "
            "cause must be TOO_BIG, BAD_SPEC, ENVIRONMENT, MODEL, or UNKNOWN. TOO_BIG is "
            "allowed only when the trace shows attempted work and distinct parent scope "
            "still remaining.\n\n"
            f"## Bounded Attempt Trace\n{trace.summary}"
        )
        try:
            response = await _invoke_execution_authority_entry(
                self,
                _FOUNDATION_A_ENTRY_DISPATCH_DECOMPOSITION_PROMPT,
                prompt=prompt,
                system_prompt="You are a conservative execution-recovery classifier.",
                independent_session=True,
            )
            if len(response) > 10_000:
                raise ValueError
            match = re.search(r"\{.*\}", response, re.DOTALL)
            payload = json.loads(match.group() if match is not None else response)
            if not _mapping_has_exact_keys(payload, _BOUNCE_CLASSIFICATION_KEYS):
                raise ValueError
            assert isinstance(payload, Mapping)
            cause = BounceCause(payload["cause"])
            remaining = payload["has_remaining_scope"]
            if type(remaining) is not bool:
                raise ValueError
            return cause, remaining
        except (TimeoutError, ValueError, json.JSONDecodeError, TypeError):
            return BounceCause.UNKNOWN, False
        except _BatchInterruptedForRecoverablePause:
            raise
        except Exception as exc:
            log.warning(
                "parallel_executor.bounce_classifier.error",
                error_type=type(exc).__name__,
            )
            return BounceCause.UNKNOWN, False

    async def _classify_bounce_result(
        self,
        *,
        result: ACExecutionResult,
        trace: DecompositionTraceSummary,
    ) -> Any:
        """Combine deterministic failure routing with bounded ambiguous classification."""
        from ouroboros.orchestrator.failure_taxonomy import FailureClass, classify_bounce

        verdict = result.atomic_verifier_verdict
        failure: FailureClass | None = None
        if verdict is not None and verdict.failure_class:
            try:
                failure = FailureClass(verdict.failure_class)
            except ValueError:
                failure = None
        admission = verdict.retry_admission if verdict is not None else None
        deterministic = classify_bounce(
            failure,
            admission,
            evidence_refs=trace.evidence_refs,
            has_attempt_evidence=bool(
                result.messages or result.typed_evidence or trace.evidence_refs
            ),
        )
        if deterministic.cause is not BounceCause.UNKNOWN:
            return deterministic
        if failure not in {None, FailureClass.SCOPE_CREEP, FailureClass.STALL}:
            return deterministic

        proposed_cause, has_remaining_scope = await self._request_bounce_classification(trace=trace)
        return classify_bounce(
            failure,
            admission,
            proposed_cause=proposed_cause,
            proposed_reasons=(),
            evidence_refs=trace.evidence_refs,
            has_attempt_evidence=bool(
                result.messages or result.typed_evidence or trace.evidence_refs
            ),
            has_remaining_scope=has_remaining_scope,
        )

    async def _maybe_recover_with_bounce_decomposition(
        self,
        *,
        result: ACExecutionResult,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        seed_goal: str,
        depth: int,
        execution_id: str,
        level_contexts: list[LevelContext] | None,
        retry_attempt: int,
        execution_counters: dict[str, int] | None,
        node_identity: ExecutionNodeIdentity,
        ac_spec: AcceptanceCriterionSpec | None,
        start_time: datetime,
        semantic_ac_key: str,
        investment_spec: InvestmentSpec | None = None,
    ) -> tuple[ACExecutionResult | None, DecompositionDecisionRecord | None]:
        """Run cause-matched bounce recovery before alternate-harness fallback."""
        if (
            self._decomposition_mode != "bounce_only"
            or result.success
            or _has_usage_limit_pause(result)
        ):
            return None, None
        previous = self._event_owned_decomposition_decisions.get(node_identity.node_id)
        if self._decomposition_decisions.get(node_identity.node_id) != previous:
            raise RuntimeError("decomposition cache lacks matching finalized-event authority")
        if previous is not None and previous.source is DecompositionSource.BOUNCE:
            return None, previous

        trace = self._build_decomposition_trace_summary(result=result, ac_spec=ac_spec)
        classification = await self._classify_bounce_result(result=result, trace=trace)
        verdict = result.atomic_verifier_verdict
        retry_admission = (
            verdict.retry_admission.value
            if verdict is not None and hasattr(verdict.retry_admission, "value")
            else (str(verdict.retry_admission) if verdict is not None else None)
        )
        await self._event_emitter.emit_bounce_classified(
            execution_id=execution_id or session_id,
            session_id=session_id,
            node_identity=node_identity,
            cause=classification.cause.value,
            rationale=classification.rationale,
            failure_class=verdict.failure_class if verdict is not None else None,
            retry_admission=retry_admission,
            evidence_refs=classification.evidence_refs,
            trace_summary=trace.summary,
        )
        if not classification.allows_decomposition:
            return None, None

        if node_identity.node_id in self._pending_bounce_decompositions:
            raise RuntimeError("a node cannot own two pending TOO_BIG bounce phases")
        pending = _DurableBounceReplayState(
            cause=classification.cause,
            rationale=classification.rationale,
            failure_class=verdict.failure_class if verdict is not None else None,
            retry_admission=retry_admission,
            evidence_refs=classification.evidence_refs,
            trace_summary=trace.summary,
            event_key=None,
        )
        self._pending_bounce_decompositions[node_identity.node_id] = pending
        return await self._continue_bounce_decomposition(
            pending=pending,
            ac_index=ac_index,
            ac_content=ac_content,
            session_id=session_id,
            tools=tools,
            tool_catalog=tool_catalog,
            system_prompt=system_prompt,
            seed_goal=seed_goal,
            depth=depth,
            execution_id=execution_id,
            level_contexts=level_contexts,
            retry_attempt=retry_attempt,
            execution_counters=execution_counters,
            node_identity=node_identity,
            ac_spec=ac_spec,
            start_time=start_time,
            semantic_ac_key=semantic_ac_key,
            investment_spec=investment_spec,
        )

    async def _continue_bounce_decomposition(
        self,
        *,
        pending: _DurableBounceReplayState,
        ac_index: int,
        ac_content: str,
        session_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        seed_goal: str,
        depth: int,
        execution_id: str,
        level_contexts: list[LevelContext] | None,
        retry_attempt: int,
        execution_counters: dict[str, int] | None,
        node_identity: ExecutionNodeIdentity,
        ac_spec: AcceptanceCriterionSpec | None,
        start_time: datetime,
        semantic_ac_key: str,
        investment_spec: InvestmentSpec | None = None,
    ) -> tuple[ACExecutionResult | None, DecompositionDecisionRecord]:
        """Resume the post-classification phase without repeating the parent attempt."""

        if (
            pending.cause is not BounceCause.TOO_BIG
            or self._pending_bounce_decompositions.get(node_identity.node_id) != pending
        ):
            raise RuntimeError("decomposition requires the node's durable TOO_BIG phase")

        if depth >= self._max_decomposition_depth:
            decision = await self._finalize_decomposition_decision(
                decision=DecompositionDecisionRecord(
                    node_id=node_identity.node_id,
                    source=DecompositionSource.BOUNCE,
                    disposition=DecompositionDisposition.ESCALATED,
                    cause=BounceCause.TOO_BIG,
                    reasons=("decomposition_depth_cap", pending.rationale),
                    evidence_refs=pending.evidence_refs,
                    compromise_reason="depth_cap_forced_atomic",
                ),
                node_identity=node_identity,
                execution_id=execution_id or session_id,
                session_id=session_id,
            )
            return None, decision

        decision = await self._try_decompose_ac(
            ac_content=ac_content,
            ac_index=ac_index,
            seed_goal=seed_goal,
            tools=tools,
            system_prompt=system_prompt,
            node_identity=node_identity,
            session_id=session_id,
            execution_id=execution_id,
            retry_attempt=retry_attempt,
            depth=depth,
            ac_spec=ac_spec,
            source=DecompositionSource.BOUNCE,
            cause=BounceCause.TOO_BIG,
            trace_summary=pending.trace_summary,
            evidence_refs=pending.evidence_refs,
        )
        decision = self._coerce_decomposition_decision(
            decision,
            node_identity=node_identity,
            source=DecompositionSource.BOUNCE,
            cause=BounceCause.TOO_BIG,
        )
        decision = await self._finalize_decomposition_decision(
            decision=decision,
            node_identity=node_identity,
            execution_id=execution_id or session_id,
            session_id=session_id,
        )
        if (
            decision.disposition is DecompositionDisposition.SPLIT
            and decision.trustworthy is True
            and len(decision.children) >= MIN_SUB_ACS
        ):
            recovered = await self._execute_decomposition_children(
                decision=decision,
                ac_index=ac_index,
                ac_content=ac_content,
                session_id=session_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                seed_goal=seed_goal,
                depth=depth,
                execution_id=execution_id,
                level_contexts=level_contexts,
                retry_attempt=retry_attempt,
                execution_counters=execution_counters,
                node_identity=node_identity,
                start_time=start_time,
                semantic_ac_key=semantic_ac_key,
                investment_spec=investment_spec,
            )
            return recovered, decision
        return None, decision

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
        investment_spec: InvestmentSpec | None = None,
        decomposition_trustworthy: bool = False,
        semantic_ac_key: str | None = None,
        route_id_override: str | None = None,
        expected_route_candidate: RouteCandidate | None = None,
        force_legacy_routing: bool = False,
        expected_resume_dispatch_id: str | None = None,
        expected_resume_capsule_fingerprint: str | None = None,
        expected_resume_runtime_scope_id: str | None = None,
    ) -> ACExecutionResult:
        """Execute a single AC via the sole recursive AC execution entry point.

        Flow:
        1. Execute the AC atomically.
        2. Classify an evidence-backed failure.
        3. Only a ``TOO_BIG`` bounce may enter verified decomposition.

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
            investment_spec: The top-level AC's investment authority. Recursive
                children inherit it because they jointly discharge the parent AC.
                This is separate from ``ac_spec`` so the parent's success contract
                is not copied into child prompts.
            decomposition_trustworthy: Explicit deterministic trust for this unit's
                decomposition. Defaults fail closed; only the verified-MECE producer
                may authorize trusted-child routing.

        Returns:
            ACExecutionResult for this AC.
        """
        start_time = datetime.now(UTC)
        execution_context_id = execution_id or session_id
        semantic_ac_key = semantic_ac_key or (
            ac_spec.semantic_ac_key
            if ac_spec is not None and ac_spec.semantic_ac_key is not None
            else derive_semantic_ac_key(ac_spec or ac_content)
        )
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

        cached_decision = self._decomposition_decisions.get(node_identity.node_id)
        node_decision = self._event_owned_decomposition_decisions.get(node_identity.node_id)
        if cached_decision != node_decision:
            raise RuntimeError("decomposition cache lacks matching finalized-event authority")
        pending_bounce = self._pending_bounce_decompositions.get(node_identity.node_id)
        if pending_bounce is not None:
            recovered, node_decision = await self._continue_bounce_decomposition(
                pending=pending_bounce,
                ac_index=ac_index,
                ac_content=ac_content,
                session_id=session_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                seed_goal=seed_goal,
                depth=depth,
                execution_id=execution_id,
                level_contexts=level_contexts,
                retry_attempt=retry_attempt,
                execution_counters=execution_counters,
                node_identity=node_identity,
                ac_spec=ac_spec,
                start_time=start_time,
                semantic_ac_key=semantic_ac_key,
                investment_spec=investment_spec,
            )
            if recovered is not None:
                return recovered
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                error="Durable TOO_BIG recovery completed without an admissible split.",
                final_message="Durable TOO_BIG recovery completed without an admissible split.",
                retry_attempt=retry_attempt,
                outcome=ACExecutionOutcome.FAILED,
                decomposition_decision=node_decision,
                decomposition_depth_warning=(
                    node_decision.compromise_reason == "depth_cap_forced_atomic"
                ),
            )
        durable_route_proves_atomic = (
            expected_route_candidate is not None or expected_resume_dispatch_id is not None
        ) and not is_sub_ac
        if durable_route_proves_atomic:
            if (
                node_decision is not None
                and node_decision.disposition is DecompositionDisposition.SPLIT
            ):
                raise RuntimeError(
                    "durable atomic route state contradicts the decomposition decision"
                )

        if (
            node_decision is not None
            and node_decision.disposition is DecompositionDisposition.SPLIT
            and len(node_decision.children) >= MIN_SUB_ACS
            and node_decision.trustworthy is True
        ):
            if depth >= self._max_decomposition_depth:
                raise RuntimeError("durable decomposition exceeds the replayable depth boundary")
            return await self._execute_decomposition_children(
                decision=node_decision,
                ac_index=ac_index,
                ac_content=ac_content,
                session_id=session_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                seed_goal=seed_goal,
                depth=depth,
                execution_id=execution_id,
                level_contexts=level_contexts,
                retry_attempt=retry_attempt,
                execution_counters=execution_counters,
                node_identity=node_identity,
                start_time=start_time,
                semantic_ac_key=semantic_ac_key,
                investment_spec=investment_spec,
            )

        decomposition_depth_warning = False

        def _finalize_node_result(result: ACExecutionResult) -> ACExecutionResult:
            return replace(
                result,
                decomposition_decision=node_decision,
                decomposition_depth_warning=(
                    result.decomposition_depth_warning or decomposition_depth_warning
                ),
            )

        # Stall recovery belongs to atomic leaves only. Once this method decides
        # to execute atomically, it can retry the leaf without re-running the
        # decomposition/dispatch branch above.
        # Routing D currently owns only top-level atomic ACs.  A decomposed
        # child has no durable child-level observation/replay owner yet, so it
        # must retain the established legacy retry/model-routing path even
        # when its decomposition attestation is trustworthy.
        bounded_route_recovery_enabled = (
            self._bounded_route_escalation_enabled and not is_sub_ac and not force_legacy_routing
        )
        atomic_retry_attempt = retry_attempt
        stall_retry_budget = 0 if bounded_route_recovery_enabled else MAX_STALL_RETRIES
        max_attempts = retry_attempt + stall_retry_budget + 1
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
            "investment_spec": investment_spec,
            "decomposition_trustworthy": decomposition_trustworthy,
            "semantic_ac_key": semantic_ac_key,
            "route_id_override": route_id_override,
            "expected_route_candidate": expected_route_candidate,
            "force_legacy_routing": force_legacy_routing,
        }
        while True:
            atomic_result = await _invoke_execution_authority_entry(
                self,
                _FOUNDATION_A_ENTRY_EXECUTE_ATOMIC_AC,
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
                investment_spec=investment_spec,
                decomposition_trustworthy=decomposition_trustworthy,
                semantic_ac_key=semantic_ac_key,
                route_id_override=route_id_override,
                expected_route_candidate=expected_route_candidate,
                force_legacy_routing=force_legacy_routing,
                expected_resume_dispatch_id=expected_resume_dispatch_id,
                expected_resume_capsule_fingerprint=expected_resume_capsule_fingerprint,
                expected_resume_runtime_scope_id=expected_resume_runtime_scope_id,
            )
            if atomic_result.error != _STALL_SENTINEL:
                if atomic_result.outcome in {
                    ACExecutionOutcome.BLOCKED,
                    ACExecutionOutcome.INVALID,
                }:
                    # Admission/authority failures are terminal before recovery.
                    # Bounce classification and alternate-harness redispatch are
                    # provider effects too; neither may bypass a fail-closed route.
                    return _finalize_node_result(atomic_result)
                if not atomic_result.success and not bounded_route_recovery_enabled:
                    (
                        bounce_result,
                        bounce_decision,
                    ) = await self._maybe_recover_with_bounce_decomposition(
                        result=atomic_result,
                        ac_index=ac_index,
                        ac_content=ac_content,
                        session_id=session_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        seed_goal=seed_goal,
                        depth=depth,
                        execution_id=execution_id,
                        level_contexts=level_contexts,
                        retry_attempt=atomic_retry_attempt,
                        execution_counters=execution_counters,
                        node_identity=node_identity,
                        ac_spec=ac_spec,
                        start_time=start_time,
                        semantic_ac_key=semantic_ac_key,
                        investment_spec=investment_spec,
                    )
                    if bounce_decision is not None:
                        node_decision = bounce_decision
                        if bounce_decision.compromise_reason == "depth_cap_forced_atomic":
                            decomposition_depth_warning = True
                    if bounce_result is not None:
                        return _finalize_node_result(bounce_result)
                if (
                    not atomic_result.success
                    and same_runtime_budget_exhausted
                    and not bounded_route_recovery_enabled
                ):
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
                return _finalize_node_result(atomic_result)

            runtime_identity = build_ac_runtime_identity(
                ac_index,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                retry_attempt=atomic_retry_attempt,
            )
            should_retry = atomic_retry_attempt - retry_attempt < stall_retry_budget
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
                if bounded_route_recovery_enabled:
                    from ouroboros.orchestrator.failure_taxonomy import FailureClass

                    failed_result = replace(
                        failed_result,
                        atomic_verifier_verdict=VerifierVerdict(
                            passed=False,
                            reasons=("atomic route stalled",),
                            failure_class=FailureClass.STALL.value,
                        ),
                    )
                bounce_result = None
                bounce_decision = None
                if not bounded_route_recovery_enabled:
                    (
                        bounce_result,
                        bounce_decision,
                    ) = await self._maybe_recover_with_bounce_decomposition(
                        result=failed_result,
                        ac_index=ac_index,
                        ac_content=ac_content,
                        session_id=session_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        seed_goal=seed_goal,
                        depth=depth,
                        execution_id=execution_id,
                        level_contexts=level_contexts,
                        retry_attempt=atomic_retry_attempt,
                        execution_counters=execution_counters,
                        node_identity=node_identity,
                        ac_spec=ac_spec,
                        start_time=start_time,
                        semantic_ac_key=semantic_ac_key,
                        investment_spec=investment_spec,
                    )
                if bounce_decision is not None:
                    node_decision = bounce_decision
                    if bounce_decision.compromise_reason == "depth_cap_forced_atomic":
                        decomposition_depth_warning = True
                if bounce_result is not None:
                    return _finalize_node_result(bounce_result)
                # An abandoned stall is re-dispatched by the batch-level
                # same-runtime retry loop (its error is no longer the stall
                # sentinel), so only try a cross-harness redispatch once that
                # budget is also spent — i.e. this is the final same-runtime
                # attempt — before the AC is finally marked FAILED.
                if same_runtime_budget_exhausted and not bounded_route_recovery_enabled:
                    alt_result = await self._maybe_redispatch_alt_harness(
                        result=failed_result,
                        execution_context_id=execution_context_id,
                        rerun_kwargs=alt_rerun_kwargs,
                        atomic_retry_attempt=atomic_retry_attempt,
                        stall_retries_exhausted=True,
                    )
                    if alt_result is not None:
                        failed_result = alt_result
                return _finalize_node_result(failed_result)

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
        root_ac_index = (
            rerun_kwargs["node_identity"].root_ac_index
            if isinstance(rerun_kwargs.get("node_identity"), ExecutionNodeIdentity)
            else int(rerun_kwargs["ac_index"])
        )
        if not decision.should_redispatch or decision.to_backend is None:
            self._alt_harness_status_by_root.setdefault(
                root_ac_index,
                "not_attempted"
                if decision.reason in {"disabled_by_config", "no_alternative_runtime"}
                else "not_eligible",
            )
            return None

        # Consume the one-per-AC cap up front so a re-run that itself fails does
        # not trigger a second harness hop.
        self._alt_harness_redispatched_acs.add(ac_key)
        self._alt_harness_status_by_root[root_ac_index] = "not_attempted"
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
            self._alt_harness_status_by_root[root_ac_index] = "failed"
            log.warning(
                "parallel_executor.alt_harness_redispatch_failed",
                to_backend=decision.to_backend,
                ac_index=rerun_kwargs["ac_index"],
                error=str(exc),
            )
            return None
        if alt_result is None:
            self._alt_harness_status_by_root[root_ac_index] = "failed"
            return None
        self._alt_harness_status_by_root[root_ac_index] = (
            "succeeded" if alt_result.success else "failed"
        )
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
        from ouroboros.orchestrator.runtime_factory import (
            create_agent_runtime,
            create_agent_runtime_async,
        )

        cwd = self._task_cwd or self._adapter.working_directory
        alt_adapter = await create_agent_runtime_async(
            create_agent_runtime,
            backend=backend,
            cwd=cwd,
            permission_mode="bypassPermissions",
        )

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
            atomic_verifier=self._authority_verifier,
            reasoning_effort=self._reasoning_effort,
            # Preserve the parent's sealed verification contract across the
            # alternate backend; defaults must not re-enable or lengthen it.
            run_verify_commands=self._run_verify_commands,
            verify_command_timeout_seconds=self._verify_command_timeout_seconds,
            # The router's backend-mismatch guard makes it inert on a different
            # backend, so passing it to the alt-harness executor is safe.
            model_router=self._model_router,
            cross_harness_redispatch=False,
            verify_shell_identity=self._verify_shell_identity,
            # The router is inert on a different backend, so the baseline resolves
            # no parent-tier model and the replay self-skips — threading the flag
            # just keeps the throwaway executor's behavior consistent.
            shadow_replay_enabled=self._shadow_replay_enabled,
            session_signal_hub=self._session_signal_hub,
        )
        return await _invoke_execution_authority_entry(
            alt_executor,
            _FOUNDATION_A_ENTRY_EXECUTE_SINGLE_AC,
            **rerun_kwargs,
            retry_attempt=retry_attempt,
        )

    @staticmethod
    def _parse_structured_decomposition(
        response_text: str,
        *,
        parent_text: str,
        min_sub_acs: int,
        max_sub_acs: int,
    ) -> tuple[DecompositionProposal | None, tuple[str, ...]]:
        """Parse a bounded generic proposal without claiming semantic trust."""
        if len(response_text) > 10_000:
            return None, ("proposal_payload_too_large",)
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        candidate = match.group() if match is not None else response_text
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            return None, ("malformed_json",)
        errors = validate_decomposition_proposal(
            payload,
            parent_text=parent_text,
            min_children=min_sub_acs,
            max_children=max_sub_acs,
        )
        if errors:
            return None, errors
        proposal = parse_decomposition_proposal(
            payload,
            parent_text=parent_text,
            min_children=min_sub_acs,
            max_children=max_sub_acs,
        )
        return proposal, (() if proposal is not None else ("invalid_structured_proposal",))

    async def _attest_decomposition_proposal(
        self,
        *,
        parent_text: str,
        proposal: DecompositionProposal,
        trace_summary: str,
        system_prompt: str,
    ) -> tuple[bool, tuple[str, ...]]:
        """Run one independent typed semantic attestation for a proposed split."""
        profile_clause = ""
        if self._execution_profile is not None:
            profile_clause = (
                f"Profile axis: {self._execution_profile.axis}.\n"
                f"Minimum unit: {self._execution_profile.min_unit}.\n"
                f"Cut signal: {self._execution_profile.cut_signal}.\n"
            )
        prompt = (
            "Independently attest this proposed decomposition. Do not modify files and do "
            "not accept the proposal merely because it declares coverage. Return ONLY JSON "
            "with boolean coverage_established, non_overlap_established, "
            "and simpler_units_established. All three booleans must be true to establish "
            "the split. Do not add explanatory fields.\n\n"
            f"{profile_clause}"
            f"Parent criterion:\n{parent_text}\n\n"
            f"Bounded attempt trace:\n{trace_summary or 'none'}\n\n"
            "Proposal:\n"
            f"{json.dumps(proposal.to_dict(), sort_keys=True)}"
        )
        try:
            response = await _invoke_execution_authority_entry(
                self,
                _FOUNDATION_A_ENTRY_DISPATCH_DECOMPOSITION_PROMPT,
                prompt=prompt,
                system_prompt=system_prompt,
                independent_session=True,
            )
            if len(response) > 10_000:
                raise ValueError
            match = re.search(r"\{.*\}", response, re.DOTALL)
            payload = json.loads(match.group() if match is not None else response)
            if not _mapping_has_exact_keys(payload, _DECOMPOSITION_ATTESTATION_KEYS):
                raise ValueError
            assert isinstance(payload, Mapping)
            checks = (
                payload.get("coverage_established"),
                payload.get("non_overlap_established"),
                payload.get("simpler_units_established"),
            )
            if any(type(value) is not bool for value in checks):
                raise ValueError
            if all(value is True for value in checks):
                return True, ("semantic_attestation_established",)
            failed_checks = tuple(
                reason
                for value, reason in zip(
                    checks,
                    (
                        "coverage_not_established",
                        "non_overlap_not_established",
                        "simpler_units_not_established",
                    ),
                    strict=True,
                )
                if value is False
            )
            return False, ("semantic_attestation_not_established", *failed_checks)
        except (TimeoutError, ValueError, json.JSONDecodeError, TypeError):
            return False, ("semantic_attestation_unparseable",)
        except Exception as exc:
            log.warning(
                "parallel_executor.decomposition.attestation_error",
                error_type=type(exc).__name__,
            )
            return False, ("semantic_attestation_runtime_error",)

    @staticmethod
    def _build_generic_decomposition_repair_prompt(
        *,
        parent_text: str,
        trace_summary: str,
        reasons: tuple[str, ...],
        min_sub_acs: int,
        max_sub_acs: int,
    ) -> str:
        """Build the single verifier-guided repair request for a generic proposal."""
        return (
            "Repair the rejected decomposition proposal exactly once. Return ONLY the "
            "structured JSON object described below; do not return ATOMIC or a string array.\n\n"
            f"Rejection reasons: {json.dumps(reasons)}\n\n"
            f"Parent criterion:\n{parent_text}\n\n"
            f"Bounded attempt trace:\n{trace_summary or 'none'}\n\n"
            f"Return {min_sub_acs}-{max_sub_acs} children in this shape:\n"
            '{"children":[{"description":"...","coverage_claims":["..."],'
            '"verification_hint":"..."}],"covers_parent":true,"rationale":"..."}'
        )

    async def _verify_generic_decomposition(
        self,
        *,
        response_text: str,
        parent_text: str,
        trace_summary: str,
        system_prompt: str,
        min_sub_acs: int,
        max_sub_acs: int,
    ) -> tuple[DecompositionProposal | None, tuple[str, ...]]:
        """Apply structural validation followed by independent semantic attestation."""
        proposal, reasons = self._parse_structured_decomposition(
            response_text,
            parent_text=parent_text,
            min_sub_acs=min_sub_acs,
            max_sub_acs=max_sub_acs,
        )
        if proposal is None:
            return None, reasons
        established, attestation_reasons = await self._attest_decomposition_proposal(
            parent_text=parent_text,
            proposal=proposal,
            trace_summary=trace_summary,
            system_prompt=system_prompt,
        )
        if not established:
            return None, attestation_reasons
        return proposal, attestation_reasons

    async def _try_decompose_ac(
        self,
        ac_content: str,
        ac_index: int,
        seed_goal: str,
        tools: list[str],
        system_prompt: str,
        node_identity: ExecutionNodeIdentity | None = None,
        session_id: str = "",
        execution_id: str = "",
        retry_attempt: int = 0,
        depth: int = 0,
        ac_spec: AcceptanceCriterionSpec | None = None,
        source: DecompositionSource = DecompositionSource.BOUNCE,
        cause: BounceCause | None = None,
        trace_summary: str = "",
        evidence_refs: tuple[str, ...] = (),
    ) -> DecompositionDecisionRecord:
        """Decompose an AC and return a versioned, fail-closed decision."""
        if source is not DecompositionSource.BOUNCE or cause is not BounceCause.TOO_BIG:
            raise RuntimeError("live decomposition requires an evidence-backed TOO_BIG bounce")
        del tools, system_prompt, retry_attempt, ac_spec
        ac_label = (
            f"AC #{node_identity.display_path}"
            if node_identity is not None
            else f"AC #{ac_index + 1}"
        )
        run_anchor = (
            execution_id
            or (node_identity.execution_context_id if node_identity is not None else "")
            or session_id
            or f"local-ac-{ac_index}"
        )
        decision_identity = node_identity or ExecutionNodeIdentity.root(
            execution_context_id=run_anchor,
            ac_index=ac_index,
        )
        decomposition_system_prompt = (
            "You are a task decomposition expert. Analyze tasks and break them down if needed."
        )
        min_sub_acs = MIN_SUB_ACS
        max_sub_acs = MAX_SUB_ACS
        profile_metadata = self._decomposition_profile_metadata()
        profile_lines = ""
        if self._execution_profile is not None:
            params = params_from_profile(
                self._execution_profile,
                min_branching=MIN_SUB_ACS,
            )
            min_sub_acs = params.min_branching
            max_sub_acs = min(params.max_branching, MAX_SUB_ACS)
            decomposition_system_prompt = build_decomposition_system_prompt(params)
            profile_lines = (
                f"Split along the axis: {params.axis}.\n"
                f"Smallest acceptable unit: {params.min_unit}.\n"
                + (
                    f"A sub-AC is small enough when: {params.cut_signal}.\n"
                    if params.cut_signal
                    else ""
                )
            )

        bounded_trace = redact_and_truncate_text(trace_summary, max_chars=1_000)
        decompose_prompt = f"""Analyze this acceptance criterion and determine if it should be decomposed.

## Goal Context
{seed_goal}

## Acceptance Criterion ({ac_label})
{ac_content}

## Instructions
Default to ATOMIC. Each sub-AC becomes a separate agent session with its own full
context, so split only when the parent bundles multiple independently valuable
outcomes that can be verified separately.
{profile_lines}
Decompose into {min_sub_acs}-{max_sub_acs} sub-ACs only when each child is simpler,
independently executable, and owns distinct parent scope. Multiple steps or files
alone are not evidence that a split is warranted.

If the AC is one focused outcome, respond with: ATOMIC

If decomposing, respond with ONLY this structured JSON object:
{{"children":[{{"description":"...","coverage_claims":["distinct parent scope"],
"verification_hint":"how this child is independently checked"}}],
"covers_parent":true,"rationale":"why the children cover the parent without overlap"}}

Respond with either ATOMIC or the structured JSON object only.
"""
        if bounded_trace:
            decompose_prompt += f"\n\n## Bounded Attempt Trace\n{bounded_trace}"

        try:
            response_text = await _invoke_execution_authority_entry(
                self,
                _FOUNDATION_A_ENTRY_DISPATCH_DECOMPOSITION_PROMPT,
                prompt=decompose_prompt,
                system_prompt=decomposition_system_prompt,
            )
            if response_text.upper().startswith("ATOMIC"):
                log.info(
                    "parallel_executor.decomposition.atomic",
                    ac_index=ac_index,
                    **profile_metadata,
                )
                return DecompositionDecisionRecord(
                    node_id=decision_identity.node_id,
                    source=source,
                    disposition=DecompositionDisposition.ESCALATED,
                    cause=cause,
                    reasons=("explicit_atomic",),
                    evidence_refs=evidence_refs,
                    compromise_reason="too_big_classifier_disagreed_with_decomposer",
                )

            proposal, proposal_reasons = await self._verify_generic_decomposition(
                response_text=response_text,
                parent_text=ac_content,
                trace_summary=bounded_trace,
                system_prompt=decomposition_system_prompt,
                min_sub_acs=min_sub_acs,
                max_sub_acs=max_sub_acs,
            )
            if proposal is not None:
                return DecompositionDecisionRecord(
                    node_id=decision_identity.node_id,
                    source=source,
                    disposition=DecompositionDisposition.SPLIT,
                    cause=cause,
                    reasons=proposal_reasons,
                    evidence_refs=evidence_refs,
                    children=proposal.children,
                    structural_status=StructuralCheckStatus.PASSED,
                    semantic_status=SemanticAttestationStatus.ESTABLISHED,
                    trustworthy=True,
                )

            repair_prompt = self._build_generic_decomposition_repair_prompt(
                parent_text=ac_content,
                trace_summary=bounded_trace,
                reasons=proposal_reasons,
                min_sub_acs=min_sub_acs,
                max_sub_acs=max_sub_acs,
            )
            repaired_text = await _invoke_execution_authority_entry(
                self,
                _FOUNDATION_A_ENTRY_DISPATCH_DECOMPOSITION_PROMPT,
                prompt=repair_prompt,
                system_prompt=decomposition_system_prompt,
            )
            repaired_proposal, repaired_reasons = await self._verify_generic_decomposition(
                response_text=repaired_text,
                parent_text=ac_content,
                trace_summary=bounded_trace,
                system_prompt=decomposition_system_prompt,
                min_sub_acs=min_sub_acs,
                max_sub_acs=max_sub_acs,
            )
            if repaired_proposal is not None:
                return DecompositionDecisionRecord(
                    node_id=decision_identity.node_id,
                    source=source,
                    disposition=DecompositionDisposition.SPLIT,
                    cause=cause,
                    reasons=repaired_reasons,
                    evidence_refs=evidence_refs,
                    children=repaired_proposal.children,
                    structural_status=StructuralCheckStatus.PASSED,
                    semantic_status=SemanticAttestationStatus.ESTABLISHED,
                    repair_count=1,
                    trustworthy=True,
                )

            final_reasons = repaired_reasons or proposal_reasons
            semantic_failure = any(
                reason.startswith("semantic_attestation") for reason in final_reasons
            )
            return DecompositionDecisionRecord(
                node_id=decision_identity.node_id,
                source=source,
                disposition=DecompositionDisposition.ESCALATED,
                cause=cause,
                reasons=final_reasons,
                evidence_refs=evidence_refs,
                structural_status=(
                    StructuralCheckStatus.PASSED
                    if semantic_failure
                    else StructuralCheckStatus.FAILED
                ),
                semantic_status=(
                    SemanticAttestationStatus.NOT_ESTABLISHED
                    if semantic_failure
                    else SemanticAttestationStatus.NOT_RUN
                ),
                repair_count=1,
                compromise_reason="generic_decomposition_repair_failed",
            )
        except TimeoutError:
            log.warning(
                "parallel_executor.decomposition.timeout",
                ac_index=ac_index,
                timeout_seconds=DECOMPOSITION_TIMEOUT_SECONDS,
                **profile_metadata,
            )
            return DecompositionDecisionRecord(
                node_id=decision_identity.node_id,
                source=source,
                disposition=DecompositionDisposition.UNKNOWN,
                cause=cause,
                reasons=("decomposition_timeout",),
                evidence_refs=evidence_refs,
            )
        except Exception as exc:
            log.warning(
                "parallel_executor.decomposition.error",
                ac_index=ac_index,
                error_type=type(exc).__name__,
                **profile_metadata,
            )
            return DecompositionDecisionRecord(
                node_id=decision_identity.node_id,
                source=source,
                disposition=DecompositionDisposition.UNKNOWN,
                cause=cause,
                reasons=("decomposition_runtime_error",),
                evidence_refs=evidence_refs,
            )

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

    async def _restore_completed_coordinator_review(
        self,
        *,
        execution_id: str,
        session_id: str,
        level: int,
        conflicts: list[FileConflict],
    ) -> CoordinatorReview | None:
        """Restore one completed coordinator effect or fail closed.

        The started/completed pair is the durable authority boundary when no
        optional checkpoint store exists.  An unmatched, duplicated, drifted,
        or malformed pair cannot authorize another coordinator provider call:
        the prior call may already have used Edit or Bash.
        """

        query_events = getattr(self._event_store, "query_events", None)
        if not callable(query_events):
            raise RuntimeError("coordinator replay requires durable event queries")
        aggregate_id = self._coordinator_aggregate_id(execution_id, level)
        try:
            started_events = await query_events(
                aggregate_id=aggregate_id,
                event_type="execution.coordinator.started",
                limit=2,
            )
            completed_events = await query_events(
                aggregate_id=aggregate_id,
                event_type="execution.coordinator.completed",
                limit=2,
            )
        except Exception as exc:
            raise RuntimeError("coordinator replay state is unreadable") from exc
        if not isinstance(started_events, list | tuple) or not isinstance(
            completed_events, list | tuple
        ):
            raise RuntimeError("coordinator replay query returned an invalid population")
        if not started_events and not completed_events:
            return None
        if not conflicts:
            raise RuntimeError("coordinator replay exists without current conflicts")
        if len(started_events) != 1 or len(completed_events) != 1:
            raise RuntimeError("coordinator replay state is incomplete or ambiguous")

        started = started_events[0]
        completed = completed_events[0]
        for event, expected_type in (
            (started, "execution.coordinator.started"),
            (completed, "execution.coordinator.completed"),
        ):
            if (
                getattr(event, "type", None) != expected_type
                or getattr(event, "aggregate_type", None) != "execution"
                or getattr(event, "aggregate_id", None) != aggregate_id
                or not isinstance(getattr(event, "data", None), Mapping)
            ):
                raise RuntimeError("coordinator replay event identity is invalid")

        runtime_scope = build_level_coordinator_runtime_scope(execution_id, level)
        expected_conflicts = tuple(conflicts)
        started_data = started.data
        try:
            validate_coordinator_started_payload(
                started_data,
                execution_id=execution_id,
                session_id=session_id,
                level_number=level,
                session_scope_id=runtime_scope.aggregate_id,
                session_state_path=runtime_scope.state_path,
                expected_conflicts=expected_conflicts,
            )
        except ValueError as exc:
            raise RuntimeError("coordinator started event drifted from the current stage") from exc

        completed_data = completed.data
        if (
            completed_data.get("execution_id") != execution_id
            or completed_data.get("session_id") != session_id
        ):
            raise RuntimeError("coordinator completed event drifted from its execution")
        try:
            return CoordinatorReview.from_artifact_payload(
                completed_data,
                level_number=level,
                expected_conflicts=expected_conflicts,
                execution_id=execution_id,
                session_id=session_id,
                session_scope_id=runtime_scope.aggregate_id,
                session_state_path=runtime_scope.state_path,
            )
        except ValueError as exc:
            raise RuntimeError("coordinator completed artifact is invalid") from exc

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
        investment_spec: InvestmentSpec | None = None,
        decomposition_trustworthy: bool = False,
        semantic_ac_key: str | None = None,
        route_id_override: str | None = None,
        expected_route_candidate: RouteCandidate | None = None,
        force_legacy_routing: bool = False,
        expected_resume_dispatch_id: str | None = None,
        expected_resume_capsule_fingerprint: str | None = None,
        expected_resume_runtime_scope_id: str | None = None,
    ) -> ACExecutionResult:
        """Execute an atomic AC directly via Claude Agent.

        Returns:
            ACExecutionResult for this AC.
        """
        ac_session_id: str | None = None
        semantic_ac_key = semantic_ac_key or derive_semantic_ac_key(ac_spec or ac_content)
        execution_context_id = execution_id or session_id
        runtime_identity = build_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
        )
        initial_model_router = self._model_router
        model_router_snapshot = (
            None
            if initial_model_router is None
            else replace(
                initial_model_router,
                tier_models=dict(initial_model_router.tier_models),
            )
        )
        route_compat_was_enabled = (
            self._route_economics is not None and model_router_snapshot is not None
        )
        # Child escalation is a deferred slice.  Until it has its own durable
        # identity and replay authority, bounded routing is top-level atomic
        # only; children continue through the legacy router.
        bounded_route_attempt_enabled = (
            self._bounded_route_escalation_enabled and not is_sub_ac and not force_legacy_routing
        )
        durable_route_projection = self._build_route_compat_projection(
            model_router=model_router_snapshot,
            effort=None,
        )
        try:
            dispatch_contract: dict[str, object] = {
                "backend": getattr(self._adapter, "runtime_backend", None),
                "tools": list(tools),
                # The allow-list is only a projection of the provider
                # contract. Fingerprint the complete canonical catalog too,
                # so schema/source changes cannot reuse a dispatch authority
                # that merely has the same tool names.
                "tool_catalog": {
                    "present": tool_catalog is not None,
                    "entries": serialize_tool_catalog(tool_catalog or ()),
                },
                "system_prompt": system_prompt,
                "ac_content": ac_content,
                "seed_goal": seed_goal,
                "retry_prompt_extra": retry_prompt_extra,
                "sibling_acs": [
                    {"ac_index": sibling_index, "content": sibling_content}
                    for sibling_index, sibling_content in (sibling_acs or [])
                ],
                "level_context_prompt": build_context_prompt(level_contexts or []),
            }
            execution_policy: dict[str, object] = {
                "retry_attempt": retry_attempt,
                "is_sub_ac": is_sub_ac,
                "decomposition_trustworthy": decomposition_trustworthy,
                "base_reasoning_effort": self._reasoning_effort,
                "model_routing": serialize_model_router(model_router_snapshot),
                "route_compat": serialize_route_compat_contract(durable_route_projection),
                "route_id_override": route_id_override,
                "expected_route_candidate": (
                    expected_route_candidate.to_contract_data()
                    if expected_route_candidate is not None
                    else None
                ),
                "execution_profile": (
                    self._execution_profile.model_dump(mode="json")
                    if self._execution_profile is not None
                    else None
                ),
                "run_verify_commands": self._run_verify_commands,
                "fat_harness_mode": self._fat_harness_mode,
                "investment_spec": (
                    investment_spec.model_dump(mode="json") if investment_spec is not None else None
                ),
            }
            request_authority_digest = build_ac_dispatch_request_digest(
                dispatch_contract=dispatch_contract,
                execution_policy=execution_policy,
            )
            capsule = compile_ac_execution_capsule(
                runtime_identity=runtime_identity,
                execution_id=execution_context_id,
                semantic_ac_key=semantic_ac_key,
                workspace=(
                    self._task_cwd
                    or getattr(self._adapter, "working_directory", None)
                    or os.getcwd()
                ),
                authority_scope=(
                    build_ac_dispatch_authority_scope(
                        base_scope=self.execution_authority.fingerprint,
                        dispatch_contract=dispatch_contract,
                        execution_policy=execution_policy,
                    )
                ),
                seed_goal=seed_goal,
                ac_content=ac_content,
                ac_spec=ac_spec,
                level_contexts=tuple(level_contexts or ()),
            )
        except UnmaterializableSuccessContractError as exc:
            duration = (datetime.now(UTC) - start_time).total_seconds()
            log.warning(
                "parallel_executor.ac.admission_rejected",
                ac_index=ac_index,
                session_id=session_id,
                execution_id=execution_context_id,
                error_code=exc.code,
                artifact=exc.artifact,
                reason=exc.reason,
            )
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                error=str(exc),
                final_message=str(exc),
                duration_seconds=duration,
                session_id=session_id,
                retry_attempt=retry_attempt,
                depth=depth,
                outcome=ACExecutionOutcome.INVALID,
            )
        if (
            expected_resume_capsule_fingerprint is not None
            and capsule.fingerprint != expected_resume_capsule_fingerprint
        ):
            raise RuntimeError("paused AC capsule fingerprint drifted")

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
            capsule=capsule,
        )
        prompt = prompt_bundle.prompt
        label = prompt_bundle.label
        indent = prompt_bundle.indent
        context_governance_audit = prompt_bundle.context_governance_audit

        messages: list[AgentMessage] = []
        final_message = ""
        success = False
        clear_cached_runtime_handle = False
        persisted_runtime_handle = await self._load_persisted_ac_runtime_handle(
            ac_index,
            execution_context_id=execution_context_id,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            retry_attempt=retry_attempt,
            expected_capsule_fingerprint=capsule.fingerprint,
            expected_process_local_resume_nonce=self._process_local_resume_nonce,
        )
        if expected_resume_dispatch_id is not None:
            persisted_metadata = (
                persisted_runtime_handle.metadata if persisted_runtime_handle is not None else {}
            )
            if (
                persisted_metadata.get("ac_dispatch_id") != expected_resume_dispatch_id
                or persisted_metadata.get("session_scope_id") != expected_resume_runtime_scope_id
                or persisted_metadata.get("ac_capsule_fingerprint")
                != expected_resume_capsule_fingerprint
            ):
                raise RuntimeError("paused AC provider boundary drifted")
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
        runtime_handle = bind_capsule_to_runtime_handle(
            capsule,
            runtime_handle,
            restored_same_attempt=(
                persisted_runtime_handle is not None
                or self._is_resumable_runtime_handle(runtime_handle)
            ),
            expected_backend=getattr(self._adapter, "runtime_backend", None),
            expected_approval_mode=getattr(self._adapter, "permission_mode", None),
        )
        session_origin = (
            "restored_same_attempt"
            if persisted_runtime_handle is not None
            or self._is_resumable_runtime_handle(runtime_handle)
            else "fresh"
        )
        await self._event_emitter.emit_ac_capsule_compiled(
            runtime_identity=runtime_identity,
            session_id=session_id,
            capsule=capsule,
            session_origin=session_origin,
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
        await _invoke_execution_authority_entry(
            self,
            _FOUNDATION_A_ENTRY_AWAIT_DISPATCH_RATE_BUDGET,
            prompt=prompt,
            system_prompt=system_prompt,
        )

        investment_assessment = assess_investment(investment_spec)
        await self._event_emitter.emit_investment_assessed(
            runtime_identity=runtime_identity,
            execution_id=execution_context_id,
            session_id=session_id,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            assessment=investment_assessment.to_event_data(),
            runtime_backend=getattr(self._adapter, "runtime_backend", None),
        )

        # Lay the executor on the capability contract: decide the effort level for
        # this unit (a decomposed child inherits the parent tier unchanged; a hard AC
        # on its second-or-later retry is raised one notch) and classify how the
        # chosen runtime will honor it from its declared capability — enforced via a
        # native knob, or advised. The level is passed to execute_task; an advised
        # runtime ignores it. Dormant by default (base effort None → level None).
        # Routing D's attempt index orders a finite route set; it is not the
        # legacy same-route retry counter.  Feeding it into effort escalation
        # would mutate the successor after that successor was durably selected.
        effort_retry_attempt = 0 if bounded_route_attempt_enabled else retry_attempt
        effort_decision, execute_effort_kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=self._reasoning_effort,
            is_decomposed_child=is_sub_ac,
            retry_attempt=effort_retry_attempt,
            investment_assessment=investment_assessment,
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
                investment_assessment=investment_assessment.to_event_data(),
            )
        # execute_effort_kwargs (from resolve_execute_effort) carries
        # reasoning_effort ONLY for runtimes that enforce it; advised runtimes that
        # do not accept the parameter are never handed it.

        # Sibling of the effort routing above: decide WHICH model tier runs this
        # unit. A decomposed child drops one tier only with explicit trust; current
        # live decomposition supplies none. Retry escalation is applied afterward.
        # A profile's suggested_model_tier seeds the starting tier ONLY when it is
        # something other than the shipped default MEDIUM ("no opinion"); MEDIUM
        # leaves precedence with the router's own base/child logic and any explicit
        # model_tier arg. Dormant by default (router None → no model override).
        suggested_tier = self._profile_suggested_tier()
        legacy_model_decision, execute_model_kwargs = resolve_execute_model(
            self._adapter,
            router=model_router_snapshot,
            is_decomposed_child=is_sub_ac,
            decomposition_trustworthy=decomposition_trustworthy,
            retry_attempt=retry_attempt,
            suggested_tier=suggested_tier,
        )
        model_decision = legacy_model_decision
        projection = (
            None
            if durable_route_projection is None
            else replace(
                durable_route_projection,
                registry=replace(
                    durable_route_projection.registry,
                    candidates=tuple(
                        replace(candidate, effort=effort_decision.level)
                        for candidate in durable_route_projection.registry.candidates
                    ),
                ),
                effort=effort_decision.level,
            )
        )
        route_admission = None
        if bounded_route_attempt_enabled:
            # Routing D deliberately bypasses the legacy base-tier/retry-count
            # choice.  The Kernel selects the cheapest eligible candidate on
            # the first attempt; later attempts pin the exact next route chosen
            # by the bounded escalation state machine.
            route_admission = admit_compat_escalation_route(
                projection,
                effort=effort_decision.level,
                route_id=route_id_override,
            )
            selected_route = route_admission.selected
            if expected_route_candidate is not None and selected_route != expected_route_candidate:
                duration = (datetime.now(UTC) - start_time).total_seconds()
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error="route admission blocked: durable successor snapshot drifted",
                    duration_seconds=duration,
                    session_id=session_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                    outcome=ACExecutionOutcome.BLOCKED,
                )
            model_support = getattr(
                getattr(self._adapter, "capabilities", None),
                "model_override_support",
                ParamSupport.IGNORED,
            )
            if selected_route is not None and projection is not None:
                selected_tier = next(
                    (
                        tier
                        for tier, route_id in projection.tier_route_ids
                        if route_id == selected_route.route_id
                    ),
                    None,
                )
                model_decision = ModelDecision(
                    tier=selected_tier,
                    model=selected_route.model,
                    mode=(
                        MODEL_MODE_ENFORCED if model_support is ParamSupport.NATIVE else "advised"
                    ),
                )
                execute_model_kwargs = (
                    {"model": selected_route.model} if model_support is ParamSupport.NATIVE else {}
                )
            if not route_admission.admitted or not model_decision.is_enforced:
                duration = (datetime.now(UTC) - start_time).total_seconds()
                reason = (
                    route_admission.reason
                    if not route_admission.admitted
                    else "runtime cannot enforce the admitted model"
                )
                log.warning(
                    "parallel_executor.ac.route_admission_blocked",
                    ac_index=ac_index,
                    runtime_backend=getattr(self._adapter, "runtime_backend", None),
                    reason=reason,
                )
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error=f"route admission blocked: {reason}",
                    duration_seconds=duration,
                    session_id=session_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                    outcome=ACExecutionOutcome.BLOCKED,
                )
        elif route_compat_was_enabled:
            route_admission = admit_compat_route(
                projection,
                model_decision=model_decision,
                effort=effort_decision.level,
            )
            if not route_admission.admitted:
                duration = (datetime.now(UTC) - start_time).total_seconds()
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    error=f"route admission blocked: {route_admission.reason}",
                    duration_seconds=duration,
                    session_id=session_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                    outcome=ACExecutionOutcome.BLOCKED,
                )
        else:
            # Dormant compatibility preserves the legacy model kwarg exactly.
            execute_effort_kwargs = {**execute_effort_kwargs, **execute_model_kwargs}
        observed_route_candidate = (
            route_admission.selected
            if bounded_route_attempt_enabled and route_admission is not None
            else None
        )

        if bounded_route_attempt_enabled:
            model_escalated = route_id_override is not None
        else:
            initial_model_decision, _initial_model_kwargs = resolve_execute_model(
                self._adapter,
                router=model_router_snapshot,
                is_decomposed_child=is_sub_ac,
                decomposition_trustworthy=decomposition_trustworthy,
                retry_attempt=0,
                suggested_tier=suggested_tier,
            )
            model_escalated = bool(
                retry_attempt > 0
                and model_decision.model is not None
                and initial_model_decision.model is not None
                and model_decision.model != initial_model_decision.model
            )
        if model_decision.model is not None:
            log.debug(
                "orchestrator.executor.model_routed",
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                model_tier=model_decision.tier,
                model=model_decision.model,
                model_mode=model_decision.mode,
                backend=getattr(self._adapter, "runtime_backend", None),
            )
            await self._event_emitter.emit_model_routed(
                runtime_identity=runtime_identity,
                execution_id=execution_context_id,
                session_id=session_id,
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                model_tier=model_decision.tier,
                model=model_decision.model,
                model_mode=model_decision.mode,
                retry_attempt=retry_attempt,
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
                decomposition_trustworthy=decomposition_trustworthy,
                semantic_ac_key=semantic_ac_key,
                base_model_tier=(
                    model_router_snapshot.base_tier if model_router_snapshot is not None else None
                ),
                escalation_retry_threshold=(
                    model_router_snapshot.escalation_retry_threshold
                    if model_router_snapshot is not None
                    else None
                ),
                model_escalated=model_escalated,
            )

        def _live_provider_kwargs() -> dict[str, Any] | None:
            """Revalidate carried admission against live state at provider entry."""

            if (
                self._expected_runtime_effect_capabilities is not None
                and runtime_effect_capabilities_contract(self._adapter)
                != self._expected_runtime_effect_capabilities
            ):
                return None
            if route_admission is None:
                return dict(execute_effort_kwargs)
            if self._model_router != model_router_snapshot:
                return None
            live_effort_decision, live_effort_kwargs = resolve_execute_effort(
                self._adapter,
                base_effort=self._reasoning_effort,
                is_decomposed_child=is_sub_ac,
                retry_attempt=effort_retry_attempt,
                investment_assessment=investment_assessment,
            )
            if live_effort_decision != effort_decision:
                return None
            live_projection = self._build_route_compat_projection(
                model_router=self._model_router,
                effort=live_effort_decision.level,
            )
            if bounded_route_attempt_enabled:
                if not validate_compat_escalation_admission(
                    live_projection,
                    route_admission,
                    effort=live_effort_decision.level,
                    route_id=route_id_override,
                ):
                    return None
                selected = route_admission.selected
                model_support = getattr(
                    getattr(self._adapter, "capabilities", None),
                    "model_override_support",
                    ParamSupport.IGNORED,
                )
                if (
                    selected is None
                    or (
                        expected_route_candidate is not None
                        and selected != expected_route_candidate
                    )
                    or model_support is not ParamSupport.NATIVE
                    or selected.model != model_decision.model
                ):
                    return None
                return {**live_effort_kwargs, "model": selected.model}
            live_model_decision, _live_model_kwargs = resolve_execute_model(
                self._adapter,
                router=self._model_router,
                is_decomposed_child=is_sub_ac,
                decomposition_trustworthy=decomposition_trustworthy,
                retry_attempt=retry_attempt,
                suggested_tier=suggested_tier,
            )
            if live_model_decision != model_decision or not validate_compat_admission(
                live_projection,
                route_admission,
                model_decision=model_decision,
                effort=live_effort_decision.level,
            ):
                return None
            live_model_kwargs = admitted_execute_model_kwargs(
                route_admission,
                model_decision=model_decision,
                projection=live_projection,
                effort=live_effort_decision.level,
            )
            return {**live_effort_kwargs, **live_model_kwargs}

        def _route_drift_blocked_result() -> ACExecutionResult:
            duration = (datetime.now(UTC) - start_time).total_seconds()
            log.warning(
                "parallel_executor.ac.route_admission_stale",
                ac_index=ac_index,
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
            )
            return ACExecutionResult(
                ac_index=ac_index,
                ac_content=ac_content,
                success=False,
                messages=tuple(messages),
                error="route admission blocked: live route state changed before provider entry",
                duration_seconds=duration,
                session_id=session_id,
                retry_attempt=retry_attempt,
                depth=depth,
                outcome=ACExecutionOutcome.BLOCKED,
                route_candidate=observed_route_candidate,
            )

        # Runtime dispatch + streaming/heartbeat consumption. The dispatcher owns
        # the stall-scoped CancelScope and the per-message loop; it mutates
        # ``dispatch_state`` in place (including on the exception path) so the
        # ``except``/``finally`` below observe the latest runtime handle, session
        # id, and partial message list. Created before the ``try`` so it is always
        # bound for the ``except``/``finally``.
        #
        # When the opt-in shadow baseline is armed, freeze the live filesystem
        # NOW — immediately before the real child dispatch. Recreating isolation
        # after the child succeeds would compare against a different input state
        # (or, with a detached worktree, silently lose all uncommitted/untracked
        # context). The ExitStack stays open through the replay and is closed on
        # every success/failure/stall exit in the outer finally below.
        shadow_snapshot_stack = contextlib.ExitStack()
        shadow_snapshot_cwd: str | None = None
        if self._shadow_replay_enabled and is_sub_ac:
            try:
                snapshot_source = self._task_cwd or getattr(
                    self._adapter, "working_directory", None
                )
                if isinstance(snapshot_source, (str, os.PathLike)):
                    shadow_snapshot_cwd = shadow_snapshot_stack.enter_context(
                        isolated_workspace(os.fspath(snapshot_source))
                    )
            except Exception as exc:
                # Experiment-only preparation must never prevent the live child.
                log.warning(
                    "parallel_executor.ac.shadow_replay.snapshot_prepare_failed",
                    ac_id=runtime_identity.ac_id,
                    error=str(exc),
                )
                with contextlib.suppress(Exception):
                    shadow_snapshot_stack.close()
                shadow_snapshot_stack = contextlib.ExitStack()
        dispatch_id = uuid4().hex
        previous_dispatch_id = None
        if runtime_handle is not None:
            previous_value = runtime_handle.metadata.get("ac_dispatch_id")
            if isinstance(previous_value, str) and previous_value:
                previous_dispatch_id = previous_value
            runtime_metadata = dict(runtime_handle.metadata)
            runtime_metadata["ac_dispatch_id"] = dispatch_id
            runtime_metadata["ac_capsule_fingerprint"] = capsule.fingerprint
            runtime_metadata["ac_session_origin"] = session_origin
            runtime_handle = replace(runtime_handle, metadata=runtime_metadata)
        await self._event_emitter.emit_ac_attempt_dispatched(
            runtime_identity=runtime_identity,
            dispatch_id=dispatch_id,
            previous_dispatch_id=previous_dispatch_id,
            execution_id=execution_context_id,
            session_id=session_id,
            capsule_fingerprint=capsule.fingerprint,
            request_authority_digest=request_authority_digest,
            session_origin=session_origin,
            runtime_handle=runtime_handle,
        )
        if runtime_handle is not None:
            # Cache only after the dispatch transition is durable.  This still
            # occurs before provider entry, so a runtime that returns a fresh
            # handle can inherit the capsule bindings, while an append failure
            # cannot leave an undurable dispatch ID as the next predecessor.
            runtime_handle = self._remember_ac_runtime_handle(
                ac_index,
                runtime_handle,
                execution_context_id=execution_context_id,
                is_sub_ac=is_sub_ac,
                parent_ac_index=parent_ac_index,
                sub_ac_index=sub_ac_index,
                node_identity=node_identity,
                retry_attempt=retry_attempt,
            )
        dispatch_state = LeafDispatchState(messages=messages, runtime_handle=runtime_handle)
        active_dispatch_id = dispatch_id
        sealed_dispatch_ids: set[str] = set()
        provider_effect_active = False

        async def _seal_dispatch(sealed_id: str, *, reason: str, replayable: bool = False) -> None:
            """Seal one provider boundary at most once."""
            if sealed_id in sealed_dispatch_ids:
                return
            # Poison unsafe boundaries before the durable append so an append
            # failure cannot expose a replayable in-memory handle.
            if not replayable:
                self._ac_runtime_handle_manager.mark_dispatch_non_replayable(sealed_id)
            await self._event_emitter.emit_ac_dispatch_sealed(
                runtime_identity=runtime_identity,
                dispatch_id=sealed_id,
                execution_id=execution_context_id,
                session_id=session_id,
                capsule_fingerprint=capsule.fingerprint,
                reason=reason,
            )
            sealed_dispatch_ids.add(sealed_id)

        async def _emit_runtime_failure(error: str) -> None:
            await self._emit_ac_runtime_event(
                event_type="execution.session.failed",
                runtime_identity=runtime_identity,
                ac_content=ac_content,
                runtime_handle=dispatch_state.runtime_handle,
                execution_id=execution_context_id,
                session_id=dispatch_state.ac_session_id,
                orchestrator_session_id=session_id,
                success=False,
                error=error,
            )

        async def _terminalize_route_drift(dispatch_id_to_terminalize: str) -> ACExecutionResult:
            """Close durable recovery state when live admission becomes stale."""

            nonlocal clear_cached_runtime_handle
            clear_cached_runtime_handle = True
            await _seal_dispatch(
                dispatch_id_to_terminalize,
                reason="live route authority changed before provider entry",
            )
            await _emit_runtime_failure(
                "route admission blocked: live route state changed before provider entry"
            )
            return _route_drift_blocked_result()

        async def _stream_provider_call(
            *,
            call_prompt: str,
            call_tools: list[str],
            before_provider_entry: Callable[[], Awaitable[None]] | None = None,
        ) -> bool:
            """Return whether an admitted, revalidated provider stream was entered."""

            nonlocal provider_effect_active
            feedback_start = len(dispatch_state.messages)
            await provider_admission.wait()
            async with self._adaptive_concurrency.slot() as permit_epoch:
                provider_kwargs = _live_provider_kwargs()
                if provider_kwargs is None:
                    return False
                _invoke_execution_authority_guard(self)
                provider_completed = False
                try:
                    if before_provider_entry is not None:
                        await before_provider_entry()
                    provider_effect_scope.enter()
                    provider_effect_active = True
                    await self._authority_leaf_dispatcher_stream(
                        self._authority_leaf_dispatcher,
                        state=dispatch_state,
                        prompt=call_prompt,
                        tools=call_tools,
                        system_prompt=system_prompt,
                        execute_effort_kwargs=provider_kwargs,
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
                        semantic_ac_key=semantic_ac_key,
                        label=label,
                        indent=indent,
                        execution_counters=execution_counters,
                    )
                    provider_completed = True
                finally:
                    try:
                        with anyio.CancelScope(shield=True):
                            await adaptive_concurrency.observe_provider_messages(
                                self._adaptive_concurrency,
                                tuple(dispatch_state.messages[feedback_start:]),
                                permit_epoch,
                                (session_id, execution_context_id, ac_index),
                                provider_completed=provider_completed,
                                on_observation=_PROVIDER_OBSERVATION_SINK.get(),
                            )
                    finally:
                        if provider_completed:
                            provider_effect_active = False
                            provider_effect_scope.complete()
            return True

        signal_target: SessionSignalTarget | None = None
        signal_target_registered = False
        try:
            if self._session_signal_hub is not None:
                signal_target = SessionSignalTarget(
                    execution_id=execution_context_id,
                    session_scope_id=runtime_identity.session_scope_id,
                    session_attempt_id=runtime_identity.session_attempt_id,
                    runtime_backend=self._adapter.runtime_backend,
                    capabilities=self._adapter.capabilities.session_signals,
                    orchestrator_session_id=session_id,
                    ac_id=runtime_identity.ac_id,
                    ac_content=ac_content,
                    display_label=label,
                    ac_index=runtime_identity.ac_index,
                    parent_ac_index=runtime_identity.parent_ac_index,
                    sub_ac_index=runtime_identity.sub_ac_index,
                    node_id=runtime_identity.node_id,
                    display_path=runtime_identity.display_path,
                    depth=runtime_identity.depth,
                )
                await self._session_signal_hub.register_replaying(signal_target)
                signal_target_registered = True

            provider_entered = await _stream_provider_call(
                call_prompt=prompt,
                call_tools=tools,
            )
            if not provider_entered:
                return await _terminalize_route_drift(active_dispatch_id)
            runtime_handle = dispatch_state.runtime_handle
            ac_session_id = dispatch_state.ac_session_id
            final_message = dispatch_state.final_message
            success = dispatch_state.success

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
                await _seal_dispatch(
                    active_dispatch_id,
                    reason="provider stall crossed an uncertain external-effect boundary",
                )
                await _emit_runtime_failure(_STALL_SENTINEL)
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
                    route_candidate=observed_route_candidate,
                )

            # A quota pause must be recognized before queued signals open another turn.
            # Preserve the exact primary handle and let the outer ``finally``
            # reject any still-pending signals as target-ended; PAUSED must
            # imply that no effect happened after the quota-ending message.
            if any(is_usage_limit_pause_message(message) for message in reversed(messages)):
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
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    messages=tuple(messages),
                    final_message=final_message,
                    duration_seconds=(datetime.now(UTC) - start_time).total_seconds(),
                    session_id=ac_session_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                    runtime_handle=runtime_handle,
                    route_candidate=observed_route_candidate,
                )

            if signal_target is not None and self._session_signal_hub is not None:
                await self._session_signal_hub.refresh_pending(signal_target)
                while True:
                    queued_signal = self._session_signal_hub.pop_pending(signal_target)
                    if queued_signal is None:
                        break
                    if queued_signal.signal.is_expired():
                        await self._event_store.append(
                            create_session_signal_rejected_event(
                                queued_signal.signal,
                                rejection_code="expired_before_delivery",
                                detail=(
                                    "The SessionSignal expired while waiting for the runtime "
                                    "delivery boundary."
                                ),
                                effective_mode=queued_signal.effective_mode,
                                runtime_backend=signal_target.runtime_backend,
                                orchestrator_session_id=session_id,
                            )
                        )
                        continue
                    if queued_signal.effective_mode not in {
                        SessionSignalMode.INFORM,
                        SessionSignalMode.AFTER_TURN,
                    }:
                        await self._event_store.append(
                            create_session_signal_rejected_event(
                                queued_signal.signal,
                                rejection_code="delivery_mode_not_implemented",
                                detail=(
                                    "The active runtime receiver currently implements "
                                    "inform and after_turn delivery only."
                                ),
                                effective_mode=queued_signal.effective_mode,
                                runtime_backend=signal_target.runtime_backend,
                                orchestrator_session_id=session_id,
                            )
                        )
                        continue

                    follow_up_prompt = (
                        render_inform_signal_prompt(queued_signal.signal)
                        if queued_signal.effective_mode is SessionSignalMode.INFORM
                        else render_after_turn_signal_prompt(queued_signal.signal)
                    )
                    follow_up_runtime_handle = dispatch_state.runtime_handle
                    if (
                        follow_up_runtime_handle is None
                        or not self._is_resumable_runtime_handle(follow_up_runtime_handle)
                        or follow_up_runtime_handle.metadata.get("ac_capsule_fingerprint")
                        != capsule.fingerprint
                        or follow_up_runtime_handle.metadata.get("ac_dispatch_id")
                        != active_dispatch_id
                    ):
                        await _seal_dispatch(
                            active_dispatch_id,
                            reason=(
                                "completed provider turn cannot accept a SessionSignal "
                                "without a capsule-bound resumable runtime handle"
                            ),
                        )
                        await self._event_store.append(
                            create_session_signal_rejected_event(
                                queued_signal.signal,
                                rejection_code="resumable_runtime_handle_unavailable",
                                detail=(
                                    "The active provider did not expose a capsule-bound "
                                    "resumable runtime handle for this follow-up."
                                ),
                                effective_mode=queued_signal.effective_mode,
                                runtime_backend=signal_target.runtime_backend,
                                orchestrator_session_id=session_id,
                            )
                        )
                        continue

                    primary_turn = CompletedProviderTurn.capture(
                        active_dispatch_id,
                        follow_up_runtime_handle,
                        dispatch_state,
                    )
                    follow_up_dispatch_id = uuid4().hex
                    follow_up_metadata = dict(follow_up_runtime_handle.metadata)
                    follow_up_metadata["ac_dispatch_id"] = follow_up_dispatch_id
                    candidate_follow_up_runtime_handle = replace(
                        follow_up_runtime_handle,
                        metadata=follow_up_metadata,
                    )
                    await self._event_emitter.emit_ac_attempt_dispatched(
                        runtime_identity=runtime_identity,
                        dispatch_id=follow_up_dispatch_id,
                        previous_dispatch_id=primary_turn.dispatch_id,
                        execution_id=execution_context_id,
                        session_id=session_id,
                        capsule_fingerprint=capsule.fingerprint,
                        request_authority_digest=request_authority_digest,
                        session_origin="restored_same_attempt",
                        runtime_handle=candidate_follow_up_runtime_handle,
                        dispatch_kind="session_signal_followup",
                        signal_id=queued_signal.signal.signal_id,
                        signal_mode=queued_signal.effective_mode.value,
                        follow_up_input_digest=(
                            "sha256:" + hashlib.sha256(follow_up_prompt.encode("utf-8")).hexdigest()
                        ),
                    )
                    # Do not expose the candidate handle to the outer failure
                    # path until its dispatch append is durable.  If the
                    # append fails, terminalization must retain the last
                    # durable predecessor (the completed primary), never the
                    # nonexistent follow-up ID.
                    active_dispatch_id = follow_up_dispatch_id
                    # Keep the same durable-before-cache invariant as the
                    # primary dispatch.  The follow-up handle is still cached
                    # before provider entry, but a failed append cannot leave
                    # its undurable dispatch ID as a phantom predecessor.
                    remembered_follow_up_runtime_handle = self._remember_ac_runtime_handle(
                        ac_index,
                        candidate_follow_up_runtime_handle,
                        execution_context_id=execution_context_id,
                        is_sub_ac=is_sub_ac,
                        parent_ac_index=parent_ac_index,
                        sub_ac_index=sub_ac_index,
                        node_identity=node_identity,
                        retry_attempt=retry_attempt,
                    )
                    if remembered_follow_up_runtime_handle is None:
                        raise RuntimeError(
                            "SessionSignal follow-up lost its capsule-bound runtime handle"
                        )
                    dispatch_state.runtime_handle = remembered_follow_up_runtime_handle
                    message_count_before_signal = primary_turn.message_list_length
                    inform_mode = queued_signal.effective_mode is SessionSignalMode.INFORM

                    async def _claim_follow_up_delivery() -> None:
                        # This durable claim is still pre-provider-entry.  The
                        # completed primary remains active until the follow-up
                        # provider stream itself has completed.
                        await claim_follow_up_delivery(
                            event_store=self._event_store,
                            signal=queued_signal.signal,
                            effective_mode=queued_signal.effective_mode,
                            runtime_backend=signal_target.runtime_backend,
                            orchestrator_session_id=session_id,
                        )

                    try:
                        provider_entered = await _stream_provider_call(
                            call_prompt=follow_up_prompt,
                            call_tools=[] if inform_mode else tools,
                            before_provider_entry=_claim_follow_up_delivery,
                        )
                        if not provider_entered:
                            await self._event_store.append(
                                create_session_signal_rejected_event(
                                    queued_signal.signal,
                                    rejection_code="route_admission_stale",
                                    detail=(
                                        "The live route authority changed before the runtime "
                                        "follow-up provider boundary."
                                    ),
                                    effective_mode=queued_signal.effective_mode,
                                    runtime_backend=signal_target.runtime_backend,
                                    orchestrator_session_id=session_id,
                                )
                            )
                            return await _terminalize_route_drift(follow_up_dispatch_id)
                        await _seal_dispatch(
                            primary_turn.dispatch_id,
                            reason=(
                                "completed provider turn superseded by a SessionSignal follow-up"
                            ),
                        )
                    except _BatchInterruptedForRecoverablePause:
                        # This dispatch exists durably, but the provider gate
                        # rejected it before the adapter call.  The claim may
                        # already be durable; reject it terminally, abort only
                        # the child boundary, and restore the still-unsealed
                        # completed primary for the batch's pause owner.
                        active_dispatch_id = await abort_unentered_follow_up(
                            event_store=self._event_store,
                            signal=queued_signal.signal,
                            effective_mode=queued_signal.effective_mode,
                            runtime_backend=signal_target.runtime_backend,
                            orchestrator_session_id=session_id,
                            follow_up_dispatch_id=follow_up_dispatch_id,
                            primary=primary_turn,
                            state=dispatch_state,
                            seal_dispatch=_seal_dispatch,
                        )
                        break
                    except Exception as exc:
                        await self._event_store.append(
                            create_session_signal_delivery_uncertain_event(
                                queued_signal.signal,
                                effective_mode=queued_signal.effective_mode,
                                detail=(
                                    "The runtime follow-up failed across the delivery "
                                    f"boundary: {type(exc).__name__}."
                                ),
                                runtime_backend=signal_target.runtime_backend,
                                orchestrator_session_id=session_id,
                            )
                        )
                        await _seal_dispatch(
                            follow_up_dispatch_id,
                            reason="SessionSignal follow-up crossed an uncertain delivery boundary",
                        )
                        if inform_mode:
                            dispatch_state.success = primary_turn.success
                            dispatch_state.final_message = primary_turn.final_message
                            continue
                        raise

                    signal_messages = messages[message_count_before_signal:]
                    acknowledgement_messages = [
                        message
                        for message in signal_messages
                        if _is_session_signal_application_acknowledgement(message)
                    ]
                    if not acknowledgement_messages:
                        detail = (
                            "The resumed runtime returned no messages."
                            if not signal_messages
                            else (
                                "The resumed runtime returned only error or "
                                "non-acknowledging messages."
                            )
                        )
                        await self._event_store.append(
                            create_session_signal_delivery_uncertain_event(
                                queued_signal.signal,
                                effective_mode=queued_signal.effective_mode,
                                detail=detail,
                                runtime_backend=signal_target.runtime_backend,
                                orchestrator_session_id=session_id,
                            )
                        )
                        await _seal_dispatch(
                            follow_up_dispatch_id,
                            reason="SessionSignal follow-up acknowledgement was uncertain",
                        )
                        if inform_mode:
                            dispatch_state.success = primary_turn.success
                            dispatch_state.final_message = primary_turn.final_message
                            continue
                        dispatch_state.success = False
                        dispatch_state.final_message = (
                            "Synapse after-turn delivery could not be acknowledged."
                        )
                        break

                    reply = _bounded_session_signal_runtime_reply(signal_messages)
                    signal_success = dispatch_state.success

                    await self._event_store.append_batch(
                        [
                            create_session_signal_applied_event(
                                queued_signal.signal,
                                effective_mode=queued_signal.effective_mode,
                                acknowledgement=(
                                    "Runtime emitted "
                                    f"{len(acknowledgement_messages)} acknowledging "
                                    "message(s) after receiving the signal turn."
                                ),
                                runtime_backend=signal_target.runtime_backend,
                                orchestrator_session_id=session_id,
                            ),
                            create_session_signal_completed_event(
                                queued_signal.signal,
                                effective_mode=queued_signal.effective_mode,
                                summary=(
                                    "Inform signal processing completed"
                                    if inform_mode and signal_success
                                    else (
                                        "After-turn signal processing completed"
                                        if signal_success
                                        else "SessionSignal was applied but the runtime "
                                        "reported an error"
                                    )
                                ),
                                reply=reply,
                                runtime_backend=signal_target.runtime_backend,
                                orchestrator_session_id=session_id,
                            ),
                        ]
                    )
                    if inform_mode:
                        dispatch_state.success = primary_turn.success
                        dispatch_state.final_message = primary_turn.final_message

                runtime_handle = dispatch_state.runtime_handle
                ac_session_id = dispatch_state.ac_session_id
                final_message = dispatch_state.final_message
                success = dispatch_state.success

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

            # A quota result is a resumable, nonterminal provider boundary.
            # Preserve its exact scoped handle and leave the capsule dispatch
            # unsealed; marking the child failed here would make safe resume
            # impossible and tempt the composite owner to repeat its effects.
            if any(is_usage_limit_pause_message(message) for message in reversed(messages)):
                return ACExecutionResult(
                    ac_index=ac_index,
                    ac_content=ac_content,
                    success=False,
                    messages=tuple(messages),
                    final_message=final_message,
                    duration_seconds=duration,
                    session_id=ac_session_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                    runtime_handle=runtime_handle,
                    route_candidate=observed_route_candidate,
                )

            # Success contracts add deterministic evidence; they never remove
            # transcript obligations. This makes verification monotonic: adding
            # a command cannot weaken the evidence required from the worker.
            has_success_contract = (
                isinstance(ac_spec, AcceptanceCriterionSpec) and ac_spec.has_success_contract
            )
            has_expected_artifacts = isinstance(ac_spec, AcceptanceCriterionSpec) and bool(
                ac_spec.expected_artifacts
            )
            verify_gate_active = self._run_verify_commands
            verify_gate_outcome: _VerifyGateOutcome | None = None
            if success and verify_gate_active and has_success_contract:
                cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
                verify_gate_outcome = await _invoke_execution_authority_entry(
                    self,
                    _FOUNDATION_A_ENTRY_RUN_AC_VERIFY_GATE,
                    spec=ac_spec,
                    cwd=cwd,
                )

            typed_evidence, typed_validation, typed_error = self._observe_atomic_typed_evidence(
                ac_content=ac_content,
                final_message=final_message,
                success=success,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
            )
            verifier_verdict = _invoke_execution_authority_entry(
                self,
                _FOUNDATION_A_ENTRY_RUN_ATOMIC_VERIFIER_PASS,
                ac_content=ac_content,
                final_message=final_message,
                success=success,
                messages=tuple(messages),
                typed_evidence=typed_evidence,
                typed_validation=typed_validation,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
            )
            verify_gate_replaces_all_evidence = bool(
                verify_gate_outcome is not None
                and self._execution_profile is not None
                and not _effective_evidence_schema_for_ac(
                    self._execution_profile,
                    ac_content,
                    has_success_contract=has_success_contract,
                    has_expected_artifacts=has_expected_artifacts,
                    verify_gate_active=verify_gate_active,
                ).required
            )
            if (
                self._fat_harness_mode
                and verifier_verdict is None
                and success
                and not any(not message.is_final for message in messages)
            ):
                verifier_verdict = VerifierVerdict(
                    passed=False,
                    reasons=(
                        "transcript_missing_infrastructure: runtime support messages were empty",
                    ),
                    failure_class="TRANSCRIPT_MISSING_INFRASTRUCTURE",
                )
            fat_harness_error = self._fat_harness_acceptance_error(
                runtime_success=success,
                typed_evidence=typed_evidence,
                typed_validation=typed_validation,
                typed_error=typed_error,
                verifier_verdict=verifier_verdict,
                verify_gate_outcome=verify_gate_outcome,
                verify_gate_replaces_all_evidence=verify_gate_replaces_all_evidence,
            )
            transcript_unavailable = bool(
                verifier_verdict is not None
                and verifier_verdict.failure_class == "TRANSCRIPT_MISSING_INFRASTRUCTURE"
            )
            if transcript_unavailable:
                # Transcript collection failed after the worker completed. Do not
                # discard or repeat the work; keep the result successful and expose
                # the unavailable verifier in telemetry and the final report.
                fat_harness_error = None
                log.warning(
                    "parallel_executor.ac.transcript_verification_unavailable",
                    session_id=session_id,
                    execution_id=execution_id,
                    ac_index=ac_index,
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
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
            )
            # Frugality-proof grounding axis (seed AC4). Only when the leaf was
            # accepted AND emitted a structured evidence claim (the fat-harness
            # case) do we run the deterministic TraceGuard verdict; the common
            # non-fat-harness leaf has no structured claim surface and is skipped.
            await self._observe_deliver_verdict(
                runtime_identity=runtime_identity,
                execution_id=execution_context_id,
                session_id=session_id,
                is_sub_ac=is_sub_ac,
                semantic_ac_key=semantic_ac_key,
                success=success,
                typed_evidence=typed_evidence,
                verifier_verdict=verifier_verdict,
            )
            # Frugality-proof baseline axis (seed AC5), OPT-IN experiment. Only an
            # accepted decomposed child has a parent baseline to price against; the
            # harness re-executes it at the parent tier/effort in an ISOLATED
            # workspace and emits ``execution.ac.shadow_replay``. Default OFF
            # (doubles token cost) and fire-and-forget — it never changes this AC's
            # result. The finalized decision's trust flag is threaded into the
            # proof producer; untrusted and depth-capped children remain excluded.
            if self._shadow_replay_enabled and is_sub_ac and success:
                await run_shadow_replay(
                    self,
                    runtime_identity=runtime_identity,
                    execution_id=execution_context_id,
                    session_id=session_id,
                    ac_index=ac_index,
                    is_sub_ac=is_sub_ac,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    tools=tools,
                    decomposition_trustworthy=decomposition_trustworthy,
                    ac_content=ac_content,
                    ac_spec=ac_spec,
                    isolated_cwd=shadow_snapshot_cwd,
                    suggested_tier=suggested_tier,
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
                orchestrator_session_id=session_id,
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
                    has_success_contract=has_success_contract,
                    has_expected_artifacts=has_expected_artifacts,
                    verify_gate_active=verify_gate_active,
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
                verify_gate_outcome=verify_gate_outcome,
                error=fat_harness_error,
                route_candidate=observed_route_candidate,
            )

        except _BatchInterruptedForRecoverablePause:
            raise

        except anyio.get_cancelled_exc_class():
            try:
                with anyio.CancelScope(shield=True):
                    seal_policy = ACRuntimeHandleManager.cancellation_seal_policy
                    reason, replayable = seal_policy(provider_effect_active)
                    await _seal_dispatch(
                        active_dispatch_id,
                        reason=reason,
                        replayable=replayable,
                    )
                    if await self._ac_runtime_handle_manager.runtime_lifecycle_is_active(
                        runtime_identity,
                        execution_context_id,
                        observed=bool(dispatch_state.lifecycle_event_count),
                    ):
                        await _emit_runtime_failure("Runtime attempt cancelled or interrupted.")
            except Exception as seal_error:
                raise RuntimeError(
                    "AC dispatch cancellation seal failed; refusing replayable recovery"
                ) from seal_error
            clear_cached_runtime_handle = not provider_effect_active
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
            raise

        except Exception as e:
            duration = (datetime.now(UTC) - start_time).total_seconds()

            await _seal_dispatch(
                active_dispatch_id,
                reason="provider attempt raised before authoritative terminalization",
            )

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
                orchestrator_session_id=session_id,
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
                route_candidate=observed_route_candidate,
            )
        finally:
            try:
                if (
                    signal_target_registered
                    and signal_target is not None
                    and self._session_signal_hub is not None
                ):
                    pending_signals = self._session_signal_hub.unregister(signal_target)
                    signal_target_registered = False
                    for pending_signal in pending_signals:
                        rejection = await target_ended_rejection_event(
                            self._event_store,
                            pending_signal,
                            runtime_backend=signal_target.runtime_backend,
                        )
                        if rejection is not None:
                            await self._safe_emit_event(rejection)
                # Frugality-proof token axis (seed AC2). Attribute this leaf's real
                # runtime-measured spend on EVERY exit — success, stall, and the
                # mid-stream exception path all consumed tokens, and spend is spend.
                # ``messages`` is the same list the dispatcher mutates in place, so the
                # partial stream is attributed even when the runtime raised.
                await self._emit_token_attribution_for_leaf(
                    messages=messages,
                    runtime_identity=runtime_identity,
                    execution_id=execution_context_id,
                    session_id=session_id,
                    ac_index=ac_index,
                    is_sub_ac=is_sub_ac,
                    retry_attempt=retry_attempt,
                    model_decision=model_decision,
                    effort_decision=effort_decision,
                    request_authority_digest=request_authority_digest,
                )
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
            finally:
                try:
                    shadow_snapshot_stack.close()
                except Exception as exc:
                    log.warning(
                        "parallel_executor.ac.shadow_replay.snapshot_cleanup_failed",
                        ac_id=runtime_identity.ac_id,
                        error=str(exc),
                    )

    async def _emit_token_attribution_for_leaf(
        self,
        *,
        messages: list[AgentMessage],
        runtime_identity: ACRuntimeIdentity,
        execution_id: str,
        session_id: str,
        ac_index: int,
        is_sub_ac: bool,
        retry_attempt: int,
        model_decision: Any,
        effort_decision: Any,
        request_authority_digest: str,
    ) -> None:
        """Harvest and emit this leaf's runtime token spend (frugality-proof AC2).

        Emits nothing when the stream carried no runtime usage telemetry — the
        proof treats missing as missing rather than fabricating a spend. Observe-only:
        failures degrade to warnings without disrupting leaf teardown or results.
        """
        try:
            harvested = _harvest_token_spend(messages)
            if harvested is None:
                return
            effective_model = _observed_effective_model(messages)
            if effective_model is None:
                return
            token_spend, usage_breakdown = harvested
            await self._event_emitter.emit_token_attribution(
                runtime_identity=runtime_identity,
                execution_id=execution_id,
                session_id=session_id,
                ac_index=ac_index,
                is_sub_ac=is_sub_ac,
                retry_attempt=retry_attempt,
                token_spend=token_spend,
                usage_breakdown=usage_breakdown,
                model=getattr(model_decision, "model", None),
                effective_model=effective_model,
                model_tier=getattr(model_decision, "tier", None),
                model_mode=getattr(model_decision, "mode", None),
                effort_level=getattr(effort_decision, "level", None),
                effort_mode=getattr(effort_decision, "mode", None),
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
                llm_backend=(
                    getattr(self._adapter, "llm_backend", None) or self._adapter.runtime_backend
                ),
                permission_mode=getattr(self._adapter, "permission_mode", None),
                request_authority_digest=request_authority_digest,
            )
        except Exception as exc:
            log.warning(
                "parallel_executor.ac.token_attribution.observe_failed",
                ac_index=ac_index,
                error=str(exc),
            )

    async def _observe_deliver_verdict(
        self,
        *,
        runtime_identity: ACRuntimeIdentity,
        execution_id: str,
        session_id: str,
        is_sub_ac: bool,
        semantic_ac_key: str | None = None,
        success: bool,
        typed_evidence: EvidenceRecord | None,
        verifier_verdict: VerifierVerdict | None,
    ) -> None:
        """Evaluate + emit the TraceGuard deliver verdict for an accepted leaf (AC4).

        Skips silently (debug log) when the leaf was not accepted or carries no
        structured evidence claim — the manifest is loaded and the deterministic
        TraceGuard verdict is only run against a genuine ``(fact_id,
        evidence_handle)`` claim surface. HARD RULE: observe-only. This never
        changes AC success/failure, retries, or routing; any failure degrades to a
        warning.
        """
        if (
            not success
            or not self._fat_harness_mode
            or typed_evidence is None
            or verifier_verdict is None
            or not verifier_verdict.passed
        ):
            return
        try:
            ac_id = runtime_identity.ac_id
            typed_data = typed_evidence.data
            has_standard_surface = any(
                field in typed_data for field in _STANDARD_DELIVER_EVIDENCE_FIELDS
            )
            explicit_facts = _structured_deliver_facts(typed_evidence)
            if not has_standard_surface and not explicit_facts:
                log.debug(
                    "parallel_executor.ac.deliver_verdict.skipped_no_claim_surface",
                    ac_id=runtime_identity.ac_id,
                )
                return
            # Bound the manifest to this execution only; the execution_id anchor
            # already isolates it, and omitting the session filter avoids pruning
            # execution-scoped journal rows that carry a different runtime session.
            # ``execution.tool.started`` rows are admitted only here, after the
            # leaf, typed record, and harness verifier have all passed; exact
            # typed-value matching below decides whether any can back a claim.
            manifest = await load_ac_evidence_manifest(
                self._event_store,
                ac_id=ac_id,
                execution_id=execution_id,
                admit_accepted_tool_starts=True,
                accepted_retry_attempt=runtime_identity.retry_attempt,
                accepted_session_attempt_id=runtime_identity.session_attempt_id,
            )
            standard_facts = _standard_deliver_facts(
                typed_evidence,
                manifest,
                task_cwd=self._task_cwd or getattr(self._adapter, "working_directory", None),
                verifier_passed=verifier_verdict.passed,
            )
            facts = standard_facts if standard_facts is not None else explicit_facts
            if not facts:
                log.debug(
                    "parallel_executor.ac.deliver_verdict.skipped_no_claim_surface",
                    ac_id=runtime_identity.ac_id,
                )
                return
            claim = DeliverEvidenceClaim(ac_id=ac_id, facts=tuple(facts))
            verdict = evaluate_deliver_claim(
                manifest,
                claim,
                traceguard_validator=validate_evidence_claims,
                claim_term_guard=strict_deterministic_claim_term_guard,
                journal_bound=True,
            )
            await self._event_emitter.emit_deliver_verdict(
                runtime_identity=runtime_identity,
                execution_id=execution_id,
                session_id=session_id,
                is_sub_ac=is_sub_ac,
                traceguard_verdict="accepted" if verdict.accepted else "rejected",
                unsupported_claim_rate=verdict.unsupported_claim_rate,
                rejected_reasons=list(verdict.rejected_reasons),
                accepted_fact_count=len(verdict.accepted_fact_ids),
                semantic_ac_key=semantic_ac_key,
                # A paired baseline deliver verdict is not available in the
                # isolated replay.  Fail closed: an accepted child cannot be a
                # newly-rejected regression; any rejected child is conservatively
                # treated as a regression rather than manufacturing ``False``.
                grounding_regression=not verdict.accepted,
                grounding_regression_mode="fail_closed_live_traceguard",
            )
        except Exception as exc:
            log.warning(
                "parallel_executor.ac.deliver_verdict.observe_failed",
                ac_id=runtime_identity.ac_id,
                error=str(exc),
            )

    def _observe_atomic_typed_evidence(
        self,
        *,
        ac_content: str,
        final_message: str,
        success: bool,
        has_success_contract: bool = False,
        has_expected_artifacts: bool = False,
        verify_gate_active: bool = False,
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
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
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

        def workspace_digest() -> str | None:
            return self._workspace_content_digest(
                cwd,
                expected_artifacts=spec.expected_artifacts,
            )

        if spec.output_assertion and not spec.verify_command:
            return _VerifyGateOutcome(
                passed=False,
                reason="output_assertion requires verify_command",
                output_tail="",
                workspace_digest=workspace_digest(),
                cause="invalid_contract",
            )

        missing_artifacts = _missing_expected_artifacts(spec.expected_artifacts, cwd)
        if missing_artifacts:
            return _VerifyGateOutcome(
                passed=False,
                reason="expected_artifacts missing: " + ", ".join(missing_artifacts),
                output_tail="",
                missing_artifacts=missing_artifacts,
                workspace_digest=workspace_digest(),
                cause=_missing_artifacts_cause(missing_artifacts, cwd),
            )

        command = spec.verify_command
        if not command:
            return _VerifyGateOutcome(
                passed=True,
                reason=None,
                output_tail="",
                workspace_digest=workspace_digest(),
            )
        workspace_before = workspace_digest()

        def workspace_mutation_outcome(output_tail: str) -> _VerifyGateOutcome | None:
            workspace_after = workspace_digest()
            if (
                workspace_before is None
                or workspace_after is None
                or workspace_before != workspace_after
            ):
                return _VerifyGateOutcome(
                    passed=False,
                    reason=(
                        "verify_command mutated the workspace or its digest could not be "
                        "revalidated"
                    ),
                    output_tail=output_tail,
                    workspace_mutated=True,
                    workspace_digest=workspace_after,
                    cause="workspace_mutated",
                )
            return None

        # verify_command is POSIX bash by contract. Resolve a real POSIX
        # interpreter instead of inheriting the platform shell default, which
        # is cmd.exe on native Windows and cannot parse that syntax. The
        # command text itself is never rewritten — the pass/fail signal stays
        # the exit code of exactly what the seed declared.
        verify_env = sanitized_verify_environment()
        verify_shell_path = verify_shell_path_from_identity(self._verify_shell_identity)
        if verify_shell_path is None:
            return _VerifyGateOutcome(
                passed=False,
                reason=verify_shell_unavailable_reason(),
                output_tail="",
                workspace_digest=workspace_before,
                environment_unverifiable=True,
                cause="environment_unverifiable",
            )
        run = await run_with_shell(
            (verify_shell_path, "-c", command),
            cwd=cwd,
            env=verify_env,
            timeout_seconds=self._verify_command_timeout_seconds,
        )

        if run.start_error is not None:
            return _VerifyGateOutcome(
                passed=False,
                reason=f"verify_command could not start: {run.start_error}",
                output_tail="",
                workspace_digest=workspace_before,
                environment_unverifiable=True,
                cause="environment_unverifiable",
            )
        if run.timed_out:
            mutated = workspace_mutation_outcome("")
            if mutated is not None:
                return mutated
            return _VerifyGateOutcome(
                passed=False,
                reason=(f"verify_command timed out after {self._verify_command_timeout_seconds}s"),
                output_tail="",
                workspace_digest=workspace_digest(),
                environment_unverifiable=True,
                cause="timeout",
            )

        combined = run.output
        tail = combined[-_VERIFY_OUTPUT_TAIL_CHARS:]
        mutated = workspace_mutation_outcome(tail)
        if mutated is not None:
            return mutated
        returncode = run.returncode
        if returncode != 0:
            return _VerifyGateOutcome(
                passed=False,
                reason=f"verify_command exited with status {returncode}",
                output_tail=tail,
                workspace_digest=workspace_digest(),
                cause="exit_nonzero",
            )
        if spec.output_assertion and spec.output_assertion not in combined:
            return _VerifyGateOutcome(
                passed=False,
                reason="output_assertion not satisfied by verify_command output",
                output_tail=tail,
                workspace_digest=workspace_digest(),
                cause="output_assertion_unmatched",
            )
        return _VerifyGateOutcome(
            passed=True,
            reason=None,
            output_tail=tail,
            workspace_digest=workspace_digest(),
        )

    async def _apply_verify_gate(
        self,
        *,
        seed: Seed,
        ac_index: int,
        result: ACExecutionResult,
        session_id: str,
        execution_id: str,
    ) -> ACExecutionResult:
        """Apply deterministic verification without changing worker execution facts.

        A passing contract adds verification evidence. A failing contract rejects
        successful work. An unavailable verifier leaves successful work intact and
        records that the result still needs confirmation. Verification never
        resurrects a failed worker result.
        """
        if not self._run_verify_commands:
            return result
        if ac_index < 0 or ac_index >= len(seed.acceptance_criteria):
            return result
        spec = seed.acceptance_criteria[ac_index]
        if not isinstance(spec, AcceptanceCriterionSpec) or not spec.has_success_contract:
            return result
        if not result.success:
            return result

        cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
        cached_outcome = result.verify_gate_outcome
        if isinstance(cached_outcome, _VerifyGateOutcome):
            outcome = _revalidate_cached_verify_gate_outcome(
                spec=spec,
                cwd=cwd,
                outcome=cached_outcome,
            )
        else:
            outcome = await _invoke_execution_authority_entry(
                self,
                _FOUNDATION_A_ENTRY_RUN_AC_VERIFY_GATE,
                spec=spec,
                cwd=cwd,
            )
        if outcome.passed:
            if cached_outcome is outcome:
                return result
            return replace(result, verify_gate_outcome=outcome)

        from ouroboros.events.base import BaseEvent
        from ouroboros.orchestrator.failure_taxonomy import FailureClass

        if outcome.environment_unverifiable:
            # Quarantine preserves the worker result instead of failing it,
            # but the rejection-cause analytics must still see this branch:
            # `timeout` and `environment_unverifiable` are documented causes
            # and would otherwise never reach telemetry.
            usage_telemetry.capture_ac_verify_failed(cause=outcome.cause)
            return await quarantine_unverifiable_result(
                result=result,
                spec=spec,
                ac_content=ac_text(spec),
                ac_index=ac_index,
                gate_reason=outcome.reason,
                verify_gate_outcome=outcome,
                session_id=session_id,
                execution_id=execution_id,
                emit_event=self._safe_emit_event,
            )

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
                    # Machine-readable rejection cause plus the exact cwd the
                    # gate ran from. Both stay in the LOCAL event store; only
                    # the closed-vocabulary cause reaches telemetry below.
                    "verify_cause": outcome.cause,
                    "verify_cwd": cwd,
                },
            )
        )
        usage_telemetry.capture_ac_verify_failed(cause=outcome.cause)
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
            verify_gate_outcome=outcome,
        )

    async def _emit_ac_attempt_judged(
        self,
        *,
        result: ACExecutionResult,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
        required: bool = False,
        route_episode_id: str | None = None,
        route_attempt_index: int | None = None,
    ) -> None:
        """Persist one provisional outer verify/retry attempt judgment.

        Leaf-level deliver and shadow events are provisional because they are
        emitted before the seed-level success contract runs.  This marker is
        deliberately telemetry only: it never grants acceptance or dispatch
        authority.  ``execution.ac.outcome_finalized`` remains readable as a
        historical alias, but new producers use ``attempt_judged``.
        """
        from ouroboros.events.base import BaseEvent

        route_candidate = result.route_candidate
        if required and (
            route_candidate is None
            or route_episode_id is None
            or type(route_attempt_index) is not int
            or not 0 <= route_attempt_index < MAX_ROUTE_ATTEMPTS
        ):
            raise ValueError("route-aware attempt judgment requires bounded correlation metadata")
        normalized_outcome = (
            result.outcome.value
            if result.outcome is not None
            else (
                ACExecutionOutcome.SUCCEEDED.value
                if result.success
                else ACExecutionOutcome.FAILED.value
            )
        )
        if required and (
            (result.success and normalized_outcome != ACExecutionOutcome.SUCCEEDED.value)
            or (
                not result.success
                and normalized_outcome
                not in {
                    ACExecutionOutcome.FAILED.value,
                    ACExecutionOutcome.BLOCKED.value,
                }
            )
        ):
            raise ValueError("route-aware attempt judgment has contradictory result semantics")
        event_data: dict[str, object] = {
            "execution_id": execution_id,
            "session_id": session_id,
            "root_ac_index": root_ac_index,
            "ac_index": root_ac_index,
            "retry_attempt": result.retry_attempt,
            "attempt_number": result.attempt_number,
            "success": result.success,
            "outcome": normalized_outcome,
            "is_decomposed": result.is_decomposed,
            "is_decomposed_child": result.is_decomposed,
        }
        if (
            result.is_invalid
            and result.error is not None
            and result.error.startswith(f"{UnmaterializableSuccessContractError.code}:")
        ):
            event_data.update(
                {
                    "error_code": UnmaterializableSuccessContractError.code,
                    "error": result.error,
                }
            )
        if required:
            assert route_candidate is not None
            assert route_episode_id is not None
            assert route_attempt_index is not None
            event_data.update(
                {
                    "route_contract_version": 1,
                    "route_episode_id": route_episode_id,
                    "route_attempt_index": route_attempt_index,
                    "route_id": route_candidate.route_id,
                    "call_site": "parallel",
                }
            )
        event = BaseEvent(
            type="execution.ac.attempt_judged",
            aggregate_type="execution",
            aggregate_id=execution_id or session_id,
            data=event_data,
        )
        if required:
            # Bounded escalation cannot authorize another provider effect until
            # the outcome it reacts to is durable.
            await self._event_store.append(event)
        else:
            await self._safe_emit_event(event)

    @staticmethod
    def _bounded_route_episode_id(
        seed: Seed,
        *,
        execution_id: str,
        session_id: str,
        root_ac_index: int,
    ) -> str:
        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        digest = hashlib.sha256(
            f"{execution_id or session_id}\0{root_ac_index}\0{semantic_ac_key}".encode()
        ).hexdigest()
        return f"route:{digest}"

    async def _persist_route_observation(
        self,
        *,
        seed: Seed,
        result: ACExecutionResult,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
        attempted_route_ids: tuple[str, ...],
        failure_class: object | None,
        decision: RouteEscalationDecision | None,
    ) -> RouteObservation:
        """Commit one provisional route outcome before any next-route effect."""
        from ouroboros.events.base import BaseEvent
        from ouroboros.orchestrator.failure_taxonomy import FailureClass
        from ouroboros.orchestrator.route_policy import RouteRequirements

        candidate = result.route_candidate
        if candidate is None:
            raise ValueError("route observation requires the attempted candidate")
        if result.success:
            outcome = RouteVerifierOutcome.ATTEMPT_SUCCEEDED
            classified = None
            reason = None
        else:
            classified = (
                failure_class
                if isinstance(failure_class, FailureClass)
                else FailureClass.EVIDENCE_MISSING
            )
            outcome = (
                RouteVerifierOutcome.BLOCKED
                if classified is FailureClass.BLOCKED
                else RouteVerifierOutcome.FAILED
            )
            reason = decision.reason if decision is not None else EscalationReason.NO_ELIGIBLE_ROUTE
        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        observation = RouteObservation.from_candidate(
            candidate,
            RouteRequirements(),
            episode_id=self._bounded_route_episode_id(
                seed,
                execution_id=execution_id,
                session_id=session_id,
                root_ac_index=root_ac_index,
            ),
            attempt_index=len(attempted_route_ids) - 1,
            verifier_outcome=outcome,
            failure_class=classified,
            escalation_reason=reason,
        )
        await self._event_store.append(
            BaseEvent(
                type="execution.ac.route_observed",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": root_ac_index,
                    "semantic_ac_key": semantic_ac_key,
                    "call_site": "parallel",
                    "observation": observation.to_contract_data(),
                    "decision": decision.to_contract_data() if decision is not None else None,
                    "provisional_result": (
                        _serialize_provisional_route_success(
                            result,
                            workspace_root=(
                                self._task_cwd or self._adapter.working_directory or os.getcwd()
                            ),
                        )
                        if result.success
                        else None
                    ),
                    "human_handoff_required": bool(decision is not None and decision.blocked),
                    "final_acceptance_declared": False,
                },
            )
        )
        return observation

    async def _persist_composite_completion(
        self,
        *,
        seed: Seed,
        result: ACExecutionResult,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Commit a terminal composite projection before an interrupted return."""

        from ouroboros.events.base import BaseEvent

        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        result_data, decision_data, fingerprint = _serialize_composite_completion_result(
            result,
            workspace_root=(self._task_cwd or self._adapter.working_directory or os.getcwd()),
        )
        decision = result.decomposition_decision
        assert decision is not None
        expected_node_id = ExecutionNodeIdentity.root(
            execution_context_id=execution_id or session_id,
            ac_index=root_ac_index,
        ).node_id
        if decision.node_id != expected_node_id:
            raise RuntimeError("composite completion crossed decomposition node identity")
        await self._event_store.append(
            BaseEvent(
                type="execution.ac.composite_completed",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": root_ac_index,
                    "semantic_ac_key": semantic_ac_key,
                    "call_site": "parallel",
                    "result": result_data,
                    "decomposition_decision": decision_data,
                    "decomposition_fingerprint": fingerprint,
                    "final_acceptance_declared": False,
                },
            )
        )

    async def _persist_parallel_route_pause(
        self,
        *,
        seed: Seed,
        result: ACExecutionResult,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
        prior_route_ids: tuple[str, ...],
        retry_prompt_extra: str = "",
        sibling_acs: tuple[_SiblingACRef, ...] = (),
        route_id_override: str | None = None,
        expected_route_candidate: RouteCandidate | None = None,
    ) -> None:
        """Bind a quota pause to the exact capsule and resumable provider boundary."""

        from ouroboros.events.base import BaseEvent

        candidate = result.route_candidate
        if candidate is None:
            raise RuntimeError("bounded route pause lost its active route candidate")
        runtime_handle = result.runtime_handle
        runtime_metadata = runtime_handle.metadata if runtime_handle is not None else {}
        runtime_scope_id = runtime_metadata.get("session_scope_id")
        dispatch_id = runtime_metadata.get("ac_dispatch_id")
        capsule_fingerprint = runtime_metadata.get("ac_capsule_fingerprint")
        if (
            runtime_handle is None
            or not self._is_resumable_runtime_handle(runtime_handle)
            or not isinstance(runtime_scope_id, str)
            or not runtime_scope_id
            or not isinstance(dispatch_id, str)
            or len(dispatch_id) != 32
            or any(char not in "0123456789abcdef" for char in dispatch_id)
            or not isinstance(capsule_fingerprint, str)
            or len(capsule_fingerprint) != 71
            or not capsule_fingerprint.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in capsule_fingerprint[7:])
        ):
            raise RuntimeError("parallel route pause has no exact resumable provider boundary")
        if result.retry_attempt != len(prior_route_ids):
            raise RuntimeError("parallel route pause retry attempt crossed route history")
        if len(retry_prompt_extra) > 16_384:
            raise RuntimeError("parallel route pause retry prompt exceeds its durable bound")
        if len(sibling_acs) > len(seed.acceptance_criteria) or any(
            type(index) is not int
            or index < 0
            or index >= len(seed.acceptance_criteria)
            or content != ac_text(seed.acceptance_criteria[index])
            for index, content in sibling_acs
        ):
            raise RuntimeError("parallel route pause has an invalid sibling population")
        if prior_route_ids:
            if route_id_override != candidate.route_id or expected_route_candidate != candidate:
                raise RuntimeError("parallel successor pause lost its exact route override")
        elif route_id_override is not None or expected_route_candidate is not None:
            raise RuntimeError("parallel initial pause invented a route override")
        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        await self._event_store.append(
            BaseEvent(
                type="execution.ac.route_paused",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "schema_version": 2,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": root_ac_index,
                    "semantic_ac_key": semantic_ac_key,
                    "call_site": "parallel",
                    "episode_id": self._bounded_route_episode_id(
                        seed,
                        execution_id=execution_id,
                        session_id=session_id,
                        root_ac_index=root_ac_index,
                    ),
                    "attempt_index": len(prior_route_ids),
                    "prior_route_ids": list(prior_route_ids),
                    "route": candidate.to_contract_data(),
                    "resume_state": {
                        "retry_attempt": result.retry_attempt,
                        "retry_prompt_extra": retry_prompt_extra,
                        "sibling_acs": [
                            {"ac_index": index, "content": content}
                            for index, content in sibling_acs
                        ],
                        "route_id_override": route_id_override,
                        "expected_route_candidate": (
                            expected_route_candidate.to_contract_data()
                            if expected_route_candidate is not None
                            else None
                        ),
                        "runtime_scope_id": runtime_scope_id,
                        "dispatch_id": dispatch_id,
                        "capsule_fingerprint": capsule_fingerprint,
                    },
                    "recoverable_pause": True,
                    "final_acceptance_declared": False,
                },
            )
        )

    async def _persist_parallel_uncertain_handoff(
        self,
        *,
        seed: Seed,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Declare human ownership for a sibling cancelled after execution entry."""

        from ouroboros.events.base import BaseEvent

        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        await self._event_store.append(
            BaseEvent(
                type="execution.ac.uncertain_handoff_required",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": root_ac_index,
                    "semantic_ac_key": semantic_ac_key,
                    "call_site": "parallel",
                    "reason": "sibling_cancelled_after_execution_authority_entry",
                    "human_handoff_required": True,
                    "final_acceptance_declared": False,
                },
            )
        )

    def _partial_composite_pause_projection(
        self,
        *,
        result: ACExecutionResult,
        node_identity: ExecutionNodeIdentity,
        workspace_root: str,
        node_budget: list[int],
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        """Project every composite frame down to one exact paused leaf."""

        decision = result.decomposition_decision
        if not result.is_decomposed or decision is None or not result.sub_results:
            raise RuntimeError("partial composite pause lost its split result tree")
        parsed, decision_data, fingerprint = _canonical_decomposition_decision(decision.to_dict())
        if (
            parsed.disposition is not DecompositionDisposition.SPLIT
            or parsed.node_id != node_identity.node_id
            or result.depth != node_identity.depth
        ):
            raise RuntimeError("partial composite pause crossed decomposition node identity")

        paused_child_index = len(result.sub_results) - 1
        paused_child = result.sub_results[paused_child_index]
        completed_prefix = result.sub_results[:paused_child_index]
        if (
            paused_child_index >= len(parsed.children)
            or tuple(child.description for child in parsed.children[:paused_child_index])
            != tuple(child.ac_content for child in completed_prefix)
            or any(
                child.ac_index != result.ac_index * 100 + child_index
                or child.depth != result.depth + 1
                for child_index, child in enumerate(completed_prefix)
            )
            or parsed.children[paused_child_index].description != paused_child.ac_content
            or paused_child.ac_index != result.ac_index * 100 + paused_child_index
            or paused_child.depth != result.depth + 1
            or not _has_usage_limit_pause(paused_child)
            or any(_has_usage_limit_pause(child) for child in completed_prefix)
        ):
            raise RuntimeError("partial composite pause has an invalid child prefix")

        expected_child = node_identity.child(paused_child_index)
        frame: dict[str, object] = {
            "completed_children": [
                _serialize_composite_result_tree(
                    child,
                    node_budget=node_budget,
                    workspace_root=workspace_root,
                )
                for child in completed_prefix
            ],
            "paused_child_index": paused_child_index,
            "paused_child_node_id": expected_child.node_id,
            "paused_child_ac_index": paused_child.ac_index,
            "paused_child_content": paused_child.ac_content,
            "paused_child_retry_attempt": paused_child.retry_attempt,
            "decomposition_decision": decision_data,
            "decomposition_fingerprint": fingerprint,
        }
        if paused_child.is_decomposed:
            nested_frames, paused_leaf = self._partial_composite_pause_projection(
                result=paused_child,
                node_identity=expected_child,
                workspace_root=workspace_root,
                node_budget=node_budget,
            )
            return [frame, *nested_frames], paused_leaf

        runtime_handle = paused_child.runtime_handle
        runtime_metadata = runtime_handle.metadata if runtime_handle is not None else {}
        runtime_scope_id = runtime_metadata.get("session_scope_id")
        dispatch_id = runtime_metadata.get("ac_dispatch_id")
        capsule_fingerprint = runtime_metadata.get("ac_capsule_fingerprint")
        if (
            runtime_handle is None
            or not self._is_resumable_runtime_handle(runtime_handle)
            or runtime_metadata.get("node_id") != expected_child.node_id
            or not isinstance(runtime_scope_id, str)
            or not runtime_scope_id
            or not isinstance(dispatch_id, str)
            or len(dispatch_id) != 32
            or any(char not in "0123456789abcdef" for char in dispatch_id)
            or not isinstance(capsule_fingerprint, str)
            or len(capsule_fingerprint) != 71
            or not capsule_fingerprint.startswith("sha256:")
            or any(char not in "0123456789abcdef" for char in capsule_fingerprint[7:])
        ):
            raise RuntimeError(
                "partial composite pause has no exact resumable leaf provider boundary"
            )
        return [frame], {
            "node_id": expected_child.node_id,
            "ac_index": paused_child.ac_index,
            "ac_content": paused_child.ac_content,
            "retry_attempt": paused_child.retry_attempt,
            "runtime_scope_id": runtime_scope_id,
            "dispatch_id": dispatch_id,
            "capsule_fingerprint": capsule_fingerprint,
        }

    async def _persist_partial_composite_pause(
        self,
        *,
        seed: Seed,
        result: ACExecutionResult,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Seal recursive completed prefixes and the exact paused leaf boundary."""

        from ouroboros.events.base import BaseEvent

        expected_node = ExecutionNodeIdentity.root(
            execution_context_id=execution_id or session_id,
            ac_index=root_ac_index,
        )
        workspace_root = self._task_cwd or self._adapter.working_directory or os.getcwd()
        node_budget = [_COMPOSITE_RESULT_MAX_NODES]
        frames, paused_leaf = self._partial_composite_pause_projection(
            result=result,
            node_identity=expected_node,
            workspace_root=workspace_root,
            node_budget=node_budget,
        )
        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        await self._event_store.append(
            BaseEvent(
                type="execution.ac.composite_paused",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "schema_version": 2,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": root_ac_index,
                    "semantic_ac_key": semantic_ac_key,
                    "call_site": "parallel",
                    "frames": frames,
                    "paused_leaf": paused_leaf,
                    "recoverable_pause": True,
                    "final_acceptance_declared": False,
                },
            )
        )

    async def _emit_recovery_exhausted(
        self,
        *,
        seed: Seed,
        result: ACExecutionResult,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
        retry_termination_reason: str,
    ) -> None:
        """Emit the authoritative root-AC recovery-closure fact exactly once."""
        from ouroboros.events.base import BaseEvent

        if result.success or result.outcome not in {
            ACExecutionOutcome.FAILED,
            ACExecutionOutcome.BLOCKED,
        }:
            return
        emission_key = (execution_id or session_id, root_ac_index)
        if emission_key in self._recovery_exhausted_emitted:
            return
        self._recovery_exhausted_emitted.add(emission_key)

        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        alternate_status = self._alt_harness_status_by_root.get(
            root_ac_index,
            "not_attempted" if self._cross_harness_redispatch_enabled else "not_attempted",
        )
        if alternate_status == "failed":
            retry_termination_reason = "alternate_harness_exhausted"
        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.recovery_exhausted",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "schema_version": 1,
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": root_ac_index,
                    "semantic_ac_key": semantic_ac_key,
                    "retry_attempt": result.retry_attempt,
                    "configured_retry_attempts": self._ac_retry_attempts,
                    "retry_termination_reason": retry_termination_reason,
                    "alternate_redispatch_status": alternate_status,
                    "last_failure_class": self._failure_class_for_result(result) or "unknown",
                    "success": False,
                    "human_handoff_required": result.outcome is ACExecutionOutcome.BLOCKED,
                },
            )
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

        A FAILED AC whose spec carries a success contract may only be flipped to satisfied by
        sibling evidence if its own contract passes the orchestrator gate now.
        ACs without a contract are never gated out.
        """
        if not self._run_verify_commands:
            return frozenset()
        gated_out: set[int] = set()
        for result in level_results:
            if result.success or result.outcome != ACExecutionOutcome.FAILED:
                continue
            ac_idx = result.ac_index
            if ac_idx < 0 or ac_idx >= len(seed.acceptance_criteria):
                continue
            spec = seed.acceptance_criteria[ac_idx]
            if not isinstance(spec, AcceptanceCriterionSpec) or not spec.has_success_contract:
                continue
            cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
            cached_outcome = result.verify_gate_outcome
            if isinstance(cached_outcome, _VerifyGateOutcome):
                outcome = _revalidate_cached_verify_gate_outcome(
                    spec=spec,
                    cwd=cwd,
                    outcome=cached_outcome,
                )
            else:
                outcome = await _invoke_execution_authority_entry(
                    self,
                    _FOUNDATION_A_ENTRY_RUN_AC_VERIFY_GATE,
                    spec=spec,
                    cwd=cwd,
                )
            if not outcome.passed:
                gated_out.add(ac_idx)
        return frozenset(gated_out)

    def _failure_class_for_result(self, result: ACExecutionResult) -> str | None:
        """Best-effort failure taxonomy label for a failed AC result."""
        return retry_hints.failure_class_for_result(result)

    def _build_ac_retry_prompt(
        self,
        *,
        result: ACExecutionResult,
        ac_content: str,
        is_final_attempt: bool,
        manifest: EvidenceManifest | None = None,
        spec: AcceptanceCriterionSpec | None = None,
    ) -> str:
        """Build the enriched retry prompt section for a re-dispatched AC (PR-V V3/V4)."""
        failure_class = self._failure_class_for_result(result)
        return retry_hints.build_ac_retry_prompt(
            failure_class=failure_class,
            outcome=(
                result.verify_gate_outcome
                if isinstance(result.verify_gate_outcome, _VerifyGateOutcome)
                else None
            ),
            result=result,
            ac_content=ac_content,
            is_final_attempt=is_final_attempt,
            manifest=manifest,
            spec=spec,
        )

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
        persisted_route_state = await self._has_persisted_bounded_route_state(
            execution_id=execution_id,
            session_id=session_id,
            root_ac_indices=tuple(batch_executable),
            root_ac_count=len(seed.acceptance_criteria),
        )
        if persisted_route_state and not self._bounded_route_escalation_enabled:
            # Once an episode has durable Routing D evidence it may never fall
            # back to retry-count/legacy dispatch merely because current config
            # or provider capability no longer enables Routing D.
            raise RuntimeError("durable bounded-route state exists but live routing is unavailable")
        if self._bounded_route_escalation_enabled:
            return await self._run_batch_with_bounded_route_escalation(
                seed=seed,
                batch_executable=batch_executable,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                ac_retry_attempts=ac_retry_attempts,
                execution_counters=execution_counters,
            )
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
        retry_termination_reasons: dict[int, str] = {}
        # V1 gate on freshly-successful ACs.
        for position, ac_idx in enumerate(batch_executable):
            result = results[position]
            if isinstance(result, ACExecutionResult):
                gated = await self._apply_verify_gate(
                    seed=seed,
                    ac_index=ac_idx,
                    result=result,
                    session_id=session_id,
                    execution_id=execution_id,
                )
                results[position] = gated
                await self._emit_ac_attempt_judged(
                    result=gated,
                    root_ac_index=ac_idx,
                    session_id=session_id,
                    execution_id=execution_id,
                )

        if self._ac_retry_attempts <= 0:
            for position, ac_idx in enumerate(batch_executable):
                result = results[position]
                if isinstance(result, ACExecutionResult):
                    await self._emit_recovery_exhausted(
                        seed=seed,
                        result=result,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                        retry_termination_reason=(
                            "budget_exhausted"
                            if retry_hints.is_retryable_failure(result)
                            else "not_retryable"
                        ),
                    )
            return results

        # V3 retry loop: re-dispatch non-stall failures up to the configured
        # attempts. Kill criterion: stop early when the failure class repeats.
        position_by_idx = {ac_idx: position for position, ac_idx in enumerate(batch_executable)}
        pending = {
            ac_idx
            for position, ac_idx in enumerate(batch_executable)
            if retry_hints.is_retryable_failure(results[position])
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
                    manifest = await retry_hints.load_ac_retry_manifest(
                        self._event_store,
                        ac_index=ac_idx,
                        execution_id=execution_id,
                    )
                    spec = seed.acceptance_criteria[ac_idx]
                    retry_prompts[ac_idx] = self._build_ac_retry_prompt(
                        result=prior,
                        ac_content=ac_text(spec),
                        is_final_attempt=is_final,
                        manifest=manifest,
                        spec=spec if isinstance(spec, AcceptanceCriterionSpec) else None,
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
                if isinstance(gated, ACExecutionResult):
                    await self._emit_ac_attempt_judged(
                        result=gated,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                    )

                if not retry_hints.is_retryable_failure(gated):
                    if (
                        isinstance(gated, ACExecutionResult)
                        and not gated.success
                        and gated.outcome is ACExecutionOutcome.FAILED
                    ):
                        retry_termination_reasons[ac_idx] = "not_retryable"
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
                    model_support = getattr(
                        getattr(self._adapter, "capabilities", None),
                        "model_override_support",
                        ParamSupport.IGNORED,
                    )
                    # Ladder-truth escalation probe. The arithmetic proxy
                    # ``ac_retry_attempts[ac_idx] < escalation_threshold`` only
                    # defeats early-stop for the SINGLE threshold crossing, which is
                    # correct only for one fixed ladder shape. Ask the router
                    # directly whether the NEXT scheduled retry resolves to a
                    # DIFFERENT enforced model than the one just dispatched. This is
                    # agnostic to the unit's start tier and ladder shape: escalation
                    # stays pending until the resolved model stops climbing (the
                    # frontier ceiling), then early-stop resumes. Whether the unit
                    # routes as a trusted child is read from the dispatched result.
                    # A trusted decomposed parent re-runs its children one tier
                    # cheaper with this retry counter, so that child ladder governs
                    # the escalation ahead; untrusted decomposition stays at base.
                    pending_enforced_escalation = False
                    if (
                        self._model_router is not None
                        and self._model_router.runtime_backend
                        == getattr(self._adapter, "runtime_backend", None)
                        and model_support is ParamSupport.NATIVE
                        and ac_retry_attempts[ac_idx] < self._ac_retry_attempts
                    ):
                        routes_as_child = (
                            isinstance(gated, ACExecutionResult) and gated.is_decomposed
                        )
                        decomposition_trustworthy = (
                            isinstance(gated, ACExecutionResult) and gated.decomposition_trustworthy
                        )
                        just_dispatched = decide_model(
                            model_support,
                            router=self._model_router,
                            is_decomposed_child=routes_as_child,
                            decomposition_trustworthy=decomposition_trustworthy,
                            retry_attempt=ac_retry_attempts[ac_idx],
                        )
                        next_scheduled = decide_model(
                            model_support,
                            router=self._model_router,
                            is_decomposed_child=routes_as_child,
                            decomposition_trustworthy=decomposition_trustworthy,
                            retry_attempt=ac_retry_attempts[ac_idx] + 1,
                        )
                        pending_enforced_escalation = (
                            just_dispatched.is_enforced
                            and next_scheduled.model is not None
                            and next_scheduled.model != just_dispatched.model
                        )
                    if pending_enforced_escalation:
                        # The next scheduled retry escalates to a stronger model.
                        # Identical weak-model failures are not evidence that the
                        # escalation itself is futile.
                        last_failure_class[ac_idx] = new_class
                        continue
                    # Identical failure class on every attempt: stop early
                    # rather than burning the last attempt.
                    log.info(
                        "parallel_executor.ac.retry_early_stop",
                        session_id=session_id,
                        ac_index=ac_idx,
                        failure_class=new_class,
                    )
                    retry_termination_reasons[ac_idx] = "repeated_failure_early_stop"
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
                            # Apply the same V1 verify gate the same-runtime results
                            # get, so an
                            # alternate 'success' with a failing verify_command or
                            # missing expected artifact is not accepted as success.
                            finalized_alt = await self._apply_verify_gate(
                                seed=seed,
                                ac_index=ac_idx,
                                result=alt,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                            results[position_by_idx[ac_idx]] = finalized_alt
                            await self._emit_ac_attempt_judged(
                                result=finalized_alt,
                                root_ac_index=ac_idx,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                    pending.discard(ac_idx)
                    continue
                last_failure_class[ac_idx] = new_class
                if ac_retry_attempts[ac_idx] >= self._ac_retry_attempts:
                    retry_termination_reasons.setdefault(ac_idx, "budget_exhausted")
                    pending.discard(ac_idx)

        for position, ac_idx in enumerate(batch_executable):
            result = results[position]
            if isinstance(result, ACExecutionResult):
                await self._emit_recovery_exhausted(
                    seed=seed,
                    result=result,
                    root_ac_index=ac_idx,
                    session_id=session_id,
                    execution_id=execution_id,
                    retry_termination_reason=retry_termination_reasons.get(
                        ac_idx,
                        "budget_exhausted"
                        if retry_hints.is_retryable_failure(result)
                        else "not_retryable",
                    ),
                )
        return results

    async def _has_persisted_bounded_route_state(
        self,
        *,
        execution_id: str,
        session_id: str,
        root_ac_indices: tuple[int, ...],
        root_ac_count: int,
    ) -> bool:
        """Detect any same-session Routing D evidence before choosing a dispatch path."""

        relevant = set(root_ac_indices)
        bounded_streams = {
            "execution.ac.route_observed": root_ac_count * MAX_ROUTE_ATTEMPTS + 1,
            "execution.ac.uncertain_handoff_required": root_ac_count + 1,
            "execution.ac.composite_completed": _composite_completion_event_sentinel(root_ac_count),
        }
        for event_type, event_limit in bounded_streams.items():
            events = await self._event_store.query_execution_related_events(
                execution_id,
                event_type=event_type,
                limit=event_limit,
            )
            if not isinstance(events, list | tuple):
                continue
            if len(events) >= event_limit:
                raise RuntimeError(
                    f"{event_type} pre-dispatch scan exceeds its execution-wide bound"
                )
            for event in events:
                if getattr(event, "type", None) != event_type:
                    continue
                data = event.data
                if data.get("session_id") != session_id:
                    continue
                root_ac_index = data.get("root_ac_index")
                if event_type in {
                    "execution.ac.route_observed",
                    "execution.ac.route_paused",
                    "execution.ac.uncertain_handoff_required",
                    "execution.ac.composite_completed",
                    "execution.ac.composite_paused",
                }:
                    if root_ac_index in relevant or type(root_ac_index) is not int:
                        return True
        # Pause streams can grow on every recoverable provider window. Detect
        # their presence with a one-row exact-scope query; the replay owner folds
        # the complete stable population in bounded-memory pages.
        for pause_event_type in (
            "execution.ac.route_paused",
            "execution.ac.composite_paused",
        ):
            pause_events = await self._event_store.query_execution_related_events(
                execution_id,
                event_type=pause_event_type,
                limit=1,
                payload_equals={"session_id": session_id, "call_site": "parallel"},
            )
            if isinstance(pause_events, list | tuple) and pause_events:
                return True
        judgment_limit = root_ac_count * MAX_ROUTE_ATTEMPTS + 1
        judgment_events = await self._event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.attempt_judged",
            limit=judgment_limit,
            payload_equals={
                "route_contract_version": 1,
                "session_id": session_id,
            },
        )
        if not isinstance(judgment_events, list | tuple):
            return False
        if len(judgment_events) >= judgment_limit:
            raise RuntimeError(
                "execution.ac.attempt_judged pre-dispatch scan exceeds its route-aware bound"
            )
        for event in judgment_events:
            if getattr(event, "type", None) != "execution.ac.attempt_judged":
                continue
            root_ac_index = event.data.get("root_ac_index")
            if root_ac_index in relevant or type(root_ac_index) is not int:
                return True
        return False

    async def _raise_if_parallel_cancellation_requested(
        self,
        *,
        session_id: str,
        execution_counters: Mapping[str, int] | None,
    ) -> None:
        """Stop at the shared route-transition gate when cancellation exists."""

        from ouroboros.orchestrator.runner import is_cancellation_requested

        requested = await is_cancellation_requested(session_id)
        if not requested:
            try:
                events = await self._event_store.query_events(
                    aggregate_id=session_id,
                    event_type="orchestrator.session.cancelled",
                    limit=1,
                )
                requested = isinstance(events, list | tuple) and bool(events)
            except Exception:
                log.warning(
                    "parallel_executor.cancellation_check_failed",
                    session_id=session_id,
                )
        if requested:
            raw_messages_processed = (
                execution_counters.get("messages_count", 0) if execution_counters is not None else 0
            )
            messages_processed = (
                raw_messages_processed
                if isinstance(raw_messages_processed, int)
                and not isinstance(raw_messages_processed, bool)
                and raw_messages_processed >= 0
                else 0
            )
            raise ParallelExecutionCancelled(session_id, messages_processed)

    async def _run_batch_with_bounded_route_escalation(
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
        """Run cheapest-first classified escalation with a finite route set.

        Every next provider effect is preceded by two hard persistence
        boundaries: the provisional attempt judgment and its RouteObservation.
        A successful attempt remains provisional; only the existing seed-level
        Final Gate may later declare acceptance.
        """
        from ouroboros.orchestrator.failure_taxonomy import FailureClass

        positions = {ac_idx: pos for pos, ac_idx in enumerate(batch_executable)}
        results: list[ACExecutionResult | BaseException] = [
            RuntimeError("route attempt not started") for _ in batch_executable
        ]
        (
            histories,
            route_overrides,
            terminal_resume_reasons,
            provisional_successes,
        ) = await self._load_bounded_route_resume_state(
            seed=seed,
            execution_id=execution_id,
            session_id=session_id,
            root_ac_indices=tuple(batch_executable),
        )
        partial_composite_resume_roots = {
            ac_idx
            for ac_idx in batch_executable
            if ExecutionNodeIdentity.root(
                execution_context_id=execution_id,
                ac_index=ac_idx,
            ).node_id
            in self._partial_composite_resumes
        }
        pending = set(batch_executable) - set(terminal_resume_reasons) - set(provisional_successes)
        for ac_idx, reason in terminal_resume_reasons.items():
            results[positions[ac_idx]] = ACExecutionResult(
                ac_index=ac_idx,
                ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                success=False,
                error=reason,
                retry_attempt=max(0, len(histories[ac_idx]) - 1),
                outcome=ACExecutionOutcome.BLOCKED,
            )
        for ac_idx, cached_result in provisional_successes.items():
            results[positions[ac_idx]] = cached_result
        for ac_idx, history in histories.items():
            ac_retry_attempts[ac_idx] = len(history)
        retry_prompts: dict[int, str] = {
            ac_idx: state.retry_prompt_extra
            for ac_idx, state in self._parallel_route_resumes.items()
            if ac_idx in pending
        }

        while pending:
            await self._raise_if_parallel_cancellation_requested(
                session_id=session_id,
                execution_counters=execution_counters,
            )
            resume_pending = pending & set(self._parallel_route_resumes)
            resume_pending |= pending & partial_composite_resume_roots
            round_indices = [
                ac_idx for ac_idx in batch_executable if ac_idx in (resume_pending or pending)
            ]
            round_results = await self._execute_ac_batch(
                seed=seed,
                batch_indices=round_indices,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                ac_retry_attempts=ac_retry_attempts,
                execution_counters=execution_counters,
                retry_prompts=retry_prompts,
                route_overrides=route_overrides,
                route_resume_states=self._parallel_route_resumes,
                batch_sibling_indices=batch_executable,
                # Route D owns recovery while active.  Legacy cross-harness and
                # retry-count paths cannot run ahead of the finite route set.
                same_runtime_budget_exhausted=False,
            )
            await self._raise_if_parallel_cancellation_requested(
                session_id=session_id,
                execution_counters=execution_counters,
            )
            next_pending = pending - set(round_indices)
            next_overrides = dict(route_overrides)
            next_prompts = dict(retry_prompts)
            recoverable_pause_seen = any(
                isinstance(value, ACExecutionResult) and _has_usage_limit_pause(value)
                for value in round_results
            )
            if not recoverable_pause_seen and any(
                isinstance(
                    value,
                    _BatchInterruptedForRecoverablePause | _BatchEnteredAtRecoverablePause,
                )
                for value in round_results
            ):
                raise RuntimeError(
                    "parallel route batch interruption has no recoverable pause owner"
                )
            for round_position, ac_idx in enumerate(round_indices):
                await self._raise_if_parallel_cancellation_requested(
                    session_id=session_id,
                    execution_counters=execution_counters,
                )
                value = round_results[round_position]
                results[positions[ac_idx]] = value
                if isinstance(value, _BatchInterruptedForRecoverablePause):
                    continue
                if isinstance(value, _BatchEnteredAtRecoverablePause):
                    if not recoverable_pause_seen:
                        raise RuntimeError(
                            "entered parallel interruption has no recoverable pause owner"
                        )
                    self._parallel_route_resumes.pop(ac_idx, None)
                    await self._persist_parallel_uncertain_handoff(
                        seed=seed,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                    )
                    results[positions[ac_idx]] = ACExecutionResult(
                        ac_index=ac_idx,
                        ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                        success=False,
                        error=(
                            "A sibling quota cancelled this AC after provider-effect entry; "
                            "the provider-effect boundary is uncertain and human handoff is required."
                        ),
                        retry_attempt=ac_retry_attempts[ac_idx],
                        outcome=ACExecutionOutcome.BLOCKED,
                    )
                    continue
                if not isinstance(value, ACExecutionResult):
                    self._parallel_route_resumes.pop(ac_idx, None)
                    continue
                if (
                    not value.is_decomposed
                    and not value.success
                    and not _has_usage_limit_pause(value)
                    and value.outcome
                    not in {ACExecutionOutcome.BLOCKED, ACExecutionOutcome.INVALID}
                ):
                    # A classified TOO_BIG result changes the root from an
                    # atomic route episode into a verified composite.  Own that
                    # transition here, before route escalation can skip the sole
                    # live decomposition path.  Once finalized, the composite
                    # branch below owns child replay and no top-level successor
                    # route is admitted for this result.
                    criterion = seed.acceptance_criteria[ac_idx]
                    semantic_ac_key = (
                        criterion.semantic_ac_key
                        if isinstance(criterion, AcceptanceCriterionSpec)
                        and criterion.semantic_ac_key is not None
                        else derive_semantic_ac_key(criterion)
                    )
                    node_identity = ExecutionNodeIdentity.root(
                        execution_context_id=execution_id or session_id,
                        ac_index=ac_idx,
                    )
                    (
                        bounce_result,
                        bounce_decision,
                    ) = await self._maybe_recover_with_bounce_decomposition(
                        result=value,
                        ac_index=ac_idx,
                        ac_content=ac_text(criterion),
                        session_id=session_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        seed_goal=seed.goal,
                        depth=0,
                        execution_id=execution_id,
                        level_contexts=level_contexts,
                        retry_attempt=ac_retry_attempts[ac_idx],
                        execution_counters=execution_counters,
                        node_identity=node_identity,
                        ac_spec=(
                            criterion if isinstance(criterion, AcceptanceCriterionSpec) else None
                        ),
                        start_time=datetime.now(UTC),
                        semantic_ac_key=semantic_ac_key,
                        investment_spec=(
                            criterion.investment
                            if isinstance(criterion, AcceptanceCriterionSpec)
                            else None
                        ),
                    )
                    if bounce_decision is not None:
                        value = replace(
                            value,
                            decomposition_decision=bounce_decision,
                            decomposition_depth_warning=(
                                value.decomposition_depth_warning
                                or bounce_decision.compromise_reason == "depth_cap_forced_atomic"
                            ),
                        )
                    if bounce_result is not None:
                        value = bounce_result
                    results[positions[ac_idx]] = value
                if value.is_decomposed:
                    if (
                        recoverable_pause_seen
                        and not _has_usage_limit_pause(value)
                        and not value.success
                    ):
                        # Decomposed legacy recovery can dispatch more provider
                        # attempts. A quota observed anywhere in this round owns
                        # the boundary first, so defer a retryable sibling to the
                        # parallel resume owner. Completed composites and the
                        # composite that owns the pause still take their
                        # no-provider persistence paths below.
                        results[positions[ac_idx]] = _BatchInterruptedForRecoverablePause(
                            "composite recovery deferred at a sibling quota boundary"
                        )
                        continue
                    # Routing D has no child/aggregate replay owner in this
                    # slice. Once a verified bounce decision proves this root
                    # is composite, finish its established legacy
                    # verify/retry policy rather than interpreting a missing
                    # top-level RouteCandidate as a bounded outcome.
                    legacy_result = await self._continue_decomposed_legacy_recovery(
                        seed=seed,
                        ac_idx=ac_idx,
                        initial_result=value,
                        session_id=session_id,
                        execution_id=execution_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        level_contexts=level_contexts,
                        ac_retry_attempts=ac_retry_attempts,
                        execution_counters=execution_counters,
                        allow_root_redispatch=ac_idx not in partial_composite_resume_roots,
                    )
                    results[positions[ac_idx]] = legacy_result
                    if isinstance(legacy_result, ACExecutionResult):
                        if _has_usage_limit_pause(legacy_result):
                            await self._persist_partial_composite_pause(
                                seed=seed,
                                result=legacy_result,
                                root_ac_index=ac_idx,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                        else:
                            await self._persist_composite_completion(
                                seed=seed,
                                result=legacy_result,
                                root_ac_index=ac_idx,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                    continue
                if _has_usage_limit_pause(value):
                    # Quota windows are nonterminal session pauses, not evidence
                    # that this route lacks capability.  Preserve the untouched
                    # provider result for the runner's pause owner and emit no
                    # judgment/observation that could authorize a successor.
                    resume_state = self._parallel_route_resumes.get(ac_idx)
                    round_siblings: tuple[_SiblingACRef, ...] = (
                        resume_state.sibling_acs
                        if resume_state is not None
                        else tuple(
                            (index, ac_text(seed.acceptance_criteria[index]))
                            for index in round_indices
                        )
                        if len(round_indices) > 1
                        else ()
                    )
                    expected_route = (
                        resume_state.expected_route_candidate
                        if resume_state is not None
                        else route_overrides.get(ac_idx)
                    )
                    await self._persist_parallel_route_pause(
                        seed=seed,
                        result=value,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                        prior_route_ids=histories[ac_idx],
                        retry_prompt_extra=(
                            resume_state.retry_prompt_extra
                            if resume_state is not None
                            else retry_prompts.get(ac_idx, "")
                        ),
                        sibling_acs=round_siblings,
                        route_id_override=(
                            resume_state.route_id_override
                            if resume_state is not None
                            else expected_route.route_id
                            if expected_route is not None
                            else None
                        ),
                        expected_route_candidate=expected_route,
                    )
                    continue
                self._parallel_route_resumes.pop(ac_idx, None)
                gated = await self._apply_verify_gate(
                    seed=seed,
                    ac_index=ac_idx,
                    result=value,
                    session_id=session_id,
                    execution_id=execution_id,
                )
                await self._raise_if_parallel_cancellation_requested(
                    session_id=session_id,
                    execution_counters=execution_counters,
                )
                results[positions[ac_idx]] = gated
                candidate = gated.route_candidate
                route_attempt_index = len(histories[ac_idx])
                await self._emit_ac_attempt_judged(
                    result=gated,
                    root_ac_index=ac_idx,
                    session_id=session_id,
                    execution_id=execution_id,
                    required=candidate is not None,
                    route_episode_id=(
                        self._bounded_route_episode_id(
                            seed,
                            execution_id=execution_id,
                            session_id=session_id,
                            root_ac_index=ac_idx,
                        )
                        if candidate is not None
                        else None
                    ),
                    route_attempt_index=(route_attempt_index if candidate is not None else None),
                )
                await self._raise_if_parallel_cancellation_requested(
                    session_id=session_id,
                    execution_counters=execution_counters,
                )
                if candidate is None:
                    continue
                history = (*histories[ac_idx], candidate.route_id)
                if len(set(history)) != len(history):
                    raise RuntimeError("bounded route episode attempted a route more than once")
                histories[ac_idx] = history

                if gated.success:
                    await self._persist_route_observation(
                        seed=seed,
                        result=gated,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                        attempted_route_ids=history,
                        failure_class=None,
                        decision=None,
                    )
                    await self._raise_if_parallel_cancellation_requested(
                        session_id=session_id,
                        execution_counters=execution_counters,
                    )
                    continue

                raw_failure = self._failure_class_for_result(gated)
                try:
                    failure = (
                        FailureClass(raw_failure) if raw_failure else FailureClass.EVIDENCE_MISSING
                    )
                except ValueError:
                    failure = FailureClass.EVIDENCE_MISSING
                if gated.outcome is ACExecutionOutcome.BLOCKED:
                    failure = FailureClass.BLOCKED

                live_projection = self._build_route_compat_projection(
                    model_router=self._model_router,
                    effort=candidate.effort,
                )
                escalation_registry = build_compat_escalation_registry(live_projection)
                requirements = (
                    build_compat_escalation_requirements(
                        live_projection,
                        effort=candidate.effort,
                    )
                    if live_projection is not None
                    else None
                )
                live_candidate = (
                    next(
                        (
                            configured
                            for configured in live_projection.registry.candidates
                            if configured.route_id == candidate.route_id
                        ),
                        None,
                    )
                    if live_projection is not None
                    else None
                )
                if (
                    requirements is None
                    or escalation_registry is None
                    or live_projection is None
                    or live_candidate != candidate
                ):
                    decision = RouteEscalationDecision(
                        action=EscalationAction.BLOCKED,
                        failure_class=failure,
                        selected=None,
                        attempted_route_ids=history,
                        remaining_route_ids=(),
                        reason=EscalationReason.NO_ELIGIBLE_ROUTE,
                    )
                else:
                    decision = advance_route(
                        escalation_registry,
                        requirements,
                        current_route_id=candidate.route_id,
                        attempted_route_ids=history,
                        failure_class=failure,
                    )
                await self._persist_route_observation(
                    seed=seed,
                    result=gated,
                    root_ac_index=ac_idx,
                    session_id=session_id,
                    execution_id=execution_id,
                    attempted_route_ids=history,
                    failure_class=failure,
                    decision=decision,
                )
                await self._raise_if_parallel_cancellation_requested(
                    session_id=session_id,
                    execution_counters=execution_counters,
                )
                if decision.action is EscalationAction.ESCALATE_ROUTE:
                    assert decision.selected is not None
                    ac_retry_attempts[ac_idx] += 1
                    next_pending.add(ac_idx)
                    next_overrides[ac_idx] = decision.selected
                    manifest = await retry_hints.load_ac_retry_manifest(
                        self._event_store,
                        ac_index=ac_idx,
                        execution_id=execution_id,
                    )
                    spec = seed.acceptance_criteria[ac_idx]
                    next_prompts[ac_idx] = self._build_ac_retry_prompt(
                        result=gated,
                        ac_content=ac_text(spec),
                        is_final_attempt=not decision.remaining_route_ids,
                        manifest=manifest,
                        spec=spec if isinstance(spec, AcceptanceCriterionSpec) else None,
                    )
                    continue

                blocked = replace(
                    gated,
                    success=False,
                    outcome=ACExecutionOutcome.BLOCKED,
                    error=(
                        f"{gated.error or gated.final_message or 'Route attempt failed'}\n"
                        f"Route escalation stopped: {decision.reason.value}; "
                        "human handoff required."
                    ),
                )
                results[positions[ac_idx]] = blocked
                await self._emit_recovery_exhausted(
                    seed=seed,
                    result=blocked,
                    root_ac_index=ac_idx,
                    session_id=session_id,
                    execution_id=execution_id,
                    retry_termination_reason=decision.reason.value,
                )

            # A provider quota is normally shared by every route on this
            # backend.  Do not dispatch successors accumulated for sibling ACs
            # in the same round; return raw failures so the runner can durably
            # mark the session PAUSED.
            pending = set() if recoverable_pause_seen else next_pending
            route_overrides = next_overrides
            retry_prompts = next_prompts

        return results

    async def _continue_decomposed_legacy_recovery(
        self,
        *,
        seed: Seed,
        ac_idx: int,
        initial_result: ACExecutionResult,
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        ac_retry_attempts: dict[int, int],
        execution_counters: dict[str, int] | None,
        allow_root_redispatch: bool,
    ) -> ACExecutionResult | BaseException:
        """Continue the pre-Routing-D recovery contract for a composite root."""

        current = initial_result
        if _has_usage_limit_pause(current):
            return current
        current = await self._apply_verify_gate(
            seed=seed,
            ac_index=ac_idx,
            result=current,
            session_id=session_id,
            execution_id=execution_id,
        )
        await self._emit_ac_attempt_judged(
            result=current,
            root_ac_index=ac_idx,
            session_id=session_id,
            execution_id=execution_id,
        )
        last_failure_class = self._failure_class_for_result(current)
        termination_reason = "not_retryable"

        while (
            allow_root_redispatch
            and retry_hints.is_retryable_failure(current)
            and ac_retry_attempts[ac_idx] < self._ac_retry_attempts
        ):
            ac_retry_attempts[ac_idx] += 1
            is_final = ac_retry_attempts[ac_idx] >= self._ac_retry_attempts
            manifest = await retry_hints.load_ac_retry_manifest(
                self._event_store,
                ac_index=ac_idx,
                execution_id=execution_id,
            )
            spec = seed.acceptance_criteria[ac_idx]
            retried = await self._execute_ac_batch(
                seed=seed,
                batch_indices=[ac_idx],
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                ac_retry_attempts=ac_retry_attempts,
                execution_counters=execution_counters,
                retry_prompts={
                    ac_idx: self._build_ac_retry_prompt(
                        result=current,
                        ac_content=ac_text(spec),
                        is_final_attempt=is_final,
                        manifest=manifest,
                        spec=spec if isinstance(spec, AcceptanceCriterionSpec) else None,
                    )
                },
                same_runtime_budget_exhausted=is_final,
                force_legacy_routing=True,
            )
            next_value = retried[0]
            if not isinstance(next_value, ACExecutionResult):
                return next_value
            if _has_usage_limit_pause(next_value):
                return next_value
            current = await self._apply_verify_gate(
                seed=seed,
                ac_index=ac_idx,
                result=next_value,
                session_id=session_id,
                execution_id=execution_id,
            )
            await self._emit_ac_attempt_judged(
                result=current,
                root_ac_index=ac_idx,
                session_id=session_id,
                execution_id=execution_id,
            )
            if not retry_hints.is_retryable_failure(current):
                termination_reason = "not_retryable"
                break

            new_failure_class = self._failure_class_for_result(current)
            if (
                new_failure_class is not None
                and last_failure_class is not None
                and new_failure_class == last_failure_class
            ):
                model_support = getattr(
                    getattr(self._adapter, "capabilities", None),
                    "model_override_support",
                    ParamSupport.IGNORED,
                )
                pending_model_escalation = False
                if (
                    self._model_router is not None
                    and self._model_router.runtime_backend
                    == getattr(self._adapter, "runtime_backend", None)
                    and model_support is ParamSupport.NATIVE
                    and not is_final
                ):
                    just_dispatched = decide_model(
                        model_support,
                        router=self._model_router,
                        is_decomposed_child=True,
                        decomposition_trustworthy=current.decomposition_trustworthy,
                        retry_attempt=ac_retry_attempts[ac_idx],
                    )
                    next_scheduled = decide_model(
                        model_support,
                        router=self._model_router,
                        is_decomposed_child=True,
                        decomposition_trustworthy=current.decomposition_trustworthy,
                        retry_attempt=ac_retry_attempts[ac_idx] + 1,
                    )
                    pending_model_escalation = bool(
                        just_dispatched.is_enforced
                        and next_scheduled.model is not None
                        and next_scheduled.model != just_dispatched.model
                    )
                if not pending_model_escalation:
                    termination_reason = "repeated_failure_early_stop"
                    if not is_final:
                        alt = await self._maybe_redispatch_alt_harness_for_batch_ac(
                            seed=seed,
                            ac_idx=ac_idx,
                            result=current,
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
                            current = await self._apply_verify_gate(
                                seed=seed,
                                ac_index=ac_idx,
                                result=alt,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                            await self._emit_ac_attempt_judged(
                                result=current,
                                root_ac_index=ac_idx,
                                session_id=session_id,
                                execution_id=execution_id,
                            )
                    break
            last_failure_class = new_failure_class
            termination_reason = "budget_exhausted"

        await self._emit_recovery_exhausted(
            seed=seed,
            result=current,
            root_ac_index=ac_idx,
            session_id=session_id,
            execution_id=execution_id,
            retry_termination_reason=termination_reason,
        )
        return current

    async def _load_bounded_route_resume_state(
        self,
        *,
        seed: Seed,
        execution_id: str,
        session_id: str,
        root_ac_indices: tuple[int, ...],
    ) -> tuple[
        dict[int, tuple[str, ...]],
        dict[int, RouteCandidate],
        dict[int, str],
        dict[int, ACExecutionResult],
    ]:
        """Replay route observations without repeating a provider effect.

        A failed observation with a durable escalation decision resumes at its
        exact selected successor.  A provisional success seals the provider
        effect but remains subject to the existing Final Gate.  A paused route
        resumes only its exact durable candidate.
        """
        from ouroboros.orchestrator.route_policy import RouteRequirements

        relevant = set(root_ac_indices)
        for root_ac_index in relevant:
            self._parallel_route_resumes.pop(root_ac_index, None)
        composite_results: dict[int, ACExecutionResult] = {}
        composite_event_limit = _composite_completion_event_sentinel(len(seed.acceptance_criteria))
        composite_events = await self._event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.composite_completed",
            limit=composite_event_limit,
        )
        if len(composite_events) >= composite_event_limit:
            raise RuntimeError("composite completion replay exceeds the admitted root population")
        for event in composite_events:
            if event.type != "execution.ac.composite_completed":
                continue
            data = event.data
            if not _mapping_has_exact_keys(data, _PARALLEL_COMPOSITE_COMPLETION_KEYS):
                raise RuntimeError("composite completion replay has an invalid event envelope")
            if data.get("session_id") != session_id:
                continue
            root_ac_index = data.get("root_ac_index")
            if (
                type(data.get("schema_version")) is not int
                or data.get("schema_version") != 1
                or data.get("execution_id") != execution_id
                or data.get("call_site") != "parallel"
                or type(root_ac_index) is not int
                or data.get("final_acceptance_declared") is not False
            ):
                raise RuntimeError("composite completion replay has invalid correlation metadata")
            assert isinstance(root_ac_index, int)
            if root_ac_index not in relevant:
                continue
            if root_ac_index in composite_results:
                raise RuntimeError("composite completion replay is duplicated")
            criterion = seed.acceptance_criteria[root_ac_index]
            semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
            if data.get("semantic_ac_key") != semantic_ac_key:
                raise RuntimeError("composite completion replay crossed AC identity")
            decision, _decision_data, fingerprint = _canonical_decomposition_decision(
                data.get("decomposition_decision")
            )
            if (
                decision.disposition is not DecompositionDisposition.SPLIT
                or data.get("decomposition_fingerprint") != fingerprint
                or decision.node_id
                != ExecutionNodeIdentity.root(
                    execution_context_id=execution_id or session_id,
                    ac_index=root_ac_index,
                ).node_id
            ):
                raise RuntimeError("composite completion replay crossed decomposition identity")
            restored = _deserialize_composite_completion_result(
                data.get("result"),
                ac_index=root_ac_index,
                ac_content=ac_text(criterion),
                decomposition_decision=decision,
            )
            composite_results[root_ac_index] = restored
            try:
                self._confirm_replayed_decomposition_decision(decision)
            except RuntimeError as exc:
                raise RuntimeError(f"composite completion {exc}") from exc
        partial_states: dict[int, tuple[_PartialCompositeResumeState, ...]] = {}
        prior_frame_states: dict[tuple[int, str], _PartialCompositeResumeState] = {}
        prior_paths: dict[int, tuple[_PartialCompositeResumeState, ...]] = {}
        # Fold a stable high-water snapshot oldest-to-newest. The page size is a
        # memory bound, never a valid-history limit, so every producer-created
        # pause remains replayable while advancing prefixes and provider handles
        # retain their chronological semantics.
        async for event in replay_execution_events_chronologically(
            self._event_store,
            execution_id=execution_id,
            event_type="execution.ac.composite_paused",
            page_size=_PARALLEL_PAUSE_REPLAY_PAGE_SIZE,
        ):
            if event.type != "execution.ac.composite_paused":
                continue
            data = event.data
            if not _mapping_has_exact_keys(data, _PARALLEL_COMPOSITE_PAUSE_KEYS):
                raise RuntimeError("partial composite replay has an invalid event envelope")
            if data.get("session_id") != session_id:
                continue
            root_ac_index = data.get("root_ac_index")
            if (
                type(data.get("schema_version")) is not int
                or data.get("schema_version") != 2
                or data.get("execution_id") != execution_id
                or data.get("call_site") != "parallel"
                or type(root_ac_index) is not int
                or data.get("recoverable_pause") is not True
                or data.get("final_acceptance_declared") is not False
            ):
                raise RuntimeError("partial composite replay has invalid authority metadata")
            assert isinstance(root_ac_index, int)
            if root_ac_index not in relevant:
                continue
            criterion = seed.acceptance_criteria[root_ac_index]
            semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
            if data.get("semantic_ac_key") != semantic_ac_key:
                raise RuntimeError("partial composite replay crossed AC identity")
            expected_root = ExecutionNodeIdentity.root(
                execution_context_id=execution_id or session_id,
                ac_index=root_ac_index,
            )
            raw_frames = data.get("frames")
            raw_leaf = data.get("paused_leaf")
            if (
                not isinstance(raw_frames, list)
                or not 1 <= len(raw_frames) <= _COMPOSITE_RESULT_MAX_DEPTH
                or not _mapping_has_exact_keys(raw_leaf, _PARALLEL_COMPOSITE_PAUSE_LEAF_KEYS)
            ):
                raise RuntimeError("partial composite replay has malformed recursive state")
            assert isinstance(raw_leaf, Mapping)
            leaf_scope_id = raw_leaf.get("runtime_scope_id")
            leaf_dispatch_id = raw_leaf.get("dispatch_id")
            leaf_capsule_fingerprint = raw_leaf.get("capsule_fingerprint")
            if (
                not isinstance(leaf_scope_id, str)
                or not leaf_scope_id
                or not isinstance(leaf_dispatch_id, str)
                or len(leaf_dispatch_id) != 32
                or any(char not in "0123456789abcdef" for char in leaf_dispatch_id)
                or not isinstance(leaf_capsule_fingerprint, str)
                or len(leaf_capsule_fingerprint) != 71
                or not leaf_capsule_fingerprint.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in leaf_capsule_fingerprint[7:])
            ):
                raise RuntimeError("partial composite replay has malformed leaf boundary")

            node_budget = [_COMPOSITE_RESULT_MAX_NODES]
            frame_states: list[_PartialCompositeResumeState] = []
            expected_node = expected_root
            parent_ac_index = root_ac_index
            for frame_index, raw_frame in enumerate(raw_frames):
                if not _mapping_has_exact_keys(raw_frame, _PARALLEL_COMPOSITE_PAUSE_FRAME_KEYS):
                    raise RuntimeError("partial composite replay has malformed frame")
                assert isinstance(raw_frame, Mapping)
                decision, _decision_data, fingerprint = _canonical_decomposition_decision(
                    raw_frame.get("decomposition_decision")
                )
                paused_child_index = raw_frame.get("paused_child_index")
                paused_child_ac_index = raw_frame.get("paused_child_ac_index")
                paused_child_content = raw_frame.get("paused_child_content")
                paused_retry_attempt = raw_frame.get("paused_child_retry_attempt")
                raw_completed = raw_frame.get("completed_children")
                if (
                    decision.disposition is not DecompositionDisposition.SPLIT
                    or raw_frame.get("decomposition_fingerprint") != fingerprint
                    or decision.node_id != expected_node.node_id
                    or type(paused_child_index) is not int
                    or not 0 <= paused_child_index < len(decision.children)
                    or type(paused_child_ac_index) is not int
                    or paused_child_ac_index != parent_ac_index * 100 + paused_child_index
                    or not isinstance(paused_child_content, str)
                    or paused_child_content != decision.children[paused_child_index].description
                    or type(paused_retry_attempt) is not int
                    or paused_retry_attempt < 0
                    or not isinstance(raw_completed, list)
                    or len(raw_completed) != paused_child_index
                ):
                    raise RuntimeError("partial composite replay has malformed frame state")
                expected_child = expected_node.child(paused_child_index)
                if raw_frame.get("paused_child_node_id") != expected_child.node_id:
                    raise RuntimeError("partial composite replay crossed child node identity")
                completed_children = tuple(
                    _deserialize_composite_result_tree(child, node_budget=node_budget)
                    for child in raw_completed
                )
                if tuple(child.ac_content for child in completed_children) != tuple(
                    child.description for child in decision.children[:paused_child_index]
                ) or any(
                    child.ac_index != parent_ac_index * 100 + child_index
                    or child.depth != expected_node.depth + 1
                    for child_index, child in enumerate(completed_children)
                ):
                    raise RuntimeError("partial composite replay drifted from its child prefix")
                is_leaf_frame = frame_index == len(raw_frames) - 1
                if is_leaf_frame and (
                    raw_leaf.get("node_id") != expected_child.node_id
                    or raw_leaf.get("ac_index") != paused_child_ac_index
                    or raw_leaf.get("ac_content") != paused_child_content
                    or raw_leaf.get("retry_attempt") != paused_retry_attempt
                ):
                    raise RuntimeError("partial composite replay crossed paused leaf identity")
                state = _PartialCompositeResumeState(
                    decision=decision,
                    completed_children=completed_children,
                    paused_child_index=paused_child_index,
                    paused_child_ac_index=paused_child_ac_index,
                    paused_child_content=paused_child_content,
                    paused_child_retry_attempt=paused_retry_attempt,
                    paused_runtime_scope_id=(leaf_scope_id if is_leaf_frame else None),
                    paused_dispatch_id=(leaf_dispatch_id if is_leaf_frame else None),
                    paused_capsule_fingerprint=(
                        leaf_capsule_fingerprint if is_leaf_frame else None
                    ),
                )
                previous = prior_frame_states.get((root_ac_index, decision.node_id))
                if previous is not None and (
                    state.paused_child_index < previous.paused_child_index
                    or state.completed_children[: len(previous.completed_children)]
                    != previous.completed_children
                    or state.decision != previous.decision
                ):
                    raise RuntimeError("partial composite replay has a conflicting state sequence")
                prior_frame_states[(root_ac_index, decision.node_id)] = state
                frame_states.append(state)
                expected_node = expected_child
                parent_ac_index = paused_child_ac_index
            current_path = tuple(frame_states)
            previous_path = prior_paths.get(root_ac_index)
            if previous_path is not None and len(current_path) < len(previous_path):
                first_progress_index: int | None = None
                for path_index, (previous_state, current_state) in enumerate(
                    zip(previous_path, current_path, strict=False)
                ):
                    if (
                        current_state.decision != previous_state.decision
                        or current_state.completed_children != previous_state.completed_children
                        or current_state.paused_child_index != previous_state.paused_child_index
                        or current_state.paused_child_ac_index
                        != previous_state.paused_child_ac_index
                        or current_state.paused_child_content != previous_state.paused_child_content
                        or current_state.paused_child_retry_attempt
                        != previous_state.paused_child_retry_attempt
                    ):
                        first_progress_index = path_index
                        break
                if first_progress_index is None:
                    raise RuntimeError(
                        "partial composite replay dropped an established descendant frame"
                    )
                previous_state = previous_path[first_progress_index]
                current_state = current_path[first_progress_index]
                if (
                    current_state.decision != previous_state.decision
                    or current_state.paused_child_index <= previous_state.paused_child_index
                ):
                    raise RuntimeError(
                        "partial composite replay shortened without consuming its subtree"
                    )
            prior_paths[root_ac_index] = current_path
            partial_states[root_ac_index] = current_path
        active_partial_roots = set(partial_states) - set(composite_results)
        for root_ac_index, states in partial_states.items():
            if root_ac_index in composite_results:
                # A later terminal composite safely consumes every earlier
                # pause projection for the same immutable split.
                if composite_results[root_ac_index].decomposition_decision != states[0].decision:
                    raise RuntimeError("partial composite replay conflicts with completion")
                continue
            for state in states:
                try:
                    self._confirm_replayed_decomposition_decision(state.decision)
                except RuntimeError as exc:
                    raise RuntimeError(f"partial composite {exc}") from exc
                self._partial_composite_resumes[state.decision.node_id] = state
        grouped: dict[int, list[tuple[RouteObservation, object, bool, object]]] = {
            ac_idx: [] for ac_idx in root_ac_indices
        }
        observation_event_limit = len(seed.acceptance_criteria) * MAX_ROUTE_ATTEMPTS + 1
        events = await self._event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.route_observed",
            limit=observation_event_limit,
        )
        if len(events) >= observation_event_limit:
            raise RuntimeError("route observation replay exceeds the execution-wide bound")
        for event in events:
            if event.type != "execution.ac.route_observed":
                continue
            data = event.data
            if not _mapping_has_exact_keys(data, _PARALLEL_ROUTE_OBSERVATION_KEYS):
                raise RuntimeError("route observation replay has an invalid event envelope")
            if data.get("session_id") != session_id:
                continue
            if data.get("call_site") != "parallel":
                # A session cannot change routing call sites during replay.
                # Missing legacy scope is ambiguous and therefore cannot be
                # treated as parallel evidence.
                raise RuntimeError("route observation replay crossed routing call sites")
            if data.get("execution_id") != execution_id:
                raise RuntimeError("route observation replay crossed execution identity")
            root_ac_index = data.get("root_ac_index")
            if type(root_ac_index) is not int:
                raise RuntimeError("route observation replay has an invalid root AC index")
            if root_ac_index not in relevant:
                continue
            if type(data.get("schema_version")) is not int or data.get("schema_version") != 1:
                raise RuntimeError("route observation replay has an invalid event schema")
            if data.get("final_acceptance_declared") is not False:
                raise RuntimeError("route observation cannot declare Final Gate acceptance")
            human_handoff_required = data.get("human_handoff_required")
            if type(human_handoff_required) is not bool:
                raise RuntimeError("route observation replay has an invalid handoff claim")
            criterion = seed.acceptance_criteria[root_ac_index]
            semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
            if data.get("semantic_ac_key") != semantic_ac_key:
                raise RuntimeError("route observation replay crossed AC identity")
            try:
                observation = RouteObservation.from_contract_data(data.get("observation"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "route observation replay contains an invalid observation"
                ) from exc
            episode_digest = hashlib.sha256(
                f"{execution_id or session_id}\0{root_ac_index}\0{semantic_ac_key}".encode()
            ).hexdigest()
            if observation.episode_id != f"route:{episode_digest}":
                raise RuntimeError("route observation replay crossed route episode identity")
            if len(grouped[root_ac_index]) >= MAX_ROUTE_ATTEMPTS:
                raise RuntimeError("route observation replay exceeds the finite route bound")
            grouped[root_ac_index].append(
                (
                    observation,
                    data.get("decision"),
                    human_handoff_required,
                    data.get("provisional_result"),
                )
            )

        observed_attempts: dict[tuple[int, str, int, str], RouteVerifierOutcome] = {}
        for root_ac_index, rows in grouped.items():
            for observation, _decision, _handoff, _provisional_result in rows:
                key = (
                    root_ac_index,
                    observation.episode_id,
                    observation.attempt_index,
                    observation.route_id,
                )
                if key in observed_attempts:
                    raise RuntimeError("durable route observation is duplicated")
                observed_attempts[key] = observation.verifier_outcome
        judged_attempts: dict[tuple[int, str, int, str], tuple[bool, str]] = {}
        judgment_event_limit = len(seed.acceptance_criteria) * MAX_ROUTE_ATTEMPTS + 1
        judgment_events = await self._event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.attempt_judged",
            limit=judgment_event_limit,
            payload_equals={
                "route_contract_version": 1,
                "session_id": session_id,
            },
        )
        if len(judgment_events) >= judgment_event_limit:
            raise RuntimeError("route-aware attempt judgment replay exceeds its population bound")
        for event in judgment_events:
            if event.type != "execution.ac.attempt_judged":
                continue
            data = event.data
            if data.get("session_id") != session_id:
                continue
            # Legacy and non-routing judgments have no Routing D marker and do
            # not participate in this replay episode.
            if data.get("route_contract_version") is None:
                continue
            if not _mapping_has_exact_keys(data, _PARALLEL_ROUTE_JUDGMENT_KEYS):
                raise RuntimeError("route-aware attempt judgment has an invalid event envelope")
            root_ac_index = data.get("root_ac_index")
            route_episode_id = data.get("route_episode_id")
            route_attempt_index = data.get("route_attempt_index")
            route_id = data.get("route_id")
            retry_attempt = data.get("retry_attempt")
            attempt_number = data.get("attempt_number")
            if (
                data.get("route_contract_version") != 1
                or data.get("execution_id") != execution_id
                or data.get("call_site") != "parallel"
                or type(root_ac_index) is not int
                or data.get("ac_index") != root_ac_index
                or not isinstance(route_episode_id, str)
                or not route_episode_id
                or len(route_episode_id) > MAX_EPISODE_ID_CHARS
                or type(route_attempt_index) is not int
                or not 0 <= route_attempt_index < MAX_ROUTE_ATTEMPTS
                or not isinstance(route_id, str)
                or not route_id
                or len(route_id) > MAX_ROUTE_ID_CHARS
                or type(retry_attempt) is not int
                or not 0 <= retry_attempt < MAX_ROUTE_ATTEMPTS
                or type(attempt_number) is not int
                or attempt_number != retry_attempt + 1
                or type(data.get("success")) is not bool
                or type(data.get("outcome")) is not str
                or data.get("is_decomposed") is not False
                or data.get("is_decomposed_child") is not False
            ):
                raise RuntimeError("route-aware attempt judgment has invalid correlation metadata")
            assert isinstance(root_ac_index, int)
            if root_ac_index not in relevant:
                continue
            key = (
                root_ac_index,
                data["route_episode_id"],
                data["route_attempt_index"],
                data["route_id"],
            )
            if key in judged_attempts:
                raise RuntimeError("route-aware attempt judgment is duplicated")
            outcome = data["outcome"]
            if outcome not in {
                ACExecutionOutcome.SUCCEEDED.value,
                ACExecutionOutcome.FAILED.value,
                ACExecutionOutcome.BLOCKED.value,
            }:
                raise RuntimeError("route-aware attempt judgment has an invalid outcome")
            judged_attempts[key] = (data["success"], outcome)
        if set(judged_attempts) - set(observed_attempts):
            raise RuntimeError(
                "route-aware attempt judgment has no matching durable route observation"
            )
        if set(observed_attempts) - set(judged_attempts):
            raise RuntimeError(
                "durable route observation has no matching route-aware attempt judgment"
            )
        expected_judgments = {
            RouteVerifierOutcome.ATTEMPT_SUCCEEDED: (
                True,
                ACExecutionOutcome.SUCCEEDED.value,
            ),
            RouteVerifierOutcome.FAILED: (False, ACExecutionOutcome.FAILED.value),
            RouteVerifierOutcome.BLOCKED: (False, ACExecutionOutcome.BLOCKED.value),
        }
        for key, verifier_outcome in observed_attempts.items():
            if judged_attempts[key] != expected_judgments[verifier_outcome]:
                raise RuntimeError(
                    "route-aware attempt judgment contradicts its durable observation"
                )

        histories: dict[int, tuple[str, ...]] = {}
        overrides: dict[int, RouteCandidate] = {}
        terminals: dict[int, str] = {}
        provisional_successes: dict[int, ACExecutionResult] = dict(composite_results)
        for ac_idx, rows in grouped.items():
            if rows and (ac_idx in composite_results or ac_idx in active_partial_roots):
                raise RuntimeError("composite replay conflicts with atomic route replay evidence")
            rows.sort(key=lambda row: row[0].attempt_index)
            if [row[0].attempt_index for row in rows] != list(range(len(rows))):
                raise RuntimeError("route observation replay has a gap or duplicate")
            episode_ids = {row[0].episode_id for row in rows}
            route_ids = tuple(row[0].route_id for row in rows)
            if len(episode_ids) > 1 or len(route_ids) != len(set(route_ids)):
                raise RuntimeError("route observation replay is inconsistent")
            histories[ac_idx] = route_ids
            if not rows:
                continue
            parsed_decisions: list[RouteEscalationDecision | None] = []
            for row_index, (
                observation,
                raw_decision,
                handoff_claim,
                raw_provisional_result,
            ) in enumerate(rows):
                live_projection = self._build_route_compat_projection(
                    model_router=self._model_router,
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
                if live_projection is None or requirements is None or escalation_registry is None:
                    raise RuntimeError("route observation replay has no compatible live registry")
                candidate = next(
                    (
                        configured
                        for configured in live_projection.registry.candidates
                        if configured.route_id == observation.route_id
                    ),
                    None,
                )
                if candidate is None:
                    raise RuntimeError("route observation replay references a removed route")
                eligible_candidate = next(
                    (
                        configured
                        for configured in escalation_registry.candidates
                        if configured.route_id == observation.route_id
                    ),
                    None,
                )
                if eligible_candidate != candidate:
                    raise RuntimeError(
                        "route observation replay detected starting-tier floor drift"
                    )
                expected_observation = RouteObservation.from_candidate(
                    candidate,
                    RouteRequirements(
                        required_capabilities=requirements.required_capabilities,
                    ),
                    episode_id=observation.episode_id,
                    attempt_index=observation.attempt_index,
                    verifier_outcome=observation.verifier_outcome,
                    failure_class=observation.failure_class,
                    escalation_reason=observation.escalation_reason,
                )
                if expected_observation != observation:
                    raise RuntimeError(
                        "route observation replay detected route configuration drift"
                    )

                attempted = route_ids[: row_index + 1]
                if observation.verifier_outcome is RouteVerifierOutcome.ATTEMPT_SUCCEEDED:
                    if (
                        raw_decision is not None
                        or handoff_claim
                        or row_index != len(rows) - 1
                        or raw_provisional_result is None
                    ):
                        raise RuntimeError(
                            "successful route observation has invalid recovery state"
                        )
                    parsed_decisions.append(None)
                    continue
                if raw_provisional_result is not None:
                    raise RuntimeError("failed route observation carries success-only context")
                if observation.failure_class is None:
                    raise RuntimeError("failed route observation lost its failure classification")
                try:
                    decision = RouteEscalationDecision.from_contract_data(
                        raw_decision,
                        registry=escalation_registry,
                    )
                    recomputed = advance_route(
                        escalation_registry,
                        requirements,
                        current_route_id=observation.route_id,
                        attempted_route_ids=attempted,
                        failure_class=observation.failure_class,
                    )
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "route escalation replay contains an invalid decision"
                    ) from exc
                if decision != recomputed or observation.escalation_reason is not decision.reason:
                    raise RuntimeError("route escalation replay decision drifted from live policy")
                if handoff_claim is not decision.blocked:
                    raise RuntimeError("route observation replay has a false handoff claim")
                if row_index < len(rows) - 1:
                    next_observation = rows[row_index + 1][0]
                    selected_snapshot = decision.selected
                    if (
                        decision.action is not EscalationAction.ESCALATE_ROUTE
                        or selected_snapshot is None
                        or (
                            selected_snapshot.route_id,
                            selected_snapshot.model,
                            selected_snapshot.harness,
                            selected_snapshot.effort,
                            selected_snapshot.cost_units,
                            selected_snapshot.capabilities,
                        )
                        != (
                            next_observation.route_id,
                            next_observation.model,
                            next_observation.harness,
                            next_observation.effort,
                            next_observation.cost_units,
                            next_observation.capabilities,
                        )
                    ):
                        raise RuntimeError(
                            "route observation replay broke its durable successor chain"
                        )
                parsed_decisions.append(decision)

            last_observation = rows[-1][0]
            last_decision = parsed_decisions[-1]
            if last_observation.verifier_outcome is RouteVerifierOutcome.ATTEMPT_SUCCEEDED:
                provisional_successes[ac_idx] = _deserialize_provisional_route_success(
                    rows[-1][3],
                    ac_index=ac_idx,
                    ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                    route_candidate=candidate,
                )
                continue
            if last_decision is None:
                raise RuntimeError("failed route observation lost its durable decision")
            if last_decision.action is EscalationAction.ESCALATE_ROUTE:
                assert last_decision.selected is not None
                overrides[ac_idx] = last_decision.selected
            elif last_decision.action is EscalationAction.BLOCKED:
                terminals[ac_idx] = (
                    "The durable route set is exhausted or hard-blocked; human handoff is required."
                )
            else:
                raise RuntimeError("route escalation replay contains an unknown action")

        handoff_event_limit = len(seed.acceptance_criteria) + 1
        handoff_events = await self._event_store.query_execution_related_events(
            execution_id,
            event_type="execution.ac.uncertain_handoff_required",
            limit=handoff_event_limit,
        )
        if len(handoff_events) >= handoff_event_limit:
            raise RuntimeError("parallel uncertain handoff replay exceeds its execution-wide bound")
        uncertain_handoffs: set[int] = set()
        for event in handoff_events:
            if event.type != "execution.ac.uncertain_handoff_required":
                continue
            data = event.data
            if not _mapping_has_exact_keys(data, _PARALLEL_UNCERTAIN_HANDOFF_KEYS):
                raise RuntimeError("parallel uncertain handoff has an invalid event envelope")
            if data.get("session_id") != session_id:
                continue
            root_ac_index = data.get("root_ac_index")
            if type(root_ac_index) is not int:
                raise RuntimeError("parallel uncertain handoff has an invalid root AC index")
            if root_ac_index not in relevant:
                continue
            criterion = seed.acceptance_criteria[root_ac_index]
            semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
            if (
                data.get("schema_version") != 1
                or data.get("execution_id") != execution_id
                or data.get("semantic_ac_key") != semantic_ac_key
                or data.get("call_site") != "parallel"
                or data.get("reason") != "sibling_cancelled_after_execution_authority_entry"
                or data.get("human_handoff_required") is not True
                or data.get("final_acceptance_declared") is not False
            ):
                raise RuntimeError("parallel uncertain handoff has invalid authority metadata")
            if root_ac_index in uncertain_handoffs:
                raise RuntimeError("parallel uncertain handoff is duplicated")
            uncertain_handoffs.add(root_ac_index)
            terminals[root_ac_index] = (
                "A sibling quota cancelled this AC after provider-effect entry; "
                "the provider-effect boundary is uncertain and human handoff is required."
            )

        unresolved_pauses: dict[int, RouteCandidate] = {}
        unresolved_pause_states: dict[int, _ParallelRouteResumeState] = {}
        # Fold the complete stable population oldest-to-newest in bounded-memory
        # pages. A repeated quota on one route replaces the prior provider handle
        # with the latest unconsumed dispatch boundary without imposing a total
        # event-count ceiling on otherwise valid durable history.
        async for event in replay_execution_events_chronologically(
            self._event_store,
            execution_id=execution_id,
            event_type="execution.ac.route_paused",
            page_size=_PARALLEL_PAUSE_REPLAY_PAGE_SIZE,
        ):
            if event.type != "execution.ac.route_paused":
                continue
            data = event.data
            if not _mapping_has_exact_keys(data, _PARALLEL_ROUTE_PAUSE_KEYS):
                raise RuntimeError("parallel route pause has an invalid event envelope")
            if data.get("session_id") != session_id:
                continue
            root_ac_index = data.get("root_ac_index")
            if type(root_ac_index) is not int:
                raise RuntimeError("parallel route pause has an invalid root AC index")
            if root_ac_index not in relevant:
                continue
            if (
                type(data.get("schema_version")) is not int
                or data.get("schema_version") != 2
                or data.get("execution_id") != execution_id
                or data.get("call_site") != "parallel"
                or data.get("recoverable_pause") is not True
                or data.get("final_acceptance_declared") is not False
            ):
                raise RuntimeError("parallel route pause has invalid authority metadata")
            criterion = seed.acceptance_criteria[root_ac_index]
            semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
            expected_episode = self._bounded_route_episode_id(
                seed,
                execution_id=execution_id,
                session_id=session_id,
                root_ac_index=root_ac_index,
            )
            raw_prior = data.get("prior_route_ids")
            if (
                data.get("semantic_ac_key") != semantic_ac_key
                or data.get("episode_id") != expected_episode
                or type(data.get("attempt_index")) is not int
                or not isinstance(raw_prior, list)
                or not all(type(route_id) is str for route_id in raw_prior)
            ):
                raise RuntimeError("parallel route pause crossed route identity")
            attempt_index = data["attempt_index"]
            prior_route_ids = tuple(raw_prior)
            if attempt_index != len(prior_route_ids) or attempt_index >= MAX_ROUTE_ATTEMPTS:
                raise RuntimeError("parallel route pause has an invalid attempt index")
            try:
                paused_candidate = RouteCandidate.from_contract_data(data.get("route"))
            except (TypeError, ValueError) as exc:
                raise RuntimeError("parallel route pause has an invalid route snapshot") from exc
            raw_resume_state = data.get("resume_state")
            if not _mapping_has_exact_keys(
                raw_resume_state,
                _PARALLEL_ROUTE_PAUSE_RESUME_KEYS,
            ):
                raise RuntimeError("parallel route pause has an invalid resume state")
            assert isinstance(raw_resume_state, Mapping)
            retry_attempt = raw_resume_state.get("retry_attempt")
            retry_prompt_extra = raw_resume_state.get("retry_prompt_extra")
            raw_siblings = raw_resume_state.get("sibling_acs")
            route_id_override = raw_resume_state.get("route_id_override")
            raw_expected_candidate = raw_resume_state.get("expected_route_candidate")
            runtime_scope_id = raw_resume_state.get("runtime_scope_id")
            dispatch_id = raw_resume_state.get("dispatch_id")
            capsule_fingerprint = raw_resume_state.get("capsule_fingerprint")
            if (
                type(retry_attempt) is not int
                or retry_attempt != attempt_index
                or not isinstance(retry_prompt_extra, str)
                or len(retry_prompt_extra) > 16_384
                or not isinstance(raw_siblings, list)
                or len(raw_siblings) > len(seed.acceptance_criteria)
                or route_id_override is not None
                and (not isinstance(route_id_override, str) or not route_id_override)
                or not isinstance(runtime_scope_id, str)
                or not runtime_scope_id
                or not isinstance(dispatch_id, str)
                or len(dispatch_id) != 32
                or any(char not in "0123456789abcdef" for char in dispatch_id)
                or not isinstance(capsule_fingerprint, str)
                or len(capsule_fingerprint) != 71
                or not capsule_fingerprint.startswith("sha256:")
                or any(char not in "0123456789abcdef" for char in capsule_fingerprint[7:])
            ):
                raise RuntimeError("parallel route pause has malformed capsule-bearing state")
            parsed_siblings: list[_SiblingACRef] = []
            seen_sibling_indices: set[int] = set()
            for raw_sibling in raw_siblings:
                if not _mapping_has_exact_keys(
                    raw_sibling,
                    frozenset({"ac_index", "content"}),
                ):
                    raise RuntimeError("parallel route pause has malformed sibling state")
                assert isinstance(raw_sibling, Mapping)
                sibling_index = raw_sibling.get("ac_index")
                sibling_content = raw_sibling.get("content")
                if (
                    type(sibling_index) is not int
                    or sibling_index < 0
                    or sibling_index >= len(seed.acceptance_criteria)
                    or sibling_index in seen_sibling_indices
                    or sibling_content != ac_text(seed.acceptance_criteria[sibling_index])
                ):
                    raise RuntimeError("parallel route pause crossed its sibling population")
                seen_sibling_indices.add(sibling_index)
                parsed_siblings.append((sibling_index, sibling_content))
            try:
                expected_route_candidate = (
                    RouteCandidate.from_contract_data(raw_expected_candidate)
                    if raw_expected_candidate is not None
                    else None
                )
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "parallel route pause has an invalid expected route snapshot"
                ) from exc
            if prior_route_ids:
                if (
                    route_id_override != paused_candidate.route_id
                    or expected_route_candidate != paused_candidate
                    or not retry_prompt_extra
                ):
                    raise RuntimeError(
                        "parallel route pause lost its durable successor capsule inputs"
                    )
            elif (
                route_id_override is not None
                or expected_route_candidate is not None
                or retry_prompt_extra
            ):
                raise RuntimeError("parallel initial route pause invented successor capsule inputs")
            parsed_resume_state = _ParallelRouteResumeState(
                candidate=paused_candidate,
                retry_attempt=retry_attempt,
                retry_prompt_extra=retry_prompt_extra,
                sibling_acs=tuple(parsed_siblings),
                route_id_override=route_id_override,
                expected_route_candidate=expected_route_candidate,
                runtime_scope_id=runtime_scope_id,
                dispatch_id=dispatch_id,
                capsule_fingerprint=capsule_fingerprint,
            )

            history = histories[root_ac_index]
            if attempt_index < len(history):
                # A later judgment/observation consumed this pause.  It must be
                # the exact same attempt, otherwise replay crossed identities.
                if (
                    prior_route_ids != history[:attempt_index]
                    or history[attempt_index] != paused_candidate.route_id
                ):
                    raise RuntimeError("parallel route pause was consumed by a different route")
                continue
            if attempt_index != len(history) or prior_route_ids != history:
                raise RuntimeError("parallel route pause does not follow durable route history")
            if root_ac_index in uncertain_handoffs:
                # The later uncertain handoff deliberately consumes replay
                # authority for this previously paused AC.
                continue
            if (
                root_ac_index in terminals
                or root_ac_index in provisional_successes
                or root_ac_index in active_partial_roots
            ):
                raise RuntimeError("parallel route pause contradicts a terminal route state")
            expected_paused_candidate = overrides.get(root_ac_index)
            if history and expected_paused_candidate != paused_candidate:
                raise RuntimeError(
                    "parallel route pause drifted from its durable successor snapshot"
                )

            investment_spec = (
                criterion.investment if isinstance(criterion, AcceptanceCriterionSpec) else None
            )
            expected_effort, _expected_effort_kwargs = resolve_execute_effort(
                self._adapter,
                base_effort=self._reasoning_effort,
                is_decomposed_child=False,
                retry_attempt=0,
                investment_assessment=assess_investment(investment_spec),
            )
            live_projection = self._build_route_compat_projection(
                model_router=self._model_router,
                effort=expected_effort.level,
            )
            live_registry = build_compat_escalation_registry(live_projection)
            live_candidate = (
                next(
                    (
                        candidate
                        for candidate in live_registry.candidates
                        if candidate.route_id == paused_candidate.route_id
                    ),
                    None,
                )
                if live_registry is not None
                else None
            )
            if live_candidate != paused_candidate:
                raise RuntimeError("parallel route pause detected route configuration drift")
            if not history:
                initial = admit_compat_escalation_route(
                    live_projection,
                    effort=expected_effort.level,
                )
                if initial.selected != paused_candidate:
                    raise RuntimeError("parallel route pause drifted from exact initial admission")
            prior_unresolved = unresolved_pauses.get(root_ac_index)
            if prior_unresolved is not None and prior_unresolved != paused_candidate:
                raise RuntimeError("parallel route pause has conflicting unconsumed snapshots")
            unresolved_pauses[root_ac_index] = paused_candidate
            unresolved_pause_states[root_ac_index] = parsed_resume_state

        for root_ac_index, paused_candidate in unresolved_pauses.items():
            resume_state = unresolved_pause_states[root_ac_index]
            if resume_state.expected_route_candidate is not None:
                overrides[root_ac_index] = paused_candidate
            self._parallel_route_resumes[root_ac_index] = resume_state
        return histories, overrides, terminals, provisional_successes

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
        ac_criterion = seed.acceptance_criteria[ac_idx]
        rerun_kwargs: dict[str, Any] = {
            "ac_index": ac_idx,
            "ac_content": ac_text(ac_criterion),
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
            "ac_spec": (
                ac_criterion if isinstance(ac_criterion, AcceptanceCriterionSpec) else None
            ),
            "investment_spec": (
                ac_criterion.investment
                if isinstance(ac_criterion, AcceptanceCriterionSpec)
                else None
            ),
            "decomposition_trustworthy": False,
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
        verify_gate_outcome: _VerifyGateOutcome | None = None,
        verify_gate_replaces_all_evidence: bool = False,
    ) -> str | None:
        """Return the fat-harness rejection reason for an atomic leaf."""
        if not self._fat_harness_mode or not runtime_success:
            return None
        if verify_gate_outcome is not None:
            if verify_gate_outcome.environment_unverifiable:
                return None
            if not verify_gate_outcome.passed:
                return f"Verify gate failed: {verify_gate_outcome.reason}"
            if verify_gate_replaces_all_evidence:
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
        has_success_contract: bool = False,
        has_expected_artifacts: bool = False,
        verify_gate_active: bool = False,
        force_runtime_transcript: bool = False,
        task_cwd_override: str | None = None,
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

        _invoke_execution_authority_guard(self)
        verifier = self._authority_verifier
        try:
            effective_schema = _effective_evidence_schema_for_ac(
                self._execution_profile,
                ac_content,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
            )
            effective_profile = _profile_with_evidence_schema(
                self._execution_profile, effective_schema
            )
            scoped_evidence = _scoped_evidence_record_for_ac(
                self._execution_profile,
                ac_content,
                typed_evidence,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
            )
            verdict = (
                verifier(
                    profile=effective_profile,
                    ac=ac_content,
                    leaf_output=final_message,
                    record=scoped_evidence,
                )
                if verifier is not None and not force_runtime_transcript
                # Do not route acceptance through the mutable executor wrapper.
                # Foundation A captured this closed transcript verifier at
                # construction and has just checked that binding above.
                else self._authority_transcript_verifier(
                    messages=messages,
                    typed_evidence=scoped_evidence,
                    ac_content=ac_content,
                    execution_profile=self._execution_profile,
                    task_cwd=task_cwd_override or self._task_cwd,
                    adapter_working_directory=(
                        task_cwd_override or self._adapter.working_directory
                    ),
                    has_success_contract=has_success_contract,
                    has_expected_artifacts=has_expected_artifacts,
                    verify_gate_active=verify_gate_active,
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
        has_success_contract: bool = False,
        has_expected_artifacts: bool = False,
        verify_gate_active: bool = False,
        task_cwd_override: str | None = None,
    ) -> VerifierVerdict:
        return self._authority_transcript_verifier(
            messages=messages,
            typed_evidence=typed_evidence,
            ac_content=ac_content,
            execution_profile=self._execution_profile,
            task_cwd=task_cwd_override or self._task_cwd,
            adapter_working_directory=(task_cwd_override or self._adapter.working_directory),
            has_success_contract=has_success_contract,
            has_expected_artifacts=has_expected_artifacts,
            verify_gate_active=verify_gate_active,
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
        has_success_contract: bool = False,
        has_expected_artifacts: bool = False,
        verify_gate_active: bool = False,
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
                _effective_evidence_schema_for_ac(
                    self._execution_profile,
                    ac_content,
                    has_success_contract=has_success_contract,
                    has_expected_artifacts=has_expected_artifacts,
                    verify_gate_active=verify_gate_active,
                ).required
            ),
            "observe_only": not self._fat_harness_mode,
            "enforced": self._fat_harness_mode
            and not (
                verifier_verdict is not None
                and verifier_verdict.status is VerifierStatus.UNAVAILABLE
            ),
            "fat_harness_mode": self._fat_harness_mode,
            "enforcement_error": enforcement_error,
            "has_success_contract": has_success_contract,
            "has_expected_artifacts": has_expected_artifacts,
            "verify_gate_active": verify_gate_active,
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
                    has_success_contract=has_success_contract,
                    has_expected_artifacts=has_expected_artifacts,
                    verify_gate_active=verify_gate_active,
                )
            )
            data["ignored_out_of_scope_evidence"] = _out_of_scope_evidence_values_for_ac(
                self._execution_profile,
                ac_content,
                typed_evidence,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
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
    "ParallelExecutionCancelled",
]
