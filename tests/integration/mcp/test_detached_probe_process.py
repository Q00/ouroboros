"""Safety regressions for the test-owned detached-process observer."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.mcp.job_manager import JobManager
from ouroboros.persistence.event_store import EventStore
from tests.integration.mcp import detached_probe_process as probes


@pytest.mark.parametrize("matches", [False, True])
def test_windows_handle_requires_original_creation_identity(monkeypatch, matches: bool) -> None:
    kernel = SimpleNamespace(
        OpenProcess=MagicMock(return_value=123),
        WaitForSingleObject=MagicMock(return_value=258),
        TerminateProcess=MagicMock(return_value=True),
        CloseHandle=MagicMock(return_value=True),
        GetProcessTimes=MagicMock(),
    )

    def process_times(_handle, creation, *_times):
        creation._obj.dwLowDateTime = 42
        creation._obj.dwHighDateTime = 0
        return True

    kernel.GetProcessTimes.side_effect = process_times
    monkeypatch.setattr(probes, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(probes, "process_start_time", lambda _pid: None)
    monkeypatch.setattr(
        probes,
        "ctypes",
        SimpleNamespace(
            WinDLL=lambda *_args, **_kwargs: kernel,
            POINTER=ctypes.POINTER,
            byref=ctypes.byref,
        ),
    )
    if matches:
        process = probes.ProbeProcess(456, "42")
        try:
            process.kill()
            kernel.TerminateProcess.assert_called_once_with(123, 1)
        finally:
            process.close()
    else:
        with pytest.raises(AssertionError, match="reused before handle acquisition"):
            probes.ProbeProcess(456, "41")
        kernel.TerminateProcess.assert_not_called()
    kernel.CloseHandle.assert_called_once_with(123)


@pytest.mark.asyncio
async def test_database_discovery_failure_still_reaps_owned_parent(tmp_path, caplog) -> None:
    # The base interpreter is the actual child, without a Windows venv redirector.
    parent = subprocess.Popen(
        [getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(30)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    store.query_events = AsyncMock(side_effect=RuntimeError("database unavailable"))
    probe = probes.DetachedProbe(parent, store, JobManager(store), tmp_path)
    try:
        await probe.cleanup()
        assert parent.poll() is not None
        assert "Cannot discover probe jobs during cleanup" in caplog.text
    finally:
        # Only the original Popen capability may terminate this test's child.
        if parent.poll() is None:
            parent.kill()
        parent.wait(timeout=15)
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("body_fails", [True, False])
async def test_cleanup_error_preserves_body_failure_without_hiding_cleanup_failure(
    tmp_path, monkeypatch, body_fails: bool
) -> None:
    original_cleanup = probes.DetachedProbe.cleanup

    async def failing_cleanup(self):
        await original_cleanup(self)
        assert self.parent.poll() is not None
        assert all(not process.alive() for process in self.processes.values())
        raise RuntimeError("cleanup diagnostic")

    monkeypatch.setattr(probes.DetachedProbe, "cleanup", failing_cleanup)
    expected = ValueError if body_fails else RuntimeError
    message = "original body failure" if body_fails else "cleanup diagnostic"
    with pytest.raises(expected, match=message):
        async with probes.accepting_parent(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'events.db'}",
            cwd=Path.cwd(),
            home=tmp_path / "home",
            delay=2.0,
        ) as probe:
            monkeypatch.setattr(
                probe.store, "query_events", AsyncMock(side_effect=RuntimeError("database failure"))
            )
            monkeypatch.setattr(
                probe.manager, "cancel_job", AsyncMock(side_effect=RuntimeError("cancel failure"))
            )
            if body_fails:
                raise ValueError("original body failure")
    assert probe.parent.poll() is not None
