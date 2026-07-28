"""Regression: a drifting /proc/stat btime must not make a live process look dead.

btime is boot time — a constant — but WSL2 re-derives it from a clock that
resyncs, so successive reads drift upward by seconds. Recomputing a process
start time as ``btime + starttime`` on every call then returns a *moving* value
for a live, unchanged pid, and ``is_process_identity_alive`` eventually exceeds
its 2s tolerance and reports the process dead. See #1699.
"""

from __future__ import annotations

import os
import sys

import pytest

from ouroboros.orchestrator import heartbeat

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="btime/procfs path is Linux-only")


@pytest.fixture(autouse=True)
def _clear_btime_cache():
    heartbeat._BTIME_CACHE = None
    yield
    heartbeat._BTIME_CACHE = None


def test_btime_is_read_once_even_when_proc_stat_drifts(monkeypatch):
    """A drifting btime source must not change a live pid's computed start time."""
    drift = iter(range(0, 100))
    real_read_text = heartbeat.Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/proc/stat":
            return f"btime {1_700_000_000 + next(drift)}\n"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(heartbeat.Path, "read_text", fake_read_text)

    pid = os.getpid()
    first = heartbeat._get_process_start_time(pid)
    assert first is not None

    for _ in range(20):
        assert heartbeat._get_process_start_time(pid) == first


def test_live_process_stays_alive_while_btime_drifts(monkeypatch):
    """The identity check must keep reporting a live pid as alive."""
    drift = iter(range(0, 100))
    real_read_text = heartbeat.Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/proc/stat":
            return f"btime {1_700_000_000 + next(drift)}\n"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(heartbeat.Path, "read_text", fake_read_text)

    pid = os.getpid()
    recorded = heartbeat.process_start_time(pid)
    assert recorded is not None

    for _ in range(20):
        assert heartbeat.is_process_identity_alive(pid, recorded) is True
