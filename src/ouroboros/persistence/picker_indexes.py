"""SQLite index contract for bounded dashboard picker projections.

The dashboard opens the EventStore read-only, so writable EventStore
initialization owns these indexes for both new and pre-existing databases.
Keep the SQL expressions byte-for-byte aligned with the reader predicates so
SQLite can select the expression and partial indexes.
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


PICKER_PROGRESS_SCOPE_SQL = _event_type_scope(PICKER_PROGRESS_EVENT_TYPES)
PICKER_DIRECT_SCOPE_SQL = _event_type_scope(PICKER_DIRECT_EVENT_TYPES)
PICKER_START_SCOPE_SQL = "event_type = 'orchestrator.session.started'"
PICKER_DIRECT_INDEX_SCOPE_SQL = (
    "((event_type >= 'execution.' AND event_type < 'execution/') "
    "OR (event_type >= 'orchestrator.session.' AND event_type < 'orchestrator.session/')) "
    "AND event_type != 'orchestrator.session.started'"
)
WORKFLOW_PROGRESS_SCOPE_SQL = "event_type = 'workflow.progress.updated'"

SAFE_EXECUTION_ID_SQL = (
    "json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.execution_id')"
)
SAFE_SESSION_ID_SQL = (
    "json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.session_id')"
)
SAFE_PROGRESS_STATUS_SQL = (
    "json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, "
    "'$.progress.runtime_status')"
)
SAFE_LAST_UPDATE_STATUS_SQL = (
    "json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, "
    "'$.last_update.runtime_status')"
)
SAFE_TOP_LEVEL_STATUS_SQL = (
    "json_extract(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, '$.runtime_status')"
)
VALID_JSON_SQL = "json_valid(payload) = 1"
WORKFLOW_SNAPSHOT_SQL = (
    "json_type(CASE WHEN json_valid(payload) THEN payload ELSE '{}' END, "
    "'$.acceptance_criteria') = 'array'"
)
RUNNING_PROGRESS_SQL = (
    f"(lower(trim({SAFE_PROGRESS_STATUS_SQL}, {RUNTIME_STATUS_ASCII_WHITESPACE_SQL})) "
    "= 'running' "
    f"OR lower(trim({SAFE_LAST_UPDATE_STATUS_SQL}, {RUNTIME_STATUS_ASCII_WHITESPACE_SQL})) "
    "= 'running' "
    f"OR lower(trim({SAFE_TOP_LEVEL_STATUS_SQL}, {RUNTIME_STATUS_ASCII_WHITESPACE_SQL})) "
    "= 'running')"
)

DIRECT_EVENT_INDEX = "ix_events_picker_direct_aggregate_event_v1"
START_EVENT_INDEX = "ix_events_picker_session_start_v1"
AGGREGATE_EVENT_INDEX = "ix_events_picker_aggregate_event_valid_v1"
RUNNING_PROGRESS_INDEX = "ix_events_picker_running_progress_v1"
WORKFLOW_SNAPSHOT_INDEX = "ix_events_picker_workflow_snapshot_v1"
PICKER_INDEX_NAMES: tuple[str, ...] = (
    DIRECT_EVENT_INDEX,
    START_EVENT_INDEX,
    AGGREGATE_EVENT_INDEX,
    RUNNING_PROGRESS_INDEX,
    WORKFLOW_SNAPSHOT_INDEX,
)

PICKER_INDEX_DDL: tuple[str, ...] = (
    f"CREATE INDEX IF NOT EXISTS {DIRECT_EVENT_INDEX} "
    "ON events (event_type, aggregate_id) "
    f"WHERE {PICKER_DIRECT_INDEX_SCOPE_SQL}",
    f"CREATE INDEX IF NOT EXISTS {START_EVENT_INDEX} "
    "ON events (event_type) "
    f"WHERE {PICKER_START_SCOPE_SQL}",
    f"CREATE INDEX IF NOT EXISTS {AGGREGATE_EVENT_INDEX} "
    "ON events (aggregate_id, event_type, json_valid(payload)) "
    f"WHERE {PICKER_PROGRESS_SCOPE_SQL}",
    f"CREATE INDEX IF NOT EXISTS {RUNNING_PROGRESS_INDEX} "
    "ON events (aggregate_id, event_type) "
    f"WHERE {PICKER_PROGRESS_SCOPE_SQL} AND {VALID_JSON_SQL} AND {RUNNING_PROGRESS_SQL}",
    f"CREATE INDEX IF NOT EXISTS {WORKFLOW_SNAPSHOT_INDEX} "
    "ON events (aggregate_id) "
    f"WHERE {WORKFLOW_PROGRESS_SCOPE_SQL} AND {VALID_JSON_SQL} "
    f"AND {WORKFLOW_SNAPSHOT_SQL}",
)
PICKER_INDEX_DDL_BY_NAME = dict(zip(PICKER_INDEX_NAMES, PICKER_INDEX_DDL, strict=True))
_PICKER_INDEX_KEY_COLUMNS: dict[str, tuple[str | None, ...]] = {
    DIRECT_EVENT_INDEX: ("event_type", "aggregate_id"),
    START_EVENT_INDEX: ("event_type",),
    AGGREGATE_EVENT_INDEX: ("aggregate_id", "event_type", None),
    RUNNING_PROGRESS_INDEX: ("aggregate_id", "event_type"),
    WORKFLOW_SNAPSHOT_INDEX: ("aggregate_id",),
}


def normalize_index_ddl(statement: str) -> str:
    """Return SQLite's persisted form without altering SQL string literals."""
    prefix = "CREATE INDEX IF NOT EXISTS "
    if statement.startswith(prefix):
        return "CREATE INDEX " + statement[len(prefix) :]
    return statement


