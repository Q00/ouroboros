"""Unit tests for ouroboros.providers.claude_code_adapter module.

Tests that system prompts are properly extracted from messages and passed
via options_kwargs["system_prompt"] to ClaudeAgentOptions, rather than
being embedded as XML in the user prompt.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
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


def _ok_completion_result(content: str) -> Result[CompletionResponse, object]:
    """Build a successful completion result with realistic typed payloads."""
    return Result.ok(
        CompletionResponse(
            content=content,
            model="claude-sonnet-4-6",
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
            raw_response={"id": "resp_123"},
        )
    )


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


class TestAdapterOverheadReductions:
    """Test per-call overhead optimizations in ClaudeCodeAdapter."""

    @pytest.mark.asyncio
    async def test_version_check_skip_env_defaults_to_one(self) -> None:
        """CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK defaults to '1' when OUROBOROS_SKIP_VERSION_CHECK is unset."""
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

        with (
            patch.dict(
                "sys.modules",
                {
                    "claude_agent_sdk": sdk_module,
                    "claude_agent_sdk._errors": sdk_module._errors,
                },
            ),
            patch.dict("os.environ", {}, clear=False),
        ):
            # Ensure the override var is NOT set
            os.environ.pop("OUROBOROS_SKIP_VERSION_CHECK", None)
            await adapter._execute_single_request("test prompt", config)

        options_call_kwargs = mock_options_cls.call_args.kwargs
        env = options_call_kwargs.get("env", {})
        assert env.get("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK") == "1"

    @pytest.mark.asyncio
    async def test_version_check_skip_env_respects_override(self) -> None:
        """OUROBOROS_SKIP_VERSION_CHECK=0 disables the SDK version-check skip."""
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

        with (
            patch.dict(
                "sys.modules",
                {
                    "claude_agent_sdk": sdk_module,
                    "claude_agent_sdk._errors": sdk_module._errors,
                },
            ),
            patch.dict("os.environ", {"OUROBOROS_SKIP_VERSION_CHECK": "0"}),
        ):
            await adapter._execute_single_request("test prompt", config)

        options_call_kwargs = mock_options_cls.call_args.kwargs
        env = options_call_kwargs.get("env", {})
        assert env.get("CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK") == "0"

    def test_initial_backoff_is_half_second(self) -> None:
        """_INITIAL_BACKOFF_SECONDS should be 0.5 for interactive responsiveness."""
        from ouroboros.providers.claude_code_adapter import _INITIAL_BACKOFF_SECONDS

        assert _INITIAL_BACKOFF_SECONDS == 0.5


class TestJsonSchemaHandling:
    """Test JSON schema handling in ClaudeCodeAdapter."""

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

        mock_execute = AsyncMock(return_value=_ok_completion_result('{"score": 0.9}'))
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            await adapter.complete(messages, config)

        prompt_arg = mock_execute.call_args.args[0]
        assert "Respond with ONLY a valid JSON object" in prompt_arg
        assert '"score"' in prompt_arg

    @pytest.mark.asyncio
    async def test_json_retry_on_prose_response(self) -> None:
        """When response_format requires JSON but LLM returns prose, adapter retries."""
        adapter = ClaudeCodeAdapter()
        messages = [Message(role=MessageRole.USER, content="Evaluate this")]
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={
                "type": "json_schema",
                "json_schema": {"type": "object", "properties": {"score": {"type": "number"}}},
            },
        )

        mock_execute = AsyncMock(
            side_effect=[
                _ok_completion_result("Let me verify the acceptance criteria..."),
                _ok_completion_result('{"score": 0.85}'),
            ]
        )
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert result.value.content == '{"score": 0.85}'
        assert mock_execute.call_count == 2

    @pytest.mark.asyncio
    async def test_json_retry_exhausted_returns_error(self) -> None:
        """When all JSON retries fail, return a ProviderError, not prose."""
        adapter = ClaudeCodeAdapter()
        messages = [Message(role=MessageRole.USER, content="Evaluate this")]
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={
                "type": "json_schema",
                "json_schema": {"type": "object", "properties": {"score": {"type": "number"}}},
            },
        )

        # 1 initial + 3 retries = 4 calls total
        mock_execute = AsyncMock(
            return_value=_ok_completion_result("I cannot produce JSON right now")
        )
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            result = await adapter.complete(messages, config)

        assert result.is_err
        assert "JSON format required" in result.error.message
        assert mock_execute.call_count == 4  # 1 initial + 3 retries

    @pytest.mark.asyncio
    async def test_json_extracted_from_prose_wrapped_response(self) -> None:
        """When response contains valid JSON wrapped in prose, extract and normalize."""
        adapter = ClaudeCodeAdapter()
        messages = [Message(role=MessageRole.USER, content="Evaluate this")]
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={
                "type": "json_schema",
                "json_schema": {"type": "object", "properties": {"score": {"type": "number"}}},
            },
        )

        mock_execute = AsyncMock(
            return_value=_ok_completion_result('Here is the result:\n{"score": 0.85}\nDone.')
        )
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert result.value.content == '{"score": 0.85}'
        assert mock_execute.call_count == 1  # No retry needed

    def test_normalize_json_content_rebuilds_frozen_completion_response(self) -> None:
        """Normalization must not mutate the frozen CompletionResponse dataclass."""
        adapter = ClaudeCodeAdapter()
        response = CompletionResponse(
            content='Here is the result:\n{"score": 0.85}\nDone.',
            model="claude-sonnet-4-6",
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
            raw_response={"id": "resp_123", "meta": {"attempt": 1}},
        )

        result = adapter._normalize_json_content(Result.ok(response))

        assert result is not None
        assert result.is_ok
        assert result.value.content == '{"score": 0.85}'
        assert result.value is not response
        assert response.content == 'Here is the result:\n{"score": 0.85}\nDone.'
        assert result.value.model == response.model
        assert result.value.usage == response.usage
        assert result.value.finish_reason == response.finish_reason
        assert result.value.raw_response is not response.raw_response
        assert result.value.raw_response["meta"] is not response.raw_response["meta"]

        result.value.raw_response["meta"]["attempt"] = 2
        assert response.raw_response["meta"]["attempt"] == 1

    @pytest.mark.asyncio
    async def test_json_normalization_rebuilds_response_without_aliasing_raw_response(self) -> None:
        """complete() should normalize JSON without aliasing nested raw_response data."""
        adapter = ClaudeCodeAdapter()
        messages = [Message(role=MessageRole.USER, content="Evaluate this")]
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={
                "type": "json_schema",
                "json_schema": {"type": "object", "properties": {"score": {"type": "number"}}},
            },
        )

        original_response = CompletionResponse(
            content='Here is the result:\n{"score": 0.85}\nDone.',
            model="claude-sonnet-4-6",
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            finish_reason="stop",
            raw_response={"id": "resp_123", "meta": {"attempt": 1}},
        )
        mock_execute = AsyncMock(return_value=Result.ok(original_response))
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert result.value.content == '{"score": 0.85}'
        assert result.value is not original_response
        assert result.value.raw_response == original_response.raw_response
        assert result.value.raw_response is not original_response.raw_response
        assert result.value.raw_response["meta"] is not original_response.raw_response["meta"]

        result.value.raw_response["meta"]["attempt"] = 2
        assert original_response.raw_response["meta"]["attempt"] == 1
        assert mock_execute.call_count == 1

    @pytest.mark.asyncio
    async def test_json_schema_array_gets_correct_prompt_steering(self) -> None:
        """json_schema with top-level array should say 'JSON array', not 'JSON object'."""
        adapter = ClaudeCodeAdapter()
        messages = [Message(role=MessageRole.USER, content="List items")]
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"name": {"type": "string"}}},
                },
            },
        )

        mock_execute = AsyncMock(return_value=_ok_completion_result('[{"name": "a"}]'))
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            result = await adapter.complete(messages, config)

        prompt_arg = mock_execute.call_args.args[0]
        assert "JSON array" in prompt_arg
        assert "JSON object" not in prompt_arg
        assert result.is_ok
        assert result.value.content == '[{"name": "a"}]'

    @pytest.mark.asyncio
    async def test_json_object_format_gets_prompt_steering(self) -> None:
        """json_object response_format should also get prompt steering."""
        adapter = ClaudeCodeAdapter()
        messages = [Message(role=MessageRole.USER, content="Return data")]
        config = CompletionConfig(
            model="claude-sonnet-4-6",
            response_format={"type": "json_object"},
        )

        mock_execute = AsyncMock(return_value=_ok_completion_result('{"data": "value"}'))
        adapter._execute_single_request = mock_execute

        with patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}):
            await adapter.complete(messages, config)

        prompt_arg = mock_execute.call_args.args[0]
        assert "Respond with ONLY a valid JSON object" in prompt_arg

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
            if False:
                yield
            raise RuntimeError("SDK connection lost")

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
        """SDK exception captures stderr lines in error details and message."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        captured_stderr: dict = {}

        def capture_options(**kwargs):
            captured_stderr["fn"] = kwargs.get("stderr")
            return MagicMock()

        mock_options_cls = MagicMock(side_effect=capture_options)

        async def failing_query(*args, **kwargs):
            # Simulate stderr output before the SDK exception
            if captured_stderr.get("fn"):
                captured_stderr["fn"]("error: connection refused")
                captured_stderr["fn"]("fatal: SDK process died")
            if False:
                yield
            raise RuntimeError("Command failed with exit code 1. Check stderr output for details")

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
        assert "connection refused" in result.error.details["stderr"]
        assert "stderr tail:" in result.error.message
        assert "fatal: SDK process died" in result.error.message

    @pytest.mark.asyncio
    async def test_cancelled_error_is_not_swallowed(self) -> None:
        """asyncio.CancelledError propagates instead of being wrapped."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def cancelled_query(*args, **kwargs):
            if False:
                yield
            raise asyncio.CancelledError()

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=cancelled_query))

        with (
            patch.dict(
                "sys.modules",
                {
                    "claude_agent_sdk": sdk_module,
                    "claude_agent_sdk._errors": sdk_module._errors,
                },
            ),
            pytest.raises(asyncio.CancelledError),
        ):
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

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=empty_no_session_query))

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

        captured_stderr: dict = {}

        def capture_options(**kwargs):
            captured_stderr["fn"] = kwargs.get("stderr")
            return MagicMock()

        mock_options_cls = MagicMock(side_effect=capture_options)

        async def error_query(*args, **kwargs):
            # Simulate stderr before error result
            if captured_stderr.get("fn"):
                captured_stderr["fn"]("warning: rate limit hit")
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
        assert "rate limit hit" in result.error.details["stderr"]


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
        """format_details falls back to message when no details."""
        error = ProviderError(message="Simple error")
        rendered = error.format_details()
        assert rendered == "Simple error"

    def test_format_details_skips_none_values(self) -> None:
        """format_details skips fields with None values."""
        error = ProviderError(
            message="Partial error",
            details={
                "error_type": "ValueError",
                "session_id": None,
                "stderr": "",
            },
        )
        rendered = error.format_details()
        assert "error_type: ValueError" in rendered
        assert "session_id:" not in rendered
        # Empty stderr string should not render stderr tail
        assert "stderr tail:" not in rendered

    def test_format_details_preserves_falsy_values(self) -> None:
        """format_details renders False and 0 instead of dropping them."""
        error = ProviderError(
            message="Diagnostic error",
            details={
                "claudecode_present": False,
                "error_type": "RuntimeError",
            },
        )
        rendered = error.format_details()
        assert "claudecode_present: False" in rendered
        assert "error_type: RuntimeError" in rendered

    def test_format_details_does_not_duplicate_details_dict(self) -> None:
        """format_details uses message, not str(self) which appends raw details."""
        error = ProviderError(
            message="SDK failed",
            details={"error_type": "RuntimeError", "session_id": "sess_1"},
        )
        rendered = error.format_details()
        # Should not contain the raw dict representation
        assert "(details:" not in rendered
