"""Unit tests for ouroboros.providers.claude_code_adapter module.

Tests that system prompts are properly extracted from messages and passed
via options_kwargs["system_prompt"] to ClaudeAgentOptions, rather than
being embedded as XML in the user prompt.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.providers.base import (
    CompletionConfig,
    Message,
    MessageRole,
)
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter


class TestBuildPrompt:
    """Test _build_prompt excludes system messages."""

    def test_build_prompt_no_system_messages(self) -> None:
        """_build_prompt builds correctly with only user/assistant messages."""
        adapter = ClaudeCodeAdapter()
        messages = [
            Message(role=MessageRole.USER, content="Hello"),
            Message(role=MessageRole.ASSISTANT, content="Hi there"),
            Message(role=MessageRole.USER, content="How are you?"),
        ]

        prompt = adapter._build_prompt(messages)

        assert "User: Hello" in prompt
        assert "Assistant: Hi there" in prompt
        assert "User: How are you?" in prompt
        assert "<system>" not in prompt

    def test_build_prompt_warns_on_leaked_system_message(self) -> None:
        """_build_prompt logs warning if a system message leaks through."""
        adapter = ClaudeCodeAdapter()
        messages = [
            Message(role=MessageRole.SYSTEM, content="You are helpful"),
            Message(role=MessageRole.USER, content="Hello"),
        ]

        with patch("ouroboros.providers.claude_code_adapter.log") as mock_log:
            prompt = adapter._build_prompt(messages)

        # Should still render as XML fallback
        assert "<system>" in prompt
        assert "You are helpful" in prompt
        # But should warn
        mock_log.warning.assert_called_once()
        assert "system_message_in_build_prompt" in mock_log.warning.call_args[0][0]

    def test_build_prompt_empty_messages(self) -> None:
        """_build_prompt handles empty message list."""
        adapter = ClaudeCodeAdapter()
        prompt = adapter._build_prompt([])

        assert "Please respond to the above conversation." in prompt


class TestCompleteSystemPromptExtraction:
    """Test that complete() extracts system messages and passes them properly."""

    @pytest.mark.asyncio
    async def test_system_prompt_extracted_and_passed(self) -> None:
        """System prompt is extracted from messages and passed via options_kwargs."""
        adapter = ClaudeCodeAdapter()

        messages = [
            Message(role=MessageRole.SYSTEM, content="You are a Socratic interviewer."),
            Message(role=MessageRole.USER, content="I want to build a CLI tool"),
        ]
        config = CompletionConfig(model="claude-sonnet-4-6")

        # Mock _execute_single_request to capture what it receives
        mock_execute = AsyncMock()
        mock_execute.return_value = MagicMock(is_ok=True)
        adapter._execute_single_request = mock_execute

        # Need to mock the SDK import check in complete()
        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            await adapter.complete(messages, config)

        # Verify _execute_single_request was called with system_prompt
        mock_execute.assert_called_once()
        call_kwargs = mock_execute.call_args
        assert call_kwargs.kwargs["system_prompt"] == "You are a Socratic interviewer."

        # Verify the prompt does NOT contain <system> tags
        prompt_arg = call_kwargs.args[0]
        assert "<system>" not in prompt_arg
        assert "You are a Socratic interviewer." not in prompt_arg

    @pytest.mark.asyncio
    async def test_no_system_messages_omits_system_prompt(self) -> None:
        """When no system messages exist, system_prompt is None."""
        adapter = ClaudeCodeAdapter()

        messages = [
            Message(role=MessageRole.USER, content="Hello"),
        ]
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_execute = AsyncMock()
        mock_execute.return_value = MagicMock(is_ok=True)
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            await adapter.complete(messages, config)

        call_kwargs = mock_execute.call_args
        assert call_kwargs.kwargs["system_prompt"] is None

    @pytest.mark.asyncio
    async def test_non_system_messages_preserved_in_prompt(self) -> None:
        """Non-system messages are still included in the built prompt."""
        adapter = ClaudeCodeAdapter()

        messages = [
            Message(role=MessageRole.SYSTEM, content="System instruction"),
            Message(role=MessageRole.USER, content="User question"),
            Message(role=MessageRole.ASSISTANT, content="Previous answer"),
            Message(role=MessageRole.USER, content="Follow-up"),
        ]
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_execute = AsyncMock()
        mock_execute.return_value = MagicMock(is_ok=True)
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            await adapter.complete(messages, config)

        prompt_arg = mock_execute.call_args.args[0]
        assert "User: User question" in prompt_arg
        assert "Assistant: Previous answer" in prompt_arg
        assert "User: Follow-up" in prompt_arg


def _make_sdk_mock(mock_options_cls: MagicMock, mock_query: MagicMock) -> MagicMock:
    """Build a fake claude_agent_sdk module with _errors submodule."""
    sdk_module = MagicMock()
    sdk_module.ClaudeAgentOptions = mock_options_cls
    sdk_module.query = mock_query

    # _safe_query() does: from claude_agent_sdk._errors import MessageParseError
    errors_module = MagicMock()
    errors_module.MessageParseError = type("MessageParseError", (Exception,), {})
    sdk_module._errors = errors_module

    return sdk_module


class TestExecuteSingleRequestSystemPrompt:
    """Test that _execute_single_request passes system_prompt to ClaudeAgentOptions."""

    @pytest.mark.asyncio
    async def test_system_prompt_in_options_kwargs(self) -> None:
        """system_prompt is added to options_kwargs when provided."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        # Make query return an async generator yielding a ResultMessage
        async def fake_query(*args, **kwargs):
            msg = MagicMock()
            type(msg).__name__ = "ResultMessage"
            msg.structured_output = None
            msg.result = "test response"
            msg.is_error = False
            yield msg

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            await adapter._execute_single_request(
                "test prompt",
                config,
                system_prompt="You are a Socratic interviewer.",
            )

        # Check that ClaudeAgentOptions was called with system_prompt
        options_call_kwargs = mock_options_cls.call_args.kwargs
        assert options_call_kwargs["system_prompt"] == "You are a Socratic interviewer."

    @pytest.mark.asyncio
    async def test_no_system_prompt_omitted_from_options(self) -> None:
        """system_prompt key is omitted from options when not provided."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def fake_query(*args, **kwargs):
            msg = MagicMock()
            type(msg).__name__ = "ResultMessage"
            msg.structured_output = None
            msg.result = "test response"
            msg.is_error = False
            yield msg

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            await adapter._execute_single_request(
                "test prompt",
                config,
                # No system_prompt
            )

        options_call_kwargs = mock_options_cls.call_args.kwargs
        assert "system_prompt" not in options_call_kwargs

    @pytest.mark.asyncio
    async def test_json_schema_is_enforced_via_prompt_not_output_format(self) -> None:
        """json_schema requests should augment the prompt, not SDK output_format."""
        adapter = ClaudeCodeAdapter()
        messages = [Message(role=MessageRole.USER, content="Score this artifact")]
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={
                "type": "json_schema",
                "json_schema": {"type": "object", "properties": {"score": {"type": "number"}}},
            },
        )

        mock_execute = AsyncMock()
        mock_execute.return_value = MagicMock(is_ok=True)
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            await adapter.complete(messages, config)

        prompt_arg = mock_execute.call_args.args[0]
        assert "Respond with ONLY a valid JSON object" in prompt_arg
        assert '"score"' in prompt_arg

    @pytest.mark.asyncio
    async def test_execute_single_request_omits_output_format(self) -> None:
        """SDK options should not include output_format for json_schema requests."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={
                "type": "json_schema",
                "json_schema": {"type": "object", "properties": {"score": {"type": "number"}}},
            },
        )

        mock_options_cls = MagicMock()

        async def fake_query(*args, **kwargs):
            msg = MagicMock()
            type(msg).__name__ = "ResultMessage"
            msg.structured_output = None
            msg.result = '{"score": 0.9}'
            msg.is_error = False
            yield msg

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            await adapter._execute_single_request(
                "test prompt",
                config,
                system_prompt="Return JSON",
            )

        options_call_kwargs = mock_options_cls.call_args.kwargs
        assert "output_format" not in options_call_kwargs
