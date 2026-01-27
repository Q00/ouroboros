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
"""

from __future__ import annotations

import os

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


class ClaudeCodeAdapter:
    """LLM adapter using Claude Agent SDK (Claude Code Max Plan).

    This adapter provides the same interface as LiteLLMAdapter but uses
    the Claude Agent SDK under the hood. This allows users to leverage
    their Claude Code Max Plan subscription without needing separate API keys.

    Example:
        adapter = ClaudeCodeAdapter()
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="Hello!")],
            config=CompletionConfig(model="claude-sonnet-4-20250514"),
        )
        if result.is_ok:
            print(result.value.content)
    """

    def __init__(
        self,
        permission_mode: str = "default",
    ) -> None:
        """Initialize Claude Code adapter.

        Args:
            permission_mode: Permission mode for SDK operations.
                - "default": Standard permissions
                - "acceptEdits": Auto-approve edits (not needed for interview)
        """
        self._permission_mode: str = permission_mode
        log.info(
            "claude_code_adapter.initialized",
            permission_mode=permission_mode,
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request via Claude Agent SDK.

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

        try:
            # Build options - no tools needed for interview (just conversation)
            # Type ignore needed because SDK uses Literal type but we store as str
            options = ClaudeAgentOptions(
                allowed_tools=[],  # No tools - pure conversation
                permission_mode=self._permission_mode,  # type: ignore[arg-type]
                cwd=os.getcwd(),
            )

            # Collect the response
            content = ""
            session_id = None

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

                    # Check for errors
                    is_error = getattr(sdk_message, "is_error", False)
                    if is_error:
                        error_msg = content or "Unknown error from Claude Agent SDK"
                        log.warning(
                            "claude_code_adapter.sdk_error",
                            error=error_msg,
                        )
                        return Result.err(
                            ProviderError(
                                message=error_msg,
                                details={"session_id": session_id},
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

        except Exception as e:
            log.exception(
                "claude_code_adapter.request_failed",
                error=str(e),
            )
            return Result.err(
                ProviderError(
                    message=f"Claude Agent SDK request failed: {e}",
                    details={"error_type": type(e).__name__},
                )
            )

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
