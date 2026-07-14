"""Replay and authority tests for the Synapse lifecycle projection."""

from __future__ import annotations

import pytest

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
)
from ouroboros.core.session_signal_projection import (
    can_supersede_session_signal,
    project_session_signal,
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
        target_session_scope_id="scope_1",
        target_session_attempt_id="scope_1_attempt_1",
        expected_execution_id="exec_1",
        mode=SessionSignalMode.REDIRECT,
        fallback_mode=SessionSignalMode.AFTER_TURN,
        message="Apply the clarified local intent.",
        source=SessionSignalSource.USER,
        reason="User clarification.",
        idempotency_key="intent_1",
    )


def _accepted_flow() -> tuple[SessionSignal, list]:
    signal = _signal()
    events = [
        create_session_signal_requested_event(signal),
        create_session_signal_accepted_event(
            signal,
            effective_mode=SessionSignalMode.REDIRECT,
            capabilities=SessionSignalCapabilities(checkpoint_redirect=True),
        ),
        create_session_signal_queued_event(
            signal,
            effective_mode=SessionSignalMode.REDIRECT,
        ),
    ]
    return signal, events


def test_happy_path_projects_to_completed() -> None:
    signal, events = _accepted_flow()
    events.extend(
        [
            create_session_signal_applied_event(
                signal,
                effective_mode=SessionSignalMode.REDIRECT,
                acknowledgement="checkpoint:next_decision",
            ),
            create_session_signal_completed_event(
                signal,
                effective_mode=SessionSignalMode.REDIRECT,
                summary="Redirect acknowledged.",
            ),
        ]
    )

    projection = project_session_signal(events)

    assert projection.state is SessionSignalState.COMPLETED


def test_delivery_claim_and_bounded_reply_are_projected() -> None:
    signal, events = _accepted_flow()
    events.extend(
        [
            create_session_signal_delivery_started_event(
                signal,
                effective_mode=SessionSignalMode.REDIRECT,
            ),
            create_session_signal_applied_event(
                signal,
                effective_mode=SessionSignalMode.REDIRECT,
                acknowledgement="checkpoint:next_decision",
            ),
            create_session_signal_completed_event(
                signal,
                effective_mode=SessionSignalMode.REDIRECT,
                summary="Redirect acknowledged.",
                reply="The affected assertion is now passing.",
            ),
        ]
    )

    projection = project_session_signal(events)

    assert projection.state is SessionSignalState.COMPLETED
    assert projection.acknowledgement == "checkpoint:next_decision"
    assert projection.summary == "Redirect acknowledged."
    assert projection.reply == "The affected assertion is now passing."
    assert projection.is_terminal is True
    assert projection.effective_mode is SessionSignalMode.REDIRECT
    assert projection.effective_idempotency_key == (
        "exec_1",
        "scope_1",
        "scope_1_attempt_1",
        "intent_1",
    )


def test_requested_can_fail_closed_without_effective_mode() -> None:
    signal = _signal()
    projection = project_session_signal(
        [
            create_session_signal_requested_event(signal),
            create_session_signal_rejected_event(
                signal,
                rejection_code="stale_attempt",
                detail="Attempt replaced.",
            ),
        ]
    )

    assert projection.state is SessionSignalState.REJECTED
    assert projection.effective_mode is None


def test_queued_can_end_delivery_uncertain() -> None:
    signal, events = _accepted_flow()
    events.append(
        create_session_signal_delivery_uncertain_event(
            signal,
            effective_mode=SessionSignalMode.REDIRECT,
            detail="Acknowledgement boundary was lost.",
        )
    )

    assert project_session_signal(events).state is SessionSignalState.DELIVERY_UNCERTAIN


def test_illegal_transition_is_rejected() -> None:
    signal = _signal()
    with pytest.raises(ValueError, match="requested -> completed"):
        project_session_signal(
            [
                create_session_signal_requested_event(signal),
                create_session_signal_completed_event(
                    signal,
                    effective_mode=SessionSignalMode.REDIRECT,
                    summary="Impossible.",
                ),
            ]
        )


def test_identity_drift_is_rejected() -> None:
    signal, events = _accepted_flow()
    drifted = events[-1].model_copy(
        update={"data": {**events[-1].data, "expected_execution_id": "exec_2"}}
    )

    with pytest.raises(ValueError, match="identity changed"):
        project_session_signal([events[0], events[1], drifted])


def test_duplicate_event_id_is_idempotently_ignored() -> None:
    _signal_value, events = _accepted_flow()
    projection = project_session_signal([events[0], events[0], events[1], events[2]])

    assert projection.state is SessionSignalState.QUEUED
    assert projection.event_ids == tuple(event.id for event in events)


def test_effective_mode_cannot_change_after_acceptance() -> None:
    signal, events = _accepted_flow()
    changed = create_session_signal_queued_event(
        signal,
        effective_mode=SessionSignalMode.AFTER_TURN,
    )

    with pytest.raises(ValueError, match="effective_mode changed"):
        project_session_signal([events[0], events[1], changed])


@pytest.mark.parametrize(
    ("pending", "incoming", "expected"),
    [
        (SessionSignalSource.USER, SessionSignalSource.CONDUCTOR, False),
        (SessionSignalSource.USER, SessionSignalSource.USER, True),
        (SessionSignalSource.WORKER, SessionSignalSource.CONDUCTOR, True),
        (SessionSignalSource.CONDUCTOR, SessionSignalSource.USER, True),
    ],
)
def test_source_authority_is_deterministic(
    pending: SessionSignalSource,
    incoming: SessionSignalSource,
    expected: bool,
) -> None:
    assert can_supersede_session_signal(pending, incoming) is expected
