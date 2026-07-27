"""Tests for ouroboros.orchestrator.failure_taxonomy (RFC v2 #830, PR 6)."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from ouroboros.orchestrator.evidence_schema import (
    BlockerCode,
    EvidenceBlocker,
    EvidenceRecord,
    ValidationResult,
)
from ouroboros.orchestrator.failure_taxonomy import (
    BounceCause,
    FailureClass,
    RecoveryAction,
    classify,
    classify_bounce,
    classify_hard_precondition,
    policy_for,
    policy_for_attempt,
)
from ouroboros.orchestrator.verifier import Attempt, RetryAdmission, VerifierVerdict


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


class TestHardPreconditionClassification:
    @pytest.mark.parametrize(
        ("content", "metadata"),
        [
            ("permission denied while opening the repository", {}),
            ("Access denied by the deployment account", {}),
            ("provider returned unauthorized", {}),
            ("authentication required", {}),
            ("missing required tool terraform", {}),
            ("Missing access to the deployment account", {}),
            ("environment variable is not configured", {}),
            ("", {"failure_class": "BLOCKED"}),
            ("", {"details": {"status": "FORBIDDEN"}}),
            ("", {"errorType": "PermissionDenied"}),
            ("", {"kind": "PermissionDenied"}),
            ("", {"error_code": "MISSING_TOOL"}),
            ("", {"cause": {"kind": "MISSING_TOOL"}}),
            ("", {"reason": "AUTHENTICATION_REQUIRED"}),
            ("", {"details": {"status": "environment_variable_not_configured"}}),
            ("", {"status": 401}),
            ("", {"httpStatusCode": 403}),
            ("", {"response": {"status_code": "401"}}),
        ],
    )
    def test_complete_compatibility_population_is_blocked(
        self,
        content: str,
        metadata: dict[str, object],
    ) -> None:
        assert classify_hard_precondition(content, metadata) is FailureClass.BLOCKED

    @pytest.mark.parametrize(
        "content",
        [
            "permission check passed but evidence is missing",
            "configuration migration returned a transient network error",
            "tool output was incomplete",
            "processed 401 records successfully",
        ],
    )
    def test_nearby_non_preconditions_remain_retryable(self, content: str) -> None:
        assert classify_hard_precondition(content, {}) is None

    def test_oversized_metadata_population_fails_closed(self) -> None:
        metadata: dict[str, object] = {}
        cursor = metadata
        for _ in range(33):
            child: dict[str, object] = {}
            cursor["details"] = child
            cursor = child

        assert classify_hard_precondition("provider failure", metadata) is FailureClass.BLOCKED

    def test_oversized_key_population_fails_closed(self) -> None:
        metadata = {f"unknown_{index}": index for index in range(33)}

        assert classify_hard_precondition("provider failure", metadata) is FailureClass.BLOCKED

    def test_infinite_mapping_population_is_bounded(self) -> None:
        class InfiniteMapping(Mapping[str, object]):
            def __getitem__(self, key: str) -> object:
                return "transient"

            def __iter__(self) -> Iterator[str]:
                index = 0
                while True:
                    yield f"unknown_{index}"
                    index += 1

            def __len__(self) -> int:
                return 2**31

        assert (
            classify_hard_precondition("provider failure", InfiniteMapping())
            is FailureClass.BLOCKED
        )

    def test_mapping_iteration_exception_fails_closed(self) -> None:
        class ExplodingMapping(Mapping[str, object]):
            def __getitem__(self, key: str) -> object:
                return "transient"

            def __iter__(self) -> Iterator[str]:
                raise RuntimeError("provider mapping iteration failed")

            def __len__(self) -> int:
                return 1

        assert (
            classify_hard_precondition("provider failure", ExplodingMapping())
            is FailureClass.BLOCKED
        )

    @pytest.mark.parametrize("status", [400, 404, 408, 409, 429, 500, 503])
    def test_non_authorization_http_statuses_remain_retryable(self, status: int) -> None:
        assert classify_hard_precondition("provider failure", {"status": status}) is None


class TestPolicyTable:
    @pytest.mark.parametrize("cls", list(FailureClass))
    def test_every_class_has_a_policy(self, cls: FailureClass) -> None:
        policy = policy_for(cls)
        assert policy.action in RecoveryAction
        assert policy.rationale

    def test_evidence_missing_retries(self) -> None:
        assert policy_for(FailureClass.EVIDENCE_MISSING).action == RecoveryAction.RETRY

    def test_evidence_form_mismatch_retries(self) -> None:
        assert policy_for(FailureClass.EVIDENCE_FORM_MISMATCH).action == RecoveryAction.RETRY

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


class TestPolicyForAttempt:
    def test_accepted_attempt_has_no_recovery_policy(self) -> None:
        assert policy_for_attempt(_accepted_attempt()) is None

    def test_retry_admission_wins_over_failure_class_policy(self) -> None:
        attempt = _attempt(
            validation=ValidationResult(ok=True),
            verdict=VerifierVerdict(
                passed=False,
                reasons=("unsupported_fact_id: fact_fake is not present",),
                failure_class="FABRICATION_SUSPECTED",
                retry_admission=RetryAdmission.REDISPATCH,
            ),
        )

        assert classify(attempt) == FailureClass.FABRICATION_SUSPECTED
        assert policy_for(FailureClass.FABRICATION_SUSPECTED).action == (
            RecoveryAction.ESCALATE_MODEL
        )
        policy = policy_for_attempt(attempt)
        assert policy is not None
        assert policy.action is RecoveryAction.REDISPATCH
        assert "retry_admission" in policy.rationale

    @pytest.mark.parametrize(
        ("admission", "action"),
        [
            (RetryAdmission.RETRY, RecoveryAction.RETRY),
            (RetryAdmission.REDISPATCH, RecoveryAction.REDISPATCH),
            (RetryAdmission.ESCALATE_MODEL, RecoveryAction.ESCALATE_MODEL),
            (RetryAdmission.ESCALATE_HUMAN, RecoveryAction.ESCALATE_HUMAN),
            (RetryAdmission.BLOCK, RecoveryAction.ESCALATE_HUMAN),
        ],
    )
    def test_retry_admission_maps_to_recovery_policy(
        self,
        admission: RetryAdmission,
        action: RecoveryAction,
    ) -> None:
        attempt = _attempt(
            validation=ValidationResult(ok=True),
            verdict=VerifierVerdict(
                passed=False,
                reasons=("route me",),
                retry_admission=admission,
            ),
        )

        policy = policy_for_attempt(attempt)
        assert policy is not None
        assert policy.action is action

    def test_falls_back_to_classification_when_no_verdict(self) -> None:
        attempt = _attempt(evidence_error="not json")

        policy = policy_for_attempt(attempt)

        assert policy is not None
        assert policy.action is RecoveryAction.RETRY


class TestBounceClassification:
    def test_blocked_maps_to_environment(self) -> None:
        decision = classify_bounce(FailureClass.BLOCKED, RetryAdmission.BLOCK)

        assert decision.cause is BounceCause.ENVIRONMENT
        assert decision.allows_decomposition is False

    def test_fabrication_maps_to_model(self) -> None:
        decision = classify_bounce(
            FailureClass.FABRICATION_SUSPECTED,
            RetryAdmission.ESCALATE_MODEL,
        )

        assert decision.cause is BounceCause.MODEL
        assert decision.allows_decomposition is False

    @pytest.mark.parametrize("failure", [FailureClass.SCOPE_CREEP, FailureClass.STALL])
    def test_ambiguous_redispatch_failure_does_not_imply_too_big(
        self,
        failure: FailureClass,
    ) -> None:
        decision = classify_bounce(failure, RetryAdmission.REDISPATCH)

        assert decision.cause is BounceCause.UNKNOWN
        assert decision.allows_decomposition is False

    @pytest.mark.parametrize(
        ("has_attempt_evidence", "has_remaining_scope"),
        [(False, False), (True, False), (False, True)],
    )
    def test_too_big_fails_closed_without_both_evidence_conditions(
        self,
        has_attempt_evidence: bool,
        has_remaining_scope: bool,
    ) -> None:
        decision = classify_bounce(
            FailureClass.SCOPE_CREEP,
            RetryAdmission.REDISPATCH,
            proposed_cause=BounceCause.TOO_BIG,
            has_attempt_evidence=has_attempt_evidence,
            has_remaining_scope=has_remaining_scope,
        )

        assert decision.cause is BounceCause.UNKNOWN
        assert decision.allows_decomposition is False

    def test_too_big_requires_attempt_evidence_and_remaining_scope(self) -> None:
        decision = classify_bounce(
            FailureClass.SCOPE_CREEP,
            RetryAdmission.REDISPATCH,
            proposed_cause=BounceCause.TOO_BIG,
            proposed_reasons=("The attempt completed one deliverable but left two distinct ones.",),
            evidence_refs=("tool:Edit", "verifier:SCOPE_CREEP"),
            has_attempt_evidence=True,
            has_remaining_scope=True,
        )

        assert decision.cause is BounceCause.TOO_BIG
        assert decision.allows_decomposition is True
        assert decision.evidence_refs == ("tool:Edit", "verifier:SCOPE_CREEP")

    def test_bad_spec_requires_explicit_contract_evidence(self) -> None:
        ungrounded = classify_bounce(
            None,
            None,
            proposed_cause=BounceCause.BAD_SPEC,
        )
        grounded = classify_bounce(
            None,
            None,
            proposed_cause=BounceCause.BAD_SPEC,
            bad_spec_evidence=True,
        )

        assert ungrounded.cause is BounceCause.UNKNOWN
        assert grounded.cause is BounceCause.BAD_SPEC
