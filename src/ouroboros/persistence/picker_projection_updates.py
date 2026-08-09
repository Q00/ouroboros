"""Same-transaction application updates for optional picker projections."""

from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache
import json
from typing import Any

from sqlalchemy import Column, Integer, MetaData, literal_column

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.picker_indexes import (
    PICKER_PROGRESS_EVENT_TYPES,
    PICKER_PROGRESS_TABLE,
    PICKER_PROJECTION_VERSION,
    PICKER_START_EXECUTION_ID_SQL,
    PICKER_START_SESSION_ID_SQL,
    PICKER_START_SESSION_TABLE,
    PICKER_START_TABLE,
    RUNNING_PROGRESS_SQL,
    RUNTIME_STATUS_ASCII_WHITESPACE,
    VALID_JSON_SQL,
    WORKFLOW_PROGRESS_SCOPE_SQL,
    WORKFLOW_SNAPSHOT_SQL,
)
from ouroboros.persistence.schema import events_table

_START_EVENT_TYPE = "orchestrator.session.started"
_RELEVANT_TYPES = frozenset((*PICKER_PROGRESS_EVENT_TYPES, _START_EVENT_TYPE))
_write_metadata = MetaData()
_fenced_event_writes = events_table.to_metadata(_write_metadata)
_fenced_event_writes.append_column(Column("picker_projection_version", Integer))


_PROGRESS_UPSERT_SQL = (
    f"INSERT INTO {PICKER_PROGRESS_TABLE} ("
    "aggregate_id, event_type, latest_valid_rowid, latest_running_rowid, "
    "latest_snapshot_rowid) VALUES (?, ?, ?, ?, ?) "
    "ON CONFLICT (aggregate_id, event_type) DO UPDATE SET "
    "latest_valid_rowid = CASE "
    "WHEN excluded.latest_valid_rowid > latest_valid_rowid "
    "THEN excluded.latest_valid_rowid ELSE latest_valid_rowid END, "
    "latest_running_rowid = CASE "
    "WHEN excluded.latest_running_rowid IS NULL THEN latest_running_rowid "
    "WHEN latest_running_rowid IS NULL "
    "OR excluded.latest_running_rowid > latest_running_rowid "
    "THEN excluded.latest_running_rowid ELSE latest_running_rowid END, "
    "latest_snapshot_rowid = CASE "
    "WHEN excluded.latest_snapshot_rowid IS NULL THEN latest_snapshot_rowid "
    "WHEN latest_snapshot_rowid IS NULL "
    "OR excluded.latest_snapshot_rowid > latest_snapshot_rowid "
    "THEN excluded.latest_snapshot_rowid ELSE latest_snapshot_rowid END"
)


def _nonblank_text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip(RUNTIME_STATUS_ASCII_WHITESPACE):
        return None
    return value


def _start_projection(event: BaseEvent, rowid: int) -> tuple[int, str | None, str | None]:
    return (
        rowid,
        _nonblank_text(event.data.get("execution_id")),
        _nonblank_text(event.aggregate_id),
    )


@lru_cache(maxsize=32)
def _start_values_sql(table: str, prefix: str, row_count: int) -> str:
    values = ",".join("(?, ?, ?)" for _ in range(row_count))
    return f"{prefix} INTO {table} VALUES {values}"


async def _write_start_batch(
    conn: Any,
    table: str,
    prefix: str,
    rows: Sequence[tuple[object, object, object]],
) -> None:
    # 5,000 rows stay below SQLite's long-standing 32,766 bind limit while
    # avoiding the JSON encode/parse loop and per-row executemany VM setup.
    chunk_size = 5_000
    for offset in range(0, len(rows), chunk_size):
        chunk = rows[offset : offset + chunk_size]
        parameters = tuple(value for row in chunk for value in row)
        await conn.exec_driver_sql(
            _start_values_sql(table, prefix, len(chunk)),
            parameters,
        )


