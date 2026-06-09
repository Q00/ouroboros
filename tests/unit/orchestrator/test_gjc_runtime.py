"""Unit tests for GjcRuntime."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.gjc_runtime import GjcRuntime


class _FakeStream:
    def __init__(self, lines: list[str], *, never: bool = False) -> None:
        self._never = never
        encoded = "".join(f"{line}\n" for line in lines).encode()
        self._buffer = bytearray(encoded)

    async def read(self, n: int = -1) -> bytes:
        if self._never:
            await asyncio.sleep(3600)
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
    def __init__(self, process: _FakeProcess) -> None:
        self._process = process
        self.writes: list[dict[str, Any]] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(json.loads(data.decode()))

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True
        self._process.stdin_eof.set()


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_lines: list[str] | None = None, returncode: int = 0, *, never_stdout: bool = False) -> None:
        self.stdin_eof = asyncio.Event()
        self.stdin = _FakeStdin(self)
        self.stdout = _FakeStream(stdout_lines, never=never_stdout)
        self.stderr = _FakeStream(stderr_lines or [])
        self._returncode = returncode
        self.returncode = None
        self.terminated = False
        self.pid = 1234

    async def wait(self) -> int:
        await self.stdin_eof.wait()
        self.returncode = self._returncode
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = self._returncode
        self.stdin_eof.set()

    def kill(self) -> None:
        self.returncode = self._returncode
        self.stdin_eof.set()


def _event(event: dict[str, object]) -> str:
    return json.dumps(event)


@pytest.mark.asyncio
async def test_missing_ready_times_out_and_terminates() -> None:
    process = _FakeProcess([], never_stdout=True)
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project", startup_output_timeout_seconds=0.01)

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    result = messages[-1]
    assert result.is_error
    assert result.data["error_type"] == "TimeoutError"
    assert process.terminated
    assert process.stdin.closed


@pytest.mark.asyncio
async def test_non_ready_before_ready_is_protocol_error() -> None:
    process = _FakeProcess([_event({"type": "agent_start"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "GjcProtocolError"
    assert process.terminated

@pytest.mark.asyncio
@pytest.mark.parametrize("frame_type", ["workflow_gate", "host_tool_call", "host_uri_request", "extension_ui_request", "mystery"])
async def test_unsupported_first_frame_raises_unsupported_and_terminates(frame_type: str) -> None:
    process = _FakeProcess([_event({"type": frame_type, "id": "frame-1"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "UnsupportedGjcRpcFrame"
    assert frame_type in messages[-1].content
    assert process.terminated


@pytest.mark.asyncio
async def test_prompt_success_false_is_command_error() -> None:
    process = _FakeProcess([
        _event({"type": "ready"}),
        _event({"type": "response", "id": "wrong", "success": False, "error": "no"}),
    ])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    # A wrong id before prompt ack is a strict protocol failure.
    assert messages[-1].data["error_type"] == "GjcProtocolError"
    assert process.terminated


@pytest.mark.asyncio
async def test_prompt_same_id_success_false_is_command_error() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend((_event({"type": "response", "id": payload["id"], "command": "prompt", "success": False, "error": "denied"}) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].content == "denied"
    assert messages[-1].data["error_type"] == "GjcCommandError"
    assert process.terminated


@pytest.mark.asyncio
async def test_ack_message_update_agent_end_success_and_stdin_pipe_closed() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(("\n".join([
                _event({"type": "response", "id": payload["id"], "command": "prompt", "success": True}),
                _event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"}}),
                _event({"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Hello"}]}]}),
            ]) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec:
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert mock_exec.call_args.args == ("/tmp/gjc", "--mode", "rpc")
    assert mock_exec.call_args.kwargs["stdin"] == asyncio.subprocess.PIPE
    assert mock_exec.call_args.kwargs["stdin"] != asyncio.subprocess.DEVNULL
    assert [m.content for m in messages if m.type == "assistant"] == ["Hel"]
    assert messages[-1].content == "Hello"
    assert messages[-1].data == {"subtype": "success", "returncode": 0}
    assert messages[-1].resume_handle is None
    assert process.stdin.closed


@pytest.mark.asyncio
async def test_late_same_id_success_false_is_error() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(("\n".join([
                _event({"type": "response", "id": payload["id"], "command": "prompt", "success": True}),
                _event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}}),
                _event({"type": "response", "id": payload["id"], "command": "prompt", "success": False, "error": "late bad"}),
            ]) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].content == "late bad"
    assert messages[-1].data["error_type"] == "GjcCommandError"
    assert process.terminated


@pytest.mark.asyncio
async def test_assistant_stop_reason_error_with_zero_exit_is_runtime_error() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(("\n".join([
                _event({"type": "response", "id": payload["id"], "command": "prompt", "success": True}),
                _event({"type": "agent_end", "messages": [{"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "OpenAI API error (401)"}]}),
            ]) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].content == "OpenAI API error (401)"
    assert messages[-1].data["error_type"] == "ProviderError"


@pytest.mark.asyncio
async def test_malformed_json_is_malformed_gjc_event() -> None:
    process = _FakeProcess([_event({"type": "ready"}), "not-json"])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend((_event({"type": "response", "id": payload["id"], "command": "prompt", "success": True}) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].content == "Malformed GJC JSON event: not-json"
    assert messages[-1].data["error_type"] == "MalformedGjcEvent"
    assert process.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize("frame_type", ["workflow_gate", "extension_ui_request", "host_tool_call", "host_tool_cancel", "host_uri_request", "host_uri_cancel", "mystery"])
async def test_unsupported_frames_raise_unsupported_and_terminate(frame_type: str) -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(("\n".join([
                _event({"type": "response", "id": payload["id"], "command": "prompt", "success": True}),
                _event({"type": frame_type, "id": "frame-1", "gate_id": "gate-1"}),
            ]) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "UnsupportedGjcRpcFrame"
    assert frame_type in messages[-1].content
    assert process.terminated


@pytest.mark.asyncio
async def test_nonzero_exit_is_gjc_exit_error() -> None:
    process = _FakeProcess([_event({"type": "ready"})], stderr_lines=["boom"], returncode=7)
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(("\n".join([
                _event({"type": "response", "id": payload["id"], "command": "prompt", "success": True}),
                _event({"type": "agent_end", "messages": [{"role": "assistant", "content": "done"}]}),
            ]) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].content == "boom"
    assert messages[-1].data == {"subtype": "error", "error_type": "GjcExitError", "returncode": 7}



@pytest.mark.asyncio
async def test_tool_lifecycle_events_are_ignored_and_stream_succeeds() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(("\n".join([
                _event({"type": "response", "id": payload["id"], "command": "prompt", "success": True}),
                _event({"type": "tool_execution_start", "id": "tool-1", "name": "read"}),
                _event({"type": "tool_execution_end", "id": "tool-1", "success": True}),
                _event({"type": "agent_end", "messages": [{"role": "assistant", "content": "done"}]}),
            ]) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].content == "done"
    assert messages[-1].data == {"subtype": "success", "returncode": 0}
    assert process.stdin.closed


@pytest.mark.asyncio
async def test_wrong_command_prompt_ack_is_protocol_error() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend((_event({"type": "response", "id": payload["id"], "command": "set_model", "success": True}) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "GjcProtocolError"


@pytest.mark.asyncio
async def test_unsupported_frame_during_prompt_ack_phase_is_unsupported() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend((_event({"type": "host_tool_call", "id": "tool-1"}) + "\n").encode())

    with patch.object(runtime, "_send_command", side_effect=send_and_append), patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "UnsupportedGjcRpcFrame"


@pytest.mark.asyncio
async def test_unsupported_frame_during_set_model_ack_phase_is_unsupported() -> None:
    process = _FakeProcess([_event({"type": "ready"}), _event({"type": "host_tool_call", "id": "tool-1"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project", model="openai/gpt-4.1")

    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "UnsupportedGjcRpcFrame"

def test_capabilities_are_non_resumable_structured_skill_dispatch() -> None:
    caps = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project").capabilities

    assert caps.skill_dispatch is True
    assert caps.targeted_resume is False
    assert caps.structured_output is True
