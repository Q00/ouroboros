from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta, timezone
import json
from types import MappingProxyType

import pytest

from ouroboros.core.hitl_contract import (
    MAX_HITL_PAYLOAD_BYTES,
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
    HumanInputRiskClass,
    HumanInputSource,
    HumanInputTimeoutAction,
)


def test_human_input_request_serializes_wait_contract() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        invocation_id="invoke-1",
        created_by="deep-interview",
        kind=HumanInputKind.SINGLE_SELECT,
        source=HumanInputSource.INTERVIEW,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Which track should run first?",
        options=("plugin", "runtime"),
        required_permission="plugin:execute",
        timeout_seconds=60,
        timeout_action=HumanInputTimeoutAction.EXPIRE_BLOCKED,
        resume_target="deep-interview:round-2",
        surface="structured_question",
        payload={"redacted": True},
    )

    data = request.to_event_data()

    assert request.aggregate_id == "hitl-1"
    assert data["kind"] == "single_select"
    assert data["source"] == "interview"
    assert data["risk_class"] == "material_branch"
    assert data["timeout_action"] == "expire_blocked"
    assert data["required_permission"] == "plugin:execute"
    assert data["options"] == ["plugin", "runtime"]
    assert data["created_at"].endswith("+00:00")


def test_request_normalizes_aware_datetimes_to_utc_and_rejects_naive_datetimes() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve?",
        resume_target="plan:approval",
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=9))),
    )

    assert request.to_event_data()["created_at"] == "2026-01-01T03:00:00+00:00"

    with pytest.raises(ValueError, match="timezone-aware"):
        HumanInputRequest(
            request_id="hitl-1",
            session_id="session-1",
            created_by="plan",
            kind=HumanInputKind.APPROVAL,
            source=HumanInputSource.PLAN_APPROVAL,
            risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
            question="Approve?",
            resume_target="plan:approval",
            created_at=datetime(2026, 1, 1, 12, 0),
        )


def test_select_request_requires_options() -> None:
    with pytest.raises(ValueError, match="requires at least one option"):
        HumanInputRequest(
            request_id="hitl-1",
            session_id="session-1",
            created_by="plan",
            kind=HumanInputKind.SINGLE_SELECT,
            source=HumanInputSource.PLAN_APPROVAL,
            risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
            question="Pick one",
            resume_target="plan:approval",
        )


def test_select_request_rejects_string_options_iterable() -> None:
    with pytest.raises(TypeError, match="options"):
        HumanInputRequest(
            request_id="hitl-1",
            session_id="session-1",
            created_by="plan",
            kind=HumanInputKind.SINGLE_SELECT,
            source=HumanInputSource.PLAN_APPROVAL,
            risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
            question="Pick one",
            resume_target="plan:approval",
            options="yes",  # type: ignore[arg-type]
        )


def test_request_rejects_non_integer_timeout_seconds() -> None:
    for timeout_seconds in (1.5, True):
        with pytest.raises(TypeError, match="timeout_seconds"):
            HumanInputRequest(
                request_id="hitl-1",
                session_id="session-1",
                created_by="plan",
                kind=HumanInputKind.APPROVAL,
                source=HumanInputSource.PLAN_APPROVAL,
                risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
                question="Approve?",
                resume_target="plan:approval",
                timeout_seconds=timeout_seconds,  # type: ignore[arg-type]
            )


def test_request_rejects_secret_like_persisted_payload() -> None:
    with pytest.raises(ValueError, match="secret-like"):
        HumanInputRequest(
            request_id="hitl-1",
            session_id="session-1",
            created_by="plugin-firewall",
            kind=HumanInputKind.APPROVAL,
            source=HumanInputSource.PLUGIN_FIREWALL,
            risk_class=HumanInputRiskClass.CREDENTIAL_GATED,
            question="Approve?",
            resume_target="plugin:permission",
            payload={"api_key": "should-not-persist"},
        )


def test_request_allows_secret_marker_words_in_plain_values() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        created_by="plugin-firewall",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLUGIN_FIREWALL,
        risk_class=HumanInputRiskClass.CREDENTIAL_GATED,
        question="Approve?",
        resume_target="plugin:permission",
        payload={"reason": "token budget exceeded; credential check required"},
    )

    assert request.to_event_data()["payload"] == {
        "reason": "token budget exceeded; credential check required"
    }


def test_request_accepts_mapping_payloads_and_freezes_normalized_copy() -> None:
    source: dict[str, object] = {"items": ["alpha", {"count": 1}]}
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        created_by="plugin-firewall",
        kind=HumanInputKind.FREE_TEXT,
        source=HumanInputSource.PLUGIN_FIREWALL,
        risk_class=HumanInputRiskClass.LOW,
        question="Explain remediation?",
        resume_target="runtime:resume",
        payload=MappingProxyType(source),
    )

    items = source["items"]
    assert isinstance(items, list)
    items.append("mutated")

    assert request.to_event_data()["payload"] == {"items": ["alpha", {"count": 1}]}


