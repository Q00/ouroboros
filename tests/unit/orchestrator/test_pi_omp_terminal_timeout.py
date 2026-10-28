"""Native completion does not disable Pi-family subprocess exit deadlines."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.omp_runtime import OmpRuntime
from ouroboros.orchestrator.pi_runtime import PiRuntime


class _Stream:
    def __init__(self, data: bytes = b"", *, blocked: bool = False) -> None:
        self.data = data
        self.blocked = blocked
        self.closed = asyncio.Event()

    async def read(self, n: int = -1) -> bytes:
        if self.blocked:
            await self.closed.wait()
        data, self.data = self.data, b""
        return data


class _Process:
    def __init__(self, *, blocked_exit: bool) -> None:
        self.stdout = _Stream(
            json.dumps(
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": "Done"}],
                }
            ).encode()
            + b"\n"
        )
        self.stderr = _Stream(blocked=not blocked_exit)
        self.returncode = None if blocked_exit else 0
        self.exited = asyncio.Event()
        self.terminated = False
        if not blocked_exit:
            self.exited.set()

    async def wait(self) -> int:
        await self.exited.wait()
        return self.returncode or 0

    async def terminate(self, process: object) -> None:
        assert process is self
        self.terminated = True
        self.returncode = -15
        self.exited.set()
        self.stderr.closed.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_type", [PiRuntime, OmpRuntime])
@pytest.mark.parametrize("blocked_exit", [False, True], ids=["open-stderr", "hung-exit"])
async def test_terminal_event_bounds_exit_and_stderr(runtime_type, blocked_exit, tmp_path) -> None:
    with patch("ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags", return_value=None):
        runtime = runtime_type(cli_path="/unused/pi", cwd=tmp_path)
    runtime._process_shutdown_timeout_seconds = 0.01
    process = _Process(blocked_exit=blocked_exit)
    with (
        patch("asyncio.create_subprocess_exec", return_value=process),
        patch.object(runtime, "_terminate_process", side_effect=process.terminate),
    ):
        result = await asyncio.wait_for(runtime.execute_task_to_result("Do the task"), timeout=0.5)
    assert process.terminated
    assert result.is_err
    assert "exit" in str(result.error).lower()
