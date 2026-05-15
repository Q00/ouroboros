from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
    HumanInputRiskClass,
    HumanInputSource,
)
from ouroboros.core.hitl_state import (
    HumanInputState,
    pending_human_input_requests,
    project_human_input_state,
)
from ouroboros.events.base import BaseEvent
from ouroboros.events.hitl import (
    create_hitl_answered_event,
    create_hitl_cancelled_event,
    create_hitl_requested_event,
    create_hitl_timed_out_event,
)


def _request(request_id: str = "hitl-1") -> HumanInputRequest:
    return HumanInputRequest(
        request_id=request_id,
        session_id="session-1",
        run_id="run-1",
        invocation_id="invoke-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve?",
        resume_target="plan:resume",
        timeout_seconds=30,
        payload={"nested": {"count": 1}},
    )


def _with_time(event: BaseEvent, seconds: int, event_id: str) -> BaseEvent:
    return event.model_copy(
        update={
            "id": event_id,
            "timestamp": datetime(2026, 5, 15, tzinfo=UTC) + timedelta(seconds=seconds),
        }
    )


def test_projects_pending_request_snapshot() -> None:
    event = _with_time(create_hitl_requested_event(_request()), 0, "evt_requested")

    snapshots = project_human_input_state([event])

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.request_id == "hitl-1"
    assert snapshot.state is HumanInputState.PENDING
    assert snapshot.request_event_id == "evt_requested"
    assert snapshot.updated_event_id == "evt_requested"
    assert snapshot.session_id == "session-1"
    assert snapshot.run_id == "run-1"
    assert snapshot.invocation_id == "invoke-1"
    assert snapshot.resume_target == "plan:resume"
    assert snapshot.is_terminal is False
    assert snapshot.request["payload"] == {"nested": {"count": 1}}


def test_answered_event_closes_request_with_response_payload() -> None:
    request = _request()
    events = [
        _with_time(create_hitl_requested_event(request), 0, "evt_requested"),
        _with_time(
            create_hitl_answered_event(
                request,
                HumanInputResponse(
                    request_id="hitl-1",
                    session_id="session-1",
                    run_id="run-1",
                    actor="local-user",
                    response_kind=HumanInputResponseKind.APPROVAL,
                    approval_decision=True,
                ),
            ),
            1,
            "evt_answered",
        ),
    ]

    snapshot = project_human_input_state(events)[0]

    assert snapshot.state is HumanInputState.ANSWERED
    assert snapshot.is_terminal is True
    assert snapshot.terminal_event_id == "evt_answered"
    assert snapshot.actor == "local-user"
    assert snapshot.response is not None
    assert snapshot.response["approval_decision"] is True
    assert pending_human_input_requests(events) == ()


def test_timeout_and_cancel_events_project_terminal_reason() -> None:
    timeout_request = _request("hitl-timeout")
    cancel_request = _request("hitl-cancel")
    events = [
        _with_time(create_hitl_requested_event(timeout_request), 0, "evt_timeout_req"),
        _with_time(create_hitl_requested_event(cancel_request), 1, "evt_cancel_req"),
        _with_time(
            create_hitl_timed_out_event(timeout_request, reason="deadline elapsed"),
            2,
            "evt_timeout",
        ),
        _with_time(
            create_hitl_cancelled_event(
                cancel_request,
                reason="user aborted",
                actor="local-user",
            ),
            3,
            "evt_cancel",
        ),
    ]

    timeout, cancelled = project_human_input_state(events)

    assert timeout.state is HumanInputState.TIMED_OUT
    assert timeout.reason == "deadline elapsed"
    assert cancelled.state is HumanInputState.CANCELLED
    assert cancelled.reason == "user aborted"
    assert cancelled.actor == "local-user"


def test_duplicate_request_after_terminal_does_not_reopen_wait() -> None:
    request = _request()
    requested = _with_time(create_hitl_requested_event(request), 0, "evt_requested")
    answered = _with_time(
        create_hitl_answered_event(
            request,
            HumanInputResponse(
                request_id="hitl-1",
                actor="local-user",
                response_kind=HumanInputResponseKind.APPROVAL,
                approval_decision=True,
            ),
        ),
        1,
        "evt_answered",
    )
    duplicate_requested = _with_time(create_hitl_requested_event(request), 2, "evt_requested_again")

    snapshot = project_human_input_state([requested, answered, duplicate_requested])[0]

    assert snapshot.state is HumanInputState.ANSWERED
    assert snapshot.terminal_event_id == "evt_answered"
    assert pending_human_input_requests([requested, answered, duplicate_requested]) == ()


def test_ignores_orphan_and_late_terminal_events() -> None:
    request = _request()
    requested = _with_time(create_hitl_requested_event(request), 0, "evt_requested")
    answered = _with_time(
        create_hitl_answered_event(
            request,
            HumanInputResponse(
                request_id="hitl-1",
                actor="local-user",
                response_kind=HumanInputResponseKind.APPROVAL,
                approval_decision=True,
            ),
        ),
        1,
        "evt_answered",
    )
    late_cancel = _with_time(
        create_hitl_cancelled_event(request, reason="late cancellation"),
        2,
        "evt_late_cancel",
    )
    orphan = BaseEvent(
        id="evt_orphan",
        type="hitl.answered",
        timestamp=datetime(2026, 5, 15, tzinfo=UTC),
        aggregate_type="hitl",
        aggregate_id="orphan",
        data={"request_id": "orphan", "actor": "local-user"},
    )

    snapshots = project_human_input_state([orphan, requested, answered, late_cancel])

    assert len(snapshots) == 1
    assert snapshots[0].state is HumanInputState.ANSWERED
    assert snapshots[0].terminal_event_id == "evt_answered"
