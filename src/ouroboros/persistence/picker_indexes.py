"""Exact SQLite contract for bounded dashboard picker projections.

SQLite 3.45 can discard equality constraints on low-quality forced partial
indexes and scan the complete index.  The writable EventStore therefore owns
two small explicit-key projections: an append-only start-row keyset and one
progress-head row per aggregate/event family.  The dashboard validates the
complete contract before reading either projection and otherwise fails closed.
"""

from __future__ import annotations

import sqlite3

PICKER_DIRECT_EVENT_TYPES: tuple[str, ...] = (
    "orchestrator.session.completed",
    "orchestrator.session.failed",
    "orchestrator.session.paused",
    "orchestrator.session.cancelled",
    "execution.node.created",
    "execution.node.updated",
    "execution.subtask.updated",
    "execution.session.started",
    "execution.ac.completed",
)
PICKER_PROGRESS_EVENT_TYPES: tuple[str, ...] = (
    "orchestrator.progress.updated",
    "workflow.progress.updated",
)
RUNTIME_STATUS_ASCII_WHITESPACE = " \t\n\r\v\f"
RUNTIME_STATUS_ASCII_WHITESPACE_SQL = "char(9, 10, 11, 12, 13, 32)"


def _event_type_scope(event_types: tuple[str, ...]) -> str:
    values = ", ".join(f"'{event_type}'" for event_type in event_types)
    return f"event_type IN ({values})"


def _safe_json_extract(payload_sql: str, path: str) -> str:
    return (
        f"json_extract(CASE WHEN json_valid({payload_sql}) "
        f"THEN {payload_sql} ELSE '{{}}' END, '{path}')"
    )


def _running_progress_sql(payload_sql: str) -> str:
    candidates = (
        _safe_json_extract(payload_sql, "$.progress.runtime_status"),
        _safe_json_extract(payload_sql, "$.last_update.runtime_status"),
        _safe_json_extract(payload_sql, "$.runtime_status"),
    )
    comparisons = (
        f"lower(trim({candidate}, {RUNTIME_STATUS_ASCII_WHITESPACE_SQL})) = 'running'"
        for candidate in candidates
    )
    return "(" + " OR ".join(comparisons) + ")"


def _workflow_snapshot_sql(payload_sql: str) -> str:
    return (
        f"json_type(CASE WHEN json_valid({payload_sql}) "
        f"THEN {payload_sql} ELSE '{{}}' END, '$.acceptance_criteria') = 'array'"
    )


PICKER_PROGRESS_SCOPE_SQL = _event_type_scope(PICKER_PROGRESS_EVENT_TYPES)
PICKER_PROJECTION_EVENT_TYPES = (
    "orchestrator.session.started",
    *PICKER_PROGRESS_EVENT_TYPES,
)
PICKER_PROJECTION_SCOPE_SQL = _event_type_scope(PICKER_PROJECTION_EVENT_TYPES)
PICKER_DIRECT_SCOPE_SQL = _event_type_scope(PICKER_DIRECT_EVENT_TYPES)
PICKER_START_SCOPE_SQL = "event_type = 'orchestrator.session.started'"
PICKER_DIRECT_INDEX_SCOPE_SQL = (
    "((event_type >= 'execution.' AND event_type < 'execution/') "
    "OR (event_type >= 'orchestrator.session.' AND event_type < 'orchestrator.session/')) "
    "AND event_type != 'orchestrator.session.started'"
)
WORKFLOW_PROGRESS_SCOPE_SQL = "event_type = 'workflow.progress.updated'"

SAFE_EXECUTION_ID_SQL = _safe_json_extract("payload", "$.execution_id")
SAFE_SESSION_ID_SQL = _safe_json_extract("payload", "$.session_id")
VALID_JSON_SQL = "json_valid(payload) = 1"
WORKFLOW_SNAPSHOT_SQL = _workflow_snapshot_sql("payload")
RUNNING_PROGRESS_SQL = _running_progress_sql("payload")

DIRECT_EVENT_INDEX = "ix_events_picker_direct_aggregate_event_v1"
PICKER_GAP_INDEX = "ix_events_picker_projection_gap_v1"
START_EVENT_INDEX = "ix_events_picker_session_start_v1"
AGGREGATE_EVENT_INDEX = "ix_events_picker_aggregate_event_valid_v1"
RUNNING_PROGRESS_INDEX = "ix_events_picker_running_progress_v1"
WORKFLOW_SNAPSHOT_INDEX = "ix_events_picker_workflow_snapshot_v1"
OBSOLETE_PICKER_INDEX_NAMES: tuple[str, ...] = (
    START_EVENT_INDEX,
    AGGREGATE_EVENT_INDEX,
    RUNNING_PROGRESS_INDEX,
    WORKFLOW_SNAPSHOT_INDEX,
)

