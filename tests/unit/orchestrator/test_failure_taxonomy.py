"""Tests for ouroboros.orchestrator.failure_taxonomy (RFC v2 #830, PR 6)."""

from __future__ import annotations

import pytest

from ouroboros.orchestrator.evidence_schema import (
    BlockerCode,
    EvidenceBlocker,
    EvidenceRecord,
    ValidationResult,
)
from ouroboros.orchestrator.failure_taxonomy import (
    FailureClass,
    RecoveryAction,
    classify,
    policy_for,
)
from ouroboros.orchestrator.verifier import Attempt, VerifierVerdict


def _accepted_attempt() -> Attempt:
    return Attempt(
        leaf_output="{}",
        record=EvidenceRecord(data={}),
        evidence_error=None,
        validation=ValidationResult(ok=True),
        verdict=VerifierVerdict(passed=True),
    )


def _attempt(
    *,
    evidence_error: str | None = None,
    validation: ValidationResult | None = None,
    verdict: VerifierVerdict | None = None,
) -> Attempt:
    return Attempt(
        leaf_output="raw",
        record=None if evidence_error else EvidenceRecord(data={}),
        evidence_error=evidence_error,
        validation=validation,
        verdict=verdict,
    )


class TestClassify:
    def test_accepted_returns_none(self) -> None:
        assert classify(_accepted_attempt()) is None

    def test_verifier_class_wins(self) -> None:
        attempt = _attempt(
            validation=ValidationResult(ok=True),
            verdict=VerifierVerdict(
                passed=False,
                reasons=("bad",),
                failure_class="FABRICATION_SUSPECTED",
            ),
        )
        assert classify(attempt) == FailureClass.FABRICATION_SUSPECTED

    def test_unknown_verifier_class_degrades_to_stall(self) -> None:
        # PR3 round 2 (#884) now rejects unknown failure_class at
        # VerifierVerdict construction time, so the public API can no
        # longer reach this path. We still keep `classify` defensive
        # against bypassed construction (e.g. deserialization or future
        # impls that hand-build the Attempt) — bypass __post_init__ via
        # object.__setattr__ on the frozen dataclass to prove the
        # defensive degradation still works.
        verdict = VerifierVerdict(passed=False, reasons=("?",), failure_class="STALL")
        # Bypass the frozen guard to inject an out-of-taxonomy tag.
        object.__setattr__(verdict, "failure_class", "UNREGISTERED")
        attempt = _attempt(
            validation=ValidationResult(ok=True),
            verdict=verdict,
        )
        assert classify(attempt) == FailureClass.STALL

    def test_evidence_parse_error_maps_to_evidence_missing(self) -> None:
        attempt = _attempt(evidence_error="not json")
        assert classify(attempt) == FailureClass.EVIDENCE_MISSING

    def test_evidence_validation_error_maps_to_evidence_missing(self) -> None:
        attempt = _attempt()
        object.__setattr__(attempt, "validation_error", "blocker.reason missing")
        assert classify(attempt) == FailureClass.EVIDENCE_MISSING

    def test_validation_failure_maps_to_evidence_missing(self) -> None:
        attempt = _attempt(
            validation=ValidationResult(ok=False, missing_fields=("tests_passed",), rejected_by=()),
        )
        assert classify(attempt) == FailureClass.EVIDENCE_MISSING

    def test_typed_blocked_validation_maps_to_blocked(self) -> None:
        attempt = _attempt(
            validation=ValidationResult(
                ok=False,
                blocker=EvidenceBlocker(
                    code=BlockerCode.MISSING_ACCESS,
                    reason="repository token is missing",
                ),
            ),
        )
        assert classify(attempt) == FailureClass.BLOCKED

    def test_unattributed_failure_maps_to_stall(self) -> None:
        attempt = _attempt(
            validation=ValidationResult(ok=True),
            verdict=VerifierVerdict(passed=False, reasons=("vibes",)),
        )
        assert classify(attempt) == FailureClass.STALL

    def test_verifier_class_overrides_validation_failure(self) -> None:
        # If both are present, trust the verifier — it has the richer view.
        attempt = _attempt(
            validation=ValidationResult(ok=False, missing_fields=("x",), rejected_by=()),
            verdict=VerifierVerdict(
                passed=False,
                reasons=("scope drift",),
                failure_class="SCOPE_CREEP",
            ),
        )
        assert classify(attempt) == FailureClass.SCOPE_CREEP


class TestPolicyTable:
    @pytest.mark.parametrize("cls", list(FailureClass))
    def test_every_class_has_a_policy(self, cls: FailureClass) -> None:
        policy = policy_for(cls)
        assert policy.action in RecoveryAction
        assert policy.rationale

    def test_evidence_missing_retries(self) -> None:
        assert policy_for(FailureClass.EVIDENCE_MISSING).action == RecoveryAction.RETRY

    def test_fabrication_escalates_model(self) -> None:
        assert (
            policy_for(FailureClass.FABRICATION_SUSPECTED).action == RecoveryAction.ESCALATE_MODEL
        )

    def test_scope_creep_redispatches(self) -> None:
        assert policy_for(FailureClass.SCOPE_CREEP).action == RecoveryAction.REDISPATCH

    def test_stall_redispatches(self) -> None:
        assert policy_for(FailureClass.STALL).action == RecoveryAction.REDISPATCH

    def test_blocked_escalates_to_human(self) -> None:
        assert policy_for(FailureClass.BLOCKED).action == RecoveryAction.ESCALATE_HUMAN
