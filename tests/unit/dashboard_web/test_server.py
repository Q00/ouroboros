"""Live HTTP boundary coverage for dashboard picker-contract failures."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import http.client
import json
import sqlite3
import sys

import pytest

from ouroboros.dashboard_web.server import serve_background
from ouroboros.persistence.picker_indexes import (
    DIRECT_EVENT_INDEX,
    PICKER_CONTRACT_DDL_BY_NAME,
    PICKER_META_TABLE,
    PICKER_PROJECTION_VERSION,
    PICKER_START_SESSION_TABLE,
    PICKER_START_TABLE,
)


def _make_events_db(path, *, contract: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT, "
            "picker_projection_version INTEGER)"
        )
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        if contract != "missing":
            for statement in PICKER_CONTRACT_DDL_BY_NAME.values():
                conn.execute(statement)
        cursor = conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch-http",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec-http"}),
            ),
        )
        if contract != "missing":
            event_rowid = int(cursor.lastrowid)
            conn.execute(
                f"INSERT INTO {PICKER_START_TABLE} "
                "(event_rowid, execution_id, session_id) VALUES (?, ?, ?)",
                (event_rowid, "exec-http", "orch-http"),
            )
            conn.execute(
                f"INSERT INTO {PICKER_START_SESSION_TABLE} "
                "(session_id, execution_id, event_rowid) VALUES (?, ?, ?)",
                ("orch-http", "exec-http", event_rowid),
            )
            conn.execute(
                "UPDATE events SET picker_projection_version = ? WHERE rowid = ?",
                (PICKER_PROJECTION_VERSION, event_rowid),
            )
            conn.execute(
                f"INSERT INTO {PICKER_META_TABLE} "
                "(contract_version, backfilled_through_rowid) VALUES (?, ?)",
                (PICKER_PROJECTION_VERSION, event_rowid),
            )
        if contract == "drifted":
            conn.execute(f"DROP INDEX {DIRECT_EVENT_INDEX}")
            conn.execute(f"CREATE INDEX {DIRECT_EVENT_INDEX} ON events (event_type, aggregate_id)")
        conn.commit()
    finally:
        conn.close()


def _add_cross_namespace_collision(path) -> None:
    """Make ``exec-http`` name a different run's session in the same contract."""
    conn = sqlite3.connect(path)
    try:
        cursor = conn.execute(
            "INSERT INTO events "
            "(aggregate_id, event_type, payload, picker_projection_version) "
            "VALUES (?, ?, ?, ?)",
            (
                "exec-http",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec-other"}),
                PICKER_PROJECTION_VERSION,
            ),
        )
        event_rowid = int(cursor.lastrowid)
        conn.execute(
            f"INSERT INTO {PICKER_START_TABLE} "
            "(event_rowid, execution_id, session_id) VALUES (?, ?, ?)",
            (event_rowid, "exec-other", "exec-http"),
        )
        conn.execute(
            f"INSERT INTO {PICKER_START_SESSION_TABLE} "
            "(session_id, execution_id, event_rowid) VALUES (?, ?, ?)",
            ("exec-http", "exec-other", event_rowid),
        )
        conn.execute(
            f"UPDATE {PICKER_META_TABLE} SET backfilled_through_rowid = ?",
            (event_rowid,),
        )
        conn.commit()
    finally:
        conn.close()


def _add_linked_interview(path) -> None:
    """Add the production aggregate namespace and one explicit C1 source link."""
    conn = sqlite3.connect(path)
    try:
        conn.execute("ALTER TABLE events ADD COLUMN aggregate_type TEXT")
        conn.execute(
            "UPDATE events SET aggregate_type = 'session', "
            "payload = json_set(payload, '$.interview_id', 'interview-http') "
            "WHERE event_type = 'orchestrator.session.started'"
        )
        conn.execute(
            "INSERT INTO events (aggregate_type, aggregate_id, event_type, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                "interview",
                "interview-http",
                "interview.response.recorded",
                json.dumps({"round_number": 2}),
            ),
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _running_server(db_path) -> Iterator[object]:
    server, thread = serve_background(db_path=str(db_path), host="127.0.0.1", port=0)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


