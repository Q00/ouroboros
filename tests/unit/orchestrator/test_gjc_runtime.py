"""Unit tests for GjcRuntime."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.adapter import RuntimeHandle
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
    def __init__(
        self,
        stdout_lines: list[str],
        stderr_lines: list[str] | None = None,
        returncode: int = 0,
        *,
        never_stdout: bool = False,
    ) -> None:
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
    runtime = GjcRuntime(
        cli_path="/tmp/gjc", cwd="/tmp/project", startup_output_timeout_seconds=0.01
    )

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
@pytest.mark.parametrize(
    "frame_type",
    ["workflow_gate", "host_tool_call", "host_uri_request", "extension_ui_request", "mystery"],
)
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
    process = _FakeProcess(
        [
            _event({"type": "ready"}),
            _event({"type": "response", "id": "wrong", "success": False, "error": "no"}),
        ]
    )
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
            proc.stdout._buffer.extend(
                (
                    _event(
                        {
                            "type": "response",
                            "id": payload["id"],
                            "command": "prompt",
                            "success": False,
                            "error": "denied",
                        }
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
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
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        [
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": True,
                                }
                            ),
                            _event(
                                {
                                    "type": "message_update",
                                    "assistantMessageEvent": {"type": "text_delta", "delta": "Hel"},
                                }
                            ),
                            _event(
                                {
                                    "type": "agent_end",
                                    "messages": [
                                        {
                                            "role": "assistant",
                                            "content": [{"type": "text", "text": "Hello"}],
                                        }
                                    ],
                                }
                            ),
                        ]
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process) as mock_exec,
    ):
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
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        [
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": True,
                                }
                            ),
                            _event(
                                {
                                    "type": "message_update",
                                    "assistantMessageEvent": {"type": "text_delta", "delta": "x"},
                                }
                            ),
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": False,
                                    "error": "late bad",
                                }
                            ),
                        ]
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
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
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        [
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": True,
                                }
                            ),
                            _event(
                                {
                                    "type": "agent_end",
                                    "messages": [
                                        {
                                            "role": "assistant",
                                            "content": [],
                                            "stopReason": "error",
                                            "errorMessage": "OpenAI API error (401)",
                                        }
                                    ],
                                }
                            ),
                        ]
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
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
            proc.stdout._buffer.extend(
                (
                    _event(
                        {
                            "type": "response",
                            "id": payload["id"],
                            "command": "prompt",
                            "success": True,
                        }
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].content == "Malformed GJC JSON event: not-json"
    assert messages[-1].data["error_type"] == "MalformedGjcEvent"
    assert process.terminated


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame_type",
    [
        "workflow_gate",
        "extension_ui_request",
        "host_tool_call",
        "host_tool_cancel",
        "host_uri_request",
        "host_uri_cancel",
        "mystery",
    ],
)
async def test_unsupported_frames_raise_unsupported_and_terminate(frame_type: str) -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        [
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": True,
                                }
                            ),
                            _event({"type": frame_type, "id": "frame-1", "gate_id": "gate-1"}),
                        ]
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
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
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        [
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": True,
                                }
                            ),
                            _event(
                                {
                                    "type": "agent_end",
                                    "messages": [{"role": "assistant", "content": "done"}],
                                }
                            ),
                        ]
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
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
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        [
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": True,
                                }
                            ),
                            _event(
                                {"type": "tool_execution_start", "id": "tool-1", "name": "read"}
                            ),
                            _event({"type": "tool_execution_end", "id": "tool-1", "success": True}),
                            _event(
                                {
                                    "type": "agent_end",
                                    "messages": [{"role": "assistant", "content": "done"}],
                                }
                            ),
                        ]
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
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
            proc.stdout._buffer.extend(
                (
                    _event(
                        {
                            "type": "response",
                            "id": payload["id"],
                            "command": "set_model",
                            "success": True,
                        }
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
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
            proc.stdout._buffer.extend(
                (_event({"type": "host_tool_call", "id": "tool-1"}) + "\n").encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
        messages = [msg async for msg in runtime.execute_task("Do it")]

    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "UnsupportedGjcRpcFrame"


@pytest.mark.asyncio
async def test_unsupported_frame_during_set_model_ack_phase_is_unsupported() -> None:
    process = _FakeProcess(
        [_event({"type": "ready"}), _event({"type": "host_tool_call", "id": "tool-1"})]
    )
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


class _ChunkStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int = -1) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _ChunkProcess(_FakeProcess):
    def __init__(
        self, chunks: list[bytes], stderr_lines: list[str] | None = None, returncode: int = 0
    ) -> None:
        super().__init__([], stderr_lines=stderr_lines, returncode=returncode)
        self.stdout = _ChunkStream(chunks)


async def _run_with_appended_events(
    events: list[dict[str, Any]],
    *,
    returncode: int = 0,
    stderr: list[str] | None = None,
    model: str | None = None,
) -> tuple[list[Any], _FakeProcess]:
    process = _FakeProcess([_event({"type": "ready"})], stderr_lines=stderr, returncode=returncode)
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project", model=model)
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        _event(
                            {**event, "id": payload["id"]}
                            if event.get("id") == "$prompt"
                            else event
                        )
                        for event in events
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
        messages = [msg async for msg in runtime.execute_task("Do it")]
    return messages, process


@pytest.mark.asyncio
async def test_huge_non_ready_preamble_before_ready_terminates() -> None:
    process = _FakeProcess(["x" * 10000, _event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "MalformedGjcEvent"
    assert process.terminated


@pytest.mark.asyncio
async def test_partial_split_jsonl_and_multibyte_utf8_boundary_preserves_delta() -> None:
    ready = (_event({"type": "ready"}) + "\n").encode()
    delta = "A🙂B"
    process = _ChunkProcess([ready])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")

    async def send_fixed(proc: Any, payload: dict[str, Any]) -> None:
        await GjcRuntime._send_command(runtime, proc, payload)
        body = (
            "\n".join(
                [
                    _event(
                        {
                            "type": "response",
                            "id": payload["id"],
                            "command": "prompt",
                            "success": True,
                        }
                    ),
                    _event(
                        {
                            "type": "message_update",
                            "assistantMessageEvent": {"type": "text_delta", "delta": delta},
                        }
                    ),
                    _event(
                        {"type": "agent_end", "messages": [{"role": "assistant", "content": delta}]}
                    ),
                ]
            )
            + "\n"
        ).encode()
        split = body.index(b"\\ud83d") + 3
        proc.stdout._chunks.extend([body[:7], body[7:split], body[split:]])

    with (
        patch.object(runtime, "_send_command", side_effect=send_fixed),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
        messages = [msg async for msg in runtime.execute_task("Do it")]
    assert [m.content for m in messages if m.type == "assistant"] == [delta]
    assert messages[-1].content == delta
    assert not messages[-1].is_error


@pytest.mark.asyncio
async def test_oversized_single_line_is_provider_error_and_terminates() -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    process.stdout._buffer.extend(b"x" * (50 * 1024 * 1024 + 1))
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    with patch("asyncio.create_subprocess_exec", return_value=process):
        messages = [msg async for msg in runtime.execute_task("Do it")]
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "ProviderError"
    assert process.terminated


@pytest.mark.asyncio
async def test_unrelated_id_supported_frame_post_ack_protocol_error_and_terminates() -> None:
    messages, process = await _run_with_appended_events(
        [
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
            {
                "type": "message_update",
                "id": "other",
                "assistantMessageEvent": {"type": "text_delta", "delta": "x"},
            },
        ]
    )
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "GjcProtocolError"
    assert process.terminated


@pytest.mark.asyncio
async def test_prompt_ack_after_agent_end_is_protocol_error() -> None:
    messages, _ = await _run_with_appended_events(
        [
            {"type": "agent_end", "messages": [{"role": "assistant", "content": "done"}]},
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
        ]
    )
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "GjcProtocolError"


@pytest.mark.asyncio
async def test_duplicate_agent_end_first_wins_closes_stdin_and_succeeds() -> None:
    messages, process = await _run_with_appended_events(
        [
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
            {"type": "agent_end", "messages": [{"role": "assistant", "content": "first"}]},
            {"type": "agent_end", "messages": [{"role": "assistant", "content": "second"}]},
        ]
    )
    assert messages[-1].content == "first"
    assert messages[-1].data["subtype"] == "success"
    assert process.stdin.closed


@pytest.mark.asyncio
async def test_assistant_stop_reason_error_mid_stream_is_error_not_success() -> None:
    messages, _ = await _run_with_appended_events(
        [
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
            {
                "type": "message_end",
                "message": {"role": "assistant", "stopReason": "error", "errorMessage": "bad"},
            },
            {"type": "agent_end", "messages": [{"role": "assistant", "content": "done"}]},
        ]
    )
    assert messages[-1].is_error
    assert messages[-1].content == "bad"
    assert messages[-1].data["error_type"] == "ProviderError"


@pytest.mark.asyncio
async def test_premature_eof_nonzero_precedes_protocol_eof_and_clean_eof_protocol_error() -> None:
    for returncode, error_type in [(9, "GjcExitError"), (0, "GjcProtocolError")]:
        process = _FakeProcess(
            [_event({"type": "ready"})], stderr_lines=["boom"], returncode=returncode
        )
        runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
        original_send = runtime._send_command

        async def send_and_eof(proc: Any, payload: dict[str, Any]) -> None:
            await original_send(proc, payload)
            if payload["type"] == "prompt":
                proc.stdout._buffer.extend(
                    (
                        _event(
                            {
                                "type": "response",
                                "id": payload["id"],
                                "command": "prompt",
                                "success": True,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                proc.stdin_eof.set()

        with (
            patch.object(runtime, "_send_command", side_effect=send_and_eof),
            patch("asyncio.create_subprocess_exec", return_value=process),
        ):
            messages = [msg async for msg in runtime.execute_task("Do it")]
        assert messages[-1].is_error
        assert messages[-1].data["error_type"] == error_type


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame_type",
    [
        "workflow_gate",
        "extension_ui_request",
        "host_tool_call",
        "host_tool_cancel",
        "host_uri_request",
        "host_uri_cancel",
        "unknown",
    ],
)
@pytest.mark.parametrize("position", ["first", "mid", "last"])
async def test_unsupported_frame_first_mid_last_for_bidirectional_types(
    frame_type: str, position: str
) -> None:
    unsupported = {"type": frame_type, "id": "bad"}
    if position == "first":
        events = [unsupported]
    elif position == "mid":
        events = [
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
            unsupported,
        ]
    else:
        events = [
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": "x"},
            },
            unsupported,
        ]
    messages, process = await _run_with_appended_events(events)
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "UnsupportedGjcRpcFrame"
    assert process.terminated


@pytest.mark.asyncio
async def test_passive_lifecycle_flood_then_agent_end_succeeds() -> None:
    events = [{"type": "response", "id": "$prompt", "command": "prompt", "success": True}]
    events.extend({"type": "tool_execution_start"} for _ in range(2000))
    events.append({"type": "agent_end", "messages": [{"role": "assistant", "content": "done"}]})
    messages, _ = await _run_with_appended_events(events)
    assert messages[-1].content == "done"
    assert messages[-1].data["subtype"] == "success"


@pytest.mark.asyncio
async def test_unicode_emoji_ansi_control_chars_in_deltas_preserved_exactly() -> None:
    delta = "hi 🙂 \x1b[31mred\x1b[0m\n\t\x00end"
    messages, _ = await _run_with_appended_events(
        [
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_delta", "delta": delta},
            },
            {"type": "agent_end", "messages": [{"role": "assistant", "content": delta}]},
        ]
    )
    assert [m.content for m in messages if m.type == "assistant"] == [delta]
    assert messages[-1].content == delta


@pytest.mark.asyncio
async def test_set_model_wrong_command_and_failed_ack_errors() -> None:
    for ack, error_type in [
        (
            {"type": "response", "id": "set", "command": "prompt", "success": True},
            "GjcProtocolError",
        ),
        (
            {
                "type": "response",
                "id": "set",
                "command": "set_model",
                "success": False,
                "error": "bad model",
            },
            "GjcCommandError",
        ),
    ]:
        process = _FakeProcess([_event({"type": "ready"})])
        runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project", model="openai/gpt")
        original_send = runtime._send_command

        async def send_set_model_ack(proc: Any, payload: dict[str, Any]) -> None:
            await original_send(proc, payload)
            if payload["type"] == "set_model":
                proc.stdout._buffer.extend((_event({**ack, "id": payload["id"]}) + "\n").encode())

        with (
            patch.object(runtime, "_send_command", side_effect=send_set_model_ack),
            patch("asyncio.create_subprocess_exec", return_value=process),
        ):
            messages = [msg async for msg in runtime.execute_task("Do it")]
        assert messages[-1].is_error
        assert messages[-1].data["error_type"] == error_type


@pytest.mark.asyncio
async def test_resume_handle_ignored_logs_and_returns_no_resume_handle() -> None:
    messages, _ = await _run_with_appended_events(
        [
            {"type": "response", "id": "$prompt", "command": "prompt", "success": True},
            {"type": "agent_end", "messages": [{"role": "assistant", "content": "done"}]},
        ]
    )
    assert messages[-1].resume_handle is None


def _child_env_script(tmp_path: Any, body: str) -> str:
    path = tmp_path / "gjc_child.py"
    path.write_text(textwrap.dedent(body))
    return str(path)


@pytest.mark.asyncio
async def test_resume_handle_argument_is_ignored_and_logs(caplog: Any) -> None:
    process = _FakeProcess([_event({"type": "ready"})])
    runtime = GjcRuntime(cli_path="/tmp/gjc", cwd="/tmp/project")
    original_send = runtime._send_command

    async def send_and_append(proc: Any, payload: dict[str, Any]) -> None:
        await original_send(proc, payload)
        if payload["type"] == "prompt":
            proc.stdout._buffer.extend(
                (
                    "\n".join(
                        [
                            _event(
                                {
                                    "type": "response",
                                    "id": payload["id"],
                                    "command": "prompt",
                                    "success": True,
                                }
                            ),
                            _event(
                                {
                                    "type": "agent_end",
                                    "messages": [{"role": "assistant", "content": "done"}],
                                }
                            ),
                        ]
                    )
                    + "\n"
                ).encode()
            )

    with (
        patch.object(runtime, "_send_command", side_effect=send_and_append),
        patch("asyncio.create_subprocess_exec", return_value=process),
    ):
        messages = [
            msg
            async for msg in runtime.execute_task(
                "Do it", resume_handle=RuntimeHandle(backend="pi")
            )
        ]
    assert messages[-1].resume_handle is None
    assert process.stdin.closed


@pytest.mark.asyncio
async def test_child_agent_end_then_ignores_stdin_eof_is_bounded_error_and_killed(
    tmp_path: Any,
) -> None:
    script = _child_env_script(
        tmp_path,
        """
        import json, sys, time
        print(json.dumps({"type":"ready"}), flush=True)
        prompt = json.loads(sys.stdin.readline())
        print(json.dumps({"type":"response","id":prompt["id"],"command":"prompt","success":True}), flush=True)
        print(json.dumps({"type":"agent_end","messages":[{"role":"assistant","content":"done"}]}), flush=True)
        time.sleep(60)
    """,
    )
    runtime = GjcRuntime(cli_path=sys.executable, cwd=str(tmp_path))
    runtime._process_shutdown_timeout_seconds = 0.05
    with patch.object(runtime, "_build_command", return_value=[sys.executable, script]):
        messages = [msg async for msg in runtime.execute_task("Do it")]
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "GjcExitError"


@pytest.mark.asyncio
async def test_child_nonzero_exit_after_agent_end_is_exit_error(tmp_path: Any) -> None:
    script = _child_env_script(
        tmp_path,
        """
        import json, sys
        print(json.dumps({"type":"ready"}), flush=True)
        prompt = json.loads(sys.stdin.readline())
        print(json.dumps({"type":"response","id":prompt["id"],"command":"prompt","success":True}), flush=True)
        print(json.dumps({"type":"agent_end","messages":[{"role":"assistant","content":"done"}]}), flush=True)
        print("boom", file=sys.stderr, flush=True)
        sys.exit(7)
    """,
    )
    runtime = GjcRuntime(cli_path=sys.executable, cwd=str(tmp_path))
    with patch.object(runtime, "_build_command", return_value=[sys.executable, script]):
        messages = [msg async for msg in runtime.execute_task("Do it")]
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "GjcExitError"
    assert messages[-1].data["returncode"] == 7
    assert messages[-1].content == "boom"


@pytest.mark.asyncio
async def test_child_sigkill_mid_stream_is_exit_error_with_signal_returncode(tmp_path: Any) -> None:
    script = _child_env_script(
        tmp_path,
        """
        import json, os, signal, sys
        print(json.dumps({"type":"ready"}), flush=True)
        prompt = json.loads(sys.stdin.readline())
        print(json.dumps({"type":"response","id":prompt["id"],"command":"prompt","success":True}), flush=True)
        print("killed", file=sys.stderr, flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    """,
    )
    runtime = GjcRuntime(cli_path=sys.executable, cwd=str(tmp_path))
    with patch.object(runtime, "_build_command", return_value=[sys.executable, script]):
        messages = [msg async for msg in runtime.execute_task("Do it")]
    assert messages[-1].is_error
    assert messages[-1].data["error_type"] == "GjcExitError"
    assert messages[-1].data["returncode"] == -signal.SIGKILL


@pytest.mark.asyncio
async def test_asyncio_cancellation_terminates_child(tmp_path: Any) -> None:
    marker = tmp_path / "child.pid"
    script = _child_env_script(
        tmp_path,
        f"""
        import json, os, pathlib, sys, time
        pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))
        print(json.dumps({{"type":"ready"}}), flush=True)
        sys.stdin.readline()
        time.sleep(60)
    """,
    )
    runtime = GjcRuntime(cli_path=sys.executable, cwd=str(tmp_path))
    runtime._process_shutdown_timeout_seconds = 0.1

    async def consume() -> None:
        with patch.object(runtime, "_build_command", return_value=[sys.executable, script]):
            async for _ in runtime.execute_task("Do it"):
                pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
