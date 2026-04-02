"""Unit tests for GeminiCLIRuntime."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        encoded = "".join(f"{line}\n" for line in lines).encode()
        self._buffer = bytearray(encoded)

    async def read(self, n: int = -1) -> bytes:
        if not self._buffer:
            return b""
        if n < 0 or n >= len(self._buffer):
            data = bytes(self._buffer)
            self._buffer.clear()
            return data
        data = bytes(self._buffer[:n])
        del self._buffer[:n]
        return data


class _FakeStdin:
    def __init__(self) -> None:
        self.written = bytearray()

    def write(self, data: bytes) -> None:
        self.written.extend(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


class _FakeProcess:
    def __init__(
        self,
        stdout_lines: list[str],
        stderr_lines: list[str],
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode

    async def wait(self) -> int:
        return self._returncode


class TestGeminiCLIRuntime:
    """Tests for GeminiCLIRuntime."""

    def test_runtime_backend_identifier(self) -> None:
        """runtime_backend property returns the canonical identifier."""
        runtime = GeminiCLIRuntime(cli_path="gemini", cwd="/tmp/project")
        assert runtime.runtime_backend == "gemini_cli"

    def test_permission_mode_default(self) -> None:
        """Default permission mode is 'default'."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        assert runtime.permission_mode == "default"

    def test_permission_mode_normalized(self) -> None:
        """Permission modes are normalized to valid values."""
        runtime = GeminiCLIRuntime(cli_path="gemini", permission_mode="acceptEdits")
        assert runtime.permission_mode == "acceptEdits"

    def test_permission_mode_invalid_falls_back_to_default(self) -> None:
        """Unknown permission modes fall back to 'default'."""
        runtime = GeminiCLIRuntime(cli_path="gemini", permission_mode="unknown_mode")
        assert runtime.permission_mode == "default"

    def test_build_command_basic(self) -> None:
        """Default command contains only the CLI path."""
        runtime = GeminiCLIRuntime(cli_path="/usr/local/bin/gemini")
        command = runtime._build_command("/tmp/out.txt")
        assert command == ["/usr/local/bin/gemini"]

    def test_build_command_with_model(self) -> None:
        """Model is appended as --model flag."""
        runtime = GeminiCLIRuntime(cli_path="gemini", model="gemini-2.5-pro")
        command = runtime._build_command("/tmp/out.txt")
        assert "--model" in command
        assert "gemini-2.5-pro" in command

    def test_build_command_ignores_output_path(self) -> None:
        """output_last_message_path is intentionally ignored."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        command = runtime._build_command("/tmp/should-not-appear.txt")
        assert "/tmp/should-not-appear.txt" not in command
        assert "--output-last-message" not in command

    def test_build_command_no_permission_flags(self) -> None:
        """No Codex-style permission flags are added."""
        for mode in ("default", "acceptEdits", "bypassPermissions"):
            runtime = GeminiCLIRuntime(cli_path="gemini", permission_mode=mode)
            command = runtime._build_command("/tmp/out.txt")
            assert "--full-auto" not in command
            assert "--dangerously-bypass-approvals-and-sandbox" not in command
            assert "--sandbox" not in command

    def test_parse_json_event_plain_text_wrapped(self) -> None:
        """Plain text lines are wrapped as gemini.content events."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        event = runtime._parse_json_event("Hello, world!")
        assert event is not None
        assert event["type"] == "gemini.content"
        assert event["text"] == "Hello, world!"

    def test_parse_json_event_empty_line_returns_none(self) -> None:
        """Empty or whitespace-only lines return None."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        assert runtime._parse_json_event("") is None
        assert runtime._parse_json_event("   ") is None

    def test_parse_json_event_valid_json_passthrough(self) -> None:
        """Valid JSON dict lines are parsed and returned as-is."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        event = runtime._parse_json_event('{"type": "custom.event", "data": "test"}')
        assert event is not None
        assert event["type"] == "custom.event"
        assert event["data"] == "test"

    def test_convert_event_gemini_content(self) -> None:
        """gemini.content events yield an assistant AgentMessage."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        messages = runtime._convert_event(
            {"type": "gemini.content", "text": "Here is the answer."},
            current_handle=None,
        )
        assert len(messages) == 1
        assert messages[0].type == "assistant"
        assert messages[0].content == "Here is the answer."

    def test_convert_event_empty_gemini_content_returns_empty(self) -> None:
        """gemini.content events with empty text yield nothing."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        messages = runtime._convert_event(
            {"type": "gemini.content", "text": ""},
            current_handle=None,
        )
        assert messages == []

    def test_build_resume_recovery_always_returns_none(self) -> None:
        """GeminiCLIRuntime never attempts session resumption."""
        runtime = GeminiCLIRuntime(cli_path="gemini")
        result = runtime._build_resume_recovery(
            attempted_resume_session_id="some-id",
            current_handle=None,
            returncode=1,
            final_message="error",
            stderr_lines=["error line"],
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_execute_task_plain_text_output(self) -> None:
        """Plain text output is collected and yielded as assistant messages."""
        runtime = GeminiCLIRuntime(cli_path="gemini", cwd="/tmp/project")

        async def fake_subprocess(*args: Any, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout_lines=["I analyzed the code.", "The fix is on line 42."],
                stderr_lines=[],
                returncode=0,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess,
        ):
            messages = []
            async for msg in runtime.execute_task(
                "Analyze the auth module",
                tools=["Read"],
            ):
                messages.append(msg)

        assert messages, "Expected at least one message"
        final = messages[-1]
        assert final.type == "result"

        content_messages = [m for m in messages if m.type == "assistant"]
        assert content_messages, "Expected at least one assistant message"
        combined = " ".join(m.content for m in content_messages)
        assert "analyzed" in combined.lower() or "fix" in combined.lower()

    @pytest.mark.asyncio
    async def test_execute_task_cli_not_found(self) -> None:
        """FileNotFoundError yields a result message with error subtype."""
        runtime = GeminiCLIRuntime(cli_path="/nonexistent/gemini")

        async def fake_subprocess(*args: Any, **kwargs: Any) -> _FakeProcess:
            raise FileNotFoundError("No such file: /nonexistent/gemini")

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess,
        ):
            messages = []
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        assert messages
        final = messages[-1]
        assert final.type == "result"
        assert final.data.get("subtype") == "error"

    @pytest.mark.asyncio
    async def test_execute_task_nonzero_returncode_yields_error(self) -> None:
        """Non-zero exit code results in a result message with error subtype."""
        runtime = GeminiCLIRuntime(cli_path="gemini")

        async def fake_subprocess(*args: Any, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout_lines=["partial output"],
                stderr_lines=["Authentication failed"],
                returncode=1,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess,
        ):
            messages = []
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        assert messages
        final = messages[-1]
        assert final.type == "result"
        assert final.data.get("subtype") == "error"

    @pytest.mark.asyncio
    async def test_execute_task_no_session_id_without_structured_output(self) -> None:
        """Plain text output produces no native_session_id on the runtime handle."""
        runtime = GeminiCLIRuntime(cli_path="gemini")

        async def fake_subprocess(*args: Any, **kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout_lines=["response text"],
                stderr_lines=[],
                returncode=0,
            )

        with patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_subprocess,
        ):
            messages = []
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        final = messages[-1]
        # No session ID since Gemini CLI doesn't emit session events
        if final.resume_handle is not None:
            assert final.resume_handle.native_session_id is None


class TestGeminiCLIRuntimeFactory:
    """Tests for GeminiCLIRuntime through the runtime factory."""

    def test_factory_creates_gemini_runtime(self) -> None:
        """create_agent_runtime returns a GeminiCLIRuntime for gemini backend."""
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
        from ouroboros.orchestrator.runtime_factory import create_agent_runtime

        with (
            patch(
                "ouroboros.orchestrator.runtime_factory.get_gemini_cli_path",
                return_value=None,
            ),
            patch(
                "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
                return_value=None,
            ),
        ):
            runtime = create_agent_runtime(backend="gemini")

        assert isinstance(runtime, GeminiCLIRuntime)

    def test_resolve_runtime_backend_returns_gemini(self) -> None:
        """resolve_agent_runtime_backend normalizes gemini variants to 'gemini'."""
        from ouroboros.orchestrator.runtime_factory import resolve_agent_runtime_backend

        assert resolve_agent_runtime_backend("gemini") == "gemini"
        assert resolve_agent_runtime_backend("gemini_cli") == "gemini"
        assert resolve_agent_runtime_backend("GEMINI") == "gemini"
