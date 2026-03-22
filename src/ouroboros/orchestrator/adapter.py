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

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import os
from typing import TYPE_CHECKING, Any, Protocol

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.providers.base import CompletionConfig, CompletionResponse, Message

log = get_logger(__name__)


# =============================================================================
# Tool Detail Extraction
# =============================================================================

_TOOL_DETAIL_EXTRACTORS: dict[str, str] = {
    "Read": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "Edit": "file_path",
    "Write": "file_path",
    "Bash": "command",
    "WebFetch": "url",
    "WebSearch": "query",
    "NotebookEdit": "notebook_path",
}


def _format_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
    """Format a human-readable tool detail string.

    Args:
        tool_name: Name of the tool being called.
        tool_input: Raw input dict from ToolUseBlock.

    Returns:
        Formatted string like "Read: src/foo.py" or just "ToolName" if no detail.
    """
    key = _TOOL_DETAIL_EXTRACTORS.get(tool_name)
    if key:
        detail = str(tool_input.get(key, ""))
    elif tool_name.startswith("mcp__"):
        detail = next((str(v)[:80] for v in tool_input.values() if v), "")
    else:
        detail = ""
    if detail and len(detail) > 80:
        detail = detail[:77] + "..."
    return f"{tool_name}: {detail}" if detail else tool_name


def _optional_str(value: object) -> str | None:
    """Return a string value when present, otherwise None."""
    return value if isinstance(value, str) and value else None


DELEGATED_EXECUTE_SEED_TOOL_NAMES: tuple[str, ...] = (
    "ouroboros_execute_seed",
    "ouroboros_start_execute_seed",
)
DELEGATED_EXECUTE_SEED_TOOL_MATCHER = (
    "mcp__plugin_ouroboros_ouroboros__ouroboros_execute_seed|"
    "mcp__plugin_ouroboros_ouroboros__ouroboros_start_execute_seed|"
    "mcp__ouroboros__ouroboros_execute_seed|"
    "mcp__ouroboros__ouroboros_start_execute_seed|"
    "ouroboros_execute_seed|"
    "ouroboros_start_execute_seed"
)

DELEGATED_PARENT_SESSION_ID_ARG = "_ooo_parent_claude_session_id"
DELEGATED_PARENT_TRANSCRIPT_PATH_ARG = "_ooo_parent_claude_transcript_path"
DELEGATED_PARENT_CWD_ARG = "_ooo_parent_claude_cwd"
DELEGATED_PARENT_PERMISSION_MODE_ARG = "_ooo_parent_claude_permission_mode"
DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG = "_ooo_parent_effective_tools"


def _is_delegated_execute_seed_tool(tool_name: object) -> bool:
    """Return True for delegated execute-seed MCP tool calls."""
    if not isinstance(tool_name, str) or not tool_name:
        return False
    return any(
        tool_name == candidate or tool_name.endswith(f"__{candidate}")
        for candidate in DELEGATED_EXECUTE_SEED_TOOL_NAMES
    )


def _build_delegated_tool_context_update(
    hook_input: dict[str, Any],
    effective_tools: list[str],
) -> dict[str, Any] | None:
    """Inject parent Claude runtime metadata into delegated execute-seed tool input."""
    tool_name = hook_input.get("tool_name")
    if not _is_delegated_execute_seed_tool(tool_name):
        return None

    tool_input = hook_input.get("tool_input")
    if not isinstance(tool_input, dict):
        return None

    updated_input = dict(tool_input)
    updated_input[DELEGATED_PARENT_SESSION_ID_ARG] = hook_input.get("session_id")
    updated_input[DELEGATED_PARENT_TRANSCRIPT_PATH_ARG] = hook_input.get("transcript_path")
    updated_input[DELEGATED_PARENT_CWD_ARG] = hook_input.get("cwd")
    updated_input[DELEGATED_PARENT_PERMISSION_MODE_ARG] = hook_input.get("permission_mode")
    updated_input[DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG] = list(effective_tools)
    return {
        "hookEventName": "PreToolUse",
        "updatedInput": updated_input,
    }


# =============================================================================
# Data Models
# =============================================================================


