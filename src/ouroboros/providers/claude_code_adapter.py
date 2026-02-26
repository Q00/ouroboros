"""Claude Code adapter for LLM completion using Claude Agent SDK.

This adapter uses the Claude Agent SDK to make completion requests,
leveraging the user's Claude Code Max Plan authentication instead of
requiring separate API keys.

Usage:
    adapter = ClaudeCodeAdapter()
    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="Hello!")],
        config=CompletionConfig(model="claude-sonnet-4-20250514"),
    )

Custom CLI Path:
    You can specify a custom Claude CLI binary path to use instead of
    the SDK's bundled CLI. This is useful for:
    - Using an instrumented CLI wrapper (e.g., for OTEL tracing)
    - Testing with a specific CLI version
    - Using a locally built CLI

    Set via constructor parameter or environment variable:
        adapter = ClaudeCodeAdapter(cli_path="/path/to/claude")
        # or
        export OUROBOROS_CLI_PATH=/path/to/claude
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import os
from pathlib import Path

import structlog

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)

log = structlog.get_logger(__name__)

# Retry configuration for transient API errors
_MAX_RETRIES = 3
_INITIAL_BACKOFF_SECONDS = 2.0  # Increased for custom CLI startup
_RETRYABLE_ERROR_PATTERNS = (
    "concurrency",
    "rate",
    "timeout",
    "overloaded",
    "temporarily",
    "empty response",  # custom CLI startup delay
    "need retry",  # explicit retry request
    "startup",
)


class ClaudeCodeAdapter:
    """LLM adapter using Claude Agent SDK (Claude Code Max Plan).

    This adapter provides the same interface as LiteLLMAdapter but uses
    the Claude Agent SDK under the hood. This allows users to leverage
    their Claude Code Max Plan subscription without needing separate API keys.

    Attributes:
        cli_path: Path to the Claude CLI binary. If not set, the SDK will
            use its bundled CLI. Set this to use a custom/instrumented CLI.

    Example:
        adapter = ClaudeCodeAdapter()
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="Hello!")],
            config=CompletionConfig(model="claude-sonnet-4-20250514"),
        )
        if result.is_ok:
            print(result.value.content)

    Example with custom CLI:
        adapter = ClaudeCodeAdapter(cli_path="/usr/local/bin/claude")
    """

    def __init__(
        self,
        permission_mode: str = "default",
        cli_path: str | Path | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 1,
        on_message: Callable[[str, str], None] | None = None,
    ) -> None:
        """Initialize Claude Code adapter.

        Args:
            permission_mode: Permission mode for SDK operations.
                - "default": Standard permissions
                - "acceptEdits": Auto-approve edits (not needed for interview)
            cli_path: Path to the Claude CLI binary. If not provided,
                checks OUROBOROS_CLI_PATH env var, then falls back to
                SDK's bundled CLI.
            allowed_tools: List of tools to allow. None means no tools.
                For interview mode with codebase access, use ["Read", "Glob", "Grep"].
            max_turns: Maximum turns for the conversation. Default 1 for single response.
                Increase for tool use scenarios.
            on_message: Callback for streaming messages. Called with (type, content):
                - ("thinking", "content") for agent reasoning
                - ("tool", "tool_name") for tool usage
        """
        self._permission_mode: str = permission_mode
        self._cli_path: Path | None = self._resolve_cli_path(cli_path)
        self._allowed_tools: list[str] = allowed_tools or []
        self._max_turns: int = max_turns
        self._on_message: Callable[[str, str], None] | None = on_message
        log.info(
            "claude_code_adapter.initialized",
            permission_mode=permission_mode,
            cli_path=str(self._cli_path) if self._cli_path else None,
        )

    def _resolve_cli_path(self, cli_path: str | Path | None) -> Path | None:
        """Resolve the CLI path from parameter, config, or environment variable.

        Priority:
            1. Explicit cli_path parameter
            2. OUROBOROS_CLI_PATH environment variable
            3. config.yaml orchestrator.cli_path
            4. None (SDK default)

        Args:
            cli_path: Explicit CLI path from constructor.

        Returns:
            Resolved Path if set and exists, None otherwise (falls back to SDK default).
        """
        # Priority: explicit parameter > env var / config > SDK default
        if cli_path:
            path_str = str(cli_path)
        else:
            # Use config helper (checks env var then config.yaml)
            from ouroboros.config import get_cli_path

            path_str = get_cli_path() or ""
        path_str = path_str.strip()

        if not path_str:
            return None

        resolved = Path(path_str).expanduser().resolve()

        if not resolved.exists():
            log.warning(
                "claude_code_adapter.cli_path_not_found",
                cli_path=str(resolved),
                fallback="using SDK bundled CLI",
            )
            return None

        if not resolved.is_file():
            log.warning(
                "claude_code_adapter.cli_path_not_file",
                cli_path=str(resolved),
                fallback="using SDK bundled CLI",
            )
            return None

        if not os.access(resolved, os.X_OK):
            log.warning(
                "claude_code_adapter.cli_not_executable",
                cli_path=str(resolved),
                fallback="using SDK bundled CLI",
            )
            return None

        log.debug(
            "claude_code_adapter.using_custom_cli",
            cli_path=str(resolved),
        )
        return resolved

    def _is_retryable_error(self, error_msg: str) -> bool:
        """Check if an error message indicates a transient/retryable error.

        Args:
            error_msg: The error message to check.

        Returns:
            True if the error is likely transient and worth retrying.
        """
        error_lower = error_msg.lower()
        return any(pattern in error_lower for pattern in _RETRYABLE_ERROR_PATTERNS)

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request via Claude Agent SDK with retry logic.

        Implements exponential backoff for transient errors like API concurrency
        conflicts that can occur when running inside an active Claude Code session.

        Args:
            messages: The conversation messages to send.
            config: Configuration for the completion request.

        Returns:
            Result containing either the completion response or a ProviderError.
        """
        try:
            # Lazy import to avoid loading SDK at module import time
            from claude_agent_sdk import ClaudeAgentOptions, query  # noqa: F401
        except ImportError as e:
            log.error("claude_code_adapter.sdk_not_installed", error=str(e))
            return Result.err(
                ProviderError(
                    message="Claude Agent SDK is not installed. Run: pip install claude-agent-sdk",
                    details={"import_error": str(e)},
                )
            )

        # Build prompt from messages
        prompt = self._build_prompt(messages)

        log.debug(
            "claude_code_adapter.request_started",
            prompt_preview=prompt[:100],
            message_count=len(messages),
        )

        last_error: ProviderError | None = None

        for attempt in range(_MAX_RETRIES):
            try:
                result = await self._execute_single_request(prompt, config)

                if result.is_ok:
                    if attempt > 0:
                        log.info(
                            "claude_code_adapter.retry_succeeded",
                            attempts=attempt + 1,
                        )
                    return result

                # Check if error is retryable
                error_msg = result.error.message
                if self._is_retryable_error(error_msg) and attempt < _MAX_RETRIES - 1:
                    backoff = _INITIAL_BACKOFF_SECONDS * (2**attempt)
                    log.warning(
                        "claude_code_adapter.retryable_error",
                        error=error_msg,
                        attempt=attempt + 1,
                        max_retries=_MAX_RETRIES,
                        backoff_seconds=backoff,
                    )
                    last_error = result.error
                    await asyncio.sleep(backoff)
                    continue

                # Non-retryable error
                return result

            except Exception as e:
                error_str = str(e)
                error_type = type(e).__name__

                # Handle unknown message types from SDK (e.g., rate_limit_event)
                # These are transient SDK issues that should be retried
                is_unknown_message = (
                    "Unknown message type" in error_str or error_type == "MessageParseError"
                )

                if (
                    self._is_retryable_error(error_str) or is_unknown_message
                ) and attempt < _MAX_RETRIES - 1:
                    backoff = _INITIAL_BACKOFF_SECONDS * (2**attempt)
                    log.warning(
                        "claude_code_adapter.retryable_exception",
                        error=error_str,
                        error_type=error_type,
                        attempt=attempt + 1,
                        max_retries=_MAX_RETRIES,
                        backoff_seconds=backoff,
                    )
                    last_error = ProviderError(
                        message=f"Claude Agent SDK request failed: {e}",
                        details={"error_type": error_type, "attempt": attempt + 1},
                    )
                    await asyncio.sleep(backoff)
                    continue

                log.exception(
                    "claude_code_adapter.request_failed",
                    error=error_str,
                    error_type=error_type,
                )
                return Result.err(
                    ProviderError(
                        message=f"Claude Agent SDK request failed: {e}",
                        details={"error_type": error_type},
                    )
                )

        # All retries exhausted
        log.error(
            "claude_code_adapter.max_retries_exceeded",
            max_retries=_MAX_RETRIES,
        )
        return Result.err(last_error or ProviderError(message="Max retries exceeded"))

    async def _execute_single_request(
        self,
        prompt: str,
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Execute a single SDK request without retry logic.

        Separated to avoid break statements in async generator loops,
        which can cause anyio cancel scope issues.

        Args:
            prompt: The formatted prompt string.
            config: Configuration for the completion request.

        Returns:
            Result containing either the completion response or a ProviderError.
        """
        from claude_agent_sdk import ClaudeAgentOptions, query

        # Build options based on configured tool permissions
        # Type ignore needed because SDK uses Literal type but we store as str
        #
        # Strategy: Block dangerous tools explicitly, allow everything else
        # This enables MCP tools (mcp__*) and read-only built-in tools
        dangerous_tools = ["Write", "Edit", "Bash", "Task", "NotebookEdit"]

        # If allowed_tools is specified, compute disallowed from it (strict mode)
        # Otherwise, only block dangerous tools (permissive mode for MCP)
        if self._allowed_tools:
            all_tools = [
                "Read",
                "Write",
                "Edit",
                "Bash",
                "WebFetch",
                "WebSearch",
                "Glob",
                "Grep",
                "Task",
                "NotebookEdit",
                "TodoRead",
                "TodoWrite",
                "LS",
            ]
            disallowed = [t for t in all_tools if t not in self._allowed_tools]
        else:
            disallowed = dangerous_tools

        options = ClaudeAgentOptions(
            allowed_tools=self._allowed_tools if self._allowed_tools else [],
            disallowed_tools=disallowed,
            max_turns=self._max_turns,
            # Allow MCP and other ~/.claude/ settings to be inherited
            permission_mode=self._permission_mode,  # type: ignore[arg-type]
            cwd=os.getcwd(),
            cli_path=self._cli_path,
        )

        # Collect the response - let the generator run to completion
        content = ""
        session_id = None
        error_result: ProviderError | None = None

        # Wrap query() to skip unknown message types (e.g., rate_limit_event)
        # that the SDK doesn't recognize yet. Without this, a single
        # MessageParseError inside the generator kills the entire request.
        async def _safe_query():
            from claude_agent_sdk._errors import MessageParseError as _MPE

            gen = query(prompt=prompt, options=options).__aiter__()
            while True:
                try:
                    yield await gen.__anext__()
                except _MPE as parse_err:
                    log.debug("claude_code_adapter.skipping_unknown_message", error=str(parse_err))
                    continue
                except StopAsyncIteration:
                    break

        async for sdk_message in _safe_query():
            class_name = type(sdk_message).__name__

            if class_name == "SystemMessage":
                # Capture session ID from init
                msg_data = getattr(sdk_message, "data", {})
                session_id = msg_data.get("session_id")

            elif class_name == "AssistantMessage":
                # Extract text content and tool use
                content_blocks = getattr(sdk_message, "content", [])
                for block in content_blocks:
                    block_type = type(block).__name__
                    if block_type == "TextBlock":
                        text = getattr(block, "text", "")
                        content += text
                        # Callback for thinking/reasoning
                        if self._on_message and text.strip():
                            self._on_message("thinking", text.strip())
                    elif block_type == "ToolUseBlock":
                        tool_name = getattr(block, "name", "unknown")
                        tool_input = getattr(block, "input", {})
                        # Format tool info with key details
                        tool_info = self._format_tool_info(tool_name, tool_input)
                        # Callback for tool usage
                        if self._on_message:
                            self._on_message("tool", tool_info)

            elif class_name == "ResultMessage":
                # Final result - use result content if we don't have content yet
                if not content:
                    content = getattr(sdk_message, "result", "") or ""

                # Check for errors - don't break, just record
                is_error = getattr(sdk_message, "is_error", False)
                if is_error:
                    error_msg = content or "Unknown error from Claude Agent SDK"
                    log.warning(
                        "claude_code_adapter.sdk_error",
                        error=error_msg,
                    )
                    error_result = ProviderError(
                        message=error_msg,
                        details={"session_id": session_id},
                    )

        # After generator completes naturally, check for errors
        if error_result:
            return Result.err(error_result)

        # Check for empty response — always an error regardless of session_id
        if not content:
            if session_id:
                log.warning(
                    "claude_code_adapter.empty_response",
                    content_length=0,
                    session_id=session_id,
                    hint="CLI started but produced no content",
                )
                return Result.err(
                    ProviderError(
                        message="Empty response from CLI - session started but no content produced",
                        details={"session_id": session_id, "content_length": 0},
                    )
                )
            else:
                log.warning(
                    "claude_code_adapter.empty_response",
                    content_length=0,
                    session_id=session_id,
                    hint="CLI may still be starting (custom CLI sync, etc.)",
                )
                return Result.err(
                    ProviderError(
                        message="Empty response from CLI - may need retry (timeout/startup)",
                        details={"session_id": session_id, "content_length": 0},
                    )
                )

        log.info(
            "claude_code_adapter.request_completed",
            content_length=len(content),
            session_id=session_id,
        )

        # Build response
        response = CompletionResponse(
            content=content,
            model=config.model,
            usage=UsageInfo(
                prompt_tokens=0,  # SDK doesn't expose token counts
                completion_tokens=0,
                total_tokens=0,
            ),
            finish_reason="stop",
            raw_response={"session_id": session_id},
        )

        return Result.ok(response)

    def _format_tool_info(self, tool_name: str, tool_input: dict) -> str:
        """Format tool name and input for display.

        Args:
            tool_name: Name of the tool being used.
            tool_input: Input parameters for the tool.

        Returns:
            Formatted string like "Read: /path/to/file" or "Glob: **/*.py"
        """
        # Extract key info based on tool type
        detail = ""
        if tool_name == "Read":
            detail = tool_input.get("file_path", "")
        elif tool_name == "Glob" or tool_name == "Grep":
            detail = tool_input.get("pattern", "")
        elif tool_name == "WebFetch":
            detail = tool_input.get("url", "")
        elif tool_name == "WebSearch":
            detail = tool_input.get("query", "")
        elif tool_name.startswith("mcp__"):
            # MCP tools - show first non-empty input value
            for v in tool_input.values():
                if v:
                    detail = str(v)[:50]
                    break

        if detail:
            # Truncate long details
            if len(detail) > 60:
                detail = detail[:57] + "..."
            return f"{tool_name}: {detail}"
        return tool_name

    def _build_prompt(self, messages: list[Message]) -> str:
        """Build a single prompt string from messages.

        The Claude Agent SDK expects a single prompt string, so we combine
        the conversation history into a formatted prompt.

        Args:
            messages: List of conversation messages.

        Returns:
            Formatted prompt string.
        """
        parts: list[str] = []

        for msg in messages:
            if msg.role == MessageRole.SYSTEM:
                parts.append(f"<system>\n{msg.content}\n</system>\n")
            elif msg.role == MessageRole.USER:
                parts.append(f"User: {msg.content}\n")
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"Assistant: {msg.content}\n")

        # Add instruction to respond
        parts.append("\nPlease respond to the above conversation.")

        return "\n".join(parts)


__all__ = ["ClaudeCodeAdapter"]
