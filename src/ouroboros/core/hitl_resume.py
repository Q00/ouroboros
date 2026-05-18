"""Validation helpers for durable HITL RESUME responses.

This module bridges persisted ``hitl.*`` event history to a new user response.
Callers that only have the EventStore stream should validate against the current
pending request before appending ``hitl.answered``.  The helper is pure: it does
not append events or perform runtime dispatch.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from ouroboros.core.hitl_contract import (
    HumanInputKind,
    HumanInputRequest,
    HumanInputResponse,
    HumanInputRiskClass,
    HumanInputSource,
    HumanInputTimeoutAction,
)
from ouroboros.core.hitl_state import HumanInputSnapshot, HumanInputState, project_human_input_state
from ouroboros.events.base import BaseEvent
from ouroboros.events.hitl import create_hitl_answered_event, create_hitl_timed_out_event


class HumanInputResumeValidationError(ValueError):
    """Raised when a HITL response cannot resolve a pending request."""


def create_validated_hitl_resume_event(
    events: Iterable[BaseEvent],
    response: HumanInputResponse,
) -> BaseEvent:
    """Return a ``hitl.answered`` event after validating pending WAIT state.

    ``events`` must contain the ordered HITL history for at least the target
    request.  Validation fails when the request is missing, already terminal, or
    the response does not satisfy the originating request contract.  This keeps
    resume surfaces from accepting stale, duplicate, wrong-session, or
    wrong-shape responses.
    """

    snapshot = pending_human_input_snapshot_for_response(events, response)
    request = human_input_request_from_snapshot(snapshot)
    return create_hitl_answered_event(request, response)


def create_validated_hitl_timeout_event(
    events: Iterable[BaseEvent],
    *,
    request_id: str,
    now: datetime,
    reason: str = "HITL request timed out",
) -> BaseEvent:
    """Return ``hitl.timed_out`` when a pending request has expired.

    The helper is intentionally pure: callers provide replayed HITL history and
    the current clock value, and receive the event they may append. No scheduler,
    UI timer, or dispatch behavior is implied by this function.
    """

    snapshot = _pending_human_input_snapshot_by_id(events, request_id)
    request = human_input_request_from_snapshot(snapshot)
    if request.timeout_seconds is None:
        raise HumanInputResumeValidationError(
            f"HITL request {request_id!r} does not define timeout_seconds"
        )
    if now.tzinfo is None or now.utcoffset() is None:
        raise HumanInputResumeValidationError("HITL timeout clock must be timezone-aware")
    elapsed = (now - request.created_at).total_seconds()
    if elapsed < request.timeout_seconds:
        raise HumanInputResumeValidationError(f"HITL request {request_id!r} has not expired")
    if request.timeout_action is HumanInputTimeoutAction.STAY_WAITING:
        raise HumanInputResumeValidationError(
            f"HITL request {request_id!r} uses timeout_action=stay_waiting; "
            "no terminal timeout event should be emitted"
        )
    return create_hitl_timed_out_event(request, reason=reason)


def _pending_human_input_snapshot_by_id(
    events: Iterable[BaseEvent], request_id: str
) -> HumanInputSnapshot:
    snapshots = project_human_input_state(events)
    matching = tuple(snapshot for snapshot in snapshots if snapshot.request_id == request_id)
    if not matching:
        raise HumanInputResumeValidationError(
            f"HITL request {request_id!r} was not found in replayed state"
        )
    snapshot = matching[-1]
    if snapshot.state is not HumanInputState.PENDING:
        raise HumanInputResumeValidationError(
            f"HITL request {request_id!r} is not pending; current state is {snapshot.state.value}"
        )
    return snapshot


def pending_human_input_snapshot_for_response(
    events: Iterable[BaseEvent],
    response: HumanInputResponse,
) -> HumanInputSnapshot:
    """Resolve the pending request snapshot targeted by ``response``.

    Raises :class:`HumanInputResumeValidationError` when no pending request can
    be resumed.  Terminal requests are reported distinctly from unknown request
    IDs so CLI/MCP surfaces can present actionable errors.
    """

    snapshots = project_human_input_state(events)
    matching = tuple(
        snapshot for snapshot in snapshots if snapshot.request_id == response.request_id
    )
    if not matching:
        raise HumanInputResumeValidationError(
            f"HITL request {response.request_id!r} was not found in replayed state"
        )

    snapshot = matching[-1]
    if snapshot.state is not HumanInputState.PENDING:
        raise HumanInputResumeValidationError(
            f"HITL request {response.request_id!r} is not pending; current state is {snapshot.state.value}"
        )

    _validate_response_context(response, snapshot)
    return snapshot


def human_input_request_from_snapshot(snapshot: HumanInputSnapshot) -> HumanInputRequest:
    """Reconstruct the immutable request contract from a pending snapshot."""

    data = snapshot.request
    try:
        return HumanInputRequest(
            request_id=_required_str(data, "request_id"),
            session_id=_required_str(data, "session_id"),
            run_id=_optional_str(data.get("run_id")),
            invocation_id=_optional_str(data.get("invocation_id")),
            created_by=_required_str(data, "created_by"),
            kind=HumanInputKind(_required_str(data, "kind")),
            source=HumanInputSource(_required_str(data, "source")),
            risk_class=HumanInputRiskClass(_required_str(data, "risk_class")),
            question=_required_str(data, "question"),
            resume_target=_required_str(data, "resume_target"),
            title=_optional_str(data.get("title")),
            body=_optional_str(data.get("body")),
            options=_string_tuple(data.get("options", ())),
            required_permission=_optional_str(data.get("required_permission")),
            timeout_seconds=_optional_int(data.get("timeout_seconds")),
            timeout_action=HumanInputTimeoutAction(
                _optional_str(data.get("timeout_action"))
                or HumanInputTimeoutAction.STAY_WAITING.value
            ),
            surface=_optional_str(data.get("surface")),
            payload=_plain_mapping(data.get("payload", {})),
            created_at=_datetime_from_payload(data.get("created_at"), fallback=snapshot.created_at),
        )
    except (TypeError, ValueError) as exc:
        raise HumanInputResumeValidationError(
            f"HITL request {snapshot.request_id!r} cannot be reconstructed from persisted state"
        ) from exc


def _validate_response_context(response: HumanInputResponse, snapshot: HumanInputSnapshot) -> None:
    for field_name in ("session_id", "run_id", "invocation_id"):
        response_value = getattr(response, field_name)
        if response_value is None:
            continue
        snapshot_value = getattr(snapshot, field_name)
        if snapshot_value is not None and response_value != snapshot_value:
            raise HumanInputResumeValidationError(
                f"HITL response {field_name} must match pending request state"
            )


def _required_str(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"missing non-empty {key}")


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        return value
    raise TypeError("expected int or None")


def _string_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise TypeError("expected sequence of strings")
    if not isinstance(value, list | tuple):
        raise TypeError("expected sequence of strings")
    return tuple(str(item) for item in value)


def _plain_mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError("expected mapping")
    return {str(key): _plain_json_value(item) for key, item in value.items()}


def _plain_json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_plain_json_value(item) for item in value]
    return value


def _datetime_from_payload(value: Any, *, fallback: datetime) -> datetime:
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return fallback
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return fallback
        return parsed
    return fallback


__all__ = [
    "HumanInputResumeValidationError",
    "create_validated_hitl_resume_event",
    "create_validated_hitl_timeout_event",
    "human_input_request_from_snapshot",
    "pending_human_input_snapshot_for_response",
]