PICKER_START_TABLE = "dashboard_picker_starts_v1"
PICKER_PROGRESS_TABLE = "dashboard_picker_progress_v1"
PICKER_META_TABLE = "dashboard_picker_projection_meta_v1"
PICKER_PROJECTION_VERSION = 1

DIRECT_EVENT_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {DIRECT_EVENT_INDEX} "
    "ON events (event_type, aggregate_id) "
    f"WHERE {PICKER_DIRECT_INDEX_SCOPE_SQL}"
)
PICKER_GAP_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {PICKER_GAP_INDEX} "
    "ON events (event_type) "
    f"WHERE {PICKER_PROJECTION_SCOPE_SQL} AND picker_projection_version IS NOT 1"
)
PICKER_START_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PICKER_START_TABLE} (event_rowid INTEGER PRIMARY KEY)"
)
PICKER_PROGRESS_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PICKER_PROGRESS_TABLE} ("
    "aggregate_id TEXT NOT NULL, event_type TEXT NOT NULL, "
    "latest_valid_rowid INTEGER NOT NULL, latest_running_rowid INTEGER, "
    "latest_snapshot_rowid INTEGER, PRIMARY KEY (aggregate_id, event_type), "
    "CHECK (event_type IN ('orchestrator.progress.updated', 'workflow.progress.updated')), "
    "CHECK (latest_running_rowid IS NULL OR latest_running_rowid <= latest_valid_rowid), "
    "CHECK (latest_snapshot_rowid IS NULL OR latest_snapshot_rowid <= latest_valid_rowid), "
    "CHECK (latest_snapshot_rowid IS NULL "
    "OR event_type = 'workflow.progress.updated')) WITHOUT ROWID"
)
PICKER_META_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PICKER_META_TABLE} ("
    "contract_version INTEGER PRIMARY KEY CHECK (contract_version = 1), "
    "backfilled_through_rowid INTEGER NOT NULL "
    "CHECK (backfilled_through_rowid >= 0))"
)

PICKER_CONTRACT_DDL_BY_NAME: dict[str, str] = {
    DIRECT_EVENT_INDEX: DIRECT_EVENT_INDEX_DDL,
    PICKER_GAP_INDEX: PICKER_GAP_INDEX_DDL,
    PICKER_START_TABLE: PICKER_START_TABLE_DDL,
    PICKER_PROGRESS_TABLE: PICKER_PROGRESS_TABLE_DDL,
    PICKER_META_TABLE: PICKER_META_TABLE_DDL,
}
PICKER_CONTRACT_NAMES: tuple[str, ...] = tuple(PICKER_CONTRACT_DDL_BY_NAME)
PICKER_INDEX_NAMES: tuple[str, ...] = (DIRECT_EVENT_INDEX, PICKER_GAP_INDEX)
PICKER_INDEX_DDL: tuple[str, ...] = (DIRECT_EVENT_INDEX_DDL, PICKER_GAP_INDEX_DDL)
PICKER_INDEX_DDL_BY_NAME = {
    DIRECT_EVENT_INDEX: DIRECT_EVENT_INDEX_DDL,
    PICKER_GAP_INDEX: PICKER_GAP_INDEX_DDL,
}

_EXPECTED_TABLE_COLUMNS: dict[str, tuple[tuple[object, ...], ...]] = {
    PICKER_START_TABLE: (("event_rowid", "INTEGER", 0, 1, 0),),
    PICKER_PROGRESS_TABLE: (
        ("aggregate_id", "TEXT", 1, 1, 0),
        ("event_type", "TEXT", 1, 2, 0),
        ("latest_valid_rowid", "INTEGER", 1, 0, 0),
        ("latest_running_rowid", "INTEGER", 0, 0, 0),
        ("latest_snapshot_rowid", "INTEGER", 0, 0, 0),
    ),
    PICKER_META_TABLE: (
        ("contract_version", "INTEGER", 0, 1, 0),
        ("backfilled_through_rowid", "INTEGER", 1, 0, 0),
    ),
}


def normalize_schema_ddl(statement: str) -> str:
    """Return SQLite's persisted DDL form without rewriting literals."""
    for kind in ("INDEX", "TABLE", "TRIGGER"):
        prefix = f"CREATE {kind} IF NOT EXISTS "
        if statement.startswith(prefix):
            return f"CREATE {kind} " + statement[len(prefix) :]
    return statement


def normalize_index_ddl(statement: str) -> str:
    """Backward-compatible alias for the former index-only normalizer."""
    return normalize_schema_ddl(statement)


