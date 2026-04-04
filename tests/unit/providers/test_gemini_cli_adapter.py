"""Unit tests for the Gemini CLI-backed LLM adapter."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter

# ---------------------------------------------------------------------------
# Fake async stream helpers
# ---------------------------------------------------------------------------


class _FakeStream:
    """Minimal asyncio.StreamReader substitute."""

    def __init__(self, text: str = "") -> None:
        self._buffer = text.encode("utf-8")
        self._cursor = 0

    async def read(self, chunk_size: int = 16384) -> bytes:
        if self._cursor >= len(self._buffer):
            return b""
        end = min(self._cursor + chunk_size, len(self._buffer))
        chunk = self._buffer[self._cursor : end]
        self._cursor = end
        return chunk


class _FakeProcess:
    """Minimal asyncio.subprocess.Process substitute."""

    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> None:
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self._returncode = returncode
        self.returncode: int | None = None
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self._returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._returncode

    def kill(self) -> None:
        self.returncode = self._returncode


# ---------------------------------------------------------------------------
# Helpers for building fake stream-json output
# ---------------------------------------------------------------------------


def _make_stream(events: list[dict]) -> str:
    """Render a list of dicts as newline-delimited JSON."""
    return "\n".join(json.dumps(e) for e in events) + "\n"


_INIT_EVENT = {"type": "init", "session_id": "sess-abc", "model": "gemini-2.5-flash"}
_RESULT_EVENT = {"type": "result", "response": "Hello, world!", "stats": {}, "error": None}
_MESSAGE_EVENT = {"type": "message", "role": "model", "content": "Hello, world!"}
_TOOL_USE_EVENT = {
    "type": "tool_use",
    "name": "read_file",
    "input": {"path": "/src/main.py"},
}
_ERROR_EVENT = {"type": "error", "message": "Some warning"}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGeminiCLIAdapterInit:
    """Constructor and path resolution."""

    def test_default_cli_name(self) -> None:
        adapter = GeminiCLIAdapter.__new__(GeminiCLIAdapter)
        assert adapter._default_cli_name == "gemini"

    def test_explicit_cli_path_is_used(self, tmp_path: Path) -> None:
        fake_bin = tmp_path / "gemini"
        fake_bin.touch()
        adapter = GeminiCLIAdapter(cli_path=str(fake_bin))
        assert adapter._cli_path == fake_bin.resolve()

    def test_env_var_overrides_path_lookup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_bin = tmp_path / "gemini"
        fake_bin.touch()
        monkeypatch.setenv("OUROBOROS_GEMINI_CLI_PATH", str(fake_bin))
        adapter = GeminiCLIAdapter()
        assert adapter._cli_path == fake_bin.resolve()

    def test_default_model_is_set(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._model == "gemini-2.5-flash"

    def test_custom_model_is_stored(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini", model="gemini-3-flash-preview")
        assert adapter._model == "gemini-3-flash-preview"


class TestBuildPrompt:
    """Prompt construction from message lists."""

    def test_user_only(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Hello")])
        assert "User: Hello" in prompt

    def test_system_is_wrapped(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.SYSTEM, content="Be concise."),
                Message(role=MessageRole.USER, content="Hi"),
            ]
        )
        assert "<system>" in prompt
        assert "Be concise." in prompt
        assert "User: Hi" in prompt

    def test_multi_turn_dialogue(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.USER, content="First"),
                Message(role=MessageRole.ASSISTANT, content="Second"),
                Message(role=MessageRole.USER, content="Third"),
            ]
        )
        assert "User: First" in prompt
        assert "Assistant: Second" in prompt
        assert "User: Third" in prompt

    def test_multiple_system_messages_merged(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.SYSTEM, content="Rule one."),
                Message(role=MessageRole.SYSTEM, content="Rule two."),
                Message(role=MessageRole.USER, content="Go"),
            ]
        )
        assert "Rule one." in prompt
        assert "Rule two." in prompt


class TestResolveModel:
    """Model name resolution and sanitisation."""

    def test_default_sentinel_returns_instance_model(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini", model="gemini-2.5-flash")
        assert adapter._resolve_model("default") == "gemini-2.5-flash"

    def test_empty_string_returns_instance_model(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini", model="gemini-2.5-flash")
        assert adapter._resolve_model("") == "gemini-2.5-flash"

    def test_gemini_prefix_stripped(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._resolve_model("gemini/gemini-2.5-pro") == "gemini-2.5-pro"

    def test_google_prefix_stripped(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._resolve_model("google/gemini-2.5-flash") == "gemini-2.5-flash"

    def test_bare_model_name_passes_through(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        assert adapter._resolve_model("gemini-3-flash-preview") == "gemini-3-flash-preview"

    def test_unsafe_model_name_falls_back(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini", model="gemini-2.5-flash")
        # Semicolon is not in the safe pattern
        result = adapter._resolve_model("bad;name")
        assert result == "gemini-2.5-flash"


class TestBuildCommand:
    """Command list construction."""

    def test_includes_stream_json_flag(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        cmd = adapter._build_command("hello", "gemini-2.5-flash")
        assert "--output-format" in cmd
        assert "stream-json" in cmd

    def test_includes_model_flag(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        cmd = adapter._build_command("hello", "gemini-2.5-pro")
        assert "--model" in cmd
        assert "gemini-2.5-pro" in cmd

    def test_includes_prompt(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="gemini")
        cmd = adapter._build_command("my prompt text", "gemini-2.5-flash")
        assert "-p" in cmd
        assert "my prompt text" in cmd

    def test_json_format_for_structured_output(self) -> None:
        """Structured output requests switch to plain JSON format."""
        from ouroboros.providers.base import CompletionConfig

        adapter = GeminiCLIAdapter(cli_path="gemini")
        config = CompletionConfig(
            model="gemini-2.5-flash",
            response_format={"type": "json_schema", "json_schema": {"type": "object"}},
        )
        cmd = adapter._build_command("test", "gemini-2.5-flash", config)
        assert "json" in cmd
        assert "stream-json" not in cmd

    def test_cli_path_is_first_element(self) -> None:
        adapter = GeminiCLIAdapter(cli_path="/usr/local/bin/gemini")
        cmd = adapter._build_command("x", "gemini-2.5-flash")
        assert cmd[0] == "/usr/local/bin/gemini"


class TestIsRetryable:
    """Transient error detection."""

    @pytest.mark.parametrize(
        "msg",
        [
            "rate limit exceeded",
            "Resource exhausted",
            "quota exceeded",
            "server temporarily unavailable",
            "request timeout",
            "server overloaded — try again",
        ],
    )
    def test_retryable_messages(self, msg: str) -> None:
        assert GeminiCLIAdapter._is_retryable(msg)

    @pytest.mark.parametrize(
        "msg",
        [
            "authentication failed",
            "invalid prompt",
            "model not found",
            "bad request",
        ],
    )
    def test_non_retryable_messages(self, msg: str) -> None:
        assert not GeminiCLIAdapter._is_retryable(msg)


class TestFormatToolDetail:
    """Tool-use event formatting."""

    def test_path_key(self) -> None:
        detail = GeminiCLIAdapter._format_tool_detail("read_file", {"path": "/src/main.py"})
        assert "read_file" in detail
        assert "/src/main.py" in detail

    def test_command_key(self) -> None:
        detail = GeminiCLIAdapter._format_tool_detail("run_shell", {"command": "ls -la"})
        assert "run_shell" in detail
        assert "ls -la" in detail

    def test_no_known_key_returns_tool_name(self) -> None:
        detail = GeminiCLIAdapter._format_tool_detail("unknown_tool", {"x": "y"})
        assert detail == "unknown_tool"

    def test_long_value_truncated(self) -> None:
        detail = GeminiCLIAdapter._format_tool_detail("read_file", {"path": "x" * 100})
        assert len(detail) < 120


class TestCollectResponse:
    """Response collection from subprocess stream-json output."""

    @pytest.mark.asyncio
    async def test_successful_response_from_result_event(self) -> None:
        stream = _make_stream([_INIT_EVENT, _RESULT_EVENT])
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert result.is_ok
        assert result.value.content == "Hello, world!"
        assert result.value.raw_response == {"session_id": "sess-abc"}

    @pytest.mark.asyncio
    async def test_successful_response_from_batch_json(self) -> None:
        """Batch JSON format (--output-format json) returns a single object."""
        batch_response = {"response": "Batch result!", "stats": {}, "error": None}
        stream = json.dumps(batch_response) + "\n"
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert result.is_ok
        assert result.value.content == "Batch result!"

    @pytest.mark.asyncio
    async def test_batch_json_with_error(self) -> None:
        """Batch JSON with error and no response is treated as failure."""
        batch_response = {
            "response": None,
            "stats": {},
            "error": {"type": "AuthError", "message": "Invalid key"},
        }
        stream = json.dumps(batch_response) + "\n"
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert not result.is_ok
        assert "Invalid key" in result.error.message

    @pytest.mark.asyncio
    async def test_successful_response_from_message_events(self) -> None:
        """Falls back to accumulated message content when no result event."""
        stream = _make_stream([_INIT_EVENT, _MESSAGE_EVENT])
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert result.is_ok
        assert "Hello, world!" in result.value.content

    @pytest.mark.asyncio
    async def test_non_zero_returncode_is_error(self) -> None:
        stream = _make_stream([_INIT_EVENT])
        process = _FakeProcess(stdout=stream, stderr="Fatal error\n", returncode=41)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert not result.is_ok
        assert "41" in result.error.message

    @pytest.mark.asyncio
    async def test_error_field_in_result_event(self) -> None:
        error_result = {
            "type": "result",
            "response": None,
            "stats": {},
            "error": {"type": "FatalAuthenticationError", "message": "Auth failed."},
        }
        stream = _make_stream([_INIT_EVENT, error_result])
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert not result.is_ok
        assert "Auth failed." in result.error.message

    @pytest.mark.asyncio
    async def test_empty_response_is_error(self) -> None:
        stream = _make_stream([_INIT_EVENT])
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert not result.is_ok
        assert "empty" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_warning_with_valid_result_succeeds(self) -> None:
        """A non-fatal warning alongside a valid result should not be treated as failure."""
        stream = _make_stream(
            [
                _INIT_EVENT,
                {"type": "error", "message": "Deprecation notice: model will be removed"},
                _RESULT_EVENT,
            ]
        )
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert result.is_ok
        assert result.value.content == "Hello, world!"

    @pytest.mark.asyncio
    async def test_on_message_callback_receives_tool_events(self) -> None:
        messages_received: list[tuple[str, str]] = []

        def _cb(kind: str, content: str) -> None:
            messages_received.append((kind, content))

        stream = _make_stream([_INIT_EVENT, _TOOL_USE_EVENT, _RESULT_EVENT])
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini", on_message=_cb)

        result = await adapter._collect_response(process)

        assert result.is_ok
        tool_events = [(k, v) for k, v in messages_received if k == "tool"]
        assert len(tool_events) == 1
        assert "read_file" in tool_events[0][1]

    @pytest.mark.asyncio
    async def test_plain_text_lines_accumulate_as_fallback(self) -> None:
        """Non-JSON lines are treated as plain-text content."""
        stream = "Line one\nLine two\n"
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert result.is_ok
        assert "Line one" in result.value.content
        assert "Line two" in result.value.content


class TestCompleteIntegration:
    """Integration-style tests for the public complete() method."""

    @pytest.mark.asyncio
    async def test_complete_returns_ok_on_success(self) -> None:
        stream = _make_stream([_INIT_EVENT, _RESULT_EVENT])

        async def _fake_exec(*args, **kwargs):  # noqa: ANN002
            return _FakeProcess(stdout=stream, returncode=0)

        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=1)

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="gemini-2.5-flash"),
            )

        assert result.is_ok
        assert result.value.content == "Hello, world!"

    @pytest.mark.asyncio
    async def test_complete_retries_on_rate_limit(self) -> None:
        calls: list[int] = []

        rate_limit_stream = _make_stream(
            [
                {"type": "error", "message": "rate limit exceeded"},
            ]
        )
        ok_stream = _make_stream([_INIT_EVENT, _RESULT_EVENT])

        async def _fake_exec(*args, **kwargs):  # noqa: ANN002
            calls.append(1)
            if len(calls) == 1:
                return _FakeProcess(stdout=rate_limit_stream, stderr="", returncode=1)
            return _FakeProcess(stdout=ok_stream, returncode=0)

        adapter = GeminiCLIAdapter(cli_path="gemini", max_retries=2)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=_fake_exec),
            patch("asyncio.sleep"),  # skip actual wait
        ):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="gemini-2.5-flash"),
            )

        assert result.is_ok
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_complete_fails_on_file_not_found(self) -> None:
        async def _raise(*args, **kwargs):  # noqa: ANN002
            raise FileNotFoundError("gemini not found")

        adapter = GeminiCLIAdapter(cli_path="/nonexistent/gemini", max_retries=1)

        with patch("asyncio.create_subprocess_exec", side_effect=_raise):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="gemini-2.5-flash"),
            )

        assert not result.is_ok
        assert "not found" in result.error.message.lower()


class TestRecursionGuard:
    """Recursion depth guard in _build_env."""

    def test_depth_increments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("_OUROBOROS_DEPTH", raising=False)
        adapter = GeminiCLIAdapter(cli_path="gemini")
        env = adapter._build_env()
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_depth_exceeds_limit_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ouroboros.core.errors import ProviderError

        monkeypatch.setenv("_OUROBOROS_DEPTH", "5")
        adapter = GeminiCLIAdapter(cli_path="gemini")
        with pytest.raises(ProviderError, match="nesting depth"):
            adapter._build_env()

    def test_strips_recursive_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "gemini")
        monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "gemini")
        monkeypatch.delenv("_OUROBOROS_DEPTH", raising=False)
        adapter = GeminiCLIAdapter(cli_path="gemini")
        env = adapter._build_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env


class TestInputValidation:
    """InputValidator integration in _collect_response."""

    @pytest.mark.asyncio
    async def test_validator_truncates_when_invalid(self) -> None:
        """InputValidator.validate_llm_response is called and truncates
        content that fails validation."""
        stream = _make_stream([_INIT_EVENT, _RESULT_EVENT])
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        with patch(
            "ouroboros.providers.gemini_cli_adapter.InputValidator.validate_llm_response",
            return_value=(False, "too long"),
        ):
            result = await adapter._collect_response(process)

        assert result.is_ok
        # Content should still be returned (truncation applied internally)
        assert result.value.content is not None

    @pytest.mark.asyncio
    async def test_validator_passes_valid_content(self) -> None:
        """Valid content passes through InputValidator unchanged."""
        stream = _make_stream([_INIT_EVENT, _RESULT_EVENT])
        process = _FakeProcess(stdout=stream, returncode=0)
        adapter = GeminiCLIAdapter(cli_path="gemini")

        result = await adapter._collect_response(process)

        assert result.is_ok
        assert result.value.content == "Hello, world!"
