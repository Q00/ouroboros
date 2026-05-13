from __future__ import annotations

import json

import pytest

from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
    HumanInputRiskClass,
    HumanInputSource,
)
from ouroboros.events.hitl import (
    create_hitl_answered_event,
    create_hitl_cancelled_event,
    create_hitl_requested_event,
    create_hitl_timed_out_event,
)


def _request() -> HumanInputRequest:
    return HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve the plan?",
        resume_target="ralplan:approval",
        timeout_seconds=60,
    )


def test_requested_event_uses_hitl_event_type_and_request_aggregate() -> None:
    event = create_hitl_requested_event(_request())

    assert event.type == "hitl.requested"
    assert event.aggregate_type == "hitl"
    assert event.aggregate_id == "hitl-1"
    assert event.data["request_id"] == "hitl-1"
    assert event.data["resume_target"] == "ralplan:approval"


def test_requested_event_data_is_plain_json() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve the plan?",
        resume_target="ralplan:approval",
        payload={"items": ("alpha", {"count": 1})},
    )

    event = create_hitl_requested_event(request)

    assert event.data["payload"] == {"items": ["alpha", {"count": 1}]}
    json.dumps(event.data)


def test_answered_event_preserves_request_correlation() -> None:
    response = HumanInputResponse(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.APPROVAL,
        approval_decision=True,
    )

    event = create_hitl_answered_event(_request(), response)

    assert event.type == "hitl.answered"
    assert event.aggregate_id == "hitl-1"
    assert event.data["request_id"] == "hitl-1"
    assert event.data["approval_decision"] is True


def test_same_run_hitl_requests_use_distinct_request_aggregates() -> None:
    first = _request()
    second = HumanInputRequest(
        request_id="hitl-2",
        session_id=first.session_id,
        run_id=first.run_id,
        created_by=first.created_by,
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve the alternate plan?",
        resume_target="ralplan:approval-2",
    )

    first_event = create_hitl_requested_event(first)
    second_event = create_hitl_requested_event(second)

    assert first_event.data["run_id"] == second_event.data["run_id"] == "run-1"
    assert first_event.aggregate_id == "hitl-1"
    assert second_event.aggregate_id == "hitl-2"


def test_timeout_and_cancel_events_reuse_request_payload() -> None:
    request = _request()

    timed_out = create_hitl_timed_out_event(request, reason="deadline elapsed")
    cancelled = create_hitl_cancelled_event(request, reason="user aborted", actor="local-user")

    assert timed_out.type == "hitl.timed_out"
    assert timed_out.data["reason"] == "deadline elapsed"
    assert cancelled.type == "hitl.cancelled"
    assert cancelled.data["actor"] == "local-user"


def test_timeout_event_rejects_empty_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        create_hitl_timed_out_event(_request(), reason="   ")


def test_timeout_event_rejects_request_without_timeout() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve the plan?",
        resume_target="ralplan:approval",
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        create_hitl_timed_out_event(request, reason="deadline elapsed")


def test_answered_event_rejects_mismatched_request_id() -> None:
    response = HumanInputResponse(
        request_id="other-hitl",
        actor="local-user",
        response_kind=HumanInputResponseKind.APPROVAL,
        approval_decision=True,
    )

    with pytest.raises(ValueError, match="request_id"):
        create_hitl_answered_event(_request(), response)


def test_answered_event_rejects_kind_mismatch() -> None:
    response = HumanInputResponse(
        request_id="hitl-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.TEXT,
        text="approved",
    )

    with pytest.raises(ValueError, match="approval"):
        create_hitl_answered_event(_request(), response)


