"""Best-effort writable provisioning for dashboard picker projections."""

from __future__ import annotations

import logging

from sqlalchemy.engine import Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from ouroboros.persistence.picker_indexes import (
    DIRECT_EVENT_INDEX,
    DIRECT_EVENT_INDEX_DDL,
    OBSOLETE_PICKER_INDEX_NAMES,
    PICKER_CONTRACT_DDL_BY_NAME,
    PICKER_CONTRACT_NAMES,
    PICKER_GAP_INDEX,
    PICKER_GAP_INDEX_DDL,
    PICKER_META_TABLE,
    PICKER_META_TABLE_DDL,
    PICKER_PROGRESS_SCOPE_SQL,
    PICKER_PROGRESS_TABLE,
    PICKER_PROGRESS_TABLE_DDL,
    PICKER_PROJECTION_SCOPE_SQL,
    PICKER_PROJECTION_VERSION,
    PICKER_START_EXECUTION_ID_SQL,
    PICKER_START_EXECUTION_INDEX,
    PICKER_START_EXECUTION_INDEX_DDL,
    PICKER_START_SCOPE_SQL,
    PICKER_START_SESSION_ID_SQL,
    PICKER_START_SESSION_TABLE,
    PICKER_START_SESSION_TABLE_DDL,
    PICKER_START_TABLE,
    PICKER_START_TABLE_DDL,
    RUNNING_PROGRESS_SQL,
    VALID_JSON_SQL,
    WORKFLOW_SNAPSHOT_SQL,
    normalize_schema_ddl,
)


def _installed_contract(connection: Connection) -> dict[str, tuple[str, str, str | None]]:
    placeholders = ",".join("?" for _ in PICKER_CONTRACT_NAMES)
    rows = connection.exec_driver_sql(
        f"SELECT name, type, sql FROM sqlite_master WHERE lower(name) IN ({placeholders})",
        PICKER_CONTRACT_NAMES,
    ).fetchall()
    return {
        str(row[0]).lower(): (str(row[0]), str(row[1]), row[2] if isinstance(row[2], str) else None)
        for row in rows
    }


def _table_columns(connection: Connection, table: str) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (row[1], row[2], int(row[3]), int(row[5]), int(row[6]))
        for row in connection.exec_driver_sql(f'PRAGMA table_xinfo("{table}")').fetchall()
    )


def picker_contract_is_complete(connection: Connection) -> bool:
    installed = _installed_contract(connection)
    for name, expected_sql in PICKER_CONTRACT_DDL_BY_NAME.items():
        actual = installed.get(name)
        if actual is None or actual[2] is None:
            return False
        if normalize_schema_ddl(actual[2]) != normalize_schema_ddl(expected_sql):
            return False

    if _table_columns(connection, PICKER_START_TABLE) != (
        ("event_rowid", "INTEGER", 0, 1, 0),
        ("execution_id", "TEXT", 0, 0, 0),
        ("session_id", "TEXT", 0, 0, 0),
    ):
        return False
    if _table_columns(connection, PICKER_START_SESSION_TABLE) != (
        ("session_id", "TEXT", 1, 1, 0),
        ("execution_id", "TEXT", 1, 2, 0),
        ("event_rowid", "INTEGER", 1, 0, 0),
    ):
        return False
    if _table_columns(connection, PICKER_PROGRESS_TABLE) != (
        ("aggregate_id", "TEXT", 1, 1, 0),
        ("event_type", "TEXT", 1, 2, 0),
        ("latest_valid_rowid", "INTEGER", 1, 0, 0),
        ("latest_running_rowid", "INTEGER", 0, 0, 0),
        ("latest_snapshot_rowid", "INTEGER", 0, 0, 0),
    ):
        return False
    if _table_columns(connection, PICKER_META_TABLE) != (
        ("contract_version", "INTEGER", 0, 1, 0),
        ("backfilled_through_rowid", "INTEGER", 1, 0, 0),
    ):
        return False
    projection_column = next(
        (
            row
            for row in connection.exec_driver_sql('PRAGMA table_xinfo("events")').fetchall()
            if row[1] == "picker_projection_version"
        ),
        None,
    )
    if projection_column is None or (
        projection_column[2],
        int(projection_column[3]),
        projection_column[4],
        int(projection_column[5]),
        int(projection_column[6]),
    ) != ("INTEGER", 0, None, 0, 0):
        return False
    expected_index_keys = {
        DIRECT_EVENT_INDEX: (None, "event_type"),
        PICKER_GAP_INDEX: ("event_type",),
        PICKER_START_EXECUTION_INDEX: ("execution_id",),
    }
    for name, expected_keys in expected_index_keys.items():
        key_columns = tuple(
            row[2]
            for row in connection.exec_driver_sql(f'PRAGMA index_xinfo("{name}")').fetchall()
            if int(row[5]) == 1
        )
        if key_columns != expected_keys:
            return False
    marker = connection.exec_driver_sql(
        f"SELECT contract_version, backfilled_through_rowid, "
        f"typeof(contract_version), typeof(backfilled_through_rowid) "
        f"FROM {PICKER_META_TABLE}"
    ).fetchall()
    return (
        len(marker) == 1
        and marker[0][0] == PICKER_PROJECTION_VERSION
        and marker[0][2] == "integer"
        and marker[0][3] == "integer"
        and isinstance(marker[0][1], int)
        and int(marker[0][1]) >= 0
    )