async def _write_start_rows(
    conn: Any,
    starts: Sequence[tuple[int, str | None, str | None]],
) -> None:
    if not starts:
        return
    if len(starts) == 1:
        await conn.exec_driver_sql(
            f"INSERT INTO {PICKER_START_TABLE} "
            "(event_rowid, execution_id, session_id) VALUES (?, ?, ?)",
            starts[0],
        )
    else:
        await _write_start_batch(
            conn,
            PICKER_START_TABLE,
            "INSERT",
            starts,
        )
    session_rows: dict[tuple[str, str], int] = {}
    for event_rowid, execution_id, session_id in starts:
        if session_id is None:
            continue
        key = (session_id, execution_id or "")
        prior = session_rows.get(key)
        if prior is None or event_rowid < prior:
            session_rows[key] = event_rowid
    if session_rows:
        await _write_start_batch(
            conn,
            PICKER_START_SESSION_TABLE,
            "INSERT OR IGNORE",
            tuple(
                (session_id, execution_id, event_rowid)
                for (session_id, execution_id), event_rowid in session_rows.items()
            ),
        )


async def _write_projection_rows(conn: Any, rows: Sequence[Any]) -> None:
    starts: list[tuple[int, str | None, str | None]] = []
    heads: dict[tuple[str, str], list[int | None]] = {}
    for row in rows:
        rowid = int(row[0])
        event_type = str(row[2])
        if event_type == _START_EVENT_TYPE:
            starts.append((rowid, row[6], row[7]))
            continue
        if not bool(row[3]):
            continue
        key = (str(row[1]), event_type)
        head = heads.setdefault(key, [rowid, None, None])
        head[0] = max(int(head[0]), rowid)
        if bool(row[4]):
            head[1] = rowid if head[1] is None else max(int(head[1]), rowid)
        if bool(row[5]):
            head[2] = rowid if head[2] is None else max(int(head[2]), rowid)
    await _write_start_rows(conn, starts)
    if heads:
        parameters = [
            (aggregate_id, event_type, values[0], values[1], values[2])
            for (aggregate_id, event_type), values in heads.items()
        ]
        await conn.exec_driver_sql(_PROGRESS_UPSERT_SQL, parameters)


async def _project_inserted_ids(conn: Any, event_ids: Sequence[str]) -> None:
    rows = (
        await conn.exec_driver_sql(
            "SELECT events.rowid, events.aggregate_id, events.event_type, "
            f"{VALID_JSON_SQL} AS is_valid, {RUNNING_PROGRESS_SQL} AS is_running, "
            f"({WORKFLOW_PROGRESS_SCOPE_SQL} AND {WORKFLOW_SNAPSHOT_SQL}) AS is_snapshot, "
            f"{PICKER_START_EXECUTION_ID_SQL} AS start_execution_id, "
            f"{PICKER_START_SESSION_ID_SQL} AS start_session_id "
            "FROM json_each(?) AS requested "
            "JOIN events ON events.id = requested.value",
            (json.dumps(event_ids),),
        )
    ).fetchall()
    if len(rows) != len(event_ids):
        raise RuntimeError("Event batch projection lookup did not resolve every relevant event.")
    await _write_projection_rows(conn, rows)


async def _project_inserted_range(conn: Any, first_rowid: int, last_rowid: int) -> None:
    rows = (
        await conn.exec_driver_sql(
            "SELECT rowid, aggregate_id, event_type, "
            f"{VALID_JSON_SQL} AS is_valid, {RUNNING_PROGRESS_SQL} AS is_running, "
            f"({WORKFLOW_PROGRESS_SCOPE_SQL} AND {WORKFLOW_SNAPSHOT_SQL}) AS is_snapshot, "
            f"{PICKER_START_EXECUTION_ID_SQL} AS start_execution_id, "
            f"{PICKER_START_SESSION_ID_SQL} AS start_session_id "
            "FROM events WHERE rowid BETWEEN ? AND ? "
            "AND event_type IN (?, ?, ?)",
            (first_rowid, last_rowid, _START_EVENT_TYPE, *PICKER_PROGRESS_EVENT_TYPES),
        )
    ).fetchall()
    if len(rows) != last_rowid - first_rowid + 1:
        raise RuntimeError("Dense event batch projection range was not contiguous.")
    await _write_projection_rows(conn, rows)


