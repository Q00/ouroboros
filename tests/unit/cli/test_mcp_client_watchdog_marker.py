"""Regression: the stdio orphan watchdog must not kill a live client (#1699).

``/proc/stat``'s ``btime`` is boot time — a constant — but WSL2 re-derives it
from a clock that resyncs, so successive reads drift upward. Any identity built
as ``btime + starttime`` therefore *moves* for an unchanged pid, and the
watchdog's tolerance eventually trips: a healthy client is declared gone and the
serve loop is cancelled.

The watchdog now compares a boot-relative marker taken straight from
``/proc/<pid>/stat`` field 22, which never moves. The epoch-based
``heartbeat.process_start_time`` is deliberately left alone because it also
defines the *cross-process* identity persisted in lease payloads, the PID
registry and detached-job ownership — caching it would make an older observer
disagree with a later worker's persisted value.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys

import pytest

from ouroboros.cli.commands.mcp import _client_is_alive, _process_start_marker
from ouroboros.orchestrator import heartbeat

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="procfs marker is Linux-only")


def _drifting_btime(monkeypatch, step: int = 1):
    """Make every /proc/stat read report a btime one step later than the last."""
    drift = iter(range(0, 1000, step))
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/proc/stat":
            return f"btime {1_700_000_000 + next(drift)}\n"
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)


def test_marker_does_not_move_while_btime_drifts(monkeypatch):
    """The watchdog marker is immune to btime drift."""
    _drifting_btime(monkeypatch)

    pid = os.getpid()
    first = _process_start_marker(pid)
    assert first is not None

    for _ in range(20):
        assert _process_start_marker(pid) == first


def test_live_client_stays_alive_while_btime_drifts(monkeypatch):
    """The watchdog keeps reporting a live client as alive (the #1699 failure)."""
    _drifting_btime(monkeypatch)

    pid = os.getpid()
    marker = _process_start_marker(pid)
    assert marker is not None

    for _ in range(20):
        assert _client_is_alive(pid, marker) is True


def test_dead_pid_is_still_reported_dead():
    """Drift immunity must not weaken the liveness check itself."""
    # Reap a real child so the pid is genuinely gone rather than a zombie.
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child never returns
        os._exit(0)
    marker = _process_start_marker(pid)
    os.waitpid(pid, 0)

    assert _client_is_alive(pid, marker) is False


def test_marker_parses_comm_containing_spaces_and_parens(monkeypatch):
    """Field 22 must be read relative to the LAST ')', not a naive split()."""
    ticks = 4242
    # fields 3..22: state, then 18 filler fields, then starttime.
    tail = " ".join(["S", *[str(i) for i in range(4, 22)], str(ticks)])
    stat_line = f"1234 (my (weird) proc) {tail} 0 0 0\n"
    real_read_text = Path.read_text

    def fake_read_text(self, *args, **kwargs):
        if str(self) == "/proc/1234/stat":
            return stat_line
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert _process_start_marker(1234) == pytest.approx(ticks / os.sysconf("SC_CLK_TCK"))


def test_concurrent_marker_reads_agree(monkeypatch):
    """First access is not cached, so concurrent callers cannot disagree."""
    _drifting_btime(monkeypatch)

    pid = os.getpid()
    with ThreadPoolExecutor(max_workers=8) as pool:
        markers = list(pool.map(lambda _: _process_start_marker(pid), range(64)))

    assert len(set(markers)) == 1


def test_cross_process_epoch_identity_is_left_uncached(monkeypatch):
    """The persisted cross-process identity must keep reading btime fresh.

    An older observer computing a *cached* boot time would disagree with the
    ``owner_start_time`` a later worker persisted, and would terminalize live
    work. This test fails the moment someone caches btime in the shared helper.
    """
    _drifting_btime(monkeypatch)

    pid = os.getpid()
    observed = {heartbeat.process_start_time(pid) for _ in range(5)}

    assert len(observed) > 1