def _get(host: str, port: int, path: str) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=2.0)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.mark.parametrize("contract", ["missing", "drifted"])
@pytest.mark.parametrize("path", ["/snapshot?run=exec-http", "/events?run=exec-http"])
def test_run_endpoints_return_json_503_before_committing_success(
    tmp_path, contract: str, path: str
) -> None:
    db = tmp_path / f"{contract}.db"
    _make_events_db(db, contract=contract)

    with _running_server(db) as server:
        host, port = server.server_address
        initial_activity = server.last_activity
        status, headers, body = _get(host, port, path)
        assert server.open_streams == 0
        assert server.last_activity == initial_activity

    assert status == 503
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"error": "picker_index_contract_unavailable"}


@pytest.mark.parametrize("path", ["/snapshot?run=exec-http", "/events?run=exec-http"])
def test_run_endpoints_reject_cross_namespace_identity_collision(tmp_path, path: str) -> None:
    db = tmp_path / "identity-collision.db"
    _make_events_db(db, contract="valid")
    _add_cross_namespace_collision(db)

    with _running_server(db) as server:
        host, port = server.server_address
        status, headers, body = _get(host, port, path)
        assert server.open_streams == 0

    assert status == 503
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"error": "picker_index_contract_unavailable"}


def test_normal_sse_emits_preflight_batch_once_then_new_events(tmp_path) -> None:
    db = tmp_path / "valid.db"
    _make_events_db(db, contract="valid")

    with _running_server(db) as server:
        host, port = server.server_address
        conn = http.client.HTTPConnection(host, port, timeout=2.0)
        try:
            conn.request("GET", "/events?run=exec-http")
            response = conn.getresponse()
            assert response.status == 200
            assert response.getheader("Content-Type") == "text/event-stream"
            first_line = response.readline()
            assert response.readline() == b"\n"

            writer = sqlite3.connect(db)
            try:
                writer.execute(
                    "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
                    (
                        "exec-http",
                        "execution.node.created",
                        json.dumps(
                            {
                                "execution_id": "exec-http",
                                "node_id": "node-http",
                                "description": "HTTP boundary regression",
                            }
                        ),
                    ),
                )
                writer.commit()
            finally:
                writer.close()

            second_line = response.readline()
            assert response.readline() == b"\n"

            writer = sqlite3.connect(db)
            try:
                writer.execute(f"DROP INDEX {DIRECT_EVENT_INDEX}")
                writer.commit()
            finally:
                writer.close()
            assert response.read() == b""
            assert server.open_streams == 0
        finally:
            conn.close()

    assert first_line.startswith(b"data: ")
    assert second_line.startswith(b"data: ")
    first_payload = json.loads(first_line.removeprefix(b"data: "))
    second_payload = json.loads(second_line.removeprefix(b"data: "))
    assert "node-http" not in json.dumps(first_payload)
    assert "node-http" in json.dumps(second_payload)


def test_snapshot_and_sse_share_linked_interview_read_only_projection(tmp_path) -> None:
    db = tmp_path / "linked-http.db"
    _make_events_db(db, contract="valid")
    _add_linked_interview(db)

    with _running_server(db) as server:
        host, port = server.server_address
        snapshot_status, snapshot_headers, snapshot_body = _get(
            host, port, "/snapshot?run=exec-http"
        )
        conn = http.client.HTTPConnection(host, port, timeout=2.0)
        try:
            conn.request("GET", "/events?run=exec-http")
            response = conn.getresponse()
            assert response.status == 200
            sse_line = response.readline()
            assert response.readline() == b"\n"
        finally:
            conn.close()

    assert snapshot_status == 200
    assert snapshot_headers["Content-Type"] == "text/html; charset=utf-8"
    snapshot_html = snapshot_body.decode("utf-8")
    streamed = json.loads(sse_line.removeprefix(b"data: "))
    expected = {
        "interview_id": "interview-http",
        "status": "active",
        "round": 2,
        "total_rounds": None,
        "last_event": "interview.response.recorded",
    }
    assert streamed["meta"]["interview"] == expected
    for value in ("interview-http", "active", "interview.response.recorded"):
        assert value in snapshot_html
    assert 'method:"POST"' not in snapshot_html


