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
from collections.abc import Awaitable, Callable, Iterator, Mapping
import contextlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
from itertools import islice
import json
import marshal
import math
import os
from pathlib import Path
import re
import socket
from typing import TYPE_CHECKING, Any, ClassVar, Literal, cast
import uuid

import anyio
from rich.console import Console

from ouroboros.core.errors import OuroborosError
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
from ouroboros.orchestrator.ac_execution_capsule import (
    MAX_AC_CONTEXT_REFERENCES,
    ACContextReference,
    ACSuccessContract,
    bind_capsule_to_runtime_handle,
    build_ac_dependency_references,
    build_ac_dispatch_authority_scope,
    compile_ac_execution_capsule,
)
from ouroboros.orchestrator.ac_runtime_handle_manager import (
    ACRuntimeHandleManager,
    AmbiguousACExecutionError,
    CompletedACExecutionError,
)
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
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
    EFFORT_LADDER,
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
    ACContextSummary,
    LevelContext,
    deserialize_level_contexts,
    extract_level_context,
    serialize_level_contexts,
)
from ouroboros.orchestrator.model_routing import (
    DEFAULT_TIER_CEILING,
    decide_model,
    deserialize_model_router,
    resolve_execute_model,
    serialize_model_router,
    tier_from_profile_hint,
)
from ouroboros.orchestrator.parallel_executor_models import (
    ACExecutionOutcome,
    ACExecutionResult,
    ParallelExecutionResult,
    ParallelExecutionStageResult,
    StageExecutionOutcome,
)
from ouroboros.orchestrator.profile_loader import (
    ExecutionProfile,
    SuggestedModelTier,
    deserialize_execution_profile,
    serialize_execution_profile,
)
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
# Round-6 Findings #3/#4: how many long-cadence background retries a
# correctness-bearing durable write gets after its bounded FOREGROUND
# retries are exhausted, before the process finally gives up loudly. At the
# default parked cadence (300s) this is ~4 hours of retrying — "keep
# retrying at a longer interval rather than giving up silently" — while
# still bounded, so a truly unwritable store does not spin a leaked task
# forever after the run ends.
_DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS = 48
# How long ``execute_parallel`` waits for still-pending deferred durable
# writes before the run completes. The primary CLI entrypoint wraps the whole
# run in ``asyncio.run``, whose teardown CANCELS every pending task the
# moment the run coroutine returns — without a bounded drain, a deferred
# write scheduled near the end of a run (the common case: they fire right
# after an AC/round completes) would be silently cancelled with zero real
# attempts. Deliberately a final-shot bound for the in-flight attempt, not
# the full multi-hour parked-cadence budget.
_DEFERRED_DURABLE_WRITE_DRAIN_TIMEOUT_SECONDS = 10.0
_VERIFY_OUTPUT_TAIL_CHARS = 2000  # How much verify-command output to attach
_DURABLE_CONTEXT_MAX_AC_CONTENT_CHARS = 2000
_DURABLE_CONTEXT_MAX_TOOL_NAMES = 64
_DURABLE_CONTEXT_MAX_FILE_PATHS = 128
_DURABLE_CONTEXT_MAX_ITEM_CHARS = 1024


@dataclass(frozen=True)
class _VerifyGateOutcome:
    """Outcome of the orchestrator-run AC success-contract gate (PR-V V1)."""

    passed: bool
    reason: str | None
    output_tail: str
    missing_artifacts: tuple[str, ...] = ()


def _recovered_verify_gate_outcome(
    value: Mapping[str, Any] | None,
) -> _VerifyGateOutcome | None:
    """Rehydrate the strict durable verify projection without running its command."""
    if value is None:
        return None
    return _VerifyGateOutcome(
        passed=value["passed"],
        reason=value["reason"],
        output_tail=value["output_tail"],
        missing_artifacts=tuple(value["missing_artifacts"]),
    )


@dataclass(frozen=True)
class _RecoveredFinalizedOutcome:
    """Latest authoritative post-verify outcome reconstructed on crash resume."""

    retry_attempt: int
    success: bool
    outcome: ACExecutionOutcome
    is_decomposed: bool = False
    recovery_exhausted: bool = False
    forced_frontier_routing: bool = False
    context_summary: ACContextSummary | None = None


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
    if parallel_result.unconfirmed_durable_writes:
        # Round-8 finding #3: the bounded run-completion drain had to give up
        # with correctness-bearing durable writes still unconfirmed. The run
        # itself is NOT failed for this (escalation-mandate direction), but
        # the uncertainty must be visible in the run's own final output —
        # never only in a log line: a later crash before the record ever
        # lands is indistinguishable from the transition never happening.
        lines.append("")
        lines.append(
            f"WARNING: {len(parallel_result.unconfirmed_durable_writes)} "
            "correctness-bearing durable write(s) could not be confirmed "
            "persisted before the run completed. The true durable state of "
            "the affected AC(s)/episode(s) is UNCERTAIN — a later resume may "
            "reconstruct stale state. Verify the event log before relying on "
            "this run's recorded outcomes:"
        )
        for description in parallel_result.unconfirmed_durable_writes:
            lines.append(f"- unconfirmed: {description}")
    return "\n".join(lines)


# =============================================================================
# Parallel Executor
# =============================================================================


class CheckpointOwnershipError(OuroborosError):
    """A non-terminal RC3 checkpoint appears to belong to a LIVE run.

    Round-12 finding #3 (BLOCKING): checkpoints are keyed by the stable
    ``seed_id`` alone and adopted automatically on launch. The round-10
    staleness gate distinguishes crashed runs from FINISHED ones, but it
    cannot tell a crashed run's checkpoint from one written by another
    process that is STILL actively running (or legitimately paused/parked)
    this same seed right now. Adopting such a checkpoint would put two live
    processes on the SAME execution aggregate — racing on durable state,
    double-dispatching ACs. This error fails the second launch immediately
    and loudly instead: a genuinely infra-fatal launch conflict, not an AC
    failure, so the escalation mandate (never surface FAILED with
    escalation options untried) is not implicated — no AC work has started
    and retrying cannot help while the owning process is alive.
    """


class CheckpointUnreadableError(OuroborosError):
    """The RC3 checkpoint store could not CONFIRM whether a checkpoint exists.

    Round-15 finding #2 (BLOCKING, load direction): a degraded checkpoint
    READ used to fall through to "proceed as if no checkpoint exists" — the
    fail-OPEN direction the ``_replay_with_retry`` convention forbids for
    every other durable read in this file. A checkpoint that genuinely
    exists but cannot be read may belong to a still-live run (silently
    bypassing the round-12 ownership gate) or to an interrupted run whose
    execution_id/policy/ladder history rounds 9-13 exist to restore;
    running fresh over either mints a new execution_id and races or orphans
    the original run's durable state. Like
    :class:`CheckpointOwnershipError`, this fails the LAUNCH loudly after
    bounded read retries, before any AC work starts — an infra-fatal store
    degradation, not an AC failure, so the escalation mandate (never
    surface FAILED with escalation options untried) is not implicated. A
    CONFIRMED "no checkpoint at any rollback level" is still an ordinary
    fresh launch — only the indeterminate read refuses.
    """


class CheckpointCorruptError(OuroborosError):
    """A persisted checkpoint exists but cannot be safely interpreted.

    Running fresh would duplicate work whose side effects may already have
    landed, while deleting the checkpoint would destroy the only recovery
    record.  Corruption therefore blocks the launch for operator repair.
    """


class CheckpointPlanMismatchError(OuroborosError):
    """The re-derived execution plan differs from the interrupted run's plan."""


class CheckpointDispatchMismatchError(OuroborosError):
    """The live tools/prompt authority differs from the interrupted run."""


class CheckpointPersistenceError(OuroborosError):
    """The mandatory pre-dispatch recovery anchor could not be persisted."""


class ConcurrentSeedExecutionError(OuroborosError):
    """Another invocation is already executing this checkpoint-backed seed.

    Round-14 finding #1 (BLOCKING): the round-12 ownership gate
    (:class:`CheckpointOwnershipError`) probes the checkpoint owner's PID —
    which by design treats ``pid == os.getpid()`` as NON-conflicting
    ("this very process wrote it"). That is correct for the cross-process
    double-launch shape, but this codebase also runs as a long-lived MCP
    server process that can drive MULTIPLE concurrent executions (separate
    asyncio tasks sharing one OS pid). Two near-simultaneous invocations of
    the SAME seed inside that one process both pass the PID probe, both
    adopt/write the same seed-keyed checkpoint, and race the same
    execution aggregate — the exact double-dispatch/state-corruption the
    round-12 gate exists to prevent, one layer down. Coroutines in one
    process share memory, so the precise same-process probe is an in-memory
    lease (``ParallelACExecutor._ACTIVE_SEED_LEASES``). Separate processes
    hold a non-blocking filesystem lease for the full execution, closing the
    load-before-first-save race that owner PID metadata cannot make atomic.
    Like the round-12 refusal, this is an infra-fatal launch conflict
    raised before any AC work starts — no AC ever surfaces FAILED over it,
    and retrying cannot help while the first invocation is still running.
    """


