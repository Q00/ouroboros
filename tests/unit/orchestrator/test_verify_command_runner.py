"""The verify runner executes only through a resolved real Bash."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shlex
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator import verify_command_runner
from ouroboros.orchestrator.verify_command_runner import (
    _run_process,
    _spawn_kwargs,
    _terminate,
    _WindowsJob,
    run_with_shell,
)
from ouroboros.orchestrator.verify_shell import resolve_verify_shell


def _route_or_skip():
    route = resolve_verify_shell()
    if route is None:
        pytest.skip("real Bash is not available on this test machine")
    return route


@pytest.mark.asyncio
async def test_run_with_shell_reports_status_and_combined_output(tmp_path: Path) -> None:
    route = _route_or_skip()

    run = await run_with_shell(
        route.argv("printf READY; printf ERROR >&2; exit 7"),
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=30,
    )

    assert run.returncode == 7
    assert run.output == "READYERROR"
    assert run.start_error is None
    assert run.timed_out is False


@pytest.mark.asyncio
async def test_run_with_shell_reports_timeout(tmp_path: Path) -> None:
    route = _route_or_skip()

    run = await run_with_shell(
        route.argv("sleep 5"),
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=0.01,
    )

    assert run.timed_out is True
    assert run.start_error is None


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX process groups")
@pytest.mark.asyncio
async def test_posix_cancellation_kills_and_reaps_process_group(tmp_path: Path) -> None:
    route = _route_or_skip()
    started = tmp_path / "started.txt"
    escaped = tmp_path / "escaped.txt"
    command = (
        f"printf started > {shlex.quote(str(started))}; "
        f"sleep 0.4; printf escaped > {shlex.quote(str(escaped))}"
    )
    task = asyncio.create_task(
        run_with_shell(
            route.argv(command),
            cwd=str(tmp_path),
            env=dict(os.environ),
            timeout_seconds=30,
        )
    )
    for _ in range(100):
        if started.exists():
            break
        await asyncio.sleep(0.01)
    assert started.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.5)

    assert not escaped.exists()


@pytest.mark.asyncio
async def test_run_with_shell_reports_start_error(tmp_path: Path) -> None:
    run = await run_with_shell(
        (str(tmp_path / "missing-bash"), "-c", "exit 0"),
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=30,
    )

    assert run.start_error is not None
    assert run.timed_out is False


def test_windows_spawn_uses_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(verify_command_runner, "_running_on_windows", lambda: True)

    assert _spawn_kwargs()["creationflags"] & 0x00000004


@pytest.mark.asyncio
async def test_windows_assigns_job_before_resuming_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_command_runner, "_running_on_windows", lambda: True)
    order: list[str] = []
    process = SimpleNamespace(
        pid=321,
        communicate=AsyncMock(return_value=(b"", None)),
        returncode=0,
        wait=AsyncMock(),
        kill=MagicMock(),
    )
    monkeypatch.setattr(
        verify_command_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=process),
    )
    job = _WindowsJob(handle=object(), close_handle=lambda _handle: order.append("close"))
    monkeypatch.setattr(
        verify_command_runner,
        "_create_windows_job",
        lambda _pid: order.append("assign") or job,
    )
    monkeypatch.setattr(
        verify_command_runner,
        "_resume_windows_process",
        lambda _pid: order.append("resume"),
    )

    run = await _run_process(("bash.exe", "-c", "exit 0"), cwd=".", env={}, timeout_seconds=1)

    assert run.returncode == 0
    assert order == ["assign", "resume", "close"]


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows process containment")
@pytest.mark.asyncio
async def test_windows_timeout_kills_immediate_background_child(tmp_path: Path) -> None:
    route = _route_or_skip()
    marker = tmp_path / "escaped-child.txt"
    code = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).write_text('escaped')"
    command = f'{sys.executable!r} -c "{code}" & sleep 5'

    run = await run_with_shell(
        route.argv(command),
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=0.05,
    )
    await asyncio.sleep(1.25)

    assert run.timed_out is True
    assert not marker.exists()


@pytest.mark.asyncio
async def test_windows_unassigned_process_kills_only_suspended_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_command_runner, "_running_on_windows", lambda: True)
    process = SimpleNamespace(pid=321, wait=AsyncMock(), kill=MagicMock())

    await _terminate(process)

    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_windows_job_close_terminates_tree_without_taskkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_command_runner, "_running_on_windows", lambda: True)
    create = AsyncMock()
    monkeypatch.setattr(verify_command_runner.asyncio, "create_subprocess_exec", create)
    close = MagicMock()
    job = _WindowsJob(handle=object(), close_handle=close)
    process = SimpleNamespace(pid=321, wait=AsyncMock(), kill=MagicMock())

    await _terminate(process, job)

    close.assert_called_once()
    create.assert_not_awaited()
    process.wait.assert_awaited_once()
