"""Deterministic lifecycle projection for Ouroboros Synapse events."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ouroboros.core.session_signal import (
    SessionSignalContractEffect,
    SessionSignalMode,
    SessionSignalSource,
    SessionSignalState,
)
from ouroboros.events.base import BaseEvent

_EVENT_PREFIX = "control.session.signal."
_ALLOWED_TRANSITIONS: dict[SessionSignalState | None, frozenset[SessionSignalState]] = {
    None: frozenset({SessionSignalState.REQUESTED}),
    SessionSignalState.REQUESTED: frozenset(
        {SessionSignalState.ACCEPTED, SessionSignalState.REJECTED}
    ),
    SessionSignalState.ACCEPTED: frozenset(
        {SessionSignalState.QUEUED, SessionSignalState.REJECTED}
    ),
    SessionSignalState.QUEUED: frozenset(
        {
            SessionSignalState.DELIVERING,
            SessionSignalState.APPLIED,
            SessionSignalState.REJECTED,
            SessionSignalState.DELIVERY_UNCERTAIN,
        }
    ),
    SessionSignalState.DELIVERING: frozenset(
        {
            SessionSignalState.APPLIED,
            SessionSignalState.REJECTED,
            SessionSignalState.DELIVERY_UNCERTAIN,
        }
    ),
    SessionSignalState.APPLIED: frozenset({SessionSignalState.COMPLETED}),
    SessionSignalState.REJECTED: frozenset(),
    SessionSignalState.DELIVERY_UNCERTAIN: frozenset(),
    SessionSignalState.COMPLETED: frozenset(),
}

_IDENTITY_FIELDS = (
    "signal_id",
    "target_session_scope_id",
    "target_session_attempt_id",
    "expected_execution_id",
    "requested_mode",
    "source",
    "idempotency_key",
    "message_digest",
)


@dataclass(frozen=True, slots=True)
class SessionSignalProjection:
    """Replay result for one exact SessionSignal aggregate."""

    signal_id: str
    target_session_scope_id: str
    target_session_attempt_id: str
    expected_execution_id: str
    requested_mode: SessionSignalMode
    effective_mode: SessionSignalMode | None
    source: SessionSignalSource
    idempotency_key: str
    message_digest: str
    state: SessionSignalState
    event_ids: tuple[str, ...]
    contract_effect: SessionSignalContractEffect = SessionSignalContractEffect.ADDITIVE
    acknowledgement: str | None = None
    summary: str | None = None
    reply: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @property
    def effective_idempotency_key(self) -> tuple[str, str, str, str]:
        return (
            self.expected_execution_id,
            self.target_session_scope_id,
            self.target_session_attempt_id,
            self.idempotency_key,
        )


def _state_for_event(event: BaseEvent) -> SessionSignalState:
    if not event.type.startswith(_EVENT_PREFIX):
        raise ValueError(f"Not a SessionSignal event: {event.type}")
    raw_state = event.type.removeprefix(_EVENT_PREFIX)
    try:
        state = SessionSignalState(raw_state)
    except ValueError as exc:
        raise ValueError(f"Unknown SessionSignal event state: {raw_state}") from exc
    if event.data.get("state") != state.value:
        raise ValueError("SessionSignal event type/state mismatch")
    if event.data.get("is_terminal") is not state.is_terminal:
        raise ValueError("SessionSignal event terminality mismatch")
    return state


def _required_string(data: dict[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"SessionSignal event requires non-empty {name}")
    return value


def project_session_signal(events: Iterable[BaseEvent]) -> SessionSignalProjection:
    """Replay an ordered event sequence and reject illegal or divergent state."""
    identity: dict[str, str] | None = None
    state: SessionSignalState | None = None
    effective_mode: SessionSignalMode | None = None
    contract_effect: SessionSignalContractEffect | None = None
    acknowledgement: str | None = None
    summary: str | None = None
    reply: str | None = None
    event_ids: list[str] = []
    seen_event_ids: set[str] = set()
    aggregate_id: str | None = None

    for event in events:
        if event.id in seen_event_ids:
            continue
        seen_event_ids.add(event.id)

        if event.aggregate_type != "session_signal":
            raise ValueError("SessionSignal events must aggregate by session_signal")
        if aggregate_id is None:
            aggregate_id = event.aggregate_id
        elif event.aggregate_id != aggregate_id:
            raise ValueError("Cannot project multiple SessionSignal aggregates together")

        next_state = _state_for_event(event)
        if next_state not in _ALLOWED_TRANSITIONS[state]:
            previous = state.value if state is not None else "<none>"
            raise ValueError(f"Illegal SessionSignal transition: {previous} -> {next_state.value}")

        current_identity = {name: _required_string(event.data, name) for name in _IDENTITY_FIELDS}
        if identity is None:
            identity = current_identity
        elif current_identity != identity:
            raise ValueError("SessionSignal identity changed within one aggregate")
        if identity["signal_id"] != event.aggregate_id:
            raise ValueError("SessionSignal payload signal_id must match aggregate_id")

        current_contract_effect = SessionSignalContractEffect(
            str(event.data.get("contract_effect", SessionSignalContractEffect.ADDITIVE.value))
        )
        if contract_effect is None:
            contract_effect = current_contract_effect
        elif current_contract_effect is not contract_effect:
            raise ValueError("SessionSignal contract_effect changed within one aggregate")

        raw_effective_mode = event.data.get("effective_mode")
        if raw_effective_mode is not None:
            if not isinstance(raw_effective_mode, str):
                raise ValueError("SessionSignal effective_mode must be a string")
            current_effective_mode = SessionSignalMode(raw_effective_mode)
            if effective_mode is None:
                effective_mode = current_effective_mode
            elif current_effective_mode is not effective_mode:
                raise ValueError("SessionSignal effective_mode changed after acceptance")
        elif next_state not in {SessionSignalState.REQUESTED, SessionSignalState.REJECTED}:
            raise ValueError(f"SessionSignal {next_state.value} requires effective_mode")

        state = next_state
        raw_acknowledgement = event.data.get("acknowledgement")
        if isinstance(raw_acknowledgement, str):
            acknowledgement = raw_acknowledgement
        raw_summary = event.data.get("summary")
        if isinstance(raw_summary, str):
            summary = raw_summary
        raw_reply = event.data.get("reply")
        if isinstance(raw_reply, str):
            reply = raw_reply
        event_ids.append(event.id)

    if identity is None or state is None:
        raise ValueError("Cannot project an empty SessionSignal event sequence")

    return SessionSignalProjection(
        signal_id=identity["signal_id"],
        target_session_scope_id=identity["target_session_scope_id"],
        target_session_attempt_id=identity["target_session_attempt_id"],
        expected_execution_id=identity["expected_execution_id"],
        requested_mode=SessionSignalMode(identity["requested_mode"]),
        effective_mode=effective_mode,
        source=SessionSignalSource(identity["source"]),
        idempotency_key=identity["idempotency_key"],
        message_digest=identity["message_digest"],
        state=state,
        event_ids=tuple(event_ids),
        contract_effect=contract_effect or SessionSignalContractEffect.ADDITIVE,
        acknowledgement=acknowledgement,
        summary=summary,
        reply=reply,
    )


def can_supersede_session_signal(
    pending_source: SessionSignalSource,
    incoming_source: SessionSignalSource,
) -> bool:
    """Return whether incoming authority is at least as strong as pending authority."""
    if not isinstance(pending_source, SessionSignalSource):
        raise TypeError("pending_source must be SessionSignalSource")
    if not isinstance(incoming_source, SessionSignalSource):
        raise TypeError("incoming_source must be SessionSignalSource")
    return incoming_source.priority >= pending_source.priority


__all__ = [
    "SessionSignalProjection",
    "can_supersede_session_signal",
    "project_session_signal",
]
