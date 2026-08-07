"""Assertion-safe retry hints derived from harness and worker traces."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ouroboros.core.seed import AcceptanceCriterionSpec
from ouroboros.harness.journal import EvidenceKind, EvidenceManifest
from ouroboros.orchestrator.contract_redaction import redact_hidden_contract_values
from ouroboros.orchestrator.decomposition_policy import redact_and_truncate_text
from ouroboros.orchestrator.parallel_executor_models import ACExecutionResult

if TYPE_CHECKING:
    from ouroboros.orchestrator.parallel_executor import _VerifyGateOutcome


_MAX_HINT_CHARS = 4_000
_MAX_TRACE_FACTS = 12


def _sanitize_fragment(text: str, spec: AcceptanceCriterionSpec | None) -> str:
    """Redact secrets and hidden contract values without discarding clean facts."""

    sanitized = (
        redact_hidden_contract_values(text, (spec.verify_command, spec.output_assertion))
        if spec is not None
        else text
    )
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