def _drop_object(connection: Connection, name: str, object_type: str) -> None:
    keyword = {
        "index": "INDEX",
        "table": "TABLE",
        "trigger": "TRIGGER",
        "view": "VIEW",
    }.get(object_type)
    if keyword is not None:
        connection.exec_driver_sql(f'DROP {keyword} "{name}"')


def _drop_obsolete_indexes(connection: Connection) -> None:
    placeholders = ",".join("?" for _ in OBSOLETE_PICKER_INDEX_NAMES)
    rows = connection.exec_driver_sql(
        f"SELECT name FROM sqlite_master WHERE type = 'index' AND lower(name) IN ({placeholders})",
        OBSOLETE_PICKER_INDEX_NAMES,
    ).fetchall()
    for row in rows:
        connection.exec_driver_sql(f'DROP INDEX "{row[0]}"')


def _ensure_events_projection_column(connection: Connection) -> None:
    columns = {
        str(row[1]) for row in connection.exec_driver_sql('PRAGMA table_info("events")').fetchall()
    }
    if "picker_projection_version" not in columns:
        connection.exec_driver_sql(
            "ALTER TABLE events ADD COLUMN picker_projection_version INTEGER"
        )


def _projection_has_gaps(connection: Connection) -> bool:
    return (
        connection.exec_driver_sql(
            "SELECT 1 FROM events "
            f"INDEXED BY {PICKER_GAP_INDEX} "
            f"WHERE {PICKER_PROJECTION_SCOPE_SQL} "
            f"AND picker_projection_version IS NOT {PICKER_PROJECTION_VERSION} LIMIT 1"
        ).first()
        is not None
    )


