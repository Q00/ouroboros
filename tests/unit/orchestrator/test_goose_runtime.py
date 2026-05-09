"""Unit tests for GooseCliRuntime."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.goose_runtime import GooseCliRuntime


class _FakeStream:
    def __init__(self, lines: list[str]) -> None:
        self._buffer = bytearray("".join(f"{line}\n" for line in lines).encode())

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


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], returncode: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream([])
        self._returncode = returncode
        self.pid = 12345

    @property
    def returncode(self) -> int | None:
        return self._returncode

    async def wait(self) -> int:
        return self._returncode


def test_goose_command_uses_run_stream_json_and_stdin() -> None:
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    command = runtime._build_command("/tmp/out.txt", resume_session_id="session-1")

    assert command[:4] == ["/tmp/goose", "run", "--output-format", "stream-json"]
    assert "--resume" in command
    assert command[-2:] == ["-i", "-"]
    assert "session-1" in command


@pytest.mark.asyncio
async def test_goose_runtime_collects_stream_json_messages() -> None:
    stdout = [
        json.dumps({"type": "session.started", "session_id": "session-1"}),
        json.dumps({"type": "assistant.message", "text": "Working"}),
        json.dumps({"type": "tool.call", "tool_name": "Bash", "input": {"command": "echo hi"}}),
        json.dumps({"type": "completed", "text": "Done"}),
    ]
    fake_process = _FakeProcess(stdout)

    async def fake_exec(*args: object, **kwargs: object) -> _FakeProcess:
        return fake_process

    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="auto")

    with (
        patch(
            "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ) as mock_exec,
        patch.object(runtime, "_maybe_dispatch_skill_intercept", return_value=None),
    ):
        messages = [m async for m in runtime.execute_task("do it", tools=["Bash"])]

    assert mock_exec.call_args.args[:4] == ("/tmp/goose", "run", "--output-format", "stream-json")
    assert b"do it" in fake_process.stdin.written
    assert any(m.type == "system" and m.resume_handle for m in messages)
    assert any(m.content == "Working" for m in messages)
    assert any(m.tool_name == "Bash" for m in messages)
    assert messages[-1].is_final
    assert messages[-1].content == "Done"


def test_goose_child_env_sets_nested_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "goose")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "goose")
    runtime = GooseCliRuntime(cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="approve")

    env = runtime._build_child_env()

    assert env["_OUROBOROS_NESTED"] == "1"
    assert env["GOOSE_MODE"] == "approve"
    assert env["GOOSE_WORKING_DIR"] == "/tmp/project"
    assert "OUROBOROS_AGENT_RUNTIME" not in env
    assert "OUROBOROS_LLM_BACKEND" not in env
