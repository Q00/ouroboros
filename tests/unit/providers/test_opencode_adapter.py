"""Unit tests for the OpenCode CLI-backed LLM adapter."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter


class _FakeStream:
    def __init__(
        self,
        text: str = "",
        *,
        read_size: int | None = None,
    ) -> None:
        self._buffer = text.encode("utf-8")
        self._cursor = 0
        self._read_size = read_size

    async def read(self, chunk_size: int = 16384) -> bytes:
        if self._cursor >= len(self._buffer):
            return b""

        size = self._read_size or chunk_size
        next_cursor = min(self._cursor + size, len(self._buffer))
        chunk = self._buffer[self._cursor : next_cursor]
        self._cursor = next_cursor
        return chunk


class _FakeAdapterStdin:
    """Minimal stdin mock for adapter tests."""

    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        pass


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        read_size: int | None = None,
    ) -> None:
        self.stdin = _FakeAdapterStdin()
        self.stdout = _FakeStream(stdout, read_size=read_size)
        self.stderr = _FakeStream(stderr, read_size=read_size)
        self.returncode = returncode
        self._final_returncode = returncode
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._final_returncode


class TestOpenCodeLLMAdapter:
    """Tests for OpenCodeLLMAdapter."""

    def test_build_prompt_preserves_system_and_roles(self) -> None:
        """Prompt builder keeps system instructions and conversation order."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp/project")
        messages = [
            Message(role=MessageRole.SYSTEM, content="Be concise."),
            Message(role=MessageRole.USER, content="Hello!"),
        ]
        prompt = adapter._build_prompt(messages)
        assert "## System Instructions" in prompt
        assert "Be concise." in prompt
        assert "### User" in prompt
        assert "Hello!" in prompt

    def test_build_prompt_multi_turn(self) -> None:
        """Multi-turn conversations preserve all messages."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp/project")
        messages = [
            Message(role=MessageRole.USER, content="What is 2+2?"),
            Message(role=MessageRole.ASSISTANT, content="4"),
            Message(role=MessageRole.USER, content="And 3+3?"),
        ]
        prompt = adapter._build_prompt(messages)
        assert "What is 2+2?" in prompt
        assert "4" in prompt
        assert "And 3+3?" in prompt

    def test_build_command_basic(self) -> None:
        """Command includes run --format json but NOT the prompt (piped via stdin)."""
        adapter = OpenCodeLLMAdapter(cli_path="/usr/bin/opencode", cwd="/tmp")
        cmd = adapter._build_command("Hello world")
        assert cmd == ["/usr/bin/opencode", "run", "--format", "json"]
        assert "Hello world" not in cmd

    def test_build_command_with_model(self) -> None:
        """Command includes --model when specified."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        cmd = adapter._build_command("Hello", model="anthropic/claude-sonnet-4-20250514")
        assert "--model" in cmd
        assert "anthropic/claude-sonnet-4-20250514" in cmd

    def test_normalize_model_default(self) -> None:
        """Default model returns None."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        assert adapter._normalize_model("default") is None
        assert adapter._normalize_model("") is None

    def test_normalize_model_unsafe(self) -> None:
        """Unsafe model names are rejected."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        with pytest.raises(ValueError, match="Unsafe model name"):
            adapter._normalize_model("model; rm -rf /")

    def test_extract_text_from_events(self) -> None:
        """Text extraction finds assistant text from events."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        events = [
            {
                "type": "text",
                "part": {"type": "text", "text": "Hello from OpenCode!"},
            },
            {
                "type": "tool_use",
                "part": {
                    "tool": "bash",
                    "state": {"status": "completed", "output": "file.txt"},
                },
            },
        ]
        result = adapter._extract_text_from_events(events)
        assert "Hello from OpenCode!" in result

    def test_extract_error_from_events(self) -> None:
        """Error extraction finds error messages from events."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        events = [
            {
                "type": "error",
                "error": {
                    "name": "RateLimitError",
                    "data": {"message": "Rate limit exceeded"},
                },
            },
        ]
        result = adapter._extract_error_from_events(events)
        assert result == "Rate limit exceeded"

    def test_extract_error_no_errors(self) -> None:
        """No error returns None."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        events = [{"type": "text", "part": {"text": "OK"}}]
        assert adapter._extract_error_from_events(events) is None

    def test_extract_error_ignores_tool_use_state(self) -> None:
        """tool_use with state.error is NOT treated as terminal.

        Only top-level error events are terminal — tool errors are
        expected during normal agent self-correction workflows.
        """
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        events = [
            {
                "type": "tool_use",
                "part": {
                    "tool": "bash",
                    "state": {
                        "input": {"command": "bad-cmd"},
                        "status": "completed",
                        "output": "",
                        "error": "command not found",
                    },
                },
            },
        ]
        result = adapter._extract_error_from_events(events)
        assert result is None, "tool_use.state.error must not be terminal"

    @pytest.mark.asyncio
    async def test_complete_tool_error_with_zero_exit_is_success(self) -> None:
        """tool_use.state.error + exit 0 = success (exit code is authoritative).

        The adapter trusts the process exit code. Intermediate tool
        errors are normal for agents that retry or switch strategies.
        """
        tool_error_event = json.dumps(
            {
                "type": "tool_use",
                "sessionID": "sess-1",
                "part": {
                    "tool": "bash",
                    "state": {
                        "input": {"command": "failing-tool"},
                        "status": "completed",
                        "output": "",
                        "error": "tool crashed hard",
                    },
                },
            }
        )
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Recovered and done."},
            }
        )
        stdout = tool_error_event + "\n" + text_event + "\n"
        process = _FakeProcess(stdout=stdout, returncode=0)
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")

        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Run it")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_ok, "tool_use.state.error + exit 0 must be success"
        assert "Recovered" in result.value.content

    @pytest.mark.asyncio
    async def test_complete_top_level_error_overrides_exit_code(self) -> None:
        """A top-level error event must produce ProviderError even with exit 0."""
        error_event = json.dumps(
            {
                "type": "error",
                "sessionID": "sess-1",
                "error": {
                    "name": "RuntimeError",
                    "data": {"message": "Session crashed"},
                },
            }
        )
        stdout = error_event + "\n"
        process = _FakeProcess(stdout=stdout, returncode=0)
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")

        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Calculate")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_err, "Top-level error must override exit code"
        assert "Session crashed" in result.error.message

    def test_extract_error_tool_use_not_terminal(self) -> None:
        """tool_use.state.error is not returned by _extract_error_from_events."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        events = [
            {
                "type": "tool_use",
                "part": {
                    "tool": "bash",
                    "state": {"error": "fail"},
                },
            },
            {
                "type": "text",
                "part": {"type": "text", "text": "Recovered."},
            },
        ]
        assert adapter._extract_error_from_events(events) is None

    @pytest.mark.asyncio
    async def test_complete_success(self) -> None:
        """Successful completion returns text content."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "The answer is 42."},
            }
        )
        stdout = text_event + "\n"

        fake_process = _FakeProcess(stdout=stdout, returncode=0)

        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp/project")

        with patch("asyncio.create_subprocess_exec", return_value=fake_process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="What is 42?")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert "42" in result.value.content

    @pytest.mark.asyncio
    async def test_complete_error_event(self) -> None:
        """Error events are surfaced as ProviderError."""
        error_event = json.dumps(
            {
                "type": "error",
                "sessionID": "sess-1",
                "error": {"name": "AuthError", "data": {"message": "Invalid API key"}},
            }
        )
        stdout = error_event + "\n"

        fake_process = _FakeProcess(stdout=stdout, returncode=1)

        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp/project", max_retries=1)

        with patch("asyncio.create_subprocess_exec", return_value=fake_process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "Invalid API key" in result.error.message

    @pytest.mark.asyncio
    async def test_complete_nonzero_exit(self) -> None:
        """Non-zero exit code returns error."""
        fake_process = _FakeProcess(stdout="", stderr="segfault", returncode=139)

        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp", max_retries=1)

        with patch("asyncio.create_subprocess_exec", return_value=fake_process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "139" in result.error.message

    @pytest.mark.asyncio
    async def test_complete_no_text_with_stderr_is_error(self) -> None:
        """Exit 0 with no assistant text must be an error, not stderr-as-response.

        stderr is transport/runtime noise and must never be promoted
        to completion content.  Matches the Codex adapter pattern.
        """
        # Only a tool_use event, no text event — exit 0
        tool_event = json.dumps(
            {
                "type": "tool_use",
                "sessionID": "sess-1",
                "part": {"tool": "bash", "state": {"status": "completed"}},
            }
        )
        stdout = tool_event + "\n"
        fake_process = _FakeProcess(stdout=stdout, stderr="plugin loaded OK", returncode=0)

        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp", max_retries=1)

        with patch("asyncio.create_subprocess_exec", return_value=fake_process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_err, "No assistant text + exit 0 must be an error"
        assert "Empty response" in result.error.message

    @pytest.mark.asyncio
    async def test_complete_empty_stdout_empty_stderr_is_error(self) -> None:
        """Exit 0 with no stdout and no stderr must still be an error."""
        fake_process = _FakeProcess(stdout="", stderr="", returncode=0)
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp", max_retries=1)

        with patch("asyncio.create_subprocess_exec", return_value=fake_process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_err, "Completely empty output + exit 0 must be an error"
        assert "Empty response" in result.error.message

    @pytest.mark.asyncio
    async def test_complete_writes_prompt_to_stdin(self) -> None:
        """The adapter must pipe the prompt via stdin, not argv."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Got it."},
            }
        )
        fake_process = _FakeProcess(stdout=text_event + "\n", returncode=0)
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")

        with patch("asyncio.create_subprocess_exec", return_value=fake_process):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="My prompt text")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_ok
        # Verify prompt was written to stdin
        assert fake_process.stdin.written, "Prompt must be written to stdin"
        written_text = fake_process.stdin.written.decode("utf-8")
        assert "My prompt text" in written_text

    @pytest.mark.asyncio
    async def test_complete_not_found(self) -> None:
        """FileNotFoundError is handled gracefully."""
        adapter = OpenCodeLLMAdapter(cli_path="/nonexistent/opencode", cwd="/tmp", max_retries=1)

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("/nonexistent/opencode"),
        ):
            result = await adapter.complete(
                messages=[Message(role=MessageRole.USER, content="Hello")],
                config=CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "not found" in result.error.message.lower()

    def test_is_retryable(self) -> None:
        """Retryable error patterns are detected."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        assert adapter._is_retryable("rate limit exceeded") is True
        assert adapter._is_retryable("timeout waiting for response") is True
        assert adapter._is_retryable("invalid syntax in prompt") is False

    def test_build_child_env_strips_ouroboros_vars(self) -> None:
        """Child environment strips Ouroboros runtime variables."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        with patch.dict(
            "os.environ",
            {"OUROBOROS_AGENT_RUNTIME": "opencode", "OUROBOROS_LLM_BACKEND": "opencode"},
        ):
            env = adapter._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert env["_OUROBOROS_DEPTH"] == "1"

    def test_build_child_env_depth_guard(self) -> None:
        """Exceeding max nesting depth raises RuntimeError."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "5"}):
            with pytest.raises(RuntimeError, match="Maximum Ouroboros nesting depth"):
                adapter._build_child_env()

    def test_allowed_tools_empty_forces_text_only(self) -> None:
        """Empty allowed_tools list produces a text-only constraint in the prompt."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp", allowed_tools=[])
        messages = [Message(role=MessageRole.USER, content="Hello")]
        prompt = adapter._build_prompt(messages)
        assert "Do NOT use any tools" in prompt

    def test_allowed_tools_none_no_constraint(self) -> None:
        """Default (None) allowed_tools omits Tool Constraints from the prompt."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        messages = [Message(role=MessageRole.USER, content="Hello")]
        prompt = adapter._build_prompt(messages)
        assert "Tool Constraints" not in prompt

    def test_max_turns_one_single_turn_constraint(self) -> None:
        """max_turns=1 adds single turn constraint to the prompt."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp", max_turns=1)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        prompt = adapter._build_prompt(messages)
        assert "single turn" in prompt

    def test_max_turns_multi_turn_constraint(self) -> None:
        """max_turns=5 adds a 5 turns constraint to the prompt."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp", max_turns=5)
        messages = [Message(role=MessageRole.USER, content="Hello")]
        prompt = adapter._build_prompt(messages)
        assert "5 turns" in prompt

    def test_extract_text_excludes_tool_output(self) -> None:
        """Text extraction returns only text events, not tool_use output."""
        adapter = OpenCodeLLMAdapter(cli_path="opencode", cwd="/tmp")
        events = [
            {
                "type": "text",
                "part": {"type": "text", "text": "Here is my answer."},
            },
            {
                "type": "tool_use",
                "part": {
                    "tool": "bash",
                    "state": {"status": "completed", "output": "SECRET_TOOL_OUTPUT"},
                },
            },
        ]
        result = adapter._extract_text_from_events(events)
        assert "Here is my answer." in result
        assert "SECRET_TOOL_OUTPUT" not in result