def matching_picker_contract(conn: sqlite3.Connection) -> frozenset[str]:
    """Return exact DDL/layout members with a completed backfill marker."""
    placeholders = ",".join("?" for _ in PICKER_CONTRACT_NAMES)
    installed = {
        str(row[0]).lower(): (str(row[0]), row[1])
        for row in conn.execute(
            f"SELECT name, sql FROM sqlite_master WHERE lower(name) IN ({placeholders})",
            PICKER_CONTRACT_NAMES,
        )
    }
    matching: set[str] = set()
    projection_column = next(
        (
            row
            for row in conn.execute('PRAGMA table_xinfo("events")')
            if row[1] == "picker_projection_version"
        ),
        None,
    )
    column_matches = projection_column is not None and (
        projection_column[2],
        int(projection_column[3]),
        projection_column[4],
        int(projection_column[5]),
        int(projection_column[6]),
    ) == ("INTEGER", 0, None, 0, 0)
    for name, expected_sql in PICKER_CONTRACT_DDL_BY_NAME.items():
        actual_name, actual_sql = installed.get(name, (name, None))
        if not isinstance(actual_sql, str):
            continue
        if normalize_schema_ddl(actual_sql) != normalize_schema_ddl(expected_sql):
            continue
        expected_columns = _EXPECTED_TABLE_COLUMNS.get(name)
        if expected_columns is not None:
            actual_columns = tuple(
                (row[1], row[2], int(row[3]), int(row[5]), int(row[6]))
                for row in conn.execute(f'PRAGMA table_xinfo("{actual_name}")')
            )
            if actual_columns != expected_columns:
                continue
        if name in PICKER_INDEX_NAMES:
            if not column_matches and name == PICKER_GAP_INDEX:
                continue
            key_columns = tuple(
                row[2]
                for row in conn.execute(f'PRAGMA index_xinfo("{actual_name}")')
                if int(row[5]) == 1
            )
            expected_keys = {
                DIRECT_EVENT_INDEX: ("event_type", "aggregate_id"),
                PICKER_GAP_INDEX: ("event_type",),
            }
            if key_columns != expected_keys[name]:
                continue
        matching.add(name)

    if PICKER_META_TABLE in matching:
        marker = conn.execute(
            f"SELECT contract_version, backfilled_through_rowid, "
            f"typeof(contract_version), typeof(backfilled_through_rowid) "
            f"FROM {PICKER_META_TABLE}"
        ).fetchall()
        if (
            len(marker) != 1
            or marker[0][0] != PICKER_PROJECTION_VERSION
            or marker[0][2] != "integer"
            or marker[0][3] != "integer"
            or not isinstance(marker[0][1], int)
            or int(marker[0][1]) < 0
        ):
            matching.remove(PICKER_META_TABLE)
    return frozenset(matching)


def matching_picker_indexes(conn: sqlite3.Connection) -> frozenset[str]:
    """Return the retained exact picker indexes."""
    matching = matching_picker_contract(conn)
    return frozenset(name for name in PICKER_INDEX_NAMES if name in matching)


__all__ = [
    "AGGREGATE_EVENT_INDEX",
    "DIRECT_EVENT_INDEX",
    "DIRECT_EVENT_INDEX_DDL",
    "OBSOLETE_PICKER_INDEX_NAMES",
    "PICKER_CONTRACT_DDL_BY_NAME",
    "PICKER_CONTRACT_NAMES",
    "PICKER_DIRECT_EVENT_TYPES",
    "PICKER_DIRECT_INDEX_SCOPE_SQL",
    "PICKER_DIRECT_SCOPE_SQL",
    "PICKER_INDEX_DDL",
    "PICKER_INDEX_DDL_BY_NAME",
    "PICKER_INDEX_NAMES",
    "PICKER_META_TABLE",
    "PICKER_META_TABLE_DDL",
    "PICKER_GAP_INDEX",
    "PICKER_GAP_INDEX_DDL",
    "PICKER_PROGRESS_EVENT_TYPES",
    "PICKER_PROJECTION_EVENT_TYPES",
    "PICKER_PROJECTION_SCOPE_SQL",
    "PICKER_PROGRESS_SCOPE_SQL",
    "PICKER_PROGRESS_TABLE",
    "PICKER_PROGRESS_TABLE_DDL",
    "PICKER_PROJECTION_VERSION",
    "PICKER_START_SCOPE_SQL",
    "PICKER_START_TABLE",
    "PICKER_START_TABLE_DDL",
    "RUNNING_PROGRESS_INDEX",
    "RUNNING_PROGRESS_SQL",
    "RUNTIME_STATUS_ASCII_WHITESPACE",
    "RUNTIME_STATUS_ASCII_WHITESPACE_SQL",
    "SAFE_EXECUTION_ID_SQL",
    "SAFE_SESSION_ID_SQL",
    "START_EVENT_INDEX",
    "VALID_JSON_SQL",
    "WORKFLOW_SNAPSHOT_INDEX",
    "WORKFLOW_SNAPSHOT_SQL",
    "WORKFLOW_PROGRESS_SCOPE_SQL",
    "matching_picker_contract",
    "matching_picker_indexes",
    "normalize_index_ddl",
    "normalize_schema_ddl",
]