@dataclass(frozen=True, slots=True)
class RuntimeHandle:
    """Backend-neutral resume handle for agent runtimes.

    Attributes:
        backend: Runtime backend identifier (for example, "claude" or "codex_cli").
        kind: Handle kind for future extensibility.
        native_session_id: Backend-native session identifier when available.
        conversation_id: Durable conversation/thread identifier when applicable.
        previous_response_id: Last response identifier for turn-chaining APIs.
        transcript_path: Optional transcript path for CLI-based runtimes.
        cwd: Working directory used for execution.
        approval_mode: Runtime approval/sandbox mode if available.
        updated_at: ISO timestamp when the handle was last updated.
        metadata: Backend-specific extension data.
    """

    backend: str
    kind: str = "agent_runtime"
    native_session_id: str | None = None
    conversation_id: str | None = None
    previous_response_id: str | None = None
    transcript_path: str | None = None
    cwd: str | None = None
    approval_mode: str | None = None
    updated_at: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the handle for progress persistence."""
        return {
            "backend": self.backend,
            "kind": self.kind,
            "native_session_id": self.native_session_id,
            "conversation_id": self.conversation_id,
            "previous_response_id": self.previous_response_id,
            "transcript_path": self.transcript_path,
            "cwd": self.cwd,
            "approval_mode": self.approval_mode,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, value: object) -> RuntimeHandle | None:
        """Deserialize a runtime handle from persisted progress data."""
        if not isinstance(value, dict):
            return None

        backend = value.get("backend")
        if not isinstance(backend, str) or not backend:
            return None

        metadata = value.get("metadata", {})
        if not isinstance(metadata, dict):
            metadata = {}

        return cls(
            backend=backend,
            kind=str(value.get("kind", "agent_runtime")),
            native_session_id=_optional_str(value.get("native_session_id")),
            conversation_id=_optional_str(value.get("conversation_id")),
            previous_response_id=_optional_str(value.get("previous_response_id")),
            transcript_path=_optional_str(value.get("transcript_path")),
            cwd=_optional_str(value.get("cwd")),
            approval_mode=_optional_str(value.get("approval_mode")),
            updated_at=_optional_str(value.get("updated_at")),
            metadata=metadata,
        )


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """Normalized message from Claude Agent SDK.

    Attributes:
        type: Message type ("assistant", "tool", "result", "system").
        content: Human-readable content.
        tool_name: Name of tool being called (if type="tool").
        data: Additional message data.
        resume_handle: Backend-neutral runtime resume handle, if available.
    """

    type: str
    content: str
    tool_name: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    resume_handle: RuntimeHandle | None = None

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
        resume_handle: Backend-neutral resume handle for resumption.
    """

    success: bool
    final_message: str
    messages: tuple[AgentMessage, ...]
    session_id: str | None = None
    resume_handle: RuntimeHandle | None = None


class AgentRuntime(Protocol):
    """Protocol for autonomous agent runtimes used by the orchestrator."""

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task and stream normalized messages."""

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and return the collected final result."""


# =============================================================================
# Adapter
# =============================================================================


# Default tools for code execution tasks
DEFAULT_TOOLS: list[str] = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

# Retry configuration for transient SDK errors
MAX_RETRIES: int = 3
RETRY_WAIT_INITIAL: float = 1.0  # seconds
RETRY_WAIT_MAX: float = 10.0  # seconds