def _pid_alive(pid: int) -> bool:
    """Best-effort same-host liveness probe (mirrors ``core.worktree``).

    PID reuse can make a dead owner look alive — this is a heuristic that
    errs toward refusing a conflicting launch, never toward corrupting a
    possibly-live run's state. Cross-host owners cannot be probed at all
    and fall back to the heartbeat-freshness window.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it — still alive.
        return True
    return True


#: Secondary liveness check after the duration-held filesystem execution lease
#: has been acquired. Same-host owners use the precise PID probe; cross-host
#: checkpoints use this coarse heartbeat as defense in depth for shared filesystems
#: whose locking implementation may not coordinate across hosts. A genuine
#: cross-host crash-restart inside the window merely has to wait it out.
_CHECKPOINT_OWNER_FRESHNESS = timedelta(minutes=15)
_EXECUTION_CHECKPOINT_CONTRACT_VERSION = 2
_CHECKPOINT_DISPATCH_CONTRACT_VERSION = 2
_MAX_VERIFIER_AUTHORITY_DEPTH = 8
_MAX_VERIFIER_AUTHORITY_ITEMS = 256
_MAX_VERIFIER_AUTHORITY_SCALAR_CHARS = 8_192
_MAX_VERIFIER_AUTHORITY_JSON_CHARS = 64_000


class ParallelACExecutor:
    """Executes ACs in parallel based on dependency graph."""

    #: Round-14 finding #1 (BLOCKING): in-process lease registry mapping the
    #: stable checkpoint ``seed_id`` to the token of the ONE invocation of
    #: :meth:`execute_parallel` currently allowed to run it. Class-level (not
    #: instance-level) on purpose: the runner constructs a FRESH executor per
    #: run invocation, so instance state can never see a sibling invocation —
    #: while every executor in the process shares this one dict. Purely
    #: in-memory on purpose: it guards same-process concurrency only (two
    #: coroutines sharing ``os.getpid()``, invisible to the round-12 PID
    #: probe); process death releases it implicitly, while CheckpointStore's
    #: duration-held filesystem execution lease guards separate processes.
    #: Only claimed for checkpoint-store-backed executions — the raced
    #: object is the seed-keyed checkpoint; store-less invocations keep
    #: their own execution aggregates and stay concurrency-safe by design.
    #: Mutated only in synchronous (no-await) sections, so asyncio's
    #: cooperative scheduling makes check-then-set atomic without a lock.
    _ACTIVE_SEED_LEASES: ClassVar[dict[str, str]] = {}

    @classmethod
    def _acquire_seed_execution_lease(cls, seed_id: str) -> str:
        """Claim the process-wide right to execute ``seed_id``, or refuse.

        Raises :class:`ConcurrentSeedExecutionError` (infra-fatal launch
        conflict, before any AC work) when another in-process invocation
        already holds the lease. Returns an opaque token that ONLY the
        acquiring invocation may use to release.
        """
        if seed_id in cls._ACTIVE_SEED_LEASES:
            raise ConcurrentSeedExecutionError(
                f"Another execution of seed '{seed_id}' is already running "
                "in this process. Two concurrent invocations must not share "
                "one seed's checkpoint and execution aggregate — wait for "
                "the active run to finish (or cancel it) before launching "
                "this seed again."
            )
        token = uuid.uuid4().hex
        cls._ACTIVE_SEED_LEASES[seed_id] = token
        return token

    @classmethod
    def _release_seed_execution_lease(cls, seed_id: str, token: str) -> None:
        """Release the lease iff ``token`` is the current holder's.

        Token-guarded so a stale/foreign release can never free a lease a
        DIFFERENT live invocation holds (the same reason the checkpoint
        ownership marker is pid-guarded, transposed in-process).
        """
        if cls._ACTIVE_SEED_LEASES.get(seed_id) == token:
            del cls._ACTIVE_SEED_LEASES[seed_id]

    @classmethod
    @contextlib.contextmanager
    def _claim_checkpoint_execution_lease(
        cls,
        checkpoint_store: Any,
        seed_id: str,
    ) -> Iterator[None]:
        """Claim both process-local and filesystem execution rights.

        The runner uses this boundary to keep the lease through terminal
        event persistence and terminal checkpoint deletion. Direct executor
        callers use the same boundary around their executor lifecycle.
        """
        token = cls._acquire_seed_execution_lease(seed_id)
        filesystem_lease = contextlib.ExitStack()
        try:
            try:
                filesystem_lease.enter_context(checkpoint_store.execution_lease(seed_id))
            except BlockingIOError as exc:
                raise ConcurrentSeedExecutionError(
                    f"Another process is already executing seed '{seed_id}'. "
                    "Checkpoint recovery and the run-start claim must be atomic; "
                    "wait for the active run to finish (or cancel it) before "
                    "launching this seed again."
                ) from exc
            yield
        finally:
            try:
                filesystem_lease.close()
            finally:
                cls._release_seed_execution_lease(seed_id, token)

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
        context_pack_enabled: bool | None = None,
        prompt_guidance_contract: Mapping[str, Any] | None = None,
        checkpoint_execution_lease_held: bool = False,
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
                paths (CLI `ooo run` via the runner) honor
                ``EconomicsConfig.lateral_escalation_enabled``, which ALSO
                defaults to ``False`` (deliberately opt-in everywhere — an
                earlier round established that default specifically to avoid
                an infinite-sleep hang for callers that never asked for the
                ladder); an operator enables it explicitly via config.
            context_pack_enabled: Round-14 finding #3 — the runner's pinned
                worker-prompt semantic (whether ``build_system_prompt``
                appends the deterministic repo context pack). Carried on the
                RC3 checkpoint so a crash-restart can REBUILD the system
                prompt with the ORIGINAL run's setting (see
                ``system_prompt_builder`` on :meth:`execute_parallel`).
                ``None`` (direct/test callers) means "unknown" and is never
                restored over a concrete value.
            prompt_guidance_contract: Round-14 finding #3 — the runner's
                resolved guidance identity (the same
                ``mode``/``provenance_scope``/items metadata shape the
                durable execution contract persists). Stored OPAQUELY on the
                RC3 checkpoint; on recovery it is handed back to the
                runner's prompt builder, whose ``_restore_guidance_contract``
                machinery re-resolves and identity-checks it (fail-closed on
                changed guidance, exactly like ``resume_session``).
            checkpoint_execution_lease_held: Internal runner/executor
                contract. ``True`` means the runner already owns both the
                process-local and filesystem seed leases and will retain them
                through terminal event persistence and checkpoint deletion.
                Direct callers leave this ``False`` so the executor claims
                and releases the same lease itself.
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
        self._context_pack_enabled = context_pack_enabled
        self._prompt_guidance_contract = (
            dict(prompt_guidance_contract) if prompt_guidance_contract is not None else None
        )
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
        self._runtime_execution_authority = self._runtime_execution_authority_contract()
        self._atomic_verifier_authority = self._atomic_verifier_authority_contract()
        self._coordinator = LevelCoordinator(
            adapter,
            inherited_runtime_handle=self._inherited_runtime_handle,
            task_cwd=task_cwd,
        )
        # Round-15 finding #5 (BLOCKING): the resolved concurrency is
        # execution semantics on a SHARED workspace (interleaved sibling
        # writes vs strictly sequential effects), so it rides the RC3
        # checkpoint's ``execution_semantics`` and is restored — semaphore
        # rebuilt — on crash-restart recovery.
        self._max_concurrent = max_concurrent
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
        self._capsule_tool_catalog_cache: dict[
            int,
            tuple[tuple[MCPToolDefinition, ...], dict[str, object]],
        ] = {}
        self._capsule_level_context_cache: dict[
            int,
            tuple[list[LevelContext], str],
        ] = {}
        self._capsule_level_item_digest_cache: dict[int, tuple[LevelContext, str]] = {}
        self._capsule_dependency_reference_cache: dict[
            tuple[str, int],
            tuple[list[LevelContext], tuple[ACContextReference, ...]],
        ] = {}
        self._checkpoint_store = checkpoint_store
        self._checkpoint_execution_lease_held = checkpoint_execution_lease_held
        self._decomposition_decisions: dict[str, DecompositionDecisionRecord] = {}
        # Gate-anchored decomposition trust attestation (Task 1, RLM thesis
        # hardening): the LATEST attestation computed for a finished
        # decomposition round, keyed by the round's node id. ``node_id`` is
        # stable across same-root retries (it does not encode retry_attempt),
        # so a prior round's untrustworthy verdict keeps conditioning THIS
        # root AC's next retry even after the executor re-decomposes it.
        self._decomposition_attestations: dict[str, DecompositionAttestation] = {}
        # Cross-execution attestation registry, keyed by the canonical
        # semantic split contract rather than execution/node identity. A
        # first successful round can only establish trust after its children
        # finish; this registry lets a later identical split actually consume
        # that durable verdict and makes the child-tier saving reachable.
        self._reusable_decomposition_attestations: dict[str, DecompositionAttestation] = {}
        self._decomposition_attestation_scope: str | None = None
        # Lateral-persona escalation ladder (Task 2): per-root-AC state, keyed
        # by ``ac_index``. Injectable sleep so tests never wait through a real
        # backoff duration.
        self._lateral_escalation_states: dict[int, LateralEscalationState] = {}
        # Round-6 Findings #3/#4: background tasks still retrying a
        # correctness-bearing durable write whose bounded foreground retries
        # were exhausted. Held here so the tasks are not garbage-collected
        # mid-flight and tests can await them deterministically.
        self._deferred_durable_write_tasks: set[asyncio.Task[None]] = set()
        # Round-8 finding #3: descriptions of correctness-bearing durable
        # writes that were NEVER confirmed persisted — their deferred
        # background retries either exhausted the attempt budget ("gave up")
        # or were cancelled by the bounded run-completion drain / shutdown
        # while still pending. A log line alone is not an operator surface:
        # this list is copied into ``ParallelExecutionResult
        # .unconfirmed_durable_writes`` at aggregation so the run's OWN
        # final output says "the true durable state of X is uncertain"
        # instead of reporting an ordinary, fully-durable completion.
        self._unconfirmed_durable_write_descriptions: list[str] = []
        # Finalized ordinary failures that reached the configured retry cap
        # after the last checkpoint but before the process crashed.  Their
        # dispatch already completed, so recovery feeds the reconstructed
        # result directly into terminal escalation/finalization instead of
        # repeating non-idempotent work under a new attempt number.
        self._ordinary_finalized_resume_results: dict[int, ACExecutionResult] = {}
        # Round-6 Finding #1: the ACTUAL in-flight dispatch-attempt number
        # durably recorded by the latest ``lateral_escalation_progressed``
        # event for each root AC, reconstructed alongside
        # ``_lateral_escalation_states`` during replay. Consumed by the
        # resumed-ladder re-entry path so a cold resume restores the real
        # attempt counter instead of resetting it to the configured retry
        # cap (runtime handles and frugality telemetry are attempt-scoped;
        # a cap-reset could resume an older attempt's stale handle and
        # double-count its telemetry). ``None`` means the durable record
        # predates the field — the cap fallback then applies.
        self._lateral_escalation_resume_attempts: dict[int, int | None] = {}
        # Round-7 Finding #4 (extended by round 8): the durably-finalized
        # outcome of the in-flight attempt recorded by the latest
        # ``progressed`` event (``execution.ac.outcome_finalized`` correlated
        # by root_ac_index + retry_attempt). ``progressed`` is written BEFORE
        # its dispatch runs, so on its own it cannot distinguish "crash
        # mid-dispatch (re-run the persona)" from "dispatch completed, crash
        # before the NEXT durable transition". The finalized marker — emitted
        # right after every ladder dispatch returns — makes that distinction
        # exact, and its ``(success, is_decomposed)`` fields say WHICH
        # completed outcome the crash interrupted: ``success=True`` means a
        # real success whose episode-resolution write never landed (resume
        # must resolve the episode as SUCCESS, never fabricate a failure or
        # consume another persona), ``success=False`` means an
        # already-tried-and-failed dispatch (advance PAST it instead of
        # re-running it under the same attempt identity), and ``None`` means
        # no finalized outcome exists — the dispatch was genuinely in flight
        # and must be re-run. ``is_decomposed=True`` on a finalized failure
        # (round-8 finding #2) means the completed dispatch came back
        # DECOMPOSED rather than staying atomic — the ladder's established
        # terminal exit for that outcome is ``redispatch_decomposed`` (stop
        # advancing personas; cheaper untried room exists), so resume must
        # preserve the flag on the reconstructed prior result instead of
        # collapsing it to a plain atomic failure that would dispatch yet
        # another persona. Reconstructed alongside
        # ``_lateral_escalation_resume_attempts`` during replay and
        # consumed (popped) by ``_resume_escalated_ac``.
        self._lateral_escalation_resume_attempt_finalized: dict[int, tuple[bool, bool] | None] = {}
        # Round-7 follow-up finding: ``lateral_escalation_progressed
        # (parked=True)`` is persisted BEFORE the separate
        # ``parked_for_operator`` event. If that second write fails and the
        # process dies, replay correctly restores ``parked=True`` — but the
        # ladder only emits ``parked_for_operator`` on the ``just_parked``
        # transition EDGE, which never re-fires for an already-parked
        # reconstruction, permanently skipping the dedicated operator
        # notification the durable-event contract advertises. The loader
        # records here whether the current episode's durable log says parked
        # with NO ``parked_for_operator`` event actually landed;
        # ``_resume_escalated_ac`` consumes (pops) it and backfills the event
        # idempotently.
        self._lateral_escalation_parked_event_missing: dict[int, bool] = {}
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
        self._recovery_exhausted_pending: set[tuple[str, int]] = set()

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
        expected_capsule_fingerprint: str | None = None,
        expected_capsule_workspace: str | None = None,
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
            expected_capsule_workspace=expected_capsule_workspace,
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
        dispatch_id: str | None = None,
        ac_content: str,
        runtime_handle: RuntimeHandle | None,
        execution_id: str | None = None,
        session_id: str | None = None,
        result_summary: str | None = None,
        success: bool | None = None,
        error: str | None = None,
        verify_gate_outcome: _VerifyGateOutcome | None = None,
    ) -> None:
        await self._ac_runtime_handle_manager._emit_ac_runtime_event(
            event_type=event_type,
            runtime_identity=runtime_identity,
            dispatch_id=dispatch_id,
            ac_content=ac_content,
            runtime_handle=runtime_handle,
            execution_id=execution_id,
            session_id=session_id,
            result_summary=result_summary,
            success=success,
            error=error,
            verify_gate_outcome=(
                {
                    "passed": verify_gate_outcome.passed,
                    "reason": verify_gate_outcome.reason,
                    "output_tail": verify_gate_outcome.output_tail,
                    "missing_artifacts": list(verify_gate_outcome.missing_artifacts),
                }
                if verify_gate_outcome is not None
                else None
            ),
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
    def _serialize_execution_plan(execution_plan: StagedExecutionPlan) -> dict[str, Any]:
        """Serialize the exact validated dependency/stage contract for resume."""
        return {
            "version": 1,
            "nodes": [
                {
                    "index": node.index,
                    "content": node.content,
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

    @classmethod
    def _checkpoint_plan_malformed(cls, cp: Any, *, total_acs: int) -> str | None:
        """Validate the complete persisted plan before any progress is adopted."""
        state = cp.state
        if not isinstance(state, Mapping):
            return f"checkpoint state is not a mapping: {type(state).__name__}"
        if "execution_plan" not in state:
            return "execution_plan contract is missing"
        raw_plan = state.get("execution_plan")
        if not isinstance(raw_plan, Mapping) or raw_plan.get("version") != 1:
            return f"execution_plan is not a version-1 mapping: {raw_plan!r}"
        if set(raw_plan) != {"version", "nodes", "stages"}:
            return "execution_plan contains missing or unknown top-level fields"
        raw_nodes = raw_plan.get("nodes")
        raw_stages = raw_plan.get("stages")
        if not isinstance(raw_nodes, list) or not isinstance(raw_stages, list):
            return "execution_plan nodes/stages are not lists"

        node_indices: set[int] = set()
        for position, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, Mapping) or set(raw_node) != {
                "index",
                "content",
                "depends_on",
                "can_run_independently",
                "requires_serial_stage",
                "serialization_reasons",
            }:
                return f"execution_plan.nodes[{position}] has an invalid shape"
            index = raw_node.get("index")
            dependencies = raw_node.get("depends_on")
            reasons = raw_node.get("serialization_reasons")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= total_acs
                or index in node_indices
                or not isinstance(raw_node.get("content"), str)
                or not isinstance(dependencies, list)
                or any(
                    not isinstance(dep, int)
                    or isinstance(dep, bool)
                    or dep < 0
                    or dep >= total_acs
                    or dep == index
                    for dep in dependencies
                )
                or not isinstance(raw_node.get("can_run_independently"), bool)
                or not isinstance(raw_node.get("requires_serial_stage"), bool)
                or not isinstance(reasons, list)
                or any(not isinstance(reason, str) for reason in reasons)
            ):
                return f"execution_plan.nodes[{position}] contains invalid values"
            node_indices.add(index)
        if node_indices != set(range(total_acs)):
            return "execution_plan nodes do not cover every acceptance criterion exactly once"

        planned_indices: set[int] = set()
        for position, raw_stage in enumerate(raw_stages):
            if not isinstance(raw_stage, Mapping) or set(raw_stage) != {
                "index",
                "ac_indices",
                "depends_on_stages",
            }:
                return f"execution_plan.stages[{position}] has an invalid shape"
            index = raw_stage.get("index")
            ac_indices = raw_stage.get("ac_indices")
            dependencies = raw_stage.get("depends_on_stages")
            if (
                index != position
                or isinstance(index, bool)
                or not isinstance(ac_indices, list)
                or not ac_indices
                or any(
                    not isinstance(ac_idx, int)
                    or isinstance(ac_idx, bool)
                    or ac_idx not in node_indices
                    or ac_idx in planned_indices
                    for ac_idx in ac_indices
                )
                or not isinstance(dependencies, list)
                or any(
                    not isinstance(dep, int) or isinstance(dep, bool) or dep < 0 or dep >= position
                    for dep in dependencies
                )
            ):
                return f"execution_plan.stages[{position}] contains invalid values"
            planned_indices.update(ac_indices)
        if planned_indices != node_indices:
            return "execution_plan stages do not cover every plan node exactly once"
        return None

    @classmethod
    def _checkpoint_plan_mismatch(
        cls,
        cp: Any,
        *,
        execution_plan: StagedExecutionPlan,
    ) -> str | None:
        """Return a loud resume refusal when dependency analysis drifted."""
        saved = cp.state.get("execution_plan")
        current = cls._serialize_execution_plan(execution_plan)
        if saved == current:
            return None
        return (
            "the current dependency analysis produced a different execution "
            "plan from the interrupted run; applying old AC outcomes to new "
            "dependency edges/stages could reorder shared-workspace writes"
        )

    @staticmethod
    def _prompt_identity(system_prompt: str) -> str:
        return "sha256:" + hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()

    @staticmethod
    def _canonical_authority_value(value: object, *, field: str) -> object:
        """Round-trip one authority payload through strict canonical JSON."""
        try:
            encoded = json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            return json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field} is not canonical JSON") from exc

    def _runtime_execution_authority_contract(self) -> dict[str, object]:
        """Capture backend-specific provider/model identity when exposed."""
        provider_descriptor = inspect.getattr_static(
            type(self._adapter),
            "execution_identity_contract",
            None,
        )
        if provider_descriptor is None:
            return {"version": 1, "observed": False}
        provider = object.__getattribute__(self._adapter, "execution_identity_contract")
        identity = provider()
        if not isinstance(identity, Mapping):
            raise ValueError("runtime execution identity contract is not a mapping")
        normalized = self._canonical_authority_value(
            dict(identity),
            field="runtime execution identity contract",
        )
        if not isinstance(normalized, dict):
            raise ValueError("runtime execution identity contract is not an object")
        return {"version": 1, "observed": True, "identity": normalized}

    @classmethod
    def _project_verifier_identity_value(
        cls,
        value: object,
        *,
        field: str,
        depth: int = 0,
        seen: set[int] | None = None,
    ) -> object:
        """Validate one explicit verifier identity as bounded canonical JSON."""
        if depth > _MAX_VERIFIER_AUTHORITY_DEPTH:
            raise ValueError(f"{field} exceeds verifier authority depth")
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError(f"{field} contains a non-finite float")
            return value
        if isinstance(value, str):
            if len(value) > _MAX_VERIFIER_AUTHORITY_SCALAR_CHARS:
                raise ValueError(f"{field} contains oversized text")
            return value

        seen = set() if seen is None else seen
        value_id = id(value)
        if value_id in seen:
            raise ValueError(f"{field} contains cyclic state")
        seen.add(value_id)
        try:
            if isinstance(value, Mapping):
                if len(value) > _MAX_VERIFIER_AUTHORITY_ITEMS:
                    raise ValueError(f"{field} contains too many mapping items")
                projected: dict[str, object] = {}
                for key, item in value.items():
                    if not isinstance(key, str) or not key:
                        raise ValueError(f"{field} contains a non-string or empty key")
                    if len(key) > _MAX_VERIFIER_AUTHORITY_SCALAR_CHARS:
                        raise ValueError(f"{field} contains an oversized key")
                    projected[key] = cls._project_verifier_identity_value(
                        item,
                        field=f"{field}.{key}",
                        depth=depth + 1,
                        seen=seen,
                    )
                return projected
            if isinstance(value, (list, tuple)):
                if len(value) > _MAX_VERIFIER_AUTHORITY_ITEMS:
                    raise ValueError(f"{field} contains too many sequence items")
                return [
                    cls._project_verifier_identity_value(
                        item,
                        field=f"{field}[{index}]",
                        depth=depth + 1,
                        seen=seen,
                    )
                    for index, item in enumerate(value)
                ]
            raise ValueError(f"{field} is not canonical JSON data")
        finally:
            seen.remove(value_id)

    @classmethod
    def _verifier_implementation_contract(cls, verifier: Verifier) -> dict[str, object]:
        """Identify the executable code behind a function, method, or callable object."""
        if inspect.ismethod(verifier):
            target = verifier.__func__
        elif inspect.isfunction(verifier) or inspect.isbuiltin(verifier):
            target = verifier
        else:
            target = type(verifier).__call__

        module = getattr(target, "__module__", type(verifier).__module__)
        qualname = getattr(target, "__qualname__", type(verifier).__qualname__)
        try:
            source_digest = cls._prompt_identity(inspect.getsource(target))
        except (OSError, TypeError):
            source_digest = None
        code = getattr(target, "__code__", None)
        code_digest = (
            "sha256:" + hashlib.sha256(marshal.dumps(code)).hexdigest()
            if code is not None
            else None
        )
        return {
            "module": str(module),
            "qualname": str(qualname),
            "source_digest": source_digest,
            "code_digest": code_digest,
        }

    def _verifier_state_authority_contract(self, verifier: Verifier) -> dict[str, object]:
        """Reuse verifier authority only when the verifier declares stable identity."""

        def _process_local(reason: str) -> dict[str, object]:
            nonce = uuid.uuid4().hex
            log.warning(
                "parallel_executor.atomic_verifier_authority_process_local",
                verifier_type=f"{type(verifier).__module__}.{type(verifier).__qualname__}",
                reason=reason,
            )
            return {
                "stability": "process_local",
                "instance_nonce": nonce,
            }

        identity_descriptor = inspect.getattr_static(
            verifier,
            "verification_identity_contract",
            None,
        )
        if identity_descriptor is None:
            return _process_local("custom verifier did not declare verification_identity_contract")
        try:
            identity_provider = object.__getattribute__(
                verifier,
                "verification_identity_contract",
            )
            if not callable(identity_provider):
                raise ValueError("verification_identity_contract is not callable")
            identity = identity_provider()
            if not isinstance(identity, Mapping):
                raise ValueError("verification_identity_contract is not a mapping")
            projected = self._project_verifier_identity_value(
                dict(identity),
                field="atomic verifier identity contract",
            )
            encoded = json.dumps(
                projected,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
            if len(encoded) > _MAX_VERIFIER_AUTHORITY_JSON_CHARS:
                raise ValueError("atomic verifier state exceeds authority budget")
            return {
                "stability": "durable",
                "state_digest": self._prompt_identity(encoded),
            }
        except (AttributeError, TypeError, ValueError) as exc:
            return _process_local(str(exc))

    def _atomic_verifier_authority_contract(self) -> dict[str, object]:
        """Return a durable identity for the acceptance judge in force."""
        verifier = self._atomic_verifier
        if verifier is None:
            return {"version": 1, "mode": "runtime_transcript"}
        return {
            "version": 1,
            "mode": "custom",
            "implementation": self._verifier_implementation_contract(verifier),
            "behavioral_state": self._verifier_state_authority_contract(verifier),
        }

    @staticmethod
    def _normalize_externally_satisfied_acs(
        raw: Mapping[int, Mapping[str, Any]] | None,
        *,
        total_acs: int,
    ) -> dict[int, dict[str, Any]]:
        """Validate the caller-controlled skip set before it can gate dispatch."""
        if raw is None:
            return {}
        if not isinstance(raw, Mapping):
            msg = "externally_satisfied_acs must be a mapping"
            raise ValueError(msg)
        normalized: dict[int, dict[str, Any]] = {}
        for ac_index, metadata in raw.items():
            if (
                isinstance(ac_index, bool)
                or not isinstance(ac_index, int)
                or not 0 <= ac_index < total_acs
            ):
                msg = f"externally_satisfied_acs contains invalid AC index: {ac_index!r}"
                raise ValueError(msg)
            if not isinstance(metadata, Mapping):
                msg = (
                    "externally_satisfied_acs metadata must be a mapping "
                    f"for AC {ac_index}: {metadata!r}"
                )
                raise ValueError(msg)
            normalized[ac_index] = dict(metadata)
        return normalized

    @classmethod
    def _normalize_reconciled_level_contexts(
        cls,
        raw: list[LevelContext] | None,
        *,
        total_acs: int,
        total_levels: int,
    ) -> list[LevelContext]:
        """Canonicalize caller-provided prompt handoff before checkpointing.

        Failed summaries never reach ``LevelContext.to_prompt_text`` and are
        therefore not dispatch authority.  Strip them while preserving the
        coordinator review, then validate the exact serialized handoff shape
        that recovery will persist and restore.
        """
        contexts: list[LevelContext] = []
        for position, context in enumerate(raw or []):
            if not isinstance(context, LevelContext):
                msg = f"reconciled_level_contexts[{position}] is not a LevelContext"
                raise ValueError(msg)
            contexts.append(
                replace(
                    context,
                    completed_acs=tuple(
                        summary for summary in context.completed_acs if summary.success
                    ),
                )
            )
        serialized = serialize_level_contexts(contexts)
        malformed = cls._checkpoint_level_contexts_malformed(
            serialized,
            total_acs=total_acs,
            plan_total_stages=total_levels,
        )
        if malformed is not None:
            msg = f"reconciled_level_contexts is invalid: {malformed}"
            raise ValueError(msg)
        return contexts

    @staticmethod
    def _runtime_capabilities_contract(adapter: object) -> dict[str, Any] | None:
        capabilities = getattr(adapter, "capabilities", None)
        if not isinstance(capabilities, RuntimeCapabilities):
            return None
        return {
            "skill_dispatch": capabilities.skill_dispatch,
            "targeted_resume": capabilities.targeted_resume,
            "structured_output": capabilities.structured_output,
            "system_prompt_support": capabilities.system_prompt_support.value,
            "tool_restriction_support": capabilities.tool_restriction_support.value,
            "permission_mode_support": capabilities.permission_mode_support.value,
            "reasoning_effort_support": capabilities.reasoning_effort_support.value,
            "enforceable_reasoning_efforts": (
                sorted(capabilities.enforceable_reasoning_efforts)
                if capabilities.enforceable_reasoning_efforts is not None
                else None
            ),
            "model_override_support": capabilities.model_override_support.value,
            "subagent_orchestration": capabilities.subagent_orchestration.value,
            "session_signals": capabilities.session_signals.to_event_data(),
        }

    def _authority_digest(self, value: object, *, field: str) -> str:
        normalized = self._canonical_authority_value(value, field=field)
        return self._prompt_identity(
            json.dumps(
                normalized,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        )

    def _capsule_tool_catalog_authority(
        self,
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
    ) -> dict[str, object]:
        """Hash a catalog once per immutable catalog object."""
        from ouroboros.orchestrator.mcp_tools import serialize_tool_catalog

        if tool_catalog is None:
            return {"present": False, "count": 0, "digest": None}
        cached = self._capsule_tool_catalog_cache.get(id(tool_catalog))
        if cached is not None and cached[0] is tool_catalog:
            return cached[1]
        serialized = serialize_tool_catalog(tool_catalog)
        authority = {
            "present": True,
            "count": len(serialized),
            "digest": self._authority_digest(
                serialized,
                field="AC capsule tool catalog",
            ),
        }
        self._capsule_tool_catalog_cache[id(tool_catalog)] = (tool_catalog, authority)
        return authority

    def _level_context_item_digest(self, context: LevelContext) -> str:
        cached = self._capsule_level_item_digest_cache.get(id(context))
        if cached is not None and cached[0] is context:
            return cached[1]
        serialized = serialize_level_contexts([context])
        digest = self._authority_digest(
            serialized[0],
            field="AC capsule level context",
        )
        self._capsule_level_item_digest_cache[id(context)] = (context, digest)
        return digest

    def _level_context_chain_digest(self, level_contexts: list[LevelContext] | None) -> str:
        if not level_contexts:
            return self._prompt_identity("ac-level-context-chain:v1")
        cached = self._capsule_level_context_cache.get(id(level_contexts))
        if cached is not None and cached[0] is level_contexts:
            return cached[1]
        digest = self._prompt_identity("ac-level-context-chain:v1")
        for context in level_contexts:
            digest = self._prompt_identity(f"{digest}\n{self._level_context_item_digest(context)}")
        self._capsule_level_context_cache[id(level_contexts)] = (level_contexts, digest)
        return digest

    def _copy_level_context_chain_digest(
        self,
        *,
        source: list[LevelContext],
        target: list[LevelContext],
    ) -> None:
        digest = self._level_context_chain_digest(source)
        self._capsule_level_context_cache[id(target)] = (target, digest)

    def _capsule_dependency_references(
        self,
        *,
        execution_id: str,
        level_contexts: list[LevelContext] | None,
    ) -> tuple[ACContextReference, ...]:
        if not level_contexts:
            return ()
        cache_key = (execution_id, id(level_contexts))
        cached = self._capsule_dependency_reference_cache.get(cache_key)
        if cached is not None and cached[0] is level_contexts:
            return cached[1]
        references = tuple(
            islice(
                build_ac_dependency_references(execution_id, level_contexts),
                MAX_AC_CONTEXT_REFERENCES + 1,
            )
        )
        self._capsule_dependency_reference_cache[cache_key] = (
            level_contexts,
            references,
        )
        return references

    def _build_capsule_dispatch_authority_contract(
        self,
        *,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext] | None,
    ) -> dict[str, object]:
        """Build an exact digest-only authority contract without large copies."""
        workspace = self._task_cwd or getattr(self._adapter, "working_directory", None)
        workspace_identity = os.path.realpath(os.path.expanduser(workspace or os.getcwd()))
        runtime_backend = getattr(self._adapter, "runtime_backend", None)
        permission_mode = getattr(self._adapter, "permission_mode", None)
        constructor_model = getattr(self._adapter, "_model", None)
        return {
            "version": 1,
            "workspace": workspace_identity,
            "runtime": {
                "backend": runtime_backend if isinstance(runtime_backend, str) else None,
                "permission_mode": (permission_mode if isinstance(permission_mode, str) else None),
                "constructor_model": (
                    constructor_model if isinstance(constructor_model, str) else None
                ),
                "capabilities": self._runtime_capabilities_contract(self._adapter),
                "execution": self._runtime_execution_authority,
            },
            "model_routing": serialize_model_router(self._model_router),
            "execution_profile": serialize_execution_profile(self._execution_profile),
            "tools": {
                "count": len(tools),
                "digest": self._authority_digest(tools, field="AC capsule tools"),
            },
            "tool_catalog": self._capsule_tool_catalog_authority(tool_catalog),
            "system_prompt_digest": self._prompt_identity(system_prompt),
            "level_contexts": {
                "count": len(level_contexts or ()),
                "chain_digest": self._level_context_chain_digest(level_contexts),
            },
        }

    def _build_checkpoint_dispatch_contract(
        self,
        *,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        system_prompt_builder: Callable[..., str] | None,
        externally_satisfied_ac_indices: tuple[int, ...] = (),
        reconciled_level_contexts: list[LevelContext] | None = None,
    ) -> dict[str, Any]:
        """Canonicalize the actual dispatch authority used by this run.

        Runner-owned prompts are rebuildable from the separately persisted
        profile/guidance/context-pack semantics, but the rebuilt text must still
        match the exact prompt the interrupted run dispatched. Direct callers
        likewise pin the exact prompt digest without a reconstruction callback.
        """
        from ouroboros.orchestrator.mcp_tools import serialize_tool_catalog

        workspace = self._task_cwd or getattr(self._adapter, "working_directory", None)
        workspace_identity = os.path.realpath(os.path.expanduser(workspace or os.getcwd()))
        runtime_backend = getattr(self._adapter, "runtime_backend", None)
        permission_mode = getattr(self._adapter, "permission_mode", None)
        constructor_model = getattr(self._adapter, "_model", None)
        return {
            "version": _CHECKPOINT_DISPATCH_CONTRACT_VERSION,
            "workspace": workspace_identity,
            "runtime": {
                "backend": runtime_backend if isinstance(runtime_backend, str) else None,
                "permission_mode": permission_mode if isinstance(permission_mode, str) else None,
                "constructor_model": (
                    constructor_model if isinstance(constructor_model, str) else None
                ),
                "capabilities": self._runtime_capabilities_contract(self._adapter),
            },
            "model_routing": serialize_model_router(self._model_router),
            "execution_profile": serialize_execution_profile(self._execution_profile),
            "externally_satisfied_ac_indices": list(externally_satisfied_ac_indices),
            "reconciled_level_contexts": serialize_level_contexts(reconciled_level_contexts or []),
            "tools": list(tools),
            "tool_catalog": {
                "present": tool_catalog is not None,
                "definitions": serialize_tool_catalog(tool_catalog or ()),
            },
            "system_prompt": (
                {"mode": "rebuildable", "identity": self._prompt_identity(system_prompt)}
                if system_prompt_builder is not None
                else {"mode": "direct", "identity": self._prompt_identity(system_prompt)}
            ),
        }

    def _build_ac_capsule_authority_scope(
        self,
        *,
        execution_context_id: str,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        level_contexts: list[LevelContext] | None,
        is_sub_ac: bool,
        decomposition_trustworthy: bool,
        force_frontier_routing: bool,
        investment_spec: InvestmentSpec | None,
        sibling_acs: list[_SiblingACRef] | None = None,
        retry_prompt_extra: str = "",
    ) -> str:
        """Bind a capsule to the exact provider dispatch authority in force."""
        dispatch_contract = self._build_capsule_dispatch_authority_contract(
            tools=tools,
            tool_catalog=tool_catalog,
            system_prompt=system_prompt,
            level_contexts=level_contexts,
        )
        execution_policy: dict[str, object] = {
            "reasoning_effort": self._reasoning_effort,
            "is_sub_ac": is_sub_ac,
            "decomposition_trustworthy": decomposition_trustworthy,
            "force_frontier_routing": force_frontier_routing,
            "investment_spec": (
                investment_spec.model_dump(mode="json") if investment_spec is not None else None
            ),
            "runtime_execution_authority": self._runtime_execution_authority,
            "atomic_verifier_authority": self._atomic_verifier_authority,
            "prompt_authority": {
                "retry_prompt_digest": self._prompt_identity(retry_prompt_extra),
                "siblings": [
                    {
                        "ac_index": sibling_index,
                        "content_digest": self._prompt_identity(sibling_content),
                    }
                    for sibling_index, sibling_content in (sibling_acs or [])
                ],
                "fat_harness_mode": self._fat_harness_mode,
                "run_verify_commands": self._run_verify_commands,
                "verify_command_timeout_seconds": self._verify_command_timeout_seconds,
                "context_pack_enabled": self._context_pack_enabled,
                "prompt_guidance_contract": self._prompt_guidance_contract,
                "decomposition_mode": self._decomposition_mode,
                "max_decomposition_depth": self._max_decomposition_depth,
            },
        }
        return build_ac_dispatch_authority_scope(
            base_scope=(
                self._decomposition_attestation_scope or f"execution:{execution_context_id}"
            ),
            dispatch_contract=dispatch_contract,
            execution_policy=execution_policy,
        )

    @staticmethod
    def _checkpoint_dispatch_contract_malformed(
        cp: Any,
        *,
        total_acs: int | None = None,
        plan_total_stages: int | None = None,
    ) -> str | None:
        state = cp.state
        if not isinstance(state, Mapping):
            return f"checkpoint state is not a mapping: {type(state).__name__}"
        if "dispatch_contract" not in state:
            return "dispatch_contract is missing"
        raw = state.get("dispatch_contract")
        if not isinstance(raw, Mapping) or set(raw) != {
            "version",
            "workspace",
            "runtime",
            "model_routing",
            "execution_profile",
            "externally_satisfied_ac_indices",
            "reconciled_level_contexts",
            "tools",
            "tool_catalog",
            "system_prompt",
        }:
            return "dispatch_contract has an invalid top-level shape"
        if raw.get("version") != _CHECKPOINT_DISPATCH_CONTRACT_VERSION:
            return (
                "dispatch_contract version is unsupported: "
                f"{raw.get('version')!r} "
                f"(expected {_CHECKPOINT_DISPATCH_CONTRACT_VERSION})"
            )
        raw_workspace = raw.get("workspace")
        if (
            not isinstance(raw_workspace, str)
            or not raw_workspace
            or not os.path.isabs(raw_workspace)
            or os.path.realpath(raw_workspace) != raw_workspace
        ):
            return "dispatch_contract.workspace is not a canonical absolute path"
        raw_runtime = raw.get("runtime")
        if not isinstance(raw_runtime, Mapping) or set(raw_runtime) != {
            "backend",
            "permission_mode",
            "constructor_model",
            "capabilities",
        }:
            return "dispatch_contract.runtime has an invalid shape"
        if any(
            value is not None and not isinstance(value, str)
            for value in (
                raw_runtime.get("backend"),
                raw_runtime.get("permission_mode"),
                raw_runtime.get("constructor_model"),
            )
        ):
            return "dispatch_contract.runtime contains invalid scalar values"
        raw_capabilities = raw_runtime.get("capabilities")
        if raw_capabilities is not None:
            if not isinstance(raw_capabilities, Mapping) or set(raw_capabilities) != {
                "skill_dispatch",
                "targeted_resume",
                "structured_output",
                "system_prompt_support",
                "tool_restriction_support",
                "permission_mode_support",
                "reasoning_effort_support",
                "enforceable_reasoning_efforts",
                "model_override_support",
                "subagent_orchestration",
                "session_signals",
            }:
                return "dispatch_contract.runtime capabilities have an invalid shape"
            if any(
                not isinstance(raw_capabilities.get(key), bool)
                for key in ("skill_dispatch", "targeted_resume", "structured_output")
            ):
                return "dispatch_contract.runtime capabilities contain invalid booleans"
            support_values = {support.value for support in ParamSupport}
            if any(
                raw_capabilities.get(key) not in support_values
                for key in (
                    "system_prompt_support",
                    "tool_restriction_support",
                    "permission_mode_support",
                    "reasoning_effort_support",
                    "model_override_support",
                )
            ):
                return "dispatch_contract.runtime capabilities contain invalid support modes"
            raw_efforts = raw_capabilities.get("enforceable_reasoning_efforts")
            if raw_efforts is not None and (
                not isinstance(raw_efforts, list)
                or any(not isinstance(level, str) for level in raw_efforts)
            ):
                return "dispatch_contract.runtime capabilities contain invalid effort levels"
            if not isinstance(raw_capabilities.get("subagent_orchestration"), str):
                return "dispatch_contract.runtime capabilities contain invalid orchestration"
            raw_signals = raw_capabilities.get("session_signals")
            if not isinstance(raw_signals, Mapping) or any(
                not isinstance(value, bool) for value in raw_signals.values()
            ):
                return "dispatch_contract.runtime signal capabilities are invalid"
        recognized_router, _ = deserialize_model_router(raw.get("model_routing"))
        if not recognized_router:
            return "dispatch_contract.model_routing is not recognized"
        recognized_profile, _ = deserialize_execution_profile(raw.get("execution_profile"))
        if not recognized_profile:
            return "dispatch_contract.execution_profile is not recognized"
        if raw.get("model_routing") != state.get("model_routing"):
            return "dispatch_contract.model_routing disagrees with checkpoint semantics"
        if raw.get("execution_profile") != state.get("execution_profile"):
            return "dispatch_contract.execution_profile disagrees with checkpoint semantics"
        external_indices = raw.get("externally_satisfied_ac_indices")
        if (
            not isinstance(external_indices, list)
            or any(
                isinstance(ac_index, bool)
                or not isinstance(ac_index, int)
                or ac_index < 0
                or (total_acs is not None and ac_index >= total_acs)
                for ac_index in external_indices
            )
            or external_indices != sorted(set(external_indices))
        ):
            return "dispatch_contract.externally_satisfied_ac_indices is invalid"
        reconciled_contexts = raw.get("reconciled_level_contexts")
        if (
            context_error := ParallelACExecutor._checkpoint_level_contexts_malformed(
                reconciled_contexts,
                total_acs=total_acs,
                plan_total_stages=plan_total_stages,
            )
        ) is not None:
            return f"dispatch_contract.reconciled_level_contexts is invalid: {context_error}"
        raw_tools = raw.get("tools")
        if not isinstance(raw_tools, list) or any(not isinstance(tool, str) for tool in raw_tools):
            return "dispatch_contract.tools is not a list of strings"
        raw_catalog = raw.get("tool_catalog")
        if not isinstance(raw_catalog, Mapping) or set(raw_catalog) != {
            "present",
            "definitions",
        }:
            return "dispatch_contract.tool_catalog has an invalid shape"
        if not isinstance(raw_catalog.get("present"), bool) or not isinstance(
            raw_catalog.get("definitions"), list
        ):
            return "dispatch_contract.tool_catalog contains invalid values"
        if any(not isinstance(item, Mapping) for item in raw_catalog["definitions"]):
            return "dispatch_contract.tool_catalog definitions are not mappings"
        try:
            json.dumps(raw_catalog["definitions"], sort_keys=True, allow_nan=False)
        except (TypeError, ValueError):
            return "dispatch_contract.tool_catalog definitions are not canonical JSON"
        raw_prompt = raw.get("system_prompt")
        if not isinstance(raw_prompt, Mapping):
            return "dispatch_contract.system_prompt is not a mapping"
        prompt_mode = raw_prompt.get("mode")
        if prompt_mode in {"rebuildable", "direct"}:
            identity = raw_prompt.get("identity")
            if (
                set(raw_prompt) != {"mode", "identity"}
                or not isinstance(identity, str)
                or not identity.startswith("sha256:")
                or len(identity) != 71
                or any(char not in "0123456789abcdef" for char in identity[7:])
            ):
                return f"{prompt_mode} system-prompt identity is malformed"
        else:
            return f"dispatch_contract.system_prompt mode is unknown: {prompt_mode!r}"
        return None

    def _checkpoint_dispatch_contract_mismatch(
        self,
        cp: Any,
        *,
        tools: list[str],
        tool_catalog: tuple[MCPToolDefinition, ...] | None,
        system_prompt: str,
        system_prompt_builder: Callable[..., str] | None,
        externally_satisfied_ac_indices: tuple[int, ...] = (),
        reconciled_level_contexts: list[LevelContext] | None = None,
    ) -> str | None:
        saved = cp.state.get("dispatch_contract")
        current = self._build_checkpoint_dispatch_contract(
            tools=tools,
            tool_catalog=tool_catalog,
            system_prompt=system_prompt,
            system_prompt_builder=system_prompt_builder,
            externally_satisfied_ac_indices=externally_satisfied_ac_indices,
            reconciled_level_contexts=reconciled_level_contexts,
        )
        # Router/profile are restorable versioned contracts.  Their dispatch
        # snapshots must agree with the checkpoint's semantic groups, while
        # workspace/runtime/constructor authority must match the live process.
        if isinstance(saved, Mapping):
            current["model_routing"] = cp.state.get("model_routing")
            current["execution_profile"] = cp.state.get("execution_profile")
            # The interrupted run's caller-controlled skip set and reconciled
            # prompt handoff are restored on adoption below. Compare every
            # other live authority axis here, but do not let a restart silently
            # replace either durable input.
            current["externally_satisfied_ac_indices"] = saved.get(
                "externally_satisfied_ac_indices"
            )
            current["reconciled_level_contexts"] = saved.get("reconciled_level_contexts")
            saved_prompt = saved.get("system_prompt")
            current_prompt = current.get("system_prompt")
            if (
                isinstance(saved_prompt, Mapping)
                and saved_prompt.get("mode") == "rebuildable"
                and isinstance(current_prompt, Mapping)
                and current_prompt.get("mode") == "rebuildable"
            ):
                # The current-process prompt was built before checkpoint
                # semantics were restored and may legitimately differ. Pin the
                # saved digest here, then verify the callback's rebuilt output
                # against it immediately after restoration and before dispatch.
                current["system_prompt"] = dict(saved_prompt)
        if saved == current:
            return None
        return (
            "the current workspace, runtime authority, tools, canonical tool catalog, "
            "reconciled prompt handoff, or direct system-prompt identity differs from "
            "the interrupted run"
        )

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

    @classmethod
    def _retry_policy_malformed(cls, raw_policy: object) -> str | None:
        """Return why a checkpointed retry policy cannot be adopted, else
        ``None`` (round-17 finding #3).

        ``None`` payload means the checkpoint predates the field — a
        genuine one-time migration shape, not corruption. These rules are
        the single source of truth shared by the pre-adoption gate
        (``_checkpoint_semantics_malformed``) and
        ``_restore_checkpoint_retry_policy``, so the gate and the restore
        helper can never drift apart. They mirror the runner's
        ``_valid_retry_policy_contract`` (round-9 #2) plus the round-11 #2
        execution-semantic fields (``reasoning_effort`` governs ladder
        terminal eligibility; the verify-gate pair governs whether
        attestation can be evaluated at all): a key ABSENT from the
        mapping is the forward-compat migration shape (keep the current
        value for that field only); a key PRESENT but type-mangled is
        corruption.
        """
        if raw_policy is None:
            return None
        if not isinstance(raw_policy, Mapping):
            return f"retry_policy is not a mapping: {type(raw_policy).__name__}"
        enabled = raw_policy.get("lateral_escalation_enabled")
        if not isinstance(enabled, bool):
            return f"retry_policy.lateral_escalation_enabled is not a bool: {enabled!r}"
        backoff = raw_policy.get("parked_retry_backoff_seconds")
        if (
            not isinstance(backoff, (int, float))
            or isinstance(backoff, bool)
            or not math.isfinite(backoff)
            or backoff < 1.0
        ):
            return (
                "retry_policy.parked_retry_backoff_seconds is not a finite "
                f"number >= 1.0: {backoff!r}"
            )
        retry_attempts = raw_policy.get("ac_retry_attempts")
        if (
            not isinstance(retry_attempts, int)
            or isinstance(retry_attempts, bool)
            or retry_attempts < 0
        ):
            return f"retry_policy.ac_retry_attempts is not a non-negative int: {retry_attempts!r}"
        if "reasoning_effort" in raw_policy:
            effort = raw_policy["reasoning_effort"]
            if effort is not None and (
                not isinstance(effort, str) or effort not in {"low", "medium", "high", "xhigh"}
            ):
                return f"retry_policy.reasoning_effort is not a routing effort: {effort!r}"
        if "run_verify_commands" in raw_policy and not isinstance(
            raw_policy["run_verify_commands"], bool
        ):
            return (
                "retry_policy.run_verify_commands is not a bool: "
                f"{raw_policy['run_verify_commands']!r}"
            )
        if "verify_command_timeout_seconds" in raw_policy:
            verify_timeout = raw_policy["verify_command_timeout_seconds"]
            if (
                not isinstance(verify_timeout, int)
                or isinstance(verify_timeout, bool)
                or verify_timeout < 1
            ):
                return (
                    "retry_policy.verify_command_timeout_seconds is not an "
                    f"int >= 1: {verify_timeout!r}"
                )
        return None

    def _restore_checkpoint_retry_policy(self, raw_policy: object) -> None:
        """Restore the checkpointed run's retry/termination policy (round-9 #2).

        ``None`` means the checkpoint predates the field — a genuine
        one-time migration that keeps the current-config posture (the
        pre-fix behavior, never worse). A present policy is validated with
        the SAME rules the runner's ``_valid_retry_policy_contract``
        enforces for the durable execution contract (bool flag; finite
        backoff ``>= 1.0`` matching ``EconomicsConfig``'s own field
        contract; non-negative int retry budget) and, when well-formed,
        replaces the values ``__init__`` just resolved from the CURRENT
        config. A present-but-malformed policy is corruption/tampering:
        fail closed in the direction the escalation mandate demands —
        honor possibly-existing durable ladder history (an AC must never
        surface FAILED while escalation options remain untried) by forcing
        the escalation gates open, while keeping the current config's
        validated backoff/budget values.
        """
        if raw_policy is None:
            return
        if isinstance(raw_policy, Mapping):
            enabled = raw_policy.get("lateral_escalation_enabled")
            backoff = raw_policy.get("parked_retry_backoff_seconds")
            retry_attempts = raw_policy.get("ac_retry_attempts")
            # Round-11 finding #2 (BLOCKING) / round-17 finding #3: the full
            # validation rules (including the three round-9 #4
            # execution-semantic fields) live in ``_retry_policy_malformed``,
            # shared with the caller's pre-adoption gate so the RC3 recovery
            # path rejects the WHOLE checkpoint before this helper ever runs
            # on a malformed payload. The fallback branch below stays as
            # defense-in-depth for any other caller.
            effort_present = "reasoning_effort" in raw_policy
            verify_flag_present = "run_verify_commands" in raw_policy
            timeout_present = "verify_command_timeout_seconds" in raw_policy
            effort = raw_policy.get("reasoning_effort")
            verify_flag = raw_policy.get("run_verify_commands")
            verify_timeout = raw_policy.get("verify_command_timeout_seconds")
            if self._retry_policy_malformed(raw_policy) is None and isinstance(enabled, bool):
                if (
                    enabled != self._lateral_escalation_enabled
                    or float(backoff) != self._parked_retry_backoff_seconds
                    or retry_attempts != self._ac_retry_attempts
                    or (effort_present and effort != self._reasoning_effort)
                    or (verify_flag_present and verify_flag != self._run_verify_commands)
                    or (timeout_present and verify_timeout != self._verify_command_timeout_seconds)
                ):
                    log.info(
                        "parallel_executor.recovery.retry_policy_restored",
                        detail=(
                            "current config differs from the checkpointed run's "
                            "retry policy; keeping the policy the run started with"
                        ),
                        checkpoint_lateral_escalation_enabled=enabled,
                        current_lateral_escalation_enabled=self._lateral_escalation_enabled,
                        checkpoint_ac_retry_attempts=retry_attempts,
                        current_ac_retry_attempts=self._ac_retry_attempts,
                        checkpoint_reasoning_effort=effort if effort_present else "<absent>",
                        current_reasoning_effort=self._reasoning_effort,
                        checkpoint_run_verify_commands=(
                            verify_flag if verify_flag_present else "<absent>"
                        ),
                        current_run_verify_commands=self._run_verify_commands,
                        checkpoint_verify_command_timeout_seconds=(
                            verify_timeout if timeout_present else "<absent>"
                        ),
                        current_verify_command_timeout_seconds=(
                            self._verify_command_timeout_seconds
                        ),
                    )
                self._lateral_escalation_enabled = enabled
                self._parked_retry_backoff_seconds = float(backoff)
                self._ac_retry_attempts = retry_attempts
                # The isinstance re-checks are guaranteed true by
                # ``round9_fields_valid`` above; they only re-narrow types.
                if effort_present and (effort is None or isinstance(effort, str)):
                    self._reasoning_effort = effort
                if verify_flag_present and isinstance(verify_flag, bool):
                    self._run_verify_commands = verify_flag
                if timeout_present and isinstance(verify_timeout, int):
                    self._verify_command_timeout_seconds = verify_timeout
                return
        log.error(
            "parallel_executor.recovery.retry_policy_malformed",
            detail=(
                "checkpointed retry policy is malformed; failing closed by "
                "honoring durable escalation history under the current "
                "config's backoff/budget values"
            ),
        )
        self._lateral_escalation_enabled = True

    @staticmethod
    def _model_routing_malformed(raw_routing: object) -> str | None:
        """Return why a checkpointed model-routing contract cannot be
        adopted, else ``None`` (round-17 finding #3).

        ``None`` payload means the checkpoint predates the field (one-time
        migration). Recognition is delegated to the SAME versioned
        contract ``_restore_checkpoint_model_router`` restores through
        (``deserialize_model_router``), so the pre-adoption gate and the
        restore helper can never disagree about what is adoptable.
        """
        if raw_routing is None:
            return None
        recognized, _ = deserialize_model_router(raw_routing)
        if not recognized:
            return f"model_routing is not a recognized serialized router contract: {raw_routing!r}"
        return None

    def _restore_checkpoint_model_router(self, raw_routing: object) -> None:
        """Restore the checkpointed run's resolved model router (round-12 #2).

        The router governs actual model-tier routing during ladder dispatch
        (``resolve_execute_model``/``decide_model``) and thereby the
        frugality-proof cohort identity — an execution-semantic input exactly
        like the retry policy. The runner's durable execution contract
        already pins it for the session-resume path via
        ``serialize_model_router``/``deserialize_model_router``; this
        CHECKPOINT-level restore (the executor-internal RC3 crash-recovery
        path) reuses the SAME versioned contract, so a crash-restart adopts
        the ORIGINAL run's routing, not whatever the current process was
        constructed with.

        ``None`` means the checkpoint predates the field — a genuine
        one-time migration that keeps the current constructor-provided
        router (the pre-fix behavior, never worse). A recognized contract is
        restored wholesale, including the deliberate dormant shape
        (``enabled=False`` -> router ``None``): a kill-switched run stays
        dormant when resumed in an environment where routing is on. A
        restored router resolved for a DIFFERENT backend than the live
        adapter is safe to adopt as-is: ``resolve_execute_model`` already
        treats a backend-mismatched router as absent rather than issuing a
        model id the adapter cannot execute. A present-but-unrecognized
        payload is corruption/tampering: fail closed exactly like a
        malformed retry policy — force the escalation gates open (durable
        ladder history must be honored) while keeping the current process's
        router for actual dispatch.
        """
        if raw_routing is None:
            return
        recognized, restored_router = deserialize_model_router(raw_routing)
        if not recognized:
            log.error(
                "parallel_executor.recovery.model_routing_malformed",
                detail=(
                    "checkpointed model-routing contract is malformed; failing "
                    "closed by honoring durable escalation history while "
                    "keeping the current process's router for dispatch"
                ),
            )
            self._lateral_escalation_enabled = True
            return
        if restored_router != self._model_router:
            log.info(
                "parallel_executor.recovery.model_router_restored",
                detail=(
                    "current process's model router differs from the "
                    "checkpointed run's; keeping the router the run started with"
                ),
                checkpoint_routing_enabled=restored_router is not None,
                current_routing_enabled=self._model_router is not None,
            )
        self._model_router = restored_router

    @staticmethod
    def _execution_profile_malformed(raw_profile: object) -> str | None:
        recognized, _ = deserialize_execution_profile(raw_profile)
        if not recognized:
            return f"execution_profile is not a recognized resolved profile: {raw_profile!r}"
        return None

    def _restore_checkpoint_execution_profile(self, raw_profile: object) -> None:
        """Restore the exact resolved profile without consulting current YAML."""
        recognized, restored_profile = deserialize_execution_profile(raw_profile)
        if not recognized:  # Defense in depth; the atomic pre-adoption gate owns this.
            raise CheckpointCorruptError(
                "Checkpoint execution_profile is malformed; operator repair is required."
            )
        self._execution_profile = restored_profile

    #: The closed vocabulary ``__init__`` accepts for ``decomposition_mode``.
    _DECOMPOSITION_MODES = frozenset({"preflight", "bounce_only", "off"})
    _CHECKPOINT_V2_RETRY_POLICY_FIELDS = frozenset(
        {
            "lateral_escalation_enabled",
            "parked_retry_backoff_seconds",
            "ac_retry_attempts",
            "reasoning_effort",
            "run_verify_commands",
            "verify_command_timeout_seconds",
        }
    )
    _CHECKPOINT_V2_EXECUTION_SEMANTICS_FIELDS = frozenset(
        {
            "decomposition_mode",
            "max_decomposition_depth",
            "fat_harness_mode",
            "cross_harness_redispatch_enabled",
            "shadow_replay_enabled",
            "context_pack_enabled",
            "max_concurrent",
        }
    )

    @classmethod
    def _execution_semantics_malformed(cls, raw_semantics: object) -> str | None:
        """Return why a checkpointed execution-semantics mapping cannot be
        adopted, else ``None`` (round-17 finding #3).

        ``None`` payload means the checkpoint predates the field (one-time
        migration); a key ABSENT from a present mapping is the
        forward-compat migration shape. These rules are the single source
        of truth shared by the pre-adoption gate
        (``_checkpoint_semantics_malformed``) and
        ``_restore_checkpoint_execution_semantics``, so the gate and the
        restore helper can never drift apart.
        """
        if raw_semantics is None:
            return None
        if not isinstance(raw_semantics, Mapping):
            return f"execution_semantics is not a mapping: {type(raw_semantics).__name__}"
        if "decomposition_mode" in raw_semantics:
            mode = raw_semantics["decomposition_mode"]
            if mode not in cls._DECOMPOSITION_MODES:
                return f"execution_semantics.decomposition_mode is not a known mode: {mode!r}"
        if "max_decomposition_depth" in raw_semantics:
            depth = raw_semantics["max_decomposition_depth"]
            if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
                return (
                    "execution_semantics.max_decomposition_depth is not a "
                    f"non-negative int: {depth!r}"
                )
        for flag_key in (
            "fat_harness_mode",
            "cross_harness_redispatch_enabled",
            "shadow_replay_enabled",
        ):
            if flag_key in raw_semantics and not isinstance(raw_semantics[flag_key], bool):
                return f"execution_semantics.{flag_key} is not a bool: {raw_semantics[flag_key]!r}"
        if "context_pack_enabled" in raw_semantics:
            cpack = raw_semantics["context_pack_enabled"]
            # ``None`` is a legitimate persisted value here (a run whose
            # runner never resolved the flag), not corruption.
            if cpack is not None and not isinstance(cpack, bool):
                return f"execution_semantics.context_pack_enabled is not a bool or None: {cpack!r}"
        if "max_concurrent" in raw_semantics:
            workers = raw_semantics["max_concurrent"]
            if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
                return f"execution_semantics.max_concurrent is not an int >= 1: {workers!r}"
        return None

    def _restore_checkpoint_execution_semantics(self, raw_semantics: object) -> None:
        """Restore the remaining execution-semantic scalars (round-12 #2).

        These are constructor-injected inputs that directly change what a
        run dispatches or how it accepts work, yet were never persisted, so
        a crash-restart silently adopted the CURRENT process's values:

        - ``decomposition_mode`` — whether decomposition runs at all
          (``preflight``/``bounce_only``/``off`` are fundamentally different
          execution modes; also drives ``_enable_decomposition``).
        - ``max_decomposition_depth`` — how deep recursive decomposition may
          go before an AC must execute atomically.
        - ``fat_harness_mode`` — whether atomic acceptance enforces typed
          evidence plus a verifier PASS (verification semantics).
        - ``cross_harness_redispatch_enabled`` — whether the cross-harness
          redispatch escalation option is available (escalation semantics).
        - ``shadow_replay_enabled`` — whether successful decomposed children
          are re-dispatched for the parent-tier baseline measurement.
        - ``context_pack_enabled`` (round-14 #3) — whether the runner's
          worker system prompt carries the deterministic repo context pack.
          Persisted ``None`` (a run whose runner never resolved it — direct
          callers) restores nothing, like an absent key; only a concrete
          bool is adopted, and only over the current value, so the
          crash-restart's rebuilt prompt uses the ORIGINAL run's setting.
        - ``max_concurrent`` (round-15 #5) — the shared-workspace fan-out
          the run dispatched under. Concurrency changes SEMANTICS here, not
          just wall-clock: every AC in a level shares one working
          directory, so sequential vs interleaved sibling writes are
          observably different workspace states. Restoring it also rebuilds
          ``self._semaphore`` before any dispatch.

        Conventions mirror ``_restore_checkpoint_retry_policy`` exactly:
        ``None`` (checkpoint predates the field) keeps the current
        constructor values — a genuine one-time migration. A key ABSENT
        from a present mapping likewise keeps the current value for that
        field only (forward-compat migration shape). Any key PRESENT but
        malformed is corruption/tampering and takes the whole mapping down
        the fail-closed branch: escalation gates forced open, nothing
        adopted.
        """
        if raw_semantics is None:
            return
        if isinstance(raw_semantics, Mapping):
            mode_present = "decomposition_mode" in raw_semantics
            depth_present = "max_decomposition_depth" in raw_semantics
            fat_present = "fat_harness_mode" in raw_semantics
            cross_present = "cross_harness_redispatch_enabled" in raw_semantics
            shadow_present = "shadow_replay_enabled" in raw_semantics
            cpack_present = "context_pack_enabled" in raw_semantics
            # Round-15 finding #5: the shared-workspace concurrency the run
            # actually dispatched under.
            workers_present = "max_concurrent" in raw_semantics
            mode = raw_semantics.get("decomposition_mode")
            depth = raw_semantics.get("max_decomposition_depth")
            fat = raw_semantics.get("fat_harness_mode")
            cross = raw_semantics.get("cross_harness_redispatch_enabled")
            shadow = raw_semantics.get("shadow_replay_enabled")
            cpack = raw_semantics.get("context_pack_enabled")
            workers = raw_semantics.get("max_concurrent")
            # Round-17 finding #3: the per-field validation rules live in
            # ``_execution_semantics_malformed``, shared with the caller's
            # pre-adoption gate so the RC3 recovery path rejects the WHOLE
            # checkpoint before this helper ever runs on a malformed
            # payload. The fallback branch below stays as defense-in-depth
            # for any other caller.
            if self._execution_semantics_malformed(raw_semantics) is None:
                if (
                    (mode_present and mode != self._decomposition_mode)
                    or (depth_present and depth != self._max_decomposition_depth)
                    or (fat_present and fat != self._fat_harness_mode)
                    or (cross_present and cross != self._cross_harness_redispatch_enabled)
                    or (shadow_present and shadow != self._shadow_replay_enabled)
                    or (
                        cpack_present
                        and isinstance(cpack, bool)
                        and cpack != self._context_pack_enabled
                    )
                    or (workers_present and workers != self._max_concurrent)
                ):
                    log.info(
                        "parallel_executor.recovery.execution_semantics_restored",
                        detail=(
                            "current process's execution-semantic config "
                            "differs from the checkpointed run's; keeping the "
                            "semantics the run started with"
                        ),
                        checkpoint_decomposition_mode=(mode if mode_present else "<absent>"),
                        current_decomposition_mode=self._decomposition_mode,
                        checkpoint_max_decomposition_depth=(depth if depth_present else "<absent>"),
                        current_max_decomposition_depth=self._max_decomposition_depth,
                        checkpoint_fat_harness_mode=(fat if fat_present else "<absent>"),
                        current_fat_harness_mode=self._fat_harness_mode,
                        checkpoint_cross_harness_redispatch_enabled=(
                            cross if cross_present else "<absent>"
                        ),
                        current_cross_harness_redispatch_enabled=(
                            self._cross_harness_redispatch_enabled
                        ),
                        checkpoint_shadow_replay_enabled=(shadow if shadow_present else "<absent>"),
                        current_shadow_replay_enabled=self._shadow_replay_enabled,
                        checkpoint_context_pack_enabled=(cpack if cpack_present else "<absent>"),
                        current_context_pack_enabled=self._context_pack_enabled,
                        checkpoint_max_concurrent=(workers if workers_present else "<absent>"),
                        current_max_concurrent=self._max_concurrent,
                    )
                # The isinstance re-checks are guaranteed true by the block
                # above; they only re-narrow types for mypy.
                if mode_present and isinstance(mode, str) and mode in self._DECOMPOSITION_MODES:
                    self._decomposition_mode = cast(
                        Literal["preflight", "bounce_only", "off"], mode
                    )
                    # ``_enable_decomposition`` is DERIVED from the mode in
                    # ``__init__``; keep the pair consistent on restore.
                    self._enable_decomposition = mode != "off"
                if depth_present and isinstance(depth, int):
                    self._max_decomposition_depth = depth
                if fat_present and isinstance(fat, bool):
                    self._fat_harness_mode = fat
                if cross_present and isinstance(cross, bool):
                    self._cross_harness_redispatch_enabled = cross
                if shadow_present and isinstance(shadow, bool):
                    self._shadow_replay_enabled = shadow
                if cpack_present and isinstance(cpack, bool):
                    self._context_pack_enabled = cpack
                if workers_present and isinstance(workers, int):
                    self._max_concurrent = workers
                    # ``__init__`` built the semaphore from the CURRENT
                    # process's construction value; rebuild it BEFORE any
                    # dispatch so the recovered run actually executes at the
                    # original run's concurrency (safe here: recovery runs
                    # before the first AC acquires it).
                    self._semaphore = anyio.Semaphore(workers)
                return
        log.error(
            "parallel_executor.recovery.execution_semantics_malformed",
            detail=(
                "checkpointed execution-semantic config is malformed; failing "
                "closed by honoring durable escalation history under the "
                "current config's values"
            ),
        )
        self._lateral_escalation_enabled = True

    #: Terminal statuses after which a run can never be resumed — mirrors
    #: ``resume_session``'s non-resumable SessionStatus set (COMPLETED /
    #: FAILED / CANCELLED). ``paused`` is deliberately absent: a paused run
    #: is exactly the state the RC3 checkpoint exists to resume.
    _NON_RESUMABLE_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

    async def _checkpoint_from_terminal_run(self, cp: Any, *, total_levels: int) -> bool:
        """Return True when the checkpointed run already reached a
        non-resumable terminal outcome (round-10 finding #1, BLOCKING).

        RC3 checkpoints are keyed by ``seed.metadata.seed_id`` alone — the
        key carries no run-generation discriminator, so an entirely NEW,
        intentional re-run of the same Seed would otherwise find the
        previous run's checkpoint, adopt it, and silently skip every level
        the previous run completed. Worst case (the review's exact probe):
        a COMPLETED checkpoint makes the fresh run skip ALL levels and
        report success without dispatching a single AC.

        The discriminator is the checkpointed run's OWN durable terminal
        record: the runner mirrors every terminal transition into the
        execution aggregate as an ``execution.terminal`` event under the
        run's ``execution_id`` (which the checkpoint carries). A genuine
        crash leaves NO terminal event — resume is correct. A recorded
        ``completed`` / ``failed`` / ``cancelled`` means the run ended and
        its checkpoint is stale; only a LAST status of ``paused`` remains
        resumable (a paused-then-resumed-then-completed run appends its
        final status after the pause, so the latest event wins).

        Indeterminate replay (``_replay_with_retry`` → ``None``) keeps the
        checkpoint resumable. Both directions can violate a mandate here —
        adopting a stale checkpoint silently skips work, refusing a genuine
        crash checkpoint re-opens the round-9 #2 regression (retry policy
        and ladder history dropped, FAILED surfaced with escalation options
        untried). The tiebreaker: terminal runs now DELETE their checkpoint
        (runner-side, plus ``_discard_stale_checkpoint`` here), so a
        checkpoint's continued existence is itself evidence of an
        interrupted run, and on a degraded event store the very ladder
        loaders this recovery exists to feed would fail closed on their own
        replays anyway. The degraded read is logged loudly instead.

        Round-13 finding #2 (BLOCKING): the tiebreaker above has a real
        gap — the runner's terminal checkpoint delete is best-effort, so a
        COMPLETED run whose delete failed leaves its checkpoint behind, and
        on an indeterminate replay "the checkpoint survived, therefore the
        run was interrupted" is exactly wrong: adoption would skip EVERY
        level and report success with zero AC dispatches. The checkpoint's
        OWN recorded state is a second, replay-independent signal: only a
        run that finished its LAST level writes ``completed_levels ==
        total_levels``, so a full-completion checkpoint plus an
        indeterminate replay is far more plausibly a finished run's failed
        delete than a crash in the instant between the final level's save
        and the terminal record. And the two failure modes are asymmetric:
        adopting wrongly is a silent false SUCCESS (the forbidden
        direction), while refusing wrongly merely re-runs work that the
        checkpoint itself says already finished — no remaining levels means
        no parked/mid-ladder escalation state to lose, so the round-9 #2
        regression cannot re-open through this branch. Treat that
        combination as terminal (discard, run fresh, re-verify by
        re-executing) and say so loudly; a PARTIAL checkpoint keeps the
        adopt posture — resuming it still dispatches the remaining levels,
        so it can never produce a zero-dispatch false success. (The
        round-12 owner pid-liveness probe was considered as a third signal
        here but cannot help: a dead owner is equally consistent with
        "completed then exited" and "crashed", so it cannot break this tie.)
        """
        saved_execution_id = cp.state.get("execution_id")
        if not (isinstance(saved_execution_id, str) and saved_execution_id):
            # Legacy checkpoint without an execution_id: no aggregate to
            # correlate a terminal record against — keep the pre-fix
            # resume posture for this one-time migration shape.
            return False
        events = await self._replay_with_retry("execution", saved_execution_id)
        if events is None:
            completed_levels = cp.state.get("completed_levels")
            # Round-16 finding #1: ``completed_levels`` counts stages of the
            # plan the CHECKPOINTED run derived — but the plan is re-derived
            # by LLM dependency analysis on every launch, so THIS launch's
            # ``total_levels`` may group the same ACs differently (or
            # collapse them into one level via the analyzer fallback).
            # "Did the run finish its last level?" is a question about the
            # checkpoint's OWN plan, so compare against the plan size the
            # checkpoint itself recorded; the current plan's size remains
            # only a legacy fallback for checkpoints predating
            # ``plan_total_stages``.
            saved_plan_total = cp.state.get("plan_total_stages")
            own_plan_total = (
                saved_plan_total
                if isinstance(saved_plan_total, int)
                and not isinstance(saved_plan_total, bool)
                and saved_plan_total > 0
                else total_levels
            )
            claims_full_completion = (
                isinstance(completed_levels, int)
                and not isinstance(completed_levels, bool)
                and own_plan_total > 0
                and completed_levels >= own_plan_total
            )
            if claims_full_completion:
                log.error(
                    "parallel_executor.recovery.terminal_check_indeterminate_full_completion",
                    detail=(
                        "could not replay the checkpointed run's execution "
                        "aggregate to verify whether it reached a terminal "
                        "state, and the checkpoint's own state claims every "
                        "level already completed — adopting it would skip "
                        "all work and report success without a single AC "
                        "dispatch. Treating it as stale and re-running from "
                        "scratch instead (re-verify, never silently skip)."
                    ),
                    execution_id=saved_execution_id,
                    completed_levels=completed_levels,
                    total_levels=total_levels,
                )
                self._console.print(
                    "[red]WARNING: durable state is uncertain — the event "
                    "log for this seed's previous run could not be read, "
                    "and its leftover checkpoint claims the whole run "
                    "already finished. Refusing to adopt it (that could "
                    "falsely report success without doing any work); "
                    "re-running all levels from scratch instead. Please "
                    "investigate the event store degradation.[/red]"
                )
                return True
            log.error(
                "parallel_executor.recovery.terminal_check_indeterminate",
                detail=(
                    "could not replay the checkpointed run's execution "
                    "aggregate to verify it never reached a terminal state; "
                    "proceeding with recovery because a surviving checkpoint "
                    "is itself evidence of interruption (terminal runs "
                    "delete theirs) and its own state still has levels "
                    "outstanding — resuming dispatches that remaining work"
                ),
                execution_id=saved_execution_id,
            )
            return False
        last_status: str | None = None
        for event in events:
            if getattr(event, "type", None) != "execution.terminal":
                continue
            data = getattr(event, "data", None)
            status = data.get("status") if isinstance(data, Mapping) else None
            if isinstance(status, str):
                last_status = status
        return last_status in self._NON_RESUMABLE_TERMINAL_STATUSES

    @staticmethod
    def _checkpoint_owner_malformed(cp: Any) -> str | None:
        """Return why a current-format checkpoint owner is invalid.

        Every v2 writer emits the complete owner record. Missing or
        unreadable ownership is therefore corruption, not a legacy migration
        shape: adopting it would bypass the cross-host liveness safeguard.
        """
        state = getattr(cp, "state", None)
        if not isinstance(state, Mapping):
            return f"checkpoint state is not a mapping: {type(state).__name__}"
        owner = state.get("owner")
        if not isinstance(owner, Mapping):
            return f"owner is not a mapping: {type(owner).__name__}"
        host = owner.get("host")
        if not isinstance(host, str) or not host.strip():
            return f"owner.host is not a non-empty string: {host!r}"
        pid = owner.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return f"owner.pid is not a positive integer: {pid!r}"
        written_at = owner.get("written_at")
        if not isinstance(written_at, str) or not written_at:
            return f"owner.written_at is not a non-empty string: {written_at!r}"
        try:
            written = datetime.fromisoformat(written_at)
        except ValueError:
            return f"owner.written_at is not ISO-8601: {written_at!r}"
        if written.tzinfo is None:
            return "owner.written_at is missing timezone information"
        return None

    def _checkpoint_owner_conflict(self, cp: Any) -> str | None:
        """Return a conflict description when the checkpoint's writer may
        still be ALIVE, else ``None`` (round-12 finding #3, BLOCKING).

        The round-10 terminal-staleness gate answers "did the checkpointed
        run already FINISH?" — it cannot answer "is another process still
        RUNNING (or legitimately paused/parked, awaiting its own resume)
        this seed right now?". Both leave a non-terminal checkpoint, but
        adopting a live run's checkpoint puts two processes on the same
        execution aggregate: racing durable state, double-dispatching ACs.

        The discriminator is the ownership marker every checkpoint save now
        embeds (``owner``: pid + host + written_at), mirroring the
        pid/host/heartbeat convention ``core.worktree``'s task lock already
        established in this codebase:

        - Same host, owner pid alive (and not this process) -> CONFLICT.
          This is the precise probe for the primary single-operator shape
          (double-launch / relaunch-while-alive on one machine). PID reuse
          can false-positive here — accepted: it errs toward refusing a
          launch (self-heals when the reusing process exits), never toward
          corrupting a live run.
        - Same host, owner pid dead -> genuine crash, adopt.
        - Different host (or unprobeable pid): fall back to heartbeat
          freshness — a checkpoint written within
          ``_CHECKPOINT_OWNER_FRESHNESS`` is treated as possibly live.
          Coarse by design (checkpoints are per-level, so a long mid-level
          live run outlives the window undetected), and a genuine
          cross-host crash-restart inside the window only has to wait it
          out.
        Current-format callers validate the complete owner record through
        :meth:`_checkpoint_owner_malformed` before reaching this probe.
        The defensive ``None`` returns below are not an adoption policy.
        """
        owner = cp.state.get("owner")
        if not isinstance(owner, Mapping):
            return None
        host = owner.get("host")
        pid = owner.get("pid")
        if (
            isinstance(host, str)
            and host == socket.gethostname()
            and isinstance(pid, int)
            and not isinstance(pid, bool)
        ):
            if pid == os.getpid():
                # This very process wrote the checkpoint (in-process
                # relaunch); it cannot race itself across processes.
                return None
            if _pid_alive(pid):
                return (
                    f"process {pid} on this host ({host!r}) wrote this "
                    "checkpoint and is still running"
                )
            return None
        written_at = owner.get("written_at")
        if isinstance(written_at, str):
            try:
                written = datetime.fromisoformat(written_at)
            except ValueError:
                return None
            if written.tzinfo is None:
                written = written.replace(tzinfo=UTC)
            age = datetime.now(UTC) - written
            if age < _CHECKPOINT_OWNER_FRESHNESS:
                return (
                    f"a process on host {host!r} wrote this checkpoint "
                    f"{int(age.total_seconds())}s ago (within the "
                    f"{int(_CHECKPOINT_OWNER_FRESHNESS.total_seconds())}s "
                    "liveness window) and cannot be probed from this host"
                )
        return None

    @staticmethod
    def _seed_semantic_fingerprint(seed: Any) -> str:
        """A stable hash of the Seed's SEMANTIC content (round-15 finding #1;
        widened to the full semantic surface by round-16 finding #2).

        ``seed_id`` is a random uuid minted at Seed creation — it names an
        object, not its content, so a Seed whose content was edited keeps
        the same ``seed_id`` and would silently adopt the pre-edit run's
        checkpoint. This fingerprint captures what the checkpointed
        progress was actually progress OF.

        Round-16 finding #2 (BLOCKING): the round-15 ``v1`` scheme hashed
        ONLY the goal text plus each AC's ``derive_semantic_ac_key`` — so a
        checkpoint saved under one set of ``constraints`` / ``task_type`` /
        ``brownfield_context`` / ``ontology_schema`` / evaluation-exit
        contracts / plugin extra fields / per-AC ``investment`` values was
        silently adopted by a resume where those values had MATERIALLY
        changed (several change prompts or routing), letting old progress
        skip work under semantics that no longer match what is about to
        execute. ``v2`` therefore hashes the Seed's ENTIRE semantic
        surface: every field that shapes prompts, routing, verification,
        or evaluation. All of these are frozen into the immutable Seed and
        re-supplied verbatim by a genuine crash-restart of the same seed,
        so none can legitimately differ across a real resume.

        Still deliberately EXCLUDED:
        - ``metadata`` (``SeedMetadata``) — volatile object identity, not
          content: the random ``seed_id`` itself, ``created_at``,
          ``ambiguity_score``, interview/lineage ids, generation
          provenance. The same exclusion ``derive_semantic_ac_key``
          documents for per-AC volatile metadata.
        - The execution-plan structure — the plan is re-derived by LLM
          dependency analysis on every launch, so its grouping can
          legitimately differ across a genuine crash-restart of identical
          content; including it would discard genuine resumes (re-opening
          the round-9 #2 lost-escalation regression) without any content
          having changed.
        """
        criteria = getattr(seed, "acceptance_criteria", ()) or ()

        def _component(value: Any) -> Any:
            dump = getattr(value, "model_dump", None)
            return dump(mode="json") if callable(dump) else value

        payload = json.dumps(
            {
                "goal": str(getattr(seed, "goal", "")),
                "task_type": str(getattr(seed, "task_type", "")),
                "constraints": [str(c) for c in (getattr(seed, "constraints", ()) or ())],
                "acceptance_criteria": [
                    {
                        "key": derive_semantic_ac_key(c),
                        "investment": _component(getattr(c, "investment", None)),
                    }
                    for c in criteria
                ],
                "brownfield_context": _component(getattr(seed, "brownfield_context", None)),
                "ontology_schema": _component(getattr(seed, "ontology_schema", None)),
                "evaluation_principles": [
                    _component(p) for p in (getattr(seed, "evaluation_principles", ()) or ())
                ],
                "exit_conditions": [
                    _component(c) for c in (getattr(seed, "exit_conditions", ()) or ())
                ],
                # Plugin-owned structured handoff data (Seed extra fields):
                # validated JSON/YAML-serializable at Seed construction.
                "plugin_extra": dict(getattr(seed, "model_extra", None) or {}),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            # Real Seed components are JSON-native after ``model_dump``;
            # ``default=str`` only guards non-Seed test doubles from
            # turning a fingerprint computation into a crash.
            default=str,
        ).encode("utf-8")
        return f"v2:{hashlib.sha256(payload).hexdigest()}"

    @staticmethod
    def _seed_semantic_fingerprint_v1(seed: Any) -> str:
        """The round-15 ``v1`` fingerprint scheme (goal + per-AC keys).

        Kept ONLY to verify checkpoints saved before the round-16 #2
        widening: a ``v1:``-prefixed saved fingerprint is compared against
        this same-scheme recomputation (strictly stronger than the
        absent-fingerprint adopt posture), while every new save writes
        ``v2``. One-time migration, mirroring the convention every other
        restored field follows.
        """
        criteria = getattr(seed, "acceptance_criteria", ()) or ()
        payload = json.dumps(
            {
                "goal": str(getattr(seed, "goal", "")),
                "acceptance_criteria": [derive_semantic_ac_key(c) for c in criteria],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return f"v1:{hashlib.sha256(payload).hexdigest()}"

    def _checkpoint_seed_content_mismatch(self, cp: Any, seed: Any) -> str | None:
        """Return a mismatch description when the checkpoint's progress does
        not describe THIS Seed's content, else ``None`` (round-15 finding #1,
        BLOCKING).

        Adoption used to validate nothing but the ``seed_id`` key itself: a
        Seed whose goal AND acceptance criteria were changed under the same
        ``seed_id`` adopted the old content's ``completed_levels``/
        ``ac_statuses`` wholesale — recovery dispatched NOTHING and reported
        SUCCESS while describing the NEW content as "restored", attributing
        progress to AC content that never executed (a silent false success,
        the forbidden direction).

        Postures, following the established conventions:
        - Absent fingerprint (legacy checkpoint): adopt — the one-time
          migration posture every other restored field follows.
        - Present but malformed: handled by the preceding corruption gate,
          which preserves the checkpoint and blocks the launch.
        - Present and mismatched: refuse adoption (the round-10 staleness
          treatment — discard, run fresh, loud operator warning).
        - Round-16 finding #2: a ``v1:`` fingerprint (saved before the
          scheme widened to the full semantic surface) is verified against
          the same ``v1`` recomputation — a one-time migration that is
          strictly stronger than the absent-fingerprint adopt posture.
        """
        saved = cp.state.get("seed_fingerprint")
        if saved is None:
            log.info(
                "parallel_executor.recovery.seed_fingerprint_migrated",
                detail=(
                    "checkpoint predates the seed content fingerprint; "
                    "keeping the pre-fix adopt posture for this one-time "
                    "migration"
                ),
            )
            return None
        if not isinstance(saved, str) or not saved.startswith(("v1:", "v2:")):
            return (
                f"checkpoint carries a malformed seed content fingerprint "
                f"({saved!r}); it cannot be verified against the current "
                "Seed, and adopting unverifiable progress risks a silent "
                "false success"
            )
        current = (
            self._seed_semantic_fingerprint_v1(seed)
            if saved.startswith("v1:")
            else self._seed_semantic_fingerprint(seed)
        )
        if saved != current:
            return (
                "checkpoint was saved for a Seed whose goal/acceptance-"
                "criteria content differs from the currently-supplied Seed "
                "(same seed_id, different semantic content) — its recorded "
                "progress describes work that is NOT this Seed's work"
            )
        return None

    @staticmethod
    def _checkpoint_seed_fingerprint_malformed(cp: Any) -> str | None:
        """Reject a present Seed identity that cannot be verified."""
        state = cp.state
        if not isinstance(state, Mapping):
            return f"checkpoint state is not a mapping: {type(state).__name__}"
        if "seed_fingerprint" not in state:
            return None
        saved = state.get("seed_fingerprint")
        if not isinstance(saved, str) or not saved.startswith(("v1:", "v2:")):
            return f"seed_fingerprint is malformed: {saved!r}"
        return None

    # Every AC status value the executor ever records into a checkpoint's
    # ``ac_statuses`` mapping. Anything else is corruption, not progress.
    _CHECKPOINT_AC_STATUS_VALUES: ClassVar[frozenset[str]] = frozenset(
        {"pending", "executing", "completed", "failed", "skipped"}
    )

    @staticmethod
    def _checkpoint_index_value(value: object) -> int | None:
        """Parse a checkpointed AC index (int, or the str a JSON round-trip
        of a dict key produces); ``None`` when it is not an integer."""
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            try:
                return int(value)
            except ValueError:
                return None
        return None

    @classmethod
    def _checkpoint_level_contexts_malformed(
        cls,
        raw_contexts: object,
        *,
        total_acs: int | None,
        plan_total_stages: int | None,
    ) -> str | None:
        """Validate the nested dataclass tree before deserialization."""
        if not isinstance(raw_contexts, list | tuple):
            return f"level_contexts is not a list: {raw_contexts!r}"
        seen_levels: set[int] = set()
        seen_ac_indices: set[int] = set()
        required_ac_fields = {
            "ac_index",
            "ac_content",
            "success",
            "tools_used",
            "files_modified",
            "key_output",
            "public_api",
        }
        for context_position, raw_context in enumerate(raw_contexts):
            if not isinstance(raw_context, Mapping):
                return f"level_contexts[{context_position}] is not a mapping"
            level_number = raw_context.get("level_number")
            completed_acs = raw_context.get("completed_acs")
            if (
                isinstance(level_number, bool)
                or not isinstance(level_number, int)
                or level_number < 0
                or level_number in seen_levels
                or (plan_total_stages is not None and level_number > plan_total_stages)
                or not isinstance(completed_acs, list | tuple)
            ):
                return f"level_contexts[{context_position}] contains invalid level values"
            seen_levels.add(level_number)

            for ac_position, raw_ac in enumerate(completed_acs):
                if not isinstance(raw_ac, Mapping) or not required_ac_fields.issubset(raw_ac):
                    return (
                        f"level_contexts[{context_position}].completed_acs[{ac_position}] "
                        "has an invalid shape"
                    )
                ac_index = raw_ac.get("ac_index")
                tools_used = raw_ac.get("tools_used")
                files_modified = raw_ac.get("files_modified")
                if (
                    isinstance(ac_index, bool)
                    or not isinstance(ac_index, int)
                    or ac_index < 0
                    or (total_acs is not None and ac_index >= total_acs)
                    or ac_index in seen_ac_indices
                    or not isinstance(raw_ac.get("ac_content"), str)
                    or raw_ac.get("success") is not True
                    or not isinstance(tools_used, list | tuple)
                    or any(not isinstance(tool, str) for tool in tools_used)
                    or not isinstance(files_modified, list | tuple)
                    or any(not isinstance(path, str) for path in files_modified)
                    or not isinstance(raw_ac.get("key_output"), str)
                    or not isinstance(raw_ac.get("public_api"), str)
                ):
                    return (
                        f"level_contexts[{context_position}].completed_acs[{ac_position}] "
                        "contains invalid values"
                    )
                seen_ac_indices.add(ac_index)

            raw_review = raw_context.get("coordinator_review")
            if raw_review is None:
                continue
            if not isinstance(raw_review, Mapping):
                return f"level_contexts[{context_position}].coordinator_review is not a mapping"
            review_level = raw_review.get("level_number")
            conflicts = raw_review.get("conflicts_detected")
            fixes = raw_review.get("fixes_applied")
            warnings = raw_review.get("warnings_for_next_level")
            duration = raw_review.get("duration_seconds")
            session_id = raw_review.get("session_id")
            if (
                isinstance(review_level, bool)
                or not isinstance(review_level, int)
                or review_level < 0
                or not isinstance(conflicts, list | tuple)
                or not isinstance(raw_review.get("review_summary"), str)
                or not isinstance(fixes, list | tuple)
                or any(not isinstance(item, str) for item in fixes)
                or not isinstance(warnings, list | tuple)
                or any(not isinstance(item, str) for item in warnings)
                or not isinstance(duration, int | float)
                or isinstance(duration, bool)
                or not math.isfinite(duration)
                or duration < 0
                or (session_id is not None and not isinstance(session_id, str))
            ):
                return f"level_contexts[{context_position}].coordinator_review has invalid values"
            for conflict_position, raw_conflict in enumerate(conflicts):
                if not isinstance(raw_conflict, Mapping):
                    return (
                        f"level_contexts[{context_position}].coordinator_review."
                        f"conflicts_detected[{conflict_position}] is not a mapping"
                    )
                conflict_indices = raw_conflict.get("ac_indices")
                if (
                    not isinstance(raw_conflict.get("file_path"), str)
                    or not isinstance(conflict_indices, list | tuple)
                    or any(
                        isinstance(index, bool)
                        or not isinstance(index, int)
                        or index < 0
                        or (total_acs is not None and index >= total_acs)
                        for index in conflict_indices
                    )
                    or not isinstance(raw_conflict.get("resolved"), bool)
                    or not isinstance(raw_conflict.get("resolution_description"), str)
                ):
                    return (
                        f"level_contexts[{context_position}].coordinator_review."
                        f"conflicts_detected[{conflict_position}] has invalid values"
                    )
        return None

    @classmethod
    def _checkpoint_progress_malformed(
        cls,
        cp: Any,
        *,
        total_acs: int | None = None,
    ) -> str | None:
        """Return a description of the FIRST malformed progress field in the
        checkpoint, else ``None`` (round-16 finding #3, BLOCKING).

        Recovery used to read fields out of ``cp.state`` and apply them to
        local execution state INCREMENTALLY — ``execution_id`` reassigned
        first, then ``ac_statuses``/``failed_indices`` entries converted
        one at a time. A malformed LATER field (a type-mismatched
        ``completed_levels``, a non-integer index key, ...) raised after
        the EARLIER mutations had already happened, leaving recovery
        partially applied when the generic "recovery failed, run fresh"
        handler took over: the same torn-state class the round-16 #2
        save-side snapshot fix closed, on the read/apply side.

        This validates EVERY progress field the adoption branch consumes,
        atomically and BEFORE any of it is applied. Any failure means the
        checkpoint as a whole is corrupt and takes the established
        fail-closed malformed-checkpoint path (discard as stale, run every
        level fresh, loud operator warning) — never a crash, never a
        partial application. Keys ABSENT from the state keep the adoption
        branch's defaults (the legacy/forward-compat migration shape, the
        same convention ``_restore_checkpoint_execution_semantics``
        documents); only a PRESENT-but-malformed value fails validation.
        """
        state = cp.state
        if not isinstance(state, Mapping):
            return f"checkpoint state is not a mapping: {type(state).__name__}"
        if "execution_id" in state:
            saved_execution_id = state["execution_id"]
            if saved_execution_id is not None and not (
                isinstance(saved_execution_id, str) and saved_execution_id
            ):
                return f"execution_id is not a non-empty string: {saved_execution_id!r}"
        completed_levels: int | None = None
        if "completed_levels" in state:
            completed_levels = state["completed_levels"]
            if (
                not isinstance(completed_levels, int)
                or isinstance(completed_levels, bool)
                or completed_levels < 0
            ):
                return f"completed_levels is not a non-negative integer: {completed_levels!r}"
        plan_total_stages: int | None = None
        if "plan_total_stages" in state:
            plan_total_stages = state["plan_total_stages"]
            if (
                not isinstance(plan_total_stages, int)
                or isinstance(plan_total_stages, bool)
                or plan_total_stages < 0
            ):
                return f"plan_total_stages is not a non-negative integer: {plan_total_stages!r}"
        normalized_statuses: dict[int, str] | None = None
        if "ac_statuses" in state:
            raw_statuses = state["ac_statuses"]
            if not isinstance(raw_statuses, Mapping):
                return f"ac_statuses is not a mapping: {raw_statuses!r}"
            normalized_statuses = {}
            for key, status in raw_statuses.items():
                normalized_index = cls._checkpoint_index_value(key)
                if normalized_index is None:
                    return f"ac_statuses key is not an integer AC index: {key!r}"
                if normalized_index < 0 or (
                    total_acs is not None and normalized_index >= total_acs
                ):
                    return f"ac_statuses key is outside the Seed AC range: {key!r}"
                if normalized_index in normalized_statuses:
                    return f"ac_statuses contains duplicate normalized AC index: {key!r}"
                if status not in cls._CHECKPOINT_AC_STATUS_VALUES:
                    return f"ac_statuses[{key!r}] is not a known AC status: {status!r}"
                normalized_statuses[normalized_index] = status
            if total_acs is not None and set(normalized_statuses) != set(range(total_acs)):
                return "ac_statuses does not cover every Seed AC exactly once"
        normalized_failed: set[int] | None = None
        if "failed_indices" in state:
            raw_failed = state["failed_indices"]
            if not isinstance(raw_failed, list | tuple):
                return f"failed_indices is not a list: {raw_failed!r}"
            normalized_failed = set()
            for entry in raw_failed:
                normalized_index = cls._checkpoint_index_value(entry)
                if normalized_index is None:
                    return f"failed_indices entry is not an integer AC index: {entry!r}"
                if normalized_index < 0 or (
                    total_acs is not None and normalized_index >= total_acs
                ):
                    return f"failed_indices entry is outside the Seed AC range: {entry!r}"
                if normalized_index in normalized_failed:
                    return f"failed_indices contains duplicate AC index: {entry!r}"
                normalized_failed.add(normalized_index)
        normalized_external_completed: set[int] | None = None
        if "satisfied_externally_indices" in state:
            raw_external_completed = state["satisfied_externally_indices"]
            if not isinstance(raw_external_completed, list | tuple):
                return f"satisfied_externally_indices is not a list: {raw_external_completed!r}"
            normalized_external_completed = set()
            for entry in raw_external_completed:
                normalized_index = cls._checkpoint_index_value(entry)
                if normalized_index is None:
                    return (
                        f"satisfied_externally_indices entry is not an integer AC index: {entry!r}"
                    )
                if normalized_index < 0 or (
                    total_acs is not None and normalized_index >= total_acs
                ):
                    return (
                        "satisfied_externally_indices entry is outside the Seed AC range: "
                        f"{entry!r}"
                    )
                if normalized_index in normalized_external_completed:
                    return "satisfied_externally_indices contains duplicate AC index"
                normalized_external_completed.add(normalized_index)
        elif state.get("checkpoint_contract_version") == _EXECUTION_CHECKPOINT_CONTRACT_VERSION:
            return "satisfied_externally_indices is missing"
        completed_count: int | None = None
        if "completed_count" in state:
            completed_count = state["completed_count"]
            if (
                not isinstance(completed_count, int)
                or isinstance(completed_count, bool)
                or completed_count < 0
            ):
                return f"completed_count is not a non-negative integer: {completed_count!r}"
            if total_acs is not None and completed_count > total_acs:
                return f"completed_count exceeds the Seed AC count: {completed_count!r}"

        raw_plan = state.get("execution_plan")
        raw_stages = raw_plan.get("stages") if isinstance(raw_plan, Mapping) else None
        if plan_total_stages is not None and isinstance(raw_stages, list):
            if plan_total_stages != len(raw_stages):
                return "plan_total_stages does not match execution_plan.stages"
        if (
            completed_levels is not None
            and plan_total_stages is not None
            and completed_levels > plan_total_stages
        ):
            return "completed_levels exceeds plan_total_stages"
        if normalized_statuses is not None and normalized_failed is not None:
            failed_status_indices = {
                index for index, status in normalized_statuses.items() if status == "failed"
            }
            if normalized_failed != failed_status_indices:
                return "failed_indices does not match failed ac_statuses"
        if normalized_statuses is not None and completed_count is not None:
            completed_status_count = sum(
                status == "completed" for status in normalized_statuses.values()
            )
            if completed_count != completed_status_count:
                return "completed_count does not match completed ac_statuses"
        if "level_contexts" in state:
            contexts_malformed = cls._checkpoint_level_contexts_malformed(
                state["level_contexts"],
                total_acs=total_acs,
                plan_total_stages=plan_total_stages,
            )
            if contexts_malformed is not None:
                return contexts_malformed
            if normalized_statuses is not None:
                raw_dispatch = state.get("dispatch_contract")
                external_indices: set[int] = set()
                reconciled_indices: set[int] = set()
                if isinstance(raw_dispatch, Mapping):
                    raw_external = raw_dispatch.get("externally_satisfied_ac_indices", [])
                    if isinstance(raw_external, list | tuple):
                        external_indices = {
                            ac_index
                            for ac_index in raw_external
                            if isinstance(ac_index, int) and not isinstance(ac_index, bool)
                        }
                    raw_reconciled = raw_dispatch.get("reconciled_level_contexts", [])
                    if isinstance(raw_reconciled, list | tuple):
                        reconciled_indices = {
                            raw_ac["ac_index"]
                            for raw_context in raw_reconciled
                            if isinstance(raw_context, Mapping)
                            for raw_ac in raw_context.get("completed_acs", [])
                            if isinstance(raw_ac, Mapping)
                            and isinstance(raw_ac.get("ac_index"), int)
                            and not isinstance(raw_ac.get("ac_index"), bool)
                        }
                if total_acs is not None and any(
                    ac_index >= total_acs for ac_index in external_indices | reconciled_indices
                ):
                    return "dispatch context provenance is outside the Seed AC range"
                context_level_by_ac = {
                    raw_ac["ac_index"]: raw_context["level_number"]
                    for raw_context in state["level_contexts"]
                    for raw_ac in raw_context["completed_acs"]
                }
                completed_status_indices = {
                    index for index, status in normalized_statuses.items() if status == "completed"
                }
                satisfied_externally = normalized_external_completed or set()
                if not satisfied_externally <= completed_status_indices:
                    return "satisfied_externally_indices does not match completed ac_statuses"
                if not satisfied_externally <= external_indices:
                    return "satisfied_externally_indices is not covered by the dispatch skip set"
                context_indices = set(context_level_by_ac)
                missing_context = completed_status_indices - context_indices - satisfied_externally
                unexpected_context = context_indices - completed_status_indices - reconciled_indices
                if missing_context or unexpected_context:
                    return "level_contexts ACs do not match completed/external/reconciled progress"

                if isinstance(raw_stages, list):
                    expected_level_by_ac: dict[int, int] = {}
                    for stage_position, raw_stage in enumerate(raw_stages):
                        if not isinstance(raw_stage, Mapping):
                            continue
                        raw_ac_indices = raw_stage.get("ac_indices")
                        if not isinstance(raw_ac_indices, list):
                            continue
                        for raw_ac_index in raw_ac_indices:
                            if isinstance(raw_ac_index, int) and not isinstance(raw_ac_index, bool):
                                expected_level_by_ac[raw_ac_index] = stage_position + 1
                    for completed_ac_index, context_level in context_level_by_ac.items():
                        if completed_ac_index in reconciled_indices:
                            continue
                        expected_level = expected_level_by_ac.get(completed_ac_index)
                        if expected_level is not None and context_level != expected_level:
                            return "level_contexts AC is attached to the wrong execution plan stage"
        return None

    @classmethod
    def _checkpoint_semantics_malformed(cls, cp: Any) -> str | None:
        """Return a description of the FIRST malformed execution-semantic
        payload in the checkpoint, else ``None`` (round-17 finding #3,
        BLOCKING).

        The three semantic restore helpers
        (``_restore_checkpoint_retry_policy``,
        ``_restore_checkpoint_model_router``,
        ``_restore_checkpoint_execution_semantics``) each validate their
        OWN payload atomically, but their malformed branches used to log,
        force the escalation gates open, and silently return — without
        ever signalling the RC3 recovery caller. The caller then marked
        the checkpoint ADOPTED with a cross-group MIX of semantics: the
        groups that validated ran under the ORIGINAL run's settings while
        the malformed group silently ran under the CURRENT process's —
        the same torn-recovery class round-16 #3 closed for the progress
        fields, one level up.

        A type-mangled semantic payload came from the same writer/store
        as every other field, so it means the checkpoint as a whole
        cannot be trusted. Validate every semantic group atomically and
        BEFORE any restoration mutates executor state, then preserve the
        checkpoint and block the launch for operator repair — never adopt a
        partially-trustworthy mixture or run fresh over unknown side effects.
        ``None``/absent payloads are the legacy migration shape, not
        corruption (identical to ``_checkpoint_progress_malformed``'s
        convention).
        """
        state = cp.state
        if not isinstance(state, Mapping):
            return f"checkpoint state is not a mapping: {type(state).__name__}"
        required_groups = {
            "retry_policy",
            "model_routing",
            "execution_semantics",
            "execution_profile",
            "prompt_guidance",
        }
        missing_groups = sorted(required_groups - set(state))
        if missing_groups:
            return "checkpoint semantic groups are missing: " + ", ".join(missing_groups)
        raw_retry_policy = state.get("retry_policy")
        raw_execution_semantics = state.get("execution_semantics")
        for reason in (
            cls._retry_policy_malformed(raw_retry_policy),
            cls._model_routing_malformed(state.get("model_routing")),
            cls._execution_semantics_malformed(raw_execution_semantics),
            cls._execution_profile_malformed(state.get("execution_profile")),
        ):
            if reason is not None:
                return reason
        if isinstance(raw_retry_policy, Mapping) and set(raw_retry_policy) != set(
            cls._CHECKPOINT_V2_RETRY_POLICY_FIELDS
        ):
            missing = sorted(cls._CHECKPOINT_V2_RETRY_POLICY_FIELDS - set(raw_retry_policy))
            unknown = sorted(set(raw_retry_policy) - cls._CHECKPOINT_V2_RETRY_POLICY_FIELDS)
            return (
                "checkpoint v2 retry_policy field set is incomplete or unknown: "
                f"missing={missing}, unknown={unknown}"
            )
        if isinstance(raw_execution_semantics, Mapping) and set(raw_execution_semantics) != set(
            cls._CHECKPOINT_V2_EXECUTION_SEMANTICS_FIELDS
        ):
            missing = sorted(
                cls._CHECKPOINT_V2_EXECUTION_SEMANTICS_FIELDS - set(raw_execution_semantics)
            )
            unknown = sorted(
                set(raw_execution_semantics) - cls._CHECKPOINT_V2_EXECUTION_SEMANTICS_FIELDS
            )
            return (
                "checkpoint v2 execution_semantics field set is incomplete or unknown: "
                f"missing={missing}, unknown={unknown}"
            )
        return None

    @staticmethod
    def _checkpoint_contract_version_malformed(cp: Any) -> str | None:
        state = cp.state
        if not isinstance(state, Mapping):
            return f"checkpoint state is not a mapping: {type(state).__name__}"
        version = state.get("checkpoint_contract_version")
        if version != _EXECUTION_CHECKPOINT_CONTRACT_VERSION or isinstance(version, bool):
            return (
                "checkpoint_contract_version is missing or unsupported: "
                f"{version!r} (expected {_EXECUTION_CHECKPOINT_CONTRACT_VERSION})"
            )
        return None

    def _durable_ac_context_summary(
        self,
        result: ACExecutionResult,
    ) -> dict[str, Any] | None:
        """Build a bounded downstream handoff for a finalized success."""
        if not result.success:
            return None
        workspace_root = self._task_cwd or self._adapter.working_directory or os.getcwd()
        try:
            level_context = extract_level_context(
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
        except Exception as exc:
            # The finalized outcome itself remains correctness-bearing even
            # when an unusual runtime message cannot be summarized. Persist
            # an explicit context omission; recovery will refuse to cross it
            # when unfinished downstream stages require the handoff.
            log.warning(
                "parallel_executor.ac.context_summary_unavailable",
                ac_index=result.ac_index,
                error=str(exc),
            )
            return None
        if not level_context.completed_acs:
            return None
        summary = level_context.completed_acs[0]

        def _bounded_items(values: tuple[str, ...], limit: int) -> list[str]:
            return [value[:_DURABLE_CONTEXT_MAX_ITEM_CHARS] for value in values[:limit]]

        return {
            "version": 1,
            "ac_index": summary.ac_index,
            "ac_content": summary.ac_content[:_DURABLE_CONTEXT_MAX_AC_CONTENT_CHARS],
            "success": summary.success,
            "tools_used": _bounded_items(summary.tools_used, _DURABLE_CONTEXT_MAX_TOOL_NAMES),
            "files_modified": _bounded_items(
                summary.files_modified,
                _DURABLE_CONTEXT_MAX_FILE_PATHS,
            ),
            "key_output": summary.key_output,
            "public_api": summary.public_api,
        }

    @staticmethod
    def _deserialize_durable_ac_context_summary(
        raw_summary: object,
        *,
        expected_ac_index: int,
    ) -> tuple[bool, ACContextSummary | None]:
        """Return ``(valid, summary)``; ``None`` is a valid legacy omission."""
        if raw_summary is None:
            return True, None
        if not isinstance(raw_summary, Mapping) or set(raw_summary) != {
            "version",
            "ac_index",
            "ac_content",
            "success",
            "tools_used",
            "files_modified",
            "key_output",
            "public_api",
        }:
            return False, None
        ac_index = raw_summary.get("ac_index")
        ac_content = raw_summary.get("ac_content")
        tools_used = raw_summary.get("tools_used")
        files_modified = raw_summary.get("files_modified")
        key_output = raw_summary.get("key_output")
        public_api = raw_summary.get("public_api")
        if (
            raw_summary.get("version") != 1
            or ac_index != expected_ac_index
            or isinstance(ac_index, bool)
            or not isinstance(ac_content, str)
            or len(ac_content) > _DURABLE_CONTEXT_MAX_AC_CONTENT_CHARS
            or raw_summary.get("success") is not True
            or not isinstance(tools_used, list | tuple)
            or len(tools_used) > _DURABLE_CONTEXT_MAX_TOOL_NAMES
            or any(
                not isinstance(tool, str) or len(tool) > _DURABLE_CONTEXT_MAX_ITEM_CHARS
                for tool in tools_used
            )
            or not isinstance(files_modified, list | tuple)
            or len(files_modified) > _DURABLE_CONTEXT_MAX_FILE_PATHS
            or any(
                not isinstance(path, str) or len(path) > _DURABLE_CONTEXT_MAX_ITEM_CHARS
                for path in files_modified
            )
            or not isinstance(key_output, str)
            or len(key_output) > 200
            or not isinstance(public_api, str)
            or len(public_api) > 500
        ):
            return False, None
        return (
            True,
            ACContextSummary(
                ac_index=ac_index,
                ac_content=ac_content,
                success=True,
                tools_used=tuple(tools_used),
                files_modified=tuple(files_modified),
                key_output=key_output,
                public_api=public_api,
            ),
        )

    @staticmethod
    def _merge_recovered_success_contexts(
        level_contexts: list[LevelContext],
        execution_plan: StagedExecutionPlan,
        recovered_contexts: Mapping[int, ACContextSummary],
    ) -> list[LevelContext]:
        """Place event-recovered summaries back into their original stages."""
        if not recovered_contexts:
            return level_contexts
        by_level = {context.level_number: context for context in level_contexts}
        for stage_position, stage in enumerate(execution_plan.stages):
            summaries = [
                recovered_contexts[ac_index]
                for ac_index in stage.ac_indices
                if ac_index in recovered_contexts
            ]
            if not summaries:
                continue
            level_number = stage_position + 1
            existing = by_level.get(level_number)
            if existing is None:
                by_level[level_number] = LevelContext(
                    level_number=level_number,
                    completed_acs=tuple(summaries),
                )
                continue
            existing_indices = {summary.ac_index for summary in existing.completed_acs}
            by_level[level_number] = replace(
                existing,
                completed_acs=(
                    *existing.completed_acs,
                    *(summary for summary in summaries if summary.ac_index not in existing_indices),
                ),
            )
        return [by_level[level_number] for level_number in sorted(by_level)]

    def _merge_level_context(
        self,
        level_contexts: list[LevelContext],
        incoming: LevelContext,
    ) -> list[LevelContext]:
        """Merge a new stage handoff with an existing reconciled stage context."""
        previous_digest = self._level_context_chain_digest(level_contexts)
        by_level = {context.level_number: context for context in level_contexts}
        existing = by_level.get(incoming.level_number)
        if existing is None:
            by_level[incoming.level_number] = incoming
        else:
            summaries = {summary.ac_index: summary for summary in existing.completed_acs}
            summaries.update({summary.ac_index: summary for summary in incoming.completed_acs})
            by_level[incoming.level_number] = LevelContext(
                level_number=incoming.level_number,
                completed_acs=tuple(summaries[index] for index in sorted(summaries)),
                coordinator_review=(incoming.coordinator_review or existing.coordinator_review),
            )
        merged = [by_level[level_number] for level_number in sorted(by_level)]
        if existing is None and (
            not level_contexts or incoming.level_number > level_contexts[-1].level_number
        ):
            digest = self._prompt_identity(
                f"{previous_digest}\n{self._level_context_item_digest(incoming)}"
            )
            self._capsule_level_context_cache[id(merged)] = (merged, digest)
        else:
            self._level_context_chain_digest(merged)
        return merged

    @staticmethod
    def _recovery_exhausted_payload_malformed(
        data: Mapping[str, Any],
        *,
        execution_id: str,
        configured_retry_attempts: int,
    ) -> str | None:
        """Validate the complete closure contract emitted by this executor."""
        from ouroboros.orchestrator.failure_taxonomy import FailureClass

        expected_fields = {
            "schema_version",
            "execution_id",
            "session_id",
            "root_ac_index",
            "semantic_ac_key",
            "retry_attempt",
            "configured_retry_attempts",
            "retry_termination_reason",
            "alternate_redispatch_status",
            "last_failure_class",
            "success",
        }
        if set(data) != expected_fields:
            return "recovery_exhausted has an invalid shape"
        if data.get("schema_version") != 1 or isinstance(data.get("schema_version"), bool):
            return "recovery_exhausted has an unsupported schema_version"
        if data.get("execution_id") != execution_id:
            return "recovery_exhausted execution_id does not match the replay aggregate"
        session_id = data.get("session_id")
        semantic_ac_key = data.get("semantic_ac_key")
        if not isinstance(session_id, str) or not session_id:
            return "recovery_exhausted session_id is invalid"
        if not isinstance(semantic_ac_key, str) or not semantic_ac_key:
            return "recovery_exhausted semantic_ac_key is invalid"
        persisted_retry_attempts = data.get("configured_retry_attempts")
        retry_attempt = data.get("retry_attempt")
        if (
            not isinstance(persisted_retry_attempts, int)
            or isinstance(persisted_retry_attempts, bool)
            or persisted_retry_attempts < 0
            or persisted_retry_attempts != configured_retry_attempts
        ):
            return "recovery_exhausted configured retry policy does not match"
        if (
            not isinstance(retry_attempt, int)
            or isinstance(retry_attempt, bool)
            or retry_attempt < 0
        ):
            return "recovery_exhausted retry_attempt is invalid"
        termination_reason = data.get("retry_termination_reason")
        alternate_status = data.get("alternate_redispatch_status")
        if termination_reason not in {
            "alternate_harness_exhausted",
            "budget_exhausted",
            "infra_fatal",
            "not_retryable",
            "repeated_failure_early_stop",
        }:
            return "recovery_exhausted termination reason is invalid"
        if alternate_status not in {"failed", "not_attempted", "not_eligible"}:
            return "recovery_exhausted alternate redispatch status is invalid"
        if (alternate_status == "failed") != (termination_reason == "alternate_harness_exhausted"):
            return "recovery_exhausted alternate status contradicts its termination reason"
        if termination_reason == "budget_exhausted" and retry_attempt < persisted_retry_attempts:
            return "recovery_exhausted budget closure precedes the configured retry cap"
        failure_class = data.get("last_failure_class")
        if failure_class not in {member.value for member in FailureClass} | {"unknown"}:
            return "recovery_exhausted failure class is invalid"
        if data.get("success") is not False:
            return "recovery_exhausted must record success=false"
        return None

    async def _reconstruct_finalized_outcomes(
        self,
        *,
        execution_id: str,
        total_acs: int,
        expected_semantic_ac_keys: Mapping[int, str],
    ) -> dict[int, _RecoveredFinalizedOutcome] | None:
        """Replay the latest post-verify outcome for every root AC.

        Attempt ordinals alone are insufficient: a success durably finalized
        after the last level checkpoint is authoritative completion and must
        be restored, not redispatched.  A failure with a matching
        ``recovery_exhausted`` marker is likewise terminal.  A finalized
        failure at the configured ordinary retry cap is reconstructed as a
        post-dispatch result and handed directly to escalation/finalization;
        recovery never repeats that already-completed attempt.
        """
        events = await self._replay_with_retry("execution", execution_id)
        if events is None:
            return None
        if set(expected_semantic_ac_keys) != set(range(total_acs)) or any(
            not isinstance(key, str) or not key for key in expected_semantic_ac_keys.values()
        ):
            return None

        latest: dict[int, _RecoveredFinalizedOutcome] = {}
        finalized_attempts: dict[tuple[int, int], _RecoveredFinalizedOutcome] = {}
        exhausted_attempts: dict[tuple[int, int], dict[str, Any]] = {}
        for event in events:
            event_type = getattr(event, "type", None)
            if event_type not in {
                "execution.ac.outcome_finalized",
                "execution.ac.recovery_exhausted",
            }:
                continue
            data = getattr(event, "data", None)
            if not isinstance(data, dict):
                return None
            ac_idx = data.get("root_ac_index")
            attempt = data.get("retry_attempt")
            if (
                not isinstance(ac_idx, int)
                or isinstance(ac_idx, bool)
                or not 0 <= ac_idx < total_acs
                or not isinstance(attempt, int)
                or isinstance(attempt, bool)
                or attempt < 0
            ):
                return None
            if event_type == "execution.ac.recovery_exhausted":
                if (
                    self._recovery_exhausted_payload_malformed(
                        data,
                        execution_id=execution_id,
                        configured_retry_attempts=self._ac_retry_attempts,
                    )
                    is not None
                    or data.get("semantic_ac_key") != expected_semantic_ac_keys[ac_idx]
                ):
                    return None
                attempt_key = (ac_idx, attempt)
                previous_closure = exhausted_attempts.get(attempt_key)
                if previous_closure is not None and previous_closure != data:
                    return None
                exhausted_attempts[attempt_key] = dict(data)
                continue
            success = data.get("success")
            raw_outcome = data.get("outcome")
            is_decomposed = data.get("is_decomposed", False)
            forced_frontier_routing = data.get("forced_frontier_routing", False)
            event_execution_id = data.get("execution_id")
            event_session_id = data.get("session_id")
            event_ac_index = data.get("ac_index")
            if (
                not isinstance(success, bool)
                or not isinstance(is_decomposed, bool)
                or not isinstance(forced_frontier_routing, bool)
                or event_execution_id != execution_id
                or not isinstance(event_session_id, str)
                or not event_session_id
                or event_ac_index != ac_idx
                or isinstance(event_ac_index, bool)
            ):
                return None
            try:
                outcome = (
                    ACExecutionOutcome(raw_outcome)
                    if isinstance(raw_outcome, str)
                    else ACExecutionOutcome.SUCCEEDED
                    if raw_outcome is None and success
                    else ACExecutionOutcome.FAILED
                    if raw_outcome is None
                    else None
                )
            except ValueError:
                return None
            expected_outcome = (
                ACExecutionOutcome.SUCCEEDED if success else ACExecutionOutcome.FAILED
            )
            if outcome is not expected_outcome:
                return None
            if not success and data.get("context_summary") is not None:
                return None
            context_valid, context_summary = self._deserialize_durable_ac_context_summary(
                data.get("context_summary"),
                expected_ac_index=ac_idx,
            )
            if not context_valid:
                return None
            candidate = _RecoveredFinalizedOutcome(
                retry_attempt=attempt,
                success=success,
                outcome=outcome,
                is_decomposed=is_decomposed,
                forced_frontier_routing=forced_frontier_routing,
                context_summary=context_summary,
            )
            attempt_key = (ac_idx, attempt)
            previous_attempt = finalized_attempts.get(attempt_key)
            if previous_attempt is not None:
                # Durable retries and mixed-version writers can append the same
                # attempt more than once. Core outcome fields must agree; no log
                # ordering can make a success and failure for one attempt both
                # authoritative. Context is backward-compatible: a legacy marker
                # may omit it, but the latest compatible marker remains
                # authoritative so a newer context-less writer cannot silently
                # inherit evidence it did not persist.
                if (
                    candidate.success,
                    candidate.outcome,
                    candidate.is_decomposed,
                    candidate.forced_frontier_routing,
                ) != (
                    previous_attempt.success,
                    previous_attempt.outcome,
                    previous_attempt.is_decomposed,
                    previous_attempt.forced_frontier_routing,
                ):
                    return None
                if (
                    candidate.context_summary is not None
                    and previous_attempt.context_summary is not None
                    and candidate.context_summary != previous_attempt.context_summary
                ):
                    return None
            finalized_attempts[attempt_key] = candidate

            previous = latest.get(ac_idx)
            if previous is None or attempt >= previous.retry_attempt:
                latest[ac_idx] = candidate

        closure_attempt_by_ac: dict[int, int] = {}
        for attempt_key in exhausted_attempts:
            ac_idx, attempt = attempt_key
            finalized = finalized_attempts.get(attempt_key)
            if finalized is None or finalized.success:
                return None
            previous_closure_attempt = closure_attempt_by_ac.get(ac_idx)
            if previous_closure_attempt is not None and previous_closure_attempt != attempt:
                return None
            closure_attempt_by_ac[ac_idx] = attempt

        for ac_idx, attempt in finalized_attempts:
            closure_attempt = closure_attempt_by_ac.get(ac_idx)
            if closure_attempt is not None and attempt > closure_attempt:
                return None

        successful_attempt_by_ac: dict[int, int] = {}
        for (ac_idx, attempt), finalized in finalized_attempts.items():
            if not finalized.success:
                continue
            previous_success_attempt = successful_attempt_by_ac.get(ac_idx)
            if previous_success_attempt is not None and previous_success_attempt != attempt:
                return None
            successful_attempt_by_ac[ac_idx] = attempt

        for ac_idx, attempt in finalized_attempts:
            successful_attempt = successful_attempt_by_ac.get(ac_idx)
            if successful_attempt is not None and attempt > successful_attempt:
                return None

        for ac_idx, recovered in tuple(latest.items()):
            if (ac_idx, recovered.retry_attempt) in exhausted_attempts:
                latest[ac_idx] = replace(recovered, recovery_exhausted=True)
        return latest

    def _discard_stale_checkpoint(
        self,
        seed_id: str,
        *,
        saved_execution_id: object,
        detail: str | None = None,
        console_message: str | None = None,
    ) -> None:
        """Drop a checkpoint this launch must not resume.

        Default messaging covers the round-10 case (a checkpoint left behind
        by an already-terminal run); the round-15 content-mismatch gate
        passes its own ``detail``/``console_message``. Best-effort: a failed
        delete only means the (already loud) gate fires again on the next
        run — never worth failing the current fresh run over.
        """
        log.warning(
            "parallel_executor.recovery.stale_checkpoint_discarded",
            detail=detail
            or (
                "checkpoint belongs to a run that already reached a "
                "non-resumable terminal state; running fresh instead of "
                "resuming it"
            ),
            seed_id=seed_id,
            checkpoint_execution_id=saved_execution_id,
        )
        self._console.print(
            console_message
            or (
                "[yellow]Found a checkpoint from an already-finished run of "
                "this seed — starting fresh (stale checkpoint discarded).[/yellow]"
            )
        )
        try:
            delete = getattr(self._checkpoint_store, "delete", None)
            delete_result = delete(seed_id) if callable(delete) else None
            if (
                delete_result is not None
                and hasattr(delete_result, "is_ok")
                and not delete_result.is_ok
            ):
                log.error(
                    "parallel_executor.recovery.stale_checkpoint_delete_failed",
                    seed_id=seed_id,
                    error=str(getattr(delete_result, "error", "unknown error")),
                )
        except Exception as e:
            log.error(
                "parallel_executor.recovery.stale_checkpoint_delete_failed",
                seed_id=seed_id,
                error=str(e),
            )

    async def _load_checkpoint_for_recovery(self, seed_id: str, *, max_retries: int = 3) -> Any:
        """Load the RC3 checkpoint, distinguishing "confirmed absent" from
        "could not read" (round-15 finding #2, BLOCKING — load direction).

        Returns the checkpoint on success, or ``None`` when the store
        CONFIRMS no checkpoint exists at any rollback level (the
        ``no_checkpoint_found`` marker on the store's load error). A
        degraded read — the store raising, or returning an error WITHOUT
        that confirmed-absent marker (corrupt-beyond-rollback, IO failure)
        — is retried on the same bounded backoff ``_replay_with_retry``
        uses, then raises :class:`CheckpointUnreadableError`: falling
        through to "no checkpoint, run fresh" is the fail-open direction
        (see the error class docstring for why running fresh over an
        unconfirmable checkpoint is never safe).
        """
        last_error = "unknown error"
        for attempt in range(max_retries):
            try:
                result = self._checkpoint_store.load(seed_id)
            except Exception as e:  # noqa: BLE001 - degraded store surfaces below.
                last_error = str(e)
            else:
                if hasattr(result, "is_ok") and result.is_ok:
                    return result.value if result.value else None
                error = getattr(result, "error", None)
                details = getattr(error, "details", None)
                if isinstance(details, Mapping) and details.get("no_checkpoint_found") is True:
                    # Every rollback level was CONFIRMED absent — an
                    # ordinary fresh launch, not a degraded read.
                    return None
                last_error = str(error) if error is not None else "unknown error"
            if attempt < max_retries - 1:
                wait = min(1.0 * (2**attempt), 5.0)
                log.warning(
                    "parallel_executor.recovery.checkpoint_load_retry",
                    seed_id=seed_id,
                    attempt=attempt + 1,
                    error=last_error,
                )
                await anyio.sleep(wait)
        log.error(
            "parallel_executor.recovery.checkpoint_load_failed",
            seed_id=seed_id,
            attempts=max_retries,
            error=last_error,
        )
        self._console.print(
            "[red]Refusing to start: the checkpoint store could not confirm "
            f"whether a prior run of seed '{seed_id}' left a resumable "
            f"checkpoint ({last_error}). Running fresh over an unconfirmed "
            "checkpoint could race a live run or orphan an interrupted "
            "run's durable state.[/red]"
        )
        raise CheckpointUnreadableError(
            f"Could not read the RC3 checkpoint for seed '{seed_id}' after "
            f"{max_retries} attempts: {last_error}. Refusing to launch — a "
            "checkpoint may exist but cannot be confirmed, and running "
            "fresh over it could race a still-live run or silently orphan "
            "an interrupted run's restorable identity/escalation state. "
            "Investigate the checkpoint store, then relaunch."
        )

    def _save_execution_checkpoint(
        self,
        *,
        seed: Seed,
        session_id: str,
        execution_id: str,
        completed_levels: int,
        plan_total_stages: int,
        execution_plan: StagedExecutionPlan,
        dispatch_contract: Mapping[str, Any],
        ac_statuses: dict[int, str],
        failed_indices: set[int],
        satisfied_externally_indices: set[int],
        completed_count: int,
        level_contexts: list[LevelContext],
        checkpoint_point: str,
    ) -> bool:
        """Persist the RC3 execution checkpoint in its one canonical shape.

        Called from two points with the SAME state layout: once at run
        start BEFORE the first AC dispatch (round-13 finding #1, BLOCKING —
        checkpoints used to exist only after a level completed, so a crash
        DURING the first level left ZERO durable record of the run's
        ``execution_id``/retry-policy/router/dispatch semantics; the
        restart minted a fresh execution_id, could not find the original
        run's durable ladder/escalation events, and silently started over,
        losing any escalation progress from that first level), and again
        after every level completion (the original RC3 cadence, which
        simply overwrites this same checkpoint with updated progress).

        The return value is authoritative at the call site.  The run-start
        anchor must succeed before any dispatch; per-level progress updates
        remain best-effort because finalized outcomes are independently
        reconciled from the durable event stream on recovery.
        """
        if not self._checkpoint_store:
            return True
        try:
            from ouroboros.persistence.checkpoint import CheckpointData

            seed_id = self._checkpoint_seed_id(seed, session_id)
            checkpoint = CheckpointData.create(
                seed_id=seed_id,
                phase="parallel_execution",
                state={
                    "checkpoint_contract_version": _EXECUTION_CHECKPOINT_CONTRACT_VERSION,
                    "session_id": session_id,
                    "execution_id": execution_id,
                    # Round-15 finding #1 (BLOCKING): what this progress is
                    # progress OF. ``seed_id`` is a random uuid, not a
                    # content identity — recovery validates this fingerprint
                    # against the CURRENTLY-supplied Seed before adopting
                    # any progress (see ``_checkpoint_seed_content_mismatch``).
                    "seed_fingerprint": self._seed_semantic_fingerprint(seed),
                    "completed_levels": completed_levels,
                    # Round-16 finding #1 (BLOCKING): ``completed_levels`` is
                    # relative to the plan THIS run derived — record that
                    # plan's stage count alongside it so recovery-time
                    # consumers (the round-13 full-completion tiebreaker)
                    # never interpret the count against a differently-grouped
                    # re-derived plan.
                    "plan_total_stages": plan_total_stages,
                    # Dependency analysis is re-run on every process start.
                    # Persist the exact validated graph/stage contract so a
                    # resume can reject drift before combining old outcomes
                    # with a materially different write ordering.
                    "execution_plan": self._serialize_execution_plan(execution_plan),
                    "dispatch_contract": dict(dispatch_contract),
                    "ac_statuses": {str(k): v for k, v in ac_statuses.items()},
                    "failed_indices": sorted(failed_indices),
                    "satisfied_externally_indices": sorted(satisfied_externally_indices),
                    "completed_count": completed_count,
                    "level_contexts": serialize_level_contexts(level_contexts),
                    "decomposition_decisions": {
                        node_id: record.to_dict()
                        for node_id, record in self._decomposition_decisions.items()
                    },
                    # Round-9 finding #2 (BLOCKING): the retry/
                    # termination policy this run STARTED with.
                    # A crash-restart constructs a fresh executor
                    # from whatever the config file says at
                    # restart time; the recovery block restores
                    # this persisted policy instead, so a
                    # parked/mid-ladder AC keeps the original
                    # run's escalation guarantee even when the
                    # current config disagrees (mirrors the
                    # runner's durable retry_policy contract for
                    # resume_session).
                    "retry_policy": {
                        "lateral_escalation_enabled": (self._lateral_escalation_enabled),
                        "parked_retry_backoff_seconds": (self._parked_retry_backoff_seconds),
                        "ac_retry_attempts": self._ac_retry_attempts,
                        # Round-11 finding #2 (BLOCKING): the
                        # round-9 #4 execution-semantic trio the
                        # runner-level durable contract already
                        # pins for session-resume must also ride
                        # the RC3 checkpoint, so a crash-restart
                        # recovering through THIS mechanism keeps
                        # the effort/verify-gate semantics the
                        # run started with (see
                        # ``_restore_checkpoint_retry_policy``).
                        "reasoning_effort": self._reasoning_effort,
                        "run_verify_commands": self._run_verify_commands,
                        "verify_command_timeout_seconds": (self._verify_command_timeout_seconds),
                    },
                    # Round-12 finding #2 (BLOCKING): the resolved
                    # model-routing contract this run STARTED
                    # with, in the SAME versioned shape the
                    # runner's durable execution contract already
                    # persists for session-resume (see
                    # ``_restore_checkpoint_model_router``).
                    "model_routing": serialize_model_router(self._model_router),
                    # Round-12 finding #2 (BLOCKING): the
                    # remaining constructor-injected scalars that
                    # change what a run dispatches or how it
                    # accepts work (see
                    # ``_restore_checkpoint_execution_semantics``).
                    "execution_semantics": {
                        "decomposition_mode": self._decomposition_mode,
                        "max_decomposition_depth": (self._max_decomposition_depth),
                        "fat_harness_mode": self._fat_harness_mode,
                        "cross_harness_redispatch_enabled": (
                            self._cross_harness_redispatch_enabled
                        ),
                        "shadow_replay_enabled": self._shadow_replay_enabled,
                        # Round-15 finding #5 (BLOCKING): the concurrency the
                        # run's shared-workspace dispatch actually ran under.
                        "max_concurrent": self._max_concurrent,
                        # Round-14 finding #3 (BLOCKING): the worker-prompt
                        # semantic the runner pinned for this run. Restored
                        # BEFORE the crash-restart's system prompt is
                        # (re)built — see ``system_prompt_builder``.
                        "context_pack_enabled": self._context_pack_enabled,
                    },
                    # The complete resolved profile controls decomposition,
                    # suggested tier, tools, evidence schema, verifier focus,
                    # and prompts.  Crash recovery restores this contract
                    # instead of re-reading live YAML.
                    "execution_profile": serialize_execution_profile(self._execution_profile),
                    # Round-14 finding #3 (BLOCKING): the runner's resolved
                    # guidance identity, stored opaquely; recovery hands it
                    # back to the runner's prompt builder for the same
                    # fail-closed identity check ``resume_session`` performs.
                    "prompt_guidance": self._prompt_guidance_contract,
                    # Round-12 finding #3 (BLOCKING): ownership
                    # marker for the liveness gate
                    # (``_checkpoint_owner_conflict``) — which
                    # process wrote this checkpoint and when, in
                    # the pid/host/heartbeat convention
                    # ``core.worktree``'s task lock established.
                    # ``written_at`` refreshes on every save,
                    # giving cross-host readers a coarse
                    # heartbeat; same-host readers probe the pid
                    # directly.
                    "owner": {
                        "pid": os.getpid(),
                        "host": socket.gethostname(),
                        "written_at": datetime.now(UTC).isoformat(),
                    },
                },
            )
            save_result = self._checkpoint_store.save(checkpoint)
            if hasattr(save_result, "is_ok") and save_result.is_ok:
                log.info(
                    "parallel_executor.checkpoint.saved",
                    checkpoint_point=checkpoint_point,
                    completed_levels=completed_levels,
                    seed_id=seed_id,
                )
                return True
            err_msg = str(save_result.error) if hasattr(save_result, "error") else "unknown error"
            log.warning(
                "parallel_executor.checkpoint.save_failed",
                checkpoint_point=checkpoint_point,
                completed_levels=completed_levels,
                seed_id=seed_id,
                error=err_msg,
            )
            self._console.print(
                f"  [yellow]Checkpoint save failed ({checkpoint_point}): {err_msg}[/yellow]"
            )
        except Exception as e:
            log.warning(
                "parallel_executor.checkpoint.save_failed",
                checkpoint_point=checkpoint_point,
                completed_levels=completed_levels,
                error=str(e),
            )
        return False

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
        force_atomic_execution: bool = False,
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

        ``force_atomic_execution`` is the decomposition-fallback half of the
        same recovery contract: a failed decomposed result has not yet spent
        the root AC's atomic option. The post-budget recovery path sets this
        flag for one redispatch, bypassing both preflight and bounce
        decomposition so the remaining option is genuinely exercised.
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
                        force_atomic_execution=force_atomic_execution,
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
        system_prompt_builder: Callable[..., str] | None = None,
    ) -> ParallelExecutionResult:
        """Execute ACs per the staged plan, always draining deferred writes.

        Thin boundary over :meth:`_execute_parallel_impl` (Round-7 Findings
        #2/#3 follow-up): deferred correctness-bearing durable writes (ladder
        resolution/interruption, decomposition attestation) previously got
        their bounded final drain only on the NORMAL completion path — an
        exception propagating out of the run body (a TaskGroup failure, a
        cancellation, an operator interrupt) skipped the drain entirely, so
        the pending background writes died silently at the ``asyncio.run``
        teardown boundary and cold resume reconstructed stale ladder /
        unattested-round state. The ``finally`` here gives those writes the
        same bounded final shot on EVERY exit path out of the run, normal or
        exceptional.

        Round-14 finding #1 (BLOCKING): before anything else — before
        checkpoint recovery can even consult the round-12 PID-based
        ownership gate — claim the IN-PROCESS lease for this seed. The PID
        gate deliberately trusts ``pid == os.getpid()``, so two concurrent
        invocations of the same seed inside one long-lived MCP server
        process would both pass it and race one checkpoint/aggregate. The
        lease makes the second in-process claimant fail loudly here (an
        infra-fatal launch conflict, zero AC work started) and is released
        in the same every-exit-path ``finally`` that drains durable writes,
        so a crash/cancel can never leave the seed permanently walled off.

        ZEP Stage-2 follow-up: the in-process lease alone does not make the
        FIRST cross-process checkpoint claim atomic. Two processes can both
        observe "no checkpoint" before either writes its run-start anchor.
        Hold the store's non-blocking execution lease across recovery, the
        initial anchor, and every AC dispatch so the loser fails before it can
        load or overwrite checkpoint state.

        Scoped to checkpoint-store-backed executions, exactly like the
        round-10/12 gates it complements (both live inside ``if
        self._checkpoint_store``): the object being raced is the SEED-KEYED
        checkpoint and the execution-aggregate adoption it drives. Without
        a checkpoint store each invocation keeps its own execution_id and
        aggregate — concurrent same-seed sessions are then isolated by
        design (a shape the session layer explicitly supports), and there
        is no shared seed-keyed durable object for the lease to protect.
        """
        lease_stack = contextlib.ExitStack()
        if self._checkpoint_store and not self._checkpoint_execution_lease_held:
            lease_seed_id = self._checkpoint_seed_id(seed, session_id)
            lease_stack.enter_context(
                self._claim_checkpoint_execution_lease(
                    self._checkpoint_store,
                    lease_seed_id,
                )
            )
        try:
            return await self._execute_parallel_impl(
                seed,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                system_prompt=system_prompt,
                tool_catalog=tool_catalog,
                dependency_graph=dependency_graph,
                execution_plan=execution_plan,
                reconciled_level_contexts=reconciled_level_contexts,
                externally_satisfied_acs=externally_satisfied_acs,
                system_prompt_builder=system_prompt_builder,
            )
        finally:
            try:
                # Keep both seed leases held through the final durable-write
                # drain. A replacement process must not begin replay while
                # this invocation is still settling correctness-bearing
                # attestation/escalation events from its last dispatch.
                await self._drain_deferred_durable_writes()
            finally:
                lease_stack.close()

    async def _execute_parallel_impl(
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
        system_prompt_builder: Callable[..., str] | None = None,
    ) -> ParallelExecutionResult:
        """Execute ACs according to a staged execution plan.

        Args:
            seed: Seed specification.
            execution_plan: Staged execution plan defining serial stages.
            session_id: Parent session ID for tracking.
            execution_id: Execution ID for event tracking.
            tools: Tools available to agents.
            system_prompt: System prompt for agents.
            system_prompt_builder: Round-14 finding #3 (BLOCKING) — the
                caller's system prompt was necessarily built BEFORE this
                method could run RC3 checkpoint recovery, i.e. from the
                CURRENT process's prompt semantics (fat-harness strategy,
                context-pack flag, resolved guidance). When recovery adopts
                a checkpoint, the prompt must instead reflect the ORIGINAL
                run's restored semantics — so after (and only after) a
                successful adoption, this callback is invoked with the
                restored ``fat_harness_mode`` / ``context_pack_enabled`` /
                opaque ``guidance_contract`` and its return value REPLACES
                ``system_prompt`` before any AC is dispatched. A raise from
                the builder (e.g. the runner's guidance identity check
                refusing changed guidance) fails the launch loudly before
                any AC work — never a silent fallback to the stale prompt.
                ``None`` (direct/test callers) keeps the passed prompt
                byte-for-byte, recovery or not.
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
        satisfied_externally_indices: set[int] = set()
        stage_results: list[ParallelExecutionStageResult] = []
        total_levels = execution_plan.total_stages
        total_acs = len(seed.acceptance_criteria)
        level_contexts = self._normalize_reconciled_level_contexts(
            reconciled_level_contexts,
            total_acs=total_acs,
            total_levels=total_levels,
        )
        external_completed = self._normalize_externally_satisfied_acs(
            externally_satisfied_acs,
            total_acs=total_acs,
        )
        execution_counters = {
            "messages_count": 0,
            "tool_calls_count": 0,
        }
        dispatch_contract = self._build_checkpoint_dispatch_contract(
            tools=tools,
            tool_catalog=tool_catalog,
            system_prompt=system_prompt,
            system_prompt_builder=system_prompt_builder,
            externally_satisfied_ac_indices=tuple(sorted(external_completed)),
            reconciled_level_contexts=level_contexts,
        )

        # Track AC statuses for TUI updates
        ac_statuses: dict[int, str] = dict.fromkeys(range(total_acs), "pending")
        ac_retry_attempts: dict[int, int] = dict.fromkeys(range(total_acs), 0)
        completed_count = 0
        resume_from_level = 0
        # Round-16 finding #1: ACs the adopted checkpoint recorded as
        # RESOLVED (completed/failed), keyed by stable AC index. This — not
        # the plan-relative ``completed_levels`` count — is what decides
        # which ACs are skipped on resume. Round-17 finding #2: "skipped"
        # is deliberately NOT resolved — see the restore loop below.
        checkpoint_resolved: dict[int, str] = {}
        # Round-14 finding #3: only a FULLY adopted recovery may trigger the
        # post-restoration prompt rebuild below; a partially restored state
        # that hit the generic "recovery failed, run fresh" path keeps the
        # caller's prompt (the pre-existing fresh-run posture).
        recovery_adopted = False
        restored_prompt_guidance: Any = None
        self._ordinary_finalized_resume_results.clear()

        # RC3: Attempt to recover from checkpoint
        if self._checkpoint_store:
            # Round-16 finding #3: snapshot every piece of fresh-run local
            # state the adoption branch may mutate, so the generic failure
            # handler below can roll ALL of it back — recovery must either
            # apply completely or leave the fresh-run posture untouched,
            # never a torn mixture.
            pre_recovery_execution_id = execution_id
            pre_recovery_result_count = len(all_results)
            pre_recovery_decomposition_decisions = dict(self._decomposition_decisions)
            pre_recovery_execution_profile = self._execution_profile
            pre_recovery_level_contexts = list(level_contexts)
            pre_recovery_external_completed = dict(external_completed)
            pre_recovery_dispatch_contract = dict(dispatch_contract)
            pre_recovery_satisfied_externally_indices = set(satisfied_externally_indices)
            try:
                seed_id = self._checkpoint_seed_id(seed, session_id)
                cp = await self._load_checkpoint_for_recovery(seed_id)
                if cp is not None:
                    # Round-10 finding #1 (BLOCKING): a checkpoint is only a
                    # resume ticket for an INTERRUPTED run. If the run it
                    # belongs to already recorded a non-resumable terminal
                    # outcome, this is a genuinely NEW execution of the same
                    # seed — adopt nothing, discard the stale checkpoint,
                    # and run every level from scratch.
                    if cp.phase == "parallel_execution" and (
                        version_malformed := self._checkpoint_contract_version_malformed(cp)
                    ):
                        log.error(
                            "parallel_executor.recovery.checkpoint_version_unsupported",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=version_malformed,
                        )
                        self._console.print(
                            "[red]Refusing to start: this seed's checkpoint has "
                            f"an unsupported format ({version_malformed}). It was "
                            "preserved for operator repair.[/red]"
                        )
                        raise CheckpointCorruptError(
                            "Checkpoint format is unsupported: "
                            f"{version_malformed}. Refusing to infer missing "
                            "execution semantics."
                        )
                    elif cp.phase == "parallel_execution" and (
                        owner_malformed := self._checkpoint_owner_malformed(cp)
                    ):
                        log.error(
                            "parallel_executor.recovery.checkpoint_owner_malformed",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=owner_malformed,
                        )
                        self._console.print(
                            "[red]Refusing to start: this seed's current-format "
                            "checkpoint has a missing or malformed owner record "
                            f"({owner_malformed}). It was preserved for operator "
                            "repair.[/red]"
                        )
                        raise CheckpointCorruptError(
                            "Checkpoint owner record is malformed: "
                            f"{owner_malformed}. Refusing to adopt uncertain "
                            "execution ownership."
                        )
                    elif (
                        cp.phase == "parallel_execution"
                        and await self._checkpoint_from_terminal_run(cp, total_levels=total_levels)
                    ):
                        self._discard_stale_checkpoint(
                            seed_id,
                            saved_execution_id=cp.state.get("execution_id"),
                        )
                    elif cp.phase == "parallel_execution" and (
                        owner_conflict := self._checkpoint_owner_conflict(cp)
                    ):
                        # Round-12 finding #3 (BLOCKING): before adopting a
                        # NON-terminal checkpoint, make sure its writer is
                        # not still alive. Two live claimants on one seed
                        # (operator double-launch, a monitor relaunching
                        # over a live process, or a run that is legitimately
                        # paused/parked awaiting ITS OWN resume) must never
                        # silently converge on the same execution aggregate.
                        # Refusing is loud and immediate — an infra-fatal
                        # launch conflict raised before any AC work starts,
                        # so no AC ever surfaces FAILED over it. Checked
                        # BEFORE the round-15 content gate below: even a
                        # content-mismatched checkpoint must never be
                        # DISCARDED out from under a still-live owner.
                        log.error(
                            "parallel_executor.recovery.active_checkpoint_conflict",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            conflict=owner_conflict,
                        )
                        self._console.print(
                            "[red]Refusing to start: an existing run of "
                            "this seed appears to still be active or "
                            f"paused ({owner_conflict}).[/red]"
                        )
                        raise CheckpointOwnershipError(
                            "An RC3 checkpoint for seed "
                            f"'{seed_id}' appears to belong to a run that "
                            f"is still active or paused: {owner_conflict}. "
                            "Refusing to adopt it — two live processes "
                            "must not share one execution's durable "
                            "state. If that run has finished or been "
                            "stopped, simply relaunch (a dead owner is "
                            "adopted automatically); otherwise wait for "
                            "it or cancel it first, or start a different "
                            "seed/session to run fresh."
                        )
                    elif cp.phase == "parallel_execution" and (
                        fingerprint_malformed := self._checkpoint_seed_fingerprint_malformed(cp)
                    ):
                        log.error(
                            "parallel_executor.recovery.seed_fingerprint_malformed",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=fingerprint_malformed,
                        )
                        self._console.print(
                            "[red]Refusing to start: the persisted checkpoint "
                            "has no verifiable Seed fingerprint "
                            f"({fingerprint_malformed}). It was preserved for "
                            "operator repair.[/red]"
                        )
                        raise CheckpointCorruptError(
                            "Checkpoint Seed identity is malformed: "
                            f"{fingerprint_malformed}. Refusing to run fresh "
                            "over an interrupted execution."
                        )
                    elif cp.phase == "parallel_execution" and (
                        content_mismatch := self._checkpoint_seed_content_mismatch(cp, seed)
                    ):
                        # Round-15 finding #1 (BLOCKING): the checkpoint key
                        # (``seed_id``) names an OBJECT, not its content — a
                        # Seed whose goal/AC content was edited under the
                        # same seed_id used to adopt the pre-edit run's
                        # progress wholesale: recovery dispatched NOTHING
                        # and reported SUCCESS while attributing the NEW
                        # content as "restored" from work that never ran on
                        # it. Treat it like the round-10 staleness case:
                        # this is not a genuine resume of the same logical
                        # run — discard, run every level fresh, and say so
                        # loudly. (A malformed fingerprint takes this same
                        # refuse-adoption branch — fail closed; a LEGACY
                        # checkpoint without one keeps the adopt posture as
                        # a one-time migration.)
                        log.error(
                            "parallel_executor.recovery.seed_content_mismatch",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=content_mismatch,
                        )
                        self._discard_stale_checkpoint(
                            seed_id,
                            saved_execution_id=cp.state.get("execution_id"),
                            detail=content_mismatch,
                            console_message=(
                                "[red]WARNING: found a checkpoint under this "
                                "seed's id, but it was saved for DIFFERENT "
                                "goal/acceptance-criteria content. Refusing "
                                "to adopt its progress (that could falsely "
                                "report this Seed's work as already done) — "
                                "running all levels fresh instead.[/red]"
                            ),
                        )
                    elif cp.phase == "parallel_execution" and (
                        plan_malformed := self._checkpoint_plan_malformed(cp, total_acs=total_acs)
                    ):
                        log.error(
                            "parallel_executor.recovery.execution_plan_malformed",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=plan_malformed,
                        )
                        self._console.print(
                            "[red]Refusing to start: this seed has a persisted "
                            "checkpoint, but its execution-plan contract is "
                            f"missing or malformed ({plan_malformed}). The "
                            "checkpoint was preserved for operator repair.[/red]"
                        )
                        raise CheckpointCorruptError(
                            "Checkpoint execution-plan contract is malformed: "
                            f"{plan_malformed}. Refusing to run fresh over work "
                            "whose side effects may already exist."
                        )
                    elif cp.phase == "parallel_execution" and (
                        dispatch_malformed := self._checkpoint_dispatch_contract_malformed(
                            cp,
                            total_acs=total_acs,
                            plan_total_stages=total_levels,
                        )
                    ):
                        log.error(
                            "parallel_executor.recovery.dispatch_contract_malformed",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=dispatch_malformed,
                        )
                        self._console.print(
                            "[red]Refusing to start: this seed's persisted "
                            "checkpoint has a missing or malformed dispatch "
                            f"contract ({dispatch_malformed}). It was preserved "
                            "for operator repair.[/red]"
                        )
                        raise CheckpointCorruptError(
                            "Checkpoint dispatch contract is malformed: "
                            f"{dispatch_malformed}. Refusing to run fresh over "
                            "an interrupted execution."
                        )
                    elif cp.phase == "parallel_execution" and (
                        dispatch_mismatch := self._checkpoint_dispatch_contract_mismatch(
                            cp,
                            tools=tools,
                            tool_catalog=tool_catalog,
                            system_prompt=system_prompt,
                            system_prompt_builder=system_prompt_builder,
                            externally_satisfied_ac_indices=tuple(sorted(external_completed)),
                            reconciled_level_contexts=level_contexts,
                        )
                    ):
                        log.error(
                            "parallel_executor.recovery.dispatch_contract_mismatch",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=dispatch_mismatch,
                        )
                        self._console.print(
                            "[red]Refusing to resume: the live tools, tool "
                            "schemas, or prompt authority differ from the "
                            "interrupted run.[/red]"
                        )
                        raise CheckpointDispatchMismatchError(dispatch_mismatch)
                    elif cp.phase == "parallel_execution" and (
                        plan_mismatch := self._checkpoint_plan_mismatch(
                            cp, execution_plan=execution_plan
                        )
                    ):
                        log.error(
                            "parallel_executor.recovery.execution_plan_mismatch",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=plan_mismatch,
                        )
                        self._console.print(
                            "[red]Refusing to resume: dependency analysis now "
                            "produced a different plan from the interrupted "
                            "run. Restore the original analysis inputs/runtime "
                            "or start an explicitly new Seed.[/red]"
                        )
                        raise CheckpointPlanMismatchError(plan_mismatch)
                    elif cp.phase == "parallel_execution" and (
                        progress_malformed := self._checkpoint_progress_malformed(
                            cp,
                            total_acs=total_acs,
                        )
                    ):
                        # Round-16 finding #3 (BLOCKING): a hash-valid
                        # checkpoint whose PROGRESS fields are type-mangled
                        # (e.g. ``completed_levels`` persisted as the string
                        # "1", a non-integer ``ac_statuses`` key) used to be
                        # applied incrementally until a conversion raised —
                        # by which point ``execution_id`` had already been
                        # reassigned, leaving recovery partially applied.
                        # Every progress field was validated atomically above.
                        # Preserve malformed persisted state for operator
                        # repair and block before any work; running fresh could
                        # duplicate side effects already applied by the
                        # interrupted execution.
                        log.error(
                            "parallel_executor.recovery.progress_state_malformed",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=progress_malformed,
                        )
                        self._console.print(
                            "[red]Refusing to start: this seed's persisted "
                            "checkpoint has malformed progress state "
                            f"({progress_malformed}). It was preserved for "
                            "operator repair; running fresh could duplicate "
                            "already-applied side effects.[/red]"
                        )
                        raise CheckpointCorruptError(
                            "Checkpoint progress state is malformed: "
                            f"{progress_malformed}. Refusing to run fresh over "
                            "an interrupted execution."
                        )
                    elif cp.phase == "parallel_execution" and (
                        semantics_malformed := self._checkpoint_semantics_malformed(cp)
                    ):
                        # Round-17 finding #3 (BLOCKING): a checkpoint whose
                        # retry-policy / model-routing / execution-semantics
                        # payload is type-mangled used to be ADOPTED anyway —
                        # the restore helper for the malformed group logged,
                        # forced the escalation gates open, and silently kept
                        # the CURRENT process's values while the OTHER groups
                        # restored the ORIGINAL run's, so the recovered run
                        # executed under a torn mixture of two runs'
                        # semantics. Every semantic group was just validated
                        # atomically above, BEFORE any restoration mutated
                        # executor state. Preserve the corrupt checkpoint and
                        # block the launch; do not invent fresh-run authority.
                        log.error(
                            "parallel_executor.recovery.semantic_state_malformed",
                            seed_id=seed_id,
                            checkpoint_execution_id=cp.state.get("execution_id"),
                            detail=semantics_malformed,
                        )
                        self._console.print(
                            "[red]Refusing to start: this seed's persisted "
                            "checkpoint has malformed execution semantics "
                            f"({semantics_malformed}). It was preserved for "
                            "operator repair; running fresh could duplicate "
                            "already-applied side effects.[/red]"
                        )
                        raise CheckpointCorruptError(
                            "Checkpoint execution semantics are malformed: "
                            f"{semantics_malformed}. Refusing to run fresh over "
                            "an interrupted execution."
                        )
                    elif cp.phase == "parallel_execution":
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
                        # Round-16 finding #3: every progress field was
                        # validated atomically by the gate above, and the
                        # one remaining raise-capable payload (the level
                        # contexts) is parsed HERE, before any local
                        # execution state is mutated — so nothing below can
                        # leave recovery partially applied.
                        saved_contexts = cp.state.get("level_contexts", [])
                        restored_contexts = (
                            deserialize_level_contexts(saved_contexts) if saved_contexts else None
                        )
                        saved_execution_id = cp.state.get("execution_id")
                        if isinstance(saved_execution_id, str) and saved_execution_id:
                            execution_id = saved_execution_id
                        saved_completed_levels = cp.state.get("completed_levels", 0)
                        for idx, status in cp.state.get("ac_statuses", {}).items():
                            # Round-17 finding #2 (BLOCKING): "skipped" is
                            # not a terminal outcome of the AC itself — the
                            # AC never ran; it was withheld because an
                            # UPSTREAM dependency of the ORIGINAL run's plan
                            # failed. That is a plan-structure-relative fact,
                            # and the plan is re-derived on every launch
                            # (exactly the class of stale plan-relative state
                            # round-16 #1 stopped trusting for level counts).
                            # Restore it as "pending" so THIS launch's
                            # dependency cascade re-decides it under the
                            # CURRENT plan: a still-failed dependency re-skips
                            # it identically (``failed_indices`` is restored
                            # below), while a vanished edge or a dependency
                            # that succeeds this time gives it the genuine
                            # dispatch it never had — an AC must never stay
                            # permanently "failed-shaped" off a dependency
                            # snapshot that no longer describes this run.
                            ac_statuses[int(idx)] = "pending" if status == "skipped" else status
                        for idx in cp.state.get("failed_indices", []):
                            failed_indices.add(int(idx))
                        satisfied_externally_indices.update(
                            int(idx) for idx in cp.state.get("satisfied_externally_indices", [])
                        )
                        completed_count = cp.state.get("completed_count", 0)
                        # Restore execution semantics before interpreting
                        # finalized attempts: the original retry cap/profile
                        # determine whether a completed failed dispatch was
                        # the last ordinary attempt and what the continuation
                        # is allowed to do next.
                        self._restore_checkpoint_retry_policy(cp.state.get("retry_policy"))
                        self._restore_checkpoint_model_router(cp.state.get("model_routing"))
                        self._restore_checkpoint_execution_semantics(
                            cp.state.get("execution_semantics")
                        )
                        self._restore_checkpoint_execution_profile(
                            cp.state.get("execution_profile")
                        )
                        # Round-16 finding #1 (BLOCKING): ``completed_levels``
                        # is an integer relative to the ORIGINAL run's plan
                        # STRUCTURE, but the plan is re-derived by LLM
                        # dependency analysis on every launch — including a
                        # documented deterministic fallback that collapses
                        # ALL ACs into one single level when the analysis
                        # fails — so the same fingerprint-matching seed
                        # content can legitimately arrive here with a
                        # DIFFERENT stage grouping. Interpreting the stale
                        # count against THIS plan's stages skipped whole
                        # levels of never-dispatched ACs and reconstructed
                        # them below as "Failed (restored from checkpoint)":
                        # FAILED with zero dispatch and zero escalation, the
                        # forbidden outcome. Resume is therefore keyed off
                        # the checkpoint's per-AC statuses — AC index is a
                        # stable identity across replans (the fingerprint
                        # gate above proved the AC list itself is unchanged)
                        # — and ``resume_from_level`` is RE-DERIVED against
                        # the CURRENT plan as the count of leading stages
                        # made entirely of resolved ACs. An AC the original
                        # run never resolved gets dispatched normally,
                        # whatever level this launch's plan re-grouped it
                        # into.
                        # Round-17 finding #2: only outcomes the AC earned on
                        # its OWN merits are terminal — "completed" and
                        # "failed" ACs actually ran (or were definitively
                        # rejected). "skipped" never appears here: the
                        # restore loop above already re-opened it as
                        # "pending" for the current plan's cascade to
                        # re-decide.
                        # Restore level contexts so subsequent levels
                        # have access to completed levels' output (parsed
                        # up front, before any mutation — round-16 #3).
                        if restored_contexts is not None:
                            level_contexts = restored_contexts
                        finalized_outcomes = await self._reconstruct_finalized_outcomes(
                            execution_id=execution_id,
                            total_acs=total_acs,
                            expected_semantic_ac_keys={
                                ac_index: (
                                    criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
                                )
                                for ac_index, criterion in enumerate(seed.acceptance_criteria)
                            },
                        )
                        if finalized_outcomes is None:
                            raise CheckpointUnreadableError(
                                "Could not replay the interrupted run's finalized AC "
                                "outcomes. Refusing to redispatch work whose completion "
                                "state cannot be determined."
                            )
                        self._ordinary_finalized_resume_results.clear()
                        existing_context_indices = {
                            summary.ac_index
                            for context in level_contexts
                            for summary in context.completed_acs
                        }
                        recovered_success_contexts: dict[int, ACContextSummary] = {}
                        missing_success_contexts = {
                            retry_ac_idx
                            for retry_ac_idx, prior_status in ac_statuses.items()
                            if prior_status == "completed"
                            and retry_ac_idx not in satisfied_externally_indices
                            and retry_ac_idx not in existing_context_indices
                        }
                        for retry_ac_idx, finalized in finalized_outcomes.items():
                            prior_status = ac_statuses.get(retry_ac_idx, "pending")
                            if prior_status in ("completed", "failed"):
                                ac_retry_attempts[retry_ac_idx] = finalized.retry_attempt
                                if prior_status == "completed":
                                    if not finalized.success:
                                        raise CheckpointUnreadableError(
                                            "Checkpoint marks an AC completed but its latest "
                                            "durable finalized outcome is a failure "
                                            f"(AC {retry_ac_idx + 1})."
                                        )
                                    if retry_ac_idx in missing_success_contexts:
                                        if finalized.context_summary is not None:
                                            recovered_success_contexts[retry_ac_idx] = (
                                                finalized.context_summary
                                            )
                                            missing_success_contexts.discard(retry_ac_idx)
                                continue
                            if finalized.success:
                                ac_statuses[retry_ac_idx] = "completed"
                                ac_retry_attempts[retry_ac_idx] = finalized.retry_attempt
                                completed_count += 1
                                if retry_ac_idx not in existing_context_indices:
                                    if finalized.context_summary is None:
                                        missing_success_contexts.add(retry_ac_idx)
                                    else:
                                        recovered_success_contexts[retry_ac_idx] = (
                                            finalized.context_summary
                                        )
                            elif finalized.recovery_exhausted:
                                ac_statuses[retry_ac_idx] = "failed"
                                ac_retry_attempts[retry_ac_idx] = finalized.retry_attempt
                                failed_indices.add(retry_ac_idx)
                            elif finalized.retry_attempt >= self._ac_retry_attempts:
                                # Dispatch completed at the ordinary cap, but
                                # the process died before escalation/terminal
                                # closure. Resume from that post-dispatch
                                # boundary; never execute the same side effect
                                # again merely to reach the finalizer.
                                ac_retry_attempts[retry_ac_idx] = finalized.retry_attempt
                                self._ordinary_finalized_resume_results[retry_ac_idx] = (
                                    ACExecutionResult(
                                        ac_index=retry_ac_idx,
                                        ac_content=ac_text(seed.acceptance_criteria[retry_ac_idx]),
                                        success=False,
                                        error=(
                                            "Restored durably finalized failure at the "
                                            "ordinary retry boundary"
                                        ),
                                        retry_attempt=finalized.retry_attempt,
                                        is_decomposed=finalized.is_decomposed,
                                        outcome=finalized.outcome,
                                        forced_frontier_routing=(finalized.forced_frontier_routing),
                                    )
                                )
                            else:
                                ac_retry_attempts[retry_ac_idx] = finalized.retry_attempt + 1

                        stage_position_by_ac = {
                            ac_index: stage_position
                            for stage_position, stage in enumerate(execution_plan.stages)
                            for ac_index in self._get_stage_ac_indices(stage)
                        }
                        for missing_ac_index in sorted(missing_success_contexts):
                            success_stage = stage_position_by_ac[missing_ac_index]
                            downstream_unresolved = any(
                                ac_statuses.get(downstream_ac_index) not in ("completed", "failed")
                                for downstream_stage in execution_plan.stages[success_stage + 1 :]
                                for downstream_ac_index in self._get_stage_ac_indices(
                                    downstream_stage
                                )
                            )
                            if downstream_unresolved:
                                raise CheckpointUnreadableError(
                                    "A durably finalized success lacks the AC context "
                                    "required by unfinished downstream stages "
                                    f"(AC {missing_ac_index + 1}). Refusing to resume "
                                    "without its files/tools/output handoff."
                                )
                        level_contexts = self._merge_recovered_success_contexts(
                            level_contexts,
                            execution_plan,
                            recovered_success_contexts,
                        )

                        checkpoint_resolved = {
                            idx: status
                            for idx, status in ac_statuses.items()
                            if 0 <= idx < total_acs and status in ("completed", "failed")
                        }
                        resume_from_level = 0
                        for planned_stage in execution_plan.stages:
                            planned_indices = [
                                idx
                                for idx in self._get_stage_ac_indices(planned_stage)
                                if 0 <= idx < total_acs
                            ]
                            if all(idx in checkpoint_resolved for idx in planned_indices):
                                resume_from_level += 1
                            else:
                                break
                        if resume_from_level != saved_completed_levels:
                            log.info(
                                "parallel_executor.recovery.progress_advanced_by_events",
                                checkpoint_completed_levels=saved_completed_levels,
                                derived_resume_from_level=resume_from_level,
                                resolved_ac_indices=sorted(checkpoint_resolved),
                            )
                        raw_decisions = cp.state.get("decomposition_decisions", {})
                        if isinstance(raw_decisions, Mapping):
                            for raw_node_id, raw_record in raw_decisions.items():
                                if not isinstance(raw_node_id, str):
                                    continue
                                restored = DecompositionDecisionRecord.from_dict(raw_record)
                                if restored is not None and restored.node_id == raw_node_id:
                                    self._decomposition_decisions[raw_node_id] = restored
                        # Round-9 finding #2 (BLOCKING): the ladder-history
                        # gate in ``_run_batch_with_verify_and_retry`` and
                        # the ladder-entry gate in
                        # ``_maybe_run_lateral_escalation_ladder`` both read
                        # THIS executor's ``_lateral_escalation_enabled`` —
                        # which was just resolved from the CURRENT config,
                        # not the config the checkpointed run started with.
                        # If an operator edit (or the default False) landed
                        # between crash and restart, a genuinely parked/
                        # mid-ladder AC would be treated as having no ladder
                        # history at all: durable state never loaded, a
                        # fresh retry budget granted, and FAILED surfaced
                        # once it is spent — the original run's escalation
                        # guarantee silently lost. A recovered run IS the
                        # original run continuing (same execution_id, same
                        # durable aggregate), so restore the persisted
                        # policy wholesale — the same "a resumed run keeps
                        # the termination semantics it STARTED with" rule
                        # the runner's durable retry_policy contract
                        # enforces for resume_session. A checkpoint
                        # predating the field keeps the current-config
                        # posture (genuine one-time migration, like the
                        # runner's ``retry_policy_migrated``).
                        # Round-12 finding #2 (BLOCKING): two more
                        # execution-semantic input groups the checkpoint
                        # never carried — the resolved model router (tier
                        # routing / frugality cohort identity) and the
                        # constructor-injected dispatch/verification scalars
                        # (decomposition mode & depth, fat-harness gate,
                        # cross-harness redispatch, shadow replay). A
                        # recovered run IS the original run continuing, so it
                        # must keep the routing and dispatch semantics it
                        # STARTED with, not the current process's
                        # construction args.
                        log.info(
                            "parallel_executor.recovery.resuming",
                            from_level=resume_from_level,
                            seed_id=seed_id,
                            execution_id=execution_id,
                            restored_contexts=len(level_contexts),
                        )
                        # Reconstruct results for every AC the checkpoint
                        # recorded as resolved — keyed by stable AC index,
                        # NOT by slicing this launch's (possibly re-grouped)
                        # stages: an AC with no terminal status in the
                        # checkpoint gets NO restored result here and is
                        # dispatched by the level loop below. (Round-17 #2:
                        # "skipped" ACs are never reconstructed — they are
                        # re-evaluated by the level loop's dependency
                        # cascade, which re-skips or dispatches them under
                        # the CURRENT plan.)
                        for ac_idx in sorted(checkpoint_resolved):
                            status = checkpoint_resolved[ac_idx]
                            is_completed = status == "completed"
                            is_external = ac_idx in satisfied_externally_indices
                            all_results.append(
                                ACExecutionResult(
                                    ac_index=ac_idx,
                                    ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                                    success=is_completed,
                                    final_message=(
                                        "[Restored externally satisfied result from checkpoint]"
                                        if is_external
                                        else "[Restored from checkpoint]"
                                        if is_completed
                                        else ""
                                    ),
                                    error=(
                                        None
                                        if is_completed
                                        else "Failed (restored from checkpoint)"
                                    ),
                                    retry_attempt=ac_retry_attempts.get(ac_idx, 0),
                                    outcome=(
                                        ACExecutionOutcome.SATISFIED_EXTERNALLY
                                        if is_external
                                        else ACExecutionOutcome.SUCCEEDED
                                        if is_completed
                                        else ACExecutionOutcome.FAILED
                                    ),
                                )
                            )
                        self._console.print(
                            f"[cyan]Resuming from level {resume_from_level + 1} "
                            f"(checkpoint recovered, "
                            f"{len(level_contexts)} level context(s) restored)[/cyan]"
                        )
                        # Round-14 finding #3: adoption is complete — the
                        # prompt rebuild below may now use the restored
                        # semantics (and the original run's opaque guidance
                        # identity, if the checkpoint carried one).
                        recovery_adopted = True
                        restored_prompt_guidance = cp.state.get("prompt_guidance")
                        saved_dispatch_contract = cp.state["dispatch_contract"]
                        saved_external_indices = saved_dispatch_contract[
                            "externally_satisfied_ac_indices"
                        ]
                        external_completed = {
                            ac_index: external_completed.get(ac_index, {})
                            for ac_index in saved_external_indices
                        }
                        dispatch_contract = dict(saved_dispatch_contract)
            except (
                CheckpointCorruptError,
                CheckpointDispatchMismatchError,
                CheckpointOwnershipError,
                CheckpointPlanMismatchError,
                CheckpointUnreadableError,
            ):
                # Round-12 finding #3: the ownership refusal must FAIL the
                # launch, never degrade into "recovery failed, run fresh" —
                # a fresh run of the same seed would still race the live
                # owner on the shared checkpoint key, the workspace, and
                # the dispatched ACs themselves. Round-15 finding #2: the
                # unreadable-checkpoint refusal follows for the same reason
                # — an UNCONFIRMED checkpoint may hide exactly that live
                # owner (or an interrupted run's restorable identity), so a
                # degraded read must not degrade into running fresh either.
                raise
            except Exception as e:
                # Round-16 finding #1: a PARTIAL restore must not leak into
                # the fresh run below. A half-adopted skip horizon or
                # failed/resolved markers could suppress dispatch of ACs
                # that never ran (the forbidden FAILED-without-dispatch
                # shape) — reset every dispatch-gating field to its
                # fresh-run value before proceeding.
                #
                # Round-16 finding #3: the reset must cover EVERYTHING the
                # adoption branch mutates, not only the dispatch-gating
                # fields — a half-restored ``execution_id`` in particular
                # would run "fresh" work under the ORIGINAL run's durable
                # aggregate (torn identity), and half-restored contexts/
                # results/decomposition decisions would mix two runs'
                # state. Roll all of it back to the pre-recovery snapshot.
                resume_from_level = 0
                checkpoint_resolved = {}
                failed_indices.clear()
                ac_statuses.update(dict.fromkeys(range(total_acs), "pending"))
                # Round-17 finding #4: the restored per-AC retry consumption
                # is part of the adopted state too — roll it back with the
                # rest.
                ac_retry_attempts.update(dict.fromkeys(range(total_acs), 0))
                self._ordinary_finalized_resume_results.clear()
                completed_count = 0
                execution_id = pre_recovery_execution_id
                level_contexts = pre_recovery_level_contexts
                external_completed = pre_recovery_external_completed
                dispatch_contract = pre_recovery_dispatch_contract
                satisfied_externally_indices = pre_recovery_satisfied_externally_indices
                del all_results[pre_recovery_result_count:]
                self._decomposition_decisions.clear()
                self._decomposition_decisions.update(pre_recovery_decomposition_decisions)
                self._execution_profile = pre_recovery_execution_profile
                recovery_adopted = False
                restored_prompt_guidance = None
                log.warning(
                    "parallel_executor.recovery.failed",
                    error=str(e),
                )

        # Round-14 finding #3 (BLOCKING): the caller built ``system_prompt``
        # BEFORE this recovery could restore the original run's prompt
        # semantics — an ordering bug, not a coverage bug. Rebuild it NOW,
        # after restoration and before any dispatch, from the restored
        # fat-harness/context-pack values and the checkpointed guidance
        # identity. Deliberately OUTSIDE the recovery try/except: a builder
        # refusal (changed guidance) must fail the launch loudly — an
        # infra-fatal condition before any AC work, like the ownership gate
        # — never degrade into dispatching with a stale or mismatched prompt.
        if recovery_adopted and system_prompt_builder is not None:
            system_prompt = system_prompt_builder(
                fat_harness_mode=self._fat_harness_mode,
                context_pack_enabled=self._context_pack_enabled,
                guidance_contract=restored_prompt_guidance,
                execution_profile=self._execution_profile,
            )
            saved_prompt_contract = dispatch_contract.get("system_prompt")
            expected_prompt_identity = (
                saved_prompt_contract.get("identity")
                if isinstance(saved_prompt_contract, Mapping)
                else None
            )
            if (
                not isinstance(system_prompt, str)
                or self._prompt_identity(system_prompt) != expected_prompt_identity
            ):
                raise CheckpointDispatchMismatchError(
                    "the restored system-prompt builder did not reproduce the "
                    "interrupted run's exact prompt identity"
                )

        # Reusable decomposition trust is correctness-bearing routing authority:
        # a passing attestation can lower the model tier of a later child
        # dispatch. Bind that authority to the exact canonical workspace and
        # dispatch contract that produced the verification evidence, not merely
        # to a serialized Seed/split. The final dispatch contract is selected
        # only after checkpoint recovery above; using the pre-recovery request
        # here would let a resumed run publish/consume trust under semantics it
        # did not actually execute.
        self._decomposition_attestation_scope = self._build_decomposition_attestation_scope(
            seed_id=self._checkpoint_seed_id(seed, session_id),
            seed_fingerprint=self._seed_semantic_fingerprint(seed),
            dispatch_contract=dispatch_contract,
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

        # Round-13 finding #1 (BLOCKING): persist the run's identity BEFORE
        # the first AC dispatch, not only after each level completes. The
        # per-level cadence alone left a first-level hole: a crash before
        # ANY level finished had never written a checkpoint, so the restart
        # had zero durable record of the original ``execution_id`` or the
        # policy/router/dispatch semantics rounds 9-12 so carefully restore
        # — none of that recovery machinery could activate, a fresh
        # execution_id was minted, the original run's durable
        # ladder/escalation events (keyed by the ORIGINAL execution_id)
        # became unreachable, and any escalation progress from that first
        # level was silently lost. This save reuses the exact per-level
        # checkpoint shape with the current progress (none, for a fresh
        # run; the restored progress, for a recovery — which also
        # refreshes the round-12 ownership marker to THIS process). The
        # level loop keeps overwriting it as levels complete.
        #
        # This anchor is the ONLY durable execution identity before first
        # dispatch.  A background retry cannot protect the crash window
        # between dispatch and persistence, so failure below blocks the
        # launch synchronously: zero AC work starts without a recoverable
        # plan/profile/policy identity.
        anchor_persisted = self._save_execution_checkpoint(
            seed=seed,
            session_id=session_id,
            execution_id=execution_id,
            completed_levels=resume_from_level,
            plan_total_stages=total_levels,
            execution_plan=execution_plan,
            dispatch_contract=dispatch_contract,
            ac_statuses=ac_statuses,
            failed_indices=failed_indices,
            satisfied_externally_indices=satisfied_externally_indices,
            completed_count=completed_count,
            level_contexts=level_contexts,
            checkpoint_point="run_start",
        )
        if self._checkpoint_store and not anchor_persisted:
            log.error(
                "parallel_executor.checkpoint.run_start_anchor_required",
                execution_id=execution_id,
                detail=(
                    "refusing to dispatch any AC because the mandatory "
                    "pre-dispatch recovery anchor was not persisted"
                ),
            )
            self._console.print(
                "[red]Refusing to dispatch: the run-start checkpoint anchor "
                "could not be persisted. No AC work was started; repair the "
                "checkpoint store and relaunch.[/red]"
            )
            raise CheckpointPersistenceError(
                "The mandatory run-start checkpoint could not be persisted. "
                "Refusing to dispatch work without a durable execution identity."
            )

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

                # RC3: Skip already-completed levels on recovery.
                # Round-16 finding #1: ``resume_from_level`` is re-derived
                # at adoption time from the checkpoint's per-AC statuses
                # against THIS plan's stages (never the checkpoint's raw
                # plan-relative level count), so a wholesale skip here is
                # only ever taken for stages made entirely of ACs the
                # checkpoint actually recorded as resolved.
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

                    # Round-16 finding #1: an AC the adopted checkpoint
                    # recorded as resolved is skipped by its stable AC
                    # index — its restored result is already in
                    # ``all_results`` — regardless of which level this
                    # launch's re-derived plan grouped it into. Unresolved
                    # ACs fall through and are dispatched normally.
                    if ac_idx in checkpoint_resolved:
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
                    satisfied_externally_indices.add(ac_idx)
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
                self._copy_level_context_chain_digest(
                    source=level_contexts,
                    target=current_contexts,
                )

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
                        if r.ac_index in executable and r.success
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

                    level_contexts = self._merge_level_context(level_contexts, level_ctx)
                stage_results.append(stage_result)

                # RC3: Save checkpoint after each level completion.
                #
                # Round-15 finding #2: deliberately still best-effort (the
                # historical posture), unlike the pre-dispatch anchor above.
                # Once the mandatory anchor has landed, a failed per-level
                # overwrite only leaves checkpoint progress stale. Recovery
                # reconciles authoritative ``outcome_finalized`` /
                # ``recovery_exhausted`` events before dispatch, so completed
                # side effects are not repeated merely because this summary
                # update failed. A deferred overwrite here could instead race
                # and roll newer progress backward.
                self._save_execution_checkpoint(
                    seed=seed,
                    session_id=session_id,
                    execution_id=execution_id,
                    completed_levels=level_idx + 1,
                    plan_total_stages=total_levels,
                    execution_plan=execution_plan,
                    dispatch_contract=dispatch_contract,
                    ac_statuses=ac_statuses,
                    failed_indices=failed_indices,
                    satisfied_externally_indices=satisfied_externally_indices,
                    completed_count=completed_count,
                    level_contexts=level_contexts,
                    checkpoint_point=f"level_{level_num}_completed",
                )

            # All levels done — cancel the background progress emitter
            outer_tg.cancel_scope.cancel()

        # Give any deferred correctness-bearing writes (ladder resolution,
        # decomposition attestation — scheduled disproportionately near the
        # END of a run) a bounded final shot BEFORE returning control toward
        # the ``asyncio.run`` boundary, whose teardown cancels all pending
        # tasks. Anything still pending after the timeout is cancelled
        # explicitly so its own cancellation handler logs loudly instead of
        # dying silently at loop teardown. Exceptional exits from this run
        # body are covered too: ``execute_parallel``'s ``finally`` repeats
        # this drain on every exit path (Round-7 Findings #2/#3 follow-up).
        await self._drain_deferred_durable_writes()

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

        # Round-8 finding #3: the drain above ran to completion, so every
        # deferred correctness-bearing write has either landed, exhausted
        # its budget, or been cancelled — the list below is final for this
        # run. A non-empty list means the durable log MAY be missing records
        # whose absence reads as different state on replay/cold-resume; the
        # run must SAY so in its own result rather than reporting an
        # ordinary fully-durable completion.
        unconfirmed_durable_writes = tuple(self._unconfirmed_durable_write_descriptions)
        if unconfirmed_durable_writes:
            log.error(
                "parallel_executor.deferred_durable_writes.unconfirmed_at_completion",
                count=len(unconfirmed_durable_writes),
                writes=list(unconfirmed_durable_writes),
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
            unconfirmed_durable_writes=unconfirmed_durable_writes,
            # Round-10 finding #3 (BLOCKING): ``execution_id`` here is the
            # possibly checkpoint-RESTORED id every AC event above was
            # emitted under. Returning it lets the caller rejoin its own
            # terminal-status / frugality bookkeeping to the SAME aggregate
            # instead of the fresh id it originally passed in.
            execution_id=execution_id,
            # Round-16 finding #4 (BLOCKING): the execution-semantic scalars
            # this run ACTUALLY dispatched under — possibly checkpoint-
            # restored by RC3 recovery, in which case they differ from the
            # values the caller constructed this executor with. Returned
            # for the same reason as ``execution_id`` above: the caller's
            # durable completion summary / verification report must
            # describe what EXECUTED, never its own pre-recovery view.
            effective_parallel_workers=self._max_concurrent,
            max_decomposition_depth=self._max_decomposition_depth,
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
        capsule_success_contract: ACSuccessContract | None = None,
    ) -> ACExecutionResult:
        """Dispatch one finalized split through the shared recursive child path."""
        sub_acs = [child.description for child in decision.children]
        active_success_spec = self._active_success_contract_spec(
            ac_content=ac_content,
            ac_spec=ac_spec,
            capsule_success_contract=capsule_success_contract,
        )
        # A prior round's gate-anchored attestation (Task 1) poisons THIS root
        # AC's future retries: once a decomposition round is proven untrustworthy
        # by re-running the parent's own verify gate, a fresh (unverified) SPLIT
        # proposal on the next retry must not re-admit the cheap child tier.
        # Fix 2 (round 2, BLOCKING): read through the durable-event-backed
        # loader, not the raw in-memory dict, so a fresh executor after a
        # resume/restart reconstructs a prior UNTRUSTWORTHY verdict instead
        # of silently forgetting it and re-admitting the discount.
        attestation_key = self._decomposition_attestation_key(
            decision=decision,
            semantic_ac_key=semantic_ac_key,
            seed_goal=seed_goal,
            ac_spec=active_success_spec,
            execution_id=execution_id,
        )
        prior_attestation = await self._load_decomposition_attestation(
            node_identity.node_id, execution_id=execution_id, session_id=session_id
        )
        if prior_attestation is None:
            prior_attestation = await self._load_reusable_decomposition_attestation(
                attestation_key,
                node_id=node_identity.node_id,
            )
        # Proposal-time structure/semantic checks are not execution evidence.
        # The first decomposition round therefore runs at the parent/base tier;
        # only a durable gate-anchored attestation from an earlier round may
        # authorize the child discount on a later retry.
        child_decomposition_trustworthy = (
            decision.trustworthy and prior_attestation is not None and prior_attestation.trustworthy
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
            and isinstance(active_success_spec, AcceptanceCriterionSpec)
            and bool(active_success_spec.expected_artifacts)
            and len(decision.children) == len(sub_acs)
        )
        artifact_cwd = ""
        child_artifact_slices: tuple[tuple[str, ...], ...] = tuple(() for _ in sub_acs)
        if artifact_oracle_active:
            assert active_success_spec is not None  # narrowed by artifact_oracle_active
            parent_artifact_set = frozenset(active_success_spec.expected_artifacts)
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
                    capsule_success_contract=ACSuccessContract(
                        expected_artifacts=assigned_artifacts
                    ),
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
                    # Round-11 Finding #3 (BLOCKING): file existence alone is
                    # NOT sufficient artifact-axis evidence. A child can write
                    # its assigned artifacts as an EARLY step of its own
                    # dispatch and then fail later, so crediting the axis on
                    # files-on-disk while the child's own dispatch reported
                    # failure would launder a genuinely failed child into a
                    # passing sibling axis — and, combined with a passing
                    # parent-gate re-run, into a false TRUSTWORTHY round
                    # verdict. Credit therefore requires BOTH legs: every
                    # assigned path absent before and present after dispatch,
                    # AND the child's own dispatch reporting success. A failed
                    # dispatch (result.success is False, an exception, or a
                    # missing result) with files present is real, evaluated
                    # NEGATIVE evidence (passed=False, driving the round
                    # UNTRUSTWORTHY), not an unevaluable (False, None) axis:
                    # we do have evidence here — it is just negative.
                    child_result = sub_results[idx]
                    child_dispatch_succeeded = (
                        isinstance(child_result, ACExecutionResult) and child_result.success
                    )
                    if missing_after:
                        child_artifact_attributions[idx] = (
                            True,
                            False,
                            "assigned expected_artifacts missing after dispatch: "
                            + ", ".join(missing_after),
                        )
                    elif not child_dispatch_succeeded:
                        child_artifact_attributions[idx] = (
                            True,
                            False,
                            "child dispatch reported failure despite creating assigned "
                            "artifacts: " + ", ".join(assigned_artifacts),
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
            ac_spec=active_success_spec,
            child_artifact_attributions=tuple(child_artifact_attributions),
        )
        self._decomposition_attestations[node_identity.node_id] = attestation
        # Round-5 Finding #4 (BLOCKING, superseding round 4's log-only stance):
        # a FUTURE resume that replays this node id and finds no matching
        # durable event cannot tell "never attested" from "attested but the
        # write failed" -- both are silence. Silence no longer authorizes the
        # child-tier discount, but losing a correctness-bearing trust verdict
        # would still make later retries needlessly stay at the base tier.
        # Give the
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
            computed_attestation = attestation
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
            # Round-6 Finding #4 (BLOCKING): the in-memory sentinel above
            # protects THIS process, but after a crash the durable log is
            # genuinely EMPTY for this node id — and
            # ``_load_decomposition_attestation`` reads a clean log with no
            # matching event as the legitimate "never attested" miss
            # (``None``), re-authorizing the proposal-trust discount. That
            # is the same "write failed vs never happened" silence problem
            # finding #3 fixed for the ladder's resolution write; reuse its
            # exact convention: keep retrying the ORIGINAL computed
            # attestation at the long parked cadence in the background, and
            # only restore the truthful verdict to the in-memory cache once
            # the durable log actually corroborates it.
            sentinel = attestation

            async def _retry_attestation_write() -> bool:
                # A newer round for this node id has since replaced the
                # sentinel (its own attestation was computed and written):
                # stop retrying — the newer record is authoritative. This
                # check-then-write is inherently racy (the write below runs
                # through ``_safe_emit_event``'s multi-second retry window,
                # during which a newer round's write can land first), so it
                # is only an optimization: the loader's replay selects by
                # HIGHEST persisted ``retry_attempt``, not log order, and
                # stays correct even if this older backfill lands last.
                if self._decomposition_attestations.get(node_identity.node_id) is not sentinel:
                    return True
                persisted = await self._event_emitter.emit_decomposition_attested(
                    execution_id=execution_id,
                    session_id=session_id,
                    node_identity=node_identity,
                    attestation=computed_attestation,
                    retry_attempt=retry_attempt,
                )
                if persisted and (
                    self._decomposition_attestations.get(node_identity.node_id) is sentinel
                ):
                    self._decomposition_attestations[node_identity.node_id] = computed_attestation
                return persisted

            self._schedule_deferred_durable_write(
                write=_retry_attestation_write,
                on_persisted=None,
                log_key="parallel_executor.decomposition_attestation",
                node_id=node_identity.node_id,
                execution_id=execution_id,
            )
        else:
            registry_persisted = (
                await self._event_emitter.emit_decomposition_attestation_registered(
                    attestation_key=attestation_key,
                    execution_id=execution_id,
                    session_id=session_id,
                    node_identity=node_identity,
                    attestation=attestation,
                )
            )
            if registry_persisted:
                self._reusable_decomposition_attestations[attestation_key] = attestation
            else:
                # Reuse is an optimization. Failure must withhold the future
                # discount, never change this round's already-attested result.
                log.warning(
                    "parallel_executor.decomposition_attestation.registry_write_failed",
                    attestation_key=attestation_key,
                    node_id=node_identity.node_id,
                    execution_id=execution_id,
                )

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
        capsule_success_contract: ACSuccessContract | None = None,
    ) -> tuple[ACExecutionResult | None, DecompositionDecisionRecord | None]:
        """Run cause-matched bounce recovery before alternate-harness fallback."""
        # Infra-fatal means the runtime itself failed.  It is neither evidence
        # that the AC is too large nor an input for semantic recovery, so it
        # must bypass the classifier and every decomposition side effect.
        if self._decomposition_mode != "bounce_only" or result.success or result.infra_fatal:
            return None, None
        previous = self._decomposition_decisions.get(node_identity.node_id)
        if previous is not None and previous.source is DecompositionSource.BOUNCE:
            return None, previous

        active_success_spec = self._active_success_contract_spec(
            ac_content=ac_content,
            ac_spec=ac_spec,
            capsule_success_contract=capsule_success_contract,
        )
        trace = self._build_decomposition_trace_summary(
            result=result,
            ac_spec=active_success_spec,
        )
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
            ac_spec=active_success_spec,
            capsule_success_contract=capsule_success_contract,
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
                capsule_success_contract=capsule_success_contract,
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
        force_atomic_execution: bool = False,
        capsule_success_contract: ACSuccessContract | None = None,
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
            force_atomic_execution: Bypass every decomposition branch for one
                root-AC recovery dispatch. Used only after a decomposed failure
                proves that the atomic fallback remains untried.

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
        if (
            not force_atomic_execution
            and self._decomposition_mode == "preflight"
            and depth < self._max_decomposition_depth
        ):
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
                    capsule_success_contract=capsule_success_contract,
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
            not force_atomic_execution
            and node_decision is not None
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
                capsule_success_contract=capsule_success_contract,
            )

        if (
            not force_atomic_execution
            and self._decomposition_mode == "preflight"
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
            not force_atomic_execution
            and self._decomposition_mode == "preflight"
            and depth >= self._max_decomposition_depth
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
            "force_atomic_execution": force_atomic_execution,
            "capsule_success_contract": capsule_success_contract,
        }
        while True:
            try:
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
                    capsule_success_contract=capsule_success_contract,
                )
            except CompletedACExecutionError as completed:
                recovered_verify_outcome = _recovered_verify_gate_outcome(
                    completed.verify_gate_outcome
                )
                if (
                    self._run_verify_commands
                    and isinstance(ac_spec, AcceptanceCriterionSpec)
                    and ac_spec.verify_command
                    and recovered_verify_outcome is None
                ):
                    raise AmbiguousACExecutionError(
                        "Completed AC recovery is missing its non-idempotent verify-command "
                        "outcome; refusing to replay the command"
                    ) from completed
                log.info(
                    "parallel_executor.ac.completed_recovered",
                    ac_index=ac_index,
                    depth=depth,
                    retry_attempt=atomic_retry_attempt,
                )
                return _finalize_node_result(
                    ACExecutionResult(
                        ac_index=ac_index,
                        ac_content=ac_content,
                        success=True,
                        messages=(),
                        final_message=completed.result_summary or "",
                        duration_seconds=(datetime.now(UTC) - start_time).total_seconds(),
                        session_id=completed.session_id,
                        retry_attempt=atomic_retry_attempt,
                        depth=depth,
                        verify_gate_outcome=recovered_verify_outcome,
                        forced_frontier_routing=force_frontier_routing,
                    )
                )
            if atomic_result.error != _STALL_SENTINEL:
                if not atomic_result.success and not force_atomic_execution:
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
                        capsule_success_contract=capsule_success_contract,
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
                if not force_atomic_execution:
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
            context_pack_enabled=self._context_pack_enabled,
            prompt_guidance_contract=self._prompt_guidance_contract,
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

    @staticmethod
    def _active_success_contract_spec(
        *,
        ac_content: str,
        ac_spec: AcceptanceCriterionSpec | None,
        capsule_success_contract: ACSuccessContract | None,
    ) -> AcceptanceCriterionSpec | None:
        """Project the contract owned by the current recursive AC node.

        ``ac_spec`` remains the root Seed record used for semantic identity and
        prompt context. Once a decomposition child receives an explicit capsule
        contract, however, that child-local contract is the only acceptance
        authority its descendants may partition or attest. An explicitly empty
        child contract must therefore stay empty instead of falling back to the
        root AC's broader gate.
        """
        if capsule_success_contract is None:
            return ac_spec
        return AcceptanceCriterionSpec(
            description=ac_content,
            verify_command=capsule_success_contract.verify_command,
            expected_artifacts=capsule_success_contract.expected_artifacts,
            output_assertion=capsule_success_contract.output_assertion,
        )

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
        capsule_success_contract: ACSuccessContract | None = None,
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
        active_success_spec = self._active_success_contract_spec(
            ac_content=ac_content,
            ac_spec=ac_spec,
            capsule_success_contract=capsule_success_contract,
        )
        parent_expected_artifacts: tuple[str, ...] = (
            tuple(active_success_spec.expected_artifacts) if active_success_spec is not None else ()
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

    def _enforced_effort_ceiling(self) -> str | None:
        """Return the strongest reasoning level this runtime can enforce."""
        capabilities = getattr(self._adapter, "capabilities", None)
        if (
            getattr(capabilities, "reasoning_effort_support", ParamSupport.IGNORED)
            is not ParamSupport.NATIVE
        ):
            return None
        enforceable = getattr(capabilities, "enforceable_reasoning_efforts", None)
        if enforceable is None:
            return DEFAULT_EFFORT_CEILING
        # ``EFFORT_LADDER`` is the portable retry vocabulary and deliberately
        # stops at ``xhigh``. Claude also exposes a native ``max`` level; when
        # the live runtime declares it enforceable, forced-frontier dispatch
        # and terminal detection must agree that ``max`` is above ``xhigh``.
        runtime_ceiling_order = (*EFFORT_LADDER, "max")
        return next(
            (level for level in reversed(runtime_ceiling_order) if level in enforceable),
            None,
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
        force_frontier_routing: bool = False,
        capsule_success_contract: ACSuccessContract | None = None,
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
        the strongest level the runtime capability contract can enforce
        (investment cheapening suspended — the ladder's premise is maximum
        strength) and an ACTIVE model axis is anchored at
        ``DEFAULT_TIER_CEILING``; a dormant axis (no base
        effort / no router) stays dormant, preserving the
        no-escalation-dial-configured opt-out.

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
        capsule = compile_ac_execution_capsule(
            runtime_identity=runtime_identity,
            execution_id=execution_context_id,
            semantic_ac_key=semantic_ac_key,
            workspace=(
                self._task_cwd or getattr(self._adapter, "working_directory", None) or os.getcwd()
            ),
            authority_scope=self._build_ac_capsule_authority_scope(
                execution_context_id=execution_context_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                is_sub_ac=is_sub_ac,
                decomposition_trustworthy=decomposition_trustworthy,
                force_frontier_routing=force_frontier_routing,
                investment_spec=investment_spec,
                sibling_acs=sibling_acs,
                retry_prompt_extra=retry_prompt_extra,
            ),
            seed_goal=seed_goal,
            ac_content=ac_content,
            ac_spec=ac_spec,
            success_contract_override=capsule_success_contract,
            level_contexts=tuple(level_contexts or ()),
            dependency_references=self._capsule_dependency_references(
                execution_id=execution_context_id,
                level_contexts=level_contexts,
            ),
        )

        # Build prompt (label/indent, governed task section, success contract,
        # retry/parallel-awareness sections, cwd scan, completion contract).
        prompt_bundle = AtomicPromptBuilder(self).build(
            capsule=capsule,
            ac_index=ac_index,
            is_sub_ac=is_sub_ac,
            parent_ac_index=parent_ac_index,
            sub_ac_index=sub_ac_index,
            node_identity=node_identity,
            level_contexts=level_contexts,
            sibling_acs=sibling_acs,
            retry_prompt_extra=retry_prompt_extra,
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
            expected_capsule_workspace=capsule.workspace,
        )
        if persisted_runtime_handle is not None:
            persisted_runtime_handle = bind_capsule_to_runtime_handle(
                capsule,
                persisted_runtime_handle,
                restored_same_attempt=True,
                expected_backend=getattr(self._adapter, "runtime_backend", None),
                expected_approval_mode=getattr(self._adapter, "permission_mode", None),
            )
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
            restored_same_attempt=persisted_runtime_handle is not None,
            expected_backend=getattr(self._adapter, "runtime_backend", None),
            expected_approval_mode=getattr(self._adapter, "permission_mode", None),
        )
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
        session_origin = (
            "restored_same_attempt" if persisted_runtime_handle is not None else "fresh"
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
                self._enforced_effort_ceiling()
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
        dispatch_id = uuid.uuid4().hex
        previous_dispatch_id: str | None = None
        if runtime_handle is not None:
            previous_dispatch_value = runtime_handle.metadata.get("ac_dispatch_id")
            if previous_dispatch_value is not None:
                previous_dispatch_id = ACRuntimeHandleManager._validate_ac_dispatch_id(
                    previous_dispatch_value
                )
            runtime_handle = replace(
                runtime_handle,
                metadata={**runtime_handle.metadata, "ac_dispatch_id": dispatch_id},
            )
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
        signal_target: SessionSignalTarget | None = None
        signal_target_registered = False
        provider_boundary_persisted = False
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

            await self._event_emitter.emit_ac_attempt_dispatched(
                runtime_identity=runtime_identity,
                dispatch_id=dispatch_id,
                previous_dispatch_id=previous_dispatch_id,
                execution_id=execution_context_id,
                session_id=session_id,
                capsule_fingerprint=capsule.fingerprint,
                session_origin=session_origin,
                runtime_handle=dispatch_state.runtime_handle,
            )
            provider_boundary_persisted = True
            await LeafDispatcher(self).stream(
                state=dispatch_state,
                prompt=prompt,
                tools=tools,
                system_prompt=system_prompt,
                execute_effort_kwargs=execute_effort_kwargs,
                runtime_identity=runtime_identity,
                dispatch_id=dispatch_id,
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
                    forced_frontier_routing=force_frontier_routing,
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
                    previous_provider_boundary_persisted = provider_boundary_persisted
                    provider_boundary_persisted = False
                    signal_dispatch_id = uuid.uuid4().hex
                    previous_runtime_handle = dispatch_state.runtime_handle
                    if previous_runtime_handle is not None:
                        dispatch_state.runtime_handle = replace(
                            previous_runtime_handle,
                            metadata={
                                **previous_runtime_handle.metadata,
                                "ac_dispatch_id": signal_dispatch_id,
                                "ac_session_origin": "restored_same_attempt",
                            },
                        )
                        dispatch_state.runtime_handle = self._remember_ac_runtime_handle(
                            ac_index,
                            dispatch_state.runtime_handle,
                            execution_context_id=execution_context_id,
                            is_sub_ac=is_sub_ac,
                            parent_ac_index=parent_ac_index,
                            sub_ac_index=sub_ac_index,
                            node_identity=node_identity,
                            retry_attempt=retry_attempt,
                        )
                    try:
                        await self._event_emitter.emit_ac_attempt_dispatched(
                            runtime_identity=runtime_identity,
                            dispatch_id=signal_dispatch_id,
                            previous_dispatch_id=dispatch_id,
                            execution_id=execution_context_id,
                            session_id=session_id,
                            capsule_fingerprint=capsule.fingerprint,
                            session_origin="restored_same_attempt",
                            runtime_handle=dispatch_state.runtime_handle,
                        )
                        dispatch_id = signal_dispatch_id
                        provider_boundary_persisted = True
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
                            dispatch_id=signal_dispatch_id,
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
                        if dispatch_id != signal_dispatch_id:
                            dispatch_state.runtime_handle = previous_runtime_handle
                            self._remember_ac_runtime_handle(
                                ac_index,
                                previous_runtime_handle,
                                execution_context_id=execution_context_id,
                                is_sub_ac=is_sub_ac,
                                parent_ac_index=parent_ac_index,
                                sub_ac_index=sub_ac_index,
                                node_identity=node_identity,
                                retry_attempt=retry_attempt,
                            )
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
                            if dispatch_id != signal_dispatch_id:
                                provider_boundary_persisted = previous_provider_boundary_persisted
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
                dispatch_id=dispatch_id,
                ac_content=ac_content,
                runtime_handle=runtime_handle,
                execution_id=execution_context_id,
                session_id=ac_session_id,
                result_summary=result_final_message or None,
                success=success,
                verify_gate_outcome=verify_gate_outcome,
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
                forced_frontier_routing=force_frontier_routing,
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
            if provider_boundary_persisted:
                await self._emit_ac_runtime_event(
                    event_type="execution.session.failed",
                    runtime_identity=runtime_identity,
                    dispatch_id=dispatch_id,
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
                forced_frontier_routing=force_frontier_routing,
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
        retry was finally accepted.

        Round-9 finding #3 (BLOCKING): this write used to be observe-only
        ("if the marker is dropped, the proof fails closed by excluding the
        rows") — true while the frugality proof was its only consumer,
        because a dropped write just meant one fewer counted sample. Rounds
        7-8 made it correctness-bearing for a second consumer: the ladder's
        resume-correlation logic (``finalized_attempts`` in the escalation
        state loader, consumed via
        ``_lateral_escalation_resume_attempt_finalized``) reads this marker
        as authoritative proof that an in-flight persona dispatch already
        completed. If this write silently fails and the process dies before
        any later event lands, a restart finds no record that the attempt
        completed, concludes "still mid-dispatch", and RE-RUNS a persona
        attempt that already ran — duplicating work and re-applying side
        effects. So this write now follows the same durable fail-closed
        convention as the other correctness-bearing writes in this file
        (attestation, ``parked_resolved``, ``lateral_escalation_interrupted``,
        parked backfill): a failed foreground emit (which already carries
        ``_safe_emit_event``'s own bounded retries) schedules a deferred
        background retry via ``_schedule_deferred_durable_write``, whose
        terminal give-up surfaces in the run's ``unconfirmed_durable_writes``.
        """
        from ouroboros.events.base import BaseEvent

        context_summary = self._durable_ac_context_summary(result)

        async def _emit_marker() -> bool:
            return await self._safe_emit_event(
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
                        "forced_frontier_routing": result.forced_frontier_routing,
                        "context_summary": context_summary,
                    },
                )
            )

        if not await _emit_marker():
            log.error(
                "parallel_executor.ac.outcome_finalized_write_failed_correctness_risk",
                root_ac_index=root_ac_index,
                execution_id=execution_id,
                retry_attempt=result.retry_attempt,
            )
            self._schedule_deferred_durable_write(
                write=_emit_marker,
                on_persisted=None,
                log_key="parallel_executor.ac.outcome_finalized",
                root_ac_index=root_ac_index,
                execution_id=execution_id,
                retry_attempt=result.retry_attempt,
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
        if (
            emission_key in self._recovery_exhausted_emitted
            or emission_key in self._recovery_exhausted_pending
        ):
            return

        criterion = seed.acceptance_criteria[root_ac_index]
        semantic_ac_key = criterion.semantic_ac_key or derive_semantic_ac_key(criterion)
        alternate_status = self._alt_harness_status_by_root.get(
            root_ac_index,
            "not_attempted" if self._cross_harness_redispatch_enabled else "not_attempted",
        )
        if alternate_status == "failed":
            retry_termination_reason = "alternate_harness_exhausted"
        event = BaseEvent(
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

        async def _emit_marker() -> bool:
            return await self._safe_emit_event(event)

        if await _emit_marker():
            self._recovery_exhausted_emitted.add(emission_key)
            return

        log.error(
            "parallel_executor.ac.recovery_exhausted_write_failed_correctness_risk",
            root_ac_index=root_ac_index,
            execution_id=execution_id,
            retry_attempt=result.retry_attempt,
        )
        self._recovery_exhausted_pending.add(emission_key)

        def _mark_persisted() -> None:
            self._recovery_exhausted_pending.discard(emission_key)
            self._recovery_exhausted_emitted.add(emission_key)

        task = self._schedule_deferred_durable_write(
            write=_emit_marker,
            on_persisted=_mark_persisted,
            log_key="parallel_executor.ac.recovery_exhausted",
            root_ac_index=root_ac_index,
            execution_id=execution_id,
            retry_attempt=result.retry_attempt,
        )
        # If the bounded background retry gives up or is cancelled, clear the
        # in-flight guard so a later authoritative call can try again.
        task.add_done_callback(lambda _task: self._recovery_exhausted_pending.discard(emission_key))

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
        force_frontier_routing: bool = False,
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

        ``force_frontier_routing`` must describe the dispatch that produced
        ``result``.  Budget exhaustion is not evidence that the last attempt
        actually ran at the ceilings; only ladder-owned dispatches pass
        ``True`` and suspend investment cheapening exactly like the live
        dispatch path.
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
            suggested_tier=(
                DEFAULT_TIER_CEILING
                if force_frontier_routing
                else self._suggested_model_tier_hint()
            ),
        )
        model_ceiling_decision, _model_ceiling_kwargs = resolve_execute_model(
            self._adapter,
            router=self._model_router,
            is_decomposed_child=False,
            retry_attempt=0,
            suggested_tier=DEFAULT_TIER_CEILING,
        )
        effort_ceiling = (
            self._enforced_effort_ceiling() if self._reasoning_effort is not None else None
        )
        effort_decision, _effort_kwargs = resolve_execute_effort(
            self._adapter,
            base_effort=(
                effort_ceiling
                if force_frontier_routing and self._reasoning_effort
                else self._reasoning_effort
            ),
            is_decomposed_child=False,
            retry_attempt=retry_attempt,
            investment_assessment=(None if force_frontier_routing else investment_assessment),
        )
        return is_terminal_state_failure(
            success=result.success,
            is_decomposed=result.is_decomposed,
            # A resolver may calculate a frontier/xhigh *advice* for a
            # runtime whose capability contract says the knob is ignored.
            # Such a value never reaches ``execute_task`` (the kwargs above
            # are empty), so it cannot prove this AC actually failed at a
            # stronger configuration.  Treat unenforced axes as dormant;
            # the ladder may engage only when at least one runtime-enforced
            # axis genuinely ran at its ceiling.
            model_tier=(model_decision.tier if model_decision.is_enforced else None),
            effort_level=(effort_decision.level if effort_decision.is_enforced else None),
            tier_ceiling=(
                model_ceiling_decision.tier
                if model_ceiling_decision.is_enforced and model_ceiling_decision.tier is not None
                else DEFAULT_TIER_CEILING
            ),
            effort_ceiling=effort_ceiling or DEFAULT_EFFORT_CEILING,
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
        retry_attempt), so the matching event with the HIGHEST persisted
        ``retry_attempt`` is authoritative — log order only breaks ties.
        Selecting by raw log position instead ("last write wins") was
        vulnerable to a deferred backfill of an OLDER round landing after a
        NEWER round's foreground write (see the selection loop below).
        No matching event means no attestation
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
        latest_retry_attempt = -1
        for event in events:
            if getattr(event, "type", None) != "execution.ac.decomposition_attested":
                continue
            data = getattr(event, "data", None)
            if not isinstance(data, dict) or data.get("node_id") != node_id:
                continue
            # Adversarial-review Bug #4 (TOCTOU on the deferred backfill):
            # raw last-write-wins BY LOG ORDER is not safe here. Round N's
            # attestation write can fail, get a deferred background backfill
            # scheduled, and then land AFTER round N+1's own foreground
            # write for the SAME node id (the backfill's pre-write
            # supersession check passes before N+1 attests, but its actual
            # write runs through ``_safe_emit_event``'s multi-second retry
            # window) — leaving the OLDER verdict last in the log and
            # durably resurrecting a stale trust verdict on replay. Select
            # by the HIGHEST ``retry_attempt`` instead: the field is the
            # root AC's monotonically-increasing retry index, persisted on
            # every one of these events since it was introduced, so the
            # read side stays correct under ANY out-of-log-order landing.
            # Events predating the field (or carrying a corrupt value) rank
            # as -1: among themselves the ``>=`` keeps plain log order (the
            # pre-fix behavior, exactly), and any event that DOES carry the
            # field outranks them.
            raw_attempt = data.get("retry_attempt")
            attempt = (
                raw_attempt
                if isinstance(raw_attempt, int)
                and not isinstance(raw_attempt, bool)
                and raw_attempt >= 0
                else -1
            )
            if attempt >= latest_retry_attempt:
                latest_retry_attempt = attempt
                latest_data = data

        if latest_data is None:
            # ONLY the genuine "no matching event exists at all" case may
            # return ``None`` -- the caller treats it as "never attested" and
            # withholds the child-tier discount until a gate-anchored verdict
            # exists.
            return None

        attestation = _decomposition_attestation_from_event_data(latest_data)
        if attestation is None:
            # Round-4 Finding #4 (BLOCKING): a matching event WAS found but
            # its payload does not round-trip into a valid attestation.
            # Returning ``None`` here would erase the distinction between a
            # legitimate first round and a corrupt prior verdict. Both paths
            # withhold discount, but corruption must remain loud and
            # explicitly fail-closed rather than masquerading as bootstrap.
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

    @staticmethod
    def _build_decomposition_attestation_scope(
        *,
        seed_id: str,
        seed_fingerprint: str,
        dispatch_contract: Mapping[str, Any],
    ) -> str:
        """Bind reusable trust to the verified Seed, workspace, and authority.

        ``dispatch_contract`` already carries the canonical absolute workspace,
        runtime/capability and permission identity, model router, execution
        profile, tool/tool-schema authority, prompt identity, and reconciled
        context. Any change to those inputs must produce a different registry
        aggregate so workspace-local verification can never authorize cheaper
        routing in another checkout or under different dispatch authority.
        """
        payload = {
            "schema_version": 2,
            "seed_id": seed_id,
            "seed_fingerprint": seed_fingerprint,
            "dispatch_contract": dict(dispatch_contract),
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return f"v2:{hashlib.sha256(canonical).hexdigest()}"

    def _decomposition_attestation_key(
        self,
        *,
        decision: DecompositionDecisionRecord,
        semantic_ac_key: str,
        seed_goal: str,
        ac_spec: AcceptanceCriterionSpec | None,
        execution_id: str,
    ) -> str:
        """Return a stable identity for one reusable semantic split.

        The key excludes execution/node ids and includes every input that can
        change the split or its gate contract. The enclosing ``seed_scope`` is
        already bound to the canonical workspace and complete dispatch
        authority, so a different checkout/runtime/tool/prompt contract cannot
        inherit an older verdict accidentally.
        """
        payload = {
            "schema_version": 2,
            "seed_scope": self._decomposition_attestation_scope or f"execution:{execution_id}",
            "semantic_ac_key": semantic_ac_key,
            "seed_goal": seed_goal,
            "children": [child.to_dict() for child in decision.children],
            "parent_contract": (ac_spec.model_dump(mode="json") if ac_spec is not None else None),
            "execution_profile": (
                serialize_execution_profile(self._execution_profile)
                if self._execution_profile is not None
                else None
            ),
        }
        canonical = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return f"v1:{hashlib.sha256(canonical).hexdigest()}"

    async def _load_reusable_decomposition_attestation(
        self,
        attestation_key: str,
        *,
        node_id: str,
    ) -> DecompositionAttestation | None:
        """Load the latest durable verdict for an identical semantic split."""

        def _for_current_node(attestation: DecompositionAttestation) -> DecompositionAttestation:
            return DecompositionAttestation(
                node_id=node_id,
                verdict=attestation.verdict,
                failed_axis=attestation.failed_axis,
                failed_sibling_id=attestation.failed_sibling_id,
                reason=attestation.reason,
            )

        cached = self._reusable_decomposition_attestations.get(attestation_key)
        if cached is not None:
            return _for_current_node(cached)

        events = await self._replay_with_retry(
            "decomposition_attestation",
            attestation_key,
        )
        if events is None:
            return DecompositionAttestation(
                node_id=node_id,
                verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
                failed_axis=None,
                failed_sibling_id=None,
                reason=(
                    "reusable attestation replay failed after retries; failing closed "
                    "rather than authorizing a child-tier discount"
                ),
            )

        latest_data: Mapping[str, Any] | None = None
        for event in events:
            if getattr(event, "type", None) != "decomposition.attestation.registered":
                continue
            data = getattr(event, "data", None)
            if isinstance(data, Mapping) and data.get("attestation_key") == attestation_key:
                latest_data = data
        if latest_data is None:
            return None

        attestation = _decomposition_attestation_from_event_data(latest_data)
        if attestation is None:
            return DecompositionAttestation(
                node_id=node_id,
                verdict=DecompositionTrustVerdict.UNTRUSTWORTHY,
                failed_axis=None,
                failed_sibling_id=None,
                reason=(
                    "reusable attestation payload is malformed; failing closed "
                    "rather than authorizing a child-tier discount"
                ),
            )
        self._reusable_decomposition_attestations[attestation_key] = attestation
        return _for_current_node(attestation)

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
        * ``execution.ac.lateral_escalation_interrupted`` (Round-6 #2) — the
          NON-SUCCESS terminal ladder exit (redispatch decomposed /
          non-retryable); resets reconstruction to fresh exactly like the
          success resolution, so a later resume never re-enters stale ladder
          state for a terminally-done AC.

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
        latest_progressed_data: dict[str, Any] | None = None
        # Round-7 Finding #4 (extended by round 8): attempt numbers this root
        # AC has a durably FINALIZED outcome for, mapped to that outcome's
        # ``(success, is_decomposed)`` flags. ``execution.ac.outcome_finalized``
        # is emitted right after every ladder dispatch returns (post
        # verify-gate), so its presence for the attempt number the latest
        # ``progressed`` event recorded proves that dispatch COMPLETED — the
        # crash happened after it, not mid-dispatch — and the ``success``
        # value says whether the completed outcome was a real SUCCESS
        # (resume must resolve the episode, never discard it as a failure)
        # or a failure (resume advances past the persona). ``is_decomposed``
        # (round-8 finding #2) distinguishes a failure that stayed atomic
        # (ladder advances to the next persona) from one whose dispatch came
        # back DECOMPOSED — the ladder's established terminal exit for that
        # is ``redispatch_decomposed``, never another persona. Correlated by
        # ``root_ac_index`` + ``retry_attempt`` (the event carries no
        # node_id); an unparseable attempt, success, or is_decomposed value
        # is simply not collected, which degrades to the pre-fix "re-run the
        # in-flight persona" posture rather than skipping a persona that
        # never really ran or misreading a success.
        finalized_attempts: dict[int, tuple[bool, bool]] = {}
        # Round-7 follow-up finding: whether the CURRENT episode's dedicated
        # ``parked_for_operator`` event actually landed. Reset on the same
        # episode-closing events that reset reconstruction, so a prior
        # episode's operator event can never satisfy a later episode's gap.
        parked_operator_event_seen = False
        for event in events:
            event_type = getattr(event, "type", None)
            if event_type == "execution.ac.outcome_finalized":
                data = getattr(event, "data", None)
                if isinstance(data, dict) and data.get("root_ac_index") == ac_idx:
                    raw_finalized_attempt = data.get("retry_attempt")
                    raw_finalized_success = data.get("success")
                    raw_finalized_decomposed = data.get("is_decomposed")
                    if (
                        isinstance(raw_finalized_attempt, int)
                        and not isinstance(raw_finalized_attempt, bool)
                        and raw_finalized_attempt >= 0
                        and isinstance(raw_finalized_success, bool)
                        and isinstance(raw_finalized_decomposed, bool)
                    ):
                        finalized_attempts[raw_finalized_attempt] = (
                            raw_finalized_success,
                            raw_finalized_decomposed,
                        )
                continue
            if event_type not in {
                "execution.ac.parked_for_operator",
                "execution.ac.lateral_escalation_progressed",
                "execution.ac.parked_resolved",
                "execution.ac.lateral_escalation_interrupted",
            }:
                continue
            data = getattr(event, "data", None)
            if not isinstance(data, dict) or data.get("node_id") != node_id:
                continue
            if event_type in {
                "execution.ac.parked_resolved",
                # Round-6 Finding #2: a non-success terminal ladder exit
                # (redispatch decomposed / non-retryable) also ends the
                # episode — reconstruction resets to fresh exactly like the
                # success resolution, so a later resume takes the ordinary
                # path instead of re-entering stale ladder state.
                "execution.ac.lateral_escalation_interrupted",
            }:
                latest_state_data = None
                latest_state_type = None
                latest_progressed_data = None
                parked_operator_event_seen = False
                # Round-8 finding #2: finalized-attempt markers are episode-
                # scoped too. Attempt numbers can restart after a closed
                # episode (``ac_retry_attempts`` reinitializes and the
                # max()-based restore is gated on escalation history
                # EXISTING, which a closed episode resets), so a stale
                # marker from a CLOSED episode could otherwise correlate
                # with a LATER episode's in-flight attempt number and make
                # resume skip a persona whose dispatch never actually ran —
                # treating an untried escalation option as exhausted. Same
                # discipline as the resets above: nothing from a closed
                # episode may satisfy a later episode's gap.
                finalized_attempts.clear()
            else:
                if event_type == "execution.ac.parked_for_operator":
                    parked_operator_event_seen = True
                latest_state_data = data
                latest_state_type = event_type
                # Round-6 Finding #1: the in-flight attempt number lives only
                # on ``progressed`` events (``parked_for_operator`` is always
                # emitted in the same iteration right after one, describing
                # the same step), so track the latest ``progressed`` payload
                # separately for attempt reconstruction below.
                if event_type == "execution.ac.lateral_escalation_progressed":
                    latest_progressed_data = data

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

        # Round-6 Finding #1: reconstruct the in-flight dispatch-attempt
        # number alongside the streak/persona state, from the latest
        # ``progressed`` event for this node id. Wrong-typed values are
        # corruption (the emitter always writes a non-negative int) and fail
        # closed like any other malformed field; a MISSING key is a durable
        # record written before the field existed — restore falls back to
        # the configured-cap behavior for those, which is the pre-fix
        # posture, never worse.
        resume_attempt: int | None = None
        if latest_progressed_data is not None and "retry_attempt" in latest_progressed_data:
            raw_attempt = latest_progressed_data["retry_attempt"]
            if not isinstance(raw_attempt, int) or isinstance(raw_attempt, bool) or raw_attempt < 0:
                log.warning(
                    "parallel_executor.lateral_escalation.malformed_event_data",
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    event_type="execution.ac.lateral_escalation_progressed",
                    reason=f"retry_attempt is not a non-negative int: {raw_attempt!r}",
                )
                return LateralEscalationState(parked=True)
            resume_attempt = raw_attempt
        self._lateral_escalation_resume_attempts[ac_idx] = resume_attempt
        # Round-7 Finding #4 (extended by round 8): the in-flight attempt is
        # only "still in flight" when NO finalized outcome exists for it. A
        # finalized outcome means the dispatch completed before the crash —
        # and its ``success`` value decides HOW resume handles it: a
        # finalized SUCCESS must resolve the episode (never be discarded as
        # a failure), a finalized failure must advance the ladder past that
        # persona instead of re-running it under the same attempt identity.
        self._lateral_escalation_resume_attempt_finalized[ac_idx] = (
            finalized_attempts.get(resume_attempt) if resume_attempt is not None else None
        )
        # Round-7 follow-up finding: durable state says parked but the
        # dedicated operator-notification event never landed (the
        # ``progressed(parked=True)`` write succeeded, the
        # ``parked_for_operator`` write did not, and the process died before
        # the in-process revert-and-retry could re-attempt it). The
        # ``just_parked`` transition edge never re-fires for an
        # already-parked reconstruction, so the resumed path must backfill
        # the event explicitly.
        self._lateral_escalation_parked_event_missing[ac_idx] = (
            state.parked and not parked_operator_event_seen
        )

        self._lateral_escalation_states[ac_idx] = state
        return state

    @staticmethod
    def _escalation_state_has_history(state: LateralEscalationState) -> bool:
        """Whether a (restored) ladder state records an OPEN escalation episode.

        The same three-field test ``_run_batch_with_verify_and_retry`` uses
        to route a batch AC through the resumed-ladder path: any parked
        flag, nonzero terminal-failure streak, or recorded persona means a
        durable ``progressed``/``parked_for_operator`` record exists for
        this AC whose episode has not yet been closed by a terminal event.
        A fresh, never-escalated AC (or one whose episode was already
        resolved/interrupted — reconstruction resets those to fresh) has
        none of the three.
        """
        return bool(state.parked or state.consecutive_terminal_failures > 0 or state.personas_tried)

    def _schedule_deferred_durable_write(
        self,
        *,
        write: Callable[[], Awaitable[bool]],
        on_persisted: Callable[[], None] | None,
        log_key: str,
        **log_context: Any,
    ) -> asyncio.Task[None]:
        """Keep retrying an exhausted correctness-bearing write in the background.

        Round-6 Findings #3/#4 (BLOCKING), same underlying problem for two
        different event types: once a durable write's bounded FOREGROUND
        retries are spent, just logging and moving on leaves the durable log
        permanently missing a record whose absence reads as different state
        on replay ("still parked/escalating" for a resolved ladder episode;
        "never attested" for an attested decomposition round). Instead of
        giving up silently, the write keeps retrying at the long
        operator-visible parked cadence in a background task — bounded at
        :data:`_DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS` so a truly unwritable
        store does not leak a spinning task forever — and only runs
        ``on_persisted`` (e.g. clearing the in-memory state the durable log
        can now corroborate) once the write actually lands. The final
        give-up is loud and leaves any fail-closed in-memory substitute in
        place.

        Round-8 finding #3: a loud LOG is not an operator surface. Every
        terminal non-persisted outcome (attempt budget exhausted, or
        cancellation by the bounded drain/shutdown while still pending) also
        records a description in
        ``self._unconfirmed_durable_write_descriptions``, which aggregation
        copies into the run result's ``unconfirmed_durable_writes`` so the
        uncertainty is visible from the run's own final output.
        """
        description = log_key
        if log_context:
            description += (
                " ("
                + ", ".join(f"{key}={value}" for key, value in sorted(log_context.items()))
                + ")"
            )

        async def _retry_loop() -> None:
            attempts_remaining = _DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS
            try:
                for attempt in range(_DEFERRED_DURABLE_WRITE_MAX_ATTEMPTS):
                    # The FIRST attempt runs immediately — never behind the
                    # full parked cadence. Deferred writes are scheduled
                    # disproportionately near the END of a run (they fire
                    # right after an AC/round completes), and the primary CLI
                    # entrypoint wraps the whole run in ``asyncio.run``,
                    # whose teardown cancels every still-pending task once
                    # the run coroutine returns: a sleep-first loop gave
                    # short runs ZERO real attempts before silent
                    # cancellation. The precondition for reaching here at
                    # all was a handful of just-failed foreground retries
                    # (most plausibly a transient blip), so the immediate
                    # attempt is also the one most likely to succeed.
                    # Subsequent attempts hold the long operator-visible
                    # parked cadence as before.
                    if attempt:
                        await self._sleep(self._parked_retry_backoff_seconds)
                    try:
                        persisted = await write()
                    except Exception as exc:  # noqa: BLE001 - store may be closing down.
                        log.warning(
                            f"{log_key}.deferred_write_error", error=str(exc), **log_context
                        )
                        persisted = False
                    attempts_remaining -= 1
                    if persisted:
                        if on_persisted is not None:
                            on_persisted()
                        log.info(f"{log_key}.deferred_write_recovered", **log_context)
                        return
                self._unconfirmed_durable_write_descriptions.append(description)
                log.error(f"{log_key}.deferred_write_gave_up", **log_context)
            except asyncio.CancelledError:
                # Cancellation (the bounded drain at run completion timing
                # out, or genuine process shutdown) must never be silent: it
                # bypasses both the "recovered" and "gave up" logs above,
                # and the durable log may still be missing this
                # correctness-bearing record.
                self._unconfirmed_durable_write_descriptions.append(description)
                log.warning(
                    f"{log_key}.deferred_write_cancelled",
                    attempts_remaining=attempts_remaining,
                    detail="durable state may be stale",
                    **log_context,
                )
                raise

        task = asyncio.create_task(_retry_loop())
        self._deferred_durable_write_tasks.add(task)
        task.add_done_callback(self._deferred_durable_write_tasks.discard)
        return task

    async def _drain_deferred_durable_writes(self) -> None:
        """Give in-flight deferred durable writes a bounded final shot.

        Called when the run's top-level execution flow completes, BEFORE
        control returns toward the CLI's ``asyncio.run`` boundary — whose
        teardown cancels every still-pending task, and deferred writes are
        scheduled disproportionately near the END of a run. Bounded by
        :data:`_DEFERRED_DURABLE_WRITE_DRAIN_TIMEOUT_SECONDS`: the goal is a
        fair final shot for the in-flight attempt (the immediate first
        attempt in particular), never sitting out the full multi-hour
        parked-cadence budget. Tasks still pending after the timeout are
        cancelled EXPLICITLY here — and then awaited — so ``_retry_loop``'s
        own ``CancelledError`` handler logs the loud "durable state may be
        stale" warning instead of the task dying silently at event-loop
        teardown.
        """
        pending_tasks = {task for task in self._deferred_durable_write_tasks if not task.done()}
        if not pending_tasks:
            return
        log.info(
            "parallel_executor.deferred_durable_writes.draining",
            pending=len(pending_tasks),
            timeout_seconds=_DEFERRED_DURABLE_WRITE_DRAIN_TIMEOUT_SECONDS,
        )
        _done, still_pending = await asyncio.wait(
            pending_tasks, timeout=_DEFERRED_DURABLE_WRITE_DRAIN_TIMEOUT_SECONDS
        )
        for task in still_pending:
            task.cancel()
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)

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
            # Round-6 Finding #3 (BLOCKING, superseding the log-and-move-on
            # stance): clearing the in-memory state while the durable log
            # still says parked/escalating leaves every replay-based
            # projection (board/HUD/conductor) stuck on that stale record
            # forever, with nothing left even trying to fix it. Keep the
            # in-memory state (the live process stays honest about the
            # unresolved durable record) and keep retrying the write at the
            # long parked cadence in the background — only a write that
            # actually LANDS clears the state, exactly as the foreground
            # success path does below.
            log.error(
                "parallel_executor.lateral_escalation.resolved_write_failed_correctness_risk",
                ac_idx=ac_idx,
                execution_id=execution_id,
                node_id=resolved_node_id,
            )

            def _clear_in_memory_state() -> None:
                self._lateral_escalation_states.pop(ac_idx, None)
                self._lateral_escalation_resume_attempts.pop(ac_idx, None)
                self._lateral_escalation_resume_attempt_finalized.pop(ac_idx, None)
                self._lateral_escalation_parked_event_missing.pop(ac_idx, None)

            self._schedule_deferred_durable_write(
                write=lambda: self._event_emitter.emit_ac_parked_resolved(
                    execution_id=execution_id,
                    session_id=session_id,
                    node_id=resolved_node_id,
                    root_ac_index=ac_idx,
                ),
                on_persisted=_clear_in_memory_state,
                log_key="parallel_executor.lateral_escalation.resolved",
                ac_idx=ac_idx,
                execution_id=execution_id,
                node_id=resolved_node_id,
            )
            return
        self._lateral_escalation_states.pop(ac_idx, None)
        self._lateral_escalation_resume_attempts.pop(ac_idx, None)
        self._lateral_escalation_resume_attempt_finalized.pop(ac_idx, None)
        self._lateral_escalation_parked_event_missing.pop(ac_idx, None)

    async def _terminate_escalation_episode(
        self, *, ac_idx: int, execution_id: str, session_id: str, reason: str
    ) -> None:
        """Durably close this root AC's escalation episode on a NON-SUCCESS exit.

        Round-6 Finding #2 (BLOCKING): two ladder exits are terminal but not
        successes — a redispatch that came back decomposed, and a redispatch
        that produced a non-retryable/infra-fatal result. Neither may reuse
        ``parked_resolved`` (Round-5 established it means SUCCESS), but both
        still need SOME durable terminal transition: without one, the node's
        latest replayable escalation record stays ``progressed``, replay
        shows a terminally-done AC as still actively escalating forever, and
        a later resume re-enters stale ladder state (auto-redispatching an
        AC whose last real outcome was, e.g., infra-fatal — contra the
        "infra-fatal fails immediately" mandate) instead of the ordinary
        fresh path.

        Same write-durability convention as ``_resolve_escalation_success``
        (Round-5 Finding #4): a terminal exit cannot be held hostage by a
        failing write, so it gets bounded EXTRA retry rounds on top of
        ``_safe_emit_event``'s own, then a loud correctness-specific error.
        """
        node_id = ExecutionNodeIdentity.root(
            execution_context_id=execution_id, ac_index=ac_idx
        ).node_id
        interrupted_persisted = False
        for extra_attempt in range(3):
            if extra_attempt:
                await self._sleep(min(2.0 * (2**extra_attempt), 10.0))
            interrupted_persisted = await self._event_emitter.emit_lateral_escalation_interrupted(
                execution_id=execution_id,
                session_id=session_id,
                node_id=node_id,
                root_ac_index=ac_idx,
                reason=reason,
            )
            if interrupted_persisted:
                break
        if not interrupted_persisted:
            # Round-6 Finding #3's convention applies to this terminal write
            # too: keep the in-memory state until the durable log can
            # corroborate the episode's end, and keep retrying the write at
            # the long parked cadence in the background.
            log.error(
                "parallel_executor.lateral_escalation.interrupted_write_failed_correctness_risk",
                ac_idx=ac_idx,
                execution_id=execution_id,
                node_id=node_id,
                reason=reason,
            )

            def _clear_in_memory_state() -> None:
                self._lateral_escalation_states.pop(ac_idx, None)
                self._lateral_escalation_resume_attempts.pop(ac_idx, None)
                self._lateral_escalation_resume_attempt_finalized.pop(ac_idx, None)
                self._lateral_escalation_parked_event_missing.pop(ac_idx, None)

            self._schedule_deferred_durable_write(
                write=lambda: self._event_emitter.emit_lateral_escalation_interrupted(
                    execution_id=execution_id,
                    session_id=session_id,
                    node_id=node_id,
                    root_ac_index=ac_idx,
                    reason=reason,
                ),
                on_persisted=_clear_in_memory_state,
                log_key="parallel_executor.lateral_escalation.interrupted",
                ac_idx=ac_idx,
                execution_id=execution_id,
                node_id=node_id,
            )
            return
        self._lateral_escalation_states.pop(ac_idx, None)
        self._lateral_escalation_resume_attempts.pop(ac_idx, None)
        self._lateral_escalation_resume_attempt_finalized.pop(ac_idx, None)
        self._lateral_escalation_parked_event_missing.pop(ac_idx, None)

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
        # Round-7 Finding #4 (extended by round 8): ``progressed`` is written
        # BEFORE its dispatch runs, so the restored "in-flight" attempt may
        # in fact have COMPLETED before the crash — the durably-finalized
        # outcome for exactly that attempt number, when present, says so, and
        # its ``(success, is_decomposed)`` values say WHICH completed outcome
        # the crash interrupted. Consumed (popped) here so a later
        # resolution/termination never sees a stale value.
        in_flight_finalized = self._lateral_escalation_resume_attempt_finalized.pop(ac_idx, None)
        in_flight_finalized_success: bool | None = (
            None if in_flight_finalized is None else in_flight_finalized[0]
        )
        in_flight_finalized_decomposed = in_flight_finalized is not None and in_flight_finalized[1]
        if in_flight_finalized_success is True:
            # The dispatch durably SUCCEEDED; the crash landed in the window
            # between that success and the episode-resolution write (which
            # has bounded retries and a deferred-background fallback — a real
            # window). Treating this like a finalized failure would silently
            # discard a durably-recorded success, corrupt the persona/streak
            # history with a phantom failure, and could even falsely park an
            # AC whose last persona had actually broken through — the exact
            # false-negative direction this PR forbids. Converge instead to
            # the SAME durable outcome a non-crashed success produces: fire
            # the shared resolution transition and report the AC as
            # succeeded, without consuming a persona or re-dispatching. The
            # attempt's own ``outcome_finalized`` marker already landed
            # before the crash, so no new marker is emitted here.
            await self._resolve_escalation_success(
                ac_idx=ac_idx, execution_id=execution_id, session_id=session_id
            )
            return ACExecutionResult(
                ac_index=ac_idx,
                ac_content=ac_content,
                success=True,
                final_message=(
                    "Execution restarted mid-escalation AFTER the in-flight "
                    "attempt's dispatch had already completed with a durably "
                    "finalized SUCCESS; resolving the escalation episode as "
                    "succeeded instead of discarding the recorded success."
                ),
                retry_attempt=ac_retry_attempts[ac_idx],
                outcome=ACExecutionOutcome.SUCCEEDED,
            )
        # Round-7 follow-up finding: the durable log says this AC is parked
        # but its dedicated ``parked_for_operator`` event never landed (the
        # write failed and the process died before the in-process
        # revert-and-retry could re-attempt it; the ``just_parked`` edge
        # never re-fires for an already-parked reconstruction). Backfill it
        # here, before anything else — the AC genuinely IS parked per the
        # durable ``progressed(parked=True)`` record, so this only makes the
        # log say what the reconstruction already concluded. Same durability
        # convention as the other correctness-bearing writes: bounded
        # in-process attempt first, then the shared deferred-write retry
        # mechanism if the store is still refusing.
        if restored_state.parked and self._lateral_escalation_parked_event_missing.pop(
            ac_idx, False
        ):
            backfill_node_id = ExecutionNodeIdentity.root(
                execution_context_id=execution_id, ac_index=ac_idx
            ).node_id
            backfill_reason = (
                "all lateral-thinking personas exhausted; AC still failing "
                "at maximum strength (operator event backfilled on resume)"
            )

            async def _emit_parked_backfill() -> bool:
                return await self._event_emitter.emit_ac_parked_for_operator(
                    execution_id=execution_id,
                    session_id=session_id,
                    node_id=backfill_node_id,
                    root_ac_index=ac_idx,
                    personas_tried=tuple(p.value for p in restored_state.personas_tried),
                    consecutive_terminal_failures=(restored_state.consecutive_terminal_failures),
                    backoff_seconds=self._parked_retry_backoff_seconds,
                    reason=backfill_reason,
                )

            if not await _emit_parked_backfill():
                log.error(
                    "parallel_executor.lateral_escalation.parked_backfill_write_failed",
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    node_id=backfill_node_id,
                )
                self._schedule_deferred_durable_write(
                    write=_emit_parked_backfill,
                    on_persisted=None,
                    log_key="parallel_executor.lateral_escalation.parked_backfill",
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    node_id=backfill_node_id,
                )
        # Round-7 Finding #4: the in-flight attempt completed with a durably
        # finalized FAILURE before the crash — the crash then happened
        # between that dispatch's finalized outcome and the next
        # ``progressed`` write. Re-running it here would repeat an
        # already-tried-and-failed persona under the SAME attempt identity,
        # duplicating work and attempt-scoped telemetry. Skip this pre-ladder
        # redispatch entirely and hand the AC straight to the ladder, which
        # advances PAST the completed step (personas are never repeated once
        # recorded) under a NEW attempt number.
        # Round-8 finding #2: the marker's ``is_decomposed`` flag is
        # preserved on the reconstructed prior result. A finalized dispatch
        # that came back DECOMPOSED must take the ladder's established
        # ``redispatch_decomposed`` terminal exit (the not-engaged branch of
        # ``_maybe_run_lateral_escalation_ladder`` closes the episode
        # durably) — collapsing it to a plain atomic failure would instead
        # advance the ladder and dispatch another persona against an AC
        # whose atomic premise no longer holds.
        if in_flight_finalized_success is False:
            completed_prior = ACExecutionResult(
                ac_index=ac_idx,
                ac_content=ac_content,
                success=False,
                error=(
                    "Execution restarted mid-escalation AFTER the in-flight "
                    "attempt's dispatch had already completed with a durably "
                    "finalized "
                    + (
                        "DECOMPOSED outcome; closing the lateral-escalation "
                        "episode via the decomposed terminal exit instead of "
                        "re-running the attempt or advancing the persona ladder."
                        if in_flight_finalized_decomposed
                        else "failure; advancing the lateral-escalation "
                        "ladder past it instead of re-running the same attempt."
                    )
                ),
                retry_attempt=ac_retry_attempts[ac_idx],
                is_decomposed=in_flight_finalized_decomposed,
            )
            return await self._finalize_batch_ac_with_escalation(
                seed=seed,
                ac_idx=ac_idx,
                result=completed_prior,
                ac_retry_attempts=ac_retry_attempts,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                execution_counters=execution_counters,
                retry_termination_reason="budget_exhausted",
                result_was_forced_frontier=True,
            )
        # Round-11 finding #4 (BLOCKING): reaching this point means the
        # durable log carries a pre-dispatch ``progressed`` record for this
        # AC's in-flight attempt and NO ``outcome_finalized`` marker for it.
        # That state is genuinely AMBIGUOUS: either the crash landed
        # mid-dispatch (re-running is correct and exactly what rounds 5-8
        # built), or the dispatch COMPLETED and its finalize write — plus
        # the deferred background retry round-9 #3 added — was lost with the
        # dying process. No durable discriminator between the two can exist:
        # the same event-store outage that lost the finalize marker would
        # have lost any discriminator written alongside it, and the deferred
        # -write tracker's in-process state died with the process. Blindly
        # refusing to continue would abandon the AC while escalation remains
        # (forbidden); blindly redispatching SILENTLY risks duplicating an
        # already-applied attempt's side effects (also forbidden). The
        # resolution reuses the EXISTING park-for-operator machinery: the
        # redispatch still happens (never give up), but only after (a) a
        # durable, operator-visible ``parked_for_operator`` signal with an
        # ambiguity-specific reason — the same event Kanban/HUD/conductor
        # already surface, so no new wiring — and (b) the parked cadence
        # held BEFORE the dispatch, giving the operator a real window to
        # inspect/cancel. Loud, delayed, and interruptible instead of silent
        # and immediate. For an already-PARKED state the operator signal
        # already exists this episode (``parked_for_operator`` landed, or
        # the backfill above just emitted it) and the parked branch below
        # already sleeps the cadence, so only the reason detail is logged —
        # never a duplicate operator event (the established no-duplication
        # invariant for this episode's operator notification). Side effect
        # accepted deliberately: if ANOTHER crash lands during the delayed
        # redispatch below, replaying this event reconstructs parked=True —
        # the codebase's established fail-closed posture for uncertain
        # escalation state (the same sentinel the loader synthesizes for an
        # unreadable/malformed log): long-backoff retries at maximum
        # strength, never a surfaced FAILED, never a repeated persona.
        ambiguous_resume_attempt = self._lateral_escalation_resume_attempts.get(ac_idx)
        ambiguity_reason = (
            "crash-restart found this AC's in-flight escalation attempt"
            + (
                f" (attempt {ambiguous_resume_attempt})"
                if ambiguous_resume_attempt is not None
                else ""
            )
            + " with no durably finalized outcome: the dispatch may have "
            "completed with its finalize write lost, so the upcoming "
            "redispatch could duplicate already-applied side effects; "
            "verify the workspace or cancel within the parked backoff "
            "window if that work already landed"
        )
        log.warning(
            "parallel_executor.lateral_escalation.ambiguous_completion_resume",
            ac_idx=ac_idx,
            execution_id=execution_id,
            resume_attempt=ambiguous_resume_attempt,
            parked=restored_state.parked,
            backoff_seconds=self._parked_retry_backoff_seconds,
        )
        self._console.print(f"  [yellow]AC {ac_idx + 1}: {ambiguity_reason}[/yellow]")
        if not restored_state.parked:
            ambiguity_node_id = ExecutionNodeIdentity.root(
                execution_context_id=execution_id, ac_index=ac_idx
            ).node_id

            async def _emit_ambiguity_signal() -> bool:
                return await self._event_emitter.emit_ac_parked_for_operator(
                    execution_id=execution_id,
                    session_id=session_id,
                    node_id=ambiguity_node_id,
                    root_ac_index=ac_idx,
                    personas_tried=tuple(p.value for p in restored_state.personas_tried),
                    consecutive_terminal_failures=(restored_state.consecutive_terminal_failures),
                    backoff_seconds=self._parked_retry_backoff_seconds,
                    reason=ambiguity_reason,
                )

            # Round-15 finding #3 (BLOCKING): this write's entire purpose is
            # to be operator-visible BEFORE the possibly-duplicating
            # redispatch — a deferred BACKGROUND retry ("eventually
            # consistent") cannot serve that purpose. The old code scheduled
            # one and then proceeded to the redispatch anyway: if the write
            # had not landed, the operator had NO durable signal to act on
            # during the cadence window, defeating round 11's inspect/cancel
            # design outright. Retry the write SYNCHRONOUSLY instead, at the
            # same parked cadence this path already holds before the
            # redispatch: the AC is HELD — never surfaced FAILED (the
            # mandate forbids that while escalation remains), and never
            # redispatched — until the warning is durably visible or the
            # run is cancelled. This reuses the exact revert-and-hold shape
            # the ladder's ``progressed``/``parked`` writes established
            # (round-5 finding #4): each hold cycle awaits the injectable
            # sleep, so operator cancellation interrupts it like every other
            # parked-cadence loop, and every cycle is loud on both the log
            # and the console. In the common case (the write lands
            # immediately) nothing changes, and the cadence sleep below then
            # starts the operator window from the moment the signal became
            # visible.
            hold_cycles = 0
            while not await _emit_ambiguity_signal():
                hold_cycles += 1
                log.error(
                    "parallel_executor.lateral_escalation.ambiguity_signal_write_failed",
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    node_id=ambiguity_node_id,
                    hold_cycles=hold_cycles,
                    detail=(
                        "holding this AC (no redispatch) until the "
                        "ambiguous-completion warning is durably visible to "
                        "the operator or the run is cancelled"
                    ),
                )
                self._console.print(
                    f"  [red]AC {ac_idx + 1}: could not durably record the "
                    "ambiguous-completion warning; holding this AC (no "
                    "redispatch) and retrying the write at the parked "
                    f"cadence (cycle {hold_cycles})[/red]"
                )
                await self._sleep(self._parked_retry_backoff_seconds)
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
        if not restored_state.parked:
            # Round-11 finding #4: the non-parked branches used to redispatch
            # IMMEDIATELY. Hold the same parked cadence here too — this is
            # the operator-attention window the durable ambiguity signal
            # above just opened, so the possibly-duplicating redispatch is
            # delayed and interruptible instead of instant.
            await self._sleep(self._parked_retry_backoff_seconds)

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
                forced_frontier_routing=True,
            )
        elif not candidate.forced_frontier_routing:
            candidate = replace(candidate, forced_frontier_routing=True)
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
            result_was_forced_frontier=True,
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
        result_was_forced_frontier: bool = False,
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
            # Round-6 review follow-up (Finding #2's THIRD exit): on the
            # RESUMED path, ``_resume_escalated_ac``'s redispatch can come
            # back non-retryable/infra-fatal and flow here BEFORE the loop
            # below ever sets ``engaged`` — so NEITHER in-loop terminal exit
            # (both added for Finding #2) is reached and the durable episode
            # stays a dangling ``progressed`` record. A later cold resume
            # would then reconstruct the stale in-progress ladder and
            # AUTO-REDISPATCH an AC whose actual last outcome was
            # infra-fatal — the exact failure mode the "infra-fatal fails
            # immediately" mandate forbids. If durable escalation history
            # exists for this AC, close the episode before stepping aside; a
            # genuinely fresh AC (no history) has nothing to terminate and
            # must not get a spurious termination event. The loader's
            # fail-closed sentinel (synthetic parked=True on an unreadable
            # log) also routes here: when history CANNOT be read, closing a
            # possibly-nonexistent episode is the safe direction — an
            # ``interrupted`` record with no prior history replays exactly
            # like no episode at all, while a dangling real episode would
            # auto-redispatch after infra-fatal.
            if self._escalation_state_has_history(
                await self._load_lateral_escalation_state(ac_idx, execution_id=execution_id)
            ):
                await self._terminate_escalation_episode(
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    session_id=session_id,
                    reason="not_retryable",
                )
            return None

        current_result = result
        current_result_was_forced_frontier = (
            result_was_forced_frontier or result.forced_frontier_routing
        )
        # Round-9 finding #1 (BLOCKING): the batch counter alone can LAG the
        # attempt number the AC's current result actually ran under —
        # internal stall retries bump ``atomic_retry_attempt`` inside
        # ``_execute_single_ac`` without the batch counter ever seeing it,
        # and an alternate-harness redispatch runs at
        # ``atomic_retry_attempt + 1``. Seeding the ladder from the lagging
        # counter would make its first redispatch (``current_retry_attempt
        # + 1`` below) REUSE an attempt number those mechanisms already
        # consumed: colliding attempt-scoped runtime handles/telemetry, and
        # — worse — letting that earlier attempt's ``outcome_finalized``
        # marker falsely correlate on a later resume as proof that the
        # ladder's persona dispatch under the reused number already
        # completed, skipping a persona that never ran. Start from the
        # highest attempt number ANY mechanism has used for this AC.
        current_retry_attempt = max(ac_retry_attempts[ac_idx], result.retry_attempt)
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
                force_frontier_routing=current_result_was_forced_frontier,
            )
            if not terminal:
                # Ordinary-budget exhaustion authorizes entering the ladder,
                # but it does NOT retroactively make the last weak dispatch a
                # frontier failure.  If the same failure would be terminal
                # under the ladder's real forced routing, perform that
                # frontier dispatch now without advancing the terminal streak.
                # Only its actual result may count as terminal failure #1.
                can_force_frontier = await self._root_ac_terminal_state(
                    seed=seed,
                    ac_idx=ac_idx,
                    result=current_result,
                    retry_attempt=current_retry_attempt,
                    force_frontier_routing=True,
                )
                if (
                    not current_result_was_forced_frontier
                    and not current_result.success
                    and not current_result.is_decomposed
                    and can_force_frontier
                ):
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
                        retry_prompts={
                            ac_idx: self._build_ac_retry_prompt(
                                result=current_result,
                                ac_content=ac_content,
                                is_final_attempt=False,
                            )
                        },
                        same_runtime_budget_exhausted=True,
                        force_frontier_routing=True,
                    )
                    candidate = retry_results[0]
                    if not isinstance(candidate, ACExecutionResult):
                        candidate = ACExecutionResult(
                            ac_index=ac_idx,
                            ac_content=ac_content,
                            success=False,
                            error=str(candidate),
                            retry_attempt=current_retry_attempt,
                            infra_fatal=True,
                            forced_frontier_routing=True,
                        )
                    elif not candidate.forced_frontier_routing:
                        # Real atomic dispatches carry this bit themselves;
                        # normalize mocked/custom runtimes at the orchestration
                        # boundary so durable replay still records the truth.
                        candidate = replace(candidate, forced_frontier_routing=True)
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
                    if candidate.success or not self._is_retryable_failure(candidate):
                        return candidate
                    if candidate.is_decomposed:
                        return candidate
                    current_result = candidate
                    current_result_was_forced_frontier = True
                    engaged = True
                    continue
                if not engaged:
                    # Never entered the ladder at all: success, or no
                    # escalation axis is actively configured (both dormant) --
                    # today's unchanged entry behavior for those cases.
                    # Round-6 review follow-up (Finding #2's RESUMED
                    # decomposed variant): a resumed AC with durable ladder
                    # history whose ``_resume_escalated_ac`` redispatch came
                    # back DECOMPOSED lands here on the very first check
                    # (``is_decomposed`` makes the failure non-terminal
                    # before ``engaged`` is ever set), so the in-loop
                    # decomposed exit below is never reached and the episode
                    # would stay a dangling ``progressed`` record — a later
                    # cold resume would auto-redispatch through the ladder
                    # again. Durably close the episode for a non-success
                    # result whenever escalation history exists; a fresh AC
                    # (no history) keeps today's silent pass-through.
                    if not current_result.success and self._escalation_state_has_history(state):
                        await self._terminate_escalation_episode(
                            ac_idx=ac_idx,
                            execution_id=execution_id,
                            session_id=session_id,
                            reason=(
                                "redispatch_decomposed"
                                if current_result.is_decomposed
                                else "ladder_not_engageable"
                            ),
                        )
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
                # Round-6 Finding #2: this is a terminal ladder exit that is
                # NOT a success — durably close the episode so replay/resume
                # never sees this AC as still "actively escalating".
                await self._terminate_escalation_episode(
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    session_id=session_id,
                    reason="redispatch_decomposed",
                )
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
                # Round-6 Finding #1: the redispatch this event precedes runs
                # under ``current_retry_attempt + 1`` (incremented just before
                # ``_execute_ac_batch`` below). Persisting THAT attempt number
                # lets a cold resume re-enter at the exact in-flight attempt —
                # runtime-handle resumption and frugality telemetry are both
                # attempt-scoped, so restoring the configured cap instead
                # could resume an OLDER attempt's stale handle and
                # double-count its telemetry.
                retry_attempt=current_retry_attempt + 1,
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
                    forced_frontier_routing=True,
                )
            elif not candidate.forced_frontier_routing:
                candidate = replace(candidate, forced_frontier_routing=True)
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
            current_result_was_forced_frontier = True
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
                # Round-6 Finding #2: this is a terminal ladder exit that is
                # NOT a success — durably close the episode so replay/resume
                # never sees this AC as still "actively escalating", and a
                # later resume never auto-redispatches an AC whose last real
                # outcome was non-retryable/infra-fatal.
                await self._terminate_escalation_episode(
                    ac_idx=ac_idx,
                    execution_id=execution_id,
                    session_id=session_id,
                    reason="not_retryable",
                )
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
                if self._escalation_state_has_history(restored):
                    resumed_ladder_states[ac_idx] = restored
                    # Re-enter at the post-budget phase — the durable record
                    # proves this AC already spent its ordinary budget before
                    # the ladder engaged. Never reset to a fresh budget.
                    # Round-6 Finding #1: prefer the ACTUAL in-flight attempt
                    # number the latest ``progressed`` event durably recorded
                    # over the configured cap — runtime handles and frugality
                    # telemetry are attempt-scoped, so resetting to the cap
                    # after multiple ladder attempts could resume an OLDER
                    # attempt's stale handle and double-count its telemetry.
                    # The cap fallback only applies to durable records that
                    # predate the field.
                    persisted_attempt = self._lateral_escalation_resume_attempts.get(ac_idx)
                    ac_retry_attempts[ac_idx] = max(
                        ac_retry_attempts[ac_idx],
                        persisted_attempt
                        if persisted_attempt is not None
                        else self._ac_retry_attempts,
                    )

        position_by_idx = {ac_idx: position for position, ac_idx in enumerate(batch_executable)}
        ordinary_boundary_results = {
            ac_idx: self._ordinary_finalized_resume_results.pop(ac_idx)
            for ac_idx in batch_executable
            if ac_idx not in resumed_ladder_states
            and ac_idx in self._ordinary_finalized_resume_results
        }
        resume_finalized_indices = set(resumed_ladder_states) | set(ordinary_boundary_results)
        fresh_executable = [
            ac_idx for ac_idx in batch_executable if ac_idx not in resume_finalized_indices
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
        for ac_idx, finalized_result in ordinary_boundary_results.items():
            results[position_by_idx[ac_idx]] = await self._finalize_batch_ac_with_escalation(
                seed=seed,
                ac_idx=ac_idx,
                result=finalized_result,
                ac_retry_attempts=ac_retry_attempts,
                session_id=session_id,
                execution_id=execution_id,
                tools=tools,
                tool_catalog=tool_catalog,
                system_prompt=system_prompt,
                level_contexts=level_contexts,
                execution_counters=execution_counters,
                retry_termination_reason="budget_exhausted",
            )

        retry_termination_reasons: dict[int, str] = {}
        # V1 gate on freshly-successful ACs.
        for position, ac_idx in enumerate(batch_executable):
            if ac_idx in resume_finalized_indices:
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
                if ac_idx in resume_finalized_indices:
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
            if ac_idx not in resume_finalized_indices
            and self._is_retryable_failure(results[position])
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
            if ac_idx in resume_finalized_indices:
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
        result_was_forced_frontier: bool = False,
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
        # A decomposed failure has not exercised the root AC's atomic path.
        # Once lateral recovery is explicitly enabled, spend that option before
        # consulting the persona ladder. Keep it behind the same opt-in gate:
        # default-off must preserve the pre-feature dispatch count and must not
        # repeat potentially non-idempotent work at frontier strength.
        if (
            self._lateral_escalation_enabled
            and result.is_decomposed
            and not result.success
            and not result_was_forced_frontier
            and not result.forced_frontier_routing
            and self._is_retryable_failure(result)
        ):
            atomic_retry_attempt = max(ac_retry_attempts[ac_idx], result.retry_attempt) + 1
            ac_retry_attempts[ac_idx] = atomic_retry_attempt
            atomic_results = await self._execute_ac_batch(
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
                        result=result,
                        ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                        is_final_attempt=False,
                    )
                },
                same_runtime_budget_exhausted=True,
                force_frontier_routing=True,
                force_atomic_execution=True,
            )
            atomic_candidate = atomic_results[0]
            if not isinstance(atomic_candidate, ACExecutionResult):
                atomic_candidate = ACExecutionResult(
                    ac_index=ac_idx,
                    ac_content=ac_text(seed.acceptance_criteria[ac_idx]),
                    success=False,
                    error=str(atomic_candidate),
                    retry_attempt=atomic_retry_attempt,
                    infra_fatal=True,
                    forced_frontier_routing=True,
                )
            elif not atomic_candidate.forced_frontier_routing:
                atomic_candidate = replace(atomic_candidate, forced_frontier_routing=True)
            atomic_candidate = await self._apply_verify_gate(
                seed=seed,
                ac_index=ac_idx,
                result=atomic_candidate,
                session_id=session_id,
                execution_id=execution_id,
            )
            await self._emit_ac_outcome_finalized(
                result=atomic_candidate,
                root_ac_index=ac_idx,
                session_id=session_id,
                execution_id=execution_id,
            )
            if atomic_candidate.success:
                return atomic_candidate
            result = atomic_candidate
            result_was_forced_frontier = True

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
            result_was_forced_frontier=result_was_forced_frontier,
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
            retry_termination_reason=(
                "infra_fatal"
                if result.infra_fatal
                else "not_retryable"
                if not self._is_retryable_failure(result)
                else retry_termination_reason
            ),
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
