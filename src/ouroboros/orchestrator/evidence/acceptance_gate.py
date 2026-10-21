"""The fat-harness acceptance decision of one atomic leaf, and whether it decides.

``fat_harness_acceptance_error`` is the leaf's evidence gate: the verify gate
(the Seed's own ``verify_command``), then the typed evidence check and the
separate verifier pass (the transcript verifier,
``orchestrator/evidence/verification.py``). It returns the rejection reason,
or ``None`` when the leaf passes.

``execution.transcript_verifier`` says what a rejection of the typed evidence
check or the verifier pass does (``split_leaf_rejection``):

- ``decide``: it fails the attempt, which drives the executor's retries;
- ``record`` (the default): it is kept as data only (the result's
  ``recorded_rejection`` and the typed-evidence event's ``would_have_rejected``)
  and the attempt keeps its runtime outcome, so no retry, redispatch or bounce
  follows from it.

A failing ``verify_command`` decides in both modes: it is the Seed's own
check, not the verifier's judgement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import structlog

if TYPE_CHECKING:
    from ouroboros.orchestrator.evidence_schema import EvidenceRecord, ValidationResult
    from ouroboros.orchestrator.verifier import VerifierVerdict

log = structlog.get_logger(__name__)

TranscriptVerifierMode = Literal["decide", "record"]


def transcript_verifier_mode(value: str) -> TranscriptVerifierMode:
    """``value`` when it is ``decide`` or ``record``; ``ValueError`` otherwise."""
    if value == "decide" or value == "record":
        return value
    raise ValueError(f"transcript_verifier must be decide or record: {value!r}")


def fat_harness_acceptance_error(
    *,
    fat_harness_mode: bool,
    execution_profile: object | None,
    runtime_success: bool,
    typed_evidence: EvidenceRecord | None,
    typed_validation: ValidationResult | None,
    typed_error: str | None,
    verifier_verdict: VerifierVerdict | None,
    verify_gate_outcome: Any | None = None,
    verify_gate_replaces_all_evidence: bool = False,
) -> str | None:
    """Return the fat-harness rejection reason for an atomic leaf."""
    if not fat_harness_mode or not runtime_success:
        return None
    if verify_gate_outcome is not None:
        if verify_gate_outcome.environment_unverifiable:
            return None
        if not verify_gate_outcome.passed:
            return f"Verify gate failed: {verify_gate_outcome.reason}"
        if verify_gate_replaces_all_evidence:
            return None
    if execution_profile is None:
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


def split_leaf_rejection(
    error: str | None,
    *,
    mode: TranscriptVerifierMode,
    verify_gate_outcome: Any | None,
    gated: bool,
    **log_fields: Any,
) -> tuple[str | None, str | None, str | None]:
    """``(deciding error, recorded rejection, legacy rejection)`` of one leaf.

    ``error`` is ``fat_harness_acceptance_error``'s. In ``record`` mode a
    rejection that is not the verify gate's is only recorded. A deciding
    rejection becomes the advisory ``legacy_rejection`` when the check package
    gate is installed (``gated``), which then decides whether it fails the
    attempt (``boundary/authority.py``).
    """
    verify_gate_failed = bool(
        verify_gate_outcome is not None
        and not verify_gate_outcome.environment_unverifiable
        and not verify_gate_outcome.passed
    )
    if error is not None and mode == "record" and not verify_gate_failed:
        log.info("parallel_executor.ac.verifier_rejection_recorded", reason=error, **log_fields)
        return None, error, None
    if error is not None and gated:
        return None, None, error
    return error, None, None


def verifier_mode_fields(
    mode: TranscriptVerifierMode, *, enforced: bool, would_have_rejected: str | None
) -> dict[str, Any]:
    """The typed-evidence event fields that say whether the verifier decided."""
    return {
        "enforced": enforced and mode == "decide",
        "transcript_verifier": mode,
        "would_have_rejected": would_have_rejected,
    }


__all__ = [
    "TranscriptVerifierMode",
    "fat_harness_acceptance_error",
    "split_leaf_rejection",
    "transcript_verifier_mode",
    "verifier_mode_fields",
]
