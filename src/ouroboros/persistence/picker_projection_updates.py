"""Same-transaction application updates for optional picker projections."""

from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from sqlalchemy import Column, Integer, MetaData, literal_column

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.picker_indexes import (
    PICKER_PROGRESS_EVENT_TYPES,
    PICKER_PROGRESS_TABLE,
    PICKER_PROJECTION_VERSION,
    PICKER_START_TABLE,
    RUNNING_PROGRESS_SQL,
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


async def _write_start_rows(conn: Any, rowids: Sequence[int]) -> None:
    if not rowids:
        return
    if len(rowids) == 1:
        await conn.exec_driver_sql(
            f"INSERT INTO {PICKER_START_TABLE} (event_rowid) VALUES (?)",
            (rowids[0],),
        )
        return
    await conn.exec_driver_sql(
        f"INSERT INTO {PICKER_START_TABLE} (event_rowid) "
        "SELECT CAST(value AS INTEGER) FROM json_each(?)",
        (json.dumps(rowids),),
    )


async def _write_projection_rows(conn: Any, rows: Sequence[Any]) -> None:
    starts: list[int] = []
    heads: dict[tuple[str, str], list[int | None]] = {}
    for row in rows:
        rowid = int(row[0])
        event_type = str(row[2])
        if event_type == _START_EVENT_TYPE:
            starts.append(rowid)
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
            f"({WORKFLOW_PROGRESS_SCOPE_SQL} AND {WORKFLOW_SNAPSHOT_SQL}) AS is_snapshot "
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
            f"({WORKFLOW_PROGRESS_SCOPE_SQL} AND {WORKFLOW_SNAPSHOT_SQL}) AS is_snapshot "
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
        await _write_start_rows(conn, (rowid,))
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
    values = [event.to_db_dict() for event in events]
    if not relevant:
        await conn.execute(statement, values)
        return
    if not projection_ready:
        await conn.execute(statement, values)
        return
    projected_statement = _fenced_event_writes.insert()
    projected_values = [_projected_values(event) for event in events]
    if len(relevant) != len(events):
        await conn.execute(projected_statement, projected_values)
        relevant_ids = [event.id for event in relevant]
        await _project_inserted_ids(conn, relevant_ids)
        return
    raw_rowids = list(
        (
            await conn.execute(
                projected_statement.returning(literal_column("rowid")), projected_values
            )
        ).scalars()
    )
    if len(raw_rowids) != len(values) or not all(isinstance(rowid, int) for rowid in raw_rowids):
        raise RuntimeError("Event batch INSERT did not return one integer rowid per event.")
    rowids = [int(rowid) for rowid in raw_rowids]
    if max(rowids) - min(rowids) + 1 != len(rowids):
        raise RuntimeError("Event batch INSERT rowids were not one contiguous SQLite range.")
    if all(event.type == _START_EVENT_TYPE for event in events):
        await _write_start_rows(conn, rowids)
        return
    await _project_inserted_range(conn, min(rowids), max(rowids))


__all__ = ["insert_event_with_picker_projection", "insert_events_with_picker_projection"]
