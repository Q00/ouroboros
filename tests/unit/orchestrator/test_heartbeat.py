"""Regression tests for conservative process-identity evidence."""

from __future__ import annotations

import platform

from ouroboros.orchestrator import heartbeat


def test_linux_start_time_parser_handles_spaces_in_process_name(monkeypatch) -> None:
    after_comm = ["S", *("0" for _ in range(18)), "12345"]

    class FakePath:
        def __init__(self, value: str) -> None:
            self.value = value

        def exists(self) -> bool:
            return True

        def read_text(self) -> str:
            if self.value == "/proc/4242/stat":
                return f"4242 (nested client child) {' '.join(after_comm)}"
            return "btime 1000\n"

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr(heartbeat, "Path", FakePath)
    monkeypatch.setattr(heartbeat.os, "sysconf", lambda _name: 100)

    assert heartbeat._get_process_start_time(4242) == 1123.45


def test_start_time_probe_failure_is_unknown_not_dead(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(heartbeat, "_get_process_start_time", lambda _pid: None)

    state = heartbeat.process_identity_state(4242, 1_700_000_000.0)

    assert state is heartbeat.ProcessIdentityState.UNKNOWN
    assert heartbeat.is_process_identity_alive(4242, 1_700_000_000.0) is True


def test_start_time_mismatch_still_proves_pid_reuse(monkeypatch) -> None:
    monkeypatch.setattr(heartbeat.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(heartbeat, "_get_process_start_time", lambda _pid: 1_800_000_000.0)

    assert (
        heartbeat.process_identity_state(4242, 1_700_000_000.0)
        is heartbeat.ProcessIdentityState.DEAD
    )


def test_unknown_session_holder_is_not_released(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(heartbeat, "LOCK_DIR", tmp_path)
    lock = heartbeat.lock_path("orch_unknown")
    lock.write_text("4242:unreadable-start-time", encoding="utf-8")

    assert heartbeat.holder_identity_state("orch_unknown") is heartbeat.ProcessIdentityState.UNKNOWN
    assert heartbeat.is_holder_alive("orch_unknown") is True
    assert lock.exists()
