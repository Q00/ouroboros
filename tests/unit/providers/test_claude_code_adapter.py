"""Unit tests for ClaudeCodeAdapter empty-response retry cap (P0-AC1)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.claude_code_adapter import (
    ClaudeCodeAdapter,
    _MAX_EMPTY_RESPONSE_WITH_SESSION,
)


@pytest.fixture
def adapter() -> ClaudeCodeAdapter:
    """Create adapter with no CLI path resolution."""
    with patch.object(ClaudeCodeAdapter, "_resolve_cli_path", return_value=None):
        return ClaudeCodeAdapter()


@pytest.fixture
def config() -> CompletionConfig:
    return CompletionConfig(model="claude-sonnet-4-6")


@pytest.fixture
def messages() -> list[Message]:
    return [Message(role=MessageRole.USER, content="Hello")]


class TestEmptyResponseRetryCap:
    """Tests for P0-AC1: empty-response retry cap + terminal fault code."""

    @pytest.mark.asyncio
    async def test_empty_with_session_stops_after_cap(
        self, adapter: ClaudeCodeAdapter, config: CompletionConfig, messages: list[Message]
    ) -> None:
        """Mock 5 consecutive empty-with-session responses, verify only 2 attempts made."""
        from ouroboros.core.errors import ProviderError

        call_count = 0

        async def mock_execute(prompt, cfg):
            nonlocal call_count
            call_count += 1
            return Result.err(
                ProviderError(
                    message="Empty response from CLI - session started but no content produced",
                    details={"session_id": "sess_abc", "content_length": 0},
                )
            )

        with patch.object(adapter, "_execute_single_request", side_effect=mock_execute):
            with patch("ouroboros.providers.claude_code_adapter.query", create=True):
                with patch("ouroboros.providers.claude_code_adapter.ClaudeAgentOptions", create=True):
                    result = await adapter.complete(messages, config)

        assert result.is_err
        assert call_count == _MAX_EMPTY_RESPONSE_WITH_SESSION  # 2, not 5
        assert result.error.details["fault_code"] == "CLAUDE_EMPTY_RESPONSE"
        assert result.error.details["session_id"] == "sess_abc"
        assert result.error.details["attempt_count"] == _MAX_EMPTY_RESPONSE_WITH_SESSION

    @pytest.mark.asyncio
    async def test_empty_without_session_remains_retryable(
        self, adapter: ClaudeCodeAdapter, config: CompletionConfig, messages: list[Message]
    ) -> None:
        """Mock empty-without-session then success, verify retry works."""
        from ouroboros.core.errors import ProviderError
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        call_count = 0

        async def mock_execute(prompt, cfg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return Result.err(
                    ProviderError(
                        message="Empty response from CLI - may need retry (timeout/startup)",
                        details={"session_id": None, "content_length": 0},
                    )
                )
            return Result.ok(
                CompletionResponse(
                    content="Hello!",
                    model="claude-sonnet-4-6",
                    usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    finish_reason="stop",
                    raw_response={},
                )
            )

        with patch.object(adapter, "_execute_single_request", side_effect=mock_execute):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await adapter.complete(messages, config)

        assert result.is_ok
        assert call_count == 2
        assert result.value.content == "Hello!"

    @pytest.mark.asyncio
    async def test_empty_with_session_fault_code_in_payload(
        self, adapter: ClaudeCodeAdapter, config: CompletionConfig, messages: list[Message]
    ) -> None:
        """Verify fault payload includes fault_code, session_id, attempt_count."""
        from ouroboros.core.errors import ProviderError

        async def mock_execute(prompt, cfg):
            return Result.err(
                ProviderError(
                    message="Empty response from CLI - session started but no content produced",
                    details={"session_id": "sess_xyz", "content_length": 0},
                )
            )

        with patch.object(adapter, "_execute_single_request", side_effect=mock_execute):
            result = await adapter.complete(messages, config)

        assert result.is_err
        details = result.error.details
        assert details["fault_code"] == "CLAUDE_EMPTY_RESPONSE"
        assert details["session_id"] == "sess_xyz"
        assert details["attempt_count"] == 2
        assert "prompt_preview" in details

    @pytest.mark.asyncio
    async def test_non_empty_error_resets_counter(
        self, adapter: ClaudeCodeAdapter, config: CompletionConfig, messages: list[Message]
    ) -> None:
        """A non-empty error between empty responses resets the consecutive counter."""
        from ouroboros.core.errors import ProviderError
        from ouroboros.providers.base import CompletionResponse, UsageInfo

        call_count = 0

        async def mock_execute(prompt, cfg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Empty with session
                return Result.err(
                    ProviderError(
                        message="Empty response from CLI - session started but no content produced",
                        details={"session_id": "sess_1", "content_length": 0},
                    )
                )
            if call_count == 2:
                # Different retryable error (resets counter)
                return Result.err(
                    ProviderError(
                        message="rate limit exceeded",
                        details={},
                    )
                )
            if call_count == 3:
                # Empty with session again (counter should be at 1, not 2)
                return Result.err(
                    ProviderError(
                        message="Empty response from CLI - session started but no content produced",
                        details={"session_id": "sess_1", "content_length": 0},
                    )
                )
            # Success on 4th
            return Result.ok(
                CompletionResponse(
                    content="Done",
                    model="claude-sonnet-4-6",
                    usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                    finish_reason="stop",
                    raw_response={},
                )
            )

        with patch.object(adapter, "_execute_single_request", side_effect=mock_execute):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                result = await adapter.complete(messages, config)

        assert result.is_ok
        assert call_count == 4
