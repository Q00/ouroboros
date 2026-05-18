from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
    HumanInputRiskClass,
    HumanInputSource,
    HumanInputTimeoutAction,
)
from ouroboros.core.hitl_resume import (
    HumanInputResumeValidationError,
    create_validated_hitl_resume_event,
    create_validated_hitl_timeout_event,
    human_input_request_from_snapshot,
    pending_human_input_snapshot_for_response,
)
from ouroboros.core.hitl_state import HumanInputState, project_human_input_state
from ouroboros.events.hitl import create_hitl_answered_event, create_hitl_requested_event


def _request(
    *,
    timeout_action: HumanInputTimeoutAction = HumanInputTimeoutAction.STAY_WAITING,
) -> HumanInputRequest:
    return HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        invocation_id="invoke-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve the plan?",
        resume_target="plan:approval",
        timeout_seconds=60,
        timeout_action=timeout_action,
        payload={"plan_id": "plan-1"},
        created_at=datetime(2026, 5, 18, tzinfo=UTC),
    )


def _approval_response(**overrides: object) -> HumanInputResponse:
    kwargs = {
        "request_id": "hitl-1",
        "session_id": "session-1",
        "run_id": "run-1",
        "invocation_id": "invoke-1",
        "actor": "local-user",
        "response_kind": HumanInputResponseKind.APPROVAL,
        "approval_decision": True,
    }
    kwargs.update(overrides)
    return HumanInputResponse(**kwargs)  # type: ignore[arg-type]


def test_create_validated_hitl_resume_event_accepts_pending_matching_response() -> None:
    requested = create_hitl_requested_event(_request())
    response = _approval_response()

    event = create_validated_hitl_resume_event([requested], response)

    assert event.type == "hitl.answered"
    assert event.aggregate_id == "hitl-1"
    assert event.data["approval_decision"] is True


def test_pending_human_input_snapshot_for_response_requires_existing_request() -> None:
    with pytest.raises(HumanInputResumeValidationError, match="not found"):
        pending_human_input_snapshot_for_response([], _approval_response())


def test_create_validated_hitl_resume_event_rejects_duplicate_answer() -> None:
    request = _request()
    requested = create_hitl_requested_event(request)
    answered = create_hitl_answered_event(request, _approval_response())

    with pytest.raises(HumanInputResumeValidationError, match="not pending"):
        create_validated_hitl_resume_event([requested, answered], _approval_response())


def test_create_validated_hitl_resume_event_rejects_context_mismatch() -> None:
    requested = create_hitl_requested_event(_request())
    response = _approval_response(session_id="other-session")

    with pytest.raises(HumanInputResumeValidationError, match="session_id"):
        create_validated_hitl_resume_event([requested], response)


def test_create_validated_hitl_resume_event_rejects_wrong_response_kind() -> None:
    requested = create_hitl_requested_event(_request())
    response = HumanInputResponse(
        request_id="hitl-1",
        session_id="session-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.TEXT,
        text="approved",
    )

    with pytest.raises(ValueError, match="approval"):
        create_validated_hitl_resume_event([requested], response)


def test_human_input_request_from_snapshot_reconstructs_persisted_contract() -> None:
    requested = create_hitl_requested_event(_request())
    snapshot = project_human_input_state([requested])[0]

    reconstructed = human_input_request_from_snapshot(snapshot)

    assert reconstructed.request_id == "hitl-1"
    assert reconstructed.session_id == "session-1"
    assert reconstructed.run_id == "run-1"
    assert reconstructed.invocation_id == "invoke-1"
    assert reconstructed.kind is HumanInputKind.APPROVAL
    assert reconstructed.source is HumanInputSource.PLAN_APPROVAL
    assert reconstructed.risk_class is HumanInputRiskClass.MATERIAL_BRANCH
    assert reconstructed.payload["plan_id"] == "plan-1"
    assert reconstructed.to_event_data()["created_at"] == "2026-05-18T00:00:00+00:00"


