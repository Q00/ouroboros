"""Unit tests for ouroboros.providers.claude_code_adapter module.

Tests that system prompts are properly extracted from messages and passed
via options_kwargs["system_prompt"] to ClaudeAgentOptions, rather than
being embedded as XML in the user prompt.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.evolution import provider_usage as provider_usage_module
from ouroboros.evolution.provider_usage import (
    capture_generation_provider_usage,
    tracked_complete,
)
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


class TestResolveCliPathPreservesPublicContract:
    """The explicit cli_path override keeps its pre-hardening behavior.

    The untrusted-.env trust boundary is enforced in config.loader, so the
    adapter must NOT second-guess the provenance of an explicit path:
    a relative wrapper override still resolves relative to the cwd.
    """

    def test_relative_explicit_override_resolves_against_cwd(self, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        wrapper = tmp_path / "claude-wrapper"
        wrapper.write_text("#!/bin/sh\n")
        wrapper.chmod(0o755)

        adapter = ClaudeCodeAdapter(cli_path="./claude-wrapper")

        assert adapter._cli_path == wrapper.resolve()

    def test_absolute_override_is_accepted(self, tmp_path) -> None:
        binary = tmp_path / "bin" / "claude"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)

        adapter = ClaudeCodeAdapter(cli_path=str(binary))

        assert adapter._cli_path == binary.resolve()

    def test_missing_sdk_discovers_claude_on_path(self, tmp_path) -> None:
        binary = tmp_path / "claude"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        adapter = ClaudeCodeAdapter()
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        proc = MagicMock(returncode=0)
        proc.communicate = AsyncMock(return_value=(payload, b""))

        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("shutil.which", return_value=str(binary)) as which,
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok
        which.assert_called_once_with("claude")
        assert spawn.await_args.args[0] == str(binary.resolve())


class TestAdapterOverheadReductions:
    """Test per-call overhead optimizations in ClaudeCodeAdapter."""

    def test_with_strict_mcp_config_clones_adapter_config(self) -> None:
        """Explicit strict-MCP opt-in returns a configured clone."""
        allowed_tools = ["Read"]

        def on_message(message_type: str, content: str) -> None:
            assert message_type
            assert content

        adapter = ClaudeCodeAdapter(
            permission_mode="acceptEdits",
            cli_path="/bin/sh",
            cwd="/tmp/project",
            allowed_tools=allowed_tools,
            max_turns=3,
            on_message=on_message,
            timeout=12.5,
        )

        strict_adapter = adapter.with_strict_mcp_config()

        assert strict_adapter is not adapter
        assert adapter._strict_mcp_config is False
        assert strict_adapter._strict_mcp_config is True
        assert strict_adapter._permission_mode == adapter._permission_mode
        assert strict_adapter._cli_path == adapter._cli_path
        assert strict_adapter._cwd == adapter._cwd
        assert strict_adapter._allowed_tools == adapter._allowed_tools
        assert strict_adapter._allowed_tools is not adapter._allowed_tools
        assert strict_adapter._max_turns == adapter._max_turns
        assert strict_adapter._on_message is on_message
        assert strict_adapter._timeout == adapter._timeout

        allowed_tools.append("Grep")
        assert adapter._allowed_tools == ["Read"]
        assert strict_adapter._allowed_tools == ["Read"]

    def test_with_strict_mcp_config_is_idempotent(self) -> None:
        """Already-strict adapters are returned unchanged."""
        adapter = ClaudeCodeAdapter(strict_mcp_config=True)

        assert adapter.with_strict_mcp_config() is adapter

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
        assert "tools" not in options_call_kwargs
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
        assert options_call_kwargs["tools"] == []
        assert options_call_kwargs["extra_args"]["allowedTools"] == ""
        assert "Read" in options_call_kwargs["disallowed_tools"]

    @pytest.mark.asyncio
    async def test_empty_allowed_tools_phantom_tool_use_salvages_streamed_text(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A phantom tool call under the sealed envelope must not discard text.

        With ``allowed_tools=[]`` the visible catalog is emptied via
        ``tools=[]`` (``--tools ""``), so an emitted ToolUseBlock can never
        execute — it is model noise (#1537), not an envelope leak. When a
        usable final text still streams, surface it and keep the incident
        observable through the ``raw_response`` marker instead of failing.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset({"extra_args", "allowed_tools", "tools"}),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True)
        config = CompletionConfig(model="claude-sonnet-4-6", max_turns=1)

        class ToolUseBlock:
            name = "Read"
            input = {"file_path": "README.md"}

        class AssistantMessage:
            content = [ToolUseBlock()]

        class ResultMessage:
            structured_output = None
            result = "What is the primary user goal?"
            is_error = False

        mock_options_cls = MagicMock()

        async def fake_query(*args, **kwargs):
            yield AssistantMessage()
            yield ResultMessage()

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_ok
        assert result.value.content == "What is the primary user goal?"
        assert result.value.raw_response["phantom_tool_use"] is True
        assert result.value.raw_response["phantom_tool_name"] == "Read"

    @pytest.mark.asyncio
    async def test_empty_allowed_tools_phantom_tool_use_without_text_fails_loud(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail-loud is preserved when a phantom tool call yields no text.

        Salvage only applies when a usable final text streamed. A tool block
        with no recoverable content must still surface the structured
        ``ToolUseBlockViolation`` so the recovery/retry layer (and ultimately
        the caller) sees the real failure instead of an empty success.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset({"extra_args", "allowed_tools", "tools"}),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True)
        config = CompletionConfig(model="claude-sonnet-4-6", max_turns=1)

        class ToolUseBlock:
            name = "Read"
            input = {"file_path": "README.md"}

        class AssistantMessage:
            content = [ToolUseBlock()]

        class ResultMessage:
            structured_output = None
            result = ""
            is_error = False

        mock_options_cls = MagicMock()

        async def fake_query(*args, **kwargs):
            yield AssistantMessage()
            yield ResultMessage()

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=fake_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_err
        assert result.error.details["error_type"] == "ToolUseBlockViolation"
        assert result.error.details["tool_name"] == "Read"
        assert result.error.details["allowed_tools"] == []
        assert result.error.details["max_turns"] == 1

    @pytest.mark.asyncio
    async def test_phantom_tool_use_error_grants_one_recovery_with_no_tools_cue(self) -> None:
        """The retry loop grants exactly one hardened retry for phantom failures.

        First attempt fails with ``ToolUseBlockViolation`` under the sealed
        envelope; the second attempt must carry the plain-text-only recovery
        cue in its system prompt. A second phantom failure is terminal.
        """
        adapter = ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True)
        config = CompletionConfig(model="claude-sonnet-4-6", max_turns=1)

        phantom_error = ProviderError(
            message="Claude Agent SDK emitted a ToolUseBlock despite allowed_tools=[]",
            details={"error_type": "ToolUseBlockViolation", "tool_name": "Read"},
        )
        success = CompletionResponse(
            content="What is the primary user goal?",
            model="claude-sonnet-4-6",
            usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            finish_reason="stop",
            raw_response={},
        )
        seen_system_prompts: list[str | None] = []

        async def fake_execute(prompt, cfg, system_prompt=None):
            seen_system_prompts.append(system_prompt)
            if len(seen_system_prompts) == 1:
                return Result.err(phantom_error)
            return Result.ok(success)

        with patch.object(adapter, "_execute_single_request", side_effect=fake_execute):
            result = await adapter._complete_with_transient_retry(
                "test prompt", config, "Ask a Socratic question."
            )

        assert result.is_ok
        assert len(seen_system_prompts) == 2
        assert seen_system_prompts[0] == "Ask a Socratic question."
        assert "Respond with plain text only" in (seen_system_prompts[1] or "")
        assert seen_system_prompts[1].startswith("Ask a Socratic question.")

    @pytest.mark.asyncio
    async def test_phantom_recovery_not_granted_without_sealed_envelope(self) -> None:
        """Phantom recovery is scoped to ``allowed_tools=[]`` adapters only.

        For permissive adapters (``allowed_tools=None``) a tool-use max-turns
        failure is a genuine turn-budget problem, not phantom noise, and must
        not be masked by a hardened retry.
        """
        adapter = ClaudeCodeAdapter(allowed_tools=None)
        config = CompletionConfig(model="claude-sonnet-4-6", max_turns=1)

        max_turns_error = ProviderError(
            message="Claude Code returned an error result: Reached maximum number of turns (1)",
            details={"error_type": "Exception"},
        )
        calls: list[str | None] = []

        async def fake_execute(prompt, cfg, system_prompt=None):
            calls.append(system_prompt)
            return Result.err(max_turns_error)

        with patch.object(adapter, "_execute_single_request", side_effect=fake_execute):
            result = await adapter._complete_with_transient_retry("test prompt", config, None)

        assert result.is_err
        assert len(calls) == 1
        assert calls[0] is None

    @pytest.mark.asyncio
    async def test_phantom_recovery_covers_sdk_max_turns_exception_shape(self) -> None:
        """#1537's observed failure shape triggers recovery under the seal.

        The SDK surfaces the phantom-consumed turn as a bare exception
        (``Claude Code returned an error result: ...``) rather than the spy's
        structured violation; the sealed-envelope predicate must catch that
        shape too.
        """
        adapter = ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True)
        config = CompletionConfig(model="claude-sonnet-4-6", max_turns=1)

        sdk_error = ProviderError(
            message="Claude Agent SDK request failed: Claude Code returned an error result: success",
            details={"error_type": "Exception"},
        )
        success = CompletionResponse(
            content="What is the primary user goal?",
            model="claude-sonnet-4-6",
            usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            finish_reason="stop",
            raw_response={},
        )
        calls: list[str | None] = []

        async def fake_execute(prompt, cfg, system_prompt=None):
            calls.append(system_prompt)
            if len(calls) == 1:
                return Result.err(sdk_error)
            return Result.ok(success)

        with patch.object(adapter, "_execute_single_request", side_effect=fake_execute):
            result = await adapter._complete_with_transient_retry("test prompt", config, None)

        assert result.is_ok
        assert len(calls) == 2
        assert "Respond with plain text only" in (calls[1] or "")

    @pytest.mark.asyncio
    async def test_explicit_allowed_tools_sets_visible_sdk_tools(self) -> None:
        """Explicit tool envelopes restrict both permissions and exposed SDK tools."""
        allowed_tools = ["Read", "Grep", "mcp__ouroboros__qa"]
        adapter = ClaudeCodeAdapter(allowed_tools=allowed_tools)
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
        assert options_call_kwargs["allowed_tools"] == allowed_tools
        assert options_call_kwargs["tools"] == allowed_tools
        assert "Read" not in options_call_kwargs["disallowed_tools"]
        assert "Grep" not in options_call_kwargs["disallowed_tools"]
        assert "ToolSearch" not in options_call_kwargs["tools"]
        assert "AskUserQuestion" not in options_call_kwargs["tools"]
        assert "Write" in options_call_kwargs["disallowed_tools"]
        # Generic explicit envelopes must not silently drop plugin/project
        # MCP servers — only opt-in callers (the nested MCP-tool entrypoint
        # in ``mcp/tools/authoring_handlers.py``) should request strict
        # isolation.  Otherwise envelopes that include MCP names like
        # ``mcp__ouroboros__qa`` would lose access to those tools at runtime.
        assert "strict_mcp_config" not in options_call_kwargs
        assert "strict-mcp-config" not in (options_call_kwargs.get("extra_args") or {})

    def test_live_claude_agent_sdk_supports_extra_args(self) -> None:
        """Pin invariant: the pinned ``claude-agent-sdk`` release MUST
        expose ``extra_args``.

        Verified empirically against the published PyPI history
        (``extra_args`` is a field on ``ClaudeAgentOptions`` since the
        earliest public release ``0.0.23``).  This test locks the
        invariant in CI so a future pin bump or vendored SDK swap that
        drops the field fails fast at test time, well before the
        adapter's defense-in-depth fail-fast path could fire in
        production.

        Skipped when ``claude-agent-sdk`` is not installed — the SDK is
        an optional extra (``ouroboros-ai[claude-sdk]``) and the rest of
        this file mocks ``sys.modules['claude_agent_sdk']`` so it does
        not require the real package.  This particular invariant only
        matters when the real package IS installed; otherwise there is
        no ``ClaudeAgentOptions`` to introspect.
        """
        pytest.importorskip(
            "claude_agent_sdk",
            reason=(
                "claude-agent-sdk is an optional extra; the live-SDK "
                "invariant only applies when it is installed."
            ),
        )

        from ouroboros.providers.claude_code_adapter import (
            _claude_options_field_names,
        )

        # ``_claude_options_field_names`` is ``lru_cache``-d, so clear it
        # to make this test independent of any monkeypatching done
        # elsewhere in the module.
        _claude_options_field_names.cache_clear()
        try:
            field_names = _claude_options_field_names()
        finally:
            _claude_options_field_names.cache_clear()
        assert "extra_args" in field_names, (
            "claude-agent-sdk lost the ``extra_args`` passthrough field; the "
            "interview recursion fix relies on it. Either pin the SDK to a "
            "release that still has it or add a typed ``strict_mcp_config`` "
            "kwarg to the adapter forwarding."
        )

    @pytest.mark.asyncio
    async def test_strict_mcp_config_uses_extra_args_when_options_supports_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opt-in MCP isolation forwards via ``extra_args`` on SDKs that
        expose ``extra_args`` but not ``strict_mcp_config`` as a typed field.

        This matches the behavior of published ``claude-agent-sdk``
        releases, where the latest releases accept the flag only through
        CLI passthrough.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset({"extra_args", "allowed_tools", "tools"}),
        )

        allowed_tools = ["Read", "Grep"]
        adapter = ClaudeCodeAdapter(
            allowed_tools=allowed_tools,
            strict_mcp_config=True,
        )
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
        assert options_call_kwargs["allowed_tools"] == allowed_tools
        assert options_call_kwargs["tools"] == allowed_tools
        # Flag forwarded via CLI passthrough surface, not as a typed kwarg.
        assert "strict_mcp_config" not in options_call_kwargs
        assert options_call_kwargs.get("extra_args", {}).get("strict-mcp-config") is None
        assert "strict-mcp-config" in options_call_kwargs.get("extra_args", {})

    @pytest.mark.asyncio
    async def test_strict_mcp_config_uses_typed_field_when_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Forward to a typed ``strict_mcp_config`` field if a future SDK
        adds one, in preference to the CLI passthrough form."""
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset({"extra_args", "allowed_tools", "tools", "strict_mcp_config"}),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=["Read"], strict_mcp_config=True)
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
        assert options_call_kwargs.get("strict_mcp_config") is True
        # Should not double-pass via extra_args when the typed field is present.
        assert "strict-mcp-config" not in (options_call_kwargs.get("extra_args") or {})

    @pytest.mark.asyncio
    async def test_strict_mcp_config_fails_fast_when_sdk_lacks_surface(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the SDK exposes neither surface, the opt-in MUST fail fast.

        Silently dropping the flag would re-open the very recursion path
        ``InterviewHandler.handle()`` is trying to close.  The error must
        be actionable (telling operators to upgrade ``claude-agent-sdk``)
        rather than a generic ``TypeError`` from
        ``ClaudeAgentOptions(**options_kwargs)``.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset({"allowed_tools", "tools"}),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=["Read"], strict_mcp_config=True)
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
            pytest.raises(ProviderError) as excinfo,
        ):
            await adapter._execute_single_request("test prompt", config)

        assert "strict-mcp-config" in str(excinfo.value).lower() or (
            "strict_mcp_config" in str(excinfo.value).lower()
        )
        assert excinfo.value.details.get("error_type") == "ConfigurationError"
        # Options must NEVER be constructed when isolation cannot be honored.
        mock_options_cls.assert_not_called()

    @pytest.mark.asyncio
    async def test_default_tool_policy_does_not_set_strict_mcp_config(self) -> None:
        """Default callers (no allowed_tools, no opt-in) keep plugin MCP servers."""
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
            await adapter._execute_single_request("test prompt", config)

        options_call_kwargs = mock_options_cls.call_args.kwargs
        assert "strict_mcp_config" not in options_call_kwargs
        assert "strict-mcp-config" not in (options_call_kwargs.get("extra_args") or {})

    @pytest.mark.asyncio
    async def test_strict_mcp_config_closes_parent_context_leak_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``strict_mcp_config=True`` must also zero out every parent-context
        leak surface the SDK exposes (skills / sub-agents / plugins /
        settings / hooks).

        ``--strict-mcp-config`` alone only blocks MCP-server discovery,
        not the other descriptor sources that the parent Claude Code
        session leaks into the spawned subprocess.  Leaving them open
        gives the sub-CLI's model enough tool descriptors that it
        emits a ``ToolUseBlock`` on the only allowed turn, exhausts
        ``max_turns=1`` before any text streams, and ultimately surfaces
        as the bare-``Exception`` failure path described in #869.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset(
                {
                    "extra_args",
                    "allowed_tools",
                    "tools",
                    "strict_mcp_config",
                    "setting_sources",
                    "skills",
                    "agents",
                    "plugins",
                    "hooks",
                    "include_hook_events",
                }
            ),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True)
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
        assert options_call_kwargs.get("strict_mcp_config") is True
        assert options_call_kwargs.get("setting_sources") == []
        assert options_call_kwargs.get("skills") == []
        assert options_call_kwargs.get("agents") == {}
        assert options_call_kwargs.get("plugins") == []
        assert options_call_kwargs.get("hooks") == {}
        assert options_call_kwargs.get("include_hook_events") is False

    @pytest.mark.asyncio
    async def test_empty_allowed_tools_with_strict_mcp_config_merges_extra_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The nested interview path must preserve both strict-envelope flags.

        The supported SDK surface forwards ``strict-mcp-config`` through
        ``extra_args`` while ``allowed_tools=[]`` also requires the literal
        ``allowedTools=""`` CLI passthrough.  Regressions in this merge would
        drop one of the two safeguards only when they are combined.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset(
                {
                    "extra_args",
                    "allowed_tools",
                    "tools",
                    "setting_sources",
                    "skills",
                    "agents",
                    "plugins",
                    "hooks",
                    "include_hook_events",
                }
            ),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True)
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
        assert options_call_kwargs["tools"] == []
        assert "strict_mcp_config" not in options_call_kwargs
        assert options_call_kwargs["extra_args"]["allowedTools"] == ""
        assert options_call_kwargs["extra_args"]["strict-mcp-config"] is None
        assert options_call_kwargs.get("setting_sources") == []
        assert options_call_kwargs.get("skills") == []
        assert options_call_kwargs.get("agents") == {}
        assert options_call_kwargs.get("plugins") == []
        assert options_call_kwargs.get("hooks") == {}
        assert options_call_kwargs.get("include_hook_events") is False

    @pytest.mark.asyncio
    async def test_strict_mcp_config_isolation_is_noop_when_sdk_lacks_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Each isolation override must be gated by SDK field presence.

        Older SDK releases predate ``skills`` / ``agents`` / ``plugins`` /
        ``setting_sources`` / ``hooks`` / ``include_hook_events`` on
        ``ClaudeAgentOptions``.  Forwarding them unconditionally would
        crash with ``TypeError`` at ``ClaudeAgentOptions(**options_kwargs)``.
        On those releases the adapter still forwards ``strict_mcp_config``
        (or its ``extra_args`` fallback) and simply omits the rest.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset({"extra_args", "allowed_tools", "tools", "strict_mcp_config"}),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=[], strict_mcp_config=True)
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
        assert options_call_kwargs.get("strict_mcp_config") is True
        for absent in (
            "setting_sources",
            "skills",
            "agents",
            "plugins",
            "hooks",
            "include_hook_events",
        ):
            assert absent not in options_call_kwargs

    @pytest.mark.asyncio
    async def test_isolation_overrides_skipped_without_strict_mcp_config(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-isolation callers must keep parent context (skills, plugins,
        settings) intact.  The isolation overrides are scoped to opt-in
        ``strict_mcp_config=True`` callers only — generic explicit
        envelopes that need ``mcp__*`` tool access or project-scoped
        skills/agents must not silently lose them.
        """
        from ouroboros.providers import claude_code_adapter as adapter_mod

        monkeypatch.setattr(
            adapter_mod,
            "_claude_options_field_names",
            lambda: frozenset(
                {
                    "extra_args",
                    "allowed_tools",
                    "tools",
                    "strict_mcp_config",
                    "setting_sources",
                    "skills",
                    "agents",
                    "plugins",
                    "hooks",
                    "include_hook_events",
                }
            ),
        )

        adapter = ClaudeCodeAdapter(allowed_tools=["Read", "mcp__ouroboros__qa"])
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
        for absent in (
            "strict_mcp_config",
            "setting_sources",
            "skills",
            "agents",
            "plugins",
            "hooks",
            "include_hook_events",
        ):
            assert absent not in options_call_kwargs


class TestErrorDiagnostics:
    """Tests for error diagnostic paths in _execute_single_request."""

    @pytest.mark.asyncio
    async def test_trailing_sdk_exit_preserves_structured_error_result(self) -> None:
        """The CLI result payload outranks the SDK's misleading trailing exception.

        claude-agent-sdk 0.2.110 summarizes an error result with an empty
        ``errors`` list and protocol subtype ``success`` as
        ``Claude Code returned an error result: success``. The same result
        payload already carries the actionable text and HTTP status, so keep
        that structured error when the subprocess then exits non-zero.
        """
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")
        mock_options_cls = MagicMock()

        async def error_result_then_exit(*args, **kwargs):
            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = (
                "There's an issue with the selected model (missing-model). "
                "It may not exist or you may not have access to it."
            )
            result_msg.is_error = True
            result_msg.subtype = "success"
            result_msg.stop_reason = None
            result_msg.errors = []
            result_msg.api_error_status = 404
            yield result_msg
            raise RuntimeError("Claude Code returned an error result: success")

        sdk_module = _make_sdk_mock(
            mock_options_cls,
            MagicMock(side_effect=error_result_then_exit),
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
        error = result.error
        assert error.provider == "claude_code"
        assert error.status_code == 404
        assert "missing-model" in error.message
        assert "error result: success" not in error.message
        assert error.details["error_type"] == "ClaudeResultError"
        assert error.details["subtype"] == "success"
        assert error.details["api_error_status"] == 404

    @pytest.mark.asyncio
    async def test_unrelated_trailing_exception_overrides_prior_error_result(self) -> None:
        """Only the SDK's known error-result wrapper may reuse the prior payload."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")
        mock_options_cls = MagicMock()

        async def error_result_then_parser_failure(*args, **kwargs):
            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = "Rate limit exceeded"
            result_msg.is_error = True
            result_msg.subtype = "success"
            result_msg.stop_reason = None
            result_msg.errors = []
            result_msg.api_error_status = 429
            yield result_msg
            raise RuntimeError("message stream parser crashed")

        sdk_module = _make_sdk_mock(
            mock_options_cls,
            MagicMock(side_effect=error_result_then_parser_failure),
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
        assert result.error.provider == "claude_code"
        assert result.error.status_code is None
        assert "message stream parser crashed" in result.error.message
        assert result.error.details["error_type"] == "RuntimeError"

    @pytest.mark.asyncio
    async def test_api_error_status_retries_only_transient_failures(self) -> None:
        """HTTP status metadata drives retry even when the message is generic."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")
        overloaded = ProviderError(
            message="Claude Code API request failed",
            provider="claude_code",
            details={"error_type": "ClaudeResultError", "api_error_status": 529},
        )
        adapter._execute_single_request = AsyncMock(
            side_effect=[
                Result.err(overloaded),
                _ok_completion_result("What outcome should the workflow produce?"),
            ]
        )

        with patch("ouroboros.providers.claude_code_adapter.asyncio.sleep", new=AsyncMock()):
            result = await adapter._complete_with_transient_retry(
                "test prompt",
                config,
                system_prompt=None,
            )

        assert result.is_ok
        assert result.value.content == "What outcome should the workflow produce?"
        assert adapter._execute_single_request.await_count == 2

    @pytest.mark.parametrize(
        ("status_code", "expected_retry"),
        [
            (400, False),
            (408, True),
            (409, True),
            (425, True),
            (429, True),
            (499, False),
            (500, True),
            (599, True),
            (600, False),
        ],
    )
    def test_structured_api_status_retry_boundaries(
        self,
        status_code: int,
        expected_retry: bool,
    ) -> None:
        """Structured HTTP status outranks ambiguous provider message text."""
        adapter = ClaudeCodeAdapter()
        error = ProviderError(
            message="Connection timed out while checking the selected model",
            provider="claude_code",
            status_code=status_code,
            details={"error_type": "ClaudeResultError", "api_error_status": status_code},
        )

        assert adapter._is_retryable_provider_error(error) is expected_retry

    @pytest.mark.parametrize("malformed_status", [True, "429"])
    def test_malformed_api_status_metadata_is_ignored(
        self,
        malformed_status: object,
    ) -> None:
        """Boolean and string status metadata must not enter numeric retry checks."""
        adapter = ClaudeCodeAdapter()
        error = ProviderError(
            message="The selected model does not exist",
            provider="claude_code",
            status_code=malformed_status,  # type: ignore[arg-type]
            details={"error_type": "ClaudeResultError", "api_error_status": malformed_status},
        )

        assert adapter._is_retryable_provider_error(error) is False

    @pytest.mark.asyncio
    async def test_non_transient_api_error_status_is_not_retried(self) -> None:
        """A model/configuration 404 must surface immediately for correction."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")
        not_found = ProviderError(
            message="Connection timed out while checking whether the selected model exists",
            provider="claude_code",
            status_code=404,
            details={"error_type": "ClaudeResultError", "api_error_status": 404},
        )
        adapter._execute_single_request = AsyncMock(return_value=Result.err(not_found))

        result = await adapter._complete_with_transient_retry(
            "test prompt",
            config,
            system_prompt=None,
        )

        assert result.is_err
        assert result.error is not_found
        adapter._execute_single_request.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_stderr_cli_process_exit_is_retried(self) -> None:
        """Transient Claude CLI exits without stderr are retried by the shared adapter."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")
        transient_error = ProviderError(
            message="Claude Agent SDK request failed: Command failed with exit code 1",
            details={
                "error_type": "ProcessError",
                "stderr": "",
                "configured_cli_path": "/Applications/cmux.app/Contents/Resources/bin/claude",
            },
        )

        adapter._execute_single_request = AsyncMock(
            side_effect=[
                Result.err(transient_error),
                _ok_completion_result("seed requirements"),
            ]
        )

        with patch("ouroboros.providers.claude_code_adapter.asyncio.sleep", new=AsyncMock()):
            result = await adapter._complete_with_transient_retry(
                "test prompt",
                config,
                system_prompt=None,
            )

        assert result.is_ok
        assert result.value.content == "seed requirements"
        assert adapter._execute_single_request.call_count == 2

    @pytest.mark.asyncio
    async def test_stderr_cli_process_exit_is_not_retried(self) -> None:
        """Actionable CLI failures with stderr should surface immediately."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")
        auth_error = ProviderError(
            message="Claude Agent SDK request failed: Command failed with exit code 1",
            details={
                "error_type": "ProcessError",
                "stderr": "error: authentication required",
            },
        )

        adapter._execute_single_request = AsyncMock(return_value=Result.err(auth_error))

        result = await adapter._complete_with_transient_retry(
            "test prompt",
            config,
            system_prompt=None,
        )

        assert result.is_err
        assert result.error is auth_error
        adapter._execute_single_request.assert_awaited_once()

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
        assert "traceback" in error.details
        assert "RuntimeError: SDK connection lost" in error.details["traceback"]

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
    async def test_error_max_turns_uses_streamed_partial_content(self) -> None:
        """error_max_turns with assistant text returns the partial result."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        class TextBlock:
            def __init__(self, text: str) -> None:
                self.text = text

        async def partial_then_max_turns_query(*args, **kwargs):
            assistant_msg = MagicMock()
            type(assistant_msg).__name__ = "AssistantMessage"
            assistant_msg.content = [TextBlock("What should the app do first?")]
            yield assistant_msg

            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = ""
            result_msg.is_error = True
            result_msg.subtype = "error_max_turns"
            result_msg.errors = ["Reached maximum number of turns (5)"]
            result_msg.stop_reason = "max_turns"
            yield result_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=partial_then_max_turns_query)
        )

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_ok
        assert result.value.content == "What should the app do first?"
        assert result.value.finish_reason == "length"
        assert result.value.raw_response["subtype"] == "error_max_turns"
        assert result.value.raw_response["stop_reason"] == "max_turns"
        assert result.value.raw_response["errors"] == ["Reached maximum number of turns (5)"]
        assert result.value.raw_response["partial_result"] is True

    @pytest.mark.asyncio
    async def test_error_max_turns_without_partial_content_remains_error(self) -> None:
        """error_max_turns still fails when there is no usable content."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def max_turns_only_query(*args, **kwargs):
            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = ""
            result_msg.is_error = True
            result_msg.subtype = "error_max_turns"
            result_msg.errors = ["Reached maximum number of turns (5)"]
            result_msg.stop_reason = "tool_use"
            yield result_msg

        sdk_module = _make_sdk_mock(mock_options_cls, MagicMock(side_effect=max_turns_only_query))

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_err
        assert result.error.details["subtype"] == "error_max_turns"
        assert result.error.details["stop_reason"] == "tool_use"

    @pytest.mark.asyncio
    async def test_error_max_turns_rejects_tool_use_partial(self) -> None:
        """Tool-use-stopped partials are not guessed into final answers."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        class TextBlock:
            def __init__(self, text: str) -> None:
                self.text = text

        async def preamble_then_max_turns_query(*args, **kwargs):
            assistant_msg = MagicMock()
            type(assistant_msg).__name__ = "AssistantMessage"
            assistant_msg.content = [TextBlock("What should the app do first?")]
            yield assistant_msg

            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = ""
            result_msg.is_error = True
            result_msg.subtype = "error_max_turns"
            result_msg.errors = ["Reached maximum number of turns (5)"]
            result_msg.stop_reason = "tool_use"
            yield result_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=preamble_then_max_turns_query)
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
        assert "usable final response" in result.error.message
        assert result.error.details["partial_rejected"] is True
        assert result.error.details["partial_content"] == "What should the app do first?"

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
                "configured_cli_path": "/Applications/cmux.app/Contents/Resources/bin/claude",
                "stderr": "error: auth failed",
            },
        )
        rendered = error.format_details()
        assert "SDK failed" in rendered
        assert "error_type: RuntimeError" in rendered
        assert "session_id: sess_abc" in rendered
        assert (
            "configured_cli_path: /Applications/cmux.app/Contents/Resources/bin/claude" in rendered
        )
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

    @pytest.mark.asyncio
    async def test_malformed_tool_use_assistant_message_surfaces_retryable_diagnostic(self) -> None:
        """AssistantMessage stop_reason=tool_use without ToolUseBlock is a runtime diagnostic."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def malformed_tool_use_query(*args, **kwargs):
            assistant_msg = MagicMock()
            type(assistant_msg).__name__ = "AssistantMessage"
            assistant_msg.stop_reason = "tool_use"
            assistant_msg.content = []
            yield assistant_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=malformed_tool_use_query)
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
        assert result.error.details["error_type"] == "MalformedToolUseTurn"
        assert result.error.details["provider"] == "claude_code"
        assert result.error.details["stop_reason"] == "tool_use"
        assert result.error.details["tool_use_count"] == 0
        assert result.error.details["is_malformed"] is True
        assert result.error.details["retryable"] is True

    @pytest.mark.asyncio
    async def test_malformed_tool_use_diagnostic_survives_later_max_turns_error(self) -> None:
        """A specific malformed-tool diagnostic is not overwritten by SDK max-turns errors."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def malformed_then_max_turns_query(*args, **kwargs):
            assistant_msg = MagicMock()
            type(assistant_msg).__name__ = "AssistantMessage"
            assistant_msg.stop_reason = "tool_use"
            assistant_msg.content = []
            yield assistant_msg

            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = ""
            result_msg.is_error = True
            result_msg.subtype = "error_max_turns"
            result_msg.errors = ["Reached maximum number of turns (5)"]
            result_msg.stop_reason = "tool_use"
            yield result_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=malformed_then_max_turns_query)
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
        assert result.error.message == (
            "stop_reason=tool_use but no tool_use content blocks were present"
        )
        assert result.error.details["error_type"] == "MalformedToolUseTurn"
        assert result.error.details["provider"] == "claude_code"
        assert result.error.details["retryable"] is True
        assert "subtype" not in result.error.details

    @pytest.mark.asyncio
    async def test_malformed_tool_use_diagnostic_does_not_mask_later_terminal_error(self) -> None:
        """Concrete terminal SDK errors must override stale malformed-turn candidates."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def malformed_then_auth_error_query(*args, **kwargs):
            assistant_msg = MagicMock()
            type(assistant_msg).__name__ = "AssistantMessage"
            assistant_msg.stop_reason = "tool_use"
            assistant_msg.content = []
            yield assistant_msg

            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = "Authentication failed"
            result_msg.is_error = True
            result_msg.subtype = "error_during_execution"
            result_msg.errors = ["Invalid API key"]
            result_msg.stop_reason = None
            yield result_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=malformed_then_auth_error_query)
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
        assert result.error.message == "Authentication failed"
        assert result.error.details["subtype"] == "error_during_execution"
        assert result.error.details["errors"] == ["Invalid API key"]
        assert result.error.details.get("error_type") != "MalformedToolUseTurn"

    @pytest.mark.asyncio
    async def test_malformed_tool_use_diagnostic_clears_after_later_success(self) -> None:
        """A transient malformed assistant turn must not override a later successful result."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        async def malformed_then_success_query(*args, **kwargs):
            assistant_msg = MagicMock()
            type(assistant_msg).__name__ = "AssistantMessage"
            assistant_msg.stop_reason = "tool_use"
            assistant_msg.content = []
            yield assistant_msg

            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = "recovered response"
            result_msg.is_error = False
            yield result_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=malformed_then_success_query)
        )

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_ok
        assert result.value.content == "recovered response"
        assert result.value.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_malformed_tool_use_diagnostic_clears_after_later_partial_content(self) -> None:
        """A malformed-turn candidate must not mask an accepted max-turns partial result."""
        adapter = ClaudeCodeAdapter(max_turns=5)
        config = CompletionConfig(model="claude-sonnet-4-6")

        mock_options_cls = MagicMock()

        class TextBlock:
            text = "usable partial response"

        async def malformed_then_partial_query(*args, **kwargs):
            malformed_msg = MagicMock()
            type(malformed_msg).__name__ = "AssistantMessage"
            malformed_msg.stop_reason = "tool_use"
            malformed_msg.content = []
            yield malformed_msg

            text_msg = MagicMock()
            type(text_msg).__name__ = "AssistantMessage"
            text_msg.stop_reason = "end_turn"
            text_msg.content = [TextBlock()]
            yield text_msg

            result_msg = MagicMock()
            type(result_msg).__name__ = "ResultMessage"
            result_msg.structured_output = None
            result_msg.result = ""
            result_msg.is_error = True
            result_msg.subtype = "error_max_turns"
            result_msg.errors = ["Reached maximum number of turns (5)"]
            result_msg.stop_reason = "end_turn"
            yield result_msg

        sdk_module = _make_sdk_mock(
            mock_options_cls, MagicMock(side_effect=malformed_then_partial_query)
        )

        with patch.dict(
            "sys.modules",
            {
                "claude_agent_sdk": sdk_module,
                "claude_agent_sdk._errors": sdk_module._errors,
            },
        ):
            result = await adapter._execute_single_request("test prompt", config)

        assert result.is_ok
        assert result.value.content == "usable partial response"
        assert result.value.finish_reason == "length"

    @pytest.mark.asyncio
    async def test_complete_retries_retryable_malformed_diagnostic_then_succeeds(self) -> None:
        """complete() retries ProviderError details marked retryable before returning success."""
        adapter = ClaudeCodeAdapter()
        config = CompletionConfig(model="claude-sonnet-4-6")
        messages = [Message(role=MessageRole.USER, content="test prompt")]
        retryable_error = ProviderError(
            "stop_reason=tool_use but no tool_use content blocks were present",
            details={
                "error_type": "MalformedToolUseTurn",
                "retryable": True,
            },
        )

        adapter._execute_single_request = AsyncMock(
            side_effect=[
                Result.err(retryable_error),
                _ok_completion_result("recovered response"),
            ]
        )

        with (
            patch.dict("sys.modules", {"claude_agent_sdk": MagicMock()}),
            patch(
                "ouroboros.providers.claude_code_adapter.asyncio.sleep",
                new_callable=AsyncMock,
            ) as mock_sleep,
        ):
            result = await adapter.complete(messages, config)

        assert result.is_ok
        assert result.value.content == "recovered response"
        assert adapter._execute_single_request.await_count == 2
        mock_sleep.assert_awaited_once()


class TestCLIFallbackWhenSDKAbsent:
    """The `claude` backend must survive a process the SDK cannot enter.

    `claude-agent-sdk` requires `mcp<2.0.0` and the MCP protocol server requires
    `mcp==2.0.0`, so a server built from the `[mcp]` extra has no SDK to import.
    Before this, `llm.backend: claude` in that process could generate nothing at
    all and reported it as an interview-content failure (Q00/ouroboros#1839).
    """

    @staticmethod
    def _adapter(**kwargs: object) -> ClaudeCodeAdapter:
        with patch.object(ClaudeCodeAdapter, "_resolve_cli_path", return_value="/bin/claude"):
            return ClaudeCodeAdapter(**kwargs)  # type: ignore[arg-type]

    @staticmethod
    def _proc(stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> MagicMock:
        proc = MagicMock()
        proc.returncode = returncode
        proc.communicate = AsyncMock(return_value=(stdout, stderr))
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=returncode)
        return proc

    @staticmethod
    def _hanging_proc() -> MagicMock:
        """A child that never answers — the case a bare subprocess cannot escape."""

        async def _never(*_a: object, **_k: object) -> tuple[bytes, bytes]:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")  # pragma: no cover

        proc = MagicMock()
        proc.returncode = None
        proc.communicate = _never
        proc.kill = MagicMock()
        proc.wait = AsyncMock(return_value=-9)
        return proc

    def test_a_missing_sdk_falls_back_to_the_cli(self) -> None:
        adapter = self._adapter()
        payload = (
            b'{"type":"result","subtype":"success","is_error":false,'
            b'"result":"the answer","stop_reason":"end_turn",'
            b'"usage":{"input_tokens":11,"output_tokens":7}}'
        )
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [
                        Message(role=MessageRole.SYSTEM, content="be terse"),
                        Message(role=MessageRole.USER, content="ping"),
                    ],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok, result
        assert result.value.content == "the answer"
        assert result.value.finish_reason == "end_turn"
        assert result.value.usage.total_tokens == 18

        argv = list(spawn.await_args.args)
        assert argv[0] == "/bin/claude"
        assert "-p" in argv and "--output-format" in argv and "json" in argv
        # The system message travels as a CLI flag rather than being folded into
        # the prompt, matching what the SDK path does with it.
        assert "--append-system-prompt" in argv
        assert argv[argv.index("--append-system-prompt") + 1] == "be terse"

    def test_cli_fallback_accepts_top_level_event_array(self) -> None:
        """Newer/wrapped Claude CLIs may batch stream-json events as one array."""
        adapter = self._adapter()
        payload = json.dumps(
            [
                {"type": "system", "subtype": "init", "session_id": "array-session"},
                {
                    "type": "assistant",
                    "session_id": "array-session",
                    "message": {
                        "content": [{"type": "text", "text": "draft"}],
                        "usage": {"input_tokens": 2, "output_tokens": 1},
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "array answer",
                    "session_id": "array-session",
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 13, "output_tokens": 5},
                },
            ]
        ).encode()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok, result
        assert result.value.content == "array answer"
        assert result.value.usage.total_tokens == 18
        assert result.value.raw_response["type"] == "result"
        assert result.value.raw_response["session_id"] == "array-session"

    def test_cli_fallback_aggregates_multi_turn_usage_without_terminal_total(self) -> None:
        adapter = self._adapter()
        payload = json.dumps(
            [
                {
                    "type": "assistant",
                    "message": {"usage": {"input_tokens": 100, "output_tokens": 20}},
                },
                {
                    "type": "assistant",
                    "message": {"usage": {"input_tokens": 10, "output_tokens": 2}},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "array answer",
                    "stop_reason": "end_turn",
                },
            ]
        ).encode()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok, result
        assert result.value.usage.prompt_tokens == 110
        assert result.value.usage.completion_tokens == 22
        assert result.value.usage.total_tokens == 132
        assert result.value.raw_response["usage"] == {
            "input_tokens": 110,
            "output_tokens": 22,
            "total_tokens": 132,
        }

    @pytest.mark.parametrize(
        ("wire_usage", "expected_components", "expected_raw"),
        [
            pytest.param(
                {"total_tokens": 132},
                (0, 0, 0, 132),
                {"total_tokens": 132},
                id="total-only-is-not-allocated",
            ),
            pytest.param(
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 12,
                },
                (100, 20, 120, 12),
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 12,
                    "total_tokens": 132,
                },
                id="components-derive-cache-inclusive-total",
            ),
            pytest.param(
                {"input_tokens": 110, "output_tokens": 22, "total_tokens": 132},
                (110, 22, 132, 0),
                {"input_tokens": 110, "output_tokens": 22, "total_tokens": 132},
                id="consistent-components-and-total",
            ),
            pytest.param(
                {"prompt_tokens": 110, "completion_tokens": 22},
                (110, 22, 132, 0),
                {"input_tokens": 110, "output_tokens": 22, "total_tokens": 132},
                id="aliases-become-canonical-components",
            ),
        ],
    )
    def test_cli_fallback_uses_normalized_canonical_usage_authority(
        self,
        wire_usage: dict[str, int],
        expected_components: tuple[int, int, int, int],
        expected_raw: dict[str, int],
    ) -> None:
        adapter = self._adapter()
        payload = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "answer",
                "usage": wire_usage,
            }
        ).encode()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok, result
        assert (
            result.value.usage.prompt_tokens,
            result.value.usage.completion_tokens,
            result.value.usage.total_tokens,
            result.value.usage.unallocated_tokens,
        ) == expected_components
        assert result.value.usage.total_tokens == (
            result.value.usage.prompt_tokens + result.value.usage.completion_tokens
        )
        assert (
            result.value.usage.total_tokens + result.value.usage.unallocated_tokens
            == expected_raw["total_tokens"]
        )
        assert result.value.raw_response["usage"] == expected_raw

    def test_invalid_terminal_total_cannot_fall_back_or_split_authority(self) -> None:
        adapter = self._adapter()
        payload = json.dumps(
            [
                {
                    "type": "assistant",
                    "message": {"usage": {"input_tokens": 100, "output_tokens": 20}},
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "answer",
                    "usage": {
                        "input_tokens": 110,
                        "output_tokens": 22,
                        "total_tokens": 12,
                    },
                },
            ]
        ).encode()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "usage.total_tokens disagrees" in result.error.details["parse_error"]

    @pytest.mark.parametrize(
        "wire_usage",
        [
            pytest.param({"total_tokens": 132}, id="total-only"),
            pytest.param(
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 12,
                },
                id="cache-inclusive",
            ),
        ],
    )
    def test_cli_usage_survives_tracked_completion_frugality_capture(
        self,
        wire_usage: dict[str, int],
    ) -> None:
        adapter = self._adapter()
        config = CompletionConfig(model="claude-haiku-4-5", role="reflect")
        payload = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "answer",
                "usage": wire_usage,
            }
        ).encode()

        # This regression isolates the completion-usage boundary from the
        # separate execution-configuration attestation gate.  The real Claude
        # CLI adapter response passes directly through tracked_complete().
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ),
            patch.object(
                provider_usage_module,
                "_prepare_completion_configuration",
                return_value=(config, {}, None, True, None),
            ),
            patch.object(
                provider_usage_module,
                "_completion_configuration",
                return_value={"surface": "completion", "backend": "claude-cli"},
            ),
        ):
            with capture_generation_provider_usage() as capture:
                result = asyncio.run(
                    tracked_complete(
                        adapter,
                        [Message(role=MessageRole.USER, content="ping")],
                        config,
                    )
                )

        summary = capture.summary(instrumentation_complete=True)
        assert result.is_ok, result
        assert summary.complete is True
        assert summary.token_spend == 132
        assert summary.issues == ()

    def test_the_adapters_own_permission_vocabulary_is_not_forwarded_blindly(self) -> None:
        """`default` is a mode this adapter has and the CLI does not.

        Forwarding it would make every fallback call fail on argument parsing,
        which is a worse failure than the one being fixed: it would look like the
        model refused rather than like a bad flag.
        """
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        for mode, forwarded in (("default", False), ("bypassPermissions", True)):
            adapter = self._adapter(permission_mode=mode)
            with (
                patch.dict("sys.modules", {"claude_agent_sdk": None}),
                patch(
                    "asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=self._proc(payload)),
                ) as spawn,
            ):
                assert asyncio.run(
                    adapter.complete(
                        [Message(role=MessageRole.USER, content="ping")],
                        CompletionConfig(model="claude-haiku-4-5"),
                    )
                ).is_ok
            assert ("--permission-mode" in list(spawn.await_args.args)) is forwarded, mode

    def test_a_cli_error_is_reported_as_one(self) -> None:
        """The CLI's own failure text reaches the caller rather than a generic one."""
        payload = (
            b'{"is_error":true,"subtype":"error_max_turns",'
            b'"result":"reached maximum number of turns","session_id":"abc"}'
        )
        adapter = self._adapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "maximum number of turns" in result.error.message
        assert result.error.details["subtype"] == "error_max_turns"

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param(
                {
                    "type": "result",
                    "is_error": True,
                    "result": None,
                    "subtype": "error_max_turns",
                    "session_id": "error-session",
                    "stop_reason": "max_turns",
                    "usage": {"input_tokens": 9, "output_tokens": 2},
                },
                id="typed-null",
            ),
            pytest.param(
                {
                    "is_error": True,
                    "subtype": "error_max_turns",
                    "session_id": "error-session",
                    "stop_reason": "max_turns",
                    "usage": {"input_tokens": 9, "output_tokens": 2},
                },
                id="untyped-omitted",
            ),
        ],
    )
    def test_empty_cli_error_preserves_structured_metadata(
        self, payload: dict[str, object]
    ) -> None:
        adapter = self._adapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(json.dumps(payload).encode())),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert result.error.message == "claude CLI reported an error"
        assert result.error.details["subtype"] == "error_max_turns"
        assert result.error.details["session_id"] == "error-session"
        assert result.error.details["stop_reason"] == "max_turns"
        assert result.error.details["usage"] == {
            "input_tokens": 9,
            "output_tokens": 2,
            "total_tokens": 11,
        }

    @pytest.mark.parametrize(
        ("payload", "message"),
        [
            pytest.param(
                b'{"type":"result","is_error":false,"result":"done","trace":' + b"9" * 4301 + b"}",
                "integer exceeds",
                id="oversized-integer",
            ),
            pytest.param(
                b'{"type":"result","is_error":false,"result":"done",'
                b'"usage":{"cache_read_input_tokens":1e9999}}',
                "non-finite JSON number",
                id="non-finite-secondary-usage",
            ),
            pytest.param(
                b'{"type":"result","is_error":false,"result":"done",'
                b'"usage":{"cache_read_input_tokens":' + b"9" * 1000 + b"}}",
                "bounded non-negative integer",
                id="thousand-digit-secondary-usage",
            ),
            pytest.param(
                b'{"type":"result","is_error":false,"result":"done",'
                b'"usage":{"prompt_tokens":' + b"9" * 1000 + b"}}",
                "bounded non-negative integer",
                id="thousand-digit-primary-alias",
            ),
            pytest.param(
                b'{"type":"result","is_error":false,"result":"done",'
                b'"usage":{"prompt_tokens":true,"completion_tokens":2}}',
                "bounded non-negative integer",
                id="malformed-primary-alias",
            ),
        ],
    )
    def test_hostile_number_becomes_structured_provider_error(
        self, payload: bytes, message: str
    ) -> None:
        adapter = self._adapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert message in result.error.details["parse_error"]

    def test_a_non_json_body_surfaces_stderr(self) -> None:
        """An auth prompt or a bad flag never reaches the result envelope.

        Whatever the CLI wrote to stderr is the only diagnosis there is, so it is
        carried rather than replaced with a parse error the user cannot act on.
        """
        adapter = self._adapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(
                    return_value=self._proc(b"not json", b"Invalid API key", returncode=1)
                ),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "Invalid API key" in result.error.details["stderr"]
        assert result.error.details["returncode"] == 1

    def test_neither_transport_is_named_as_neither(self) -> None:
        """With no SDK and no CLI the message says so, instead of naming only pip."""
        with patch.object(ClaudeCodeAdapter, "_resolve_cli_path", return_value=None):
            adapter = ClaudeCodeAdapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("shutil.which", return_value=None),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "Neither" in result.error.message
        assert "claude CLI" in result.error.message

    def test_a_json_retry_stays_on_the_transport_that_answered(self) -> None:
        """Prose-then-JSON must not route the retry back into the absent SDK.

        Question generation and scoring both request a schema, so this is on the
        reported path rather than beside it: if the first body is prose, the
        retry has to reach the CLI again. Reaching for the SDK here turns a
        recoverable response into `ImportError` in the one process that has no
        SDK to import.
        """
        adapter = self._adapter()
        prose = b'{"is_error":false,"result":"Sure! Here you go:","stop_reason":"end_turn"}'
        good = b'{"is_error":false,"result":"{\\"q\\": 1}","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[self._proc(prose), self._proc(good)]),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ask something")],
                    CompletionConfig(
                        model="claude-haiku-4-5",
                        response_format={"type": "json_object"},
                    ),
                )
            )

        assert result.is_ok, result
        assert result.value.content == '{"q": 1}'
        # Two CLI spawns: the prose answer and the retry. Had the retry gone to
        # the SDK there would be one spawn and an import failure.
        assert spawn.await_count == 2

    def test_the_no_tools_envelope_reaches_the_cli(self) -> None:
        """`allowed_tools=[]` and `strict_mcp_config` are carried, not dropped.

        Interview/PM/QA construct this adapter that way precisely so the spawned
        CLI cannot execute tools or rediscover ouroboros' own MCP server and
        recurse. A fallback that ignored them would be more dangerous than the
        failure it replaces.
        """
        adapter = self._adapter(allowed_tools=[], strict_mcp_config=True)
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ) as spawn,
        ):
            assert asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            ).is_ok

        argv = list(spawn.await_args.args)
        assert "--tools" in argv
        assert argv[argv.index("--tools") + 1] == ""
        assert "--allowedTools" in argv
        # The CLI honors an empty allow-list literally, unlike the SDK.
        assert argv[argv.index("--allowedTools") + 1] == ""
        assert "--strict-mcp-config" in argv
        # `--strict-mcp-config` closes MCP discovery only. The parent's project
        # and user instructions, hooks, agents and plugins arrive through
        # setting sources, which the SDK path empties too.
        assert "--setting-sources" in argv
        assert argv[argv.index("--setting-sources") + 1] == ""

    def test_the_cli_child_environment_is_isolated(self) -> None:
        adapter = self._adapter()
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        inherited = {
            "CLAUDECODE": "1",
            "OUROBOROS_AGENT_RUNTIME": "claude",
            "OUROBOROS_LLM_BACKEND": "claude",
            "_OUROBOROS_DEPTH": "2",
        }
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch.dict(os.environ, inherited, clear=False),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok
        child_env = spawn.await_args.kwargs["env"]
        assert "CLAUDECODE" not in child_env
        assert "OUROBOROS_AGENT_RUNTIME" not in child_env
        assert "OUROBOROS_LLM_BACKEND" not in child_env
        assert child_env["_OUROBOROS_DEPTH"] == "3"

    def test_a_nonempty_tool_envelope_controls_catalog_and_permissions(self) -> None:
        adapter = self._adapter(allowed_tools=["Read", "Grep"])
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok
        argv = list(spawn.await_args.args)
        assert argv[argv.index("--tools") + 1] == "Read Grep"
        assert argv[argv.index("--allowedTools") + 1] == "Read Grep"

    def test_a_permissive_caller_is_not_sealed_by_the_fallback(self) -> None:
        """The other half of the pin: no envelope means no envelope flags.

        `allowed_tools=None` is the permissive default, and forcing an empty
        allow-list onto it would silently disable tools for callers that never
        asked for that.
        """
        adapter = self._adapter(allowed_tools=None, strict_mcp_config=False)
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(payload)),
            ) as spawn,
        ):
            assert asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            ).is_ok

        argv = list(spawn.await_args.args)
        assert "--tools" not in argv
        assert "--allowedTools" not in argv
        assert "--strict-mcp-config" not in argv
        assert "--setting-sources" not in argv

    def test_the_turn_budget_is_forwarded_to_the_cli(self) -> None:
        """`--max-turns` is absent from `claude --help` but is a real flag.

        The CLI rejects genuinely unknown options (`error: unknown option`),
        and accepts this one — so an SDK-absent run that omitted it would run
        past the configured turn and cost boundary. Semantic evaluation asks
        for 20 turns with tools enabled, which is where that bites.
        """
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        # Constructor value, then a per-request override of the same adapter.
        for ctor_turns, request_turns, expected in ((20, None, "20"), (20, 3, "3"), (1, None, "1")):
            adapter = self._adapter(max_turns=ctor_turns)
            with (
                patch.dict("sys.modules", {"claude_agent_sdk": None}),
                patch(
                    "asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=self._proc(payload)),
                ) as spawn,
            ):
                assert asyncio.run(
                    adapter.complete(
                        [Message(role=MessageRole.USER, content="ping")],
                        CompletionConfig(model="claude-haiku-4-5", max_turns=request_turns),
                    )
                ).is_ok
            argv = list(spawn.await_args.args)
            assert argv[argv.index("--max-turns") + 1] == expected, (ctor_turns, request_turns)

    def test_a_transient_cli_failure_is_retried_like_a_transient_sdk_failure(self) -> None:
        """Rate limits belong to the service, not to how we reached it.

        The SDK transport gets `_MAX_RETRIES` for overload/rate-limit/bootstrap
        errors. Selecting the fallback must not silently convert those same
        errors into permanent failures.
        """
        adapter = self._adapter()
        overloaded = b'{"is_error":true,"result":"API error: 529 overloaded_error"}'
        good = b'{"is_error":false,"result":"recovered","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[self._proc(overloaded, returncode=1), self._proc(good)]),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok, result
        assert result.value.content == "recovered"
        assert spawn.await_count == 2

    def test_nonzero_exit_stale_success_uses_transient_stderr_and_retries(self) -> None:
        adapter = self._adapter()
        stale_success = b'{"is_error":false,"result":"completed"}'
        good = b'{"is_error":false,"result":"recovered","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(
                    side_effect=[
                        self._proc(
                            stale_success,
                            stderr=b"API error: 529 overloaded_error",
                            returncode=1,
                        ),
                        self._proc(good),
                    ]
                ),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok, result
        assert result.value.content == "recovered"
        assert spawn.await_count == 2

    def test_nonzero_exit_stale_success_surfaces_nontransient_stderr(self) -> None:
        adapter = self._adapter()
        stale_success = b'{"is_error":false,"result":"completed"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(
                    return_value=self._proc(
                        stale_success,
                        stderr=b"error: authentication required",
                        returncode=1,
                    )
                ),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert result.error.message == "error: authentication required"
        assert result.error.details["error_type"] == "ProcessError"
        assert result.error.details["envelope_is_error"] is False
        assert result.error.details["stderr"] == "error: authentication required"
        assert spawn.await_count == 1

    def test_a_permanent_cli_failure_is_not_retried(self) -> None:
        """The other half: a non-transient error still fails on the first try."""
        adapter = self._adapter()
        bad_request = b'{"is_error":true,"result":"invalid model name"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=self._proc(bad_request, returncode=1)),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert spawn.await_count == 1

    def test_an_empty_result_envelope_is_not_a_successful_answer(self) -> None:
        """`is_error: false` with an empty body is infrastructure, not an answer.

        The SDK path already classifies empty content as a retryable provider
        error. Returning `content=""` as `is_ok` would hand an interview or QA
        caller a false answer *past* the retry loop, since the loop returns on
        the first success.
        """
        adapter = self._adapter()
        empty = b'{"is_error":false,"result":"","stop_reason":"end_turn"}'
        good = b'{"is_error":false,"result":"an actual answer","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=[self._proc(empty), self._proc(good)]),
            ) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_ok, result
        assert result.value.content == "an actual answer"
        assert spawn.await_count == 2

    def test_a_persistently_empty_cli_fails_explicitly(self) -> None:
        """Exhausting the retries reports the emptiness rather than returning it."""
        adapter = self._adapter()
        empty = b'{"is_error":false,"result":"","stop_reason":"end_turn"}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=lambda *_a, **_k: self._proc(empty)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "Empty response" in result.error.message
        assert result.error.details["content_length"] == 0

    def test_a_success_envelope_with_missing_result_fails_closed(self) -> None:
        """Only an explicit error envelope may omit its result content."""
        adapter = self._adapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(
                    side_effect=lambda *_a, **_k: self._proc(
                        b'{"is_error":false,"stop_reason":"end_turn"}'
                    )
                ),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "no valid JSON result" in result.error.message
        assert "no result content" in result.error.details["parse_error"]

    def test_unencodable_caller_text_never_reaches_a_spawn(self) -> None:
        """A lone surrogate must fail before there is a child to orphan.

        MCP payloads are caller-controlled, so this text is reachable. Encoding
        it after the spawn left a process waiting on a stdin that would never
        be written, and repeated bad inputs accumulated them. The pin is that no
        subprocess is created at all, not that a created one gets cleaned up.
        """
        adapter = self._adapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="lone surrogate: \ud800")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "cannot be encoded" in result.error.message
        spawn.assert_not_awaited()

    def test_unencodable_system_text_never_reaches_a_spawn(self) -> None:
        """The same for text that travels as an argv flag rather than on stdin."""
        adapter = self._adapter()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn,
        ):
            result = asyncio.run(
                adapter.complete(
                    [
                        Message(role=MessageRole.SYSTEM, content="be terse \udfff"),
                        Message(role=MessageRole.USER, content="ping"),
                    ],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "cannot be encoded" in result.error.message
        spawn.assert_not_awaited()

    def test_any_post_spawn_failure_reaps_the_child(self) -> None:
        """Ownership, not a catalogue of exception types.

        Once the child exists every exit path has to reap it, including error
        classes nobody enumerated. `communicate` raising an arbitrary error
        stands in for that.
        """
        adapter = self._adapter()
        # returncode stays None: a child that is still alive, which is the only
        # state in which reaping is observable at all.
        proc = self._hanging_proc()
        proc.communicate = AsyncMock(side_effect=RuntimeError("pipe exploded"))
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        proc.kill.assert_called()
        proc.wait.assert_awaited()

    def test_a_failure_names_the_transport_that_actually_failed(self) -> None:
        """Blaming the SDK from a process without one is the original bug's shape.

        A malformed-but-parseable envelope raises inside the CLI path and lands
        in the shared exception handler. Naming the SDK there would send the
        reader to `pip install claude-agent-sdk` — advice that cannot help in
        the process where this transport is the one being used.
        """
        adapter = self._adapter()
        malformed = b'{"is_error":false,"result":"ok","usage":{"input_tokens":"not-a-number"}}'
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.sleep", new=AsyncMock()),
            patch(
                "asyncio.create_subprocess_exec",
                new=AsyncMock(side_effect=lambda *_a, **_k: self._proc(malformed)),
            ),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "claude CLI request failed" in result.error.message
        assert "Claude Agent SDK" not in result.error.message

    def test_a_model_id_is_normalized_the_same_way_on_both_transports(self) -> None:
        """`anthropic/...` is valid config the SDK strips; raw it fails the CLI."""
        payload = b'{"is_error":false,"result":"ok","stop_reason":"end_turn"}'
        cases: list[tuple[str, str | None]] = [
            ("anthropic/claude-sonnet-4-5", "claude-sonnet-4-5"),
            ("openrouter/anthropic/claude-opus-4-1", "claude-opus-4-1"),
            ("claude-haiku-4-5", "claude-haiku-4-5"),
            # No usable preference — omit the flag and take the CLI default.
            ("default", None),
            ("openrouter/openai/gpt-4o", None),
        ]
        for configured, expected in cases:
            adapter = self._adapter()
            with (
                patch.dict("sys.modules", {"claude_agent_sdk": None}),
                patch(
                    "asyncio.create_subprocess_exec",
                    new=AsyncMock(return_value=self._proc(payload)),
                ) as spawn,
            ):
                assert asyncio.run(
                    adapter.complete(
                        [Message(role=MessageRole.USER, content="ping")],
                        CompletionConfig(model=configured),
                    )
                ).is_ok
            argv = list(spawn.await_args.args)
            if expected is None:
                assert "--model" not in argv, configured
            else:
                assert argv[argv.index("--model") + 1] == expected, configured

    def test_a_nonresponsive_cli_is_bounded_and_reaped(self) -> None:
        """A child nothing else owns must not be able to hang the caller forever."""
        adapter = self._adapter(timeout=0.05)
        proc = self._hanging_proc()
        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            result = asyncio.run(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )

        assert result.is_err
        assert "timed out" in result.error.message
        proc.kill.assert_called_once()
        proc.wait.assert_awaited()

    def test_cancellation_does_not_leave_the_child_running(self) -> None:
        """Cancelling the caller is the only chance to reap this subprocess."""
        adapter = self._adapter()
        proc = self._hanging_proc()

        async def _run() -> None:
            task = asyncio.create_task(
                adapter.complete(
                    [Message(role=MessageRole.USER, content="ping")],
                    CompletionConfig(model="claude-haiku-4-5"),
                )
            )
            # Let the task reach the await on the child before cancelling.
            await asyncio.sleep(0.05)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        with (
            patch.dict("sys.modules", {"claude_agent_sdk": None}),
            patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        ):
            asyncio.run(_run())

        proc.kill.assert_called_once()
        proc.wait.assert_awaited()
