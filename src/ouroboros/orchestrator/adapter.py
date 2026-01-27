"""Claude Agent SDK adapter for Ouroboros orchestrator.

This module provides a wrapper around the Claude Agent SDK that:
- Normalizes SDK messages to internal AgentMessage format
- Handles streaming with async generators
- Maps SDK exceptions to Ouroboros error types
- Supports configurable tools and permission modes

Usage:
    adapter = ClaudeAgentAdapter(api_key="...")
    async for message in adapter.execute_task(
        prompt="Fix the bug in auth.py",
        tools=["Read", "Edit", "Bash"],
    ):
        print(message.content)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import os
from typing import TYPE_CHECKING, Any

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)


# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """Normalized message from Claude Agent SDK.

    Attributes:
        type: Message type ("assistant", "tool", "result", "system").
        content: Human-readable content.
        tool_name: Name of tool being called (if type="tool").
        data: Additional message data.
    """

    type: str
    content: str
    tool_name: str | None = None
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_final(self) -> bool:
        """Return True if this is the final result message."""
        return self.type == "result"

    @property
    def is_error(self) -> bool:
        """Return True if this message indicates an error."""
        return self.data.get("subtype") == "error"


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Result of executing a task via Claude Agent.

    Attributes:
        success: Whether the task completed successfully.
        final_message: The final result message content.
        messages: All messages from the execution.
        session_id: Claude Agent session ID for resumption.
    """

    success: bool
    final_message: str
    messages: tuple[AgentMessage, ...]
    session_id: str | None = None


# =============================================================================
# Adapter
# =============================================================================


# Default tools for code execution tasks
DEFAULT_TOOLS: list[str] = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


