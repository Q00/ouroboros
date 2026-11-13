"""Equivalence and scaling tests for ``EventStore.get_session_activity_snapshots``.

The production query probes each session through the aggregate indexes. These
tests pin it to the previous window-function implementation (kept below as a
private reference) and to a pure-Python model of the same selection rules, and
prove the saving by counting SQLite VM steps instead of timing.
"""

from __future__ import annotations

from dataclasses import astuple
from datetime import datetime
import json
from pathlib import Path
import random
import sqlite3
from typing import Any

import pytest
from sqlalchemy import and_, case, event, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ouroboros.core.errors import PersistenceError
from ouroboros.persistence.event_store import (
    _SESSION_TERMINAL_EVENT_TYPES,
    EventStore,
    SessionActivitySnapshot,
)
from ouroboros.persistence.schema import events_table

_PAUSED = "orchestrator.session.paused"
_PROGRESS = ("orchestrator.progress.updated", "workflow.progress.updated")
_EVENT_TYPES = (
    "orchestrator.session.started",
    *_PROGRESS,
    _PAUSED,
    *sorted(_SESSION_TERMINAL_EVENT_TYPES),
    "orchestrator.tool.called",
    "execution.ac.heartbeat",
)
_STATUS_VALUES: tuple[Any, ...] = ("running", "  Running ", "", None, 3, True, False, [1])

Row = tuple[str, str, str, str, Any, Any]


async def _legacy_session_activity_snapshots(
    engine: AsyncEngine,
) -> list[SessionActivitySnapshot]:
    """The window-function implementation this PR replaced, kept as the oracle."""
    status_expr = func.coalesce(
        func.json_extract(events_table.c.payload, "$.progress.runtime_status"),
        func.json_extract(events_table.c.payload, "$.runtime_status"),
    )

    first_session_event_ranked = (
        select(
            events_table.c.aggregate_id.label("session_id"),
            func.json_extract(events_table.c.payload, "$.execution_id").label("execution_id"),
            func.json_extract(events_table.c.payload, "$.seed_id").label("seed_id"),
            func.coalesce(
                func.json_extract(events_table.c.payload, "$.start_time"),
                events_table.c.timestamp,
            ).label("start_time"),
            func.row_number()
            .over(
                partition_by=events_table.c.aggregate_id,
                order_by=(events_table.c.timestamp.asc(), events_table.c.id.asc()),
            )
            .label("rn"),
        )
        .where(events_table.c.aggregate_type == "session")
        .subquery()
    )

    latest_activity_ranked = (
        select(
            events_table.c.aggregate_id.label("session_id"),
            events_table.c.timestamp.label("last_activity"),
            func.row_number()
            .over(
                partition_by=events_table.c.aggregate_id,
                order_by=(events_table.c.timestamp.desc(), events_table.c.id.desc()),
            )
            .label("rn"),
        )
        .where(events_table.c.aggregate_type == "session")
        .subquery()
    )

    latest_status_ranked = (
        select(
            events_table.c.aggregate_id.label("session_id"),
            events_table.c.event_type.label("status_event_type"),
            status_expr.label("runtime_status"),
            func.row_number()
            .over(
                partition_by=events_table.c.aggregate_id,
                order_by=(
                    case(
                        (events_table.c.event_type.in_(_SESSION_TERMINAL_EVENT_TYPES), 0),
                        else_=1,
                    ),
                    events_table.c.timestamp.desc(),
                    events_table.c.id.desc(),
                ),
            )
            .label("rn"),
        )
        .where(events_table.c.aggregate_type == "session")
        .where(
            or_(
                events_table.c.event_type.in_(
                    (
                        "orchestrator.session.completed",
                        "orchestrator.session.failed",
                        "orchestrator.session.paused",
                        "orchestrator.session.cancelled",
                    )
                ),
                and_(
                    events_table.c.event_type.in_(
                        ("orchestrator.progress.updated", "workflow.progress.updated")
                    ),
                    status_expr.is_not(None),
                ),
            )
        )
        .subquery()
    )

    try:
        async with engine.begin() as conn:
            query = (
                select(
                    first_session_event_ranked.c.session_id,
                    first_session_event_ranked.c.execution_id,
                    first_session_event_ranked.c.seed_id,
                    first_session_event_ranked.c.start_time,
                    latest_activity_ranked.c.last_activity,
                    latest_status_ranked.c.status_event_type,
                    latest_status_ranked.c.runtime_status,
                )
                .select_from(first_session_event_ranked)
                .join(
                    latest_activity_ranked,
                    and_(
                        latest_activity_ranked.c.session_id
                        == first_session_event_ranked.c.session_id,
                        latest_activity_ranked.c.rn == 1,
                    ),
                )
                .outerjoin(
                    latest_status_ranked,
                    and_(
                        latest_status_ranked.c.session_id
                        == first_session_event_ranked.c.session_id,
                        latest_status_ranked.c.rn == 1,
                    ),
                )
                .where(first_session_event_ranked.c.rn == 1)
                .order_by(first_session_event_ranked.c.session_id.asc())
            )
            rows = (await conn.execute(query)).mappings().all()
            return [
                SessionActivitySnapshot(
                    session_id=row["session_id"],
                    execution_id=row.get("execution_id"),
                    seed_id=row.get("seed_id"),
                    start_time=row.get("start_time"),
                    last_activity=row.get("last_activity"),
                    status_event_type=row.get("status_event_type"),
                    runtime_status=row.get("runtime_status"),
                )
                for row in rows
            ]
    except Exception as e:
        raise PersistenceError(
            f"Failed to fetch session activity snapshots: {e}",
            operation="select",
            table="events",
        ) from e


