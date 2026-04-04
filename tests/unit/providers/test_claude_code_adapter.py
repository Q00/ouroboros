"""Unit tests for ouroboros.providers.claude_code_adapter module.

Tests that system prompts are properly extracted from messages and passed
via options_kwargs["system_prompt"] to ClaudeAgentOptions, rather than
being embedded as XML in the user prompt.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import ProviderError
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

    @pytest.mark.asyncio
    async def test_default_tool_policy_omits_allowed_tools_and_uses_configured_cwd(self) -> None:
        """Default Claude adapters should not force a blanket no-tools policy."""
        adapter = ClaudeCodeAdapter(cwd="/tmp/project")
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
            await adapter._execute_single_request("test prompt", config)

        options_call_kwargs = mock_options_cls.call_args.kwargs
        assert "allowed_tools" not in options_call_kwargs
        assert options_call_kwargs["cwd"] == "/tmp/project"
        assert "Write" in options_call_kwargs["disallowed_tools"]

    @pytest.mark.asyncio
    async def test_explicit_empty_allowed_tools_blocks_all_sdk_tools(self) -> None:
        """An explicit empty list keeps the strict no-tools interview policy."""
        adapter = ClaudeCodeAdapter(allowed_tools=[])
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
            await adapter._execute_single_request("test prompt", config)

        options_call_kwargs = mock_options_cls.call_args.kwargs
        assert options_call_kwargs["allowed_tools"] == []
        assert "Read" in options_call_kwargs["disallowed_tools"]


class TestErrorDiagnostics:
    """Tests for error diagnostic paths in _execute_single_request."""

    @pytest.mark.asyncio
    async def test_sdk_exception_produces_provider_error_with_details(self) -> None:
        """SDK exception is caught and returns ProviderError with diagnostic details."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def failing_query(*args, **kwargs):
            raise RuntimeError("SDK connection lost")
            yield  # noqa: unreachable — makes this an async generator

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=failing_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_err
        error = result.error
        assert isinstance(error, ProviderError)
        assert "SDK connection lost" in error.message
        assert error.details["error_type"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_sdk_exception_includes_stderr_in_details(self) -> None:
        """SDK exception captures stderr lines in error details."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def failing_query(*args, **kwargs):
            raise subprocess.CalledProcessError(1, "claude")
            yield  # noqa: unreachable

        import subprocess

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=failing_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_err
        assert "stderr" in result.error.details

    @pytest.mark.asyncio
    async def test_cancelled_error_is_not_swallowed(self) -> None:
        """asyncio.CancelledError propagates instead of being wrapped."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def cancelled_query(*args, **kwargs):
            raise asyncio.CancelledError()
            yield  # noqa: unreachable

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=cancelled_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ), pytest.raises(asyncio.CancelledError):
            await adapter._execute_single_request("test prompt", config)

    @pytest.mark.asyncio
    async def test_empty_response_with_session_id(self) -> None:
        """Empty response with session_id returns descriptive error."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def empty_query(*args, **kwargs):
            # SystemMessage with session_id but no content
            sys_msg = MagicMock()
            type(sys_msg).__name__ = "SystemMessage"
            sys_msg.data = {"session_id": "sess_abc123"}
            yield sys_msg
            # ResultMessage with empty content
            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = ""
            result_msg.is_error = False
            yield result_msg

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=empty_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_err
        assert "sess_abc123" in result.error.details.get("session_id", "")
        assert "Empty response" in result.error.message

    @pytest.mark.asyncio
    async def test_empty_response_without_session_id(self) -> None:
        """Empty response without session_id suggests retry."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def empty_no_session_query(*args, **kwargs):
            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = ""
            result_msg.is_error = False
            yield result_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=empty_no_session_query)
        )

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_err
        assert "retry" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_sdk_error_message_includes_stderr(self) -> None:
        """SDK is_error result includes stderr in ProviderError details."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def error_query(*args, **kwargs):
            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = "Rate limit exceeded"
            result_msg.is_error = True
            yield result_msg

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=error_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_err
        assert "Rate limit exceeded" in result.error.message
        assert "stderr" in result.error.details


class TestProviderErrorFormatDetails:
    """Tests for ProviderError.format_details method."""

    def test_format_details_with_all_fields(self) -> None:
        """format_details renders all diagnostic fields."""
        error = ProviderError(
            message="SDK failed",
            details={
                "error_type": "RuntimeError",
                "session_id": "sess_abc",
                "claudecode_present": True,
                "claude_code_entrypoint": "sdk-py",
                "stderr": "error: auth failed",
            },
        )
        rendered = error.format_details()
        assert "SDK failed" in rendered
        assert "error_type: RuntimeError" in rendered
        assert "session_id: sess_abc" in rendered
        assert "stderr tail:\nerror: auth failed" in rendered

    def test_format_details_without_details(self) -> None:
        """format_details falls back to str(error) when no details."""
        error = ProviderError(message="Simple error")
        rendered = error.format_details()
        assert rendered == str(error)

    def test_format_details_skips_empty_values(self) -> None:
        """format_details skips fields with falsy values."""
        error = ProviderError(
            message="Partial error",
            details={
                "error_type": "ValueError",
                "session_id": "",
                "stderr": "",
            },
        )
        rendered = error.format_details()
        assert "error_type: ValueError" in rendered
        # Empty values should not get their own formatted lines
        assert "session_id:" not in rendered
        assert "stderr tail:" not in rendered
