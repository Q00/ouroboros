"""Event factories for human-in-the-loop WAIT/RESUME contracts."""

from __future__ import annotations

from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
)
from ouroboros.events.base import BaseEvent


def _require_non_empty_event_field(name: str, value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"HITL event {name} must be non-empty")
    return normalized


def create_hitl_requested_event(request: HumanInputRequest) -> BaseEvent:
    return BaseEvent(
        type=HumanInputRequest.REQUESTED_EVENT_TYPE,
        aggregate_type="hitl",
        aggregate_id=request.aggregate_id,
        data=request.to_event_data(),
    )


def _validate_response_matches_request(
    request: HumanInputRequest, response: HumanInputResponse
) -> None:
    if response.request_id != request.request_id:
        raise ValueError("HITL response request_id must match the originating request")

    for field_name in ("session_id", "run_id", "invocation_id"):
        response_value = getattr(response, field_name)
        if response_value is None:
            continue
        request_value = getattr(request, field_name)
        if request_value is not None and response_value != request_value:
            raise ValueError(
                f"HITL response {field_name} must match the originating request when provided"
            )

    if response.response_kind is HumanInputResponseKind.CANCEL:
        return

    if response.response_kind is HumanInputResponseKind.TIMEOUT:
        if request.timeout_seconds is None:
            raise ValueError("timeout HITL responses require a request timeout_seconds value")
        return

    if request.kind in (
        HumanInputKind.APPROVAL,
        HumanInputKind.DESTRUCTIVE_CONFIRMATION,
    ):
        if response.response_kind is not HumanInputResponseKind.APPROVAL:
            raise ValueError("approval HITL requests require an approval response")
        return

    if request.kind is HumanInputKind.FREE_TEXT:
        if response.response_kind is not HumanInputResponseKind.TEXT:
            raise ValueError("free-text HITL requests require a text response")
        return

    if request.kind in (HumanInputKind.SINGLE_SELECT, HumanInputKind.MULTI_SELECT):
        if response.response_kind is not HumanInputResponseKind.SELECTION:
            raise ValueError("select HITL requests require a selection response")
        options = set(request.options or ())
        selected_values = response.selected_values or ()
        invalid_values = [value for value in selected_values if value not in options]
        if invalid_values:
            raise ValueError("HITL response selected_values must be present in request options")
        if request.kind is HumanInputKind.SINGLE_SELECT and len(selected_values) != 1:
            raise ValueError("single-select HITL requests require exactly one selected value")
        return

    raise ValueError(f"unsupported HITL request kind: {request.kind}")


def create_hitl_answered_event(
    request: HumanInputRequest, response: HumanInputResponse
) -> BaseEvent:
    _validate_response_matches_request(request, response)
    return BaseEvent(
        type=HumanInputResponse.ANSWERED_EVENT_TYPE,
        aggregate_type="hitl",
        aggregate_id=response.aggregate_id,
        data=response.to_event_data(),
    )


def create_hitl_timed_out_event(request: HumanInputRequest, *, reason: str) -> BaseEvent:
    if request.timeout_seconds is None:
        raise ValueError("HITL timed_out events require a request timeout_seconds value")
    data = request.to_event_data()
    data["reason"] = _require_non_empty_event_field("reason", reason)
    return BaseEvent(
        type=HumanInputRequest.TIMED_OUT_EVENT_TYPE,
        aggregate_type="hitl",
        aggregate_id=request.aggregate_id,
        data=data,
    )


def create_hitl_cancelled_event(
    request: HumanInputRequest, *, reason: str, actor: str | None = None
) -> BaseEvent:
    data = request.to_event_data()
    data["reason"] = _require_non_empty_event_field("reason", reason)
    if actor is not None:
        data["actor"] = _require_non_empty_event_field("actor", actor)
    return BaseEvent(
        type=HumanInputRequest.CANCELLED_EVENT_TYPE,
        aggregate_type="hitl",
        aggregate_id=request.aggregate_id,
        data=data,
    )
