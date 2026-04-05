"""Thin OpenClaw adapter over the ``ouroboros_channel_workflow`` MCP tool."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPClientError
from ouroboros.mcp.types import MCPToolResult
from ouroboros.openclaw.contracts import OpenClawChannelEvent, OpenClawWorkflowCommand
from ouroboros.openclaw.ux import parse_channel_command


class ChannelWorkflowToolCaller(Protocol):
    """Minimal tool-calling interface used by the OpenClaw adapter."""

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Result[MCPToolResult, MCPClientError]:
        """Call an MCP tool."""
        ...


@dataclass(frozen=True, slots=True)
class OpenClawAdapterResponse:
    """Normalized outbound response for an OpenClaw adapter."""

    reply_text: str
    meta: dict[str, Any]
    is_error: bool = False


@dataclass
class OpenClawWorkflowAdapter:
    """Translate channel events into ``ouroboros_channel_workflow`` tool calls."""

    client: ChannelWorkflowToolCaller
    tool_name: str = "ouroboros_channel_workflow"

    async def handle_event(
        self,
        event: OpenClawChannelEvent,
    ) -> Result[OpenClawAdapterResponse, MCPClientError]:
        """Handle a raw inbound channel event."""
        parsed = parse_channel_command(event.message)
        if parsed is None:
            command = OpenClawWorkflowCommand.from_event(event, mode="auto")
        elif parsed.action == "set_repo":
            if not parsed.repo:
                return Result.ok(
                    OpenClawAdapterResponse(
                        reply_text="Usage: /ouro repo set <repo>",
                        meta={"action": "set_repo", "valid": False},
                        is_error=True,
                    )
                )
            command = OpenClawWorkflowCommand.set_repo(
                channel_id=event.channel_id,
                guild_id=event.guild_id,
                repo=parsed.repo,
            )
        elif parsed.action == "status":
            command = OpenClawWorkflowCommand.status(
                channel_id=event.channel_id,
                guild_id=event.guild_id,
            )
        elif parsed.action == "poll":
            command = OpenClawWorkflowCommand.poll(
                channel_id=event.channel_id,
                guild_id=event.guild_id,
            )
        elif parsed.action == "wait":
            command = OpenClawWorkflowCommand.wait(
                channel_id=event.channel_id,
                guild_id=event.guild_id,
            )
        else:
            command = OpenClawWorkflowCommand(
                action="message",
                channel_id=event.channel_id,
                guild_id=event.guild_id,
                user_id=event.user_id,
                message=parsed.message or event.message,
                mode=parsed.mode or "auto",
                message_id=event.message_id,
                event_id=event.event_id,
            )

        return await self.dispatch(command)

    async def dispatch(
        self,
        command: OpenClawWorkflowCommand,
    ) -> Result[OpenClawAdapterResponse, MCPClientError]:
        """Dispatch a normalized workflow command to MCP."""
        result = await self.client.call_tool(self.tool_name, command.to_tool_arguments())
        if result.is_err:
            return Result.err(result.error)
        tool_result = result.value
        return Result.ok(
            OpenClawAdapterResponse(
                reply_text=tool_result.text_content,
                meta=tool_result.meta,
                is_error=tool_result.is_error,
            )
        )
