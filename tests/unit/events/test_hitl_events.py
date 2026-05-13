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

    event = create_hitl_answered_event(response)

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


def test_cancel_event_rejects_empty_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        create_hitl_cancelled_event(_request(), reason="   ")


def test_cancel_event_rejects_empty_actor() -> None:
    with pytest.raises(ValueError, match="actor"):
        create_hitl_cancelled_event(_request(), reason="user aborted", actor="   ")
