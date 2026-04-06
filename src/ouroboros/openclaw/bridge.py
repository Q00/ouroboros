"""Concrete transport-facing bridge for OpenClaw/Discord-style adapters.

This module intentionally avoids a hard dependency on a specific Discord SDK.
Instead, it provides a thin bridge that:

1. normalizes inbound transport payloads into ``OpenClawChannelEvent``
2. hands them to ``OpenClawWorkflowOrchestrator``
3. sends replies back through a minimal transport protocol
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPClientError
from ouroboros.openclaw.contracts import OpenClawChannelEvent
from ouroboros.openclaw.orchestrator import OpenClawReplySink, OpenClawWorkflowOrchestrator


class OpenClawTransport(Protocol):
    """Minimal transport contract for Discord/OpenClaw message delivery."""

    async def post_message(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        """Post a message back to the originating channel."""
        ...


def event_from_payload(payload: dict[str, Any]) -> OpenClawChannelEvent:
    """Normalize a generic OpenClaw/Discord payload into a channel event."""
    channel_id = payload.get("channel_id")
    message = payload.get("message")
    if not isinstance(channel_id, str) or not channel_id.strip():
        msg = "payload.channel_id is required"
        raise ValueError(msg)
    if not isinstance(message, str) or not message.strip():
        msg = "payload.message is required"
        raise ValueError(msg)

    guild_id = payload.get("guild_id")
    user_id = payload.get("user_id")
    message_id = payload.get("message_id")
    event_id = payload.get("event_id")

    return OpenClawChannelEvent(
        channel_id=channel_id.strip(),
        guild_id=guild_id.strip() if isinstance(guild_id, str) and guild_id.strip() else None,
        user_id=user_id.strip() if isinstance(user_id, str) and user_id.strip() else None,
        message=message.strip(),
        message_id=message_id.strip() if isinstance(message_id, str) and message_id.strip() else None,
        event_id=event_id.strip() if isinstance(event_id, str) and event_id.strip() else None,
    )


@dataclass
class OpenClawTransportBridge(OpenClawReplySink):
    """Attach the OpenClaw workflow orchestrator to a real message transport."""

    orchestrator: OpenClawWorkflowOrchestrator
    transport: OpenClawTransport

    async def handle_payload(
        self,
        payload: dict[str, Any],
    ) -> Result[OpenClawChannelEvent, MCPClientError]:
        """Handle a raw inbound payload from a Discord/OpenClaw transport."""
        try:
            event = event_from_payload(payload)
        except ValueError as exc:
            return Result.err(MCPClientError.from_exception(exc))
        result = await self.orchestrator.handle_event(event, self)
        if result.is_err:
            return Result.err(result.error)
        return Result.ok(event)

    async def send_reply(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        """Forward replies from the orchestrator to the transport."""
        await self.transport.post_message(
            channel_id=channel_id,
            guild_id=guild_id,
            text=text,
            meta=meta,
        )
