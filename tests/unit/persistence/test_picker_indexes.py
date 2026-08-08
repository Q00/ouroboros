"""Dashboard picker index lifecycle and exact contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import time

import pytest
from sqlalchemy.exc import OperationalError

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.picker_indexes import (
    AGGREGATE_EVENT_INDEX,
    PICKER_INDEX_DDL_BY_NAME,
    PICKER_INDEX_NAMES,
    PICKER_PROGRESS_SCOPE_SQL,
    matching_picker_indexes,
    normalize_index_ddl,
)


def _create_legacy_events_table(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE events (aggregate_id TEXT, event_type TEXT, payload TEXT)")
        conn.commit()
    finally:
        conn.close()


def _picker_index_sql(conn: sqlite3.Connection) -> dict[str, str]:
    placeholders = ",".join("?" for _ in PICKER_INDEX_NAMES)
    return dict(
        conn.execute(
            f"SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            f"AND name IN ({placeholders})",
            PICKER_INDEX_NAMES,
        )
    )


async def test_writable_initialize_installs_picker_indexes_on_existing_store(tmp_path) -> None:
    db = tmp_path / "legacy.db"
    _create_legacy_events_table(db)

    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.close()

    conn = sqlite3.connect(db)
    try:
        assert matching_picker_indexes(conn) == frozenset(PICKER_INDEX_NAMES)
    finally:
        conn.close()


async def test_read_only_initialize_does_not_install_picker_indexes(tmp_path) -> None:
    db = tmp_path / "read-only.db"
    _create_legacy_events_table(db)

    store = EventStore(f"sqlite+aiosqlite:///{db}", read_only=True)
    await store.initialize(create_schema=False)
    await store.close()

    conn = sqlite3.connect(db)
    try:
        assert _picker_index_sql(conn) == {}
    finally:
        conn.close()


async def test_writable_initialize_repairs_wrong_same_name_indexes(tmp_path) -> None:
    db = tmp_path / "wrong-indexes.db"
    _create_legacy_events_table(db)
    conn = sqlite3.connect(db)
    try:
        for name in PICKER_INDEX_NAMES:
            conn.execute(
                f"CREATE INDEX \"{name}\" ON events (aggregate_id) WHERE event_type = 'other'"
            )
        conn.commit()
    finally:
        conn.close()

    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.close()

    conn = sqlite3.connect(db)
    try:
        assert matching_picker_indexes(conn) == frozenset(PICKER_INDEX_NAMES)
        actual = _picker_index_sql(conn)
        for name, expected in PICKER_INDEX_DDL_BY_NAME.items():
            assert normalize_index_ddl(actual[name]) == normalize_index_ddl(expected)
    finally:
        conn.close()


def test_contract_checker_rejects_column_order_drift_with_same_predicate(tmp_path) -> None:
    db = tmp_path / "column-drift.db"
    _create_legacy_events_table(db)
    conn = sqlite3.connect(db)
    try:
        for statement in PICKER_INDEX_DDL_BY_NAME.values():
            conn.execute(statement)
        conn.execute(f'DROP INDEX "{AGGREGATE_EVENT_INDEX}"')
        conn.execute(
            f'CREATE INDEX "{AGGREGATE_EVENT_INDEX}" '
            "ON events (event_type, aggregate_id, json_valid(payload)) "
            f"WHERE {PICKER_PROGRESS_SCOPE_SQL}"
        )

        matching = matching_picker_indexes(conn)

        assert AGGREGATE_EVENT_INDEX not in matching
        assert matching == frozenset(PICKER_INDEX_NAMES) - {AGGREGATE_EVENT_INDEX}
    finally:
        conn.close()


@pytest.mark.parametrize(
    "replacements",
    [
        {
            "orchestrator.progress.updated": "ORCHESTRATOR.PROGRESS.UPDATED",
            "workflow.progress.updated": "WORKFLOW.PROGRESS.UPDATED",
            "running": "RUNNING",
        },
        {"'workflow.progress.updated'": "'workflow.progress. updated'"},
    ],
)
async def test_contract_rejects_and_repairs_changed_string_literals(tmp_path, replacements) -> None:
    db = tmp_path / "literal-drift.db"
    _create_legacy_events_table(db)
    conn = sqlite3.connect(db)
    try:
        for statement in PICKER_INDEX_DDL_BY_NAME.values():
            changed = statement
            for canonical, case_changed in replacements.items():
                changed = changed.replace(canonical, case_changed)
            conn.execute(changed)

        assert matching_picker_indexes(conn) == frozenset()
    finally:
        conn.close()

    store = EventStore(f"sqlite+aiosqlite:///{db}")
    await store.initialize()
    await store.close()

    conn = sqlite3.connect(db)
    try:
        assert matching_picker_indexes(conn) == frozenset(PICKER_INDEX_NAMES)
    finally:
        conn.close()


@pytest.mark.parametrize("sqlite_error", ["database or disk is full", "database is locked"])
async def test_picker_index_failure_does_not_block_writer(
    tmp_path, monkeypatch, caplog, sqlite_error
) -> None:
    from ouroboros.persistence import event_store as event_store_module
    from ouroboros.persistence import picker_index_provisioning as provisioning_module

    def fail_provision(_connection) -> None:
        raise OperationalError(
            "CREATE INDEX",
            {},
            sqlite3.OperationalError(sqlite_error),
        )

    monkeypatch.setattr(provisioning_module, "provision_picker_indexes", fail_provision)
    caplog.set_level(logging.WARNING, logger=event_store_module.__name__)
    db = tmp_path / "full.db"
    store = EventStore(f"sqlite+aiosqlite:///{db}")

    await store.initialize()
    event = BaseEvent(
        type="ontology.concept.added",
        aggregate_type="ontology",
        aggregate_id="ont-123",
        data={"name": "still writable"},
    )
    await store.append(event)
    replayed, _last_row = await store.get_events_after("ontology", "ont-123", 0)
    await store.close()

    assert [item.id for item in replayed] == [event.id]
    assert "picker index provisioning deferred" in caplog.text.lower()


async def test_concurrent_writable_initializers_are_benign(tmp_path) -> None:
    db = tmp_path / "concurrent.db"
    first = EventStore(f"sqlite+aiosqlite:///{db}")
    second = EventStore(f"sqlite+aiosqlite:///{db}")

    await asyncio.gather(first.initialize(), second.initialize())
    event = BaseEvent(
        type="ontology.concept.added",
        aggregate_type="ontology",
        aggregate_id="ont-concurrent",
        data={"name": "ready"},
    )
    await first.append(event)
    replayed, _last_row = await second.get_events_after("ontology", "ont-concurrent", 0)
    await asyncio.gather(first.close(), second.close())

    assert [item.id for item in replayed] == [event.id]
    conn = sqlite3.connect(db)
    try:
        assert matching_picker_indexes(conn) == frozenset(PICKER_INDEX_NAMES)
    finally:
        conn.close()


def _measure_bulk_append(path: Path, event_type: str, *, indexed: bool) -> tuple[float, int]:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE events (id TEXT PRIMARY KEY, aggregate_type TEXT, "
            "aggregate_id TEXT, event_type TEXT, payload TEXT, timestamp TEXT, "
            "consensus_id TEXT)"
        )
        conn.execute("CREATE INDEX ix_events_aggregate_type ON events (aggregate_type)")
        conn.execute("CREATE INDEX ix_events_aggregate_id ON events (aggregate_id)")
        conn.execute(
            "CREATE INDEX ix_events_aggregate_type_id ON events (aggregate_type, aggregate_id)"
        )
        conn.execute("CREATE INDEX ix_events_event_type ON events (event_type)")
        conn.execute("CREATE INDEX ix_events_timestamp ON events (timestamp)")
        if indexed:
            for statement in PICKER_INDEX_DDL_BY_NAME.values():
                conn.execute(statement)
        payload = json.dumps(
            {
                "progress": {"runtime_status": "completed"},
                "acceptance_criteria": [{"node_id": "n", "status": "completed"}],
            }
        )
        started = time.perf_counter()
        conn.executemany(
            "INSERT INTO events "
            "(id, aggregate_type, aggregate_id, event_type, payload, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (str(index), "test", "aggregate", event_type, payload, str(index))
                for index in range(100_000)
            ),
        )
        conn.commit()
        elapsed = time.perf_counter() - started
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        return elapsed, page_count * page_size
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("event_type", "max_time_ratio", "max_size_ratio"),
    [
        ("telemetry.unrelated", 1.25, 1.02),
        ("workflow.progress.updated", 2.1, 1.35),
    ],
)
def test_picker_index_write_and_disk_budgets(
    tmp_path, event_type: str, max_time_ratio: float, max_size_ratio: float
) -> None:
    baseline_time, baseline_size = _measure_bulk_append(
        tmp_path / f"baseline-{event_type}.db", event_type, indexed=False
    )
    indexed_time, indexed_size = _measure_bulk_append(
        tmp_path / f"indexed-{event_type}.db", event_type, indexed=True
    )

    assert indexed_time / baseline_time < max_time_ratio
    assert indexed_size / baseline_size < max_size_ratio
