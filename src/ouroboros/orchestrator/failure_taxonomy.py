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

from ouroboros.orchestrator.verifier import Attempt


class FailureClass(StrEnum):
    """Domain-agnostic failure taxonomy from shaun0927's H7 sketch (#830).

    Members:
        EVIDENCE_MISSING: Leaf could not emit a parseable / validated
            evidence record (covers both parse errors and H2 rejections).
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

    if attempt.evidence_error is not None:
        return FailureClass.EVIDENCE_MISSING

    if attempt.validation is not None and not attempt.validation.ok:
        return FailureClass.EVIDENCE_MISSING

    return FailureClass.STALL


__all__ = [
    "FailureClass",
    "RecoveryAction",
    "RecoveryPolicy",
    "classify",
    "policy_for",
]
