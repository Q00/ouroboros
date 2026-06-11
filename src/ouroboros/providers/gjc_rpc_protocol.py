"""Shared GJC RPC protocol helpers."""

from __future__ import annotations

from typing import Any

from ouroboros.core.errors import ProviderError

RESPONSE_TYPE = "response"
READY_TYPE = "ready"
EVENT_ENVELOPE_TYPE = "event"

AGENT_EVENT_TYPES = {
    "agent_start",
    "turn_start",
    "message_start",
    "message_update",
    "message_end",
    "turn_end",
    "agent_end",
}

PASSIVE_LIFECYCLE_EVENT_TYPES = {
    "tool_execution_start",
    "tool_execution_update",
    "tool_execution_end",
    "auto_compaction_start",
    "auto_compaction_end",
    "auto_compaction_skipped",
    "auto_retry_start",
    "auto_retry_end",
    "auto_retry_exhausted",
    "retry_fallback_applied",
    "retry_fallback_succeeded",
    "ttsr_triggered",
    "todo_reminder",
    "todo_auto_clear",
    "irc_message",
    "notice",
    "thinking_level_changed",
    "goal_updated",
}

UNSUPPORTED_BIDIRECTIONAL_FRAME_TYPES = {
    "workflow_gate",
    "extension_ui_request",
    "host_tool_call",
    "host_tool_cancel",
    "host_uri_request",
    "host_uri_cancel",
}

SUPPORTED_EVENT_TYPES = (
    AGENT_EVENT_TYPES | PASSIVE_LIFECYCLE_EVENT_TYPES | {READY_TYPE, RESPONSE_TYPE}
)


class GjcProtocolError(ProviderError):
    """Raised when GJC violates the RPC state machine."""


class GjcCommandError(ProviderError):
    """Raised when a correlated GJC command ack reports failure."""


class UnsupportedGjcRpcFrame(ProviderError):
    """Raised when GJC requests an unsupported host/runtime interaction."""


def is_passive_lifecycle_event(event: dict[str, Any]) -> bool:
    """Return whether an event is documented passive lifecycle/status noise."""
    return event.get("type") in PASSIVE_LIFECYCLE_EVENT_TYPES


def unwrap_event_envelope(frame: dict[str, Any], *, provider: str = "gjc") -> dict[str, Any] | None:
    """Return the inner agent event of a protocol-v2 agent-wire envelope, else None.

    GJC >= 0.4 emits every agent session event wrapped as
    ``{"type": "event", "protocol_version": 2, "seq": ..., "frame_id": ...,
    "payload": {"event_type": ..., "event": {...}}}`` (gjc docs/rpc.md), while
    command acks (``response``) and ``ready`` remain top-level frames. Returns
    ``None`` for non-envelope frames so bare (pre-envelope) events keep working.

    Raises:
        GjcProtocolError: for an envelope frame whose payload carries no event.
    """
    if frame.get("type") != EVENT_ENVELOPE_TYPE:
        return None
    payload = frame.get("payload")
    if isinstance(payload, dict):
        inner = payload.get("event")
        if isinstance(inner, dict):
            return inner
    raise GjcProtocolError(
        message="Malformed GJC event envelope: payload.event missing or not an object",
        provider=provider,
    )


def unsupported_frame_error(
    event: dict[str, Any], *, provider: str = "gjc"
) -> UnsupportedGjcRpcFrame | None:
    """Return a fail-closed error for unsupported bidirectional or unknown frames."""
    event_type = event.get("type")
    if event_type in SUPPORTED_EVENT_TYPES:
        return None
    frame_type = str(event_type or "unknown")
    frame_id = event.get("id") or event.get("gate_id") or event.get("gateId")
    detail = f"Unsupported GJC RPC frame: type={frame_type}"
    if frame_id is not None:
        detail = f"{detail} id={frame_id}"
    return UnsupportedGjcRpcFrame(
        message=detail, provider=provider, details={"frame_type": frame_type, "id": frame_id}
    )


def validate_response_ack(
    event: dict[str, Any],
    *,
    command_id: str,
    command: str,
    provider: str = "gjc",
) -> None:
    """Validate a same-id successful response ack for the expected command."""
    if event.get("type") != RESPONSE_TYPE or event.get("id") != command_id:
        raise GjcProtocolError(
            message=f"GJC {command} command was not acknowledged by the matching response id",
            provider=provider,
        )
    if event.get("command") != command:
        raise GjcProtocolError(
            message=f"GJC {command} command ack had wrong command {event.get('command')!r}",
            provider=provider,
        )
    if event.get("success") is not True:
        raise GjcCommandError(
            message=str(event.get("error") or f"GJC {command} command failed"),
            provider=provider,
        )
