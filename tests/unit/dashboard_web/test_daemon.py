"""Singleton-daemon election primitives — filesystem/lock logic (no real spawn)."""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
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
        daemon.write_state(host="127.0.0.1", port=12345, pid=999, db_path="/tmp/one.db")
        state = daemon.read_state()
        assert state is not None
        assert state["port"] == 12345
        assert state["pid"] == 999
        assert state["host"] == "127.0.0.1"
        assert state["db_path"] == "/tmp/one.db"

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
        daemon.write_state(host="127.0.0.1", port=9, pid=1, db_path="/tmp/one.db")
        assert daemon._state_alive(daemon.read_state()) is False


class TestDbIdentity:
    """A live daemon for a DIFFERENT EventStore must never be reused."""

    @staticmethod
    def _make_events_db(path, execution_id: str) -> None:
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
            conn.execute(
                "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
                (
                    f"orch_{execution_id}",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": execution_id}),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _get_runs(port: int) -> list[dict]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        try:
            conn.request("GET", "/api/runs")
            body = conn.getresponse().read()
        finally:
            conn.close()
        return json.loads(body)["runs"]

    def test_ensure_spawns_fresh_daemon_for_a_different_db(self, tmp_path, monkeypatch) -> None:
        from ouroboros.dashboard_web.server import serve_background

        one_db = tmp_path / "one.db"
        two_db = tmp_path / "two.db"
        self._make_events_db(one_db, "exec_one")
        self._make_events_db(two_db, "exec_two")

        # In-process stand-in for the detached daemon: real HTTP server (healthz,
        # /api/runs) + the same state publish run_daemon performs.
        servers = []

        def _fake_spawn(*, db_path: str, host: str) -> None:
            port = daemon._free_port(host)
            server, _thread = serve_background(db_path=db_path, host=host, port=port)
            servers.append(server)
            daemon.write_state(host=host, port=port, pid=os.getpid(), db_path=db_path)

        monkeypatch.setattr(daemon, "_spawn_detached", _fake_spawn)
        try:
            first = daemon.ensure_dashboard(db_path=str(one_db))
            assert first is not None
            assert first.reused is False

            # Same DB again → reuse (singleton behaviour unchanged).
            again = daemon.ensure_dashboard(db_path=str(one_db))
            assert again is not None
            assert again.reused is True
            assert again.port == first.port

            # DIFFERENT DB → must NOT reuse: fresh daemon, new port.
            second = daemon.ensure_dashboard(db_path=str(two_db))
            assert second is not None
            assert second.reused is False
            assert second.port != first.port

            # The new daemon actually serves two.db's runs.
            runs = self._get_runs(second.port)
            ids = {r["execution_id"] for r in runs}
            assert ids == {"exec_two"}

            # State now records two.db — one.db's daemon was replaced, not reused.
            state = daemon.read_state()
            assert state is not None
            assert state["db_path"] == daemon._resolve_db(str(two_db))
        finally:
            for server in servers:
                server.shutdown()


class TestPendingRun:
    """The base URL is opened before any run exists (auto flow); the server must
    serve an empty run list AND the polling page without breaking the contract."""

    @staticmethod
    def _get(port: int, path: str) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    def test_empty_runs_and_polling_page_are_served(self, tmp_path) -> None:
        from ouroboros.dashboard_web.server import serve_background

        # A DB that exists but holds no runs yet — exactly the pending-auto state.
        empty_db = tmp_path / "empty.db"
        conn = sqlite3.connect(empty_db)
        try:
            conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
            conn.commit()
        finally:
            conn.close()

        server, _thread = serve_background(
            db_path=str(empty_db), host="127.0.0.1", port=daemon._free_port("127.0.0.1")
        )
        try:
            port = server.server_address[1]
            # /api/runs answers 200 with an empty list — no error, no hang.
            status, body = self._get(port, "/api/runs")
            assert status == 200
            assert json.loads(body) == {"runs": []}

            # The index page keeps polling instead of dead-ending on the empty list.
            status, body = self._get(port, "/")
            assert status == 200
            page = body.decode("utf-8")
            assert "waiting for run" in page
            assert "while (!runId)" in page
            assert "no active run" not in page
        finally:
            server.shutdown()
