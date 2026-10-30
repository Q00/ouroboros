"""Safety regressions for the test-owned detached-process observer."""

from __future__ import annotations

import ast
import ctypes
import json
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


def _receipt_publisher(receipt: Path, record: dict, clock: SimpleNamespace):
    """Exercise the exact embedded writer without launching its process tree."""
    main = next(
        node
        for node in ast.parse(probes._LAUNCH_PARENT).body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main"
    )
    publish = next(
        node for node in main.body if isinstance(node, ast.FunctionDef) and node.name == "publish"
    )
    namespace = {"receipt": receipt, "record": record, "json": json, "time": clock}
    exec(compile(ast.Module(body=[publish], type_ignores=[]), "<probe-publish>", "exec"), namespace)
    return namespace["publish"]


def _publication_clock() -> SimpleNamespace:
    clock = SimpleNamespace(now=0.0, sleeps=[])

    def sleep(seconds):
        assert 0 < seconds <= 0.01
        clock.sleeps.append(seconds)
        clock.now += seconds

    clock.monotonic = lambda: clock.now
    clock.sleep = sleep
    return clock


def test_receipt_publication_retries_transient_permission_error(tmp_path, monkeypatch) -> None:
    receipt = tmp_path / "parent.json"
    previous = b'{"ready": false}'
    receipt.write_bytes(previous)
    record = {"ready": True, "job_id": "job_publication_probe"}
    replace = Path.replace
    attempts = 0

    def replace_after_reader_releases(path, target):
        nonlocal attempts
        attempts += 1
        assert receipt.read_bytes() == previous
        assert json.loads(path.read_text(encoding="utf-8")) == record
        if attempts == 1:
            raise PermissionError("reader holds destination")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", replace_after_reader_releases)
    clock = _publication_clock()
    _receipt_publisher(receipt, record, clock)()
    assert attempts == 2
    assert clock.sleeps == [0.01]
    assert json.loads(receipt.read_text(encoding="utf-8")) == record
    assert not receipt.with_suffix(".tmp").exists()


def test_receipt_publication_permission_failure_has_bounded_retry(tmp_path, monkeypatch) -> None:
    receipt = tmp_path / "parent.json"
    previous = b'{"ready": false}'
    receipt.write_bytes(previous)
    clock = _publication_clock()
    attempts = 0

    def blocked_replace(path, target):
        nonlocal attempts
        attempts += 1
        assert target == receipt
        raise PermissionError("persistent destination denial")

    monkeypatch.setattr(Path, "replace", blocked_replace)
    with pytest.raises(PermissionError, match="persistent destination denial"):
        _receipt_publisher(receipt, {"ready": True}, clock)()
    assert attempts > 1
    assert clock.now == pytest.approx(2.0)
    assert receipt.read_bytes() == previous
    assert json.loads(receipt.with_suffix(".tmp").read_text(encoding="utf-8")) == {"ready": True}


@pytest.mark.parametrize("error", [FileNotFoundError, OSError])
def test_receipt_publication_does_not_retry_other_replace_errors(
    tmp_path, monkeypatch, error
) -> None:
    receipt = tmp_path / "parent.json"
    previous = b'{"ready": false}'
    receipt.write_bytes(previous)
    replace = MagicMock(side_effect=error("publication failed"))
    monkeypatch.setattr(Path, "replace", replace)
    clock = _publication_clock()
    with pytest.raises(error, match="publication failed"):
        _receipt_publisher(receipt, {"ready": True}, clock)()
    replace.assert_called_once_with(receipt)
    assert clock.sleeps == []
    assert receipt.read_bytes() == previous


def test_receipt_publication_invalid_record_still_fails_before_replacement(tmp_path, monkeypatch):
    receipt = tmp_path / "parent.json"
    previous = b'{"ready": false}'
    receipt.write_bytes(previous)
    replace = MagicMock()
    monkeypatch.setattr(Path, "replace", replace)
    clock = _publication_clock()
    with pytest.raises(TypeError, match="not JSON serializable"):
        _receipt_publisher(receipt, {"invalid": object()}, clock)()
    replace.assert_not_called()
    assert clock.sleeps == []
    assert receipt.read_bytes() == previous


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
