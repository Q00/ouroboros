from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
    HumanInputRiskClass,
    HumanInputSource,
    HumanInputTimeoutAction,
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


def _request(
    request_id: str = "hitl-1",
    *,
    timeout_action: HumanInputTimeoutAction = HumanInputTimeoutAction.STAY_WAITING,
) -> HumanInputRequest:
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
        timeout_action=timeout_action,
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


def test_malformed_answered_events_do_not_close_pending_request() -> None:
    request = _request()
    requested = _with_time(create_hitl_requested_event(request), 0, "evt_requested")
    missing_kind = _with_time(
        BaseEvent(
            type="hitl.answered",
            aggregate_type="hitl",
            aggregate_id="hitl-1",
            data={"request_id": "hitl-1", "actor": "local-user"},
        ),
        1,
        "evt_missing_kind",
    )
    unknown_kind = _with_time(
        BaseEvent(
            type="hitl.answered",
            aggregate_type="hitl",
            aggregate_id="hitl-1",
            data={
                "request_id": "hitl-1",
                "actor": "local-user",
                "response_kind": "unknown",
            },
        ),
        2,
        "evt_unknown_kind",
    )
    missing_answer_content = _with_time(
        BaseEvent(
            type="hitl.answered",
            aggregate_type="hitl",
            aggregate_id="hitl-1",
            data={
                "request_id": "hitl-1",
                "actor": "local-user",
                "response_kind": "approval",
            },
        ),
        3,
        "evt_missing_answer_content",
    )
    invalid_selection = _with_time(
        BaseEvent(
            type="hitl.answered",
            aggregate_type="hitl",
            aggregate_id="hitl-1",
            data={
                "request_id": "hitl-1",
                "actor": "local-user",
                "response_kind": "selection",
                "selected_values": ["Approve", 7],
            },
        ),
        4,
        "evt_invalid_selection",
    )
    cancel_with_answer_content = _with_time(
        BaseEvent(
            type="hitl.answered",
            aggregate_type="hitl",
            aggregate_id="hitl-1",
            data={
                "request_id": "hitl-1",
                "actor": "local-user",
                "response_kind": "cancel",
                "text": "never mind",
            },
        ),
        5,
        "evt_cancel_with_answer_content",
    )

    malformed_events = [
        requested,
        missing_kind,
        unknown_kind,
        missing_answer_content,
        invalid_selection,
        cancel_with_answer_content,
    ]

    snapshots = project_human_input_state(malformed_events)

    assert len(snapshots) == 1
    assert snapshots[0].state is HumanInputState.PENDING
    assert snapshots[0].updated_event_id == "evt_requested"
    assert pending_human_input_requests(malformed_events) == snapshots


def test_malformed_requested_event_does_not_abort_projection() -> None:
    valid_request = _request("hitl-valid")
    malformed_request = BaseEvent(
        type="hitl.requested",
        aggregate_type="hitl",
        aggregate_id="hitl-bad",
        data={"request_id": "hitl-bad", "session_id": "session-1"},
    )

    snapshots = project_human_input_state(
        [
            _with_time(malformed_request, 0, "evt_bad_request"),
            _with_time(create_hitl_requested_event(valid_request), 1, "evt_valid_request"),
        ]
    )

    assert len(snapshots) == 1
    assert snapshots[0].request_id == "hitl-valid"
    assert snapshots[0].state is HumanInputState.PENDING


def test_stay_waiting_timeout_events_do_not_close_pending_request() -> None:
    request = _request("hitl-stay-waiting")
    events = [
        _with_time(create_hitl_requested_event(request), 0, "evt_requested"),
        _with_time(
            create_hitl_timed_out_event(request, reason="deadline elapsed"), 1, "evt_timeout"
        ),
        _with_time(
            create_hitl_answered_event(
                request,
                HumanInputResponse(
                    request_id="hitl-stay-waiting",
                    session_id="session-1",
                    run_id="run-1",
                    actor="runtime",
                    response_kind=HumanInputResponseKind.TIMEOUT,
                    payload={"deadline_ms": 30000},
                ),
            ),
            2,
            "evt_timeout_answer",
        ),
    ]

    snapshot = project_human_input_state(events)[0]

    assert snapshot.state is HumanInputState.PENDING
    assert snapshot.updated_event_id == "evt_requested"
    assert pending_human_input_requests(events) == (snapshot,)


