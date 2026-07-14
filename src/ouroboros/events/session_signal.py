"""Durable lifecycle events for Ouroboros Synapse SessionSignals."""

from __future__ import annotations

from typing import Any, Final

from ouroboros.core.session_signal import (
    SessionSignal,
    SessionSignalCapabilities,
    SessionSignalMode,
    SessionSignalState,
    bounded_session_signal_reply,
)
from ouroboros.events.base import BaseEvent

SESSION_SIGNAL_EVENT_PREFIX: Final[str] = "control.session.signal."
SESSION_SIGNAL_EVENT_TYPES: Final[dict[SessionSignalState, str]] = {
    state: f"{SESSION_SIGNAL_EVENT_PREFIX}{state.value}" for state in SessionSignalState
}

_MAX_EVENT_TEXT_BYTES = 1_000
_MAX_CORRELATION_BYTES = 256


def _bounded_optional(name: str, value: str | None, *, max_bytes: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"SessionSignal event {name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"SessionSignal event {name} must be non-empty")
    if len(normalized.encode("utf-8")) > max_bytes:
        raise ValueError(f"SessionSignal event {name} exceeds {max_bytes} UTF-8 bytes")
    return normalized


def _create_signal_event(
    signal: SessionSignal,
    state: SessionSignalState,
    *,
    effective_mode: SessionSignalMode | None = None,
    include_message: bool = False,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
    runtime_backend: str | None = None,
    extra: dict[str, Any] | None = None,
) -> BaseEvent:
    if not isinstance(state, SessionSignalState):
        raise TypeError("state must be a SessionSignalState")
    if effective_mode is not None and not isinstance(effective_mode, SessionSignalMode):
        raise TypeError("effective_mode must be a SessionSignalMode")

    data = signal.to_event_data(include_message=include_message)
    data["state"] = state.value
    data["is_terminal"] = state.is_terminal
    if effective_mode is not None:
        data["effective_mode"] = effective_mode.value

    bounded_correlations = {
        "job_id": _bounded_optional("job_id", job_id, max_bytes=_MAX_CORRELATION_BYTES),
        "orchestrator_session_id": _bounded_optional(
            "orchestrator_session_id",
            orchestrator_session_id,
            max_bytes=_MAX_CORRELATION_BYTES,
        ),
        "runtime_backend": _bounded_optional(
            "runtime_backend",
            runtime_backend,
            max_bytes=_MAX_CORRELATION_BYTES,
        ),
    }
    data.update({key: value for key, value in bounded_correlations.items() if value is not None})
    if extra:
        data.update(extra)

    return BaseEvent(
        type=SESSION_SIGNAL_EVENT_TYPES[state],
        aggregate_type="session_signal",
        aggregate_id=signal.signal_id,
        data=data,
    )


def create_session_signal_requested_event(
    signal: SessionSignal,
    *,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record the original bounded signal before target/capability validation."""
    return _create_signal_event(
        signal,
        SessionSignalState.REQUESTED,
        include_message=True,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
    )


def create_session_signal_accepted_event(
    signal: SessionSignal,
    *,
    effective_mode: SessionSignalMode,
    capabilities: SessionSignalCapabilities,
    runtime_backend: str | None = None,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record validated addressing and the enforceable effective mode."""
    return _create_signal_event(
        signal,
        SessionSignalState.ACCEPTED,
        effective_mode=effective_mode,
        runtime_backend=runtime_backend,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
        extra={"capabilities": capabilities.to_event_data()},
    )


def create_session_signal_queued_event(
    signal: SessionSignal,
    *,
    effective_mode: SessionSignalMode,
    runtime_backend: str | None = None,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record that Synapse durably owns pending delivery."""
    return _create_signal_event(
        signal,
        SessionSignalState.QUEUED,
        effective_mode=effective_mode,
        runtime_backend=runtime_backend,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
    )


def create_session_signal_delivery_started_event(
    signal: SessionSignal,
    *,
    effective_mode: SessionSignalMode,
    runtime_backend: str | None = None,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record the at-most-once provider-delivery claim before handoff."""
    return _create_signal_event(
        signal,
        SessionSignalState.DELIVERING,
        effective_mode=effective_mode,
        runtime_backend=runtime_backend,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
        extra={"automatic_retry_allowed": False},
    )


def create_session_signal_applied_event(
    signal: SessionSignal,
    *,
    effective_mode: SessionSignalMode,
    acknowledgement: str,
    runtime_backend: str | None = None,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record a runtime/checkpoint acknowledgement of context application."""
    bounded = _bounded_optional(
        "acknowledgement",
        acknowledgement,
        max_bytes=_MAX_EVENT_TEXT_BYTES,
    )
    assert bounded is not None
    return _create_signal_event(
        signal,
        SessionSignalState.APPLIED,
        effective_mode=effective_mode,
        runtime_backend=runtime_backend,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
        extra={"acknowledgement": bounded},
    )


def create_session_signal_rejected_event(
    signal: SessionSignal,
    *,
    rejection_code: str,
    detail: str,
    effective_mode: SessionSignalMode | None = None,
    runtime_backend: str | None = None,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record a fail-closed validation or pre-application rejection."""
    code = _bounded_optional("rejection_code", rejection_code, max_bytes=128)
    bounded_detail = _bounded_optional("detail", detail, max_bytes=_MAX_EVENT_TEXT_BYTES)
    assert code is not None and bounded_detail is not None
    return _create_signal_event(
        signal,
        SessionSignalState.REJECTED,
        effective_mode=effective_mode,
        runtime_backend=runtime_backend,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
        extra={"rejection_code": code, "detail": bounded_detail},
    )


def create_session_signal_delivery_uncertain_event(
    signal: SessionSignal,
    *,
    effective_mode: SessionSignalMode,
    detail: str,
    runtime_backend: str | None = None,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record an ambiguous provider acknowledgement boundary without retrying."""
    bounded_detail = _bounded_optional("detail", detail, max_bytes=_MAX_EVENT_TEXT_BYTES)
    assert bounded_detail is not None
    return _create_signal_event(
        signal,
        SessionSignalState.DELIVERY_UNCERTAIN,
        effective_mode=effective_mode,
        runtime_backend=runtime_backend,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
        extra={"detail": bounded_detail, "automatic_retry_allowed": False},
    )


def create_session_signal_completed_event(
    signal: SessionSignal,
    *,
    effective_mode: SessionSignalMode,
    summary: str,
    reply: str | None = None,
    runtime_backend: str | None = None,
    job_id: str | None = None,
    orchestrator_session_id: str | None = None,
) -> BaseEvent:
    """Record terminal completion with a bounded acknowledgement summary."""
    bounded_summary = _bounded_optional("summary", summary, max_bytes=_MAX_EVENT_TEXT_BYTES)
    assert bounded_summary is not None
    extra = {"summary": bounded_summary}
    if reply is not None:
        extra["reply"] = bounded_session_signal_reply(reply)
    return _create_signal_event(
        signal,
        SessionSignalState.COMPLETED,
        effective_mode=effective_mode,
        runtime_backend=runtime_backend,
        job_id=job_id,
        orchestrator_session_id=orchestrator_session_id,
        extra=extra,
    )


__all__ = [
    "SESSION_SIGNAL_EVENT_PREFIX",
    "SESSION_SIGNAL_EVENT_TYPES",
    "create_session_signal_accepted_event",
    "create_session_signal_applied_event",
    "create_session_signal_completed_event",
    "create_session_signal_delivery_started_event",
    "create_session_signal_delivery_uncertain_event",
    "create_session_signal_queued_event",
    "create_session_signal_rejected_event",
    "create_session_signal_requested_event",
]