def test_answered_event_accepts_cancel_and_timeout_responses() -> None:
    cancel = HumanInputResponse(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.CANCEL,
        payload={"reason_code": "user_abort"},
    )
    timeout = HumanInputResponse(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        actor="runtime",
        response_kind=HumanInputResponseKind.TIMEOUT,
        payload={"deadline_ms": 60000},
    )

    cancel_event = create_hitl_answered_event(_request(), cancel)
    timeout_event = create_hitl_answered_event(_request(), timeout)

    assert cancel_event.type == "hitl.answered"
    assert cancel_event.data["response_kind"] == "cancel"
    assert cancel_event.data["payload"] == {"reason_code": "user_abort"}
    assert cancel_event.data["actor"] == "local-user"
    assert timeout_event.type == "hitl.answered"
    assert timeout_event.data["response_kind"] == "timeout"
    assert timeout_event.data["payload"] == {"deadline_ms": 60000}
    assert timeout_event.data["actor"] == "runtime"


def test_answered_event_rejects_timeout_response_without_request_timeout() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve the plan?",
        resume_target="ralplan:approval",
    )
    response = HumanInputResponse(
        request_id="hitl-1",
        actor="runtime",
        response_kind=HumanInputResponseKind.TIMEOUT,
    )

    with pytest.raises(ValueError, match="timeout_seconds"):
        create_hitl_answered_event(request, response)


def test_destructive_confirmation_answered_event_accepts_approval_response() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.DESTRUCTIVE_CONFIRMATION,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.DESTRUCTIVE,
        question="Delete the index?",
        resume_target="ralplan:approval",
    )
    response = HumanInputResponse(
        request_id="hitl-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.APPROVAL,
        approval_decision=True,
    )

    event = create_hitl_answered_event(request, response)

    assert event.type == "hitl.answered"
    assert event.aggregate_id == "hitl-1"
    assert event.data["approval_decision"] is True


def test_answered_event_rejects_selection_outside_request_options() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.SINGLE_SELECT,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Pick one",
        resume_target="ralplan:approval",
        options=("Approve", "Reject"),
    )
    response = HumanInputResponse(
        request_id="hitl-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.SELECTION,
        selected_values=("Escalate",),
    )

    with pytest.raises(ValueError, match="selected_values"):
        create_hitl_answered_event(request, response)


def test_answered_event_rejects_multiple_single_select_values() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        created_by="plan",
        kind=HumanInputKind.SINGLE_SELECT,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Pick one",
        resume_target="ralplan:approval",
        options=("Approve", "Reject"),
    )
    response = HumanInputResponse(
        request_id="hitl-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.SELECTION,
        selected_values=("Approve", "Reject"),
    )

    with pytest.raises(ValueError, match="single-select"):
        create_hitl_answered_event(request, response)


def test_cancel_event_rejects_empty_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        create_hitl_cancelled_event(_request(), reason="   ")


def test_cancel_event_rejects_empty_actor() -> None:
    with pytest.raises(ValueError, match="actor"):
        create_hitl_cancelled_event(_request(), reason="user aborted", actor="   ")


@pytest.mark.parametrize(
    ("field_name", "request_value", "response_value"),
    (
        ("session_id", "session-1", "session-2"),
        ("run_id", "run-1", "run-2"),
        ("invocation_id", "inv-1", "inv-2"),
    ),
)
def test_answered_event_rejects_correlation_mismatch(
    field_name: str, request_value: str, response_value: str
) -> None:
    request_kwargs: dict[str, object] = {
        "request_id": "hitl-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "invocation_id": "inv-1",
        "created_by": "plan",
        "kind": HumanInputKind.APPROVAL,
        "source": HumanInputSource.PLAN_APPROVAL,
        "risk_class": HumanInputRiskClass.MATERIAL_BRANCH,
        "question": "Approve the plan?",
        "resume_target": "ralplan:approval",
        "timeout_seconds": 60,
    }
    request = HumanInputRequest(**request_kwargs)  # type: ignore[arg-type]

    response_kwargs: dict[str, object] = {
        "request_id": "hitl-1",
        "actor": "local-user",
        "response_kind": HumanInputResponseKind.APPROVAL,
        "approval_decision": True,
        "session_id": request.session_id,
        "run_id": request.run_id,
        "invocation_id": request.invocation_id,
    }
    response_kwargs[field_name] = response_value
    response = HumanInputResponse(**response_kwargs)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match=field_name):
        create_hitl_answered_event(request, response)
