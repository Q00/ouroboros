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
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal

import anyio
from rich.console import Console

from ouroboros.core.seed import (
    AcceptanceCriterionSpec,
    InvestmentSpec,
    ac_text,
    derive_semantic_ac_key,
)
from ouroboros.core.session_signal import (
    SessionSignalMode,
    bounded_session_signal_reply,
)
from ouroboros.events.session_signal import (
    create_session_signal_applied_event,
    create_session_signal_completed_event,
    create_session_signal_delivery_started_event,
    create_session_signal_delivery_uncertain_event,
    create_session_signal_rejected_event,
)

# Import the harness submodules directly, NOT the ``ouroboros.harness`` package
# aggregate: ``harness.__init__`` pulls in ``deliver_routing`` which imports from
# ``ouroboros.orchestrator``, so importing the aggregate here would re-enter a
# partially-initialized ``harness`` during ``orchestrator`` package import. The
# concrete submodules below import nothing from ``orchestrator``, breaking the cycle.
from ouroboros.harness.claim_term_guard import strict_deterministic_claim_term_guard
from ouroboros.harness.decomposition_attestation import (
    DecompositionAttestation,
    DecompositionTrustAxis,
    DecompositionTrustVerdict,
    ParentVerifyOutcome,
    SiblingVerifyOutcome,
    attest_decomposition,
)
from ouroboros.harness.deliver_gate import (
    DeliverEvidenceClaim,
    DeliverEvidenceFact,
    evaluate_deliver_claim,
    load_ac_evidence_manifest,
)
from ouroboros.harness.journal import EvidenceEntry, EvidenceManifest
from ouroboros.harness.traceguard_validator import validate_evidence_claims
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.ac_runtime_handle_manager import ACRuntimeHandleManager
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
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
    params_from_profile,
)
from ouroboros.orchestrator.decomposition_policy import (
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
from ouroboros.orchestrator.effort_routing import (
    DEFAULT_EFFORT_CEILING,
    assess_investment,
    resolve_execute_effort,
)
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
from ouroboros.orchestrator.lateral_escalation import (
    LateralEscalationState,
    advance_lateral_escalation,
    build_persona_retry_prompt,
    is_terminal_state_failure,
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
from ouroboros.orchestrator.model_routing import (
    DEFAULT_TIER_CEILING,
    decide_model,
    resolve_execute_model,
    tier_from_profile_hint,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
    StageExecutionOutcome,
)
from ouroboros.orchestrator.profile_loader import ExecutionProfile, SuggestedModelTier
from ouroboros.orchestrator.rate_limit import (
    RateLimitBackoff,
    RateLimitGate,
    build_rate_limit_gate,
    estimate_runtime_request_tokens,
)
from ouroboros.orchestrator.runtime_param_negotiation import (
    announce_execution_param_degradations,
)
from ouroboros.orchestrator.shadow_replay import isolated_workspace, run_shadow_replay
from ouroboros.orchestrator.synapse import (
    SessionSignalTarget,
    render_after_turn_signal_prompt,
    render_inform_signal_prompt,
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
    from ouroboros.orchestrator.model_routing import ModelRouter
    from ouroboros.orchestrator.synapse import SessionSignalHub
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)


def _is_session_signal_application_acknowledgement(message: AgentMessage) -> bool:
    """Return whether a resumed-turn message proves provider context entry."""
    subtype = message.data.get("subtype")
    if message.type == "assistant":
        return bool(message.content.strip()) and subtype not in {"error", "runtime_error"}
    return message.type == "result" and subtype == "success"


def _bounded_session_signal_runtime_reply(messages: list[AgentMessage]) -> str | None:
    """Build one bounded provider reply without persisting a raw transcript.

    Some CLIs emit one assistant message while streaming transports such as
    Goose emit many token chunks.  Prefer an explicit completion payload when
    present; otherwise concatenate only the acknowledging assistant chunks from
    this signal turn.  A successful result message is the final fallback.
    """
    assistant_messages = [
        message
        for message in messages
        if message.type == "assistant"
        and _is_session_signal_application_acknowledgement(message)
        and message.content.strip()
    ]
    completion_messages = [
        message for message in assistant_messages if message.data.get("subtype") == "completion"
    ]
    if completion_messages:
        return bounded_session_signal_reply(completion_messages[-1].content)
    if assistant_messages:
        return bounded_session_signal_reply(
            "".join(message.content for message in assistant_messages)
        )

    for message in reversed(messages):
        if message.type != "result":
            continue
        if not _is_session_signal_application_acknowledgement(message):
            continue
        if message.content.strip():
            return bounded_session_signal_reply(message.content)
    return None


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
_TOKEN_SPEND_FALLBACK_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
_TOKEN_USAGE_KEYS: tuple[str, ...] = (
    *_TOKEN_SPEND_FALLBACK_KEYS,
    "cached_input_tokens",
    "total_tokens",
)


def _finite_nonneg_number(value: object) -> float | None:
    """Return ``value`` as a finite, non-negative float, else ``None``.

    Mirrors ``frugality_proof._finite_number`` (rejects ``None``, booleans,
    non-numerics, NaN/inf) and additionally rejects negatives: a token count is a
    spend, and a negative spend is malformed telemetry that must be dropped rather
    than counted (a negative would understate the run's real spend and skew the
    proof's aggregate reduction).
    """
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _harvest_token_spend(
    messages: list[AgentMessage],
) -> tuple[float, dict[str, float]] | None:
    """Sum runtime-reported token usage across a leaf's message stream.

    Usage semantics are resolved PER MESSAGE before messages are added together:

    * a usable ``total_tokens`` is authoritative for that message;
    * otherwise spend is ``input_tokens + output_tokens`` plus Anthropic's
      additive ``cache_creation_input_tokens + cache_read_input_tokens``;
    * OpenAI's ``cached_input_tokens`` remains in the diagnostic breakdown but is
      never added separately because it is already a subset of ``input_tokens``.

    Token telemetry is all-or-nothing across the leaf. If a ``usage`` payload is
    malformed, or any present recognized counter is invalid, the whole attempt
    returns ``None``. Dropping only the bad component (or falling back when an
    invalid ``total_tokens`` is present) would undercount spend and can create a
    false frugality PASS. An absent payload or a valid payload with no spend
    counter contributes nothing; when no spend is observed the function returns
    ``None`` rather than fabricating a char-proxy or zero-token spend.

    Multiple usage-bearing messages in one stream (e.g. a Claude result message
    plus Codex ``turn.completed`` messages) are summed, so a decomposed child's
    full spend is attributed even when the runtime reports it in pieces.

    Returns:
        ``(token_spend, usage_breakdown)`` where ``usage_breakdown`` is the summed
        per-key total for every usable key, or ``None`` when no spend was seen.
    """
    breakdown: dict[str, float] = {}
    token_spend = 0.0
    observed_spend = False
    for message in messages:
        data = getattr(message, "data", None)
        if not isinstance(data, dict):
            continue
        if data.get("usage_invalid") is True:
            return None
        if "usage" not in data:
            continue
        usage = data["usage"]
        if not isinstance(usage, Mapping):
            return None
        usable_usage: dict[str, float] = {}
        for key in _TOKEN_USAGE_KEYS:
            if key not in usage:
                continue
            raw_value = usage[key]
            number = _finite_nonneg_number(raw_value)
            if number is None:
                return None
            usable_usage[key] = number
            breakdown[key] = breakdown.get(key, 0.0) + number

        total_tokens = usable_usage.get("total_tokens")
        if total_tokens is not None:
            token_spend += total_tokens
            observed_spend = True
            continue

        spend_components = [
            usable_usage[key] for key in _TOKEN_SPEND_FALLBACK_KEYS if key in usable_usage
        ]
        if spend_components:
            token_spend += sum(spend_components)
            observed_spend = True

    if (
        not observed_spend
        or not math.isfinite(token_spend)
        or any(not math.isfinite(value) for value in breakdown.values())
    ):
        return None
    return token_spend, breakdown


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
                _matching_journal_entries(manifest, field=field, value=match_value)
                if eligible
                else ()
            )
            handle = matches[0].handle if len(matches) == 1 else f"missing:{field}:{index}"
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
) -> tuple[EvidenceEntry, ...]:
    matches: list[EvidenceEntry] = []
    for entry in manifest.entries:
        if entry.ok is not True or not isinstance(entry.payload, Mapping):
            continue
        payload = entry.payload
        tool_name = payload.get("tool_name")
        if field == "files_touched":
            if tool_name not in _FILE_MUTATION_TOOLS:
                continue
            observed = payload.get("workspace_relative_path")
        else:
            if tool_name != "Bash":
                continue
            observed = payload.get("command")
            if not isinstance(observed, str):
                observed = payload.get("args_preview")
        if isinstance(observed, str) and observed.strip() == value:
            matches.append(entry)
    return tuple(matches)


def _structured_literal(value: str) -> str | None:
    """Quote a scalar for the strict key=value claim-term grammar."""
    if not value or "\n" in value or "\r" in value:
        return None
    for quote in ("`", '"', "'"):
        if quote not in value:
            return f"{quote}{value}{quote}"
    return None


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


def _revalidate_cached_verify_gate_outcome(
    *,
    spec: AcceptanceCriterionSpec,
    cwd: str,
    outcome: _VerifyGateOutcome,
) -> _VerifyGateOutcome:
    """Refresh filesystem evidence without replaying a cached command.

    Verify commands may be non-idempotent, so an atomic result caches their
    outcome for finalization. Expected artifacts live in the shared workspace,
    however, and sibling ACs can delete or replace them after the atomic gate.
    A cached success is therefore valid only while its artifact leg still
    passes at the final acceptance boundary.
    """
    if not outcome.passed or not spec.expected_artifacts:
        return outcome
    missing_artifacts = _missing_expected_artifacts(spec.expected_artifacts, cwd)
    if not missing_artifacts:
        return outcome
    return _VerifyGateOutcome(
        passed=False,
        reason="expected_artifacts missing: " + ", ".join(missing_artifacts),
        output_tail=outcome.output_tail,
        missing_artifacts=missing_artifacts,
    )