def _rebuild_projection(connection: Connection) -> None:
    installed = _installed_contract(connection)
    # SQLite DDL is transactional, so a failed backfill restores the prior
    # complete contract without a partial marker becoming visible to readers.
    for name in (
        PICKER_START_TABLE,
        PICKER_START_SESSION_TABLE,
        PICKER_PROGRESS_TABLE,
        PICKER_META_TABLE,
    ):
        actual = installed.get(name)
        if actual is not None:
            _drop_object(connection, actual[0], actual[1])
    for name in (DIRECT_EVENT_INDEX, PICKER_GAP_INDEX):
        index = installed.get(name)
        if index is not None:
            _drop_object(connection, index[0], index[1])

    _drop_obsolete_indexes(connection)
    connection.exec_driver_sql(PICKER_START_TABLE_DDL)
    connection.exec_driver_sql(PICKER_START_SESSION_TABLE_DDL)
    connection.exec_driver_sql(PICKER_PROGRESS_TABLE_DDL)
    connection.exec_driver_sql(PICKER_META_TABLE_DDL)
    connection.exec_driver_sql(DIRECT_EVENT_INDEX_DDL)

    connection.exec_driver_sql(
        f"INSERT INTO {PICKER_START_TABLE} "
        "(event_rowid, execution_id, session_id) "
        f"SELECT rowid, {PICKER_START_EXECUTION_ID_SQL}, "
        f"{PICKER_START_SESSION_ID_SQL} FROM events WHERE {PICKER_START_SCOPE_SQL}"
    )
    connection.exec_driver_sql(PICKER_START_EXECUTION_INDEX_DDL)
    connection.exec_driver_sql(
        f"INSERT OR IGNORE INTO {PICKER_START_SESSION_TABLE} "
        "(session_id, execution_id, event_rowid) "
        "SELECT session_id, coalesce(execution_id, ''), MIN(event_rowid) "
        f"FROM {PICKER_START_TABLE} WHERE session_id IS NOT NULL "
        "GROUP BY session_id, execution_id"
    )
    connection.exec_driver_sql(
        f"INSERT INTO {PICKER_PROGRESS_TABLE} ("
        "aggregate_id, event_type, latest_valid_rowid, latest_running_rowid, "
        "latest_snapshot_rowid) SELECT aggregate_id, event_type, MAX(rowid), "
        f"MAX(CASE WHEN {RUNNING_PROGRESS_SQL} THEN rowid END), "
        "MAX(CASE WHEN event_type = 'workflow.progress.updated' "
        f"AND {WORKFLOW_SNAPSHOT_SQL} THEN rowid END) FROM events "
        f"WHERE {PICKER_PROGRESS_SCOPE_SQL} AND {VALID_JSON_SQL} "
        "GROUP BY aggregate_id, event_type"
    )
    connection.exec_driver_sql(
        f"UPDATE events SET picker_projection_version = ? WHERE {PICKER_PROJECTION_SCOPE_SQL}",
        (PICKER_PROJECTION_VERSION,),
    )
    connection.exec_driver_sql(PICKER_GAP_INDEX_DDL)
    backfilled_through = connection.exec_driver_sql(
        "SELECT CASE WHEN MAX(rowid) > 0 THEN MAX(rowid) ELSE 0 END FROM events"
    ).scalar_one()
    connection.exec_driver_sql(
        f"INSERT INTO {PICKER_META_TABLE} "
        "(contract_version, backfilled_through_rowid) VALUES (?, ?)",
        (PICKER_PROJECTION_VERSION, int(backfilled_through)),
    )


def provision_picker_indexes(connection: Connection) -> None:
    """Repair/backfill the exact versioned picker contract transactionally."""
    _ensure_events_projection_column(connection)
    if not picker_contract_is_complete(connection) or _projection_has_gaps(connection):
        _rebuild_projection(connection)
    else:
        _drop_obsolete_indexes(connection)
    if not picker_contract_is_complete(connection) or _projection_has_gaps(connection):
        raise SQLAlchemyError("Dashboard picker projection postcondition failed.")


async def provision_picker_indexes_best_effort(
    engine: AsyncEngine,
    logger: logging.Logger,
) -> bool:
    """Provision the optional projection without blocking durable startup on failure."""
    try:
        async with engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                await connection.run_sync(provision_picker_indexes)
            except BaseException:
                await connection.rollback()
                raise
            else:
                await connection.commit()
                return True
    except SQLAlchemyError as exc:
        logger.warning(
            "Dashboard picker projection provisioning deferred: %s; "
            "the writer remains available and EventStore.initialize() can be called "
            "again in-process to retry repair",
            exc,
        )
        return False


__all__ = [
    "picker_contract_is_complete",
    "provision_picker_indexes",
    "provision_picker_indexes_best_effort",
]