def test_create_validated_hitl_resume_event_answers_wait_with_malformed_created_at() -> None:
    base_requested = create_hitl_requested_event(_request())
    requested = base_requested.model_copy(
        update={
            "timestamp": datetime(2026, 5, 19, 12, 30, tzinfo=UTC),
            "data": {
                **base_requested.data,
                "created_at": "not-a-timestamp",
            },
        }
    )
    snapshot = project_human_input_state([requested])[0]

    assert snapshot.state is HumanInputState.PENDING

    reconstructed = human_input_request_from_snapshot(snapshot)
    event = create_validated_hitl_resume_event([requested], _approval_response())

    assert reconstructed.created_at == requested.timestamp
    assert reconstructed.to_event_data()["created_at"] == "2026-05-19T12:30:00+00:00"
    assert event.type == "hitl.answered"
    assert event.aggregate_id == "hitl-1"


def test_create_validated_hitl_resume_event_answers_wait_with_naive_created_at() -> None:
    base_requested = create_hitl_requested_event(_request())
    requested = base_requested.model_copy(
        update={
            "timestamp": datetime(2026, 5, 19, 12, 30, tzinfo=UTC),
            "data": {
                **base_requested.data,
                "created_at": "2026-05-19T12:30:00",
            },
        }
    )
    snapshot = project_human_input_state([requested])[0]

    assert snapshot.state is HumanInputState.PENDING

    reconstructed = human_input_request_from_snapshot(snapshot)
    event = create_validated_hitl_resume_event([requested], _approval_response())

    assert reconstructed.created_at == requested.timestamp
    assert reconstructed.to_event_data()["created_at"] == "2026-05-19T12:30:00+00:00"
    assert event.type == "hitl.answered"
    assert event.aggregate_id == "hitl-1"


def test_create_validated_hitl_timeout_event_accepts_expired_pending_request() -> None:
    request = _request(timeout_action=HumanInputTimeoutAction.EXPIRE_BLOCKED)
    requested = create_hitl_requested_event(request)

    event = create_validated_hitl_timeout_event(
        [requested],
        request_id="hitl-1",
        now=request.created_at + timedelta(seconds=60),
        reason="approval timed out",
    )

    assert event.type == "hitl.timed_out"
    assert event.aggregate_id == "hitl-1"
    assert event.data["reason"] == "approval timed out"
    assert event.data["timeout_action"] == "expire_blocked"

    snapshot = project_human_input_state([requested, event])[0]
    assert snapshot.state is HumanInputState.TIMED_OUT
    assert snapshot.is_terminal is True


def test_create_validated_hitl_timeout_event_rejects_stay_waiting_request() -> None:
    request = _request()
    requested = create_hitl_requested_event(request)

    with pytest.raises(
        HumanInputResumeValidationError,
        match="timeout_action=stay_waiting",
    ):
        create_validated_hitl_timeout_event(
            [requested],
            request_id="hitl-1",
            now=request.created_at + timedelta(seconds=60),
        )


def test_create_validated_hitl_timeout_event_rejects_not_expired_request() -> None:
    request = _request()
    requested = create_hitl_requested_event(request)

    with pytest.raises(HumanInputResumeValidationError, match="not expired"):
        create_validated_hitl_timeout_event(
            [requested],
            request_id="hitl-1",
            now=request.created_at + timedelta(seconds=59),
        )


def test_create_validated_hitl_timeout_event_rejects_terminal_request() -> None:
    request = _request()
    requested = create_hitl_requested_event(request)
    answered = create_hitl_answered_event(request, _approval_response())

    with pytest.raises(HumanInputResumeValidationError, match="not pending"):
        create_validated_hitl_timeout_event(
            [requested, answered],
            request_id="hitl-1",
            now=request.created_at + timedelta(seconds=120),
        )


def test_create_validated_hitl_timeout_event_requires_timezone_aware_clock() -> None:
    request = _request()
    requested = create_hitl_requested_event(request)

    with pytest.raises(HumanInputResumeValidationError, match="timezone-aware"):
        create_validated_hitl_timeout_event(
            [requested],
            request_id="hitl-1",
            now=datetime(2026, 5, 18),
        )
