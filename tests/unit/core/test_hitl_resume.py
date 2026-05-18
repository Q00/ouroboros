from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
    HumanInputRiskClass,
    HumanInputSource,
)
from ouroboros.core.hitl_resume import (
    HumanInputResumeValidationError,
    create_validated_hitl_resume_event,
    human_input_request_from_snapshot,
    pending_human_input_snapshot_for_response,
)
from ouroboros.core.hitl_state import project_human_input_state
from ouroboros.events.hitl import create_hitl_answered_event, create_hitl_requested_event


def _request() -> HumanInputRequest:
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