def _decomposition_attestation_from_event_data(
    data: Mapping[str, Any],
) -> DecompositionAttestation | None:
    """Reconstruct a :class:`DecompositionAttestation` from durable event data.

    Inverse of :meth:`DecompositionAttestation.to_event_data`, used to replay
    a persisted ``execution.ac.decomposition_attested`` event back into the
    same shape ``_attest_decomposition_round`` originally produced (Fix 2,
    round 2). Fails closed: any payload that does not round-trip cleanly
    (unknown enum value, wrong type, missing required field) returns
    ``None`` -- treated the same as "no attestation recorded for this node
    id", never optimistically defaulted to a trustworthy verdict.
    """
    node_id = data.get("node_id")
    verdict_raw = data.get("verdict")
    if not isinstance(node_id, str) or not node_id.strip():
        return None
    if not isinstance(verdict_raw, str):
        return None
    try:
        verdict = DecompositionTrustVerdict(verdict_raw)
    except ValueError:
        return None

    failed_axis_raw = data.get("failed_axis")
    failed_axis: DecompositionTrustAxis | None = None
    if failed_axis_raw is not None:
        if not isinstance(failed_axis_raw, str):
            return None
        try:
            failed_axis = DecompositionTrustAxis(failed_axis_raw)
        except ValueError:
            return None

    failed_sibling_id = data.get("failed_sibling_id")
    if failed_sibling_id is not None and not isinstance(failed_sibling_id, str):
        return None

    reason = data.get("reason")
    if not isinstance(reason, str):
        reason = ""

    # Round-6 fix: cross-validate the payload against the only shapes
    # ``attest_decomposition`` ever produces, and against the independently
    # serialized ``trustworthy`` boolean ``to_event_data`` always writes.
    # ``DecompositionAttestation.trustworthy`` is COMPUTED from ``verdict``,
    # so a payload whose ``verdict`` string was corrupted to "trustworthy"
    # while ``trustworthy``/``failed_axis``/``reason`` still describe a
    # failure would otherwise reconstruct into a spuriously-trustworthy
    # attestation and silently authorize the cheap-tier discount. Any
    # inconsistency fails closed (``None`` == "no attestation recorded").
    trustworthy_raw = data.get("trustworthy")
    if not isinstance(trustworthy_raw, bool):
        return None
    if trustworthy_raw is not (verdict is DecompositionTrustVerdict.TRUSTWORTHY):
        return None
    if verdict is DecompositionTrustVerdict.TRUSTWORTHY:
        # A trustworthy verdict never carries a failure attribution.
        if failed_axis is not None or failed_sibling_id is not None:
            return None
    else:
        # UNTRUSTWORTHY and INDETERMINATE always attribute a failed axis,
        # and only SIBLING_GATE ever names a specific sibling.
        if failed_axis is None:
            return None
        if failed_sibling_id is not None and failed_axis is not DecompositionTrustAxis.SIBLING_GATE:
            return None
        # An evaluated sibling-gate FAILURE always names the failing sibling.
        if (
            verdict is DecompositionTrustVerdict.UNTRUSTWORTHY
            and failed_axis is DecompositionTrustAxis.SIBLING_GATE
            and failed_sibling_id is None
        ):
            return None

    return DecompositionAttestation(
        node_id=node_id,
        verdict=verdict,
        failed_axis=failed_axis,
        failed_sibling_id=failed_sibling_id,
        reason=reason,
    )


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
        decomposition_mode: Literal["preflight", "bounce_only", "off"] = "preflight",
        max_concurrent: int = 3,
        max_decomposition_depth: int = DEFAULT_MAX_DECOMPOSITION_DEPTH,
        checkpoint_store: Any | None = None,
        inherited_runtime_handle: RuntimeHandle | None = None,
        task_cwd: str | None = None,
        execution_profile: ExecutionProfile | None = None,
        fat_harness_mode: bool = False,
        atomic_verifier: Verifier | None = None,
        reasoning_effort: str | None = None,
        model_router: ModelRouter | None = None,
        run_verify_commands: bool = True,
        verify_command_timeout_seconds: int = 600,
        ac_retry_attempts: int = 0,
        cross_harness_redispatch: bool | None = None,
        shadow_replay_enabled: bool = False,
        session_signal_hub: SessionSignalHub | None = None,
        parked_retry_backoff_seconds: float = 300.0,
        lateral_escalation_enabled: bool = False,
    ):
        """Initialize executor.

        Args:
            adapter: Agent runtime for execution.
            event_store: Event store for progress tracking.
            console: Rich console for output.
            enable_decomposition: Enable Claude to decompose complex ACs.
            decomposition_mode: Whether decomposition runs before execution,
                only after a classified bounce, or not at all.
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
            parked_retry_backoff_seconds: Backoff between retries once a root
                AC has exhausted the lateral-persona escalation ladder (Task
                2) and is "parked for operator" attention. Mirrors
                ``EconomicsConfig.parked_retry_backoff_seconds``.
            lateral_escalation_enabled: Opt-in switch for the lateral-persona
                escalation ladder (Task 2). Default OFF, like
                ``shadow_replay_enabled`` — an AC that keeps retrying forever
                instead of surfacing FAILED is a significant behavior change
                that direct/test callers must not get for free. Real run
                paths (CLI `ooo run` via the runner) turn it on.
        """
        self._adapter = adapter
        self._event_store = event_store
        self._console = console or Console()
        if decomposition_mode not in {"preflight", "bounce_only", "off"}:
            msg = f"Unsupported decomposition_mode: {decomposition_mode!r}"
            raise ValueError(msg)
        self._decomposition_mode: Literal["preflight", "bounce_only", "off"] = (
            "off" if not enable_decomposition else decomposition_mode
        )
        self._enable_decomposition = self._decomposition_mode != "off"
        self._max_decomposition_depth = max(0, max_decomposition_depth)
        approval_mode = getattr(adapter, "permission_mode", None)
        self._inherited_runtime_handle = (
            replace(inherited_runtime_handle, approval_mode=approval_mode.strip())
            if inherited_runtime_handle is not None
            and isinstance(approval_mode, str)
            and approval_mode.strip()
            else inherited_runtime_handle
        )
        self._task_cwd = task_cwd
        self._execution_profile = execution_profile
        self._fat_harness_mode = fat_harness_mode
        self._run_verify_commands = run_verify_commands
        self._verify_command_timeout_seconds = max(1, verify_command_timeout_seconds)
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
        # Opt-in shadow-replay baseline harness (frugality-proof AC5). Default OFF:
        # replaying a decomposed child at the parent tier doubles token cost, so
        # this is an experiment lever, never a production default. When on, a
        # successful decomposed child is re-executed in an isolated workspace to
        # measure its parent-tier baseline spend. See ``shadow_replay`` module.
        self._shadow_replay_enabled = shadow_replay_enabled
        self._session_signal_hub = session_signal_hub
        self._atomic_verifier = atomic_verifier
        self._coordinator = LevelCoordinator(
            adapter,
            inherited_runtime_handle=self._inherited_runtime_handle,
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
        self._decomposition_decisions: dict[str, DecompositionDecisionRecord] = {}
        # Gate-anchored decomposition trust attestation (Task 1, RLM thesis
        # hardening): the LATEST attestation computed for a finished
        # decomposition round, keyed by the round's node id. ``node_id`` is
        # stable across same-root retries (it does not encode retry_attempt),
        # so a prior round's untrustworthy verdict keeps conditioning THIS
        # root AC's next retry even after the executor re-decomposes it.
        self._decomposition_attestations: dict[str, DecompositionAttestation] = {}
        # Lateral-persona escalation ladder (Task 2): per-root-AC state, keyed
        # by ``ac_index``. Injectable sleep so tests never wait through a real
        # backoff duration.
        self._lateral_escalation_states: dict[int, LateralEscalationState] = {}
        # Fix 7 (round 2, BLOCKING) defense-in-depth: the two contract
        # boundaries that populate this value (EconomicsConfig's Pydantic
        # field validator, and the resume-time
        # ``_valid_retry_policy_contract`` re-check in runner.py) both now
        # reject non-finite values, but this low-level constructor is a
        # direct, unvalidated entry point of its own. ``max(1.0, ...)`` only
        # enforces a floor -- it happily lets ``float("inf")`` through, which
        # reaches ``asyncio.sleep(inf)`` below and hangs that AC's slot
        # forever with no operator-visible signal. Fail closed here too
        # rather than silently clamping to an arbitrary finite ceiling.
        if not math.isfinite(parked_retry_backoff_seconds):
            msg = "parked_retry_backoff_seconds must be finite"
            raise ValueError(msg)
        self._parked_retry_backoff_seconds = max(1.0, parked_retry_backoff_seconds)
        self._lateral_escalation_enabled = lateral_escalation_enabled
        self._sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
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
        self._alt_harness_status_by_root: dict[int, str] = {}
        self._recovery_exhausted_emitted: set[tuple[str, int]] = set()

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

    async def _replay_with_retry(
        self, aggregate_type: str, aggregate_id: str, *, max_retries: int = 3
    ) -> list[Any] | None:
        """Replay durable events with retry, mirroring ``_safe_emit_event`` (Fix 5,
        round 3, BLOCKING).

        Attestation/escalation/parked state now controls live routing
        decisions (child model discount, persona/parking replay), so a
        failed READ must never be silently treated as "no prior state
        exists" — that is fail-OPEN and directly violates this PR's
        fail-closed mandate (a restart could silently re-authorize an
        already-untrustworthy cheap child tier, repeat already-tried
        personas, or lose parked status).

        Returns:
            The replayed events on success (an empty list is a LEGITIMATE
            "nothing was ever written" result, distinct from failure).
            ``None`` only when every attempt raised — callers MUST treat
            ``None`` as "we don't know" and fail closed for whatever piece
            of state they are reconstructing, never fall back to acting as
            if an empty/optimistic history were confirmed.
        """
        for attempt in range(max_retries):
            try:
                return await self._event_store.replay(aggregate_type, aggregate_id)
            except Exception as e:
                if attempt < max_retries - 1:
                    wait = min(1.0 * (2**attempt), 5.0)
                    log.warning(
                        "parallel_executor.event_replay.retry",
                        aggregate_type=aggregate_type,
                        aggregate_id=aggregate_id,
                        attempt=attempt + 1,
                        error=str(e),
                    )
                    await anyio.sleep(wait)
                else:
                    log.error(
                        "parallel_executor.event_replay.failed",
                        aggregate_type=aggregate_type,
                        aggregate_id=aggregate_id,
                        attempts=max_retries,
                        error=str(e),
                    )
                    self._console.print(
                        f"  [yellow]Event replay degraded: {aggregate_type}/{aggregate_id} "
                        f"failed after {max_retries} retries[/yellow]"
                    )
        return None

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

    @staticmethod
    def _checkpoint_seed_id(seed: Seed, session_id: str) -> str:
        """Return the stable identifier used to key RC3 checkpoints.

        ``Seed`` has no ``id`` attribute — its durable identifier lives at
        ``seed.metadata.seed_id`` (generated once at seed creation and
        preserved through serialization). The previous
        ``getattr(seed, "id", session_id)`` therefore ALWAYS fell through to
        the ``session_id`` fallback, and since a crash-restart run is created
        with a fresh session, the recovery load could never find the
        checkpoint the crashed run saved under the old session's id. Keying
        by ``seed.metadata.seed_id`` makes save and load agree across
        restarts of the same seed. ``session_id`` remains only as a graceful
        fallback for duck-typed seeds without metadata (mirrors the
        ``getattr(getattr(seed, "metadata", None), "seed_id", None)``
        convention already used in ``mcp.server.adapter``).
        """
        seed_id = getattr(getattr(seed, "metadata", None), "seed_id", None)
        if isinstance(seed_id, str) and seed_id:
            return seed_id
        return session_id

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
        force_frontier_routing: bool = False,
    ) -> list[ACExecutionResult | BaseException]:
        """Execute one batch of stage-ready ACs using the shared worker pool.

        ``same_runtime_budget_exhausted`` is forwarded to every AC in the batch:
        it is ``True`` only on the batch attempt that spends the AC's configured
        same-runtime retry budget, gating cross-harness redispatch (PR-X X1) so
        it never pre-empts those retries.

        ``force_frontier_routing`` (Round-5 Finding #2, BLOCKING) is set only
        by the lateral-escalation ladder's own redispatches: each ACTIVELY
        configured routing axis is forced to its true ceiling (frontier tier /
        max effort) instead of the incremental per-retry climb designed for
        the pre-ladder retry loop — the ladder's eligibility check already
        treats the AC as operating at those ceilings, so its dispatches must
        actually run there.
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
                        force_frontier_routing=force_frontier_routing,
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
                seed_id = self._checkpoint_seed_id(seed, session_id)
                load_result = self._checkpoint_store.load(seed_id)
                if hasattr(load_result, "is_ok") and load_result.is_ok and load_result.value:
                    cp = load_result.value
                    if cp.phase == "parallel_execution":
                        # Restore the ORIGINAL run's execution_id. A crash-
                        # restart run is created with a fresh execution_id,
                        # but every durable-state loader keyed off
                        # execution_id — ``_load_lateral_escalation_state``,
                        # ``_load_decomposition_attestation``, and the node
                        # ids derived via ``ExecutionNodeIdentity.root`` —
                        # reads the event aggregate of the ORIGINAL
                        # execution. Without restoring it, a recovered run
                        # replays an EMPTY aggregate and
                        # ``_resume_escalated_ac`` can never find the prior
                        # ladder/parked state (the AC would restart its
                        # escalation from scratch with a fresh, un-backed-off
                        # budget — exactly what 242e3529f exists to prevent).
                        # ``session_id`` is deliberately NOT restored:
                        # cancellation checks, signal-hub scoping, and
                        # progress events must keep following the LIVE
                        # session driving this recovery run.
                        saved_execution_id = cp.state.get("execution_id")
                        if isinstance(saved_execution_id, str) and saved_execution_id:
                            execution_id = saved_execution_id
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
                        raw_decisions = cp.state.get("decomposition_decisions", {})
                        if isinstance(raw_decisions, Mapping):
                            for raw_node_id, raw_record in raw_decisions.items():
                                if not isinstance(raw_node_id, str):
                                    continue
                                restored = DecompositionDecisionRecord.from_dict(raw_record)
                                if restored is not None and restored.node_id == raw_node_id:
                                    self._decomposition_decisions[raw_node_id] = restored
                        log.info(
                            "parallel_executor.recovery.resuming",
                            from_level=resume_from_level,
                            seed_id=seed_id,
                            execution_id=execution_id,
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

                        seed_id = self._checkpoint_seed_id(seed, session_id)
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
        """Cache and emit a finalized node decision once per distinct value."""
        if decision.node_id != node_identity.node_id:
            decision = DecompositionDecisionRecord(
                node_id=node_identity.node_id,
                source=decision.source,
                disposition=DecompositionDisposition.UNKNOWN,
                cause=decision.cause,
                reasons=("decomposition_decision_identity_mismatch",),
            )
        previous = self._decomposition_decisions.get(node_identity.node_id)
        self._decomposition_decisions[node_identity.node_id] = decision
        if previous != decision:
            await self._event_emitter.emit_decomposition_decision_finalized(
                execution_id=execution_id,
                session_id=session_id,
                mode=self._decomposition_mode,
                node_identity=node_identity,
                decision=decision,
            )
        return decision

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
        ac_spec: AcceptanceCriterionSpec | None = None,
    ) -> ACExecutionResult:
        """Dispatch one finalized split through the shared recursive child path."""
        sub_acs = [child.description for child in decision.children]
        # A prior round's gate-anchored attestation (Task 1) poisons THIS root
        # AC's future retries: once a decomposition round is proven untrustworthy
        # by re-running the parent's own verify gate, a fresh (unverified) SPLIT
        # proposal on the next retry must not re-admit the cheap child tier.
        # Fix 2 (round 2, BLOCKING): read through the durable-event-backed
        # loader, not the raw in-memory dict, so a fresh executor after a
        # resume/restart reconstructs a prior UNTRUSTWORTHY verdict instead
        # of silently forgetting it and re-admitting the discount.
        prior_attestation = await self._load_decomposition_attestation(
            node_identity.node_id, execution_id=execution_id, session_id=session_id
        )
        child_decomposition_trustworthy = decision.trustworthy and (
            prior_attestation is None or prior_attestation.trustworthy
        )
        display_label = (
            f"AC {node_identity.display_path}"
            if node_identity.depth == 0
            else f"Sub-AC {node_identity.display_path}"
        )
        self._console.print(
            f"  [cyan]{display_label} → Decomposed into {len(sub_acs)} Sub-ACs (parallel)[/cyan]"
        )
        self._flush_console()
        for idx, sub_ac in enumerate(sub_acs):
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
        sub_depth = depth + 1

        # Per-child artifact-slice oracle: when the PARENT's seed-authored
        # contract declares expected_artifacts and the decomposer partitioned
        # them across children (structurally validated against the parent's
        # set at proposal time -- see validate_decomposition_proposal), each
        # child is graded against ITS OWN slice with the same deterministic
        # artifact-existence oracle the parent's gate already trusts
        # (_missing_expected_artifacts). Because children dispatch strictly
        # sequentially, existence is snapshotted BEFORE each child runs and
        # re-checked AFTER it completes, so a child can never borrow credit
        # for a file an earlier step already created. Gated on
        # self._run_verify_commands to match every other success-contract
        # evaluation site in this file (artifact existence checks included --
        # see _apply_verify_gate and the --skip-completed gate). The runtime
        # subset filter below is defense in depth on top of the proposal-time
        # validation: a path outside the parent's declared set is silently
        # un-creditable, never evidence.
        artifact_oracle_active = (
            self._run_verify_commands
            and isinstance(ac_spec, AcceptanceCriterionSpec)
            and bool(ac_spec.expected_artifacts)
            and len(decision.children) == len(sub_acs)
        )
        artifact_cwd = ""
        child_artifact_slices: tuple[tuple[str, ...], ...] = tuple(() for _ in sub_acs)
        if artifact_oracle_active:
            assert ac_spec is not None  # narrowed by artifact_oracle_active
            parent_artifact_set = frozenset(ac_spec.expected_artifacts)
            child_artifact_slices = tuple(
                tuple(path for path in child.expected_artifacts if path in parent_artifact_set)
                for child in decision.children
            )
            artifact_cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
        # One record per child: (has_verify_command, passed, reason) in the
        # shape _attest_decomposition_round consumes for the sibling axis.
        child_artifact_attributions: list[tuple[bool, bool | None, str]] = [
            (False, None, "no artifact slice assigned to this child")
        ] * len(sub_acs)

        for idx, sub_ac in enumerate(sub_acs):
            assigned_artifacts = child_artifact_slices[idx]
            # Snapshot WHICH specific paths already existed before this child
            # dispatched (not merely how many): ANY pre-existing path in the
            # slice voids credit for the whole slice, so the post-dispatch
            # check needs the exact set. Kept in the slice's declared order
            # for stable reason strings.
            preexisting_paths: tuple[str, ...] = ()
            if assigned_artifacts:
                missing_before = frozenset(
                    _missing_expected_artifacts(assigned_artifacts, artifact_cwd)
                )
                preexisting_paths = tuple(
                    path for path in assigned_artifacts if path not in missing_before
                )
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
                    investment_spec=investment_spec,
                    decomposition_trustworthy=child_decomposition_trustworthy,
                    semantic_ac_key=semantic_ac_key,
                    # Forward the PARENT's spec for prompt context and
                    # semantic-key derivation. Children NEVER execute this
                    # borrowed contract as their own verify gate (Fix 1,
                    # round 3 -- see the ``verify_gate_active`` comment in
                    # ``_execute_atomic_ac``): running the parent's whole
                    # (possibly non-idempotent) contract once per child was
                    # both a cost bug and a false trust signal. A child's
                    # own evidence comes from the per-child artifact-slice
                    # oracle above (its assigned slice of the parent's
                    # seed-authored expected_artifacts), and the parent's
                    # own gate is still re-run exactly once, after all
                    # children finish, by ``_attest_decomposition_round``.
                    ac_spec=ac_spec,
                )
            except BaseException as exc:
                if isinstance(exc, anyio.get_cancelled_exc_class()):
                    raise
                sub_results[idx] = exc
            if assigned_artifacts:
                if preexisting_paths:
                    # Credit-borrowing guard: at least one path in this child's
                    # slice already existed before it dispatched (e.g. leftovers
                    # from a prior retry attempt on the same un-reset
                    # workspace), so creation of the slice cannot be attributed
                    # to this child. Partial pre-existence is treated exactly
                    # like full pre-existence: a slice is either fully,
                    # verifiably this child's own fresh work (every path absent
                    # before dispatch and present after) or it is not
                    # attributable at all -- never a mix, never partial credit.
                    # Fail closed to an un-evaluable axis (INDETERMINATE for
                    # this sibling), never a pass.
                    child_artifact_attributions[idx] = (
                        False,
                        None,
                        "assigned artifacts pre-existed dispatch: "
                        + ", ".join(preexisting_paths)
                        + "; cannot attribute this child's slice to its own work",
                    )
                else:
                    missing_after = _missing_expected_artifacts(assigned_artifacts, artifact_cwd)
                    if missing_after:
                        child_artifact_attributions[idx] = (
                            True,
                            False,
                            "assigned expected_artifacts missing after dispatch: "
                            + ", ".join(missing_after),
                        )
                    else:
                        child_artifact_attributions[idx] = (
                            True,
                            True,
                            "assigned expected_artifacts present after dispatch: "
                            + ", ".join(assigned_artifacts),
                        )

        final_sub_results: list[ACExecutionResult] = []
        for idx, result in enumerate(sub_results):
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
                status="completed" if result.success else "failed",
                node_identity=node_identity.child(idx),
            )

        duration = (datetime.now(UTC) - start_time).total_seconds()
        all_success = all(result.success for result in final_sub_results)

        # Task 1 (RLM thesis hardening): right after every sibling of this
        # decomposition round has finished dispatch, judge whether the round was
        # actually trustworthy — gate-anchored, not the pre-dispatch JSON-shape
        # heuristic on ``decision.trustworthy``. The verdict is cached per node id
        # so the NEXT retry of this same root AC can condition its child-tier
        # discount on it (see ``child_decomposition_trustworthy`` above).
        attestation, parent_verify_gate_outcome = await self._attest_decomposition_round(
            node_identity=node_identity,
            final_sub_results=final_sub_results,
            ac_spec=ac_spec,
            child_artifact_attributions=tuple(child_artifact_attributions),
        )
        self._decomposition_attestations[node_identity.node_id] = attestation
        # Round-5 Finding #4 (BLOCKING, superseding round 4's log-only stance):
        # a FUTURE resume that replays this node id and finds no matching
        # durable event cannot tell "never attested" from "attested but the
        # write failed" -- both are silence, and silence reads as "no prior
        # state" (``prior_attestation is None`` re-admits the child-tier
        # discount): fail-OPEN. Logging alone changes nothing about how this
        # run behaves after the failure, so it is not fail-closed. Give the
        # write bounded EXTRA retry rounds on top of ``_safe_emit_event``'s
        # own internal retries; if truly exhausted, prevent the semantic
        # advancement that depends on it: swap the verdict this run caches
        # AND returns for a fail-closed UNTRUSTWORTHY sentinel (the same
        # shape ``_load_decomposition_attestation`` synthesizes for an
        # unreadable log), so a trust verdict the durable log cannot
        # corroborate never authorizes the cheap child tier — in THIS run via
        # the in-memory cache, and in the returned result via the
        # ``next_scheduled`` routing probe.
        attestation_persisted = False
        for extra_attempt in range(3):
            if extra_attempt:
                await self._sleep(min(2.0 * (2**extra_attempt), 10.0))
            attestation_persisted = await self._event_emitter.emit_decomposition_attested(
                execution_id=execution_id,
                session_id=session_id,
                node_identity=node_identity,
                attestation=attestation,
                retry_attempt=retry_attempt,
            )
            if attestation_persisted:
                break
        if not attestation_persisted:
            log.error(
                "parallel_executor.decomposition_attestation.write_failed_correctness_risk",
                node_id=node_identity.node_id,
                execution_id=execution_id,
                verdict=attestation.verdict.value,
                trustworthy=attestation.trustworthy,
            )
            attestation = DecompositionAttestation(
                node_id=node_identity.node_id,
                verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
                failed_axis=None,
                failed_sibling_id=None,
                reason=(
                    "attestation write not durable after retries; failing closed "
                    "(untrustworthy) rather than acting on a trust verdict the "
                    "durable log cannot corroborate"
                ),
            )
            self._decomposition_attestations[node_identity.node_id] = attestation

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
            decomposition_attestation=attestation,
            # Fix 2 (round 3, BLOCKING): record the trust value ACTUALLY
            # consumed to dispatch THIS round's children -- computed BEFORE
            # this round ran (``child_decomposition_trustworthy`` above), not
            # this round's own just-computed ``attestation``. Model-routing
            # probes that ask "what was true for the dispatch that just
            # finished" must read this field; probes asking "what will be
            # true for the next dispatch" must read ``decomposition_attestation``
            # instead (this round's verdict becomes the NEXT round's prior).
            dispatched_decomposition_trustworthy=child_decomposition_trustworthy,
            # Thread the SAME parent verify-gate outcome computed for
            # attestation into the result's cache slot. The final acceptance
            # gate (``_apply_verify_gate``) reads this cache and, when
            # present, only cheaply revalidates the filesystem leg instead of
            # re-running a (possibly non-idempotent/mutating) verify_command a
            # second time for the same dispatch.
            verify_gate_outcome=parent_verify_gate_outcome,
        )

    async def _attest_decomposition_round(
        self,
        *,
        node_identity: ExecutionNodeIdentity,
        final_sub_results: list[ACExecutionResult],
        ac_spec: AcceptanceCriterionSpec | None,
        child_artifact_attributions: tuple[tuple[bool, bool | None, str], ...] | None = None,
    ) -> tuple[DecompositionAttestation, _VerifyGateOutcome | None]:
        """Gate-anchor one finished decomposition round (Task 1).

        Reuses the SAME verify-gate oracle every other AC success/failure
        decision reuses: :meth:`_run_ac_verify_gate`. Each sibling's own
        evidence stands for "did this sibling pass its own verify gate";
        the parent's contract, if any, is RE-RUN here — after every sibling
        has finished — against the current shared workspace, so a clobbering
        split fails this check even when every child individually reported
        success.

        Per-sibling evidence comes from two possible child-local sources:

        * ``child_artifact_attributions`` — the artifact-slice oracle computed
          by ``_execute_decomposition_children`` (this child's assigned slice
          of the PARENT's seed-authored ``expected_artifacts``, snapshotted
          before dispatch and re-checked after). This is the primary source
          for live decomposition rounds.
        * ``result.verify_gate_outcome`` — kept as a fallback for any code
          path that validly populates a per-child gate outcome. Children never
          run the PARENT's full verify_command gate (that per-child re-run was
          removed as both a cost and correctness bug), so this stays ``None``
          on the live path today.

        When BOTH sources exist for one sibling, they are merged fail-closed:
        the sibling passes only if EVERY present evidence leg passed. A
        conflicting signal (one leg passed, the other failed) therefore
        resolves to NOT-passed — ambiguity never resolves open.

        The parent gate is only actually re-run when ``self._run_verify_commands``
        is enabled, mirroring every other verify-gate call site in this file:
        an operator who disabled verify-command execution must not have shell
        commands run on their behalf by this path either. When disabled, the
        parent axis reports ``has_verify_command=False`` — the same
        fail-closed ``INDETERMINATE`` shape already used for "no contract at
        all" — never an assumed pass. (The artifact-slice oracle is gated on
        the same flag at its computation site.)

        The computed ``_VerifyGateOutcome`` (or ``None`` when not run) is
        returned alongside the attestation so the caller can cache it on the
        dispatch result; the final acceptance gate then reuses that cached
        outcome (see ``_apply_verify_gate``) instead of invoking a
        possibly-mutating verify_command a second time for the same dispatch.
        """
        sibling_outcomes: list[SiblingVerifyOutcome] = []
        for idx, result in enumerate(final_sub_results):
            gate = (
                result.verify_gate_outcome
                if isinstance(result.verify_gate_outcome, _VerifyGateOutcome)
                else None
            )
            artifact_evidence: tuple[bool, str] | None = None
            if (
                child_artifact_attributions is not None
                and idx < len(child_artifact_attributions)
                and child_artifact_attributions[idx][0]
                and child_artifact_attributions[idx][1] is not None
            ):
                _, artifact_passed, artifact_reason = child_artifact_attributions[idx]
                artifact_evidence = (bool(artifact_passed), artifact_reason)

            if artifact_evidence is not None and gate is not None:
                # Merge choice (documented, fail-closed): both legs are
                # child-local checks of THIS sibling's own slice of work, so a
                # failure on either is real negative evidence about this
                # sibling; the sibling only passes when every present leg
                # passed. Conflicting legs resolve to NOT-passed, never
                # passed.
                sibling_outcomes.append(
                    SiblingVerifyOutcome(
                        sibling_id=str(idx),
                        has_verify_command=True,
                        passed=artifact_evidence[0] and gate.passed,
                        reason=(
                            f"artifact-slice: {artifact_evidence[1]}; "
                            f"verify-gate: {gate.reason or 'passed'}"
                        ),
                    )
                )
            elif artifact_evidence is not None:
                sibling_outcomes.append(
                    SiblingVerifyOutcome(
                        sibling_id=str(idx),
                        has_verify_command=True,
                        passed=artifact_evidence[0],
                        reason=artifact_evidence[1],
                    )
                )
            else:
                # PRECEDENCE RISK (latent, guard-rail for future code): in
                # this branch the artifact-slice oracle produced NO evaluable
                # evidence -- which includes its deliberate DENIALS, e.g.
                # "assigned artifacts pre-existed dispatch" -- so a non-None
                # ``gate`` here would stand alone as this sibling's verify
                # signal, and ``gate.passed=True`` would bypass that denial
                # entirely. Today this is unreachable on the live path
                # (nothing populates a decomposition child's
                # ``verify_gate_outcome``; children never run the borrowed
                # parent gate), so the fallback is safe. If a future change
                # starts populating per-child gate outcomes, it MUST NOT let
                # a passing gate override an artifact-oracle denial: an
                # unevaluable-by-denial artifact record means "this child's
                # slice is not attributable", and no independent gate pass
                # may resurrect it into sibling-axis credit.
                #
                # Surface the artifact oracle's UNEVALUABLE record's reason
                # (e.g. "assigned artifacts pre-existed dispatch: ...") so an
                # INDETERMINATE round stays diagnosable, instead of the
                # generic no-contract string.
                unevaluable_artifact_reason = ""
                if (
                    child_artifact_attributions is not None
                    and idx < len(child_artifact_attributions)
                    and not child_artifact_attributions[idx][0]
                ):
                    unevaluable_artifact_reason = child_artifact_attributions[idx][2]
                sibling_outcomes.append(
                    SiblingVerifyOutcome(
                        sibling_id=str(idx),
                        has_verify_command=gate is not None,
                        passed=gate.passed if gate is not None else None,
                        reason=(
                            (gate.reason or "")
                            if gate is not None
                            else unevaluable_artifact_reason
                            or "sibling was dispatched without a structured verify contract"
                        ),
                    )
                )
        siblings = tuple(sibling_outcomes)

        parent_has_contract = isinstance(ac_spec, AcceptanceCriterionSpec) and bool(
            ac_spec.verify_command or ac_spec.expected_artifacts
        )
        parent_verify_gate_outcome: _VerifyGateOutcome | None = None
        if parent_has_contract and self._run_verify_commands:
            assert ac_spec is not None  # narrowed by parent_has_contract
            cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
            parent_verify_gate_outcome = await self._run_ac_verify_gate(spec=ac_spec, cwd=cwd)
            parent = ParentVerifyOutcome(
                has_verify_command=True,
                passed=parent_verify_gate_outcome.passed,
                reason=parent_verify_gate_outcome.reason or "",
            )
        else:
            parent = ParentVerifyOutcome(has_verify_command=False, passed=None)

        attestation = attest_decomposition(
            node_id=node_identity.node_id,
            siblings=siblings,
            parent=parent,
        )
        return attestation, parent_verify_gate_outcome

    def _build_decomposition_trace_summary(
        self,
        *,
        result: ACExecutionResult,
        ac_spec: AcceptanceCriterionSpec | None,
    ) -> DecompositionTraceSummary:
        """Project one failed attempt into bounded, secret-safe recovery evidence."""
        verdict = result.atomic_verifier_verdict
        tool_names = tuple(
            dict.fromkeys(
                message.tool_name
                for message in result.messages
                if isinstance(message.tool_name, str) and message.tool_name.strip()
            )
        )[:8]
        evidence_fields = (
            tuple(sorted(str(key) for key in result.typed_evidence.data))[:8]
            if result.typed_evidence is not None
            else ()
        )
        evidence_refs = tuple(verdict.evidence_used) if verdict is not None else ()
        verified_artifacts: list[str] = []
        remaining_artifacts: list[str] = []
        if ac_spec is not None and ac_spec.expected_artifacts:
            cwd = Path(self._task_cwd or self._adapter.working_directory or os.getcwd())
            for artifact in ac_spec.expected_artifacts[:8]:
                target = Path(artifact)
                if not target.is_absolute():
                    target = cwd / target
                (verified_artifacts if target.exists() else remaining_artifacts).append(artifact)

        failure_class = verdict.failure_class if verdict is not None else None
        retry_admission = (
            verdict.retry_admission.value
            if verdict is not None and hasattr(verdict.retry_admission, "value")
            else (str(verdict.retry_admission) if verdict is not None else None)
        )
        reasons = tuple(verdict.reasons) if verdict is not None else ()
        lines = [
            "attempted_tools=" + (", ".join(tool_names) if tool_names else "none-recorded"),
            "evidence_fields="
            + (", ".join(evidence_fields) if evidence_fields else "none-recorded"),
            "verified_artifacts="
            + (", ".join(verified_artifacts) if verified_artifacts else "none-recorded"),
            "remaining_artifacts="
            + (", ".join(remaining_artifacts) if remaining_artifacts else "none-recorded"),
            f"failure_class={failure_class or 'UNKNOWN'}",
            f"retry_admission={retry_admission or 'UNKNOWN'}",
            "verifier_reasons=" + ("; ".join(reasons) if reasons else "none-recorded"),
            f"failure_detail_present={bool(result.error or result.final_message)}",
        ]
        if ac_spec is not None:
            lines.append(f"verify_command_present={bool(ac_spec.verify_command)}")
            lines.append(f"output_assertion_present={bool(ac_spec.output_assertion)}")
        return summarize_decomposition_trace("\n".join(lines), evidence_refs=evidence_refs)

    async def _dispatch_decomposition_prompt(
        self,
        *,
        prompt: str,
        system_prompt: str,
        independent_session: bool = False,
    ) -> str:
        """Run one bounded tool-free decomposition-policy request.

        Semantic attestation must not resume the proposer conversation. Passing
        ``independent_session=True`` starts a fresh runtime session even when the
        parent executor inherited a resumable handle.
        """
        self._announce_param_degradations(system_prompt=system_prompt, tools=[])
        await self._await_dispatch_rate_budget(prompt=prompt, system_prompt=system_prompt)
        response_text = ""
        async with asyncio.timeout(DECOMPOSITION_TIMEOUT_SECONDS):
            async for message in self._adapter.execute_task(
                prompt=prompt,
                tools=[],
                system_prompt=system_prompt,
                resume_handle=None if independent_session else self._inherited_runtime_handle,
            ):
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
        return response_text.strip()

    async def _request_bounce_classification(
        self,
        *,
        trace: DecompositionTraceSummary,
    ) -> tuple[BounceCause, str, tuple[str, ...], bool]:
        """Ask a bounded tool-free classifier only for ambiguous failure causes."""
        prompt = (
            "Classify this failed execution attempt for recovery. Use only the bounded "
            "attempt evidence below. Do not infer complexity from task length or wording. "
            "Return ONLY JSON with cause, reason, evidence_refs, and has_remaining_scope. "
            "cause must be TOO_BIG, BAD_SPEC, ENVIRONMENT, MODEL, or UNKNOWN. TOO_BIG is "
            "allowed only when the trace shows attempted work and distinct parent scope "
            "still remaining.\n\n"
            f"## Bounded Attempt Trace\n{trace.summary}"
        )
        try:
            response = await self._dispatch_decomposition_prompt(
                prompt=prompt,
                system_prompt="You are a conservative execution-recovery classifier.",
            )
            if len(response) > 10_000:
                raise ValueError
            match = re.search(r"\{.*\}", response, re.DOTALL)
            payload = json.loads(match.group() if match is not None else response)
            if not isinstance(payload, dict):
                raise ValueError
            cause = BounceCause(payload.get("cause", BounceCause.UNKNOWN.value))
            reason = payload.get("reason", "")
            refs = payload.get("evidence_refs", ())
            remaining = payload.get("has_remaining_scope", False)
            if not isinstance(reason, str):
                reason = ""
            if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
                refs = []
            if type(remaining) is not bool:
                remaining = False
            bounded_refs = DecompositionTraceSummary(
                summary="",
                evidence_refs=tuple(refs[:8]),
            ).evidence_refs
            return (
                cause,
                redact_and_truncate_text(reason, max_chars=240),
                bounded_refs,
                remaining,
            )
        except (TimeoutError, ValueError, json.JSONDecodeError, TypeError):
            return BounceCause.UNKNOWN, "Bounce classifier returned no admissible cause.", (), False
        except Exception as exc:
            log.warning(
                "parallel_executor.bounce_classifier.error",
                error=redact_and_truncate_text(str(exc), max_chars=240),
            )
            return BounceCause.UNKNOWN, "Bounce classifier failed operationally.", (), False

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

        (
            proposed_cause,
            reason,
            proposed_refs,
            has_remaining_scope,
        ) = await self._request_bounce_classification(trace=trace)
        refs = tuple(dict.fromkeys((*trace.evidence_refs, *proposed_refs)))
        return classify_bounce(
            failure,
            admission,
            proposed_cause=proposed_cause,
            proposed_reasons=(reason,),
            evidence_refs=refs,
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
        if self._decomposition_mode != "bounce_only" or result.success:
            return None, None
        previous = self._decomposition_decisions.get(node_identity.node_id)
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

        if depth >= self._max_decomposition_depth:
            decision = await self._finalize_decomposition_decision(
                decision=DecompositionDecisionRecord(
                    node_id=node_identity.node_id,
                    source=DecompositionSource.BOUNCE,
                    disposition=DecompositionDisposition.ESCALATED,
                    cause=BounceCause.TOO_BIG,
                    reasons=("decomposition_depth_cap", classification.rationale),
                    evidence_refs=classification.evidence_refs,
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
            trace_summary=trace.summary,
            evidence_refs=classification.evidence_refs,
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
                ac_spec=ac_spec,
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
        force_frontier_routing: bool = False,
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
            ac_spec: The root AC's structured spec, when it carries a success
                contract, so the atomic leaf prompt can surface it. The batch
                layer passes it for top-level ACs; decomposition recursion
                (``_execute_decomposition_children``) forwards the SAME root
                spec to every child dispatch for prompt context, but a child
                never executes that borrowed contract as its own verify gate
                (Fix 1, round 3). A decomposed child's OWN evidence instead
                comes from the per-child artifact-slice oracle in
                ``_execute_decomposition_children``: its assigned slice of
                the parent's seed-authored ``expected_artifacts``, checked
                deterministically with a pre-dispatch existence snapshot.
            investment_spec: The top-level AC's investment authority. Recursive
                children inherit it because they jointly discharge the parent AC.
                This is tracked separately from ``ac_spec`` because it governs a
                different concern (spend authority, not the success contract).
            decomposition_trustworthy: Explicit deterministic trust for this unit's
                decomposition. Defaults fail closed; current live decomposition has
                no trusted producer.
            force_frontier_routing: Round-5 Finding #2 (BLOCKING). ``True`` only
                for lateral-escalation-ladder-owned redispatches: each ACTIVELY
                configured routing axis dispatches at its true ceiling (frontier
                tier / max effort) instead of the incremental per-retry climb.
                Forwarded to the atomic leaf and to a cross-harness replay of
                this same call; dormant axes stay dormant.

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

        node_decision = self._decomposition_decisions.get(node_identity.node_id)

        # Compatibility mode keeps preflight ordering, but every result is now a
        # persisted explicit decision and only a trusted SPLIT may lower children.
        if self._decomposition_mode == "preflight" and depth < self._max_decomposition_depth:
            display_label = (
                f"AC {node_identity.display_path}"
                if node_identity.depth == 0
                else f"Sub-AC {node_identity.display_path}"
            )
            self._console.print(f"  [dim]{display_label}: Analyzing complexity...[/dim]")
            self._flush_console()
            if node_decision is None:
                raw_decision = await self._try_decompose_ac(
                    ac_content=ac_content,
                    ac_index=ac_index,
                    seed_goal=seed_goal,
                    tools=tools,
                    system_prompt=system_prompt,
                    node_identity=node_identity,
                    session_id=session_id,
                    execution_id=execution_context_id,
                    retry_attempt=retry_attempt,
                    depth=depth,
                    ac_spec=ac_spec,
                    source=DecompositionSource.PREFLIGHT,
                )
                node_decision = self._coerce_decomposition_decision(
                    raw_decision,
                    node_identity=node_identity,
                    source=DecompositionSource.PREFLIGHT,
                )
                node_decision = await self._finalize_decomposition_decision(
                    decision=node_decision,
                    node_identity=node_identity,
                    execution_id=execution_context_id,
                    session_id=session_id,
                )

        if (
            node_decision is not None
            and node_decision.disposition is DecompositionDisposition.SPLIT
            and len(node_decision.children) >= MIN_SUB_ACS
            and (self._decomposition_mode == "preflight" or node_decision.trustworthy is True)
        ):
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
                ac_spec=ac_spec,
            )

        if (
            self._decomposition_mode == "preflight"
            and depth >= self._max_decomposition_depth
            and node_decision is None
        ):
            node_decision = await self._finalize_decomposition_decision(
                decision=DecompositionDecisionRecord(
                    node_id=node_identity.node_id,
                    source=DecompositionSource.PREFLIGHT,
                    disposition=DecompositionDisposition.ESCALATED,
                    reasons=("decomposition_depth_cap",),
                    compromise_reason="depth_cap_forced_atomic",
                ),
                node_identity=node_identity,
                execution_id=execution_context_id,
                session_id=session_id,
            )

        # Depth-limit canary: execution is forced atomic once the soft recursion
        # safety net is reached, so downstream stages can detect decomposition pressure.
        decomposition_depth_warning = (
            self._decomposition_mode == "preflight" and depth >= self._max_decomposition_depth
        )

        def _finalize_node_result(result: ACExecutionResult) -> ACExecutionResult:
            updates: dict[str, Any] = {"decomposition_decision": node_decision}
            if decomposition_depth_warning:
                updates["decomposition_depth_warning"] = True
            return replace(result, **updates)

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
            "investment_spec": investment_spec,
            "decomposition_trustworthy": decomposition_trustworthy,
            "semantic_ac_key": semantic_ac_key,
            "force_frontier_routing": force_frontier_routing,
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
                investment_spec=investment_spec,
                decomposition_trustworthy=decomposition_trustworthy,
                semantic_ac_key=semantic_ac_key,
                force_frontier_routing=force_frontier_routing,
            )
            if atomic_result.error != _STALL_SENTINEL:
                if not atomic_result.success:
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
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        cwd = self._task_cwd or self._adapter.working_directory
        alt_adapter = create_agent_runtime(
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
            atomic_verifier=self._atomic_verifier,
            reasoning_effort=self._reasoning_effort,
            # The router's backend-mismatch guard makes it inert on a different
            # backend, so passing it to the alt-harness executor is safe.
            model_router=self._model_router,
            cross_harness_redispatch=False,
            # The router is inert on a different backend, so the baseline resolves
            # no parent-tier model and the replay self-skips — threading the flag
            # just keeps the throwaway executor's behavior consistent.
            shadow_replay_enabled=self._shadow_replay_enabled,
            session_signal_hub=self._session_signal_hub,
        )
        return await alt_executor._execute_single_ac(**rerun_kwargs, retry_attempt=retry_attempt)

    @staticmethod
    def _parse_legacy_decomposition(
        response_text: str,
        *,
        min_sub_acs: int,
        max_sub_acs: int,
    ) -> list[str] | None:
        """Parse a legacy string-array response without granting it trust."""
        match = re.search(r"\[.*\]", response_text, re.DOTALL)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group())
        except json.JSONDecodeError:
            return None
        if (
            isinstance(parsed, list)
            and all(isinstance(item, str) and item.strip() for item in parsed)
            and min_sub_acs <= len(parsed) <= max_sub_acs
        ):
            return [item.strip() for item in parsed]
        return None

    @staticmethod
    def _parse_structured_decomposition(
        response_text: str,
        *,
        parent_text: str,
        min_sub_acs: int,
        max_sub_acs: int,
        parent_expected_artifacts: tuple[str, ...] = (),
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
            parent_expected_artifacts=parent_expected_artifacts,
        )
        if errors:
            return None, errors
        proposal = parse_decomposition_proposal(
            payload,
            parent_text=parent_text,
            min_children=min_sub_acs,
            max_children=max_sub_acs,
            parent_expected_artifacts=parent_expected_artifacts,
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
        """Run one independent bounded semantic attestation for a proposed split."""
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
            "simpler_units_established, and a reasons string array. All three booleans must "
            "be true to establish the split.\n\n"
            f"{profile_clause}"
            f"Parent criterion:\n{parent_text}\n\n"
            f"Bounded attempt trace:\n{trace_summary or 'none'}\n\n"
            "Proposal:\n"
            f"{json.dumps(proposal.to_dict(), sort_keys=True)}"
        )
        try:
            response = await self._dispatch_decomposition_prompt(
                prompt=prompt,
                system_prompt=system_prompt,
                independent_session=True,
            )
            if len(response) > 10_000:
                raise ValueError
            match = re.search(r"\{.*\}", response, re.DOTALL)
            payload = json.loads(match.group() if match is not None else response)
            if not isinstance(payload, dict):
                raise ValueError
            checks = (
                payload.get("coverage_established"),
                payload.get("non_overlap_established"),
                payload.get("simpler_units_established"),
            )
            reasons_raw = payload.get("reasons", ())
            reasons = (
                tuple(
                    redact_and_truncate_text(item, max_chars=240)
                    for item in reasons_raw[:7]
                    if isinstance(item, str) and item.strip()
                )
                if isinstance(reasons_raw, list)
                else ()
            )
            if all(value is True for value in checks):
                return True, ("semantic_attestation_established", *reasons)
            return False, ("semantic_attestation_not_established", *reasons)
        except (TimeoutError, ValueError, json.JSONDecodeError, TypeError):
            return False, ("semantic_attestation_unparseable",)
        except Exception as exc:
            log.warning(
                "parallel_executor.decomposition.attestation_error",
                error=redact_and_truncate_text(str(exc), max_chars=240),
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
        parent_expected_artifacts: tuple[str, ...] = (),
    ) -> str:
        """Build the single verifier-guided repair request for a generic proposal."""
        artifact_clause = ""
        child_example = '{"description":"...","coverage_claims":["..."],"verification_hint":"..."}'
        if parent_expected_artifacts:
            artifact_list = "\n".join(f"- {path}" for path in parent_expected_artifacts)
            artifact_clause = (
                "\nThe parent criterion declares these expected artifact paths:\n"
                f"{artifact_list}\n"
                "EVERY path above must be assigned to EXACTLY ONE child via that child's "
                '"expected_artifacts" field. Do not invent paths outside this list and do '
                "not assign the same path to more than one child.\n"
            )
            child_example = (
                '{"description":"...","coverage_claims":["..."],'
                '"verification_hint":"...",'
                '"expected_artifacts":["one/path/from/the/list/above"]}'
            )
        return (
            "Repair the rejected decomposition proposal exactly once. Return ONLY the "
            "structured JSON object described below; do not return ATOMIC or a string array.\n\n"
            f"Rejection reasons: {json.dumps(reasons)}\n\n"
            f"Parent criterion:\n{parent_text}\n"
            f"{artifact_clause}\n"
            f"Bounded attempt trace:\n{trace_summary or 'none'}\n\n"
            f"Return {min_sub_acs}-{max_sub_acs} children in this shape:\n"
            f'{{"children":[{child_example}],"covers_parent":true,"rationale":"..."}}'
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
        parent_expected_artifacts: tuple[str, ...] = (),
    ) -> tuple[DecompositionProposal | None, tuple[str, ...]]:
        """Apply structural validation followed by independent semantic attestation."""
        proposal, reasons = self._parse_structured_decomposition(
            response_text,
            parent_text=parent_text,
            min_sub_acs=min_sub_acs,
            max_sub_acs=max_sub_acs,
            parent_expected_artifacts=parent_expected_artifacts,
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
        source: DecompositionSource = DecompositionSource.PREFLIGHT,
        cause: BounceCause | None = None,
        trace_summary: str = "",
        evidence_refs: tuple[str, ...] = (),
    ) -> DecompositionDecisionRecord:
        """Decompose an AC and return a versioned, fail-closed decision."""
        del tools, system_prompt, retry_attempt
        # The PARENT's own seed-authored expected_artifacts (fixed before the
        # decomposer ever runs) is the only material a proposal may partition
        # into per-child artifact slices -- see validate_decomposition_proposal.
        parent_expected_artifacts: tuple[str, ...] = (
            tuple(ac_spec.expected_artifacts) if ac_spec is not None else ()
        )
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
        # Mirroring profile_lines: this block is only present when the parent
        # actually declares expected_artifacts, so a decomposer for a
        # contract-less parent is never tempted to invent the field.
        artifact_partition_lines = ""
        child_example_keys = (
            '"description":"...","coverage_claims":["distinct parent scope"],\n'
            '"verification_hint":"how this child is independently checked"'
        )
        if parent_expected_artifacts:
            artifact_list = "\n".join(f"- {path}" for path in parent_expected_artifacts)
            artifact_partition_lines = (
                "This criterion declares the following expected artifact paths:\n"
                f"{artifact_list}\n"
                "If you decompose, EVERY path above must be assigned to EXACTLY ONE child "
                'via that child\'s "expected_artifacts" JSON field. Do not invent paths '
                "outside this list and do not assign the same path to more than one "
                "child.\n"
            )
            child_example_keys += ',\n"expected_artifacts":["one/path/from/the/list/above"]'
        decompose_prompt = f"""Analyze this acceptance criterion and determine if it should be decomposed.

