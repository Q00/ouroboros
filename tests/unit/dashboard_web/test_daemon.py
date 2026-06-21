"""Singleton-daemon election primitives — filesystem/lock logic (no real spawn)."""

from __future__ import annotations

import os
import time

import pytest

from ouroboros.dashboard_web import daemon


@pytest.fixture(autouse=True)
def _isolated_paths(tmp_path, monkeypatch):
    home = tmp_path / ".ouroboros"
    monkeypatch.setattr(daemon, "_HOME", home)
    monkeypatch.setattr(daemon, "_STATE_PATH", home / "dashboard.json")
    monkeypatch.setattr(daemon, "_LOCK_PATH", home / "dashboard.lock")
    yield


class TestState:
    def test_write_then_read_roundtrip(self) -> None:
        daemon.write_state(host="127.0.0.1", port=12345, pid=999)
        state = daemon.read_state()
        assert state is not None
        assert state["port"] == 12345
        assert state["pid"] == 999
        assert state["host"] == "127.0.0.1"

    def test_read_missing_state_is_none(self) -> None:
        assert daemon.read_state() is None


class TestLock:
    def test_acquire_is_exclusive_until_released(self) -> None:
        fd = daemon._try_acquire_lock()
        assert fd is not None
        # A second acquire while held (and fresh) is refused.
        assert daemon._try_acquire_lock() is None
        daemon._release_lock(fd)
        # After release, it can be acquired again.
        fd2 = daemon._try_acquire_lock()
        assert fd2 is not None
        daemon._release_lock(fd2)

    def test_stale_lock_is_stolen(self, monkeypatch) -> None:
        fd = daemon._try_acquire_lock()
        assert fd is not None
        os.close(fd)  # leak the lock file (simulate a crashed spawner)
        # Age it past the stale threshold.
        old = time.time() - (daemon._LOCK_STALE_SEC + 5)
        os.utime(daemon._LOCK_PATH, (old, old))
        stolen = daemon._try_acquire_lock()
        assert stolen is not None  # stale lock was reclaimed
        daemon._release_lock(stolen)


class TestEnablement:
    def test_enabled_by_default(self, monkeypatch) -> None:
        monkeypatch.delenv("OUROBOROS_DASHBOARD", raising=False)
        assert daemon.is_enabled() is True

    @pytest.mark.parametrize("value", ["0", "off", "false", "no", "OFF"])
    def test_disabled_via_env(self, monkeypatch, value) -> None:
        monkeypatch.setenv("OUROBOROS_DASHBOARD", value)
        assert daemon.is_enabled() is False

    def test_url_for_run_none_when_disabled(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_DASHBOARD", "0")
        # Must NOT even attempt to ensure/spawn when disabled.
        monkeypatch.setattr(daemon, "ensure_dashboard", lambda **_: pytest.fail("should not spawn"))
        assert daemon.dashboard_url_for_run("exec_x") is None
        assert daemon.dashboard_base_url() is None

    def test_url_for_run_uses_ensured_daemon(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_DASHBOARD", "1")
        info = daemon.DashboardInfo(
            url="http://localhost:9999", host="127.0.0.1", port=9999, pid=42, reused=True
        )
        monkeypatch.setattr(daemon, "ensure_dashboard", lambda **_: info)
        assert daemon.dashboard_url_for_run("exec_abc") == "http://localhost:9999/?run=exec_abc"
        assert daemon.dashboard_base_url() == "http://localhost:9999"

    def test_url_for_run_none_on_ensure_failure(self, monkeypatch) -> None:
        monkeypatch.setenv("OUROBOROS_DASHBOARD", "1")

        def _boom(**_):
            raise RuntimeError("no daemon")

        monkeypatch.setattr(daemon, "ensure_dashboard", _boom)
        assert daemon.dashboard_url_for_run("exec_abc") is None


class TestHealthz:
    def test_healthz_false_when_nothing_listening(self) -> None:
        # Port 1 is privileged/unused — nothing answers.
        assert daemon.healthz("127.0.0.1", 1, timeout=0.2) is False

    def test_state_alive_false_without_server(self) -> None:
        daemon.write_state(host="127.0.0.1", port=9, pid=1)
        assert daemon._state_alive(daemon.read_state()) is False