def test_mismatched_timeout_and_cancel_events_do_not_close_pending_request() -> None:
    timeout_request = _request(
        "hitl-timeout-mismatch", timeout_action=HumanInputTimeoutAction.CANCEL
    )
    cancel_request = _request("hitl-cancel-mismatch")
    events = [
        _with_time(create_hitl_requested_event(timeout_request), 0, "evt_timeout_req"),
        _with_time(create_hitl_requested_event(cancel_request), 1, "evt_cancel_req"),
        _with_time(
            BaseEvent(
                type="hitl.timed_out",
                aggregate_type="hitl",
                aggregate_id="hitl-timeout-mismatch",
                data={
                    "request_id": "hitl-timeout-mismatch",
                    "session_id": "other-session",
                    "run_id": "run-1",
                    "invocation_id": "invoke-1",
                    "reason": "deadline elapsed",
                },
            ),
            2,
            "evt_timeout_mismatch",
        ),
        _with_time(
            BaseEvent(
                type="hitl.cancelled",
                aggregate_type="hitl",
                aggregate_id="hitl-cancel-mismatch",
                data={
                    "request_id": "hitl-cancel-mismatch",
                    "session_id": "session-1",
                    "run_id": "other-run",
                    "invocation_id": "invoke-1",
                    "reason": "user aborted",
                    "actor": "local-user",
                },
            ),
            3,
            "evt_cancel_mismatch",
        ),
    ]

    snapshots = project_human_input_state(events)

    assert tuple(snapshot.state for snapshot in snapshots) == (
        HumanInputState.PENDING,
        HumanInputState.PENDING,
    )
    assert pending_human_input_requests(events) == snapshots


def test_incompatible_answered_events_do_not_close_pending_request() -> None:
    text_request = HumanInputRequest(
        request_id="hitl-text",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.FREE_TEXT,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Explain?",
        resume_target="plan:resume",
    )
    single_select_request = HumanInputRequest(
        request_id="hitl-select",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.SINGLE_SELECT,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Choose?",
        resume_target="plan:resume",
        options=("Approve", "Reject"),
    )
    approval_without_timeout = HumanInputRequest(
        request_id="hitl-no-timeout",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve?",
        resume_target="plan:resume",
    )
    events = [
        _with_time(create_hitl_requested_event(text_request), 0, "evt_text_req"),
        _with_time(create_hitl_requested_event(single_select_request), 1, "evt_select_req"),
        _with_time(create_hitl_requested_event(approval_without_timeout), 2, "evt_timeout_req"),
        _with_time(
            BaseEvent(
                type="hitl.answered",
                aggregate_type="hitl",
                aggregate_id="hitl-text",
                data={
                    "request_id": "hitl-text",
                    "actor": "local-user",
                    "response_kind": "approval",
                    "approval_decision": True,
                },
            ),
            3,
            "evt_wrong_kind",
        ),
        _with_time(
            BaseEvent(
                type="hitl.answered",
                aggregate_type="hitl",
                aggregate_id="hitl-select",
                data={
                    "request_id": "hitl-select",
                    "actor": "local-user",
                    "response_kind": "selection",
                    "selected_values": ["Approve", "Reject"],
                },
            ),
            4,
            "evt_multi_single_select",
        ),
        _with_time(
            BaseEvent(
                type="hitl.answered",
                aggregate_type="hitl",
                aggregate_id="hitl-select",
                data={
                    "request_id": "hitl-select",
                    "actor": "local-user",
                    "response_kind": "selection",
                    "selected_values": ["Other"],
                },
            ),
            5,
            "evt_selection_outside_options",
        ),
        _with_time(
            BaseEvent(
                type="hitl.answered",
                aggregate_type="hitl",
                aggregate_id="hitl-select",
                data={
                    "request_id": "hitl-select",
                    "actor": "local-user",
                    "response_kind": "selection",
                    "selected_values": ["Approve", " Approve "],
                },
            ),
            6,
            "evt_duplicate_selection",
        ),
        _with_time(
            BaseEvent(
                type="hitl.answered",
                aggregate_type="hitl",
                aggregate_id="hitl-no-timeout",
                data={
                    "request_id": "hitl-no-timeout",
                    "actor": "runtime",
                    "response_kind": "timeout",
                },
            ),
            7,
            "evt_timeout_answer_without_timeout",
        ),
        _with_time(
            BaseEvent(
                type="hitl.timed_out",
                aggregate_type="hitl",
                aggregate_id="hitl-no-timeout",
                data={"request_id": "hitl-no-timeout", "reason": "deadline elapsed"},
            ),
            8,
            "evt_timeout_event_without_timeout",
        ),
    ]

    snapshots = project_human_input_state(events)

    assert tuple(snapshot.state for snapshot in snapshots) == (
        HumanInputState.PENDING,
        HumanInputState.PENDING,
        HumanInputState.PENDING,
    )
    assert pending_human_input_requests(events) == snapshots


