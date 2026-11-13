"""Typed ``query_execution_related_events`` queries use the event-type index.

Without ``sqlite_stat1`` SQLite plans ``aggregate_type = 'execution'`` through
an aggregate index and scans every execution row; a no-op unary ``+`` on that
column lets a typed query seek ``ix_events_event_type`` instead. These tests pin
the rows and their order to the previous query (kept below as a private
reference), check the plan, and prove the saving by counting SQLite VM steps.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import random
import sqlite3
from typing import Any

import pytest
from sqlalchemy import and_, event, func, or_, select
from sqlalchemy.ext.asyncio import AsyncEngine

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import events_table

Row = tuple[str, str, str, str, dict[str, Any], str]

_EXECUTION_ID = "exec-target"
_EVENT_TYPES = (
    "execution.session.completed",
    "execution.ac.attempt_judged",
    "execution.tool.started",
    "workflow.progress.updated",
)
_STAMPS = tuple(f"2026-01-01 00:00:0{second}.000000" for second in range(5))


async def _legacy_query_execution_related_events(
    engine: AsyncEngine,
    execution_id: str,
    event_type: str | None = None,
    limit: int | None = 50,
    offset: int = 0,
    payload_equals: dict[str, str | int | float | bool | None] | None = None,
    *,
    chronological: bool = False,
    cursor_after: tuple[datetime, str] | None = None,
    cursor_through: tuple[datetime, str] | None = None,
) -> list[BaseEvent]:
    """``EventStore.query_execution_related_events`` as it was before this change."""
    conditions = [
        events_table.c.aggregate_id == execution_id,
        func.json_extract(events_table.c.payload, "$.execution_id") == execution_id,
        func.json_extract(events_table.c.payload, "$.parent_execution_id") == execution_id,
    ]

    try:
        async with engine.begin() as conn:
            query = (
                select(events_table)
                .where(events_table.c.aggregate_type == "execution")
                .where(or_(*conditions))
            )

            query = query.order_by(
                *(
                    (events_table.c.timestamp.asc(), events_table.c.id.asc())
                    if chronological
                    else (events_table.c.timestamp.desc(), events_table.c.id.desc())
                )
            )

            if event_type:
                query = query.where(events_table.c.event_type == event_type)

            if cursor_after is not None or cursor_through is not None:
                if not chronological:
                    raise ValueError("execution event cursors require chronological order")
                for cursor_name, cursor in (
                    ("cursor_after", cursor_after),
                    ("cursor_through", cursor_through),
                ):
                    if cursor is not None and (
                        type(cursor) is not tuple
                        or len(cursor) != 2
                        or not isinstance(cursor[0], datetime)
                        or type(cursor[1]) is not str
                        or not cursor[1]
                    ):
                        raise ValueError(f"{cursor_name} is invalid")
                if cursor_after is not None:
                    after_timestamp, after_id = cursor_after
                    query = query.where(
                        or_(
                            events_table.c.timestamp > after_timestamp,
                            and_(
                                events_table.c.timestamp == after_timestamp,
                                events_table.c.id > after_id,
                            ),
                        )
                    )
                if cursor_through is not None:
                    through_timestamp, through_id = cursor_through
                    query = query.where(
                        or_(
                            events_table.c.timestamp < through_timestamp,
                            and_(
                                events_table.c.timestamp == through_timestamp,
                                events_table.c.id <= through_id,
                            ),
                        )
                    )

            if payload_equals:
                if type(payload_equals) is not dict or len(payload_equals) > 8:
                    raise ValueError(
                        "execution-related payload filters require a bounded built-in dict"
                    )
                for field, value in payload_equals.items():
                    if (
                        type(field) is not str
                        or not field
                        or not field.isascii()
                        or (not field[0].isalpha() and field[0] != "_")
                        or not field.replace("_", "").isalnum()
                        or (value is not None and type(value) not in {str, int, float, bool})
                    ):
                        raise ValueError(
                            "execution-related payload filters require "
                            "top-level ASCII identifier keys and JSON scalar values"
                        )
                    query = query.where(
                        func.json_extract(events_table.c.payload, f"$.{field}") == value
                    )

            if limit is not None:
                query = query.limit(limit).offset(offset)
            elif offset:
                query = query.offset(offset)

            result = await conn.execute(query)
            rows = result.mappings().all()
            return [BaseEvent.from_db_row(dict(row)) for row in rows]
    except Exception as e:
        raise PersistenceError(
            f"Failed to query execution-related events: {e}",
            operation="select",
            table="events",
            details={
                "execution_id": execution_id,
                "event_type": event_type,
                "limit": limit,
                "offset": offset,
                "payload_equals": dict(payload_equals or {}),
                "chronological": chronological,
                "cursor_after": cursor_after,
                "cursor_through": cursor_through,
            },
        ) from e


def _random_rows(rng: random.Random, count: int) -> list[Row]:
    rows: list[Row] = []
    for index in range(count):
        aggregate_type = rng.choice(("execution",) * 6 + ("session", "job"))
        aggregate_id = rng.choice((_EXECUTION_ID, f"{_EXECUTION_ID}_ac_1", "exec-other", "orch-1"))
        payload: dict[str, Any] = {}
        if rng.random() < 0.5:
            payload["execution_id"] = rng.choice((_EXECUTION_ID, "exec-other"))
        if rng.random() < 0.2:
            payload["parent_execution_id"] = rng.choice((_EXECUTION_ID, "exec-other"))
        if rng.random() < 0.6:
            payload["session_id"] = rng.choice(("orch-1", "orch-2"))
        if rng.random() < 0.4:
            payload["route_contract_version"] = rng.choice((1, 2))
        event_id = "".join(rng.choice("0123456789abcdef") for _ in range(rng.choice((1, 2, 8))))
        rows.append(
            (
                f"{event_id}-{index}",
                aggregate_type,
                aggregate_id,
                rng.choice(_EVENT_TYPES),
                payload,
                rng.choice(_STAMPS),
            )
        )
    return rows


async def _seeded_store(path: Path, rows: list[Row]) -> EventStore:
    store = EventStore(f"sqlite+aiosqlite:///{path}")
    await store.initialize()
    conn = sqlite3.connect(path)
    try:
        conn.executemany(
            "INSERT INTO events (id, aggregate_type, aggregate_id, event_type, payload, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [(*row[:4], json.dumps(row[4]), row[5]) for row in rows],
        )
        conn.commit()
    finally:
        conn.close()
    return store


def _drop_index(path: Path, name: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"DROP INDEX {name}")
        conn.commit()
    finally:
        conn.close()


def _query_cases(rows: list[Row]) -> list[dict[str, Any]]:
    ordered = sorted((row[5], row[0]) for row in rows)
    after = (datetime.fromisoformat(ordered[len(ordered) // 4][0]), ordered[len(ordered) // 4][1])
    through = (
        datetime.fromisoformat(ordered[3 * len(ordered) // 4][0]),
        ordered[3 * len(ordered) // 4][1],
    )
    cases: list[dict[str, Any]] = []
    for event_type in (*_EVENT_TYPES, "execution.absent", None):
        for limit, offset in ((None, 0), (1, 0), (3, 2), (50, 0)):
            for payload_equals in (None, {"session_id": "orch-1"}, {"route_contract_version": 1}):
                cases.append(
                    {
                        "event_type": event_type,
                        "limit": limit,
                        "offset": offset,
                        "payload_equals": payload_equals,
                    }
                )
                cases.append(
                    {
                        "event_type": event_type,
                        "limit": limit,
                        "offset": offset,
                        "payload_equals": payload_equals,
                        "chronological": True,
                        "cursor_after": after,
                        "cursor_through": through,
                    }
                )
    return cases


async def _vm_steps(store: EventStore, **kwargs: Any) -> tuple[list[BaseEvent], int, str, Any]:
    """Run the real query, counting SQLite VM steps on the connection it uses."""
    engine = store._engine
    assert engine is not None
    steps = 0
    statements: list[tuple[str, Any]] = []

    def tick() -> int:
        nonlocal steps
        steps += 1
        return 0

    def on_checkout(dbapi_connection: Any, _record: Any, _proxy: Any) -> None:
        dbapi_connection.run_async(lambda driver: driver.set_progress_handler(tick, 1))

    def on_execute(_conn: Any, _cursor: Any, statement: str, parameters: Any, *_args: Any) -> None:
        statements.append((statement, parameters))

    event.listen(engine.sync_engine, "checkout", on_checkout)
    event.listen(engine.sync_engine, "before_cursor_execute", on_execute)
    try:
        events = await store.query_execution_related_events(_EXECUTION_ID, **kwargs)
    finally:
        event.remove(engine.sync_engine, "checkout", on_checkout)
        event.remove(engine.sync_engine, "before_cursor_execute", on_execute)
    assert len(statements) == 1
    return events, steps, *statements[0]


def _plan(path: Path, statement: str, parameters: Any) -> list[str]:
    conn = sqlite3.connect(path)
    try:
        return [row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {statement}", parameters)]
    finally:
        conn.close()


class TestTypedExecutionQueryEquivalence:
    @pytest.mark.parametrize("seed", range(4))
    @pytest.mark.parametrize("missing_index", [None, "ix_events_event_type"])
    async def test_rows_and_order_match_legacy_query(
        self, tmp_path: Path, seed: int, missing_index: str | None
    ) -> None:
        rows = _random_rows(random.Random(seed), 300)
        path = tmp_path / "events.db"
        store = await _seeded_store(path, rows)
        if missing_index:
            # No ``INDEXED BY``: the query must keep working on a store without it.
            _drop_index(path, missing_index)
        assert store._engine is not None
        try:
            for case in _query_cases(rows):
                current = await store.query_execution_related_events(_EXECUTION_ID, **case)
                legacy = await _legacy_query_execution_related_events(
                    store._engine, _EXECUTION_ID, **case
                )
                assert [item.id for item in current] == [item.id for item in legacy], case
                assert current == legacy, case
        finally:
            await store.close()

    async def test_invalid_cursor_error_is_unchanged(self, tmp_path: Path) -> None:
        store = await _seeded_store(tmp_path / "events.db", _random_rows(random.Random(0), 10))
        assert store._engine is not None
        invalid_cursor = (datetime(2026, 1, 1), "")
        try:
            with pytest.raises(PersistenceError) as current:
                await store.query_execution_related_events(
                    _EXECUTION_ID, event_type=_EVENT_TYPES[0], cursor_after=invalid_cursor
                )
            with pytest.raises(PersistenceError) as legacy:
                await _legacy_query_execution_related_events(
                    store._engine,
                    _EXECUTION_ID,
                    event_type=_EVENT_TYPES[0],
                    cursor_after=invalid_cursor,
                )
        finally:
            await store.close()
        assert str(current.value) == str(legacy.value)


class TestTypedExecutionQueryPlan:
    async def test_typed_query_seeks_event_type_index(self, tmp_path: Path) -> None:
        path = tmp_path / "events.db"
        store = await _seeded_store(path, _random_rows(random.Random(1), 200))
        try:
            _events, _steps, typed, typed_params = await _vm_steps(
                store, event_type="execution.session.completed", limit=None
            )
            _events, _steps, untyped, untyped_params = await _vm_steps(store, limit=None)
        finally:
            await store.close()

        assert "+events.aggregate_type" in typed
        assert "SEARCH events USING INDEX ix_events_event_type (event_type=?)" in _plan(
            path, typed, typed_params
        )
        # Without an event type the predicate and plan are left as they were.
        assert "+events.aggregate_type" not in untyped
        assert not any(
            "ix_events_event_type" in line for line in _plan(path, untyped, untyped_params)
        )

    async def test_rare_type_cost_is_independent_of_other_execution_rows(
        self, tmp_path: Path
    ) -> None:
        steps: list[int] = []
        for noise in (500, 2000):
            rows: list[Row] = [
                (
                    f"noise-{index:05d}",
                    "execution",
                    _EXECUTION_ID,
                    "execution.tool.started",
                    {},
                    _STAMPS[1],
                )
                for index in range(noise)
            ]
            rows += [
                (
                    f"rare-{index}",
                    "execution",
                    _EXECUTION_ID,
                    "execution.session.completed",
                    {},
                    _STAMPS[2],
                )
                for index in range(3)
            ]
            store = await _seeded_store(tmp_path / f"events-{noise}.db", rows)
            try:
                events, vm_steps, _statement, _params = await _vm_steps(
                    store, event_type="execution.session.completed", limit=None
                )
            finally:
                await store.close()
            assert [item.id for item in events] == ["rare-2", "rare-1", "rare-0"]
            steps.append(vm_steps)

        # 4x the unrelated execution rows must not cost more steps for a rare
        # type. Scanning every execution row grew ~4x here.
        assert steps[0] > 0
        assert steps[1] < 1.2 * steps[0], steps