class ClaudeAgentAdapter:
    """Adapter for Claude Agent SDK with streaming support.

    This adapter wraps the Claude Agent SDK's query() function to provide:
    - Async generator interface for message streaming
    - Normalized message format (AgentMessage)
    - Error handling with Result type
    - Configurable tools and permission modes

    Example:
        adapter = ClaudeAgentAdapter(permission_mode="acceptEdits")

        async for message in adapter.execute_task(
            prompt="Review and fix bugs in auth.py",
            tools=["Read", "Edit", "Bash"],
        ):
            if message.type == "assistant":
                print(f"Claude: {message.content[:100]}")
            elif message.type == "tool":
                print(f"Using tool: {message.tool_name}")
    """

    def __init__(
        self,
        api_key: str | None = None,
        permission_mode: str = "acceptEdits",
    ) -> None:
        """Initialize Claude Agent adapter.

        Args:
            api_key: Anthropic API key. If not provided, uses ANTHROPIC_API_KEY
                    environment variable or Claude Code CLI authentication.
            permission_mode: Permission mode for tool execution.
                - "acceptEdits": Auto-approve file edits
                - "bypassPermissions": Run without prompts (CI/CD)
                - "default": Require canUseTool callback
        """
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._permission_mode = permission_mode

        log.info(
            "orchestrator.adapter.initialized",
            permission_mode=permission_mode,
            has_api_key=bool(self._api_key),
        )

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task and yield progress messages.

        This is an async generator that streams messages as Claude works.
        Use async for to consume messages in real-time.

        Args:
            prompt: The task for Claude to perform.
            tools: List of tools Claude can use. Defaults to DEFAULT_TOOLS.
            system_prompt: Optional custom system prompt.
            resume_session_id: Session ID to resume from.

        Yields:
            AgentMessage for each SDK message (assistant reasoning, tool calls, results).

        Raises:
            ProviderError: If SDK initialization fails.
        """
        try:
            # Lazy import to avoid loading SDK at module import time
            from claude_agent_sdk import ClaudeAgentOptions, query
        except ImportError as e:
            log.error(
                "orchestrator.adapter.sdk_not_installed",
                error=str(e),
            )
            yield AgentMessage(
                type="result",
                content="Claude Agent SDK is not installed. Run: pip install claude-agent-sdk",
                data={"subtype": "error"},
            )
            return

        effective_tools = tools or DEFAULT_TOOLS

        log.info(
            "orchestrator.adapter.task_started",
            prompt_preview=prompt[:100],
            tools=effective_tools,
            has_system_prompt=bool(system_prompt),
            resume_session_id=resume_session_id,
        )

        try:
            # Build options
            import os
            options_kwargs: dict[str, Any] = {
                "allowed_tools": effective_tools,
                "permission_mode": self._permission_mode,
                "cwd": os.getcwd(),  # Use current working directory
            }

            if system_prompt:
                options_kwargs["system_prompt"] = system_prompt

            if resume_session_id:
                options_kwargs["resume"] = resume_session_id

            options = ClaudeAgentOptions(**options_kwargs)

            # Stream messages from SDK
            session_id: str | None = None
            async for sdk_message in query(prompt=prompt, options=options):
                agent_message = self._convert_message(sdk_message)

                # Capture session ID from init message
                if hasattr(sdk_message, "session_id"):
                    session_id = sdk_message.session_id

                # Update data with session_id if available
                if session_id and agent_message.is_final:
                    agent_message = AgentMessage(
                        type=agent_message.type,
                        content=agent_message.content,
                        tool_name=agent_message.tool_name,
                        data={**agent_message.data, "session_id": session_id},
                    )

                yield agent_message

                if agent_message.is_final:
                    log.info(
                        "orchestrator.adapter.task_completed",
                        success=not agent_message.is_error,
                        session_id=session_id,
                    )

        except Exception as e:
            log.exception(
                "orchestrator.adapter.task_failed",
                error=str(e),
            )
            yield AgentMessage(
                type="result",
                content=f"Task execution failed: {e!s}",
                data={"subtype": "error", "error_type": type(e).__name__},
            )

    def _convert_message(self, sdk_message: Any) -> AgentMessage:
        """Convert SDK message to internal AgentMessage format.

        Args:
            sdk_message: Message from Claude Agent SDK.

        Returns:
            Normalized AgentMessage.
        """
        # SDK uses class names, not 'type' attribute
        class_name = type(sdk_message).__name__

        log.debug(
            "orchestrator.adapter.message_received",
            class_name=class_name,
            sdk_message=str(sdk_message)[:500],
        )

        # Extract content based on message class
        content = ""
        tool_name = None
        data: dict[str, Any] = {}
        msg_type = "unknown"

        if class_name == "AssistantMessage":
            msg_type = "assistant"
            # Assistant message with content blocks
            content_blocks = getattr(sdk_message, "content", [])
            for block in content_blocks:
                block_type = type(block).__name__
                if block_type == "TextBlock" and hasattr(block, "text"):
                    content = block.text
                    break
                elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                    tool_name = block.name
                    content = f"Calling tool: {tool_name}"
                    break

        elif class_name == "ResultMessage":
            msg_type = "result"
            # Final result message
            content = getattr(sdk_message, "result", "") or ""
            data["subtype"] = getattr(sdk_message, "subtype", "success")
            data["is_error"] = getattr(sdk_message, "is_error", False)
            data["session_id"] = getattr(sdk_message, "session_id", None)
            log.info(
                "orchestrator.adapter.result_message",
                result_content=content[:200] if content else "empty",
                subtype=data["subtype"],
                is_error=data["is_error"],
            )

        elif class_name == "SystemMessage":
            msg_type = "system"
            subtype = getattr(sdk_message, "subtype", "")
            msg_data = getattr(sdk_message, "data", {})
            if subtype == "init":
                session_id = msg_data.get("session_id")
                content = f"Session initialized: {session_id}"
                data["session_id"] = session_id
            else:
                content = f"System: {subtype}"
            data["subtype"] = subtype

        elif class_name == "UserMessage":
            msg_type = "user"
            # Tool result message
            content_blocks = getattr(sdk_message, "content", [])
            for block in content_blocks:
                if hasattr(block, "content"):
                    content = str(block.content)[:500]
                    break

        else:
            # Unknown message type
            content = str(sdk_message)
            data["raw_class"] = class_name

        return AgentMessage(
            type=msg_type,
            content=content,
            tool_name=tool_name,
            data=data,
        )

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and collect all messages into a TaskResult.

        This is a convenience method that collects all messages from
        execute_task() into a single TaskResult. Use this when you don't
        need streaming progress updates.

        Args:
            prompt: The task for Claude to perform.
            tools: List of tools Claude can use. Defaults to DEFAULT_TOOLS.
            system_prompt: Optional custom system prompt.
            resume_session_id: Session ID to resume from.

        Returns:
            Result containing TaskResult on success, ProviderError on failure.
        """
        messages: list[AgentMessage] = []
        final_message = ""
        success = True
        session_id: str | None = None

        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)

            if message.is_final:
                final_message = message.content
                success = not message.is_error
                session_id = message.data.get("session_id")

        if not success:
            return Result.err(
                ProviderError(
                    message=final_message,
                    details={"messages": [m.content for m in messages]},
                )
            )

        return Result.ok(
            TaskResult(
                success=success,
                final_message=final_message,
                messages=tuple(messages),
                session_id=session_id,
            )
        )


__all__ = [
    "AgentMessage",
    "ClaudeAgentAdapter",
    "DEFAULT_TOOLS",
    "TaskResult",
]
