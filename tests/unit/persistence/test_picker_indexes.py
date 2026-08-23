"""Dashboard picker projection lifecycle, repair, and budget tests."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sqlite3
from statistics import median
import time

import pytest
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.exc import IntegrityError, OperationalError

from ouroboros.dashboard_web.reader import (
    EventTail,
    PickerIndexContractError,
    list_recent_executions,
)
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.picker_indexes import (
    DIRECT_EVENT_INDEX,
    OBSOLETE_PICKER_INDEX_NAMES,
    PICKER_CONTRACT_NAMES,
    PICKER_GAP_INDEX,
    PICKER_META_TABLE,
    PICKER_PROGRESS_TABLE,
    PICKER_PROJECTION_SCOPE_SQL,
    PICKER_PROJECTION_VERSION,
    PICKER_START_SESSION_TABLE,
    PICKER_START_TABLE,
    matching_picker_contract,
)


def _create_legacy_events_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.commit()
    finally:
        conn.close()


async def _initialize(path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{path}")
    await store.initialize()
    await store.close()


def _insert_raw_start_without_projection_version(
    conn: sqlite3.Connection, *, suffix: str
) -> BaseEvent:
    event = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id=f"orch-{suffix}",
        data={"execution_id": f"exec-{suffix}"},
    )
    values = event.to_db_dict()
    values["payload"] = json.dumps(values["payload"])
    values["timestamp"] = values["timestamp"].isoformat()
    conn.execute(
        "INSERT INTO events (id, aggregate_type, aggregate_id, event_type, payload, "
        "timestamp, consensus_id) VALUES (:id, :aggregate_type, :aggregate_id, "
        ":event_type, :payload, :timestamp, :consensus_id)",
        values,
    )
    return event


async def test_writable_initialize_backfills_exact_projection_on_existing_store(tmp_path) -> None:
    db = tmp_path / "legacy.db"
    _create_legacy_events_table(db)
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            [
                ("orch", "orchestrator.session.started", json.dumps({"execution_id": "exec"})),
                (
                    "exec",
                    "workflow.progress.updated",
                    json.dumps(
                        {
                            "progress": {"runtime_status": "running"},
                            "acceptance_criteria": [],
                        }
                    ),
                ),
                ("exec", "workflow.progress.updated", json.dumps({"runtime_status": "done"})),
                ("exec", "workflow.progress.updated", "{malformed"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    await _initialize(db)

    conn = sqlite3.connect(db)
    try:
        assert matching_picker_contract(conn) == frozenset(PICKER_CONTRACT_NAMES)
        assert conn.execute(f"SELECT event_rowid FROM {PICKER_START_TABLE}").fetchall() == [(1,)]
        assert conn.execute(
            f"SELECT latest_valid_rowid, latest_running_rowid, latest_snapshot_rowid "
            f"FROM {PICKER_PROGRESS_TABLE}"
        ).fetchall() == [(3, 2, 2)]
        assert conn.execute(f"SELECT * FROM {PICKER_META_TABLE}").fetchall() == [
            (PICKER_PROJECTION_VERSION, 4)
        ]
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert {DIRECT_EVENT_INDEX} <= names
        assert not (set(OBSOLETE_PICKER_INDEX_NAMES) & names)
        table_sql = dict(
            conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND name IN (?, ?)",
                (PICKER_START_TABLE, PICKER_PROGRESS_TABLE),
            )
        )
        assert "WITHOUT ROWID" not in table_sql[PICKER_START_TABLE]
        assert table_sql[PICKER_PROGRESS_TABLE].endswith("WITHOUT ROWID")
    finally:
        conn.close()


async def test_application_heads_advance_independently(tmp_path) -> None:
    db = tmp_path / "application.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    events = [
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="orch",
            data={"execution_id": "exec"},
        ),
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="workflow",
            aggregate_id="exec",
            data={"acceptance_criteria": [], "runtime_status": "idle"},
        ),
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="workflow",
            aggregate_id="exec",
            data={"progress": {"runtime_status": "running"}},
        ),
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="workflow",
            aggregate_id="exec",
            data={"runtime_status": "done"},
        ),
    ]
    for event in events:
        await store.append(event)
    await store.close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(f"SELECT * FROM {PICKER_START_TABLE}").fetchall() == [
            (1, "exec", "orch")
        ]
        assert conn.execute(
            f"SELECT latest_valid_rowid, latest_running_rowid, latest_snapshot_rowid "
            f"FROM {PICKER_PROGRESS_TABLE}"
        ).fetchall() == [(4, 3, 2)]
    finally:
        conn.close()


async def test_start_batch_uses_one_bulk_insert_without_losing_identity_or_large_rowids(
    tmp_path,
) -> None:
    from ouroboros.persistence.picker_projection_updates import (
        insert_events_with_picker_projection,
    )

    db = tmp_path / "start-batch-json.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    assert store._engine is not None
    starts = [
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="orch-雪-'\n",
            data={"execution_id": 'exec-☃-"-\t'},
        ),
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="orch-plain",
            data={"execution_id": "exec-plain"},
        ),
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="orch-雪-'\n",
            data={"execution_id": 'exec-☃-"-\t'},
        ),
    ]
    projection_inserts: list[tuple[str, bool]] = []
    event_batch_inserts: list[str] = []

    def capture_start_insert(
        _connection, _cursor, statement, _parameters, _context, executemany
    ) -> None:
        if statement.lstrip().startswith("INSERT INTO dashboard_picker_starts_v1"):
            projection_inserts.append((statement, bool(executemany)))
        if statement.lstrip().startswith("INSERT INTO events") and executemany:
            event_batch_inserts.append(statement)

    sqlalchemy_event.listen(
        store._engine.sync_engine,
        "before_cursor_execute",
        capture_start_insert,
    )
    large_rowid = 2**53 + 7
    try:
        async with store._engine.begin() as connection:
            await connection.exec_driver_sql(
                "INSERT INTO events "
                "(rowid, id, aggregate_type, aggregate_id, event_type, payload, "
                "picker_projection_version) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    large_rowid,
                    "00000000-0000-0000-0000-000000000000",
                    "noise",
                    "noise",
                    "telemetry.unrelated",
                    "{}",
                    None,
                ),
            )
            await insert_events_with_picker_projection(
                connection,
                starts,
                projection_ready=True,
            )
    finally:
        sqlalchemy_event.remove(
            store._engine.sync_engine,
            "before_cursor_execute",
            capture_start_insert,
        )
        await store.close()

    assert len(projection_inserts) == 1
    statement, executemany = projection_inserts[0]
    assert statement.count("(?, ?, ?)") == len(starts)
    assert executemany is False
    assert len(event_batch_inserts) == 1
    assert "RETURNING" not in event_batch_inserts[0]
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            f"SELECT event_rowid, execution_id, session_id FROM {PICKER_START_TABLE} "
            "ORDER BY event_rowid"
        ).fetchall() == [
            (large_rowid + 1, 'exec-☃-"-\t', "orch-雪-'\n"),
            (large_rowid + 2, "exec-plain", "orch-plain"),
            (large_rowid + 3, 'exec-☃-"-\t', "orch-雪-'\n"),
        ]
        assert conn.execute(
            f"SELECT session_id, execution_id, event_rowid "
            f"FROM {PICKER_START_SESSION_TABLE} ORDER BY session_id"
        ).fetchall() == [
            ("orch-plain", "exec-plain", large_rowid + 2),
            ("orch-雪-'\n", 'exec-☃-"-\t', large_rowid + 1),
        ]
    finally:
        conn.close()

    traced = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    traced.row_factory = sqlite3.Row
    vm_steps = 0

    def count_step() -> int:
        nonlocal vm_steps
        vm_steps += 1
        return 0

    traced.set_progress_handler(count_step, 1)
    try:
        assert EventTail(db, "orch-雪-'\n")._resolve_ids(traced) == [
            'exec-☃-"-\t',
            "orch-雪-'\n",
        ]
        assert EventTail(db, 'exec-☃-"-\t')._resolve_ids(traced) == [
            'exec-☃-"-\t',
            "orch-雪-'\n",
        ]
    finally:
        traced.close()
    assert vm_steps < 1_000


async def test_single_then_batch_start_writes_keep_earliest_session_pointer(tmp_path) -> None:
    from ouroboros.persistence.picker_projection_updates import (
        insert_event_with_picker_projection,
        insert_events_with_picker_projection,
    )

    db = tmp_path / "single-then-batch-start.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    assert store._engine is not None
    starts = [
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="orch-shared",
            data={"execution_id": "exec-shared"},
        )
        for _ in range(3)
    ]
    async with store._engine.begin() as connection:
        await insert_event_with_picker_projection(connection, starts[0], True)
        await insert_events_with_picker_projection(connection, starts[1:], True)
    await store.close()

    conn = sqlite3.connect(db)
    try:
        first_rowid = conn.execute(f"SELECT MIN(event_rowid) FROM {PICKER_START_TABLE}").fetchone()[
            0
        ]
        assert conn.execute(
            f"SELECT session_id, execution_id, event_rowid FROM {PICKER_START_SESSION_TABLE}"
        ).fetchall() == [("orch-shared", "exec-shared", first_rowid)]
    finally:
        conn.close()


async def test_start_batch_rowid_gap_fails_closed_and_rolls_back(tmp_path) -> None:
    from ouroboros.persistence.picker_projection_updates import (
        insert_events_with_picker_projection,
    )

    db = tmp_path / "start-batch-gap.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    assert store._engine is not None
    starts = [
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id=f"orch-gap-{index}",
            data={"execution_id": f"exec-gap-{index}"},
        )
        for index in range(2)
    ]
    async with store._engine.begin() as connection:
        await connection.exec_driver_sql(
            "CREATE TRIGGER inject_start_gap AFTER INSERT ON events "
            "WHEN NEW.event_type = 'orchestrator.session.started' BEGIN "
            "INSERT INTO events "
            "(id, aggregate_type, aggregate_id, event_type, payload, "
            "picker_projection_version) VALUES "
            "('gap-' || NEW.id, 'noise', 'noise', 'telemetry.unrelated', '{}', NULL); "
            "END"
        )
    with pytest.raises(RuntimeError, match="not one contiguous SQLite range"):
        async with store._engine.begin() as connection:
            await insert_events_with_picker_projection(
                connection,
                starts,
                projection_ready=True,
            )
    await store.close()

    conn = sqlite3.connect(db)
    try:
        event_ids = [event.id for event in starts]
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE id IN (?, ?)", event_ids
        ).fetchone() == (0,)
        assert conn.execute(f"SELECT COUNT(*) FROM {PICKER_START_TABLE}").fetchone() == (0,)
    finally:
        conn.close()


async def test_sparse_relevant_batch_projects_only_requested_event_ids(tmp_path) -> None:
    db = tmp_path / "sparse-batch.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    events = [
        BaseEvent(
            type="telemetry.unrelated",
            aggregate_type="telemetry",
            aggregate_id=f"noise-{index}",
            data={},
        )
        for index in range(100)
    ]
    progress = BaseEvent(
        type="workflow.progress.updated",
        aggregate_type="workflow",
        aggregate_id="exec-sparse",
        data={
            "last_update": {"runtime_status": "running"},
            "acceptance_criteria": [],
        },
    )
    events.insert(50, progress)
    await store.append_batch(events)
    await store.close()

    conn = sqlite3.connect(db)
    try:
        progress_rowid = conn.execute(
            "SELECT rowid FROM events WHERE id = ?", (progress.id,)
        ).fetchone()[0]
        assert conn.execute(f"SELECT COUNT(*) FROM {PICKER_START_TABLE}").fetchone() == (0,)
        assert conn.execute(
            f"SELECT aggregate_id, event_type, latest_valid_rowid, "
            f"latest_running_rowid, latest_snapshot_rowid FROM {PICKER_PROGRESS_TABLE}"
        ).fetchall() == [
            (
                "exec-sparse",
                "workflow.progress.updated",
                progress_rowid,
                progress_rowid,
                progress_rowid,
            ),
        ]
    finally:
        conn.close()


async def test_read_only_initialize_does_not_install_projection(tmp_path) -> None:
    db = tmp_path / "read-only.db"
    _create_legacy_events_table(db)
    store = EventStore(f"sqlite+aiosqlite:///{db}", read_only=True)
    await store.initialize(create_schema=False)
    await store.close()
    conn = sqlite3.connect(db)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert not (set(PICKER_CONTRACT_NAMES) & names)
    finally:
        conn.close()


@pytest.mark.parametrize("bad_marker", [-1, 1.5, "text", sqlite3.Binary(b"blob")])
async def test_writable_initialize_repairs_malformed_completion_marker(
    tmp_path, bad_marker
) -> None:
    db = tmp_path / "marker.db"
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(f"UPDATE {PICKER_META_TABLE} SET backfilled_through_rowid = ?", (bad_marker,))
        conn.commit()
        assert matching_picker_contract(conn) != frozenset(PICKER_CONTRACT_NAMES)
    finally:
        conn.close()

    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        assert matching_picker_contract(conn) == frozenset(PICKER_CONTRACT_NAMES)
        assert conn.execute(f"SELECT * FROM {PICKER_META_TABLE}").fetchall() == [
            (PICKER_PROJECTION_VERSION, 0)
        ]
    finally:
        conn.close()


async def test_nonpositive_legacy_rowids_backfill_with_nonnegative_marker(tmp_path) -> None:
    db = tmp_path / "nonpositive.db"
    _create_legacy_events_table(db)
    conn = sqlite3.connect(db)
    try:
        conn.executemany(
            "INSERT INTO events (rowid, aggregate_id, event_type, payload) VALUES (?, ?, ?, ?)",
            [
                (
                    -(2**63),
                    "orch-min",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec-min"}),
                ),
                (
                    0,
                    "orch-zero",
                    "orchestrator.session.started",
                    json.dumps({"execution_id": "exec-zero"}),
                ),
            ],
        )
        conn.commit()
    finally:
        conn.close()
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            f"SELECT * FROM {PICKER_START_TABLE} ORDER BY event_rowid"
        ).fetchall() == [
            (-(2**63), "exec-min", "orch-min"),
            (0, "exec-zero", "orch-zero"),
        ]
        assert conn.execute(f"SELECT * FROM {PICKER_META_TABLE}").fetchall() == [
            (PICKER_PROJECTION_VERSION, 0)
        ]
    finally:
        conn.close()


async def test_raw_old_writer_gap_fails_closed_then_writable_repair_recovers(
    tmp_path,
) -> None:
    db = tmp_path / "raw-old-writer-gap.db"
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        event = _insert_raw_start_without_projection_version(conn, suffix="old-writer")
        conn.commit()
        assert conn.execute(
            "SELECT picker_projection_version FROM events WHERE id = ?", (event.id,)
        ).fetchone() == (None,)
        plan = " ".join(
            str(column)
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM events "
                f"INDEXED BY {PICKER_GAP_INDEX} "
                f"WHERE {PICKER_PROJECTION_SCOPE_SQL} "
                f"AND picker_projection_version IS NOT {PICKER_PROJECTION_VERSION} LIMIT 1"
            )
            for column in row
        ).upper()
        assert "SEARCH EVENTS" in plan
        assert "TEMP B-TREE" not in plan
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match="contains unprojected relevant events"):
        list_recent_executions(db)
    with pytest.raises(PickerIndexContractError, match="contains unprojected relevant events"):
        EventTail(db, "exec-old-writer").fetch_new()

    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT picker_projection_version FROM events WHERE id = ?", (event.id,)
        ).fetchone() == (PICKER_PROJECTION_VERSION,)
    finally:
        conn.close()
    assert [run["execution_id"] for run in list_recent_executions(db)] == ["exec-old-writer"]


@pytest.mark.parametrize(
    "bad_version",
    [
        pytest.param(0, id="zero"),
        pytest.param(1, id="rolling-old-writer"),
        pytest.param(PICKER_PROJECTION_VERSION + 1, id="future-integer"),
        pytest.param(1.5, id="real"),
        pytest.param("bad", id="text"),
        pytest.param(sqlite3.Binary(b"1"), id="blob"),
    ],
)
async def test_reader_fails_closed_for_every_malformed_projection_version(
    tmp_path, bad_version
) -> None:
    db = tmp_path / "malformed-projection-version.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    event = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-version",
        data={"execution_id": "exec-version"},
    )
    await store.append(event)
    await store.close()

    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE events SET picker_projection_version = ? WHERE id = ?",
            (bad_version, event.id),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError, match="contains unprojected relevant events"):
        list_recent_executions(db)
    with pytest.raises(PickerIndexContractError, match="contains unprojected relevant events"):
        EventTail(db, "exec-version").fetch_new()


def test_reader_fails_closed_for_legacy_events_table_without_version_column(tmp_path) -> None:
    db = tmp_path / "legacy-without-version-column.db"
    _create_legacy_events_table(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch-legacy",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec-legacy"}),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError):
        list_recent_executions(db)


@pytest.mark.parametrize(
    "column_ddl",
    [
        pytest.param("INTEGER DEFAULT 1", id="default-one"),
        pytest.param("TEXT", id="wrong-affinity"),
        pytest.param("INTEGER NOT NULL DEFAULT 1", id="not-null"),
    ],
)
async def test_wrong_same_name_projection_column_stays_unavailable_and_fails_closed(
    tmp_path, column_ddl: str
) -> None:
    db = tmp_path / "wrong-projection-column.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE events ("
            "id VARCHAR(36) PRIMARY KEY, aggregate_type VARCHAR(100) NOT NULL, "
            "aggregate_id VARCHAR(36) NOT NULL, event_type VARCHAR(200) NOT NULL, "
            "payload JSON NOT NULL, timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "consensus_id VARCHAR(36), "
            f"picker_projection_version {column_ddl})"
        )
        conn.commit()
    finally:
        conn.close()

    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    assert store._picker_projection_ready is False
    event = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-wrong-column",
        data={"execution_id": "exec-wrong-column"},
    )
    await store.append(event)
    replayed, _ = await store.get_events_after("session", "orch-wrong-column", 0)
    await store.close()
    assert [item.id for item in replayed] == [event.id]

    conn = sqlite3.connect(db)
    try:
        assert matching_picker_contract(conn) != frozenset(PICKER_CONTRACT_NAMES)
    finally:
        conn.close()
    with pytest.raises(PickerIndexContractError):
        list_recent_executions(db)


async def test_writable_initialize_backfills_100k_legacy_progress_rows(tmp_path) -> None:
    db = tmp_path / "legacy-100k.db"
    _create_legacy_events_table(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "orch",
                "orchestrator.session.started",
                json.dumps({"execution_id": "exec"}),
            ),
        )
        completed = json.dumps({"runtime_status": "completed"})
        conn.executemany(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (("exec", "workflow.progress.updated", completed) for _ in range(99_999)),
        )
        conn.execute(
            "INSERT INTO events (aggregate_id, event_type, payload) VALUES (?, ?, ?)",
            (
                "exec",
                "workflow.progress.updated",
                json.dumps(
                    {
                        "progress": {"runtime_status": "running"},
                        "acceptance_criteria": [],
                    }
                ),
            ),
        )
        conn.commit()
    finally:
        conn.close()

    await _initialize(db)

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(f"SELECT * FROM {PICKER_START_TABLE}").fetchall() == [
            (1, "exec", "orch")
        ]
        assert conn.execute(
            f"SELECT latest_valid_rowid, latest_running_rowid, latest_snapshot_rowid "
            f"FROM {PICKER_PROGRESS_TABLE}"
        ).fetchall() == [(100_001, 100_001, 100_001)]
        assert conn.execute(f"SELECT * FROM {PICKER_META_TABLE}").fetchall() == [
            (PICKER_PROJECTION_VERSION, 100_001)
        ]
    finally:
        conn.close()


@pytest.mark.parametrize("sqlite_error", ["database or disk is full", "database is locked"])
async def test_projection_failure_does_not_block_writer(
    tmp_path, monkeypatch, caplog, sqlite_error
) -> None:
    from ouroboros.persistence import event_store as event_store_module
    from ouroboros.persistence import picker_index_provisioning as provisioning_module

    def fail_provision(_connection) -> None:
        raise OperationalError("CREATE TABLE", {}, sqlite3.OperationalError(sqlite_error))

    monkeypatch.setattr(provisioning_module, "provision_picker_indexes", fail_provision)
    caplog.set_level(logging.WARNING, logger=event_store_module.__name__)
    db = tmp_path / "full.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    event = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-123",
        data={"execution_id": "exec-123"},
    )
    await store.append(event)
    replayed, _ = await store.get_events_after("session", "orch-123", 0)
    await store.close()
    assert [item.id for item in replayed] == [event.id]
    assert "projection provisioning deferred" in caplog.text.lower()


async def test_repeated_initialize_repairs_transient_projection_failure_in_process(
    tmp_path, monkeypatch, caplog
) -> None:
    from ouroboros.persistence import event_store as event_store_module
    from ouroboros.persistence import picker_index_provisioning as provisioning_module

    original_provision = provisioning_module.provision_picker_indexes
    attempts = 0

    def fail_once(connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "CREATE INDEX",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        original_provision(connection)

    monkeypatch.setattr(provisioning_module, "provision_picker_indexes", fail_once)
    caplog.set_level(logging.WARNING, logger=event_store_module.__name__)
    db = tmp_path / "transient-repair.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")

    await store.initialize()
    assert store._picker_projection_ready is False
    deferred = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-deferred",
        data={"execution_id": "exec-deferred"},
    )
    await store.append(deferred)
    assert [event.id for event in await store.replay("session", "orch-deferred")] == [deferred.id]

    # Same store, engine, and process: no close/restart is needed. The repair
    # transaction backfills the event accepted while projection was deferred.
    await store.initialize()
    assert store._picker_projection_ready is True
    assert attempts == 2
    assert list_recent_executions(db)[0]["execution_id"] == "exec-deferred"

    projected = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-projected",
        data={"execution_id": "exec-projected"},
    )
    await store.append(projected)
    await store.close()

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            f"SELECT execution_id, session_id FROM {PICKER_START_TABLE} ORDER BY event_rowid"
        ).fetchall() == [
            ("exec-deferred", "orch-deferred"),
            ("exec-projected", "orch-projected"),
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM events "
            "WHERE event_type = 'orchestrator.session.started' "
            f"AND picker_projection_version = {PICKER_PROJECTION_VERSION}"
        ).fetchone() == (2,)
    finally:
        conn.close()
    assert "eventstore.initialize() can be called again in-process" in caplog.text.lower()


async def test_projection_repair_drains_stale_writes_before_final_backfill(
    tmp_path, monkeypatch
) -> None:
    from ouroboros.persistence import event_store as event_store_module
    from ouroboros.persistence import picker_index_provisioning as provisioning_module

    original_insert = event_store_module._insert_event
    original_provision = provisioning_module.provision_picker_indexes
    append_entered = asyncio.Event()
    release_append = asyncio.Event()
    first_repair_finished = asyncio.Event()
    captured_readiness: list[bool] = []
    attempts = 0

    def fail_once_then_signal(connection) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError(
                "CREATE INDEX",
                {},
                sqlite3.OperationalError("database is locked"),
            )
        original_provision(connection)
        if attempts == 2:
            first_repair_finished.set()

    async def pause_stale_insert(connection, event, projection_ready):
        if event.aggregate_id == "exec-race":
            captured_readiness.append(projection_ready)
            append_entered.set()
            await release_append.wait()
        return await original_insert(connection, event, projection_ready)

    monkeypatch.setattr(provisioning_module, "provision_picker_indexes", fail_once_then_signal)
    monkeypatch.setattr(event_store_module, "_insert_event", pause_stale_insert)
    db = tmp_path / "repair-write-race.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    assert store._picker_projection_ready is False

    event = BaseEvent(
        type="workflow.progress.updated",
        aggregate_type="workflow",
        aggregate_id="exec-race",
        data={"execution_id": "exec-race", "runtime_status": "running"},
    )
    append_task = asyncio.create_task(store.append(event))
    await asyncio.wait_for(append_entered.wait(), timeout=5)
    repair_task = asyncio.create_task(store.initialize())
    await asyncio.wait_for(first_repair_finished.wait(), timeout=5)
    await asyncio.sleep(0)
    assert not repair_task.done(), "repair returned before the stale write settled"

    release_append.set()
    await asyncio.wait_for(append_task, timeout=5)
    await asyncio.wait_for(repair_task, timeout=5)
    assert captured_readiness == [False]
    assert attempts == 3
    assert store._picker_projection_ready is True
    assert list_recent_executions(db) == []
    await store.close()

    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT rowid, picker_projection_version FROM events WHERE id = ?",
            (event.id,),
        ).fetchone()
        assert row is not None
        assert row[1] == PICKER_PROJECTION_VERSION
        assert conn.execute(
            f"SELECT latest_valid_rowid FROM {PICKER_PROGRESS_TABLE} "
            "WHERE aggregate_id = ? AND event_type = ?",
            (event.aggregate_id, event.type),
        ).fetchone() == (row[0],)
    finally:
        conn.close()


async def test_relevant_append_survives_deferred_repair_of_malformed_meta(
    tmp_path, monkeypatch
) -> None:
    from ouroboros.persistence import picker_index_provisioning as provisioning_module

    db = tmp_path / "malformed-meta.db"
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(f'DROP TABLE "{PICKER_META_TABLE}"')
        conn.execute(f'CREATE TABLE "{PICKER_META_TABLE}" (contract_version INTEGER)')
        conn.commit()
    finally:
        conn.close()

    def fail_provision(_connection) -> None:
        raise OperationalError(
            "projection rebuild",
            {},
            sqlite3.OperationalError("database is locked"),
        )

    monkeypatch.setattr(provisioning_module, "provision_picker_indexes", fail_provision)
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    event = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-123",
        data={"execution_id": "exec-123"},
    )
    await store.append(event)
    replayed, _ = await store.get_events_after("session", "orch-123", 0)
    await store.close()

    assert [item.id for item in replayed] == [event.id]
    conn = sqlite3.connect(db)
    try:
        assert matching_picker_contract(conn) != frozenset(PICKER_CONTRACT_NAMES)
    finally:
        conn.close()


async def test_projection_write_failure_rolls_back_single_and_batch_events(tmp_path) -> None:
    from ouroboros.core.errors import PersistenceError
    from ouroboros.persistence.picker_projection_updates import (
        insert_events_with_picker_projection,
    )

    db = tmp_path / "projection-write-rollback.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    conn = sqlite3.connect(db)
    try:
        conn.execute(f'DROP TABLE "{PICKER_START_TABLE}"')
        conn.execute(f'DROP TABLE "{PICKER_PROGRESS_TABLE}"')
        conn.commit()
    finally:
        conn.close()

    start_single = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-start-single",
        data={"execution_id": "exec-start-single"},
    )
    start_batch = [
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id=f"orch-start-batch-{index}",
            data={"execution_id": f"exec-start-batch-{index}"},
        )
        for index in range(2)
    ]
    single = BaseEvent(
        type="workflow.progress.updated",
        aggregate_type="workflow",
        aggregate_id="exec-single",
        data={"acceptance_criteria": []},
    )
    batch = [
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="workflow",
            aggregate_id="exec-batch",
            data={"runtime_status": status},
        )
        for status in ("running", "completed")
    ]
    mixed_batch = [
        BaseEvent(
            type="telemetry.unrelated",
            aggregate_type="telemetry",
            aggregate_id="noise-mixed",
            data={},
        ),
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="workflow",
            aggregate_id="exec-mixed",
            data={"runtime_status": "running"},
        ),
    ]
    with pytest.raises(PersistenceError):
        await store.append(start_single)
    assert store._engine is not None
    with pytest.raises(OperationalError):
        async with store._engine.begin() as connection:
            await insert_events_with_picker_projection(
                connection,
                start_batch,
                projection_ready=True,
            )
    with pytest.raises(PersistenceError):
        await store.append(single)
    with pytest.raises(PersistenceError):
        await store.append_batch(batch)
    with pytest.raises(PersistenceError):
        await store.append_batch(mixed_batch)
    await store.close()

    conn = sqlite3.connect(db)
    try:
        event_ids = [
            start_single.id,
            *(event.id for event in start_batch),
            single.id,
            *(event.id for event in batch),
            *(event.id for event in mixed_batch),
        ]
        placeholders = ",".join("?" for _ in event_ids)
        assert conn.execute(
            f"SELECT COUNT(*) FROM events WHERE id IN ({placeholders})", event_ids
        ).fetchone() == (0,)
    finally:
        conn.close()


async def test_projection_integrity_failure_is_also_best_effort(tmp_path, monkeypatch) -> None:
    from ouroboros.persistence import picker_index_provisioning as provisioning_module

    def fail_provision(_connection) -> None:
        raise IntegrityError("backfill", {}, sqlite3.IntegrityError("constraint failed"))

    monkeypatch.setattr(provisioning_module, "provision_picker_indexes", fail_provision)
    db = tmp_path / "integrity.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.append(
        BaseEvent(type="test.added", aggregate_type="test", aggregate_id="a", data={})
    )
    await store.close()


async def test_concurrent_writable_initializers_are_benign(tmp_path) -> None:
    db = tmp_path / "concurrent.db"
    first = EventStore(f"sqlite+aiosqlite:///{db}")
    second = EventStore(f"sqlite+aiosqlite:///{db}")
    await asyncio.gather(first.initialize(), second.initialize())
    event = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-concurrent",
        data={"execution_id": "exec-concurrent"},
    )
    await first.append(event)
    replayed, _ = await second.get_events_after("session", "orch-concurrent", 0)
    await asyncio.gather(first.close(), second.close())

    assert [item.id for item in replayed] == [event.id]
    conn = sqlite3.connect(db)
    try:
        assert matching_picker_contract(conn) == frozenset(PICKER_CONTRACT_NAMES)
        assert conn.execute(f"SELECT COUNT(*) FROM {PICKER_META_TABLE}").fetchone() == (1,)
        assert conn.execute(f"SELECT COUNT(*) FROM {PICKER_START_TABLE}").fetchone() == (1,)
    finally:
        conn.close()


async def test_writable_initialize_repairs_wrong_same_name_direct_index(tmp_path) -> None:
    db = tmp_path / "wrong-direct.db"
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(f'DROP INDEX "{DIRECT_EVENT_INDEX}"')
        conn.execute(f'CREATE INDEX "{DIRECT_EVENT_INDEX}" ON events (aggregate_id)')
        conn.commit()
        assert matching_picker_contract(conn) != frozenset(PICKER_CONTRACT_NAMES)
    finally:
        conn.close()
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        assert matching_picker_contract(conn) == frozenset(PICKER_CONTRACT_NAMES)
    finally:
        conn.close()


async def test_writable_initialize_migrates_pr_local_aggregate_index_to_canonical_link(
    tmp_path,
) -> None:
    db = tmp_path / "old-pr-local-direct-index.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    event = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch-migrate",
        data={"execution_id": "exec-migrate"},
    )
    await store.append(event)
    await store.close()

    old_index = "ix_events_picker_direct_aggregate_event_v1"
    conn = sqlite3.connect(db)
    try:
        conn.execute(f'DROP INDEX "{DIRECT_EVENT_INDEX}"')
        conn.execute(
            f"CREATE INDEX {old_index} ON events (event_type, aggregate_id) "
            "WHERE ((event_type >= 'execution.' AND event_type < 'execution/') "
            "OR (event_type >= 'orchestrator.session.' "
            "AND event_type < 'orchestrator.session/')) "
            "AND event_type != 'orchestrator.session.started'"
        )
        conn.commit()
    finally:
        conn.close()

    with pytest.raises(PickerIndexContractError):
        list_recent_executions(db)

    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert old_index not in names
        assert matching_picker_contract(conn) == frozenset(PICKER_CONTRACT_NAMES)
        key_columns = tuple(
            row[2]
            for row in conn.execute(f'PRAGMA index_xinfo("{DIRECT_EVENT_INDEX}")')
            if int(row[5]) == 1
        )
        assert key_columns == (None, "event_type")
    finally:
        conn.close()
    assert [run["execution_id"] for run in list_recent_executions(db)] == ["exec-migrate"]


async def test_writable_initialize_repairs_wrong_projection_objects_and_backfills(
    tmp_path,
) -> None:
    db = tmp_path / "wrong-projection-objects.db"
    await _initialize(db)
    started = BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id="orch",
        data={"execution_id": "exec"},
    )
    progress = BaseEvent(
        type="workflow.progress.updated",
        aggregate_type="workflow",
        aggregate_id="exec",
        data={"progress": {"runtime_status": "running"}, "acceptance_criteria": []},
    )
    conn = sqlite3.connect(db)
    try:
        conn.execute(f'DROP TABLE "{PICKER_START_TABLE}"')
        conn.execute(f'DROP TABLE "{PICKER_PROGRESS_TABLE}"')
        conn.execute(f'DROP TABLE "{PICKER_META_TABLE}"')
        conn.execute(
            f'CREATE VIEW "{PICKER_START_TABLE}" AS SELECT rowid AS event_rowid FROM events WHERE 0'
        )
        conn.execute(f'CREATE TABLE "{PICKER_PROGRESS_TABLE}" (aggregate_id TEXT)')
        conn.execute(f'CREATE TABLE "{PICKER_META_TABLE}" (contract_version TEXT)')
        for event in (started, progress):
            values = event.to_db_dict()
            values["payload"] = json.dumps(values["payload"])
            conn.execute(
                "INSERT INTO events (id, aggregate_type, aggregate_id, event_type, payload, "
                "timestamp, consensus_id) VALUES (:id, :aggregate_type, :aggregate_id, "
                ":event_type, :payload, :timestamp, :consensus_id)",
                values,
            )
        conn.commit()
        assert matching_picker_contract(conn) != frozenset(PICKER_CONTRACT_NAMES)
    finally:
        conn.close()

    await _initialize(db)

    conn = sqlite3.connect(db)
    try:
        assert matching_picker_contract(conn) == frozenset(PICKER_CONTRACT_NAMES)
        assert conn.execute(f"SELECT * FROM {PICKER_START_TABLE}").fetchall() == [
            (1, "exec", "orch")
        ]
        assert conn.execute(
            f"SELECT latest_valid_rowid, latest_running_rowid, latest_snapshot_rowid "
            f"FROM {PICKER_PROGRESS_TABLE}"
        ).fetchall() == [(2, 2, 2)]
    finally:
        conn.close()


async def test_failed_rebuild_rolls_back_all_projection_ddl(tmp_path, monkeypatch) -> None:
    from ouroboros.persistence import picker_index_provisioning as provisioning

    db = tmp_path / "rollback.db"
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        conn.execute(f'DROP INDEX "{DIRECT_EVENT_INDEX}"')
        conn.execute(f'CREATE INDEX "{DIRECT_EVENT_INDEX}" ON events (aggregate_id)')
        conn.commit()
    finally:
        conn.close()

    def fail_mid_rebuild(connection) -> None:
        connection.exec_driver_sql(f'DROP TABLE "{PICKER_START_TABLE}"')
        raise OperationalError(
            "projection rebuild",
            {},
            sqlite3.OperationalError("injected rebuild failure"),
        )

    monkeypatch.setattr(provisioning, "_rebuild_projection", fail_mid_rebuild)
    await _initialize(db)
    conn = sqlite3.connect(db)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
        assert PICKER_START_TABLE in names
        assert PICKER_META_TABLE in names
        # The pre-existing drift remains; none of the failed rebuild's partial
        # DDL became visible.
        assert matching_picker_contract(conn) != frozenset(PICKER_CONTRACT_NAMES)
    finally:
        conn.close()


def test_all_event_inserts_are_owned_by_projection_wrapper() -> None:
    repo_root = Path(__file__).parents[3]
    insert_sites: list[str] = []
    for path in (repo_root / "src" / "ouroboros").rglob("*.py"):
        count = path.read_text().count("events_table.insert")
        if count:
            insert_sites.append(str(path.relative_to(repo_root)))
    assert insert_sites == ["src/ouroboros/persistence/picker_projection_updates.py"]


# Bulk size for the write/disk budget benchmark. Large enough that per-row
# projection cost dominates fixed setup cost in the measured ratios, small
# enough that the six parametrized budgets stay cheap in the unit suite.
_BULK_EVENT_COUNT = 50_000


def _measure_bulk_append(path: Path, event_type: str, *, projected: bool) -> tuple[float, int]:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE events (id TEXT PRIMARY KEY, aggregate_type TEXT, "
            "aggregate_id TEXT, event_type TEXT, payload TEXT, timestamp TEXT, "
            "consensus_id TEXT)"
        )
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute("CREATE INDEX ix_events_aggregate_type ON events (aggregate_type)")
        conn.execute(
            "CREATE INDEX ix_events_aggregate_type_id ON events (aggregate_type, aggregate_id)"
        )
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        conn.execute("CREATE INDEX ix_events_timestamp ON events (timestamp)")
        if projected:
            from sqlalchemy import create_engine

            from ouroboros.persistence.picker_index_provisioning import provision_picker_indexes

            conn.commit()
            conn.close()
            engine = create_engine(f"sqlite:///{path}")
            with engine.begin() as connection:
                provision_picker_indexes(connection)
            engine.dispose()
            conn = sqlite3.connect(path)
        payload = {
            "progress": {"runtime_status": "completed"},
            "acceptance_criteria": [{"node_id": "n", "status": "completed"}],
        }
        if event_type == "execution.ac.token_attribution.reported":
            payload = {
                "execution_id": "execution",
                "session_id": "native-session",
                "node_id": "n",
                "token_spend": 1234,
            }
        elif event_type == "orchestrator.tool.called":
            payload = {"tool_name": "Read"}
        conn.close()
        event_chunks = [
            [
                BaseEvent(
                    id=f"{offset + index:036d}",
                    type=event_type,
                    aggregate_type="test",
                    aggregate_id="aggregate",
                    data=payload,
                )
                for index in range(5_000)
            ]
            for offset in range(0, _BULK_EVENT_COUNT, 5_000)
        ]

        async def append_all() -> None:
            from sqlalchemy.ext.asyncio import create_async_engine

            from ouroboros.persistence.picker_projection_updates import (
                insert_events_with_picker_projection,
            )
            from ouroboros.persistence.schema import events_table

            engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
            try:
                async with engine.begin() as connection:
                    for events in event_chunks:
                        if projected:
                            await insert_events_with_picker_projection(
                                connection,
                                events,
                                projection_ready=True,
                            )
                        else:
                            await connection.execute(
                                events_table.insert(),
                                [event.to_db_dict() for event in events],
                            )
            finally:
                await engine.dispose()

        started = time.process_time()
        asyncio.run(append_all())
        elapsed = time.process_time() - started
        conn = sqlite3.connect(path)
        if projected:
            start_count = conn.execute(f"SELECT COUNT(*) FROM {PICKER_START_TABLE}").fetchone()[0]
            progress_count = conn.execute(
                f"SELECT COUNT(*) FROM {PICKER_PROGRESS_TABLE}"
            ).fetchone()[0]
            assert start_count == (
                _BULK_EVENT_COUNT if event_type == "orchestrator.session.started" else 0
            )
            assert progress_count == (1 if event_type == "workflow.progress.updated" else 0)
        size = int(conn.execute("PRAGMA page_count").fetchone()[0]) * int(
            conn.execute("PRAGMA page_size").fetchone()[0]
        )
        return elapsed, size
    finally:
        conn.close()


@pytest.mark.performance
@pytest.mark.parametrize(
    ("event_type", "max_time_ratio", "max_size_ratio"),
    [
        # Unmatched batches still pay for the projection fence column and the
        # relevance scan; six CI samples put that structural CPU cost at 1.27x.
        ("telemetry.unrelated", 1.5, 1.02),
        ("orchestrator.session.started", 1.75, 1.25),
        ("execution.node.updated", 1.75, 1.25),
        ("execution.ac.token_attribution.reported", 1.75, 1.25),
        ("orchestrator.tool.called", 1.75, 1.25),
        ("workflow.progress.updated", 2.1, 1.35),
    ],
)
def test_picker_projection_write_and_disk_budgets(
    tmp_path, event_type: str, max_time_ratio: float, max_size_ratio: float
) -> None:
    time_ratios: list[float] = []
    size_ratios: list[float] = []
    for trial in range(3):
        baseline = tmp_path / f"baseline-{trial}.db"
        projected = tmp_path / f"projected-{trial}.db"
        try:
            baseline_time, baseline_size = _measure_bulk_append(
                baseline, event_type, projected=False
            )
            projected_time, projected_size = _measure_bulk_append(
                projected, event_type, projected=True
            )
            time_ratios.append(projected_time / baseline_time)
            size_ratios.append(projected_size / baseline_size)
        finally:
            baseline.unlink(missing_ok=True)
            projected.unlink(missing_ok=True)
    assert median(time_ratios) < max_time_ratio
    assert max(size_ratios) < max_size_ratio