## Goal Context
{seed_goal}

## Acceptance Criterion ({ac_label})
{ac_content}

## Instructions
Default to ATOMIC. Each sub-AC becomes a separate agent session with its own full
context, so split only when the parent bundles multiple independently valuable
outcomes that can be verified separately.
{profile_lines}{artifact_partition_lines}
Decompose into {min_sub_acs}-{max_sub_acs} sub-ACs only when each child is simpler,
independently executable, and owns distinct parent scope. Multiple steps or files
alone are not evidence that a split is warranted.

If the AC is one focused outcome, respond with: ATOMIC

If decomposing, respond with ONLY this structured JSON object:
{{"children":[{{{child_example_keys}}}],
"covers_parent":true,"rationale":"why the children cover the parent without overlap"}}

Respond with either ATOMIC or the structured JSON object only.
"""
        if bounded_trace:
            decompose_prompt += f"\n\n## Bounded Attempt Trace\n{bounded_trace}"

        try:
            response_text = await self._dispatch_decomposition_prompt(
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
                    disposition=(
                        DecompositionDisposition.ATOMIC
                        if source is DecompositionSource.PREFLIGHT
                        else DecompositionDisposition.ESCALATED
                    ),
                    cause=cause,
                    reasons=("explicit_atomic",),
                    evidence_refs=evidence_refs,
                    compromise_reason=(
                        None
                        if source is DecompositionSource.PREFLIGHT
                        else "too_big_classifier_disagreed_with_decomposer"
                    ),
                )

            if "{" in response_text:
                proposal, proposal_reasons = await self._verify_generic_decomposition(
                    response_text=response_text,
                    parent_text=ac_content,
                    trace_summary=bounded_trace,
                    system_prompt=decomposition_system_prompt,
                    min_sub_acs=min_sub_acs,
                    max_sub_acs=max_sub_acs,
                    parent_expected_artifacts=parent_expected_artifacts,
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
                    parent_expected_artifacts=parent_expected_artifacts,
                )
                repaired_text = await self._dispatch_decomposition_prompt(
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
                    parent_expected_artifacts=parent_expected_artifacts,
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

            sub_acs = self._parse_legacy_decomposition(
                response_text,
                min_sub_acs=min_sub_acs,
                max_sub_acs=max_sub_acs,
            )
            if sub_acs is not None:
                log.warning(
                    "parallel_executor.decomposition.legacy_array_untrusted",
                    ac_index=ac_index,
                    sub_ac_count=len(sub_acs),
                    **profile_metadata,
                )
                return legacy_unverified_split_decision(
                    node_id=decision_identity.node_id,
                    source=source,
                    child_descriptions=sub_acs,
                    cause=cause,
                    reasons=("legacy_array_without_attestation",),
                    evidence_refs=evidence_refs,
                )

            log.warning(
                "parallel_executor.decomposition.unparseable_unknown",
                ac_index=ac_index,
                response_preview=redact_and_truncate_text(response_text, max_chars=100),
                **profile_metadata,
            )
            return DecompositionDecisionRecord(
                node_id=decision_identity.node_id,
                source=source,
                disposition=DecompositionDisposition.UNKNOWN,
                cause=cause,
                reasons=("unparseable_decomposition_response",),
                evidence_refs=evidence_refs,
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
                error=redact_and_truncate_text(str(exc), max_chars=240),
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

    def _suggested_model_tier_hint(self) -> str | None:
        """The profile-suggested starting model tier, or ``None`` for "no opinion".

        A profile's ``suggested_model_tier`` seeds the starting tier ONLY when
        it is something other than the shipped default MEDIUM ("no
        opinion"); MEDIUM leaves precedence with the router's own base/child
        logic and any explicit ``model_tier`` arg. Dormant by default (no
        profile, or a MEDIUM hint, or no router configured -> no model
        override).

        This is the single source of truth for that hint so every caller that
        needs to reconstruct a live model-routing decision (the real atomic
        dispatch AND the terminal-strength check that must match it,
        ``_root_ac_terminal_state``) computes it identically instead of each
        maintaining its own copy that can silently drift apart.
        """
        if (
            self._execution_profile is not None
            and self._execution_profile.suggested_model_tier is not SuggestedModelTier.MEDIUM
        ):
            return tier_from_profile_hint(self._execution_profile.suggested_model_tier.value)
        return None

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
        force_frontier_routing: bool = False,
    ) -> ACExecutionResult:
        """Execute an atomic AC directly via Claude Agent.

        ``force_frontier_routing`` (Round-5 Finding #2, BLOCKING): ``True``
        only for lateral-escalation-ladder-owned redispatches. The ladder's
        eligibility check (``_root_ac_terminal_state`` with
        ``same_runtime_retry_available=False``) treats every ACTIVELY
        configured routing axis as already at its ceiling — but normal
        incremental routing raises effort exactly ONE notch above base
        (``EFFORT_RAISE_RETRY_THRESHOLD``), so a low/medium-base AC could
        cycle the whole persona ladder without ever actually dispatching at
        max effort. When set, an ACTIVE effort axis dispatches at
        ``DEFAULT_EFFORT_CEILING`` (investment cheapening suspended — the
        ladder's premise is maximum strength) and an ACTIVE model axis is
        anchored at ``DEFAULT_TIER_CEILING``; a dormant axis (no base
        effort / no router) stays dormant, preserving the
        no-escalation-dial-configured opt-out.

        Returns:
            ACExecutionResult for this AC.
        """
        ac_session_id: str | None = None
        semantic_ac_key = semantic_ac_key or derive_semantic_ac_key(ac_spec or ac_content)

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
        # Round-5 Finding #2 (BLOCKING): a ladder-owned redispatch runs an
        # ACTIVE effort axis at the true ceiling — the incremental one-notch
        # retry raise below can never reach it from a low/medium base — and
        # suspends investment cheapening (the ladder's premise is maximum
        # strength). A dormant axis (``base_effort=None``) stays dormant.
        effort_decision, execute_effort_kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=(
                DEFAULT_EFFORT_CEILING
                if force_frontier_routing and self._reasoning_effort
                else self._reasoning_effort
            ),
            is_decomposed_child=is_sub_ac,
            retry_attempt=retry_attempt,
            investment_assessment=(None if force_frontier_routing else investment_assessment),
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
        # Round-5 Finding #2 (BLOCKING): a ladder-owned redispatch anchors an
        # ACTIVE model axis at the frontier ceiling (escalation notches can only
        # raise, never lower, so this holds); a dormant router stays dormant.
        suggested_tier = (
            DEFAULT_TIER_CEILING if force_frontier_routing else self._suggested_model_tier_hint()
        )
        model_decision, execute_model_kwargs = resolve_execute_model(
            self._adapter,
            router=self._model_router,
            is_decomposed_child=is_sub_ac,
            decomposition_trustworthy=decomposition_trustworthy,
            retry_attempt=retry_attempt,
            suggested_tier=suggested_tier,
        )
        initial_model_decision, _initial_model_kwargs = resolve_execute_model(
            self._adapter,
            router=self._model_router,
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
                    self._model_router.base_tier if self._model_router is not None else None
                ),
                escalation_retry_threshold=(
                    self._model_router.escalation_retry_threshold
                    if self._model_router is not None
                    else None
                ),
                model_escalated=model_escalated,
            )
        # Merge the model override into the effort kwargs. The merged dict flows
        # through LeafDispatcher.stream → execute_task unchanged (LeafDispatcher
        # itself is untouched); ``model`` is present ONLY for runtimes that enforce
        # a per-call override, so an advised runtime is never handed one.
        execute_effort_kwargs = {**execute_effort_kwargs, **execute_model_kwargs}

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
        dispatch_state = LeafDispatchState(messages=messages, runtime_handle=runtime_handle)
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
                semantic_ac_key=semantic_ac_key,
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

                    message_count_before_signal = dispatch_state.message_count
                    primary_final_message = dispatch_state.final_message
                    primary_success = dispatch_state.success
                    # Fix 4 (round 3, BLOCKING): an INFORM-mode signal turn's
                    # outcome is always discarded in favor of the PRIMARY
                    # dispatch's own result below -- its infra_fatal verdict
                    # must be discarded and restored right alongside
                    # success/final_message, or a merely infra-fatal-looking
                    # error on this secondary side-channel turn could leak
                    # into the primary (unrelated) result.
                    primary_infra_fatal = dispatch_state.infra_fatal
                    await self._event_store.append(
                        create_session_signal_delivery_started_event(
                            queued_signal.signal,
                            effective_mode=queued_signal.effective_mode,
                            runtime_backend=signal_target.runtime_backend,
                            orchestrator_session_id=session_id,
                        )
                    )
                    inform_mode = queued_signal.effective_mode is SessionSignalMode.INFORM
                    try:
                        await LeafDispatcher(self).stream(
                            state=dispatch_state,
                            prompt=(
                                render_inform_signal_prompt(queued_signal.signal)
                                if inform_mode
                                else render_after_turn_signal_prompt(queued_signal.signal)
                            ),
                            tools=[] if inform_mode else tools,
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
                            semantic_ac_key=semantic_ac_key,
                            label=label,
                            indent=indent,
                            execution_counters=execution_counters,
                        )
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
                        if inform_mode:
                            dispatch_state.success = primary_success
                            dispatch_state.final_message = primary_final_message
                            dispatch_state.infra_fatal = primary_infra_fatal
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
                        if inform_mode:
                            dispatch_state.success = primary_success
                            dispatch_state.final_message = primary_final_message
                            dispatch_state.infra_fatal = primary_infra_fatal
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
                        dispatch_state.success = primary_success
                        dispatch_state.final_message = primary_final_message
                        dispatch_state.infra_fatal = primary_infra_fatal

                self._session_signal_hub.unregister(signal_target)
                signal_target_registered = False

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

            # A contract-carrying AC (declares verify_command or expected
            # artifacts) delegates commands_run and tests_passed to the
            # orchestrator's authoritative _run_ac_verify_gate. When it declares
            # expected_artifacts, files_touched is delegated to the same
            # filesystem oracle so artifact work does not require fabricated
            # transcript-shaped evidence.
            has_success_contract = isinstance(ac_spec, AcceptanceCriterionSpec) and bool(
                ac_spec.verify_command or ac_spec.expected_artifacts
            )
            has_expected_artifacts = isinstance(ac_spec, AcceptanceCriterionSpec) and bool(
                ac_spec.expected_artifacts
            )
            # Delegating commands_run/tests_passed/files_touched to
            # _run_ac_verify_gate is only valid when that gate actually runs.
            # _apply_verify_gate returns early when run_verify_commands is disabled,
            # so with the gate off we must retain the transcript-backed evidence
            # rather than drop it.
            #
            # Fix 1 (round 3): a decomposition child (``is_sub_ac``) is only ever
            # handed the PARENT's contract (see the ``ac_spec=ac_spec`` forward in
            # ``_execute_decomposition_children``). Running the parent's own
            # (possibly non-idempotent) verify_command once per successful child,
            # IN ADDITION to the single authoritative re-run over the union of all
            # children in ``_attest_decomposition_round``, is both a cost bug (N+1
            # executions of the same command) and a correctness bug: a child
            # passing the PARENT's whole contract in isolation is not evidence
            # that child did its own job correctly, so treating that result as
            # "this sibling's own verify gate" (as ``_attest_decomposition_round``
            # would, via ``result.verify_gate_outcome``) is a false trust signal.
            # Children therefore never execute this gate; ``verify_gate_outcome``
            # stays ``None`` for them. A child's OWN evidence instead comes from
            # the per-child artifact-slice oracle in
            # ``_execute_decomposition_children`` (its assigned slice of the
            # parent's seed-authored expected_artifacts, checked with a
            # pre-dispatch existence snapshot); with no slice assigned, the
            # sibling axis of the gate-anchored attestation resolves to
            # INDETERMINATE (fail closed -- see ``decomposition_attestation.py``)
            # instead of borrowing the parent-wide result as if it were
            # child-local proof. The parent's own gate is still re-run exactly
            # once, after all children finish, by ``_attest_decomposition_round``.
            verify_gate_active = self._run_verify_commands and not is_sub_ac
            verify_gate_outcome: _VerifyGateOutcome | None = None
            if success and verify_gate_active and has_success_contract:
                cwd = self._task_cwd or self._adapter.working_directory or os.getcwd()
                verify_gate_outcome = await self._run_ac_verify_gate(spec=ac_spec, cwd=cwd)

            typed_evidence, typed_validation, typed_error = self._observe_atomic_typed_evidence(
                ac_content=ac_content,
                final_message=final_message,
                success=success,
                has_success_contract=has_success_contract,
                has_expected_artifacts=has_expected_artifacts,
                verify_gate_active=verify_gate_active,
            )
            verifier_verdict = self._run_atomic_verifier_pass(
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
            fat_harness_error = self._fat_harness_acceptance_error(
                runtime_success=success,
                typed_evidence=typed_evidence,
                typed_validation=typed_validation,
                typed_error=typed_error,
                verifier_verdict=verifier_verdict,
                verify_gate_outcome=verify_gate_outcome,
                verify_gate_replaces_all_evidence=verify_gate_replaces_all_evidence,
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
                # Fix 4 (round 3, BLOCKING): a structured error RESULT (never
                # raised) can still be a genuinely infra-fatal condition --
                # e.g. a missing CLI binary or a bad auth credential reported
                # as an ordinary final error message. ``dispatch_state``'s
                # classifier (LeafDispatcher.stream) already judged this
                # dispatch's own final message; thread that verdict through so
                # ``_is_retryable_failure`` treats it identically to the
                # raised-exception path below, instead of defaulting to
                # ``False`` and entering the infinite retry/parking loop.
                infra_fatal=dispatch_state.infra_fatal,
            )

        except Exception as e:
            # Anything reaching this handler escaped the runtime dispatch
            # (``LeafDispatcher.stream`` / ``self._adapter.execute_task``) or
            # the bookkeeping around it WITHOUT going through the normal
            # message-stream contract (``message.is_error`` -> a structured,
            # ordinary AC-level failure). That makes it infra-fatal by
            # construction — an adapter crash, an auth failure (401/403), a
            # network partition, or some other environmental fault, never an
            # AC's own verify-gate/quality failure. It is still wrapped as a
            # normal-looking ``ACExecutionResult`` (not re-raised) purely so
            # existing logging/event-emission/teardown code keeps working —
            # ``infra_fatal=True`` is the one bit downstream recovery logic
            # (``_is_retryable_failure``) needs to keep it OUT of the
            # ordinary retry loop and the lateral-escalation ladder, exactly
            # as it would if this had surfaced as a raw, uncaught exception.
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
                infra_fatal=True,
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
                        await self._safe_emit_event(
                            create_session_signal_rejected_event(
                                pending_signal.signal,
                                rejection_code="target_ended_before_boundary",
                                detail=(
                                    "The runtime attempt ended before the queued signal "
                                    "reached its delivery boundary."
                                ),
                                effective_mode=pending_signal.effective_mode,
                                runtime_backend=signal_target.runtime_backend,
                            )
                        )
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
    ) -> None:
        """Harvest and emit this leaf's runtime token spend (frugality-proof AC2).

        Emits nothing when the stream carried no runtime usage telemetry — the
        proof treats missing as missing rather than fabricating a spend. Observe-only:
        any failure degrades to a warning so token attribution never disrupts the
        leaf's teardown or result.
        """
        try:
            harvested = _harvest_token_spend(messages)
            if harvested is None:
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
                model_tier=getattr(model_decision, "tier", None),
                model_mode=getattr(model_decision, "mode", None),
                effort_level=getattr(effort_decision, "level", None),
                runtime_backend=getattr(self._adapter, "runtime_backend", None),
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
        subprocess_kwargs: dict[str, Any] = {}
        if os.name != "nt":
            subprocess_kwargs["start_new_session"] = True
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                **subprocess_kwargs,
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
            if os.name != "nt":
                import signal

                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            else:
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
        failed are recovered only when the same contract passes independently,
        so contract-less behavior — and the single fat-harness failure event
        for an already-failed AC without a passing contract — is preserved
        (no double-fail for one root cause).
        """
        if not self._run_verify_commands:
            return result
        if ac_index < 0 or ac_index >= len(seed.acceptance_criteria):
            return result
        spec = seed.acceptance_criteria[ac_index]
        if not isinstance(spec, AcceptanceCriterionSpec) or not (
            spec.verify_command or spec.expected_artifacts
        ):
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
            outcome = await self._run_ac_verify_gate(spec=spec, cwd=cwd)
        if outcome.passed:
            if not result.success and not result.is_blocked and not result.is_invalid:
                from ouroboros.events.base import BaseEvent

                recovery_message = (
                    "Runtime reported failure, but the AC success contract passed: "
                    "expected_artifacts/verify_command satisfied."
                )
                await self._safe_emit_event(
                    BaseEvent(
                        type="execution.verify.recovered",
                        aggregate_type="execution",
                        aggregate_id=execution_id or session_id,
                        data={
                            "session_id": session_id,
                            "execution_id": execution_id,
                            "ac_index": ac_index,
                            "ac_content": ac_text(spec),
                            "verify_command": spec.verify_command,
                            "expected_artifacts": list(spec.expected_artifacts),
                            "prior_error": result.error,
                            "output_tail": outcome.output_tail,
                        },
                    )
                )
                log.info(
                    "parallel_executor.ac.verify_gate_recovered",
                    session_id=session_id,
                    ac_index=ac_index,
                    prior_error=result.error,
                )
                return replace(
                    result,
                    success=True,
                    error=None,
                    final_message=(result.final_message or recovery_message),
                    outcome=ACExecutionOutcome.SUCCEEDED,
                    verify_gate_outcome=outcome,
                )
            return result
        if not result.success:
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
            verify_gate_outcome=outcome,
        )

    async def _emit_ac_outcome_finalized(
        self,
        *,
        result: ACExecutionResult,
        root_ac_index: int,
        session_id: str,
        execution_id: str,
    ) -> None:
        """Persist the outer verify/retry layer's authoritative AC outcome.

        Leaf-level deliver and shadow events are provisional because they are
        emitted before the seed-level success contract runs.  The deterministic
        frugality proof requires this marker and admits only roots whose latest
        retry was finally accepted.  Event persistence remains observe-only: if
        the marker is dropped, the proof fails closed by excluding the rows.
        """
        from ouroboros.events.base import BaseEvent

        await self._safe_emit_event(
            BaseEvent(
                type="execution.ac.outcome_finalized",
                aggregate_type="execution",
                aggregate_id=execution_id or session_id,
                data={
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "root_ac_index": root_ac_index,
                    "ac_index": root_ac_index,
                    "retry_attempt": result.retry_attempt,
                    "success": result.success,
                    "outcome": result.outcome.value if result.outcome is not None else None,
                    "is_decomposed": result.is_decomposed,
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

        if result.success or result.outcome is not ACExecutionOutcome.FAILED:
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

        A FAILED AC whose spec carries a success contract (``verify_command``
        OR non-empty ``expected_artifacts``) may only be flipped to satisfied by
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
            if not isinstance(spec, AcceptanceCriterionSpec) or not (
                spec.verify_command or spec.expected_artifacts
            ):
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
        """Whether a batch result is a non-stall, non-blocked AC failure (PR-V V3).

        A raw ``BaseException`` (an uncaught exception that escaped even the
        atomic leaf's own exception handling) is never retryable. Neither is
        an ``ACExecutionResult`` with ``infra_fatal=True`` — a genuinely
        infra-fatal exception (adapter crash, auth failure, network
        partition) that the atomic leaf caught and wrapped as a structured
        result purely for logging/observability. Both shapes must be treated
        identically here: whether an infra-fatal failure arrives as a raw
        exception or as this wrapped form is an implementation detail of
        WHERE it was caught, not a difference in what recovery is
        appropriate. Neither may re-enter the ordinary retry loop or the
        lateral-escalation ladder — the runtime itself failed, not the AC's
        work, so redispatching cannot help.
        """
        if not isinstance(result, ACExecutionResult):
            return False
        if result.infra_fatal:
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
            redacted_error = redact_and_truncate_text(
                last_error,
                max_chars=max(500, len(last_error) * 2),
            )
            parts.append("### Last error (tail)\n" + redacted_error[-500:])
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

    async def _root_ac_terminal_state(
        self,
        *,
        seed: Seed,
        ac_idx: int,
        result: ACExecutionResult,
        retry_attempt: int,
        same_runtime_retry_available: bool = True,
    ) -> bool:
        """Whether ``result`` is a failure at this root AC's maximum strength.

        Recomputes the model tier / reasoning effort the NEXT dispatch of
        this root AC would actually run at by calling the EXACT SAME
        resolution entry points a live dispatch uses —
        :func:`~ouroboros.orchestrator.model_routing.resolve_execute_model`
        and
        :func:`~ouroboros.orchestrator.effort_routing.resolve_execute_effort`
        — with the SAME inputs a live dispatch would pass them, never a
        parallel/incomplete reconstruction. Calling the lower-level
        ``decide_model``/``decide_effort`` directly (as an earlier revision
        did) skips everything those wrappers own on top: the runtime's
        actual enforceable-effort vocabulary, the cross-harness
        backend-mismatch guard (a router built for a different backend than
        the currently-swapped-in adapter is treated as absent), and the
        profile's suggested-tier hint — and it silently dropped the AC's own
        investment assessment entirely, which is how a low/low measured
        assessment's real "high effort" dispatch could get reported as a
        terminal "xhigh" here: one notch of the investment-authorized
        cheapening the real dispatch applies, then never subtracted back out.

        Args:
            same_runtime_retry_available: Whether a FUTURE budget-funded
                same-runtime retry of this root AC could still happen under
                this run's configuration. Defaults to ``True`` (some
                same-runtime retry budget remains, so it is meaningful to ask
                "would the NEXT retry already be at ceiling"). ``False``
                means the ordinary same-runtime retry budget is EXHAUSTED
                (which includes the ``self._ac_retry_attempts <= 0`` case the
                Fix 6 redo originally special-cased, and — Round-4 Finding
                #2 — every check made from inside the lateral-escalation
                ladder, whose redispatches are ladder-owned rather than
                budget-funded): there is no future budget-funded dispatch
                that could climb this AC to a higher tier/effort, so the raw
                tier/effort the dispatches happened to reach must not gate
                ladder eligibility — effort escalation raises exactly one
                notch total, so a low/medium base can never literally reach
                the "xhigh" ceiling regardless of retries. Each
                ACTIVELY-configured routing axis is instead treated as
                already at this run's ceiling for it (a dormant axis,
                resolved as ``None``, stays dormant) — eligibility is
                decided by ``success``/``is_decomposed`` alone for those
                axes.
        """
        ac_criterion = seed.acceptance_criteria[ac_idx]
        investment_spec = (
            ac_criterion.investment if isinstance(ac_criterion, AcceptanceCriterionSpec) else None
        )
        investment_assessment = assess_investment(investment_spec)
        model_decision, _model_kwargs = resolve_execute_model(
            self._adapter,
            router=self._model_router,
            is_decomposed_child=False,
            retry_attempt=retry_attempt,
            suggested_tier=self._suggested_model_tier_hint(),
        )
        effort_decision, _effort_kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=self._reasoning_effort,
            is_decomposed_child=False,
            retry_attempt=retry_attempt,
            investment_assessment=investment_assessment,
        )
        model_tier = model_decision.tier
        effort_level = effort_decision.level
        if not same_runtime_retry_available:
            if model_tier is not None:
                model_tier = DEFAULT_TIER_CEILING
            if effort_level is not None:
                effort_level = DEFAULT_EFFORT_CEILING
        return is_terminal_state_failure(
            success=result.success,
            is_decomposed=result.is_decomposed,
            model_tier=model_tier,
            effort_level=effort_level,
        )

    async def _load_decomposition_attestation(
        self, node_id: str, *, execution_id: str, session_id: str
    ) -> DecompositionAttestation | None:
        """Load the LATEST gate-anchored attestation for this node id, durable
        across restarts (Fix 2, round 2, BLOCKING).

        ``self._decomposition_attestations`` is an in-memory cache. A process
        restart/resume recreates the executor with it EMPTY, so a fresh
        executor forgets a prior round's UNTRUSTWORTHY verdict for this root
        AC and silently re-authorizes the cheap child-tier discount it
        should still be withholding -- exactly the poisoning this cache
        exists to enforce, just no longer surviving a restart.

        Mirrors the existing convention this codebase already uses for the
        same shape of problem (see ``_load_lateral_escalation_state``): a
        cache hit returns immediately; a miss replays this execution's own
        durable events and rebuilds the verdict from the LATEST
        ``execution.ac.decomposition_attested`` event for this node id.
        ``node_id`` is stable across same-root retries (it does not encode
        retry_attempt), so the newest matching event in the durable log is
        authoritative -- identical "last write wins" semantics to the
        in-memory dict it replaces. No matching event means no attestation
        has ever been recorded for this node id (``None``, exactly like
        today's in-memory default); that is NOT cached, since a decomposition
        round for this node id may not have run yet in THIS process and a
        later call after it finishes must not keep returning a stale
        ``None``.
        """
        if node_id in self._decomposition_attestations:
            return self._decomposition_attestations[node_id]

        def _fail_closed_sentinel(reason: str) -> DecompositionAttestation:
            # Shared synthetic fail-closed verdict for BOTH "replay failed
            # entirely" and "replay succeeded but the found payload is
            # malformed". Not cached: a LATER successful read in this same
            # process must not stay poisoned by a transient failure.
            return DecompositionAttestation(
                node_id=node_id,
                verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
                failed_axis=None,
                failed_sibling_id=None,
                reason=reason,
            )

        aggregate_id = execution_id or session_id
        events = await self._replay_with_retry("execution", aggregate_id)
        if events is None:
            # Fix 5 (round 3, BLOCKING): a genuine READ failure (every retry
            # exhausted) must NOT be silently treated the same as "no prior
            # attestation was ever recorded" -- that is fail-OPEN and would
            # let a restart re-authorize a cheap child tier this exact node
            # id was already proven untrustworthy for. Fail closed with a
            # synthetic UNTRUSTWORTHY verdict instead of falling through to
            # the legitimate-miss ``None`` path below.
            log.warning(
                "parallel_executor.decomposition_attestation.state_reconstruction_failed",
                node_id=node_id,
                execution_id=execution_id,
            )
            return _fail_closed_sentinel(
                "durable event replay failed after retries; failing closed "
                "(untrustworthy) rather than assuming no prior attestation exists"
            )

        latest_data: dict[str, Any] | None = None
        for event in events:
            if getattr(event, "type", None) != "execution.ac.decomposition_attested":
                continue
            data = getattr(event, "data", None)
            if not isinstance(data, dict) or data.get("node_id") != node_id:
                continue
            latest_data = data

        if latest_data is None:
            # ONLY the genuine "no matching event exists at all" case may
            # return ``None`` -- the caller treats ``None`` as "never
            # attested; proposal trust applies".
            return None

        attestation = _decomposition_attestation_from_event_data(latest_data)
        if attestation is None:
            # Round-4 Finding #4 (BLOCKING): a matching event WAS found but
            # its payload does not round-trip into a valid attestation.
            # Returning ``None`` here is indistinguishable from "never
            # attested" at the caller (``prior_attestation is None`` admits
            # the child-tier discount) -- so a corrupt record of a prior
            # UNTRUSTWORTHY verdict would silently un-poison this node id.
            # Fail closed with the SAME synthetic UNTRUSTWORTHY sentinel the
            # total-replay-failure case above uses.
            log.warning(
                "parallel_executor.decomposition_attestation.malformed_event_data",
                node_id=node_id,
                execution_id=execution_id,
            )
            return _fail_closed_sentinel(
                "durable attestation event found for this node id but its payload "
                "is malformed; failing closed (untrustworthy) rather than treating "
                "corruption as 'never attested'"
            )
        self._decomposition_attestations[node_id] = attestation
        return attestation

    async def _load_lateral_escalation_state(
        self, ac_idx: int, *, execution_id: str
    ) -> LateralEscalationState:
        """Load this root AC's persona-escalation streak, durable across restarts.

        ``self._lateral_escalation_states`` is an in-memory cache. A process
        restart/resume recreates the executor with it EMPTY, which silently
        dropped a parked AC's escalation history (restarting the persona
        cycle from scratch on the very next terminal failure) and its
        long-backoff parked cadence — despite the design intending durable
        parking.

        Mirrors the existing convention this codebase already uses for the
        SAME shape of problem — an in-memory cache reconstructed from the
        event store on a miss (see
        ``ACRuntimeHandleManager._load_persisted_ac_runtime_handle``): a
        cache hit returns immediately; a miss replays this execution's own
        durable events and rebuilds the state from the LATEST of three event
        types for this AC's node id, in log order:

        * ``execution.ac.lateral_escalation_progressed`` (Fix 5, round 2) —
          emitted every loop iteration, so an in-flight persona
          attempt/streak advancement that never reached parking is still
          reconstructed instead of lost.
        * ``execution.ac.parked_for_operator`` — the full-parking transition;
          always implies ``parked=True``.
        * ``execution.ac.parked_resolved`` (Fix 8) — resets reconstruction to
          a fresh state, so a parked-then-succeeded AC correctly starts a
          brand new cycle rather than staying "still parked" forever.

        No matching event means a fresh, never-escalated state — identical
        to today's in-memory default.

        The rebuilt (or freshly-defaulted) state is cached back onto
        ``self._lateral_escalation_states`` so later calls in the SAME
        process do not repeat the replay.

        SCOPE OF THE DURABILITY GUARANTEE: this reconstruction only runs for
        executions dispatched through ``ParallelACExecutor`` — i.e. fresh
        runs and crash-restarts recovered via the RC3 checkpoint block in
        ``execute_parallel`` (which restores the original ``execution_id``
        so this replay targets the right aggregate). The OTHER resume path,
        ``OrchestratorRunner.resume_session`` (CLI ``--resume-session`` /
        MCP ``is_resume``), drives a single ``adapter.execute_task`` stream
        and bypasses this executor entirely, so an AC parked or mid-ladder
        before such a resume restarts its escalation from scratch. See the
        limitation note on ``resume_session`` in ``runner.py``.
        """
        cached = self._lateral_escalation_states.get(ac_idx)
        if cached is not None:
            return cached

        from ouroboros.resilience.lateral import ThinkingPersona

        node_id = ExecutionNodeIdentity.root(
            execution_context_id=execution_id, ac_index=ac_idx
        ).node_id
        events = await self._replay_with_retry("execution", execution_id)
        if events is None:
            # Fix 5 (round 3, BLOCKING): a genuine READ failure must not be
            # silently treated as "this AC was never escalated" -- that is
            # fail-OPEN and would let a restart repeat already-tried personas
            # or forget a parked AC's long-backoff cadence. Fail closed by
            # assuming this AC is ALREADY PARKED: ``advance_lateral_escalation``
            # treats ``parked=True`` as authoritative on its own (it applies
            # the long-backoff cadence and never selects a new persona
            # regardless of ``personas_tried`` contents), so this single flag
            # is sufficient to prevent both hazards without fabricating a
            # persona history we cannot actually reconstruct. Not cached: a
            # LATER successful read in this same process must not stay
            # poisoned by a transient failure.
            log.warning(
                "parallel_executor.lateral_escalation.state_reconstruction_failed",
                ac_idx=ac_idx,
                execution_id=execution_id,
            )
            return LateralEscalationState(parked=True)

        latest_state_data: dict[str, Any] | None = None
        latest_state_type: str | None = None
        for event in events:
            event_type = getattr(event, "type", None)
            if event_type not in {
                "execution.ac.parked_for_operator",
                "execution.ac.lateral_escalation_progressed",
                "execution.ac.parked_resolved",
            }:
                continue
            data = getattr(event, "data", None)
            if not isinstance(data, dict) or data.get("node_id") != node_id:
                continue
            if event_type == "execution.ac.parked_resolved":
                latest_state_data = None
                latest_state_type = None
            else:
                latest_state_data = data
                latest_state_type = event_type

        state = LateralEscalationState()
        if latest_state_data is not None:
            # Round-4 Finding #3 (BLOCKING): every field below is ALWAYS
            # written by its emitter (``emit_lateral_escalation_progressed``
            # / ``emit_ac_parked_for_operator``), so a missing or wrong-typed
            # field means the persisted payload is CORRUPT -- not merely
            # sparse. The previous parse silently "cleaned up" corruption
            # (unknown persona discarded, invalid streak -> 0, invalid
            # parked -> False), reconstructing a fresh non-parked state from
            # a record that may have said PARKED -- fail-OPEN. Treat "found
            # the event but couldn't validate its contents" exactly like a
            # total replay failure above: a synthetic PARKED=True sentinel,
            # not cached, so a later clean read is not poisoned.
            def _malformed(reason: str) -> LateralEscalationState:
                log.warning(
                    "parallel_executor.lateral_escalation.malformed_event_data",
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    event_type=latest_state_type,
                    reason=reason,
                )
                return LateralEscalationState(parked=True)

            raw_personas = latest_state_data.get("personas_tried")
            if not isinstance(raw_personas, list):
                return _malformed("personas_tried is not a list")
            recovered: list[ThinkingPersona] = []
            for raw_value in raw_personas:
                try:
                    recovered.append(ThinkingPersona(raw_value))
                except ValueError:
                    return _malformed(f"unrecognized persona value: {raw_value!r}")
            personas = tuple(recovered)
            raw_streak = latest_state_data.get("consecutive_terminal_failures")
            if not isinstance(raw_streak, int) or isinstance(raw_streak, bool) or raw_streak < 0:
                return _malformed(
                    f"consecutive_terminal_failures is not a non-negative int: {raw_streak!r}"
                )
            streak = raw_streak
            # ``parked_for_operator``'s payload predates Fix 5 and has no
            # ``parked`` field of its own -- it is ALWAYS emitted exactly on
            # the parking transition, so winning with that event type means
            # parked=True unconditionally. The newer ``progressed`` event
            # carries the field explicitly (it fires every iteration, parked
            # or not) -- and MUST carry it as a real bool, else corrupt.
            if latest_state_type == "execution.ac.parked_for_operator":
                parked_flag = True
            else:
                raw_parked = latest_state_data.get("parked")
                if not isinstance(raw_parked, bool):
                    return _malformed(f"parked is not a bool: {raw_parked!r}")
                parked_flag = raw_parked
            state = LateralEscalationState(
                consecutive_terminal_failures=streak,
                personas_tried=personas,
                parked=parked_flag,
            )

        self._lateral_escalation_states[ac_idx] = state
        return state

    async def _resolve_escalation_success(
        self, *, ac_idx: int, execution_id: str, session_id: str
    ) -> None:
        """Durably close this root AC's escalation episode after a success.

        Shared by the ladder's own breakthrough exit and the resumed-ladder
        re-entry path (Round-5 Finding #1): whichever path an AC with durable
        escalation history succeeds on, the ``parked_resolved`` companion
        event must fire so replay-based projections and later resumes
        converge to "resolved" instead of a stale parked/escalating record.

        Round-5 Finding #4 (BLOCKING): a durably-missing resolution leaves
        the latest replayable event for this node id still
        ``parked_for_operator``/``progressed``. Unlike the ladder's
        pre-redispatch writes, a genuine SUCCESS cannot be held hostage
        forever (reverting a completed AC would surface failure where
        escalation actually broke through — the exact false-negative
        direction this PR forbids), so this write gets bounded EXTRA retry
        rounds on top of ``_safe_emit_event``'s own internal retries, then a
        loud correctness-specific error if truly exhausted.
        """
        resolved_node_id = ExecutionNodeIdentity.root(
            execution_context_id=execution_id, ac_index=ac_idx
        ).node_id
        resolved_persisted = False
        for extra_attempt in range(3):
            if extra_attempt:
                await self._sleep(min(2.0 * (2**extra_attempt), 10.0))
            resolved_persisted = await self._event_emitter.emit_ac_parked_resolved(
                execution_id=execution_id,
                session_id=session_id,
                node_id=resolved_node_id,
                root_ac_index=ac_idx,
            )
            if resolved_persisted:
                break
        if not resolved_persisted:
            log.error(
                "parallel_executor.lateral_escalation.resolved_write_failed_correctness_risk",
                ac_idx=ac_idx,
                execution_id=execution_id,
                node_id=resolved_node_id,
            )
        self._lateral_escalation_states.pop(ac_idx, None)

    async def _resume_escalated_ac(
        self,
        *,
        seed: Seed,
        ac_idx: int,
        restored_state: LateralEscalationState,
        ac_retry_attempts: dict[int, int],
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        execution_counters: dict[str, int] | None,
    ) -> ACExecutionResult:
        """Re-enter a mid-ladder/parked AC at its restored phase after a resume.

        Round-5 Finding #1 (BLOCKING): checkpoints only save after a level
        completes, so a crash mid-ladder restarts the whole level — and the
        ordinary batch path used to run this AC through a FRESH un-backed-off
        same-runtime retry budget before any escalation state was loaded
        (loading only happened inside the ladder, which is only reached after
        that budget is spent). Worse, a fresh attempt that happened to
        succeed bypassed the ``parked_resolved`` transition entirely, leaving
        durable/projected state stuck on parked/escalating for a completed
        AC. This helper is the correct re-entry: the resumed attempt runs at
        the ladder's post-budget phase (persona-framed prompt for an
        in-flight persona, parked cadence honored via a pre-dispatch long
        backoff, frontier/max-effort routing per the ladder's own contract),
        a SUCCESS fires the same resolution event the non-crash path fires,
        and a failure hands straight back to the ladder — which resumes at
        the restored streak/persona/parked phase, never a fresh
        top-of-ladder state.
        """
        ac_content = ac_text(seed.acceptance_criteria[ac_idx])
        synthetic_prior = ACExecutionResult(
            ac_index=ac_idx,
            ac_content=ac_content,
            success=False,
            error=(
                "Execution restarted while this AC was mid-escalation; resuming "
                "the lateral-escalation ladder at its durably recorded phase."
            ),
            retry_attempt=ac_retry_attempts[ac_idx],
        )
        if restored_state.parked:
            # Honor the parked cadence that governs retry pacing: the parked
            # loop always sleeps the long backoff BEFORE a redispatch.
            await self._sleep(self._parked_retry_backoff_seconds)
            retry_prompt = self._build_ac_retry_prompt(
                result=synthetic_prior, ac_content=ac_content, is_final_attempt=True
            )
        elif restored_state.personas_tried:
            # The ``progressed`` event is emitted BEFORE its redispatch, so
            # the last recorded persona is the one whose attempt the crash
            # interrupted — re-run THAT persona's attempt, don't skip it.
            in_flight_persona = restored_state.personas_tried[-1]
            retry_prompt = build_persona_retry_prompt(
                persona=in_flight_persona,
                ac_content=ac_content,
                current_approach=(
                    self._build_ac_retry_prompt(
                        result=synthetic_prior, ac_content=ac_content, is_final_attempt=False
                    )
                    or "The previous attempts failed as described above."
                ),
                failed_attempts=(),
            )
        else:
            retry_prompt = self._build_ac_retry_prompt(
                result=synthetic_prior, ac_content=ac_content, is_final_attempt=False
            )

        retry_results = await self._execute_ac_batch(
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
            retry_prompts={ac_idx: retry_prompt},
            same_runtime_budget_exhausted=True,
            # The ladder's own redispatch contract (Round-5 Finding #2): a
            # resumed mid-ladder attempt runs at maximum strength.
            force_frontier_routing=True,
        )
        candidate = retry_results[0]
        if not isinstance(candidate, ACExecutionResult):
            # Mirrors the ladder's own handling of a raw escaped exception:
            # genuinely infra-fatal by construction.
            candidate = ACExecutionResult(
                ac_index=ac_idx,
                ac_content=ac_content,
                success=False,
                error=str(candidate),
                retry_attempt=ac_retry_attempts[ac_idx],
                infra_fatal=True,
            )
        candidate = await self._apply_verify_gate(
            seed=seed,
            ac_index=ac_idx,
            result=candidate,
            session_id=session_id,
            execution_id=execution_id,
        )
        await self._emit_ac_outcome_finalized(
            result=candidate,
            root_ac_index=ac_idx,
            session_id=session_id,
            execution_id=execution_id,
        )
        if candidate.success:
            # The resumed attempt succeeded without re-entering the ladder
            # loop: fire the SAME resolution transition the non-crash path
            # fires, so durable/projected state converges to resolved.
            await self._resolve_escalation_success(
                ac_idx=ac_idx, execution_id=execution_id, session_id=session_id
            )
            return candidate
        # Still failing: hand straight to the ladder, which resumes at the
        # restored (cached) streak/persona/parked phase.
        return await self._finalize_batch_ac_with_escalation(
            seed=seed,
            ac_idx=ac_idx,
            result=candidate,
            ac_retry_attempts=ac_retry_attempts,
            session_id=session_id,
            execution_id=execution_id,
            tools=tools,
            tool_catalog=tool_catalog,
            system_prompt=system_prompt,
            level_contexts=level_contexts,
            execution_counters=execution_counters,
            retry_termination_reason=(
                "budget_exhausted" if self._is_retryable_failure(candidate) else "not_retryable"
            ),
        )

    async def _maybe_run_lateral_escalation_ladder(
        self,
        *,
        seed: Seed,
        ac_idx: int,
        result: ACExecutionResult,
        ac_retry_attempts: dict[int, int],
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        execution_counters: dict[str, int] | None,
    ) -> ACExecutionResult | None:
        """Task 2: never let a root AC give up at maximum strength.

        Called once this AC's ORDINARY retry budget is exhausted. A genuinely
        infra-fatal failure (adapter crash, auth failure, an uncaught
        exception — anything ``_is_retryable_failure`` does not consider a
        structured, retryable verify-gate/quality failure) is exempt and
        returns ``None`` immediately so it surfaces through whatever existing
        mechanism already handles that distinction. A run with NO escalation
        axis actively configured (model routing and effort routing both
        dormant) also returns ``None`` — there is no "maximum strength" to
        have exhausted, so the unchanged give-up-after-N-retries behavior
        applies. Round-4 Finding #2 (BLOCKING): exhausting the same-runtime
        retry budget is otherwise SUFFICIENT to engage this ladder,
        regardless of what raw tier/effort the budget-funded dispatches
        happened to reach — effort escalation raises exactly one notch
        total, so a low/medium base can never literally reach the "xhigh"
        ceiling, and gating entry on the ceiling being reached let a small
        retry budget starve the ladder out entirely (a FAILED status with
        every persona untried — the exact outcome this feature forbids).

        Once engaged, this method OWNS ``ac_idx`` until it produces a
        successful result: it keeps retrying identically until
        :data:`_LATERAL_ESCALATION_THRESHOLD` consecutive terminal-state
        failures accrue, then cycles a NEW lateral-thinking persona into the
        retry prompt each attempt (never repeating one already tried), then —
        once all personas are exhausted — emits
        ``execution.ac.parked_for_operator`` and keeps retrying forever at
        ``self._parked_retry_backoff_seconds`` cadence. This AC never
        surfaces a final FAILED status. The caller's batch therefore blocks
        on this AC until it succeeds or the run is cancelled — the
        deliberate, disclosed cost of "never silently give up".

        Opt-in via ``self._lateral_escalation_enabled`` (default OFF, like
        ``shadow_replay_enabled``): direct/test construction of the executor
        must not get this significant behavior change for free.
        """
        if not self._lateral_escalation_enabled:
            return None
        if not self._is_retryable_failure(result):
            return None

        current_result = result
        current_retry_attempt = ac_retry_attempts[ac_idx]
        state = await self._load_lateral_escalation_state(ac_idx, execution_id=execution_id)
        ac_content = ac_text(seed.acceptance_criteria[ac_idx])
        # Fix 4 (round 2, BLOCKING): re-verified every iteration below, not
        # just once here before the loop starts.
        engaged = False

        while True:
            terminal = await self._root_ac_terminal_state(
                seed=seed,
                ac_idx=ac_idx,
                result=current_result,
                retry_attempt=current_retry_attempt,
                # Round-4 Finding #2 (BLOCKING), generalizing the Fix 6 redo:
                # this method is ONLY ever reached once the AC's ordinary
                # same-runtime retry budget is spent (both call sites route
                # through ``_finalize_batch_ac_with_escalation``), so the
                # premise "a higher tier/effort might arrive on a future
                # BUDGET-FUNDED same-runtime retry" is false here in EVERY
                # case -- not just the zero-budget-from-the-start one the Fix
                # 6 redo special-cased. Effort escalation raises exactly ONE
                # notch total (``EFFORT_RAISE_RETRY_THRESHOLD``), so a
                # low/medium base can NEVER reach the "xhigh" ceiling no
                # matter how many retries ran: gating ladder entry on
                # literally reaching the ceiling let a small retry budget
                # starve the ladder out entirely, surfacing FAILED with every
                # persona untried. Each ACTIVELY-configured routing axis is
                # therefore treated as at ceiling on every check (a dormant
                # axis stays dormant, preserving the
                # no-escalation-dial-configured opt-out), and eligibility is
                # decided by ``success``/``is_decomposed`` alone -- the
                # ladder's own redispatches still resolve their REAL
                # tier/effort for the actual dispatch.
                same_runtime_retry_available=False,
            )
            if not terminal:
                if not engaged:
                    # Never entered the ladder at all: success, or no
                    # escalation axis is actively configured (both dormant) --
                    # today's unchanged entry behavior for those cases.
                    return None
                # A redispatch INSIDE the loop bounced into a non-terminal
                # outcome (with forced-ceiling semantics this now means: it
                # got decomposed instead of staying atomic, so
                # ``is_decomposed`` makes ``is_terminal_state_failure``
                # false) -- there is cheaper room left that was not tried, so
                # the persona ladder must stop advancing rather than keep
                # cycling personas on a result that no longer reflects a
                # genuine "stuck at maximum strength" state. Hand back this
                # CURRENT result (not ``None``, which would make the caller
                # fall back to the stale pre-ladder result -- the same class
                # of bug Fix 3 fixes for the infra-fatal case).
                return current_result
            engaged = True

            failure_class = self._failure_class_for_result(current_result)
            failure_text = (
                failure_class or current_result.error or current_result.final_message or ""
            )
            pre_advance_state = state
            step = advance_lateral_escalation(
                state, terminal_state_failure=True, failure_text=failure_text
            )
            state = step.state
            self._lateral_escalation_states[ac_idx] = state
            # Fix 5 (round 2, BLOCKING): persist EVERY streak advancement, not
            # just the moment this AC actually reaches full parking, so a
            # process cancelled/restarted mid-persona-attempt can reconstruct
            # exactly which personas were already tried instead of restarting
            # the ladder from scratch. Emitted BEFORE the redispatch below so
            # a crash during that redispatch still leaves this step's
            # progress durably recorded.
            progressed_persisted = await self._event_emitter.emit_lateral_escalation_progressed(
                execution_id=execution_id,
                session_id=session_id,
                node_id=ExecutionNodeIdentity.root(
                    execution_context_id=execution_id, ac_index=ac_idx
                ).node_id,
                root_ac_index=ac_idx,
                personas_tried=tuple(p.value for p in state.personas_tried),
                consecutive_terminal_failures=state.consecutive_terminal_failures,
                parked=state.parked,
                persona=step.persona.value if step.persona is not None else None,
            )
            if not progressed_persisted:
                # Round-5 Finding #4 (BLOCKING, superseding round 4's log-only
                # stance): logging does not make persistence fail-closed. On a
                # future replay, a write that FAILED and a write that NEVER
                # HAPPENED are both silence — and silence reads as "no prior
                # state" (fail-open: personas re-tried from scratch, parked
                # status lost). ``_safe_emit_event`` already retried with
                # backoff; once it is exhausted, the semantic advancement that
                # depends on this write must NOT proceed: revert to the
                # pre-advance state, hold at the operator-visible parked
                # cadence, and retry the SAME step (``advance_lateral_escalation``
                # is deterministic for identical inputs) on the next
                # iteration. The redispatch below never runs under a state the
                # durable log cannot corroborate.
                log.error(
                    "parallel_executor.lateral_escalation.progressed_write_failed_correctness_risk",
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    personas_tried=[p.value for p in state.personas_tried],
                    parked=state.parked,
                )
                state = pre_advance_state
                self._lateral_escalation_states[ac_idx] = pre_advance_state
                await self._sleep(self._parked_retry_backoff_seconds)
                continue

            if step.just_parked:
                parked_node_id = ExecutionNodeIdentity.root(
                    execution_context_id=execution_id, ac_index=ac_idx
                ).node_id
                parked_persisted = await self._event_emitter.emit_ac_parked_for_operator(
                    execution_id=execution_id,
                    session_id=session_id,
                    node_id=parked_node_id,
                    root_ac_index=ac_idx,
                    personas_tried=tuple(p.value for p in state.personas_tried),
                    consecutive_terminal_failures=state.consecutive_terminal_failures,
                    backoff_seconds=self._parked_retry_backoff_seconds,
                    reason=(
                        "all lateral-thinking personas exhausted; AC still failing "
                        "at maximum strength"
                    ),
                )
                if not parked_persisted:
                    # Round-5 Finding #4 (BLOCKING): same fail-closed
                    # treatment as the ``progressed`` write above, for the
                    # full-parking transition. A future resume unable to find
                    # THIS event durably recorded would lose the
                    # operator-visible parked signal entirely
                    # (``_load_lateral_escalation_state`` fails closed only
                    # on a genuine replay exception, not on a
                    # clean-but-incomplete durable log). Revert and hold: the
                    # next iteration re-advances to this same parking step
                    # (re-emitting the ``progressed`` event is harmless — its
                    # payload is identical and reconstruction takes the
                    # latest) and re-attempts this write until it lands.
                    log.error(
                        "parallel_executor.lateral_escalation.parked_write_failed_correctness_risk",
                        ac_idx=ac_idx,
                        execution_id=execution_id,
                        node_id=parked_node_id,
                    )
                    state = pre_advance_state
                    self._lateral_escalation_states[ac_idx] = pre_advance_state
                    await self._sleep(self._parked_retry_backoff_seconds)
                    continue

            if step.apply_long_backoff:
                await self._sleep(self._parked_retry_backoff_seconds)
                retry_prompt = self._build_ac_retry_prompt(
                    result=current_result, ac_content=ac_content, is_final_attempt=True
                )
            elif step.persona is not None:
                retry_prompt = build_persona_retry_prompt(
                    persona=step.persona,
                    ac_content=ac_content,
                    current_approach=(
                        self._build_ac_retry_prompt(
                            result=current_result, ac_content=ac_content, is_final_attempt=False
                        )
                        or "The previous attempts failed as described above."
                    ),
                    failed_attempts=(failure_text,) if failure_text else (),
                )
            else:
                # Below the persona-cycling threshold: one more IDENTICAL
                # configuration retry before the ladder changes strategy.
                retry_prompt = self._build_ac_retry_prompt(
                    result=current_result, ac_content=ac_content, is_final_attempt=False
                )

            current_retry_attempt += 1
            ac_retry_attempts[ac_idx] = current_retry_attempt
            retry_results = await self._execute_ac_batch(
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
                retry_prompts={ac_idx: retry_prompt},
                same_runtime_budget_exhausted=True,
                # Round-5 Finding #2 (BLOCKING): the eligibility check above
                # treats every active routing axis as at ceiling — make the
                # actual dispatch honor that instead of the one-notch
                # incremental climb designed for the pre-ladder retry loop.
                force_frontier_routing=True,
            )
            candidate = retry_results[0]
            if not isinstance(candidate, ACExecutionResult):
                # A raw, uncaught exception escaped even _execute_ac_batch's
                # own per-AC exception handling -- genuinely infra-fatal by
                # construction, exactly like _execute_atomic_ac's own
                # exception handler (which wraps this same class of failure
                # as a structured result instead of letting it propagate).
                # Wrap it the same way so it flows through the SAME
                # finalization path below and can be propagated OUT as the
                # actual current result (Fix 3, round 2, BLOCKING) instead of
                # silently vanishing into a bare exception object no caller
                # downstream of this ladder knows how to handle.
                candidate = ACExecutionResult(
                    ac_index=ac_idx,
                    ac_content=ac_content,
                    success=False,
                    error=str(candidate),
                    retry_attempt=current_retry_attempt,
                    infra_fatal=True,
                )
            candidate = await self._apply_verify_gate(
                seed=seed,
                ac_index=ac_idx,
                result=candidate,
                session_id=session_id,
                execution_id=execution_id,
            )
            await self._emit_ac_outcome_finalized(
                result=candidate,
                root_ac_index=ac_idx,
                session_id=session_id,
                execution_id=execution_id,
            )

            if candidate.success:
                # Breakthrough: reset the streak and surface the success.
                # Round-5 Finding #3 (BLOCKING): emit the durable resolution
                # companion event on EVERY successful ladder exit — parked or
                # not. Every loop iteration above has already durably emitted
                # a ``lateral_escalation_progressed`` event BEFORE its
                # redispatch, so a persona that succeeds partway through the
                # cycle (before parking) otherwise leaves that ``progressed``
                # event as the LATEST replayable record for this node id:
                # replay-based projections (Kanban/HUD/conductor) would show
                # a COMPLETED AC as still actively "escalating" forever, and
                # ``_load_lateral_escalation_state`` would reconstruct a
                # stale mid-ladder streak on a later resume. The previous
                # ``if state.parked:`` gate only covered the fully-parked
                # case; ``parked_resolved`` already means "this node's
                # escalation episode is over" to every consumer (the board/
                # HUD reducers clear all escalation badges on it, and the
                # state loader resets reconstruction to fresh), so it is the
                # correct signal for both exits.
                await self._resolve_escalation_success(
                    ac_idx=ac_idx, execution_id=execution_id, session_id=session_id
                )
                return candidate

            current_result = candidate
            if not self._is_retryable_failure(candidate):
                # Fix 3 (round 2, BLOCKING): propagate this CURRENT terminal
                # result (infra-fatal, blocked, or otherwise non-retryable)
                # out to the caller instead of ``None``. Returning ``None``
                # here previously made the caller interpret "the ladder
                # produced no new result" and fall back to finalizing the
                # STALE quality-failure result captured before the ladder
                # started — silently discarding whatever genuinely happened
                # on this redispatch and reporting a wrong reason for the
                # AC's final state. The caller now distinguishes a returned
                # FAILED result from a returned SUCCESS result and emits
                # recovery-exhausted for the former using THIS fresh result.
                return candidate

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

        Round-5 Finding #1 (BLOCKING): BEFORE the ordinary dispatch/retry
        path runs, each AC's durable lateral-escalation state is restored
        (lazily, from the event store — the same replay-on-miss convention
        every other durable state here uses). An AC with a recorded
        mid-ladder or parked phase never re-enters the fresh un-backed-off
        retry budget: it is routed through ``_resume_escalated_ac``, which
        re-enters the ladder at the restored phase/cadence and fires the
        resolution transition on success. In a non-crash run this is a
        no-op: the in-memory cache serves fresh default states.
        """
        # Restore ladder/parked phase BEFORE any ordinary dispatch. Note the
        # loader fails closed (synthetic parked=True, uncached) on a replay
        # failure — such an AC is deliberately routed through the resumed
        # path too, trading an unnecessary parked-cadence attempt (an
        # acceptable false positive) for never granting a fresh retry budget
        # to an AC whose durable phase we cannot read.
        resumed_ladder_states: dict[int, LateralEscalationState] = {}
        if self._lateral_escalation_enabled:
            for ac_idx in batch_executable:
                restored = await self._load_lateral_escalation_state(
                    ac_idx, execution_id=execution_id
                )
                if (
                    restored.parked
                    or restored.consecutive_terminal_failures > 0
                    or restored.personas_tried
                ):
                    resumed_ladder_states[ac_idx] = restored
                    # Re-enter at the post-budget phase — the durable record
                    # proves this AC already spent its ordinary budget before
                    # the ladder engaged. Never reset to a fresh budget.
                    ac_retry_attempts[ac_idx] = max(
                        ac_retry_attempts[ac_idx], self._ac_retry_attempts
                    )

        position_by_idx = {ac_idx: position for position, ac_idx in enumerate(batch_executable)}
        fresh_executable = [
            ac_idx for ac_idx in batch_executable if ac_idx not in resumed_ladder_states
        ]
        results: list[ACExecutionResult | BaseException] = [None] * len(batch_executable)  # type: ignore[list-item]
        if fresh_executable:
            fresh_results = await self._execute_ac_batch(
                seed=seed,
                batch_indices=fresh_executable,
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
            for fresh_position, ac_idx in enumerate(fresh_executable):
                results[position_by_idx[ac_idx]] = fresh_results[fresh_position]
        for ac_idx, restored in resumed_ladder_states.items():
            # Sequential, like the ladder finalization at the bottom of this
            # function: a resumed ladder AC may block on the parked cadence.
            # Its returned result is FINAL (verify-gated, outcome-finalized,
            # ladder-processed) — it must not re-enter the fresh-path
            # gate/retry/finalize machinery below.
            results[position_by_idx[ac_idx]] = await self._resume_escalated_ac(
                seed=seed,
                ac_idx=ac_idx,
                restored_state=restored,
                ac_retry_attempts=ac_retry_attempts,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                execution_counters=execution_counters,
            )

        retry_termination_reasons: dict[int, str] = {}
        # V1 gate on freshly-successful ACs.
        for position, ac_idx in enumerate(batch_executable):
            if ac_idx in resumed_ladder_states:
                continue
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
                await self._emit_ac_outcome_finalized(
                    result=gated,
                    root_ac_index=ac_idx,
                    session_id=session_id,
                    execution_id=execution_id,
                )

        if self._ac_retry_attempts <= 0:
            # Fix 6 (round 3, BLOCKING): ``ac_retry_attempts=0`` is a valid,
            # documented configuration (no same-runtime retries before
            # escalation) -- it must behave like "immediately exhausted, now
            # try the lateral-escalation ladder," not "exhausted, stop." This
            # early return used to emit recovery-exhausted directly and
            # return, completely bypassing ``_maybe_run_lateral_escalation_ladder``
            # (only reachable, before this fix, through the ``while pending:``
            # loop below -- which a zero same-runtime-retry-budget config
            # never enters). Route through the SAME escalate-then-finalize
            # helper the bottom of this function uses, so the ladder is tried
            # here too even with ``lateral_escalation_enabled=True`` and a
            # zero ordinary retry budget.
            for position, ac_idx in enumerate(batch_executable):
                if ac_idx in resumed_ladder_states:
                    continue
                result = results[position]
                if isinstance(result, ACExecutionResult):
                    results[position] = await self._finalize_batch_ac_with_escalation(
                        seed=seed,
                        ac_idx=ac_idx,
                        result=result,
                        ac_retry_attempts=ac_retry_attempts,
                        session_id=session_id,
                        execution_id=execution_id,
                        tools=tools,
                        tool_catalog=tool_catalog,
                        system_prompt=system_prompt,
                        level_contexts=level_contexts,
                        execution_counters=execution_counters,
                        retry_termination_reason=(
                            "budget_exhausted"
                            if self._is_retryable_failure(result)
                            else "not_retryable"
                        ),
                    )
            return results

        # V3 retry loop: re-dispatch non-stall failures up to the configured
        # attempts. Kill criterion: stop early when the failure class repeats.
        pending = {
            ac_idx
            for position, ac_idx in enumerate(batch_executable)
            if ac_idx not in resumed_ladder_states and self._is_retryable_failure(results[position])
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
                if isinstance(gated, ACExecutionResult):
                    await self._emit_ac_outcome_finalized(
                        result=gated,
                        root_ac_index=ac_idx,
                        session_id=session_id,
                        execution_id=execution_id,
                    )

                if not self._is_retryable_failure(gated):
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
                        # Fix 2 (round 3, BLOCKING): these two probes ask two
                        # DIFFERENT questions and must read two DIFFERENT trust
                        # values, not the same stale proposal-time heuristic.
                        # ``just_dispatched`` asks "what trust was ACTUALLY
                        # ACTIVE for the dispatch that just finished" -- the
                        # value ``_execute_decomposition_children`` actually
                        # consumed to pick the child tier THIS round, computed
                        # from the PRIOR round's attestation before this round
                        # ran. ``next_scheduled`` asks "what will be active for
                        # the NEXT dispatch" -- the CURRENT/latest gate-anchored
                        # attestation, i.e. THIS round's own just-computed
                        # verdict, which becomes the prior attestation the next
                        # retry's decomposition consults. Reading the same
                        # (stale, pre-dispatch) value for both meant a round
                        # that just flipped from trustworthy to untrustworthy
                        # could early-stop retries before that change ever took
                        # effect.
                        just_dispatched_trustworthy = (
                            isinstance(gated, ACExecutionResult)
                            and gated.dispatched_decomposition_trustworthy
                        )
                        next_scheduled_trustworthy = (
                            isinstance(gated, ACExecutionResult)
                            and gated.decomposition_attestation is not None
                            and gated.decomposition_attestation.trustworthy
                        )
                        just_dispatched = decide_model(
                            model_support,
                            router=self._model_router,
                            is_decomposed_child=routes_as_child,
                            decomposition_trustworthy=just_dispatched_trustworthy,
                            retry_attempt=ac_retry_attempts[ac_idx],
                        )
                        next_scheduled = decide_model(
                            model_support,
                            router=self._model_router,
                            is_decomposed_child=routes_as_child,
                            decomposition_trustworthy=next_scheduled_trustworthy,
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
                            await self._emit_ac_outcome_finalized(
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
            if ac_idx in resumed_ladder_states:
                # Round-5 Finding #1: already final — verify-gated, outcome-
                # finalized, and ladder-processed inside _resume_escalated_ac.
                continue
            result = results[position]
            if not isinstance(result, ACExecutionResult):
                continue
            # Task 2: an AC that exhausted its ordinary retry budget while
            # failing at maximum strength (frontier tier, max effort, atomic)
            # is handed to the lateral-persona escalation ladder INSTEAD of
            # being marked exhausted/FAILED here. Genuinely infra-fatal
            # failures and ACs still short of maximum strength fall through
            # to the unchanged recovery-exhausted path below.
            results[position] = await self._finalize_batch_ac_with_escalation(
                seed=seed,
                ac_idx=ac_idx,
                result=result,
                ac_retry_attempts=ac_retry_attempts,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                execution_counters=execution_counters,
                retry_termination_reason=retry_termination_reasons.get(
                    ac_idx,
                    "budget_exhausted" if self._is_retryable_failure(result) else "not_retryable",
                ),
            )
        return results

    async def _finalize_batch_ac_with_escalation(
        self,
        *,
        seed: Seed,
        ac_idx: int,
        result: ACExecutionResult,
        ac_retry_attempts: dict[int, int],
        session_id: str,
        execution_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext],
        execution_counters: dict[str, int] | None,
        retry_termination_reason: str,
    ) -> ACExecutionResult:
        """Hand one exhausted batch AC to the lateral-escalation ladder before
        accepting it as recovery-exhausted (Fix 6, round 3 / Task 2).

        Shared by both call sites that reach "this AC's ordinary retry budget
        is spent" -- the ``ac_retry_attempts <= 0`` early return above AND the
        bottom of the ``while pending:`` loop -- so an AC stuck at maximum
        strength always gets a chance at the lateral-persona ladder before
        surfacing as exhausted/failed, regardless of which path got it here.
        A result the ladder does not engage with (success, not retryable, or
        the ladder disabled) passes through ``_maybe_run_lateral_escalation_ladder``
        unchanged (returns ``None``), so this always falls back to the
        original recovery-exhausted behavior for those cases.
        """
        escalated = await self._maybe_run_lateral_escalation_ladder(
            seed=seed,
            ac_idx=ac_idx,
            result=result,
            ac_retry_attempts=ac_retry_attempts,
            session_id=session_id,
            execution_id=execution_id,
            tools=tools,
            tool_catalog=tool_catalog,
            system_prompt=system_prompt,
            level_contexts=level_contexts,
            execution_counters=execution_counters,
        )
        if escalated is not None:
            # Fix 3 (round 2, BLOCKING): the ladder can now return a FAILED
            # result too (infra-fatal or otherwise non-retryable, discovered
            # mid-ladder), not just a breakthrough SUCCESS. Emit
            # recovery-exhausted for that case using THIS fresh result --
            # never the stale pre-ladder ``result`` argument -- so the
            # durable record reflects what actually happened on the ladder's
            # last redispatch.
            if not escalated.success:
                await self._emit_recovery_exhausted(
                    seed=seed,
                    result=escalated,
                    root_ac_index=ac_idx,
                    session_id=session_id,
                    execution_id=execution_id,
                    retry_termination_reason=(
                        "infra_fatal" if escalated.infra_fatal else "not_retryable"
                    ),
                )
            return escalated
        await self._emit_recovery_exhausted(
            seed=seed,
            result=result,
            root_ac_index=ac_idx,
            session_id=session_id,
            execution_id=execution_id,
            retry_termination_reason=retry_termination_reason,
        )
        return result

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

        verifier = self._atomic_verifier
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
                else self._verify_atomic_evidence_against_runtime_messages(
                    messages=messages,
                    typed_evidence=scoped_evidence,
                    ac_content=ac_content,
                    has_success_contract=has_success_contract,
                    has_expected_artifacts=has_expected_artifacts,
                    verify_gate_active=verify_gate_active,
                    task_cwd_override=task_cwd_override,
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
        return _verify_atomic_evidence_against_runtime_messages(
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
            "enforced": self._fat_harness_mode,
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
]
