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

from ouroboros.orchestrator.decomposition_policy import BounceCause
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
    # Cross-harness recovery (PR-X): re-dispatch the SAME AC on a *different*
    # runtime backend. Unlike ESCALATE_MODEL (higher tier, same runtime) or
    # REDISPATCH (re-split, same runtime), this is the meta-harness move no
    # single-vendor harness can make — it swaps the vendor, not the tier or the
    # decomposition. Wired via the runtime picker + parallel-executor hook.
    REDISPATCH_ALT_HARNESS = "REDISPATCH_ALT_HARNESS"


@dataclass(frozen=True)
class RecoveryPolicy:
    """Recovery action plus a one-line rationale for logging."""

    action: RecoveryAction
    rationale: str


@dataclass(frozen=True, slots=True)
class BounceClassification:
    """Cause-matched recovery classification for one failed execution attempt."""

    cause: BounceCause
    rationale: str
    evidence_refs: tuple[str, ...] = ()

    @property
    def allows_decomposition(self) -> bool:
        """Whether this classification may enter the decomposition path."""
        return self.cause is BounceCause.TOO_BIG


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


_ALT_HARNESS_STALL_RATIONALE = (
    "Same-runtime stall retries are exhausted; the failure looks runtime-specific "
    "(the leaf keeps stalling on this backend), so hand the same AC to a different "
    "harness before abandoning it."
)
_ALT_HARNESS_FABRICATION_RATIONALE = (
    "This backend produced fabricated references; a different harness with different "
    "grounding is more likely to succeed on the same AC than another same-runtime try."
)
_ALT_HARNESS_TRANSIENT_RATIONALE = (
    "Provider transient retries (sustained 429/529) are exhausted on this backend; "
    "a different harness sidesteps the overloaded provider entirely."
)


def alt_harness_policy(
    failure: FailureClass | None,
    *,
    stall_retries_exhausted: bool = False,
    transient_exhausted: bool = False,
) -> RecoveryPolicy | None:
    """Return a cross-harness redispatch policy when the failure warrants it.

    This is the PR-X extension of the recovery vocabulary. It is intentionally a
    *separate* decision layer rather than a mutation of :data:`_POLICY_TABLE`:
    the class→policy table stays the canonical same-runtime routing (fabrication
    still escalates the model tier on the same runtime by default), and this
    function is consulted only at the terminal AC-failure site, once the
    same-runtime recovery budget is spent.

    ``REDISPATCH_ALT_HARNESS`` is returned for the three conditions PR-X targets:

    * ``FABRICATION_SUSPECTED`` — a different harness's grounding may not fabricate.
    * ``STALL`` once ``stall_retries_exhausted`` — the stall looks runtime-specific.
    * ``transient_exhausted`` — sustained 429/529 after providers-retry gives up,
      regardless of failure class, since another harness avoids that provider.

    Returns ``None`` when none apply, so the caller keeps today's failure path.
    """
    if transient_exhausted:
        return RecoveryPolicy(
            action=RecoveryAction.REDISPATCH_ALT_HARNESS,
            rationale=_ALT_HARNESS_TRANSIENT_RATIONALE,
        )
    if failure is FailureClass.FABRICATION_SUSPECTED:
        return RecoveryPolicy(
            action=RecoveryAction.REDISPATCH_ALT_HARNESS,
            rationale=_ALT_HARNESS_FABRICATION_RATIONALE,
        )
    if failure is FailureClass.STALL and stall_retries_exhausted:
        return RecoveryPolicy(
            action=RecoveryAction.REDISPATCH_ALT_HARNESS,
            rationale=_ALT_HARNESS_STALL_RATIONALE,
        )
    return None


