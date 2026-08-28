"""Assertion-safe retry hints derived from harness and worker traces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.harness.deliver_gate import load_ac_evidence_manifest
from ouroboros.harness.journal import EvidenceKind, EvidenceManifest
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.contract_redaction import (
    contains_transformed_hidden_contract_value,
    contains_unsupported_terminal_control,
    redact_hidden_contract_values,
)
from ouroboros.orchestrator.decomposition_policy import redact_and_truncate_text
from ouroboros.orchestrator.evidence.runtime_metadata import _STALL_SENTINEL
from ouroboros.orchestrator.execution_runtime_scope import build_ac_runtime_identity
from ouroboros.orchestrator.failure_taxonomy import FailureClass, classify_hard_precondition
from ouroboros.orchestrator.parallel_executor_models import ACExecutionOutcome, ACExecutionResult
from ouroboros.resilience.lateral import build_lateral_change_of_approach_directive

if TYPE_CHECKING:
    from ouroboros.orchestrator.parallel_executor import _VerifyGateOutcome


_MAX_HINT_CHARS = 4_000
_MAX_TRACE_FACTS = 12
log = get_logger(__name__)


def _sanitize_fragment(text: str, spec: AcceptanceCriterionSpec | None) -> str:
    """Redact exact values and drop fragments carrying transformed variants."""

    if spec is None:
        return redact_and_truncate_text(text, max_chars=_MAX_HINT_CHARS)
    if contains_unsupported_terminal_control(text):
        return ""
    hidden_values = (spec.verify_command, spec.output_assertion)
    if contains_transformed_hidden_contract_value(text, hidden_values):
        return ""
    sanitized = redact_hidden_contract_values(text, hidden_values)
    return redact_and_truncate_text(sanitized, max_chars=_MAX_HINT_CHARS)


def _entry_fact(entry: Any) -> str | None:
    payload = entry.payload
    if not isinstance(payload, Mapping):
        return None
    status = "succeeded" if entry.ok is True else "failed" if entry.ok is False else "pending"
    preview = payload.get("args_preview")
    if not isinstance(preview, str) or not preview.strip():
        preview = payload.get("tool_name")
    if not isinstance(preview, str) or not preview.strip():
        return None
    if entry.kind is EvidenceKind.COMMAND_EXECUTED:
        return f"Command observed ({status}): {preview.strip()}"
    if entry.kind is EvidenceKind.FILE_MODIFIED:
        return f"File operation observed ({status}): {preview.strip()}"
    return None


def _manifest_facts(
    manifest: EvidenceManifest | None,
    spec: AcceptanceCriterionSpec | None,
) -> tuple[str, ...]:
    if manifest is None:
        return ()
    facts: list[str] = []
    for entry in manifest.entries:
        fact = _entry_fact(entry)
        if fact is None:
            continue
        sanitized = _sanitize_fragment(fact, spec)
        if sanitized:
            facts.append(sanitized)
        if len(facts) >= _MAX_TRACE_FACTS:
            break
    return tuple(facts)


def build_assertion_safe_retry_hint(
    *,
    outcome: _VerifyGateOutcome | None,
    result: ACExecutionResult,
    manifest: EvidenceManifest | None,
    spec: AcceptanceCriterionSpec | None,
) -> str:
    """Build a deterministic hint without exposing harness grading inputs."""

    sections: list[str] = []
    if outcome is not None:
        observations: list[str] = []
        if outcome.missing_artifacts:
            observations.append(
                "Missing required artifacts: " + ", ".join(outcome.missing_artifacts)
            )
        if outcome.reason:
            observations.append("Harness verification failed: " + outcome.reason)
        if outcome.workspace_mutated:
            observations.append("The workspace changed while harness verification was running.")
        if observations:
            sections.append(
                "### Harness observations\n"
                + "\n".join(f"- {_sanitize_fragment(item, spec)}" for item in observations)
            )
        if outcome.output_tail:
            output_tail = _sanitize_fragment(outcome.output_tail, spec)
            if output_tail:
                sections.append("### Harness verification output (tail)\n" + output_tail)

    facts = _manifest_facts(manifest, spec)
    if facts:
        sections.append("### Observed work trace\n" + "\n".join(f"- {fact}" for fact in facts))

    if outcome is None:
        fallback = result.error or result.final_message or ""
        if fallback:
            sanitized = _sanitize_fragment(fallback, spec)
            if sanitized:
                sections.append("### Last error (tail)\n" + sanitized[-500:])

    return redact_and_truncate_text("\n\n".join(sections), max_chars=_MAX_HINT_CHARS)


def build_ac_retry_prompt(
    *,
    failure_class: str | None,
    outcome: _VerifyGateOutcome | None,
    result: ACExecutionResult,
    ac_content: str,
    is_final_attempt: bool,
    manifest: EvidenceManifest | None = None,
    spec: AcceptanceCriterionSpec | None = None,
) -> str:
    """Build assertion-safe retry coaching for one re-dispatched AC."""

    parts: list[str] = []
    if failure_class:
        parts.append(f"### Prior failure classification\n{failure_class}")
    if result.error != _STALL_SENTINEL:
        hint = build_assertion_safe_retry_hint(
            outcome=outcome,
            result=result,
            manifest=manifest,
            spec=spec,
        )
        if hint:
            parts.append(hint)
    if is_final_attempt:
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


def failure_class_for_result(result: ACExecutionResult) -> str | None:
    """Return the best available failure taxonomy label for an AC result."""

    if result.outcome is ACExecutionOutcome.BLOCKED:
        return FailureClass.BLOCKED.value
    for message in reversed(result.messages):
        if not (message.is_final and message.is_error):
            continue
        hard_precondition = classify_hard_precondition(message.content, message.data)
        if hard_precondition is not None:
            return hard_precondition.value
    hard_precondition = classify_hard_precondition(
        " ".join(part for part in (result.error, result.final_message) if part)
    )
    if hard_precondition is not None:
        return hard_precondition.value
    verdict = result.atomic_verifier_verdict
    if verdict is not None and verdict.failure_class:
        return verdict.failure_class
    if result.error == _STALL_SENTINEL:
        return FailureClass.STALL.value
    return None


def is_retryable_failure(result: ACExecutionResult | BaseException) -> bool:
    """Return whether a batch result is a runnable non-stall AC failure."""

    return (
        isinstance(result, ACExecutionResult)
        and not result.success
        and not result.is_blocked
        and not result.is_invalid
        and result.error != _STALL_SENTINEL
    )


async def load_ac_retry_manifest(
    event_store: Any,
    *,
    ac_index: int,
    execution_id: str,
) -> EvidenceManifest | None:
    """Best-effort read-only evidence load for retry coaching."""

    try:
        identity = build_ac_runtime_identity(
            ac_index,
            execution_context_id=execution_id,
            retry_attempt=0,
        )
        return await load_ac_evidence_manifest(
            event_store,
            ac_id=identity.ac_id,
            execution_id=execution_id,
        )
    except Exception as exc:
        log.warning(
            "parallel_executor.ac.retry_hint_manifest_unavailable",
            ac_index=ac_index,
            execution_id=execution_id,
            error=str(exc),
        )
        return None
