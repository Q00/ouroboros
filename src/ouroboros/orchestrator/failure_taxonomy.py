"""Failure classifier + recovery policy (RFC v2 H7, #830).

H7 replaces the count-based retry in `parallel_executor` with a
classifier: every failed leaf attempt is mapped to a FailureClass, and
each class maps to a RecoveryPolicy that the orchestrator can act on.

Currently `retry_attempt` in parallel_executor is a stall counter — it
re-dispatches the same prompt with no notion of *why* the previous
attempt failed. After PR 9 wires this module in, the harness will
inspect the verifier's Attempt transcript, classify it, and route to
the right recovery (retry / escalate model / redispatch / human).

This module ships the classifier + policy table only. parallel_executor
stays count-based until the integration PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ouroboros.orchestrator.verifier import Attempt, RetryAdmission


class FailureClass(StrEnum):
    """Domain-agnostic failure taxonomy from shaun0927's H7 sketch (#830).

    Members:
        EVIDENCE_MISSING: Leaf could not emit a parseable / validated
            evidence record (covers both parse errors and H2 rejections).
        EVIDENCE_FORM_MISMATCH: Leaf executed related work, but the evidence
            shape cannot prove it under the contract (for example an
            unprotected output-filter pipeline for a test command).
        FABRICATION_SUSPECTED: Verifier flagged claims about files,
            symbols, or sources that do not exist. Verifier sets this
            via VerifierVerdict.failure_class.
        SCOPE_CREEP: Leaf's restatement / output drifted away from the
            AC. Verifier-classified.
        STALL: Verifier failed for an unclassified reason and the next
            retry is unlikely to help (e.g. the leaf keeps repeating
            itself). Verifier-classified or fallback for unrecognised
            tags.
        BLOCKED: Leaf surfaced a hard precondition it could not satisfy
            (missing tool, missing access, env variable). Verifier-
            classified.
    """

    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_FORM_MISMATCH = "EVIDENCE_FORM_MISMATCH"
    FABRICATION_SUSPECTED = "FABRICATION_SUSPECTED"
    SCOPE_CREEP = "SCOPE_CREEP"
    STALL = "STALL"
    BLOCKED = "BLOCKED"


class RecoveryAction(StrEnum):
    """What the orchestrator should do next after a classified failure."""

    RETRY = "RETRY"  # same dispatch, with the verifier's feedback.
    ESCALATE_MODEL = "ESCALATE_MODEL"  # rerun on a higher model tier.
    REDISPATCH = "REDISPATCH"  # discard and split the AC again.
    ESCALATE_HUMAN = "ESCALATE_HUMAN"  # surface to the operator.


@dataclass(frozen=True)
class RecoveryPolicy:
    """Recovery action plus a one-line rationale for logging."""

    action: RecoveryAction
    rationale: str


_POLICY_TABLE: dict[FailureClass, RecoveryPolicy] = {
    FailureClass.EVIDENCE_MISSING: RecoveryPolicy(
        action=RecoveryAction.RETRY,
        rationale=(
            "Leaf failed to emit a parseable evidence record; the "
            "verifier feedback already names the missing/rejected fields."
        ),
    ),
    FailureClass.EVIDENCE_FORM_MISMATCH: RecoveryPolicy(
        action=RecoveryAction.RETRY,
        rationale=(
            "Leaf ran related work, but its evidence shape cannot prove the "
            "claim; retry with contract-compliant evidence such as pipefail "
            "for output-filtered test commands."
        ),
    ),
    FailureClass.FABRICATION_SUSPECTED: RecoveryPolicy(
        action=RecoveryAction.ESCALATE_MODEL,
        rationale=(
            "Lower-tier leaf invented references; escalate to a tier "
            "whose self-grounding is stronger before retrying."
        ),
    ),
    FailureClass.SCOPE_CREEP: RecoveryPolicy(
        action=RecoveryAction.REDISPATCH,
        rationale=(
            "Leaf's interpretation drifted; the AC needs to be split "
            "further so each sub-AC names a single concrete deliverable."
        ),
    ),
    FailureClass.STALL: RecoveryPolicy(
        action=RecoveryAction.REDISPATCH,
        rationale=(
            "Repeat retries on the same prompt are unlikely to help; "
            "redispatch with a sharper sub-AC."
        ),
    ),
    FailureClass.BLOCKED: RecoveryPolicy(
        action=RecoveryAction.ESCALATE_HUMAN,
        rationale=(
            "Leaf reported a hard precondition the harness cannot "
            "satisfy automatically (missing tool / access / config)."
        ),
    ),
}


def policy_for(failure: FailureClass) -> RecoveryPolicy:
    """Return the canonical recovery policy for a failure class."""
    try:
        return _POLICY_TABLE[failure]
    except KeyError as exc:  # defensive — StrEnum makes this nearly unreachable.
        msg = f"No recovery policy registered for {failure!r}"
        raise ValueError(msg) from exc


def policy_for_attempt(attempt: Attempt) -> RecoveryPolicy | None:
    """Return the recovery policy for an Attempt.

    Prefer explicit verifier ``retry_admission`` when present. ``failure_class``
    remains a useful taxonomy label, but deliver-gate routes can intentionally
    diverge from the old class-to-policy table (for example fabricated evidence
    that should redispatch before model escalation). Callers that need an action
    should use this helper rather than classifying and then calling
    :func:`policy_for` themselves.
    """
    if attempt.accepted:
        return None
    if attempt.verdict is not None:
        policy = _policy_for_retry_admission(attempt.verdict.retry_admission)
        if policy is not None:
            return policy
    failure = classify(attempt)
    return policy_for(failure) if failure is not None else None


def _policy_for_retry_admission(
    retry_admission: RetryAdmission,
) -> RecoveryPolicy | None:
    if retry_admission is RetryAdmission.ACCEPT:
        return None
    if retry_admission is RetryAdmission.RETRY:
        return RecoveryPolicy(
            action=RecoveryAction.RETRY,
            rationale="Verifier retry_admission explicitly requested same-leaf retry.",
        )
    if retry_admission is RetryAdmission.REDISPATCH:
        return RecoveryPolicy(
            action=RecoveryAction.REDISPATCH,
            rationale="Verifier retry_admission explicitly requested redispatch.",
        )
    if retry_admission is RetryAdmission.ESCALATE_MODEL:
        return RecoveryPolicy(
            action=RecoveryAction.ESCALATE_MODEL,
            rationale="Verifier retry_admission explicitly requested model escalation.",
        )
    if retry_admission is RetryAdmission.ESCALATE_HUMAN:
        return RecoveryPolicy(
            action=RecoveryAction.ESCALATE_HUMAN,
            rationale="Verifier retry_admission explicitly requested human escalation.",
        )
    if retry_admission is RetryAdmission.BLOCK:
        return RecoveryPolicy(
            action=RecoveryAction.ESCALATE_HUMAN,
            rationale="Verifier retry_admission reported a hard block.",
        )
    return None


def classify(attempt: Attempt) -> FailureClass | None:
    """Classify a single Attempt from the verifier loop.

    Returns:
        None when the attempt was accepted; otherwise a FailureClass.

    Precedence (most specific first):
        1. Verifier-supplied verdict.failure_class wins — the verifier
           has the richest view of the leaf output.
        2. Evidence parse failure or H2 validation failure both map to
           EVIDENCE_MISSING.
        3. Unattributed verifier FAILs fall through to STALL.
    """
    if attempt.accepted:
        return None

    if attempt.verdict is not None and attempt.verdict.failure_class:
        raw = attempt.verdict.failure_class
        try:
            return FailureClass(raw)
        except ValueError:
            # Unknown tags from upstream verifiers degrade to STALL
            # rather than crashing the orchestrator.
            return FailureClass.STALL

    if attempt.evidence_error is not None or attempt.validation_error is not None:
        return FailureClass.EVIDENCE_MISSING

    if attempt.validation is not None and attempt.validation.blocker is not None:
        return FailureClass.BLOCKED

    if attempt.validation is not None and not attempt.validation.ok:
        return FailureClass.EVIDENCE_MISSING

    return FailureClass.STALL


__all__ = [
    "FailureClass",
    "RecoveryAction",
    "RecoveryPolicy",
    "classify",
    "policy_for",
    "policy_for_attempt",
]
