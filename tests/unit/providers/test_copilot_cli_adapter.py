"""Unit tests for the GitHub Copilot CLI-backed LLM adapter."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.core.errors import ProviderError
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.copilot_cli_adapter import CopilotCliLLMAdapter


class _FakeStream:
    def __init__(self, text: str = "", *, read_size: int | None = None) -> None:
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


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self,
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        wait_forever: bool = False,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = None if wait_forever else returncode
        self._final_returncode = returncode
        self._wait_forever = wait_forever
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        if self._wait_forever and self.returncode is None:
            await asyncio.Future()
        self.returncode = self._final_returncode
        return self.returncode

    async def communicate(self, _input: bytes | None = None) -> tuple[bytes, bytes]:
        raise AssertionError("communicate() should not be used by the streaming adapter")

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = self._final_returncode


class TestPromptBuilding:
    def test_preserves_system_and_roles(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd())
        prompt = adapter._build_prompt(
            [
                Message(role=MessageRole.SYSTEM, content="Be concise."),
                Message(role=MessageRole.USER, content="Why is the sky blue?"),
                Message(role=MessageRole.ASSISTANT, content="Rayleigh scattering."),
                Message(role=MessageRole.USER, content="Explain it."),
            ]
        )
        assert "## System Instructions" in prompt
        assert "Be concise." in prompt
        assert "User: Why is the sky blue?" in prompt
        assert "Assistant: Rayleigh scattering." in prompt
        assert "User: Explain it." in prompt

    def test_tool_constraints_listed_when_provided(self) -> None:
        adapter = CopilotCliLLMAdapter(
            cli_path="copilot",
            allowed_tools=["read", "grep"],
            max_turns=4,
        )
        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Search.")])
        assert "## Tool Constraints" in prompt
        assert "- read" in prompt
        assert "- grep" in prompt
        assert "## Execution Budget" in prompt
        assert "4 tool-assisted turns" in prompt

    def test_explicit_empty_tools_forbids_use(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", allowed_tools=[], max_turns=3)
        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Summarise.")])
        assert "Do NOT use any tools or MCP calls" in prompt
        assert "tool-assisted turns" not in prompt

    def test_no_tool_section_when_unspecified(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        prompt = adapter._build_prompt([Message(role=MessageRole.USER, content="Hi")])
        assert "## Tool Constraints" not in prompt

    def test_json_object_directive_appended(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        prompt = adapter._build_prompt(
            [Message(role=MessageRole.USER, content="Return data.")],
            response_format={"type": "json_object"},
        )
        assert "## Response Format" in prompt
        assert "single JSON object" in prompt

    def test_json_schema_directive_includes_schema(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        prompt = adapter._build_prompt(
            [Message(role=MessageRole.USER, content="Return a vote.")],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "type": "object",
                    "properties": {"approved": {"type": "boolean"}},
                    "required": ["approved"],
                },
            },
        )
        assert "Schema:" in prompt
        assert '"approved"' in prompt


class TestCommandBuilding:
    def test_default_command_includes_add_dir_and_read_only_flags(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd="/tmp/work")
        command = adapter._build_command(model=None)
        assert command[0] == "copilot"
        assert "--add-dir" in command
        add_dir_index = command.index("--add-dir")
        # On Windows, Path expansion normalises forward slashes to backslashes;
        # compare via Path equality so the test is platform-neutral.
        assert Path(command[add_dir_index + 1]) == Path("/tmp/work")
        # Default permission mode = read_only -> empty allowlist (Copilot
        # exits 1 with --deny-tool=* so we use --available-tools= instead).
        assert "--available-tools=" in command
        assert "--allow-all-tools" not in command

    def test_command_emits_tool_envelope_when_allowed_tools_set(self) -> None:
        adapter = CopilotCliLLMAdapter(
            cli_path="copilot",
            allowed_tools=["read", "grep"],
            permission_mode="acceptEdits",
        )
        command = adapter._build_command(model=None)
        assert "--available-tools=read,grep" in command
        assert "--allow-tool=read,grep" in command
        # acceptEdits maps to plain --allow-all-tools (no deny wildcard).
        assert "--allow-all-tools" in command
        assert "--deny-tool=*" not in command

    def test_command_uses_allow_all_for_bypass_mode(self) -> None:
        adapter = CopilotCliLLMAdapter(
            cli_path="copilot",
            permission_mode="bypassPermissions",
        )
        command = adapter._build_command(model=None)
        assert "--allow-all" in command

    def test_command_includes_model_when_provided(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        command = adapter._build_command(model="claude-sonnet-4.5")
        idx = command.index("--model")
        assert command[idx + 1] == "claude-sonnet-4.5"

    def test_command_uses_agent_over_model_when_runtime_profile_set(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", runtime_profile="worker")
        command = adapter._build_command(model="gpt-5", agent="ouroboros-fast")
        # runtime_profile -> --agent ouroboros-worker takes precedence.
        agent_idx = command.index("--agent")
        assert command[agent_idx + 1] == "ouroboros-worker"
        assert "--model" not in command
        # Conflicting per-call agent is dropped.
        assert "ouroboros-fast" not in command

    def test_command_omits_agent_when_runtime_profile_none(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        command = adapter._build_command(model=None)
        assert "--agent" not in command


class TestNormalizeModel:
    def test_default_returns_none(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        assert adapter._normalize_model("default") is None
        assert adapter._normalize_model("  ") is None

    def test_passes_through_safe_names(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        assert adapter._normalize_model("claude-sonnet-4.5") == "claude-sonnet-4.5"

    def test_rejects_unsafe_model_name(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        with pytest.raises(ValueError, match="Unsafe model name"):
            adapter._normalize_model("../etc/passwd; rm -rf /")


class TestEventParsing:
    def test_parse_json_event_returns_dict(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        assert adapter._parse_json_event('{"type": "ok"}') == {"type": "ok"}

    def test_parse_json_event_returns_none_for_garbage(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        assert adapter._parse_json_event("not json") is None
        assert adapter._parse_json_event("[1,2,3]") is None  # not a dict

    def test_extract_text_from_nested_dict(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        text = adapter._extract_text({"message": {"text": "Hello world"}})
        assert text == "Hello world"

    def test_extract_session_id_supports_multiple_keys(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        assert (
            adapter._extract_session_id_from_event({"session_id": "abc"}) == "abc"
        )
        assert (
            adapter._extract_session_id_from_event({"sessionId": "def"}) == "def"
        )
        assert (
            adapter._extract_session_id_from_event({"thread_id": "ghi"}) == "ghi"
        )
        assert adapter._extract_session_id_from_event({"foo": "bar"}) is None

    def test_extract_stdout_errors_collects_error_events(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        lines = [
            json.dumps({"type": "error", "message": "rate limit reached"}),
            json.dumps({"type": "info", "message": "ignored"}),
            json.dumps({"type": "turn.failed", "error": {"message": "auth missing"}}),
        ]
        errors = adapter._extract_stdout_errors(lines)
        assert errors == ["rate limit reached", "auth missing"]


class TestRetryLogic:
    def test_is_retryable_error_matches_known_patterns(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot")
        assert adapter._is_retryable_error("Rate limit exceeded, try again later")
        assert adapter._is_retryable_error("Connection reset by peer")
        assert adapter._is_retryable_error("GitHub API error: 502")
        assert adapter._is_retryable_error("Quota exceeded for user")
        assert not adapter._is_retryable_error("Invalid model name")

    def test_looks_like_auth_error_matches_known_patterns(self) -> None:
        assert CopilotCliLLMAdapter._looks_like_auth_error("401 Unauthorized")
        assert CopilotCliLLMAdapter._looks_like_auth_error("Missing token: GH_TOKEN")
        assert CopilotCliLLMAdapter._looks_like_auth_error("Authentication required")
        assert not CopilotCliLLMAdapter._looks_like_auth_error("Invalid prompt")


class TestChildEnv:
    def test_child_env_strips_recursive_markers(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OUROBOROS_AGENT_RUNTIME": "copilot",
                "OUROBOROS_LLM_BACKEND": "copilot",
                "COPILOT_SESSION_ID": "x",
                "_OUROBOROS_DEPTH": "1",
                "GH_TOKEN": "ghp_xxx",
            },
            clear=False,
        ):
            env = CopilotCliLLMAdapter._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert "COPILOT_SESSION_ID" not in env
        assert env["_OUROBOROS_DEPTH"] == "2"
        assert env["GH_TOKEN"] == "ghp_xxx"

    def test_child_env_raises_provider_error_at_max_depth(self) -> None:
        with patch.dict(os.environ, {"_OUROBOROS_DEPTH": "5"}, clear=False):
            with pytest.raises(ProviderError, match="nesting depth"):
                CopilotCliLLMAdapter._build_child_env()


class TestComplete:
    @pytest.mark.asyncio
    async def test_success_uses_streaming_jsonl_output(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd())

        stdout = "\n".join(
            [
                json.dumps({"type": "session.started", "session_id": "sess-1"}),
                json.dumps(
                    {
                        "type": "agent.message",
                        "message": {"text": "All clear."},
                    }
                ),
            ]
        )

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            assert command[0] == "copilot"
            # Prompt arrives via -p, last positional arguments.
            assert "-p" in command
            assert kwargs.get("stdin") is not None
            return _FakeProcess(stdout=stdout, returncode=0)

        with patch(
            "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Status?")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert result.value.content == "All clear."
        assert result.value.raw_response["session_id"] == "sess-1"
        assert result.value.raw_response["usage_estimated"] is True

    @pytest.mark.asyncio
    async def test_falls_back_to_plain_text_when_no_jsonl(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd())

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout="Hello there!\n", returncode=0)

        with patch(
            "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Greet")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert "Hello there!" in result.value.content

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_provider_error(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd())

        stdout = json.dumps({"type": "error", "message": "Invalid prompt format"})

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout=stdout, stderr="boom", returncode=2)

        with patch(
            "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Bad input")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "Invalid prompt format" in result.error.message
        assert result.error.details["returncode"] == 2

    @pytest.mark.asyncio
    async def test_auth_error_short_circuits_retries(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd(), max_retries=3)
        call_count = 0

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            nonlocal call_count
            call_count += 1
            return _FakeProcess(
                stdout="",
                stderr="401 Unauthorized: missing GH_TOKEN",
                returncode=1,
            )

        with patch(
            "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Hi")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "GH_TOKEN" in result.error.message
        assert result.error.details["auth_error"] is True
        assert call_count == 1  # No retry on auth error.

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit_then_succeeds(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd(), max_retries=3)
        call_count = 0
        success_stdout = json.dumps({"type": "agent.message", "message": "ok"})

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _FakeProcess(
                    stdout=json.dumps({"type": "error", "message": "Rate limit reached"}),
                    returncode=1,
                )
            return _FakeProcess(stdout=success_stdout, returncode=0)

        with (
            patch(
                "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
            patch("ouroboros.providers.copilot_cli_adapter.asyncio.sleep") as mock_sleep,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Hi")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert call_count == 2
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_missing_cli_returns_provider_error(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot-not-installed", cwd=os.getcwd())

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> Any:
            raise FileNotFoundError("copilot not on PATH")

        with patch(
            "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Hi")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "not found" in result.error.message.lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_partial_content(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd(), timeout=0.05)

        # Pre-buffered partial JSONL message that is delivered before the wait
        # blocks indefinitely. Wait_forever ensures we hit the timeout branch.
        stdout = json.dumps({"type": "agent.message", "message": "Working on it"}) + "\n"

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout=stdout, wait_forever=True)

        with patch(
            "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Hang")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.details["timed_out"] is True
        assert result.error.details["timeout_seconds"] == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_depth_guard_returns_provider_error_without_spawning(self) -> None:
        adapter = CopilotCliLLMAdapter(cli_path="copilot", cwd=os.getcwd())

        spawn_calls = 0

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> Any:
            nonlocal spawn_calls
            spawn_calls += 1
            return _FakeProcess(returncode=0)

        with (
            patch.dict(os.environ, {"_OUROBOROS_DEPTH": "5"}, clear=False),
            patch(
                "ouroboros.providers.copilot_cli_adapter.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Hi")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "nesting depth" in result.error.message
        assert spawn_calls == 0
