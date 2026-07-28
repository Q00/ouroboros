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


def _fake_stat_bytes(monkeypatch, pid: int, payload: bytes):
    """Serve raw bytes for one /proc/<pid>/stat, leaving other reads alone."""
    real_read_bytes = Path.read_bytes

    def fake_read_bytes(self, *args, **kwargs):
        if str(self) == f"/proc/{pid}/stat":
            return payload
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)


def _stat_payload(comm: bytes, ticks: int) -> bytes:
    """A /proc/<pid>/stat line whose field 22 is ``ticks``."""
    tail = b" ".join([b"S", *[str(i).encode() for i in range(4, 22)], str(ticks).encode()])
    return b"1234 (" + comm + b") " + tail + b" 0 0 0\n"


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
    _fake_stat_bytes(monkeypatch, 1234, _stat_payload(b"my (weird) proc", ticks))

    assert _process_start_marker(1234) == pytest.approx(ticks / os.sysconf("SC_CLK_TCK"))


def test_marker_survives_non_utf8_process_name(monkeypatch):
    """comm is arbitrary non-NUL bytes; decoding it would abort server startup.

    read_text() raises UnicodeDecodeError — not an OSError — on a legal name
    like b"bad-\xff-name", and that escapes _resolve_client_identity.
    """
    ticks = 777
    _fake_stat_bytes(monkeypatch, 1234, _stat_payload(b"bad-\xff-name", ticks))

    assert _process_start_marker(1234) == pytest.approx(ticks / os.sysconf("SC_CLK_TCK"))


def test_concurrent_marker_reads_agree(monkeypatch):
    """First access is not cached, so concurrent callers cannot disagree."""
    _drifting_btime(monkeypatch)

    pid = os.getpid()
    with ThreadPoolExecutor(max_workers=8) as pool:
        markers = list(pool.map(lambda _: _process_start_marker(pid), range(64)))

    assert len(set(markers)) == 1