def test_request_rejects_non_json_payload_values() -> None:
    with pytest.raises(TypeError, match="JSON serializable"):
        HumanInputRequest(
            request_id="hitl-1",
            session_id="session-1",
            created_by="plugin-firewall",
            kind=HumanInputKind.APPROVAL,
            source=HumanInputSource.PLUGIN_FIREWALL,
            risk_class=HumanInputRiskClass.CREDENTIAL_GATED,
            question="Approve?",
            resume_target="plugin:permission",
            payload={"created_at": datetime.now(UTC)},
        )


def test_request_payload_is_json_serializable_and_deeply_unaliased() -> None:
    nested = {"items": [{"name": "alpha"}]}
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve?",
        resume_target="plan:approval",
        payload=nested,
    )

    nested["items"][0]["name"] = "mutated"
    data = request.to_event_data()

    assert data["payload"] == {"items": [{"name": "alpha"}]}
    json.dumps(data)


def test_request_payload_event_data_is_deep_copy() -> None:
    request = HumanInputRequest(
        request_id="hitl-1",
        session_id="session-1",
        created_by="plan",
        kind=HumanInputKind.APPROVAL,
        source=HumanInputSource.PLAN_APPROVAL,
        risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
        question="Approve?",
        resume_target="plan:approval",
        payload={"items": [{"name": "alpha"}]},
    )

    first = request.to_event_data()
    first["payload"]["items"][0]["name"] = "mutated"
    second = request.to_event_data()

    assert second["payload"] == {"items": [{"name": "alpha"}]}


def test_request_rejects_payload_over_json_encoded_byte_limit() -> None:
    with pytest.raises(ValueError, match=str(MAX_HITL_PAYLOAD_BYTES)):
        HumanInputRequest(
            request_id="hitl-1",
            session_id="session-1",
            created_by="plan",
            kind=HumanInputKind.APPROVAL,
            source=HumanInputSource.PLAN_APPROVAL,
            risk_class=HumanInputRiskClass.MATERIAL_BRANCH,
            question="Approve?",
            resume_target="plan:approval",
            payload={"value": "x" * MAX_HITL_PAYLOAD_BYTES},
        )


def test_human_input_response_serializes_matching_answer() -> None:
    response = HumanInputResponse(
        request_id="hitl-1",
        session_id="session-1",
        run_id="run-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.APPROVAL,
        approval_decision=True,
        surface="cli",
    )

    data = response.to_event_data()

    assert response.aggregate_id == "hitl-1"
    assert data["request_id"] == "hitl-1"
    assert data["actor"] == "local-user"
    assert data["response_kind"] == "approval"
    assert data["approval_decision"] is True


def test_response_is_frozen() -> None:
    response = HumanInputResponse(
        request_id="hitl-1",
        actor="local-user",
        session_id="session-1",
        response_kind=HumanInputResponseKind.TEXT,
        text="continue",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        response.actor = "other"  # type: ignore[misc]


def test_response_allows_request_id_only_correlation() -> None:
    response = HumanInputResponse(
        request_id="hitl-1",
        actor="local-user",
        response_kind=HumanInputResponseKind.TEXT,
        text="continue",
    )

    assert response.aggregate_id == "hitl-1"
    assert response.to_event_data()["request_id"] == "hitl-1"
    assert "session_id" not in response.to_event_data()
    assert "run_id" not in response.to_event_data()
    assert "invocation_id" not in response.to_event_data()


def test_response_rejects_approval_decision_on_non_approval() -> None:
    with pytest.raises(ValueError, match="approval_decision"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.TEXT,
            text="yes",
            approval_decision=True,
        )


def test_text_response_requires_text_and_forbids_other_answer_content() -> None:
    with pytest.raises(ValueError, match="require text"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.TEXT,
        )
    with pytest.raises(ValueError, match="must not include selection"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.TEXT,
            text="yes",
            selected_values=("yes",),
        )


def test_selection_response_requires_selected_values_and_forbids_other_answer_content() -> None:
    with pytest.raises(ValueError, match="require selected_values"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.SELECTION,
        )
    with pytest.raises(ValueError, match="must not include text"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.SELECTION,
            selected_values=("yes",),
            text="yes",
        )


def test_selection_response_rejects_string_selected_values_iterable() -> None:
    with pytest.raises(TypeError, match="selected_values"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.SELECTION,
            selected_values="approve",  # type: ignore[arg-type]
        )


def test_approval_response_requires_decision_and_forbids_other_answer_content() -> None:
    with pytest.raises(ValueError, match="require approval_decision"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.APPROVAL,
        )
    with pytest.raises(ValueError, match="must not include text"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.APPROVAL,
            approval_decision=True,
            text="yes",
        )


def test_cancel_and_timeout_responses_forbid_answer_content() -> None:
    with pytest.raises(ValueError, match="must not include answer content"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.CANCEL,
            text="never mind",
        )
    with pytest.raises(ValueError, match="must not include answer content"):
        HumanInputResponse(
            request_id="hitl-1",
            actor="local-user",
            session_id="session-1",
            response_kind=HumanInputResponseKind.TIMEOUT,
            selected_values=("late",),
        )
