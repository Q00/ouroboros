"""High-level orchestration helper for OpenClaw channel workflows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPClientError
from ouroboros.openclaw.adapter import OpenClawAdapterResponse, OpenClawWorkflowAdapter
from ouroboros.openclaw.contracts import OpenClawChannelEvent, OpenClawWorkflowCommand


class OpenClawReplySink(Protocol):
    """Transport-facing sink for channel replies."""

    async def send_reply(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        """Send a reply back to the originating channel."""
        ...

@dataclass
class OpenClawWorkflowOrchestrator:
    """Coordinate an adapter call plus change-driven waiting for updates."""

    adapter: OpenClawWorkflowAdapter
    wait_timeout_seconds: int = 30
    max_waits: int = 30

    async def handle_event(
        self,
        event: OpenClawChannelEvent,
        sink: OpenClawReplySink,
    ) -> Result[OpenClawAdapterResponse, MCPClientError]:
        """Handle an inbound channel event and emit replies via the sink."""
        initial_result = await self.adapter.handle_event(event)
        if initial_result.is_err:
            return Result.err(initial_result.error)

        initial = initial_result.value
        await sink.send_reply(
            channel_id=event.channel_id,
            guild_id=event.guild_id,
            text=initial.reply_text,
            meta=initial.meta,
        )

        if self._needs_wait(initial):
            wait_result = await self._wait_until_stable(event, sink, initial)
            if wait_result.is_err:
                return Result.err(wait_result.error)
            if wait_result.value is not None:
                return Result.ok(wait_result.value)

        return Result.ok(initial)

    async def _wait_until_stable(
        self,
        event: OpenClawChannelEvent,
        sink: OpenClawReplySink,
        initial: OpenClawAdapterResponse,
    ) -> Result[OpenClawAdapterResponse | None, MCPClientError]:
        """Wait for workflow changes until execution settles."""
        last_text: str | None = initial.reply_text
        last_response: OpenClawAdapterResponse | None = initial

        for _ in range(self.max_waits):
            wait_command = OpenClawWorkflowCommand.wait(
                channel_id=event.channel_id,
                guild_id=event.guild_id,
                timeout_seconds=self.wait_timeout_seconds,
            )
            wait_result = await self.adapter.dispatch(wait_command)
            if wait_result.is_err:
                return Result.err(wait_result.error)

            response = wait_result.value
            last_response = response
            if response.reply_text != last_text:
                await sink.send_reply(
                    channel_id=event.channel_id,
                    guild_id=event.guild_id,
                    text=response.reply_text,
                    meta=response.meta,
                )
                last_text = response.reply_text

            if not self._needs_wait(response):
                return Result.ok(response)

        return Result.ok(last_response)

    @staticmethod
    def _needs_wait(response: OpenClawAdapterResponse) -> bool:
        """Return True when the workflow should continue waiting for execution updates."""
        stage = response.meta.get("stage")
        job_status = response.meta.get("job_status")
        return stage == "executing" or job_status in {"running", "queued", "cancel_requested"}
