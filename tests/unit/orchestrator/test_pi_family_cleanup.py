from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import anyio
import pytest

from ouroboros.orchestrator.omp_runtime import OmpRuntime
from ouroboros.orchestrator.pi_runtime import PiRuntime
from ouroboros.orchestrator.runtime_factory import create_agent_runtime


class _Stream:
    def __init__(self, content: bytes = b"", *, block: bool = False) -> None:
        self.content = content
        self.block = block
        self.started = anyio.Event()
        self.finished = anyio.Event()
        self.on_read: Callable[[], None] = lambda: None

    async def read(self, n: int = -1) -> bytes:
        self.started.set()
        self.on_read()
        try:
            if self.block:
                await anyio.sleep_forever()
            content, self.content = self.content, b""
            return content
        finally:
            self.finished.set()


class _Process:
    def __init__(self, stdout: _Stream, *, exit_code: int | None = None) -> None:
        self.stdout = stdout
        self.stderr = _Stream(block=exit_code is None)
        self.returncode = exit_code
        self.terminated = False
        self.killed = False
        self.exited = anyio.Event()
        if exit_code is not None:
            self.exited.set()

    async def wait(self) -> int:
        await self.exited.wait()
        assert self.returncode is not None
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.exited.set()


@pytest.mark.parametrize("backend", ["pi", "omp"])
async def test_cancel_scope_reaps_process_and_stderr_before_next_dispatch(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given a factory-created runtime and a CLI which ignores SIGTERM.
    monkeypatch.setattr(PiRuntime, "_process_shutdown_timeout_seconds", 0.01)
    monkeypatch.setattr(OmpRuntime, "_process_shutdown_timeout_seconds", 0.01)
    with patch(
        "ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags", return_value=(True,) * 3
    ):
        runtime = create_agent_runtime(backend=backend, cli_path="fixture-cli", cwd=tmp_path)
    blocked = _Process(_Stream(block=True))
    succeeding = _Process(_Stream(b'{"type":"agent_end"}\n'), exit_code=0)

    # When cancellation reaches the public dispatch iterator, then retry it.
    with patch(
        f"ouroboros.orchestrator.{backend}_runtime.asyncio.create_subprocess_exec",
        side_effect=[blocked, succeeding],
    ):
        with anyio.CancelScope() as scope:
            blocked.stdout.on_read = scope.cancel
            messages = [message async for message in runtime.execute_task("work")]
        assert scope.cancelled_caught
        assert blocked.terminated
        assert blocked.killed
        assert blocked.returncode == -9
        assert blocked.stderr.finished.is_set()
        messages = [message async for message in runtime.execute_task("retry")]

    # Then cleanup has completed before the same runtime successfully returns.
    assert messages[-1].is_final
    assert not messages[-1].is_error


@pytest.mark.parametrize("backend", ["pi", "omp"])
@pytest.mark.parametrize("ending", ["close", "timeout", "malformed", "success", "exit_error"])
async def test_dispatch_cleanup_preserves_outcome(
    backend: str, ending: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given each supported exit path on the same public factory surface.
    monkeypatch.setattr(PiRuntime, "_process_shutdown_timeout_seconds", 0.01)
    monkeypatch.setattr(OmpRuntime, "_process_shutdown_timeout_seconds", 0.01)
    with patch(
        "ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags", return_value=(True,) * 3
    ):
        runtime = create_agent_runtime(
            backend=backend,
            cli_path="fixture-cli",
            cwd=tmp_path,
            startup_output_timeout_seconds=0.01,
        )
    content = b'{"type":"message_update","delta":"started"}\n'
    if ending == "malformed":
        content = b"invalid-json\n"
    exit_code = {"success": 0, "exit_error": 7}.get(ending)
    process = _Process(_Stream(content, block=ending == "timeout"), exit_code=exit_code)
    if ending == "timeout":
        process.stderr.block = False

    # When dispatch ends or its consumer explicitly closes the generator.
    with patch(
        f"ouroboros.orchestrator.{backend}_runtime.asyncio.create_subprocess_exec",
        return_value=process,
    ):
        stream = runtime.execute_task("work")
        if ending == "close":
            message = await anext(stream)
            assert message.content == "started"
            await process.stderr.started.wait()
            await stream.aclose()
            messages = []
        else:
            messages = [message async for message in stream]

    # Then exit state and failure identity survive cleanup without a reader leak.
    assert process.returncode is not None
    assert process.stderr.finished.is_set()
    if exit_code is None:
        assert process.terminated and process.killed
    if messages:
        assert messages[-1].is_final
        assert messages[-1].is_error == (ending != "success")
        if ending == "timeout":
            assert messages[-1].data["error_type"] == "TimeoutError"
        if ending == "exit_error":
            assert messages[-1].data["returncode"] == 7
