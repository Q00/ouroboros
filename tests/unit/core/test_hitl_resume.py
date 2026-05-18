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
from ouroboros.core.hitl_state import (
    HumanInputSnapshot,
    HumanInputState,
    project_human_input_state,
)
from ouroboros.events.base import BaseEvent
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


def test_human_input_request_from_legacy_plugin_firewall_snapshot_accepts_missing_new_fields() -> (
    None
):
    requested = BaseEvent(
        type="hitl.requested",
        aggregate_type="hitl",
        aggregate_id="hitl-legacy-plugin",
        data={
            "schema_version": 1,
            "request_id": "hitl-legacy-plugin",
            "session_id": "plugin-session-1",
            "created_by": "plugin-firewall",
            "kind": "destructive_confirmation",
            "source": "plugin_firewall",
            "risk_class": "destructive",
            "question": "Allow plugin deployer to run external production deployment?",
            "resume_target": "plugin-firewall:permission:plugin-session-1",
            "payload": {"plugin_id": "deployer"},
        },
        timestamp=datetime(2026, 5, 18, tzinfo=UTC),
    )
    snapshot = project_human_input_state([requested])[0]

    reconstructed = human_input_request_from_snapshot(snapshot)
    event = create_validated_hitl_resume_event(
        [requested],
        HumanInputResponse(
            request_id="hitl-legacy-plugin",
            session_id="plugin-session-1",
            actor="local-user",
            response_kind=HumanInputResponseKind.APPROVAL,
            approval_decision=True,
        ),
    )

    assert reconstructed.kind is HumanInputKind.DESTRUCTIVE_CONFIRMATION
    assert reconstructed.source is HumanInputSource.PLUGIN_FIREWALL
    assert reconstructed.required_permission is None
    assert reconstructed.surface is None
    assert reconstructed.payload == {"plugin_id": "deployer"}
    assert event.type == "hitl.answered"
    assert event.aggregate_id == "hitl-legacy-plugin"


def test_human_input_request_from_schema_v2_plugin_firewall_snapshot_reconstructs() -> None:
    requested = BaseEvent(
        type="hitl.requested",
        aggregate_type="hitl",
        aggregate_id="hitl-plugin-v2",
        data={
            "schema_version": 2,
            "request_id": "hitl-plugin-v2",
            "session_id": "plugin-session-1",
            "created_by": "plugin-firewall",
            "kind": "approval",
            "source": "plugin_firewall",
            "risk_class": "material_branch",
            "question": "Allow plugin acme.docs to use plugin:lifecycle:read?",
            "required_permission": "plugin:lifecycle:read",
            "resume_target": "plugin-firewall:permission:plugin-session-1",
            "surface": "plugin.firewall.permission",
            "payload": {
                "plugin_id": "acme.docs",
                "permission_scope": "plugin:lifecycle:read",
            },
        },
        timestamp=datetime(2026, 5, 18, tzinfo=UTC),
    )
    snapshot = project_human_input_state([requested])[0]

    reconstructed = human_input_request_from_snapshot(snapshot)

    assert reconstructed.schema_version == 2
    assert reconstructed.source is HumanInputSource.PLUGIN_FIREWALL
    assert reconstructed.required_permission == "plugin:lifecycle:read"
    assert reconstructed.payload["permission_scope"] == "plugin:lifecycle:read"


def test_human_input_request_from_schema_v2_plugin_firewall_snapshot_rejects_missing_fields() -> (
    None
):
    snapshot = HumanInputSnapshot(
        request_id="hitl-plugin-v2",
        state=HumanInputState.PENDING,
        request_event_id="evt_plugin_v2_requested",
        updated_event_id="evt_plugin_v2_requested",
        created_at=datetime(2026, 5, 18, tzinfo=UTC),
        updated_at=datetime(2026, 5, 18, tzinfo=UTC),
        session_id="plugin-session-1",
        resume_target="plugin-firewall:permission:plugin-session-1",
        request={
            "schema_version": 2,
            "request_id": "hitl-plugin-v2",
            "session_id": "plugin-session-1",
            "created_by": "plugin-firewall",
            "kind": "approval",
            "source": "plugin_firewall",
            "risk_class": "material_branch",
            "question": "Allow plugin acme.docs to use plugin:lifecycle:read?",
            "resume_target": "plugin-firewall:permission:plugin-session-1",
            "surface": "plugin.firewall.permission",
            "payload": {
                "plugin_id": "acme.docs",
                "permission_scope": "plugin:lifecycle:read",
            },
        },
    )

    with pytest.raises(HumanInputResumeValidationError, match="cannot be reconstructed"):
        human_input_request_from_snapshot(snapshot)


def test_human_input_request_from_unversioned_plugin_firewall_snapshot_rejects_missing_fields() -> (
    None
):
    snapshot = HumanInputSnapshot(
        request_id="hitl-plugin-unversioned",
        state=HumanInputState.PENDING,
        request_event_id="evt_plugin_unversioned_requested",
        updated_event_id="evt_plugin_unversioned_requested",
        created_at=datetime(2026, 5, 18, tzinfo=UTC),
        updated_at=datetime(2026, 5, 18, tzinfo=UTC),
        session_id="plugin-session-1",
        resume_target="plugin-firewall:permission:plugin-session-1",
        request={
            "request_id": "hitl-plugin-unversioned",
            "session_id": "plugin-session-1",
            "created_by": "plugin-firewall",
            "kind": "approval",
            "source": "plugin_firewall",
            "risk_class": "material_branch",
            "question": "Allow plugin acme.docs to use plugin:lifecycle:read?",
            "resume_target": "plugin-firewall:permission:plugin-session-1",
            "payload": {"plugin_id": "acme.docs"},
        },
    )

    with pytest.raises(HumanInputResumeValidationError, match="cannot be reconstructed"):
        human_input_request_from_snapshot(snapshot)


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
