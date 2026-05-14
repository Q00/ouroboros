"""Tests for the #925 runtime transition boundary contract."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.core.runtime_transition import (
    RUNTIME_TRANSITION_SCHEMA_VERSION,
    RuntimeFailureClass,
    RuntimeScope,
    RuntimeTransition,
    RuntimeTransitionActor,
    RuntimeTransitionDecision,
    RuntimeTransitionFailureKind,
    RuntimeTransitionResult,
    evaluate_runtime_transition,
)

_ALLOWED = (
    ("pending", "running"),
    ("running", "blocked"),
    ("blocked", "running"),
    ("running", "completed"),
)


def _transition(**overrides: object) -> RuntimeTransition:
    values = {
        "runtime_scope": RuntimeScope.MCP_JOB,
        "subject_id": "job-123",
        "from_state": "pending",
        "to_state": "running",
        "reason": "job claimed by worker",
        "actor": RuntimeTransitionActor.HARNESS,
        "expected_revision": 7,
        "evidence_refs": ("event://job-123/claimed",),
        "timestamp": datetime(2026, 5, 15, tzinfo=UTC),
    }
    values.update(overrides)
    return RuntimeTransition(**values)  # type: ignore[arg-type]


def test_transition_event_data_is_bounded_and_audit_ready() -> None:
    transition = _transition(metadata={"status_ref": "job://job-123"})

    data = transition.to_event_data()

    assert data["schema_version"] == RUNTIME_TRANSITION_SCHEMA_VERSION
    assert data["runtime_scope"] == "mcp_job"
    assert data["subject_id"] == "job-123"
    assert data["from_state"] == "pending"
    assert data["to_state"] == "running"
    assert data["actor"] == "harness"
    assert data["expected_revision"] == 7
    assert data["evidence_refs"] == ["event://job-123/claimed"]
    assert data["metadata"] == {"status_ref": "job://job-123"}
    assert transition.aggregate_id == "mcp_job:job-123"


def test_accepts_allowed_transition_with_matching_revision_and_evidence() -> None:
    result = evaluate_runtime_transition(
        _transition(),
        current_state="pending",
        allowed_transitions=_ALLOWED,
        terminal_states=("completed", "failed", "cancelled"),
        current_revision=7,
        require_evidence=True,
    )

    assert result.accepted is True
    assert result.decision is RuntimeTransitionDecision.ACCEPTED
    assert result.to_event_data()["decision"] == "accepted"


def test_stale_revision_is_retryable_and_does_not_accept() -> None:
    result = evaluate_runtime_transition(
        _transition(expected_revision=6),
        current_state="pending",
        allowed_transitions=_ALLOWED,
        current_revision=7,
    )

    assert result.accepted is False
    assert result.failure_class is RuntimeFailureClass.RETRYABLE
    assert result.failure_kind is RuntimeTransitionFailureKind.STALE_REVISION
    assert result.to_event_data()["current_revision"] == 7


def test_expected_revision_without_current_revision_fails_closed() -> None:
    result = evaluate_runtime_transition(
        _transition(expected_revision=7),
        current_state="pending",
        allowed_transitions=_ALLOWED,
        current_revision=None,
    )

    assert result.accepted is False
    assert result.failure_class is RuntimeFailureClass.RETRYABLE
    assert result.failure_kind is RuntimeTransitionFailureKind.STALE_REVISION
    assert "requires current_revision" in result.message


def test_result_rejects_non_enum_failure_data() -> None:
    with pytest.raises(TypeError, match="failure_class must be a RuntimeFailureClass"):
        RuntimeTransitionResult(
            transition=_transition(),
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class="retryable",  # type: ignore[arg-type]
            failure_kind=RuntimeTransitionFailureKind.STALE_REVISION,
        )

    with pytest.raises(TypeError, match="failure_kind must be a RuntimeTransitionFailureKind"):
        RuntimeTransitionResult(
            transition=_transition(),
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.RETRYABLE,
            failure_kind="stale_revision",  # type: ignore[arg-type]
        )


def test_result_rejects_invalid_current_revision_type() -> None:
    with pytest.raises(TypeError, match="current_revision must be an int"):
        RuntimeTransitionResult(
            transition=_transition(),
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.RETRYABLE,
            failure_kind=RuntimeTransitionFailureKind.STALE_REVISION,
            current_revision="7",  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError, match="current_revision must be an int"):
        RuntimeTransitionResult(
            transition=_transition(),
            decision=RuntimeTransitionDecision.REJECTED,
            failure_class=RuntimeFailureClass.RETRYABLE,
            failure_kind=RuntimeTransitionFailureKind.STALE_REVISION,
            current_revision=True,  # type: ignore[arg-type]
        )


def test_invalid_current_revision_is_retryable_rejection_not_crash() -> None:
    result = evaluate_runtime_transition(
        _transition(expected_revision=7),
        current_state="pending",
        allowed_transitions=_ALLOWED,
        current_revision="7",  # type: ignore[arg-type]
    )

    assert result.accepted is False
    assert result.failure_class is RuntimeFailureClass.RETRYABLE
    assert result.failure_kind is RuntimeTransitionFailureKind.STALE_REVISION
    assert "current_revision must be an int" in result.message


def test_from_state_mismatch_is_retryable_snapshot_drift() -> None:
    result = evaluate_runtime_transition(
        _transition(from_state="pending", expected_revision=None),
        current_state="running",
        allowed_transitions=_ALLOWED,
        current_revision=8,
    )

    assert result.failure_class is RuntimeFailureClass.RETRYABLE
    assert result.failure_kind is RuntimeTransitionFailureKind.INVALID_STATE
    assert result.current_state == "running"


def test_terminal_current_state_is_terminal_rejection() -> None:
    result = evaluate_runtime_transition(
        _transition(from_state="completed", to_state="running", expected_revision=None),
        current_state="completed",
        allowed_transitions=_ALLOWED,
        terminal_states=("completed", "failed", "cancelled"),
        current_revision=9,
    )

    assert result.failure_class is RuntimeFailureClass.TERMINAL
    assert result.failure_kind is RuntimeTransitionFailureKind.TERMINAL_STATE


def test_terminal_states_rejects_plain_string_iterable() -> None:
    with pytest.raises(TypeError, match="terminal_states must be an iterable of strings"):
        evaluate_runtime_transition(
            _transition(from_state="running", to_state="blocked", expected_revision=None),
            current_state="completed",
            allowed_transitions=_ALLOWED,
            terminal_states="completed",
            current_revision=9,
        )


def test_terminal_current_state_wins_over_from_state_drift() -> None:
    result = evaluate_runtime_transition(
        _transition(from_state="running", to_state="blocked", expected_revision=None),
        current_state="completed",
        allowed_transitions=_ALLOWED,
        terminal_states=("completed", "failed", "cancelled"),
        current_revision=9,
    )

    assert result.failure_class is RuntimeFailureClass.TERMINAL
    assert result.failure_kind is RuntimeTransitionFailureKind.TERMINAL_STATE
    assert result.current_state == "completed"


def test_disallowed_transition_is_blocking_scope_error() -> None:
    result = evaluate_runtime_transition(
        _transition(from_state="pending", to_state="completed", expected_revision=None),
        current_state="pending",
        allowed_transitions=_ALLOWED,
        current_revision=7,
    )

    assert result.failure_class is RuntimeFailureClass.BLOCKING
    assert result.failure_kind is RuntimeTransitionFailureKind.INVALID_SCOPE


def test_missing_required_evidence_is_blocking() -> None:
    result = evaluate_runtime_transition(
        _transition(evidence_refs=(), expected_revision=None),
        current_state="pending",
        allowed_transitions=_ALLOWED,
        current_revision=7,
        require_evidence=True,
    )

    assert result.failure_class is RuntimeFailureClass.BLOCKING
    assert result.failure_kind is RuntimeTransitionFailureKind.MISSING_EVIDENCE


def test_validation_rejects_noop_duplicate_evidence_and_secret_metadata() -> None:
    with pytest.raises(ValueError, match="from_state and to_state must differ"):
        _transition(from_state="running", to_state="running")

    with pytest.raises(ValueError, match="must be unique"):
        _transition(evidence_refs=("event://same", "event://same"))

    with pytest.raises(TypeError, match="iterable of strings"):
        _transition(evidence_refs="event://job-123/claimed")

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"nested": {"api_key": "secret"}})

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"db_passwd": "secret"})

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"passwd": "secret"})

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"apiKey": "secret"})

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"clientSecret": "secret"})

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"dbPassword": "secret"})

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"db.password": "secret"})

    with pytest.raises(ValueError, match="secret-like key"):
        _transition(metadata={"db/password": "secret"})
