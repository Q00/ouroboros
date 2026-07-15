"""Event-contract tests for Ouroboros Synapse."""

from __future__ import annotations

import pytest

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalSource,
)
from ouroboros.events.session_signal import (
    create_session_signal_accepted_event,
    create_session_signal_applied_event,
    create_session_signal_completed_event,
    create_session_signal_delivery_started_event,
    create_session_signal_delivery_uncertain_event,
    create_session_signal_queued_event,
    create_session_signal_rejected_event,
    create_session_signal_requested_event,
)


def _signal() -> SessionSignal:
    return SessionSignal(
        signal_id="sig_1",
        target_session_scope_id="exec_1_ac_2",
        target_session_attempt_id="exec_1_ac_2_attempt_1",
        expected_execution_id="exec_1",
        mode=SessionSignalMode.REDIRECT,
        fallback_mode=SessionSignalMode.AFTER_TURN,
        message="Use the clarified interaction while preserving the AC.",
        source=SessionSignalSource.USER,
        reason="User clarification.",
        idempotency_key="turn_7_ac_2",
    )


def test_requested_event_is_bounded_signal_aggregate() -> None:
    signal = _signal()
    event = create_session_signal_requested_event(
        signal,
        job_id="job_1",
        orchestrator_session_id="orch_1",
    )

    assert event.type == "control.session.signal.requested"
    assert event.aggregate_type == "session_signal"
    assert event.aggregate_id == "sig_1"
    assert event.data["state"] == "requested"
    assert event.data["is_terminal"] is False
    assert event.data["message"] == signal.message
    assert event.data["message_digest"] == signal.message_digest
    assert event.data["job_id"] == "job_1"
    assert "raw_payload" not in event.data


def test_accepted_event_records_effective_mode_and_capability_snapshot() -> None:
    event = create_session_signal_accepted_event(
        _signal(),
        effective_mode=SessionSignalMode.AFTER_TURN,
        capabilities=SessionSignalCapabilities(after_turn_delivery=True),
        runtime_backend="codex_mcp",
    )

    assert event.data["effective_mode"] == "after_turn"
    assert event.data["capabilities"]["after_turn_delivery"] is True
    assert event.data["capabilities"]["checkpoint_redirect"] is False
    assert event.data["runtime_backend"] == "codex_mcp"
    assert "message" not in event.data


def test_queued_does_not_claim_application() -> None:
    event = create_session_signal_queued_event(
        _signal(),
        effective_mode=SessionSignalMode.AFTER_TURN,
    )

    assert event.type.endswith(".queued")
    assert event.data["state"] == "queued"
    assert "acknowledgement" not in event.data


def test_delivery_started_is_non_terminal_and_disables_automatic_retry() -> None:
    event = create_session_signal_delivery_started_event(
        _signal(),
        effective_mode=SessionSignalMode.AFTER_TURN,
    )

    assert event.type.endswith(".delivering")
    assert event.data["is_terminal"] is False
    assert event.data["automatic_retry_allowed"] is False


def test_applied_and_completed_have_distinct_terminality() -> None:
    signal = _signal()
    applied = create_session_signal_applied_event(
        signal,
        effective_mode=SessionSignalMode.REDIRECT,
        acknowledgement="checkpoint:before_next_model_decision",
    )
    completed = create_session_signal_completed_event(
        signal,
        effective_mode=SessionSignalMode.REDIRECT,
        summary="The target acknowledged the redirect.",
        reply="The confirmation copy was updated.",
    )

    assert applied.data["is_terminal"] is False
    assert completed.data["is_terminal"] is True
    assert completed.data["reply"] == "The confirmation copy was updated."


def test_rejected_and_uncertain_are_terminal() -> None:
    signal = _signal()
    rejected = create_session_signal_rejected_event(
        signal,
        rejection_code="stale_attempt",
        detail="The target attempt has already been replaced.",
    )
    uncertain = create_session_signal_delivery_uncertain_event(
        signal,
        effective_mode=SessionSignalMode.REDIRECT,
        detail="Provider acknowledgement was lost during process exit.",
    )

    assert rejected.data["is_terminal"] is True
    assert uncertain.data["is_terminal"] is True
    assert uncertain.data["automatic_retry_allowed"] is False


def test_event_detail_bounds_fail_closed() -> None:
    with pytest.raises(ValueError, match="1000 UTF-8 bytes"):
        create_session_signal_rejected_event(
            _signal(),
            rejection_code="invalid",
            detail="€" * 334,
        )
