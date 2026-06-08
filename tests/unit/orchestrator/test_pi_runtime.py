"""Unit tests for PiRuntime."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import AgentMessage, ParamSupport, RuntimeHandle
from ouroboros.orchestrator.pi_runtime import PiRuntime


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


class _FakeProcess:
    def __init__(
        self, stdout_lines: list[str], stderr_lines: list[str], returncode: int = 0
    ) -> None:
        self.stdin = None
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._returncode = returncode
        self.returncode = None
        self.pid = 1234
        self.terminated = False

    async def wait(self) -> int:
        self.returncode = self._returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._returncode

    def kill(self) -> None:
        self.returncode = self._returncode


def _jsonl_event(event: dict[str, object]) -> str:
    return json.dumps(event)


def test_build_command_uses_documented_json_prompt_argument() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project", model="fast")

    command = runtime._build_command(prompt="Do the task", resume_session_id="sess_123-OK")

    assert command == [
        "/tmp/pi",
        "--mode",
        "json",
        "--model",
        "fast",
        "--session",
        "sess_123-OK",
        "Do the task",
    ]


def test_tracks_requested_permission_mode_and_declares_ignored_support() -> None:
    default_runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")
    requested_runtime = PiRuntime(
        cli_path="/tmp/pi",
        cwd="/tmp/project",
        permission_mode="acceptEdits",
    )

    assert default_runtime.permission_mode is None
    assert default_runtime.permission_mode_requested is False
    assert requested_runtime.permission_mode == "acceptEdits"
    assert requested_runtime.permission_mode_requested is True
    assert requested_runtime.capabilities.system_prompt_support is ParamSupport.TRANSLATED
    assert requested_runtime.capabilities.tool_restriction_support is ParamSupport.TRANSLATED
    assert requested_runtime.capabilities.permission_mode_support is ParamSupport.IGNORED


def test_build_command_rejects_unsafe_resume_session_id() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with pytest.raises(ValueError, match="Invalid resume_session_id"):
        runtime._build_command(prompt="Do it", resume_session_id="../../bad")


def test_extract_content_delta_reads_documented_assistant_message_event() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    delta = runtime._extract_content_delta(
        {
            "type": "message_update",
            "message": {"role": "assistant", "content": []},
            "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
        }
    )

    assert delta == "Hello"


def test_extract_content_delta_ignores_documented_text_end_event() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    delta = runtime._extract_content_delta(
        {
            "type": "message_update",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            "assistantMessageEvent": {"type": "text_end", "content": "Hello"},
        }
    )

    assert delta is None


def test_extract_final_content_reads_agent_end_messages() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    content = runtime._extract_final_content(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "request"},
                {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
            ],
        }
    )

    assert content == "Done."


def test_extract_error_content_reads_agent_end_stop_reason_error() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    content = runtime._extract_error_content(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "request"},
                {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": "OpenAI API error (401)",
                },
            ],
        }
    )

    assert content == "OpenAI API error (401)"


def test_build_runtime_handle_from_session_header() -> None:
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project", permission_mode="acceptEdits")

    sid = runtime._extract_session_id({"type": "session", "id": "session-1"})
    handle = runtime._build_runtime_handle(sid)

    assert handle is not None
    assert handle.backend == "pi"
    assert handle.kind == "agent_runtime"
    assert handle.native_session_id == "session-1"
    assert handle.cwd == "/tmp/project"
    assert handle.approval_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_execute_task_dispatches_ooo_skill_before_spawning_pi() -> None:
    captured: dict[str, Any] = {}
    dispatched_handle = RuntimeHandle(backend="pi", native_session_id="skill-session")

    async def skill_dispatcher(intercept: Any, current_handle: RuntimeHandle | None):
        captured["skill_name"] = intercept.skill_name
        captured["command_prefix"] = intercept.command_prefix
        captured["current_handle"] = current_handle
        return (
            AgentMessage(
                type="tool",
                content="Calling tool: ouroboros_start_auto",
                tool_name=intercept.mcp_tool,
                data={"command_prefix": intercept.command_prefix},
                resume_handle=dispatched_handle,
            ),
            AgentMessage(
                type="result",
                content="auto started",
                data={"subtype": "success"},
                resume_handle=dispatched_handle,
            ),
        )

    runtime = PiRuntime(
        cli_path="/tmp/pi",
        cwd="/tmp/project",
        skill_dispatcher=skill_dispatcher,
    )

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        messages = [msg async for msg in runtime.execute_task("ooo auto Build docs")]

    mock_exec.assert_not_called()
    assert captured["skill_name"] == "auto"
    assert captured["command_prefix"] == "ooo auto"
    assert [message.content for message in messages] == [
        "Calling tool: ouroboros_start_auto",
        "auto started",
    ]
    assert messages[-1].resume_handle == dispatched_handle


def test_pi_runtime_accepts_stream_timeout_overrides() -> None:
    runtime = PiRuntime(
        cli_path="/tmp/pi",
        cwd="/tmp/project",
        startup_output_timeout_seconds=0,
        stdout_idle_timeout_seconds=0,
    )

    assert runtime._startup_output_timeout_seconds is None
    assert runtime._stdout_idle_timeout_seconds is None


@pytest.mark.asyncio
async def test_execute_task_streams_delta_and_final_result() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "message_update",
                    "message": {"role": "assistant", "content": []},
                    "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"},
                }
            ),
            _jsonl_event(
                {
                    "type": "message_update",
                    "message": {"role": "assistant", "content": []},
                    "assistantMessageEvent": {"type": "text_delta", "delta": "lo"},
                }
            ),
            _jsonl_event(
                {
                    "type": "message_update",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Hello"}],
                    },
                    "assistantMessageEvent": {"type": "text_end", "content": "Hello"},
                }
            ),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}
                    ],
                }
            ),
        ],
        stderr_lines=[
            "Extension loading...",
            "Extension loaded: /loop, /loop-stop, /loop-list, /loop-stop-all",
        ],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert mock_exec.call_args.args == ("/tmp/pi", "--mode", "json", "Do it")
    assert mock_exec.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert [m.content for m in messages if m.type == "assistant"] == ["Hel", "lo"]
    result = [m for m in messages if m.type == "result"][-1]
    assert result.content == "Hello"
    assert result.data == {"subtype": "success", "returncode": 0}
    assert result.resume_handle is not None
    assert result.resume_handle.native_session_id == "session-1"


@pytest.mark.asyncio
async def test_agent_end_does_not_mask_nonzero_exit() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "assistant", "content": [{"type": "text", "text": "Looks done"}]}
                    ],
                }
            )
        ],
        stderr_lines=["pi failed"],
        returncode=7,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error
    assert result.content == "pi failed"
    assert result.data["subtype"] == "error"
    assert result.data["returncode"] == 7
    assert result.data["error_type"] == "PiError"


@pytest.mark.asyncio
async def test_agent_stop_reason_error_overrides_zero_exit() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            _jsonl_event(
                {
                    "type": "agent_end",
                    "messages": [
                        {"role": "user", "content": [{"type": "text", "text": "request"}]},
                        {
                            "role": "assistant",
                            "content": [],
                            "stopReason": "error",
                            "errorMessage": "OpenAI API error (401)",
                        },
                    ],
                }
            ),
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error
    assert result.content == "OpenAI API error (401)"
    assert result.data == {
        "subtype": "error",
        "returncode": 0,
        "error_type": "PiError",
    }
    assert result.resume_handle is not None
    assert result.resume_handle.native_session_id == "session-1"


@pytest.mark.asyncio
async def test_execute_task_reports_malformed_json_event() -> None:
    process = _FakeProcess(
        stdout_lines=[
            _jsonl_event({"type": "session", "id": "session-1"}),
            "not-json",
        ],
        stderr_lines=[],
        returncode=0,
    )
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    result = [m for m in messages if m.type == "result"][-1]
    assert result.is_error
    assert result.content == "Malformed Pi JSON event: not-json"
    assert result.data == {
        "subtype": "error",
        "error_type": "MalformedPiEvent",
    }
    assert result.resume_handle is not None
    assert result.resume_handle.native_session_id == "session-1"
    assert process.terminated


@pytest.mark.asyncio
async def test_execute_task_to_result_maps_malformed_event_to_provider_error() -> None:
    process = _FakeProcess(stdout_lines=["[bad-json]"], stderr_lines=[], returncode=0)
    runtime = PiRuntime(cli_path="/tmp/pi", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        result = await runtime.execute_task_to_result("Do it")

    assert result.is_err
    error = result.error
    assert error is not None
    assert error.provider == "pi"
    assert error.message == "Malformed Pi JSON event: [bad-json]"
    assert error.details == {"messages": ["Malformed Pi JSON event: [bad-json]"]}
    assert process.terminated


def test_runtime_factory_constructs_pi_runtime() -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    runtime = create_agent_runtime(backend="pi", cli_path="/tmp/pi", cwd="/tmp/project")

    assert isinstance(runtime, PiRuntime)
    assert runtime.runtime_backend == "pi"
    assert runtime.working_directory == "/tmp/project"


def test_runtime_factory_passes_pi_stream_timeout_overrides() -> None:
    from ouroboros.orchestrator.runtime_factory import create_agent_runtime

    with patch(
        "ouroboros.orchestrator.runtime_factory.create_codex_command_dispatcher",
        return_value=object(),
    ):
        runtime = create_agent_runtime(
            backend="pi",
            cli_path="/tmp/pi",
            cwd="/tmp/project",
            startup_output_timeout_seconds=0,
            stdout_idle_timeout_seconds=0,
        )

    assert isinstance(runtime, PiRuntime)
    assert runtime._startup_output_timeout_seconds is None
    assert runtime._stdout_idle_timeout_seconds is None


def test_runtime_handle_accepts_pi_backend() -> None:
    handle = RuntimeHandle(backend="pi_cli", native_session_id="session-1")

    assert handle.backend == "pi"
