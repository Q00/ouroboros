"""Adapter-facing contracts for OpenClaw channel workflow integration.

These models are intentionally simple and transport-agnostic so a Discord /
OpenClaw adapter can translate incoming channel events into a stable
``ouroboros_channel_workflow`` tool call without depending on MCP internals.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class OpenClawChannelEvent:
    """Normalized inbound channel event from OpenClaw/Discord."""

    channel_id: str
    guild_id: str | None
    user_id: str | None
    message: str
    message_id: str | None = None
    event_id: str | None = None


@dataclass(frozen=True, slots=True)
class OpenClawWorkflowCommand:
    """Stable boundary model for invoking channel workflow operations."""

    action: str
    channel_id: str
    guild_id: str | None = None
    user_id: str | None = None
    message: str | None = None
    repo: str | None = None
    seed_content: str | None = None
    seed_path: str | None = None
    mode: str | None = None
    timeout_seconds: int | None = None
    message_id: str | None = None
    event_id: str | None = None

    @classmethod
    def from_event(
        cls,
        event: OpenClawChannelEvent,
        *,
        repo: str | None = None,
        seed_content: str | None = None,
        seed_path: str | None = None,
        mode: str = "auto",
    ) -> OpenClawWorkflowCommand:
        """Create a workflow command from an inbound channel event."""
        return cls(
            action="message",
            channel_id=event.channel_id,
            guild_id=event.guild_id,
            user_id=event.user_id,
            message=event.message,
            repo=repo,
            seed_content=seed_content,
            seed_path=seed_path,
            mode=mode,
            message_id=event.message_id,
            event_id=event.event_id,
        )

    @classmethod
    def set_repo(
        cls,
        *,
        channel_id: str,
        guild_id: str | None,
        repo: str,
    ) -> OpenClawWorkflowCommand:
        """Build a default-repo configuration command."""
        return cls(
            action="set_repo",
            channel_id=channel_id,
            guild_id=guild_id,
            repo=repo,
        )

    @classmethod
    def status(
        cls,
        *,
        channel_id: str,
        guild_id: str | None,
    ) -> OpenClawWorkflowCommand:
        """Build a status inspection command."""
        return cls(
            action="status",
            channel_id=channel_id,
            guild_id=guild_id,
        )

    @classmethod
    def poll(
        cls,
        *,
        channel_id: str,
        guild_id: str | None,
    ) -> OpenClawWorkflowCommand:
        """Build a poll/update command."""
        return cls(
            action="poll",
            channel_id=channel_id,
            guild_id=guild_id,
        )

    @classmethod
    def wait(
        cls,
        *,
        channel_id: str,
        guild_id: str | None,
        timeout_seconds: int = 30,
    ) -> OpenClawWorkflowCommand:
        """Build a change-driven wait command."""
        return cls(
            action="wait",
            channel_id=channel_id,
            guild_id=guild_id,
            timeout_seconds=timeout_seconds,
        )

    def to_tool_arguments(self) -> dict[str, Any]:
        """Convert to flat MCP tool arguments."""
        data = {
            "action": self.action,
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "user_id": self.user_id,
            "message": self.message,
            "repo": self.repo,
            "seed_content": self.seed_content,
            "seed_path": self.seed_path,
            "mode": self.mode,
            "timeout_seconds": self.timeout_seconds,
            "message_id": self.message_id,
            "event_id": self.event_id,
        }
        return {key: value for key, value in data.items() if value is not None}
