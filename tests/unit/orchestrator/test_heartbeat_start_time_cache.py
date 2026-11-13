"""This process's start time is read from ``ps`` once on macOS, not on every check.

``heartbeat._get_process_start_time`` spawns ``ps -p <pid> -o lstart=`` on
macOS. Owner-liveness checks (``persisted_process_owner_alive``, run on every
``job_wait`` poll) ask about the *current* process, whose start time cannot
change while it runs. ``heartbeat._start_time_of`` reuses that one value:
current PID only, Darwin only, and only once a value was obtained.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
import os
import platform
import subprocess
from typing import Any

import pytest

from ouroboros.orchestrator import heartbeat
from ouroboros.orchestrator.persisted_process_identity import (
    current_persisted_process_owner,
    persisted_process_owner_alive,
)

_C_LSTART = "Mon Nov  9 02:32:10 2026    \n"
_C_LSTART_EPOCH = datetime(2026, 11, 9, 2, 32, 10).timestamp()


class _FakePs:
    """Stand-in for ``subprocess.run`` that answers ``ps -p <pid> -o lstart=``."""

    def __init__(self, outcome: Any = None) -> None:
        self.outcome = (0, _C_LSTART) if outcome is None else outcome
        self.pids: list[int] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "ps", argv
        self.pids.append(int(argv[argv.index("-p") + 1]))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        returncode, stdout = self.outcome
        return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr="")


class _OsWithPid:
    """The real ``os`` module, except that ``getpid`` reports another process."""

    def __init__(self, pid: int) -> None:
        self._pid = pid

    def getpid(self) -> int:
        return self._pid

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(heartbeat, "_CURRENT_PROCESS_START_TIME", None, raising=False)
    yield


def _install(monkeypatch: pytest.MonkeyPatch, system: str, fake: _FakePs) -> _FakePs:
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_repeated_current_owner_checks_spawn_ps_at_most_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps = _install(monkeypatch, "Darwin", _FakePs())

    owner = current_persisted_process_owner()
    alive = [persisted_process_owner_alive(owner) for _ in range(20)]

    assert owner == {"owner_pid": os.getpid(), "owner_start_time": _C_LSTART_EPOCH}
    assert alive == [True] * 20
    assert ps.pids == [os.getpid()]  # one spawn for 1 identity read + 20 checks (was 21)


def test_every_current_process_reader_shares_the_one_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps = _install(monkeypatch, "Darwin", _FakePs())
    pid = os.getpid()

    assert heartbeat.current_process_identity() == (pid, _C_LSTART_EPOCH)
    assert heartbeat.process_start_time(pid) == _C_LSTART_EPOCH
    assert heartbeat.is_process_identity_alive(pid, _C_LSTART_EPOCH) is True
    assert heartbeat.is_process_identity_alive(pid, _C_LSTART_EPOCH + 60) is False

    assert ps.pids == [pid]


def test_other_pid_still_spawns_ps_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    ps = _install(monkeypatch, "Darwin", _FakePs())
    other = os.getppid()

    for _ in range(5):
        assert heartbeat.is_process_identity_alive(other, _C_LSTART_EPOCH) is True
    for _ in range(3):
        assert heartbeat.process_start_time(other) == _C_LSTART_EPOCH

    assert ps.pids == [other] * 8
    assert heartbeat._CURRENT_PROCESS_START_TIME is None


@pytest.mark.parametrize(
    "outcome",
    [
        (1, ""),
        (0, "   \n"),
        (0, "2026년 11월  9일 월요일 02시 32분 10초\n"),
        subprocess.TimeoutExpired(["ps"], 3),
        OSError("ps unavailable"),
    ],
    ids=["nonzero-exit", "empty-output", "unparseable-output", "timeout", "spawn-error"],
)
def test_failed_read_is_not_cached(monkeypatch: pytest.MonkeyPatch, outcome: Any) -> None:
    ps = _install(monkeypatch, "Darwin", _FakePs(outcome))
    pid = os.getpid()

    assert [heartbeat.process_start_time(pid) for _ in range(3)] == [None, None, None]
    assert ps.pids == [pid] * 3
    assert heartbeat._CURRENT_PROCESS_START_TIME is None

    ps.outcome = (0, _C_LSTART)  # once a value is obtained it is reused
    assert [heartbeat.process_start_time(pid) for _ in range(3)] == [_C_LSTART_EPOCH] * 3
    assert ps.pids == [pid] * 4


@pytest.mark.parametrize("system", ["Linux", "Windows", ""])
def test_non_darwin_reads_afresh_every_time(monkeypatch: pytest.MonkeyPatch, system: str) -> None:
    monkeypatch.setattr(platform, "system", lambda: system)
    reads: list[int] = []

    def read(pid: int) -> float:
        reads.append(pid)
        return 1234.5

    monkeypatch.setattr(heartbeat, "_get_process_start_time", read)
    pid = os.getpid()

    assert [heartbeat.process_start_time(pid) for _ in range(5)] == [1234.5] * 5
    assert heartbeat.current_process_identity() == (pid, 1234.5)
    assert reads == [pid] * 6
    assert heartbeat._CURRENT_PROCESS_START_TIME is None


def test_cached_value_is_dropped_when_the_process_id_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A forked child (new PID) must not reuse its parent's start time."""
    ps = _install(monkeypatch, "Darwin", _FakePs())
    parent = os.getpid()
    child = parent + 100_000
    assert heartbeat.process_start_time(parent) == _C_LSTART_EPOCH
    assert (parent, _C_LSTART_EPOCH) == heartbeat._CURRENT_PROCESS_START_TIME

    monkeypatch.setattr(heartbeat, "os", _OsWithPid(child))
    assert heartbeat.process_start_time(parent) == _C_LSTART_EPOCH
    assert heartbeat.process_start_time(parent) == _C_LSTART_EPOCH
    assert heartbeat.process_start_time(child) == _C_LSTART_EPOCH
    assert heartbeat.process_start_time(child) == _C_LSTART_EPOCH

    assert ps.pids == [parent, parent, parent, child]
    assert (child, _C_LSTART_EPOCH) == heartbeat._CURRENT_PROCESS_START_TIME


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
@pytest.mark.parametrize(
    "outcome",
    [
        (0, _C_LSTART),
        (0, "Tue Feb 29 23:59:59 2028\n"),
        (1, ""),
        (0, "2026년 11월  9일 월요일 02시 32분 10초\n"),
        OSError("ps unavailable"),
    ],
    ids=["c-locale", "leap-day", "nonzero-exit", "localized", "spawn-error"],
)
@pytest.mark.parametrize("which", ["current", "other"])
def test_wrapper_returns_what_the_uncached_reader_returns(
    monkeypatch: pytest.MonkeyPatch, system: str, outcome: Any, which: str
) -> None:
    """Equivalence with the previous call target, on first and repeated reads."""
    _install(monkeypatch, system, _FakePs(outcome))
    pid = os.getpid() if which == "current" else os.getppid()

    expected = [heartbeat._get_process_start_time(pid) for _ in range(3)]
    actual = [heartbeat._start_time_of(pid) for _ in range(3)]

    assert actual == expected
