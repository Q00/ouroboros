"""Replay projection for durable human-in-the-loop WAIT/RESUME state.

This module is a narrow #960 read-model slice over the existing HITL event
contract. It intentionally stays pure: callers provide an ordered event stream
from the EventStore and receive immutable request snapshots that can be used by
CLI/MCP/runtime surfaces to list pending waits, detect terminal answers, and
resume without reparsing raw event payloads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ouroboros.core.hitl_contract import (
    HumanInputRequest,
    HumanInputResponse,
    HumanInputResponseKind,
)
from ouroboros.events.base import BaseEvent


class HumanInputState(StrEnum):
    """Effective replay state for one HITL request."""

    PENDING = "pending"
    ANSWERED = "answered"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


_TERMINAL_STATES = frozenset(
    {
        HumanInputState.ANSWERED,
        HumanInputState.TIMED_OUT,
        HumanInputState.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class HumanInputSnapshot:
    """Immutable read-model row for a single HITL request.

    ``request`` and ``response`` are normalized event payloads, not live
    dataclass instances. Keeping the raw JSON-safe contract data avoids adding a
    second parser while preserving enough information for durable WAIT/RESUME
    query surfaces.
    """

    request_id: str
    state: HumanInputState
    request_event_id: str
    updated_event_id: str
    created_at: datetime
    updated_at: datetime
    session_id: str
    resume_target: str
    run_id: str | None = None
    invocation_id: str | None = None
    terminal_event_id: str | None = None
    actor: str | None = None
    reason: str | None = None
    request: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    response: Mapping[str, Any] | None = None

    @property
    def is_terminal(self) -> bool:
        """Return whether this request can no longer be resumed as pending."""

        return self.state in _TERMINAL_STATES


def project_human_input_state(events: Iterable[BaseEvent]) -> tuple[HumanInputSnapshot, ...]:
    """Project HITL request snapshots from an ordered event stream.

    Orphan terminal events are ignored because they cannot be resumed safely
    without a prior ``hitl.requested`` payload. If an already terminal request
    receives later HITL events, the first terminal state is retained so replay is
    deterministic and double-terminal histories remain visible in raw events.
    """

    snapshots: dict[str, HumanInputSnapshot] = {}
    order: list[str] = []

    for event in events:
        request_id = _event_request_id(event)
        if request_id is None:
            continue

        if event.type == HumanInputRequest.REQUESTED_EVENT_TYPE:
            current = snapshots.get(request_id)
            if current is not None and current.is_terminal:
                continue
            snapshot = _snapshot_from_requested(event, request_id)
            if request_id not in snapshots:
                order.append(request_id)
            # A pre-terminal duplicate request refreshes the request payload
            # while preserving insertion order. Once terminal, first terminal
            # state wins so replay cannot reopen an already-resolved wait.
            snapshots[request_id] = snapshot
            continue

        current = snapshots.get(request_id)
        if current is None or current.is_terminal:
            continue

        if event.type == HumanInputResponse.ANSWERED_EVENT_TYPE:
            snapshots[request_id] = _snapshot_from_terminal(
                current,
                event,
                state=_state_from_answered_event(event),
                response=_frozen_payload(event.data),
            )
            continue

        if event.type == HumanInputRequest.TIMED_OUT_EVENT_TYPE:
            snapshots[request_id] = _snapshot_from_terminal(
                current,
                event,
                state=HumanInputState.TIMED_OUT,
                reason=_optional_str(event.data.get("reason")),
            )
            continue

        if event.type == HumanInputRequest.CANCELLED_EVENT_TYPE:
            snapshots[request_id] = _snapshot_from_terminal(
                current,
                event,
                state=HumanInputState.CANCELLED,
                actor=_optional_str(event.data.get("actor")),
                reason=_optional_str(event.data.get("reason")),
            )

    return tuple(snapshots[request_id] for request_id in order if request_id in snapshots)


def pending_human_input_requests(events: Iterable[BaseEvent]) -> tuple[HumanInputSnapshot, ...]:
    """Return only pending HITL requests from an ordered event stream."""

    return tuple(
        snapshot
        for snapshot in project_human_input_state(events)
        if snapshot.state is HumanInputState.PENDING
    )


def _snapshot_from_requested(event: BaseEvent, request_id: str) -> HumanInputSnapshot:
    session_id = _required_str(event.data, "session_id", event.id)
    resume_target = _required_str(event.data, "resume_target", event.id)
    return HumanInputSnapshot(
        request_id=request_id,
        state=HumanInputState.PENDING,
        request_event_id=event.id,
        updated_event_id=event.id,
        created_at=event.timestamp,
        updated_at=event.timestamp,
        session_id=session_id,
        resume_target=resume_target,
        run_id=_optional_str(event.data.get("run_id")),
        invocation_id=_optional_str(event.data.get("invocation_id")),
        request=_frozen_payload(event.data),
    )


def _snapshot_from_terminal(
    current: HumanInputSnapshot,
    event: BaseEvent,
    *,
    state: HumanInputState,
    actor: str | None = None,
    reason: str | None = None,
    response: Mapping[str, Any] | None = None,
) -> HumanInputSnapshot:
    return replace(
        current,
        state=state,
        updated_event_id=event.id,
        updated_at=event.timestamp,
        terminal_event_id=event.id,
        actor=actor if actor is not None else _optional_str(event.data.get("actor")),
        reason=reason,
        response=response,
    )


def _state_from_answered_event(event: BaseEvent) -> HumanInputState:
    response_kind = event.data.get("response_kind")
    if response_kind == HumanInputResponseKind.CANCEL.value:
        return HumanInputState.CANCELLED
    if response_kind == HumanInputResponseKind.TIMEOUT.value:
        return HumanInputState.TIMED_OUT
    return HumanInputState.ANSWERED


def _event_request_id(event: BaseEvent) -> str | None:
    if event.type not in {
        HumanInputRequest.REQUESTED_EVENT_TYPE,
        HumanInputResponse.ANSWERED_EVENT_TYPE,
        HumanInputRequest.TIMED_OUT_EVENT_TYPE,
        HumanInputRequest.CANCELLED_EVENT_TYPE,
    }:
        return None
    value = event.data.get("request_id") or event.aggregate_id
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _required_str(data: Mapping[str, Any], key: str, event_id: str) -> str:
    value = data.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    msg = f"HITL requested event {event_id} missing non-empty {key!r}"
    raise ValueError(msg)


def _optional_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _frozen_payload(data: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType({key: _freeze_value(value) for key, value in data.items()})


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


__all__ = [
    "HumanInputSnapshot",
    "HumanInputState",
    "pending_human_input_requests",
    "project_human_input_state",
]
