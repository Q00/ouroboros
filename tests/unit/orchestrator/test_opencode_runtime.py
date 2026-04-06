"""Unit tests for OpenCodeRuntime."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime


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
    """Minimal stdin mock that supports write/drain/close/wait_closed."""

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
        stdout_lines: list[str],
        stderr_lines: list[str],
        returncode: int = 0,
    ) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode
        self.returncode = None
        self.pid = 12345
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._returncode

    def kill(self) -> None:
        self.returncode = self._returncode


class TestOpenCodeRuntimeProperties:
    """Test basic runtime properties."""

    def test_runtime_backend(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime.runtime_backend == "opencode"

    def test_working_directory(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project")
        assert runtime.working_directory == "/tmp/project"

    def test_permission_mode_default(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime.permission_mode == "bypassPermissions"

    def test_permission_mode_custom(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp", permission_mode="acceptEdits")
        assert runtime.permission_mode == "acceptEdits"


class TestOpenCodeRuntimeBuildCommand:
    """Test command building."""

    def test_basic_command(self) -> None:
        runtime = OpenCodeRuntime(cli_path="/usr/bin/opencode", cwd="/tmp")
        cmd = runtime._build_command(prompt="Hello world")
        assert cmd == ["/usr/bin/opencode", "run", "--format", "json"]
        assert "Hello world" not in cmd  # prompt piped via stdin, not argv

    def test_command_with_model(self) -> None:
        runtime = OpenCodeRuntime(
            cli_path="opencode", cwd="/tmp", model="anthropic/claude-sonnet-4-20250514"
        )
        cmd = runtime._build_command(prompt="Hello")
        assert "--model" in cmd
        assert "anthropic/claude-sonnet-4-20250514" in cmd

    def test_command_with_resume_session(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        cmd = runtime._build_command(
            resume_session_id="sess-abc123",
            prompt="Continue",
        )
        assert "--session" in cmd
        assert "sess-abc123" in cmd
        assert "Continue" not in cmd  # prompt piped via stdin

    def test_command_rejects_unsafe_session_id(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with pytest.raises(ValueError, match="Invalid resume_session_id"):
            runtime._build_command(
                resume_session_id="sess; rm -rf /",
                prompt="Hello",
            )


class TestOpenCodeRuntimeComposePrompt:
    """Test prompt composition."""

    def test_basic_prompt(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        result = runtime._compose_prompt("Do the thing", None, None)
        assert result == "Do the thing"

    def test_prompt_with_system(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        result = runtime._compose_prompt("Do the thing", "Be helpful.", None)
        assert "## System Instructions" in result
        assert "Be helpful." in result
        assert "Do the thing" in result

    def test_prompt_with_tools(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        result = runtime._compose_prompt("Do the thing", None, ["Read", "Edit"])
        assert "## Tooling Guidance" in result
        assert "- Read" in result
        assert "- Edit" in result


class TestOpenCodeRuntimeHandleManagement:
    """Test runtime handle creation."""

    def test_build_runtime_handle_new(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        handle = runtime._build_runtime_handle("sess-123")
        assert handle is not None
        assert handle.backend == "opencode"
        assert handle.native_session_id == "sess-123"
        assert handle.cwd == "/tmp"

    def test_build_runtime_handle_none(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        handle = runtime._build_runtime_handle(None)
        assert handle is None

    def test_build_runtime_handle_update(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        existing = RuntimeHandle(
            backend="opencode",
            native_session_id="sess-old",
            cwd="/tmp",
        )
        updated = runtime._build_runtime_handle("sess-new", existing)
        assert updated is not None
        assert updated.native_session_id == "sess-new"
        assert updated.backend == "opencode"


class TestOpenCodeRuntimeEventConversion:
    """Test JSON event parsing and conversion."""

    def test_parse_json_event_valid(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        event = runtime._parse_json_event('{"type": "text", "part": {}}')
        assert event == {"type": "text", "part": {}}

    def test_parse_json_event_invalid(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime._parse_json_event("not json") is None
        assert runtime._parse_json_event("42") is None
        assert runtime._parse_json_event('"string"') is None

    def test_extract_session_id(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        assert runtime._extract_event_session_id({"sessionID": "sess-1"}) == "sess-1"
        assert runtime._extract_event_session_id({"type": "text"}) is None
        assert runtime._extract_event_session_id({"sessionID": ""}) is None

    def test_convert_text_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "text",
            "sessionID": "sess-1",
            "part": {"type": "text", "text": "Hello there!"},
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].type == "assistant"
        assert messages[0].content == "Hello there!"

    def test_convert_tool_use_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "tool_use",
            "sessionID": "sess-1",
            "part": {
                "tool": "bash",
                "state": {
                    "input": {"command": "ls -la"},
                    "status": "completed",
                    "output": "total 42",
                },
            },
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].tool_name == "Bash"
        assert "ls -la" in messages[0].content

    def test_convert_error_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "error",
            "sessionID": "sess-1",
            "error": {"name": "AuthError", "data": {"message": "Bad key"}},
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert "Bad key" in messages[0].content

    def test_convert_reasoning_event(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        from ouroboros.orchestrator.opencode_event_normalizer import OpenCodeEventContext

        ctx = OpenCodeEventContext(session_id="sess-1")
        event = {
            "type": "reasoning",
            "sessionID": "sess-1",
            "part": {"type": "reasoning", "text": "Let me think..."},
        }
        messages = runtime._convert_event(event, ctx)
        assert len(messages) == 1
        assert messages[0].data.get("thinking") == "Let me think..."


class TestOpenCodeRuntimeExecuteTask:
    """Test execute_task integration."""

    @pytest.mark.asyncio
    async def test_execute_task_success(self) -> None:
        """Successful task execution streams messages and produces a final result."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-abc",
                "part": {"type": "text", "text": "Task completed successfully."},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp/project")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        assert len(messages) >= 1
        # Should have at least the text message and a final result
        text_msgs = [m for m in messages if m.type == "assistant"]
        [m for m in messages if m.type == "result"]
        assert len(text_msgs) >= 1
        assert "Task completed" in text_msgs[0].content

    @pytest.mark.asyncio
    async def test_execute_task_with_session_tracking(self) -> None:
        """Session ID from events is captured into the runtime handle."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-tracked-123",
                "part": {"type": "text", "text": "Working..."},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        # Find a message with a resume handle that has the session ID
        handles = [m.resume_handle for m in messages if m.resume_handle is not None]
        assert any(h.native_session_id == "sess-tracked-123" for h in handles)

    @pytest.mark.asyncio
    async def test_execute_task_cli_not_found(self) -> None:
        """FileNotFoundError yields a result error message."""
        runtime = OpenCodeRuntime(cli_path="/nonexistent/opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=FileNotFoundError("/nonexistent/opencode"),
        ):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        assert len(messages) == 1
        assert messages[0].type == "result"
        assert messages[0].is_error
        assert "not found" in messages[0].content.lower()

    @pytest.mark.asyncio
    async def test_execute_task_nonzero_exit(self) -> None:
        """Non-zero exit code produces an error result."""
        process = _FakeProcess(
            stdout_lines=[],
            stderr_lines=["Something went wrong"],
            returncode=1,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) == 1
        assert result_msgs[0].data.get("subtype") == "error"
        assert result_msgs[0].data.get("returncode") == 1

    @pytest.mark.asyncio
    async def test_execute_task_error_event_is_final(self) -> None:
        """Error events in the stream are treated as final results."""
        error_event = json.dumps(
            {
                "type": "error",
                "sessionID": "sess-1",
                "error": {"name": "Crash", "data": {"message": "Internal error"}},
            }
        )

        process = _FakeProcess(
            stdout_lines=[error_event],
            stderr_lines=[],
            returncode=1,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        error_msgs = [m for m in messages if m.type == "result" and m.is_error]
        assert len(error_msgs) >= 1
        assert "Internal error" in error_msgs[0].content

    @pytest.mark.asyncio
    async def test_execute_task_to_result_success(self) -> None:
        """execute_task_to_result collects messages into a TaskResult."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Done!"},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        with patch("asyncio.create_subprocess_exec", return_value=process):
            result = await runtime.execute_task_to_result("Do it")

        assert result.is_ok
        assert result.value.success is True
        assert len(result.value.messages) >= 1

    @pytest.mark.asyncio
    async def test_execute_task_multiple_events(self) -> None:
        """Multiple events in the stream are all converted."""
        events = [
            json.dumps(
                {
                    "type": "tool_use",
                    "sessionID": "sess-1",
                    "part": {
                        "tool": "read",
                        "state": {
                            "input": {"filePath": "README.md"},
                            "status": "completed",
                            "output": "# Hello",
                        },
                    },
                }
            ),
            json.dumps(
                {
                    "type": "text",
                    "sessionID": "sess-1",
                    "part": {"type": "text", "text": "I read the README."},
                }
            ),
        ]

        process = _FakeProcess(
            stdout_lines=events,
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Read README"):
                messages.append(msg)

        tool_msgs = [m for m in messages if m.tool_name is not None]
        [m for m in messages if m.type == "assistant" and m.tool_name is None]
        assert len(tool_msgs) >= 1
        assert tool_msgs[0].tool_name == "Read"


class TestResolveDispatchTemplates:
    """Test ``$1`` / ``$CWD`` template expansion in skill-intercept args."""

    def test_string_dollar_one_replaced(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        result = runtime._resolve_dispatch_templates("$1", first_argument="seed.yaml")
        assert result == "seed.yaml"

    def test_string_dollar_cwd_replaced(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        result = runtime._resolve_dispatch_templates("$CWD", first_argument=None)
        assert result == "/project"

    def test_string_dollar_one_none_becomes_empty(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        result = runtime._resolve_dispatch_templates("$1", first_argument=None)
        assert result == ""

    def test_plain_string_unchanged(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        result = runtime._resolve_dispatch_templates("hello", first_argument="x")
        assert result == "hello"

    def test_dict_recursive(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        result = runtime._resolve_dispatch_templates(
            {"seed_path": "$1", "cwd": "$CWD", "keep": "value"},
            first_argument="my.yaml",
        )
        assert result == {"seed_path": "my.yaml", "cwd": "/project", "keep": "value"}

    def test_list_recursive(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        result = runtime._resolve_dispatch_templates(
            ["$1", "$CWD", "literal"],
            first_argument="arg",
        )
        assert result == ["arg", "/project", "literal"]

    def test_nested_dict_in_list(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        result = runtime._resolve_dispatch_templates(
            [{"path": "$1"}],
            first_argument="file.txt",
        )
        assert result == [{"path": "file.txt"}]

    def test_non_string_passthrough(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/project")
        assert runtime._resolve_dispatch_templates(42, first_argument="x") == 42
        assert runtime._resolve_dispatch_templates(None, first_argument="x") is None
        assert runtime._resolve_dispatch_templates(True, first_argument="x") is True


class TestExitCodeDeterminesSuccess:
    """Runtime trusts the process exit code as the authoritative signal.

    Tool-level errors during the run are *not* latched — if the process
    exits 0 the runtime reports success, matching the Codex runtime
    pattern.
    """

    @pytest.mark.asyncio
    async def test_tool_error_with_zero_exit_reports_success(self) -> None:
        """A tool_use with state.error + returncode=0 must still be success.

        The exit code is the ground truth for subprocess runtimes.
        Intermediate tool errors are expected when agents self-correct.
        """
        tool_error_event = json.dumps(
            {
                "type": "tool_use",
                "sessionID": "sess-err",
                "part": {
                    "tool": "bash",
                    "state": {
                        "input": {"command": "bad-cmd"},
                        "status": "completed",
                        "output": "command not found",
                        "error": "command not found",
                    },
                },
            }
        )

        process = _FakeProcess(
            stdout_lines=[tool_error_event],
            stderr_lines=[],
            returncode=0,  # exit code 0 — agent decided it succeeded
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Run bad-cmd"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert final.data.get("subtype") == "success", (
            "Exit code 0 must produce success regardless of tool errors"
        )
        assert final.data.get("returncode") == 0

    @pytest.mark.asyncio
    async def test_clean_exit_without_tool_error_reports_success(self) -> None:
        """No tool errors + returncode=0 must produce subtype 'success'."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-ok",
                "part": {"type": "text", "text": "All done."},
            }
        )

        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=[],
            returncode=0,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something safe"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert final.data.get("subtype") == "success"

    @pytest.mark.asyncio
    async def test_nonzero_exit_with_tool_error_reports_error(self) -> None:
        """Exit code != 0 produces error regardless of stream content."""
        tool_error_event = json.dumps(
            {
                "type": "tool_use",
                "sessionID": "sess-r",
                "part": {
                    "tool": "bash",
                    "state": {
                        "input": {"command": "bad-cmd"},
                        "status": "completed",
                        "output": "",
                        "error": "command not found",
                    },
                },
            }
        )

        process = _FakeProcess(
            stdout_lines=[tool_error_event],
            stderr_lines=["fatal error"],
            returncode=1,
        )

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert final.data.get("subtype") == "error"
        assert final.data.get("returncode") == 1

    """Prompt must be piped via stdin, not argv."""

    @pytest.mark.asyncio
    async def test_prompt_written_to_stdin(self) -> None:
        """execute_task must write the composed prompt to the process stdin."""
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Done."},
            }
        )
        process = _FakeProcess(stdout_lines=[text_event], stderr_lines=[], returncode=0)
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        captured_process = None

        async def _fake_exec(*args, **kwargs):
            nonlocal captured_process
            captured_process = process
            return process

        with patch("asyncio.create_subprocess_exec", side_effect=_fake_exec) as mock_exec:
            messages = []
            async for msg in runtime.execute_task("Hello big prompt"):
                messages.append(msg)

            # Prompt must NOT appear in argv
            call_args = mock_exec.call_args[0]
            assert "Hello big prompt" not in call_args

        # Prompt must have been written to stdin
        assert captured_process is not None
        assert b"Hello big prompt" in captured_process.stdin.written
        assert captured_process.stdin.closed

    def test_build_command_excludes_prompt(self) -> None:
        """_build_command must not include the prompt in argv."""
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        cmd = runtime._build_command(prompt="This should not appear")
        assert "This should not appear" not in cmd

    @pytest.mark.asyncio
    async def test_stdin_broken_pipe_yields_error_result(self) -> None:
        """BrokenPipeError on stdin write must not crash — falls through to stderr."""

        class _BrokenStdin(_FakeStdin):
            def write(self, data: bytes) -> None:
                raise BrokenPipeError("opencode exited early")

        process = _FakeProcess(
            stdout_lines=[],
            stderr_lines=["opencode: invalid argument"],
            returncode=1,
        )
        process.stdin = _BrokenStdin()

        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Hello"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        # Should have fallen through to normal stderr reporting
        assert final.data.get("subtype") == "error"
        assert "invalid argument" in final.content


class TestStderrPriorityOnFailure:
    """On non-zero exit, stderr should win over stale last_content."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_prefers_stderr(self) -> None:
        """When process exits non-zero, stderr should be the final message."""
        # Emit a text event (stale content), then exit with error + stderr
        text_event = json.dumps(
            {
                "type": "text",
                "sessionID": "sess-1",
                "part": {"type": "text", "text": "Calling tool: Bash..."},
            }
        )
        process = _FakeProcess(
            stdout_lines=[text_event],
            stderr_lines=["FATAL: segfault in plugin"],
            returncode=1,
        )
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")

        messages: list[AgentMessage] = []
        with patch("asyncio.create_subprocess_exec", return_value=process):
            async for msg in runtime.execute_task("Do something"):
                messages.append(msg)

        result_msgs = [m for m in messages if m.type == "result"]
        assert len(result_msgs) >= 1
        final = result_msgs[-1]
        assert "segfault" in final.content, (
            "On non-zero exit, stderr must win over stale last_content"
        )
        assert "Calling tool" not in final.content


class TestOpenCodeRuntimeChildEnv:
    """Test child environment construction."""

    def test_strips_ouroboros_vars(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with patch.dict(
            "os.environ",
            {
                "OUROBOROS_AGENT_RUNTIME": "opencode",
                "OUROBOROS_LLM_BACKEND": "opencode",
            },
        ):
            env = runtime._build_child_env()
        assert "OUROBOROS_AGENT_RUNTIME" not in env
        assert "OUROBOROS_LLM_BACKEND" not in env

    def test_increments_depth(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "2"}):
            env = runtime._build_child_env()
        assert env["_OUROBOROS_DEPTH"] == "3"

    def test_depth_guard(self) -> None:
        runtime = OpenCodeRuntime(cli_path="opencode", cwd="/tmp")
        with patch.dict("os.environ", {"_OUROBOROS_DEPTH": "5"}):
            with pytest.raises(RuntimeError, match="Maximum Ouroboros nesting depth"):
                runtime._build_child_env()