def test_answered_cancel_and_timeout_responses_project_terminal_state() -> None:
    cancel_request = _request("hitl-cancel-answer")
    timeout_request = _request("hitl-timeout-answer", timeout_action=HumanInputTimeoutAction.CANCEL)
    events = [
        _with_time(create_hitl_requested_event(cancel_request), 0, "evt_cancel_req"),
        _with_time(create_hitl_requested_event(timeout_request), 1, "evt_timeout_req"),
        _with_time(
            create_hitl_answered_event(
                cancel_request,
                HumanInputResponse(
                    request_id="hitl-cancel-answer",
                    session_id="session-1",
                    run_id="run-1",
                    actor="local-user",
                    response_kind=HumanInputResponseKind.CANCEL,
                    payload={"reason_code": "user_abort"},
                ),
            ),
            2,
            "evt_cancel_answer",
        ),
        _with_time(
            create_hitl_answered_event(
                timeout_request,
                HumanInputResponse(
                    request_id="hitl-timeout-answer",
                    session_id="session-1",
                    run_id="run-1",
                    actor="runtime",
                    response_kind=HumanInputResponseKind.TIMEOUT,
                    payload={"deadline_ms": 30000},
                ),
            ),
            3,
            "evt_timeout_answer",
        ),
    ]

    cancelled, timed_out = project_human_input_state(events)

    assert cancelled.state is HumanInputState.CANCELLED
    assert cancelled.terminal_event_id == "evt_cancel_answer"
    assert cancelled.actor == "local-user"
    assert cancelled.response is not None
    assert cancelled.response["response_kind"] == "cancel"
    assert cancelled.response["payload"] == {"reason_code": "user_abort"}
    assert timed_out.state is HumanInputState.TIMED_OUT
    assert timed_out.terminal_event_id == "evt_timeout_answer"
    assert timed_out.actor == "runtime"
    assert timed_out.response is not None
    assert timed_out.response["response_kind"] == "timeout"
    assert pending_human_input_requests(events) == ()


def test_timeout_and_cancel_events_project_terminal_reason() -> None:
    timeout_request = _request("hitl-timeout", timeout_action=HumanInputTimeoutAction.CANCEL)
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


def test_duplicate_pending_request_preserves_original_request_provenance() -> None:
    request = _request()
    duplicate_request = HumanInputRequest(
        request_id=request.request_id,
        session_id=request.session_id,
        run_id=request.run_id,
        invocation_id=request.invocation_id,
        created_by=request.created_by,
        kind=request.kind,
        source=request.source,
        risk_class=request.risk_class,
        question="Approve the refreshed plan?",
        resume_target=request.resume_target,
        timeout_seconds=request.timeout_seconds,
        payload={"nested": {"count": 2}},
    )
    requested = _with_time(create_hitl_requested_event(request), 0, "evt_requested")
    duplicate_requested = _with_time(
        create_hitl_requested_event(duplicate_request), 1, "evt_requested_again"
    )

    snapshot = project_human_input_state([requested, duplicate_requested])[0]

    assert snapshot.state is HumanInputState.PENDING
    assert snapshot.request_event_id == "evt_requested"
    assert snapshot.created_at == requested.timestamp
    assert snapshot.updated_event_id == "evt_requested_again"
    assert snapshot.updated_at == duplicate_requested.timestamp
    assert snapshot.request["question"] == "Approve the refreshed plan?"
    assert snapshot.request["payload"] == {"nested": {"count": 2}}


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