# Error patterns that indicate transient failures worth retrying
TRANSIENT_ERROR_PATTERNS: tuple[str, ...] = (
    "concurrency",
    "rate limit",
    "429",
    "500",
    "502",
    "503",
    "504",
    "timeout",
    "connection",
    "exit code 1",  # SDK CLI process failed
)


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
        model: str | None = None,
    ) -> None:
        """Initialize Claude Agent adapter.

        Args:
            api_key: Anthropic API key. If not provided, uses ANTHROPIC_API_KEY
                    environment variable or Claude Code CLI authentication.
            permission_mode: Permission mode for tool execution.
                - "acceptEdits": Auto-approve file edits
                - "bypassPermissions": Run without prompts (CI/CD)
                - "default": Require canUseTool callback
            model: Claude model to use (e.g., "claude-sonnet-4-6").
                If not provided, uses the SDK default.
        """
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._permission_mode = permission_mode
        self._model = model

        log.info(
            "orchestrator.adapter.initialized",
            permission_mode=permission_mode,
            has_api_key=bool(self._api_key),
        )

    def _is_transient_error(self, error: Exception) -> bool:
        """Check if an error is transient and worth retrying.

        Args:
            error: The exception to check.

        Returns:
            True if the error appears to be transient.
        """
        error_str = str(error).lower()
        return any(pattern in error_str for pattern in TRANSIENT_ERROR_PATTERNS)

    def _build_runtime_handle(
        self,
        native_session_id: str | None,
        *,
        approval_mode: str | None = None,
    ) -> RuntimeHandle | None:
        """Build a normalized runtime handle for the current Claude session."""
        if not native_session_id:
            return None

        return RuntimeHandle(
            backend="claude",
            native_session_id=native_session_id,
            cwd=os.getcwd(),
            approval_mode=approval_mode or self._permission_mode,
            updated_at=datetime.now(UTC).isoformat(),
        )

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task and yield progress messages.

        This is an async generator that streams messages as Claude works.
        Use async for to consume messages in real-time.

        Args:
            prompt: The task for Claude to perform.
            tools: List of tools Claude can use. Defaults to DEFAULT_TOOLS.
            system_prompt: Optional custom system prompt.
            resume_handle: Backend-neutral handle to resume from.
            resume_session_id: Legacy Claude session ID to resume from.

        Yields:
            AgentMessage for each SDK message (assistant reasoning, tool calls, results).

        Raises:
            ProviderError: If SDK initialization fails.
        """
        try:
            # Lazy import to avoid loading SDK at module import time
            from claude_agent_sdk import ClaudeAgentOptions, query
            from claude_agent_sdk.types import HookMatcher
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
            resume_backend=resume_handle.backend if resume_handle else None,
            resume_session_id=resume_session_id,
        )

        # Retry loop for transient errors
        attempt = 0
        last_error: Exception | None = None
        current_runtime_handle = resume_handle
        current_session_id = (
            resume_handle.native_session_id
            if resume_handle and resume_handle.native_session_id
            else resume_session_id
        )

        while attempt < MAX_RETRIES:
            attempt += 1
            try:
                effective_permission_mode = (
                    current_runtime_handle.approval_mode
                    if current_runtime_handle and current_runtime_handle.approval_mode
                    else self._permission_mode
                )

                # Build options
                options_kwargs: dict[str, Any] = {
                    "allowed_tools": effective_tools,
                    "permission_mode": effective_permission_mode,
                    "cwd": os.getcwd(),  # Use current working directory
                }

                async def _delegated_tool_context_hook(
                    hook_input: dict[str, Any],
                    _tool_name: str | None,
                    _context: dict[str, Any],
                ) -> dict[str, Any] | None:
                    return _build_delegated_tool_context_update(hook_input, effective_tools)

                options_kwargs["hooks"] = {
                    "PreToolUse": [
                        HookMatcher(
                            matcher=DELEGATED_EXECUTE_SEED_TOOL_MATCHER,
                            hooks=[_delegated_tool_context_hook],
                        )
                    ]
                }

                if self._model:
                    options_kwargs["model"] = self._model

                if system_prompt:
                    options_kwargs["system_prompt"] = system_prompt

                if current_session_id:
                    options_kwargs["resume"] = current_session_id
                    if current_runtime_handle and current_runtime_handle.metadata.get(
                        "fork_session"
                    ):
                        options_kwargs["fork_session"] = True

                options = ClaudeAgentOptions(**options_kwargs)

                # Stream messages from SDK
                session_id: str | None = None
                async for sdk_message in query(prompt=prompt, options=options):
                    agent_message = self._convert_message(sdk_message)

                    # Capture session ID from init message
                    session_id = getattr(sdk_message, "session_id", None) or agent_message.data.get(
                        "session_id"
                    )
                    if session_id:
                        current_session_id = session_id  # Save for potential retry
                        current_runtime_handle = self._build_runtime_handle(
                            session_id,
                            approval_mode=effective_permission_mode,
                        )

                    if current_runtime_handle:
                        data = agent_message.data
                        if current_session_id and data.get("session_id") != current_session_id:
                            data = {**data, "session_id": current_session_id}
                        agent_message = replace(
                            agent_message,
                            data=data,
                            resume_handle=current_runtime_handle,
                        )

                    yield agent_message

                    if agent_message.is_final:
                        log.info(
                            "orchestrator.adapter.task_completed",
                            success=not agent_message.is_error,
                            session_id=session_id,
                        )

                # Success - exit retry loop
                return

            except Exception as e:
                last_error = e
                if self._is_transient_error(e) and attempt < MAX_RETRIES:
                    wait_time = min(
                        RETRY_WAIT_INITIAL * (2 ** (attempt - 1)),
                        RETRY_WAIT_MAX,
                    )
                    log.warning(
                        "orchestrator.adapter.transient_error_retry",
                        error=str(e),
                        attempt=attempt,
                        max_retries=MAX_RETRIES,
                        wait_seconds=wait_time,
                        will_resume=bool(current_session_id),
                    )
                    await asyncio.sleep(wait_time)
                    continue
                else:
                    # Non-transient error or max retries reached
                    log.exception(
                        "orchestrator.adapter.task_failed",
                        error=str(e),
                        attempts=attempt,
                    )
                    data = {
                        "subtype": "error",
                        "error_type": type(e).__name__,
                    }
                    if current_session_id:
                        data["session_id"] = current_session_id
                    yield AgentMessage(
                        type="result",
                        content=f"Task execution failed: {e!s}",
                        data=data,
                        resume_handle=current_runtime_handle,
                    )
                    return

        # Max retries exhausted (shouldn't normally reach here)
        if last_error:
            log.error(
                "orchestrator.adapter.max_retries_exhausted",
                error=str(last_error),
                attempts=MAX_RETRIES,
            )
            yield AgentMessage(
                type="result",
                content=f"Task failed after {MAX_RETRIES} retries: {last_error!s}",
                data={
                    "subtype": "error",
                    "error_type": type(last_error).__name__,
                    **({"session_id": current_session_id} if current_session_id else {}),
                },
                resume_handle=current_runtime_handle,
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
            # Assistant message with content blocks -- iterate ALL blocks
            content_blocks = getattr(sdk_message, "content", [])
            text_parts: list[str] = []

            for block in content_blocks:
                block_type = type(block).__name__

                if block_type == "TextBlock" and hasattr(block, "text"):
                    text_parts.append(block.text)

                elif block_type == "ToolUseBlock" and hasattr(block, "name"):
                    tool_name = block.name
                    tool_input = getattr(block, "input", {}) or {}
                    data["tool_input"] = tool_input
                    data["tool_detail"] = _format_tool_detail(tool_name, tool_input)

                elif block_type == "ThinkingBlock":
                    thinking = getattr(block, "thinking", "") or getattr(block, "text", "")
                    if thinking:
                        data["thinking"] = thinking.strip()

            if text_parts:
                content = "\n".join(text_parts)
            elif tool_name:
                content = f"Calling tool: {data.get('tool_detail', tool_name)}"

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

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """LLMAdapter-compatible completion interface.

        Bridges ClaudeAgentAdapter to the LLMAdapter protocol so it can be
        used by InterviewEngine and other components that expect complete().

        Args:
            messages: Conversation messages (system, user, assistant).
            config: Completion configuration (model, temperature, etc.).

        Returns:
            Result containing CompletionResponse or ProviderError.
        """
        from ouroboros.providers.base import (
            CompletionResponse,
            MessageRole,
            UsageInfo,
        )

        # Extract system prompt from messages
        system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
        non_system_msgs = [m for m in messages if m.role != MessageRole.SYSTEM]
        system_prompt = system_msgs[0].content if system_msgs else None

        # Build prompt from non-system messages.
        # For the first interview round, conversation_history is empty
        # so we must provide a minimal user prompt to prevent execute_task()
        # from early-returning. The system_prompt carries the full context.
        prompt_parts: list[str] = []
        for m in non_system_msgs:
            prompt_parts.append(f"[{m.role.value}]\n{m.content}")
        prompt = "\n\n".join(prompt_parts) if prompt_parts else "Proceed."

        # Allow read-only tools so the LLM can explore the codebase
        # when generating interview questions for brownfield projects.
        tools = ["Read", "Glob", "Grep"]
        assistant_texts: list[str] = []
        error_content: str | None = None

        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
        ):
            if message.type == "assistant" and message.content:
                assistant_texts.append(message.content)
            elif message.is_final and message.is_error:
                error_content = message.content

        if error_content:
            return Result.err(
                ProviderError(
                    message=error_content,
                    details={"assistant_texts": assistant_texts},
                )
            )

        # Use the last assistant message as the primary content
        content = assistant_texts[-1] if assistant_texts else ""

        if not content:
            return Result.err(
                ProviderError(
                    message="Empty response from Claude Agent SDK",
                    details={"message_count": len(assistant_texts)},
                )
            )

        return Result.ok(
            CompletionResponse(
                content=content,
                model=self._model or "claude-agent-sdk",
                usage=UsageInfo(
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                ),
                finish_reason="stop",
            )
        )

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
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
            resume_handle: Backend-neutral handle to resume from.
            resume_session_id: Legacy Claude session ID to resume from.

        Returns:
            Result containing TaskResult on success, ProviderError on failure.
        """
        messages: list[AgentMessage] = []
        final_message = ""
        success = True
        session_id: str | None = None
        final_resume_handle = resume_handle

        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)

            if message.resume_handle is not None:
                final_resume_handle = message.resume_handle

            if message.is_final:
                final_message = message.content
                success = not message.is_error
                session_id = message.data.get("session_id")
                if session_id and final_resume_handle is None:
                    final_resume_handle = self._build_runtime_handle(session_id)

        if not success:
            return Result.err(
                ProviderError(
                    message=final_message,
                    details={"messages": [m.content for m in messages]},
                )
            )

        if session_id is None and final_resume_handle is not None:
            session_id = final_resume_handle.native_session_id

        return Result.ok(
            TaskResult(
                success=success,
                final_message=final_message,
                messages=tuple(messages),
                session_id=session_id,
                resume_handle=final_resume_handle,
            )
        )


ClaudeCodeRuntime = ClaudeAgentAdapter


__all__ = [
    "AgentRuntime",
    "AgentMessage",
    "ClaudeAgentAdapter",
    "ClaudeCodeRuntime",
    "DEFAULT_TOOLS",
    "RuntimeHandle",
    "TaskResult",
]
