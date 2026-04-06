"""Example wiring for a Discord/OpenClaw transport bridge.

This is a reference example, not a production-ready bot.
It shows how a Discord/OpenClaw runtime could connect:

    raw channel event
      -> OpenClawTransportBridge
      -> OpenClawWorkflowOrchestrator
      -> OpenClawWorkflowAdapter
      -> ouroboros_channel_workflow MCP tool

Replace the fake transport/client pieces with your real Discord/OpenClaw
implementations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.client.protocol import MCPClient
from ouroboros.mcp.errors import MCPClientError
from ouroboros.openclaw import (
    OpenClawTransport,
    OpenClawTransportBridge,
    OpenClawWorkflowAdapter,
    OpenClawWorkflowOrchestrator,
)


@dataclass
class DiscordTransport(OpenClawTransport):
    """Replace this with your real Discord/OpenClaw transport implementation."""

    sent_messages: list[tuple[str, str | None, str, dict[str, Any]]] = field(default_factory=list)

    async def post_message(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        # In production:
        # await discord_client.send_message(channel_id=channel_id, text=text)
        self.sent_messages.append((channel_id, guild_id, text, meta))


@dataclass
class OpenClawDiscordBridge:
    """Reference composition for Discord/OpenClaw message handling."""

    mcp_client: MCPClient
    transport: OpenClawTransport

    @classmethod
    def create(
        cls,
        *,
        mcp_client: MCPClient,
        transport: OpenClawTransport,
        wait_timeout_seconds: int = 30,
        max_waits: int = 30,
    ) -> OpenClawDiscordBridge:
        """Create the composed bridge stack."""
        adapter = OpenClawWorkflowAdapter(client=mcp_client)
        orchestrator = OpenClawWorkflowOrchestrator(
            adapter=adapter,
            wait_timeout_seconds=wait_timeout_seconds,
            max_waits=max_waits,
        )
        bridge = OpenClawTransportBridge(
            orchestrator=orchestrator,
            transport=transport,
        )
        instance = cls(mcp_client=mcp_client, transport=transport)
        instance._bridge = bridge  # type: ignore[attr-defined]
        return instance

    async def on_channel_message(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        user_id: str | None,
        message: str,
    ) -> Result[None, MCPClientError]:
        """Handle a real inbound Discord/OpenClaw message event."""
        result = await self._bridge.handle_payload(  # type: ignore[attr-defined]
            {
                "channel_id": channel_id,
                "guild_id": guild_id,
                "user_id": user_id,
                "message": message,
            }
        )
        if result.is_err:
            return Result.err(result.error)
        return Result.ok(None)
