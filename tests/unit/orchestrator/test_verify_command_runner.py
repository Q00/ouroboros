"""The verify runner executes only through a resolved real Bash."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.orchestrator import verify_command_runner
from ouroboros.orchestrator.verify_command_runner import (
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

    assert "creationflags" in _spawn_kwargs()


@pytest.mark.asyncio
async def test_windows_timeout_terminates_the_full_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_command_runner, "_running_on_windows", lambda: True)
    killer = SimpleNamespace(communicate=AsyncMock(), returncode=0)
    create = AsyncMock(return_value=killer)
    monkeypatch.setattr(verify_command_runner.asyncio, "create_subprocess_exec", create)
    process = SimpleNamespace(pid=321, wait=AsyncMock(), kill=MagicMock())

    await _terminate(process)

    args = create.await_args.args
    assert args[0].endswith(r"System32\taskkill.exe")
    assert args[1:] == ("/PID", "321", "/T", "/F")
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_windows_taskkill_failure_has_bounded_parent_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify_command_runner, "_running_on_windows", lambda: True)
    killer = SimpleNamespace(communicate=AsyncMock(), returncode=1)
    monkeypatch.setattr(
        verify_command_runner.asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=killer),
    )
    process = SimpleNamespace(pid=321, wait=AsyncMock(), kill=MagicMock())

    await _terminate(process)

    process.kill.assert_called_once()
    process.wait.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["startup", "timeout"])
async def test_windows_taskkill_operational_failure_is_bounded(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(verify_command_runner, "_running_on_windows", lambda: True)
    if failure == "startup":
        create = AsyncMock(side_effect=OSError("taskkill unavailable"))
    else:
        killer = SimpleNamespace(
            communicate=AsyncMock(side_effect=TimeoutError),
            returncode=None,
        )
        create = AsyncMock(return_value=killer)
    monkeypatch.setattr(verify_command_runner.asyncio, "create_subprocess_exec", create)
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
