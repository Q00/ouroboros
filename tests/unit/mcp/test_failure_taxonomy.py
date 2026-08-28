"""Tests for privacy-safe workflow failure classification."""

from ouroboros.mcp.failure_taxonomy import (
    FailureReasonCode,
    RecoveryAction,
    classify_failure,
)


def test_success_is_not_a_failure() -> None:
    assert classify_failure("completed", {}) is None


def test_machine_metadata_maps_without_reading_raw_error() -> None:
    resolution = classify_failure(
        "failed",
        {
            "failure_reason_code": "auth",
            "error": "private prompt and path must not matter",
        },
    )

    assert resolution is not None
    assert resolution.reason_code is FailureReasonCode.AUTH
    assert resolution.recovery_action is RecoveryAction.LOGIN


def test_timeout_status_metadata_maps_to_retry() -> None:
    resolution = classify_failure("failed", {"evaluation_status": "timed_out"})

    assert resolution is not None
    assert resolution.reason_code is FailureReasonCode.TIMEOUT
    assert resolution.recovery_action is RecoveryAction.RETRY


def test_unhashable_metadata_value_fails_closed_to_unknown() -> None:
    resolution = classify_failure("failed", {"status": {"private": "value"}})

    assert resolution is not None
    assert resolution.reason_code is FailureReasonCode.UNKNOWN
    assert resolution.recovery_action is RecoveryAction.INSPECT_LOGS


def test_ralph_timeout_stop_reason_maps_to_retry() -> None:
    resolution = classify_failure("failed", {"stop_reason": "iteration_timeout"})

    assert resolution is not None
    assert resolution.reason_code is FailureReasonCode.TIMEOUT
    assert resolution.recovery_action is RecoveryAction.RETRY


def test_ralph_quality_guard_maps_to_validation() -> None:
    resolution = classify_failure("failed", {"action": "oscillation_detected"})

    assert resolution is not None
    assert resolution.reason_code is FailureReasonCode.VALIDATION


def test_ralph_generation_budget_is_not_a_timeout() -> None:
    resolution = classify_failure("failed", {"stop_reason": "max_generations reached"})

    assert resolution is not None
    assert resolution.reason_code is FailureReasonCode.VALIDATION
    assert resolution.recovery_action is RecoveryAction.INSPECT_LOGS
