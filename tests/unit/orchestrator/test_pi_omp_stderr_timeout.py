"""A stdout timeout must not wait indefinitely for inherited stderr writers."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.omp_runtime import OmpRuntime
from ouroboros.orchestrator.pi_runtime import PiRuntime


class _NeverEOF:
    def __init__(self) -> None:
        self.cancelled = False

    async def read(self, _n: int = -1) -> bytes:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return b""


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = _NeverEOF()
        self.stderr = _NeverEOF()
        self.returncode = None
        self.terminated = False
        self.termination_finished = asyncio.Event()

    async def terminate(self, process: object) -> None:
        assert process is self
        self.returncode = -15
        self.terminated = True
        self.termination_finished.set()


class _FinishedStderr:
    def __init__(self) -> None:
        self.data = b"synthetic diagnostic\n"

    async def read(self, _n: int = -1) -> bytes:
        data, self.data = self.data, b""
        return data


@pytest.mark.parametrize("runtime_type", [PiRuntime, OmpRuntime])
async def test_stdout_timeout_with_unclosed_stderr_returns_error(runtime_type, tmp_path) -> None:
    with patch("ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags", return_value=None):
        runtime = runtime_type(
            cli_path="/unused/pi", cwd=tmp_path, startup_output_timeout_seconds=0.02
        )
    runtime._process_shutdown_timeout_seconds = 0.02
    process = _FakeProcess()
    with (
        patch("asyncio.create_subprocess_exec", return_value=process),
        patch.object(runtime, "_terminate_process", side_effect=process.terminate),
    ):
        result = await asyncio.wait_for(runtime.execute_task_to_result("test"), timeout=0.5)
    assert process.terminated
    assert process.stderr.cancelled
    assert result.is_err
    assert "startup window" in str(result.error)


@pytest.mark.parametrize("runtime_type", [PiRuntime, OmpRuntime])
async def test_stdout_timeout_preserves_completed_stderr(runtime_type, tmp_path) -> None:
    with patch("ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags", return_value=None):
        runtime = runtime_type(
            cli_path="/unused/pi", cwd=tmp_path, startup_output_timeout_seconds=0.02
        )
    runtime._process_shutdown_timeout_seconds = 0.02
    process = _FakeProcess()
    process.stderr = _FinishedStderr()  # type: ignore[assignment]
    with (
        patch("asyncio.create_subprocess_exec", return_value=process),
        patch.object(runtime, "_terminate_process", side_effect=process.terminate),
    ):
        result = await asyncio.wait_for(runtime.execute_task_to_result("test"), timeout=0.5)
    assert result.is_err
    assert "synthetic diagnostic" in str(result.error)


@pytest.mark.parametrize("runtime_type", [PiRuntime, OmpRuntime])
async def test_cancellation_during_stderr_wait_propagates(runtime_type, tmp_path) -> None:
    with patch("ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags", return_value=None):
        runtime = runtime_type(
            cli_path="/unused/pi", cwd=tmp_path, startup_output_timeout_seconds=0.02
        )
    runtime._process_shutdown_timeout_seconds = 1.0
    process = _FakeProcess()
    with (
        patch("asyncio.create_subprocess_exec", return_value=process),
        patch.object(runtime, "_terminate_process", side_effect=process.terminate),
    ):
        task = asyncio.create_task(runtime.execute_task_to_result("test"))
        await asyncio.wait_for(process.termination_finished.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)
    assert process.stderr.cancelled


@pytest.mark.skipif(os.name == "nt", reason="POSIX pipe and process-session test")
@pytest.mark.parametrize("runtime_type", [PiRuntime, OmpRuntime])
async def test_real_child_terminated_with_external_stderr_writer(runtime_type, tmp_path) -> None:
    """A pipe writer outside the worker tree can outlive the killed leader."""
    with patch("ouroboros.orchestrator.pi_runtime._probe_pi_native_param_flags", return_value=None):
        runtime = runtime_type(
            cli_path="/unused/pi", cwd=tmp_path, startup_output_timeout_seconds=0.05
        )
    runtime._process_shutdown_timeout_seconds = 0.1
    read_fd, write_fd = os.pipe()
    read_file = os.fdopen(read_fd, "rb", buffering=0)
    reader = asyncio.StreamReader()
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), read_file
    )
    original_create = asyncio.create_subprocess_exec
    child = None

    async def start_child(*_args, **_kwargs):
        nonlocal child
        child = await original_create(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
            stdout=asyncio.subprocess.PIPE,
            stderr=write_fd,
            start_new_session=True,
        )
        child.stderr = reader
        return child

    try:
        with patch("asyncio.create_subprocess_exec", side_effect=start_child):
            result = await asyncio.wait_for(runtime.execute_task_to_result("test"), timeout=2.0)
        assert result.is_err
        assert "startup window" in str(result.error)
        assert child is not None and child.returncode is not None
    finally:
        os.close(write_fd)
        transport.close()
        if child is not None and child.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                child.kill()
            await child.wait()
