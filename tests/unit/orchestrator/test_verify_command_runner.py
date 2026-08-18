"""The verify runner executes only through a resolved real Bash."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ouroboros.orchestrator.verify_command_runner import run_with_shell
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