def _projected_values(event: BaseEvent) -> dict[str, Any]:
    values = event.to_db_dict()
    values["picker_projection_version"] = (
        PICKER_PROJECTION_VERSION if event.type in _RELEVANT_TYPES else None
    )
    return values


async def insert_event_with_picker_projection(
    conn: Any,
    event: BaseEvent,
    projection_ready: bool,
) -> None:
    """Insert one canonical event and update relevant picker keys atomically."""
    relevant = event.type in _RELEVANT_TYPES
    statement = events_table.insert().values(**event.to_db_dict())
    if not relevant:
        await conn.execute(statement)
        return
    if not projection_ready:
        await conn.execute(statement)
        return
    statement = _fenced_event_writes.insert().values(**_projected_values(event))
    rowid = int((await conn.execute(statement.returning(literal_column("rowid")))).scalar_one())
    if event.type == _START_EVENT_TYPE:
        await _write_start_rows(conn, (_start_projection(event, rowid),))
        return
    await _project_inserted_range(conn, rowid, rowid)


async def insert_events_with_picker_projection(
    conn: Any,
    events: Sequence[BaseEvent],
    projection_ready: bool,
) -> None:
    """Insert one batch while keeping every relevant projection in its transaction."""
    relevant = [event for event in events if event.type in _RELEVANT_TYPES]
    statement = events_table.insert()
    if not relevant:
        await conn.execute(statement, [event.to_db_dict() for event in events])
        return
    if not projection_ready:
        await conn.execute(statement, [event.to_db_dict() for event in events])
        return
    projected_statement = _fenced_event_writes.insert()
    projected_values = [_projected_values(event) for event in events]
    if len(relevant) != len(events):
        await conn.execute(projected_statement, projected_values)
        relevant_ids = [event.id for event in relevant]
        await _project_inserted_ids(conn, relevant_ids)
        return
    if all(event.type == _START_EVENT_TYPE for event in events):
        result = await conn.execute(projected_statement, projected_values)
        if result.rowcount != len(events):
            raise RuntimeError("Event batch INSERT did not write every requested start event.")
        last_rowid = int((await conn.exec_driver_sql("SELECT last_insert_rowid()")).scalar_one())
        first_rowid = last_rowid - len(events) + 1
        if first_rowid <= 0:
            raise RuntimeError("Start batch INSERT did not expose a valid SQLite rowid range.")
        boundary_rows = (
            await conn.exec_driver_sql(
                "SELECT events.id, events.rowid FROM json_each(?) AS requested "
                "JOIN events ON events.id = requested.value",
                (json.dumps([events[0].id, events[-1].id]),),
            )
        ).fetchall()
        boundaries = {str(row[0]): int(row[1]) for row in boundary_rows}
        if boundaries != {events[0].id: first_rowid, events[-1].id: last_rowid}:
            raise RuntimeError("Start batch INSERT rowids were not one contiguous SQLite range.")
        await _write_start_rows(
            conn,
            tuple(
                _start_projection(event, rowid)
                for event, rowid in zip(
                    events,
                    range(first_rowid, last_rowid + 1),
                    strict=True,
                )
            ),
        )
        return
    raw_rowids = list(
        (
            await conn.execute(
                projected_statement.returning(literal_column("rowid")), projected_values
            )
        ).scalars()
    )
    if len(raw_rowids) != len(events) or not all(isinstance(rowid, int) for rowid in raw_rowids):
        raise RuntimeError("Event batch INSERT did not return one integer rowid per event.")
    rowids = [int(rowid) for rowid in raw_rowids]
    if max(rowids) - min(rowids) + 1 != len(rowids):
        raise RuntimeError("Event batch INSERT rowids were not one contiguous SQLite range.")
    await _project_inserted_range(conn, min(rowids), max(rowids))


__all__ = ["insert_event_with_picker_projection", "insert_events_with_picker_projection"]