def classify_bounce(
    failure: FailureClass | str | None,
    retry_admission: RetryAdmission | str | None,
    *,
    proposed_cause: BounceCause | str | None = None,
    proposed_reasons: tuple[str, ...] = (),
    evidence_refs: tuple[str, ...] = (),
    has_attempt_evidence: bool = False,
    has_remaining_scope: bool = False,
    bad_spec_evidence: bool = False,
) -> BounceClassification:
    """Classify why a failed unit bounced without conflating recovery taxonomies.

    Existing ``FailureClass`` and ``RetryAdmission`` remain authoritative for
    deterministic environment/model routes. ``TOO_BIG`` is admitted only when an
    external classifier proposes it *and* the caller proves both observed attempt
    evidence and remaining parent scope. Ambiguous scope/stall failures therefore
    fail closed to ``UNKNOWN`` instead of triggering decomposition by themselves.
    """

    normalized_failure = _normalize_failure_class(failure)
    normalized_admission = _normalize_retry_admission(retry_admission)
    normalized_proposal = _normalize_bounce_cause(proposed_cause)
    refs = tuple(ref for ref in evidence_refs if isinstance(ref, str) and ref.strip())
    reasons = tuple(reason for reason in proposed_reasons if isinstance(reason, str) and reason)

    if normalized_admission in {RetryAdmission.BLOCK, RetryAdmission.ESCALATE_HUMAN} or (
        normalized_failure is FailureClass.BLOCKED
    ):
        return BounceClassification(
            cause=BounceCause.ENVIRONMENT,
            rationale=(
                reasons[0] if reasons else "Execution is blocked by an environment precondition."
            ),
            evidence_refs=refs,
        )

    if normalized_admission is RetryAdmission.ESCALATE_MODEL or (
        normalized_failure is FailureClass.FABRICATION_SUSPECTED
    ):
        return BounceClassification(
            cause=BounceCause.MODEL,
            rationale=(reasons[0] if reasons else "The failure requires stronger model grounding."),
            evidence_refs=refs,
        )

    if bad_spec_evidence or normalized_proposal is BounceCause.BAD_SPEC:
        if bad_spec_evidence:
            return BounceClassification(
                cause=BounceCause.BAD_SPEC,
                rationale=(
                    reasons[0] if reasons else "The success contract is invalid or contradictory."
                ),
                evidence_refs=refs,
            )
        return BounceClassification(
            cause=BounceCause.UNKNOWN,
            rationale="BAD_SPEC was proposed without explicit contract evidence.",
            evidence_refs=refs,
        )

    if normalized_proposal is BounceCause.TOO_BIG:
        if has_attempt_evidence and has_remaining_scope:
            return BounceClassification(
                cause=BounceCause.TOO_BIG,
                rationale=(
                    reasons[0]
                    if reasons
                    else "Attempt evidence shows distinct parent scope remains."
                ),
                evidence_refs=refs,
            )
        return BounceClassification(
            cause=BounceCause.UNKNOWN,
            rationale="TOO_BIG requires both observed attempt evidence and remaining scope.",
            evidence_refs=refs,
        )

    if normalized_proposal in {BounceCause.ENVIRONMENT, BounceCause.MODEL}:
        return BounceClassification(
            cause=normalized_proposal,
            rationale=(
                reasons[0]
                if reasons
                else "External classifier identified a non-decomposition recovery cause."
            ),
            evidence_refs=refs,
        )

    return BounceClassification(
        cause=BounceCause.UNKNOWN,
        rationale=(
            reasons[0]
            if reasons
            else "The failure does not have enough evidence for cause-matched decomposition."
        ),
        evidence_refs=refs,
    )


def _normalize_failure_class(value: FailureClass | str | None) -> FailureClass | None:
    if isinstance(value, FailureClass):
        return value
    if isinstance(value, str):
        try:
            return FailureClass(value)
        except ValueError:
            return None
    return None


def _normalize_retry_admission(
    value: RetryAdmission | str | None,
) -> RetryAdmission | None:
    if isinstance(value, RetryAdmission):
        return value
    if isinstance(value, str):
        try:
            return RetryAdmission(value)
        except ValueError:
            return None
    return None


def _normalize_bounce_cause(value: BounceCause | str | None) -> BounceCause | None:
    if isinstance(value, BounceCause):
        return value
    if isinstance(value, str):
        try:
            return BounceCause(value)
        except ValueError:
            return None
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
    "BounceClassification",
    "BounceCause",
    "FailureClass",
    "RecoveryAction",
    "RecoveryPolicy",
    "alt_harness_policy",
    "classify",
    "classify_bounce",
    "policy_for",
    "policy_for_attempt",
]