def matching_picker_indexes(conn: sqlite3.Connection) -> frozenset[str]:
    """Return indexes whose persisted DDL and key layout match the contract."""
    placeholders = ",".join("?" for _ in PICKER_INDEX_NAMES)
    installed = {
        str(row[0]).lower(): (str(row[0]), row[1])
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' "
            f"AND lower(name) IN ({placeholders})",
            PICKER_INDEX_NAMES,
        )
    }
    matching: set[str] = set()
    for name, expected_sql in PICKER_INDEX_DDL_BY_NAME.items():
        actual_name, actual_sql = installed.get(name, (name, None))
        if not isinstance(actual_sql, str):
            continue
        if normalize_index_ddl(actual_sql) != normalize_index_ddl(expected_sql):
            continue
        key_columns = tuple(
            row[2]
            for row in conn.execute(f'PRAGMA index_xinfo("{actual_name}")')
            if int(row[5]) == 1
        )
        if key_columns == _PICKER_INDEX_KEY_COLUMNS[name]:
            matching.add(name)
    return frozenset(matching)


__all__ = [
    "PICKER_INDEX_DDL",
    "PICKER_INDEX_DDL_BY_NAME",
    "PICKER_INDEX_NAMES",
    "PICKER_DIRECT_EVENT_TYPES",
    "PICKER_PROGRESS_EVENT_TYPES",
    "PICKER_DIRECT_INDEX_SCOPE_SQL",
    "PICKER_DIRECT_SCOPE_SQL",
    "PICKER_START_SCOPE_SQL",
    "PICKER_PROGRESS_SCOPE_SQL",
    "DIRECT_EVENT_INDEX",
    "START_EVENT_INDEX",
    "AGGREGATE_EVENT_INDEX",
    "RUNNING_PROGRESS_INDEX",
    "RUNNING_PROGRESS_SQL",
    "RUNTIME_STATUS_ASCII_WHITESPACE",
    "RUNTIME_STATUS_ASCII_WHITESPACE_SQL",
    "SAFE_EXECUTION_ID_SQL",
    "SAFE_SESSION_ID_SQL",
    "VALID_JSON_SQL",
    "WORKFLOW_SNAPSHOT_SQL",
    "WORKFLOW_SNAPSHOT_INDEX",
    "WORKFLOW_PROGRESS_SCOPE_SQL",
    "matching_picker_indexes",
    "normalize_index_ddl",
]
