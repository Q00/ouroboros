"""Singleton-daemon election primitives — filesystem/lock logic (no real spawn)."""

from __future__ import annotations

import http.client
import json
import os
import sqlite3
import threading
import time

import pytest

from ouroboros.dashboard_web import daemon
from ouroboros.persistence.picker_indexes import (
    PICKER_CONTRACT_DDL_BY_NAME,
    PICKER_META_TABLE,
    PICKER_PROJECTION_SCOPE_SQL,
    PICKER_PROJECTION_VERSION,
    PICKER_START_EXECUTION_ID_SQL,
    PICKER_START_SESSION_ID_SQL,
    PICKER_START_SESSION_TABLE,
    PICKER_START_TABLE,
)


def _create_events_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT, "
        "picker_projection_version INTEGER)"
    )
    for statement in PICKER_CONTRACT_DDL_BY_NAME.values():
        conn.execute(statement)
    conn.execute(
        f"INSERT INTO {PICKER_META_TABLE} "
        "(contract_version, backfilled_through_rowid) VALUES (?, 0)",
        (PICKER_PROJECTION_VERSION,),
    )


def _backfill_picker_starts(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"INSERT INTO {PICKER_START_TABLE} (event_rowid, execution_id, session_id) "
        f"SELECT rowid, {PICKER_START_EXECUTION_ID_SQL}, {PICKER_START_SESSION_ID_SQL} "
        "FROM events WHERE event_type = 'orchestrator.session.started'"
    )
    conn.execute(
        f"INSERT OR IGNORE INTO {PICKER_START_SESSION_TABLE} "
        "(session_id, execution_id, event_rowid) "
        "SELECT session_id, coalesce(execution_id, ''), MIN(event_rowid) "
        f"FROM {PICKER_START_TABLE} WHERE session_id IS NOT NULL "
        "GROUP BY session_id, execution_id"
    )
    conn.execute(
        f"UPDATE events SET picker_projection_version = ? WHERE {PICKER_PROJECTION_SCOPE_SQL}",
        (PICKER_PROJECTION_VERSION,),
    )


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
            _create_events_table(conn)
            conn.execute(
                "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
                (
                    f"orch_{execution_id}",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": execution_id}),
                ),
            )
            _backfill_picker_starts(conn)
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
            _create_events_table(conn)
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
            assert "startList()" in page
            assert "no active run" not in page
        finally:
            server.shutdown()

    def test_successful_runs_poll_refreshes_idle_watchdog(self, tmp_path) -> None:
        from ouroboros.dashboard_web.server import serve_background

        empty_db = tmp_path / "empty.db"
        conn = sqlite3.connect(empty_db)
        try:
            _create_events_table(conn)
            conn.commit()
        finally:
            conn.close()

        server, _thread = serve_background(
            db_path=str(empty_db), host="127.0.0.1", port=daemon._free_port("127.0.0.1")
        )
        try:
            touch_completed = threading.Event()
            original_touch = server.touch

            def synchronized_touch() -> None:
                original_touch()
                touch_completed.set()

            server.touch = synchronized_touch  # type: ignore[method-assign]
            server.last_activity = time.monotonic() - daemon.DEFAULT_IDLE_SHUTDOWN_SEC - 1
            assert server.idle_seconds() > daemon.DEFAULT_IDLE_SHUTDOWN_SEC

            status, body = self._get(server.server_address[1], "/api/runs")

            assert status == 200
            assert json.loads(body) == {"runs": []}
            assert touch_completed.wait(timeout=2.0)
            assert server.idle_seconds() < 1.0
        finally:
            server.shutdown()

    def test_unavailable_picker_contract_returns_503_without_watchdog_touch(self, tmp_path) -> None:
        from ouroboros.dashboard_web.server import serve_background

        legacy_db = tmp_path / "legacy.db"
        conn = sqlite3.connect(legacy_db)
        try:
            conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
            conn.commit()
        finally:
            conn.close()

        server, _thread = serve_background(
            db_path=str(legacy_db), host="127.0.0.1", port=daemon._free_port("127.0.0.1")
        )
        try:
            server.last_activity = time.monotonic() - daemon.DEFAULT_IDLE_SHUTDOWN_SEC - 1
            before = server.last_activity

            status, body = self._get(server.server_address[1], "/api/runs")

            assert status == 503
            assert json.loads(body) == {
                "runs": [],
                "error": "picker_index_contract_unavailable",
            }
            assert server.last_activity == before
        finally:
            server.shutdown()

    def test_runs_api_ignores_malformed_progress_and_refreshes_watchdog(self, tmp_path) -> None:
        from ouroboros.dashboard_web.server import serve_background

        db = tmp_path / "malformed-progress.db"
        conn = sqlite3.connect(db)
        try:
            _create_events_table(conn)
            conn.executemany(
                "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
                [
                    (
                        "orch_good",
                        "orchestrator.session.started",
                        json.dumps({"execution_id": "exec_good", "seed_goal": "Visible"}),
                    ),
                    ("foreign", "orchestrator.progress.updated", "{not-json"),
                ],
            )
            _backfill_picker_starts(conn)
            conn.commit()
        finally:
            conn.close()

        server, _thread = serve_background(
            db_path=str(db), host="127.0.0.1", port=daemon._free_port("127.0.0.1")
        )
        try:
            touch_completed = threading.Event()
            original_touch = server.touch

            def synchronized_touch() -> None:
                original_touch()
                touch_completed.set()

            server.touch = synchronized_touch  # type: ignore[method-assign]
            server.last_activity = time.monotonic() - daemon.DEFAULT_IDLE_SHUTDOWN_SEC - 1

            status, body = self._get(server.server_address[1], "/api/runs")

            assert status == 200
            assert json.loads(body)["runs"][0]["execution_id"] == "exec_good"
            assert touch_completed.wait(timeout=2.0)
            assert server.idle_seconds() < 1.0
        finally:
            server.shutdown()

    def test_runs_api_skips_malformed_starts_before_applying_limit(self, tmp_path) -> None:
        from ouroboros.dashboard_web.server import serve_background

        db = tmp_path / "malformed-starts.db"
        conn = sqlite3.connect(db)
        try:
            _create_events_table(conn)
            conn.execute(
                "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
                (
                    "orch_visible",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec_visible", "seed_goal": "Visible"}),
                ),
            )
            conn.executemany(
                "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
                [
                    (f"orch_bad_{index}", "orchestrator.session.started", "{not-json")
                    for index in range(10)
                ],
            )
            _backfill_picker_starts(conn)
            conn.commit()
        finally:
            conn.close()

        server, _thread = serve_background(
            db_path=str(db), host="127.0.0.1", port=daemon._free_port("127.0.0.1")
        )
        try:
            status, body = self._get(server.server_address[1], "/api/runs")

            assert status == 200
            assert [run["execution_id"] for run in json.loads(body)["runs"]] == ["exec_visible"]
        finally:
            server.shutdown()