def _sqlite_json_value(value: Any) -> Any:
    """Model what ``json_extract`` returns for one decoded JSON value."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, dict | list):
        return json.dumps(value, separators=(",", ":"))
    return value


def _json_path(payload: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(payload, dict) or key not in payload:
            return None
        payload = payload[key]
    return _sqlite_json_value(payload)


def _model_status(payload: Any) -> Any:
    progress_status = _json_path(payload, "progress", "runtime_status")
    return progress_status if progress_status is not None else _json_path(payload, "runtime_status")


def _model_snapshots(rows: list[Row]) -> list[SessionActivitySnapshot]:
    """Pure-Python model of the snapshot selection rules over raw event rows."""
    by_session: dict[str, list[Row]] = {}
    for row in rows:
        if row[1] == "session":
            by_session.setdefault(row[2], []).append(row)

    def order(row: Row) -> tuple[Any, str]:
        return (row[5], row[0])

    snapshots = []
    for session_id in sorted(by_session):
        session_rows = by_session[session_id]
        first = min(session_rows, key=order)
        last = max(session_rows, key=order)
        candidates = [
            row
            for row in session_rows
            if row[3] in _SESSION_TERMINAL_EVENT_TYPES
            or row[3] == _PAUSED
            or (row[3] in _PROGRESS and _model_status(row[4]) is not None)
        ]
        terminal = [row for row in candidates if row[3] in _SESSION_TERMINAL_EVENT_TYPES]
        status_row = max(terminal or candidates, key=order) if candidates else None
        start_time = _json_path(first[4], "start_time")
        snapshots.append(
            SessionActivitySnapshot(
                session_id=session_id,
                execution_id=_json_path(first[4], "execution_id"),
                seed_id=_json_path(first[4], "seed_id"),
                start_time=datetime.fromisoformat(start_time or first[5]),
                last_activity=datetime.fromisoformat(last[5]),
                status_event_type=status_row[3] if status_row else None,
                runtime_status=_model_status(status_row[4]) if status_row else None,
            )
        )
    return snapshots


def _typed(snapshots: list[SessionActivitySnapshot]) -> list[tuple[tuple[str, Any], ...]]:
    """Compare values together with their Python types (``1 == True`` is not enough)."""
    return [tuple((type(value).__name__, value) for value in astuple(item)) for item in snapshots]


def _random_rows(rng: random.Random, session_count: int) -> list[Row]:
    # Few distinct timestamps force heavy (timestamp, id) ties.
    stamps = [f"2026-01-01 00:00:0{second}.000000" for second in range(4)]
    stamps.append("2026-01-01T00:00:02+00:00")
    rows: list[Row] = []

    def add(
        aggregate_type: str, session_id: str, event_type: str, payload: Any, stamp: str = ""
    ) -> None:
        event_id = "".join(rng.choice("0123456789abcdef") for _ in range(rng.choice((1, 2, 8))))
        rows.append(
            (
                f"{event_id}-{len(rows)}",
                aggregate_type,
                session_id,
                event_type,
                payload,
                stamp or rng.choice(stamps),
            )
        )

    def payload() -> Any:
        if rng.random() < 0.05:
            return rng.choice(([1, 2], 7, None))
        body: dict[str, Any] = {}
        if rng.random() < 0.5:
            body["execution_id"] = rng.choice(("exec-a", "exec-b", 7, None, True))
        if rng.random() < 0.3:
            body["seed_id"] = rng.choice(("seed-a", None))
        if rng.random() < 0.3:
            body["start_time"] = rng.choice(("2025-06-01T12:00:00+00:00", None))
        roll = rng.random()
        if roll < 0.35:
            body["progress"] = {"runtime_status": rng.choice(_STATUS_VALUES)}
        elif roll < 0.45:
            body["progress"] = rng.choice(("not-an-object", None, {"step": 2}))
        if rng.random() < 0.3:
            body["runtime_status"] = rng.choice(_STATUS_VALUES)
        return body

    for index in range(session_count):
        session_id = f"sess-{index:03d}"
        shape = rng.random()
        if shape < 0.15:
            # Progress rows only, no lifecycle event at all.
            for _ in range(rng.randint(1, 5)):
                add("session", session_id, rng.choice(_PROGRESS), payload())
        elif shape < 0.3:
            # Terminal event followed by later (or tied) progress checkpoints.
            terminal_at = rng.randrange(len(stamps) - 1)
            terminal_type = rng.choice(sorted(_SESSION_TERMINAL_EVENT_TYPES))
            add("session", session_id, terminal_type, payload(), stamps[terminal_at])
            for _ in range(rng.randint(1, 4)):
                late = rng.choice(stamps[terminal_at:])
                add(
                    "session",
                    session_id,
                    rng.choice(_PROGRESS),
                    {"runtime_status": "running"},
                    late,
                )
        else:
            for _ in range(rng.randint(1, 12)):
                add("session", session_id, rng.choice(_EVENT_TYPES), payload())
        # Same aggregate id under other aggregate types must never leak in.
        if rng.random() < 0.4:
            add(rng.choice(("execution", "job")), session_id, rng.choice(_EVENT_TYPES), payload())
    for index in range(3):
        add("execution", f"exec-only-{index}", "orchestrator.session.completed", payload())
    return rows


async def _seeded_store(path: Path, rows: list[Row]) -> EventStore:
    store = EventStore(f"sqlite+aiosqlite:///{path}")
    await store.initialize()
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO events (id, aggregate_type, aggregate_id, event_type, payload, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    *row[:4],
                    row[4] if isinstance(row[4], bytes | str) else json.dumps(row[4]),
                    row[5],
                )
                for row in rows
            ],
        )
        conn.commit()
    finally:
        conn.close()
    return store


def _drop_timestamp_index(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP INDEX ix_events_agg_type_id_timestamp")
        conn.commit()
    finally:
        conn.close()


async def _snapshots_with_vm_steps(
    store: EventStore,
) -> tuple[list[SessionActivitySnapshot], int, str]:
    """Run the real call, counting SQLite VM steps on the connection it uses."""
    engine = store._engine
    assert engine is not None
    steps = 0
    statements: list[str] = []

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    def on_checkout(dbapi_connection: Any, _record: Any, _proxy: Any) -> None:
        dbapi_connection.run_async(lambda driver: driver.set_progress_handler(tick, 1))

    def on_execute(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "checkout", on_checkout)
    event.listen(engine.sync_engine, "before_cursor_execute", on_execute)
    try:
        snapshots = await store.get_session_activity_snapshots()
    finally:
        event.remove(engine.sync_engine, "checkout", on_checkout)
        event.remove(engine.sync_engine, "before_cursor_execute", on_execute)
    assert len(statements) == 1
    return snapshots, steps, statements[0]


def _uniform_rows(session_count: int) -> list[Row]:
    rows: list[Row] = []
    for index in range(session_count):
        session_id = f"sess-{index:05d}"
        for position in range(10):
            if position == 0:
                event_type = "orchestrator.session.started"
            elif position == 9 and index % 2:
                event_type = "orchestrator.session.completed"
            elif position % 3 == 0:
                event_type = "orchestrator.tool.called"
            else:
                event_type = "orchestrator.progress.updated"
            payload = {"execution_id": f"exec-{index}"}
            if position % 2:
                payload["progress"] = {"runtime_status": "running"}
            stamp = f"2026-01-01 {index // 3600:02d}:{index // 60 % 60:02d}:{index % 60:02d}"
            rows.append(
                (
                    f"{index:05d}-{position}",
                    "session",
                    session_id,
                    event_type,
                    payload,
                    f"{stamp}.{position:06d}",
                )
            )
    return rows


class TestSessionActivitySnapshotEquivalence:
    @pytest.mark.parametrize("seed", range(8))
    @pytest.mark.parametrize("timestamp_index", [True, False])
    async def test_matches_legacy_query_and_python_model(
        self, tmp_path: Path, seed: int, timestamp_index: bool
    ) -> None:
        rows = _random_rows(random.Random(seed), session_count=40)
        path = tmp_path / "events.db"
        store = await _seeded_store(path, rows)
        if not timestamp_index:
            # Stores created before ix_events_agg_type_id_timestamp existed
            # keep only ix_events_aggregate_type_id; create_all never adds it.
            _drop_timestamp_index(path)
        try:
            snapshots = await store.get_session_activity_snapshots()
            assert store._engine is not None
            legacy = await _legacy_session_activity_snapshots(store._engine)
        finally:
            await store.close()

        assert len(snapshots) == 40
        assert _typed(snapshots) == _typed(legacy)
        assert _typed(snapshots) == _typed(_model_snapshots(rows))

    async def test_empty_store_returns_no_snapshots(self, tmp_path: Path) -> None:
        store = await _seeded_store(tmp_path / "events.db", [])
        try:
            assert await store.get_session_activity_snapshots() == []
        finally:
            await store.close()


_STARTED: Row = (
    "a",
    "session",
    "s1",
    "orchestrator.session.started",
    {"execution_id": "e"},
    "2026-01-01 00:00:01.000000",
)


class TestSessionActivitySnapshotErrorContract:
    @pytest.mark.parametrize(
        ("rows", "message"),
        [
            pytest.param(
                [
                    _STARTED,
                    (
                        "b",
                        "session",
                        "s1",
                        "orchestrator.tool.called",
                        "{not json",
                        "2026-01-01 00:00:02.000000",
                    ),
                ],
                "malformed JSON",
                id="malformed-payload-on-unselected-session-row",
            ),
            pytest.param(
                [
                    _STARTED,
                    (
                        "b",
                        "session",
                        "s1",
                        "orchestrator.tool.called",
                        b"\x00garbage",
                        "2026-01-01 00:00:02.000000",
                    ),
                ],
                "malformed JSON",
                id="blob-payload-on-unselected-session-row",
            ),
            pytest.param(
                [
                    (
                        "a",
                        "session",
                        "s1",
                        "orchestrator.session.started",
                        {"start_time": "not a date"},
                        "2026-01-01 00:00:01.000000",
                    )
                ],
                "Failed to fetch session activity snapshots: Invalid isoformat string: 'not a date'",
                id="unparseable-start-time",
            ),
            pytest.param(
                [
                    (
                        "a",
                        "session",
                        "s1",
                        "orchestrator.session.started",
                        {"start_time": 12345},
                        "2026-01-01 00:00:01.000000",
                    )
                ],
                "Failed to fetch session activity snapshots: fromisoformat: argument must be str",
                id="non-string-start-time",
            ),
        ],
    )
    async def test_failures_match_legacy_query(
        self, tmp_path: Path, rows: list[Row], message: str
    ) -> None:
        store = await _seeded_store(tmp_path / "events.db", rows)
        assert store._engine is not None
        try:
            with pytest.raises(PersistenceError) as current:
                await store.get_session_activity_snapshots()
            with pytest.raises(PersistenceError) as legacy:
                await _legacy_session_activity_snapshots(store._engine)
        finally:
            await store.close()

        assert message in str(current.value)
        assert message in str(legacy.value)
        if "malformed" not in message:
            # Python-side conversion errors carry no SQL text, so they match exactly.
            assert str(current.value) == str(legacy.value)

    async def test_malformed_payload_outside_session_aggregate_is_ignored(
        self, tmp_path: Path
    ) -> None:
        rows = [
            _STARTED,
            (
                "b",
                "execution",
                "s1",
                "workflow.progress.updated",
                "{not json",
                "2026-01-01 00:00:02.000000",
            ),
        ]
        store = await _seeded_store(tmp_path / "events.db", rows)
        assert store._engine is not None
        try:
            snapshots = await store.get_session_activity_snapshots()
            legacy = await _legacy_session_activity_snapshots(store._engine)
        finally:
            await store.close()

        assert _typed(snapshots) == _typed(legacy)
        assert [item.execution_id for item in snapshots] == ["e"]


class TestSessionActivitySnapshotCost:
    @pytest.mark.parametrize("timestamp_index", [True, False])
    async def test_vm_steps_grow_linearly_with_session_count(
        self, tmp_path: Path, timestamp_index: bool
    ) -> None:
        steps: list[int] = []
        for session_count in (50, 200):
            path = tmp_path / f"events-{session_count}.db"
            store = await _seeded_store(path, _uniform_rows(session_count))
            if not timestamp_index:
                _drop_timestamp_index(path)
            try:
                snapshots, vm_steps, _statement = await _snapshots_with_vm_steps(store)
            finally:
                await store.close()
            assert len(snapshots) == session_count
            steps.append(vm_steps)

        # 4x the sessions (same rows per session) must cost about 4x the VM
        # steps. The replaced window/nested-scan query grew ~quadratically here.
        assert steps[0] > 0
        assert steps[1] < 5 * steps[0], steps

    @pytest.mark.parametrize(
        ("timestamp_index", "probe_index"),
        [
            (True, "ix_events_agg_type_id_timestamp"),
            (False, "ix_events_aggregate_type_id"),
        ],
    )
    async def test_per_session_probes_use_aggregate_index(
        self, tmp_path: Path, timestamp_index: bool, probe_index: str
    ) -> None:
        path = tmp_path / "events.db"
        store = await _seeded_store(path, _uniform_rows(20))
        if not timestamp_index:
            _drop_timestamp_index(path)
        try:
            _snapshots, _steps, statement = await _snapshots_with_vm_steps(store)
        finally:
            await store.close()

        conn = sqlite3.connect(path)
        try:
            plan = [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {statement}")]
        finally:
            conn.close()
        probe = f"SEARCH e USING INDEX {probe_index} (aggregate_type=? AND aggregate_id=?)"
        assert sum(detail == probe for detail in plan) == 4, plan
        assert not [
            detail for detail in plan if detail.startswith("SCAN") and detail != "SCAN s"
        ], plan
