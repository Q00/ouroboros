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
_INITIAL_BACKOFF_SECONDS = 1.0
_RETRYABLE_ERROR_PATTERNS = (
    "concurrency",
    "rate",
    "timeout",
    "overloaded",
    "temporarily",
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
    ) -> None:
        """Initialize Claude Code adapter.

        Args:
            permission_mode: Permission mode for SDK operations.
                - "default": Standard permissions
                - "acceptEdits": Auto-approve edits (not needed for interview)
            cli_path: Path to the Claude CLI binary. If not provided,
                checks OUROBOROS_CLI_PATH env var, then falls back to
                SDK's bundled CLI.
        """
        self._permission_mode: str = permission_mode
        self._cli_path: Path | None = self._resolve_cli_path(cli_path)
        log.info(
            "claude_code_adapter.initialized",
            permission_mode=permission_mode,
            cli_path=str(self._cli_path) if self._cli_path else None,
        )

    def _resolve_cli_path(self, cli_path: str | Path | None) -> Path | None:
        """Resolve the CLI path from parameter or environment variable.

        Args:
            cli_path: Explicit CLI path from constructor.

        Returns:
            Resolved Path if set and exists, None otherwise (falls back to SDK default).
        """
        # Priority: explicit parameter > environment variable > None (SDK default)
        path_str = str(cli_path) if cli_path else os.environ.get("OUROBOROS_CLI_PATH", "")
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
            from claude_agent_sdk import ClaudeAgentOptions, query
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
                if self._is_retryable_error(error_str) and attempt < _MAX_RETRIES - 1:
                    backoff = _INITIAL_BACKOFF_SECONDS * (2**attempt)
                    log.warning(
                        "claude_code_adapter.retryable_exception",
                        error=error_str,
                        attempt=attempt + 1,
                        max_retries=_MAX_RETRIES,
                        backoff_seconds=backoff,
                    )
                    last_error = ProviderError(
                        message=f"Claude Agent SDK request failed: {e}",
                        details={"error_type": type(e).__name__, "attempt": attempt + 1},
                    )
                    await asyncio.sleep(backoff)
                    continue

                log.exception(
                    "claude_code_adapter.request_failed",
                    error=error_str,
                )
                return Result.err(
                    ProviderError(
                        message=f"Claude Agent SDK request failed: {e}",
                        details={"error_type": type(e).__name__},
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

        # Build options - no tools needed for interview (just conversation)
        # Type ignore needed because SDK uses Literal type but we store as str
        # max_turns=1 ensures single response without tool use attempts
        options = ClaudeAgentOptions(
            allowed_tools=[],  # No tools - pure conversation
            disallowed_tools=["Read", "Write", "Edit", "Bash", "WebFetch", "WebSearch", "Glob", "Grep"],
            max_turns=1,  # Single turn - no tool use loop
            setting_sources=[],  # CRITICAL: Isolate from parent session state (~/.claude/)
            permission_mode=self._permission_mode,  # type: ignore[arg-type]
            cwd=os.getcwd(),
            cli_path=self._cli_path,
        )

        # Collect the response - let the generator run to completion
        content = ""
        session_id = None
        error_result: ProviderError | None = None

        async for sdk_message in query(prompt=prompt, options=options):
            class_name = type(sdk_message).__name__

            if class_name == "SystemMessage":
                # Capture session ID from init
                msg_data = getattr(sdk_message, "data", {})
                session_id = msg_data.get("session_id")

            elif class_name == "AssistantMessage":
                # Extract text content
                content_blocks = getattr(sdk_message, "content", [])
                for block in content_blocks:
                    if type(block).__name__ == "TextBlock":
                        content += getattr(block, "text", "")

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