def test_stream_contract_loss_closes_without_handler_traceback(tmp_path, monkeypatch) -> None:
    db = tmp_path / "contract-loss.db"
    _make_events_db(db, contract="valid")
    errors: list[BaseException] = []

    with _running_server(db) as server:
        host, port = server.server_address
        opened = 0
        closed = 0
        original_opened = server.stream_opened
        original_closed = server.stream_closed

        def count_opened() -> None:
            nonlocal opened
            opened += 1
            original_opened()

        def count_closed() -> None:
            nonlocal closed
            closed += 1
            original_closed()

        monkeypatch.setattr(server, "stream_opened", count_opened)
        monkeypatch.setattr(server, "stream_closed", count_closed)

        def capture_error(_request, _client_address) -> None:
            error = sys.exc_info()[1]
            if error is not None:
                errors.append(error)

        monkeypatch.setattr(server, "handle_error", capture_error)
        conn = http.client.HTTPConnection(host, port, timeout=2.0)
        try:
            conn.request("GET", "/events?run=exec-http")
            response = conn.getresponse()
            assert response.status == 200
            assert response.readline().startswith(b"data: ")
            assert response.readline() == b"\n"
            assert server.open_streams == 1
            assert opened == 1
            assert closed == 0

            writer = sqlite3.connect(db)
            try:
                writer.execute(f"DROP INDEX {DIRECT_EVENT_INDEX}")
                writer.commit()
            finally:
                writer.close()

            assert response.read() == b""
            assert server.open_streams == 0
        finally:
            conn.close()

    assert errors == []
    assert opened == 1
    assert closed == 1


async def test_interview_roots_actual_eventstore_http(tmp_path) -> None:
    from ouroboros.events.interview import interview_started
    from ouroboros.persistence.event_store import EventStore
    from ouroboros.persistence.picker_indexes import PICKER_INTERVIEW_ROOT_INDEX

    db = tmp_path / "interview-http.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.append(interview_started("pre-run", "private text is not a picker field"))
    await store.close()
    with _running_server(db) as server:
        host, port = server.server_address
        status, _, body = _get(host, port, "/api/interviews?limit=1")
        assert status == 200
        assert json.loads(body) == {
            "interviews": [
                {"family": "interview", "interview_id": "pre-run", "root_event_rowid": 1}
            ]
        }
        assert _get(host, port, "/api/interviews?limit=101")[0] == 400
        assert _get(host, port, "/api/interviews?limit=oops")[0] == 400
        assert json.loads(_get(host, port, "/api/runs")[2]) == {"runs": []}
        with sqlite3.connect(db) as conn:
            conn.execute(f"DROP INDEX {PICKER_INTERVIEW_ROOT_INDEX}")
        status, _, body = _get(host, port, "/api/interviews")
        assert status == 503
        assert json.loads(body)["error"] == "picker_index_contract_unavailable"


async def test_unlinked_interview_collision_is_absent_from_snapshot_and_sse(tmp_path) -> None:
    from ouroboros.events.base import BaseEvent
    from ouroboros.events.interview import interview_started
    from ouroboros.persistence.event_store import EventStore

    db = tmp_path / "unlinked-interview.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="shared",
            data={"execution_id": "exec-shared"},
        )
    )
    await store.append(interview_started("shared", "PRIVATE CONTEXT"))
    await store.close()
    with _running_server(db) as server:
        host, port = server.server_address
        status, _, body = _get(host, port, "/snapshot?run=exec-shared")
        assert status == 200
        assert b"PRIVATE CONTEXT" not in body
        conn = http.client.HTTPConnection(host, port, timeout=2)
        try:
            conn.request("GET", "/events?run=exec-shared")
            response = conn.getresponse()
            assert response.status == 200
            batch = response.readline()
            assert (
                json.loads(batch.removeprefix(b"data: "))["meta"]["execution_id"] == "exec-shared"
            )
            assert b"interview.started" not in batch
            assert b"PRIVATE CONTEXT" not in batch
        finally:
            conn.close()
