"""Exact SQLite contract for bounded dashboard picker projections.

SQLite 3.45 can discard equality constraints on low-quality forced partial
indexes and scan the complete index.  The writable EventStore therefore owns
two small explicit-key projections: an append-only start-row keyset with exact
execution/session identity indexes, and one progress-head row per
aggregate/event family.  The dashboard validates the complete contract before
reading either projection and otherwise fails closed.
"""

from __future__ import annotations

import sqlite3

PICKER_PROGRESS_EVENT_TYPES: tuple[str, ...] = (
    "orchestrator.progress.updated",
    "workflow.progress.updated",
)
PICKER_PROJECTION_EVENT_TYPES: tuple[str, ...] = (
    "orchestrator.session.started",
    *PICKER_PROGRESS_EVENT_TYPES,
)
# One canonical family contract for EventTail and the recent-run picker.  The
# high-volume projection families are separated below; every remaining family
# is served through the bounded canonical-link index path.
PICKER_RELEVANT_EVENT_TYPES: tuple[str, ...] = (
    "execution.node.created",
    "execution.node.updated",
    "execution.subtask.updated",
    "execution.session.started",
    "execution.ac.completed",
    "execution.tool.started",
    "execution.coordinator.tool.started",
    "orchestrator.tool.called",
    "orchestrator.progress.updated",
    "workflow.progress.updated",
    "execution.session.completed",
    "orchestrator.session.completed",
    "orchestrator.session.failed",
    "orchestrator.session.paused",
    "orchestrator.session.cancelled",
    "orchestrator.session.started",
    "execution.ac.model_routed",
    "execution.ac.token_attribution.reported",
    "execution.frugality_proof.evaluated",
    "execution.frugality_retrospective.reported",
    # Terminal-acceptance flips: without these the board reducer never sees a
    # verdict that overturns an AC after its ac.completed event (cards stay
    # DONE while the CLI reports the run failed).
    "execution.verify.failed",
    "execution.ac.acceptance_finalized",
)
PICKER_DIRECT_EVENT_TYPES: tuple[str, ...] = tuple(
    event_type
    for event_type in PICKER_RELEVANT_EVENT_TYPES
    if event_type not in PICKER_PROJECTION_EVENT_TYPES
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
PICKER_PROJECTION_SCOPE_SQL = _event_type_scope(PICKER_PROJECTION_EVENT_TYPES)
PICKER_DIRECT_SCOPE_SQL = _event_type_scope(PICKER_DIRECT_EVENT_TYPES)
PICKER_START_SCOPE_SQL = "event_type = 'orchestrator.session.started'"
# Prefix ranges keep unrelated-write predicate cost low. Any canonical direct
# family outside those ranges is derived into an exact clause automatically, so
# adding a reader family cannot silently leave it outside the partial index.
_PICKER_DIRECT_INDEX_PREFIXES: tuple[str, ...] = (
    "execution.",
    "orchestrator.session.",
)
_PICKER_DIRECT_INDEX_EXACT_EVENT_TYPES: tuple[str, ...] = tuple(
    event_type
    for event_type in PICKER_DIRECT_EVENT_TYPES
    if not event_type.startswith(_PICKER_DIRECT_INDEX_PREFIXES)
)
_PICKER_DIRECT_INDEX_EXACT_SCOPE_SQL = " OR ".join(
    f"event_type = '{event_type}'" for event_type in _PICKER_DIRECT_INDEX_EXACT_EVENT_TYPES
)
PICKER_DIRECT_INDEX_SCOPE_SQL = (
    "((event_type >= 'execution.' AND event_type < 'execution/') "
    "OR (event_type >= 'orchestrator.session.' AND event_type < 'orchestrator.session/')"
    + (
        f" OR {_PICKER_DIRECT_INDEX_EXACT_SCOPE_SQL}"
        if _PICKER_DIRECT_INDEX_EXACT_SCOPE_SQL
        else ""
    )
    + ") "
    "AND event_type != 'orchestrator.session.started'"
)
WORKFLOW_PROGRESS_SCOPE_SQL = "event_type = 'workflow.progress.updated'"

SAFE_EXECUTION_ID_SQL = _safe_json_extract("payload", "$.execution_id")
SAFE_SESSION_ID_SQL = _safe_json_extract("payload", "$.session_id")
VALID_JSON_SQL = "json_valid(payload) = 1"


def _nonblank_text_sql(value_sql: str) -> str:
    return (
        "CASE "
        f"WHEN typeof({value_sql}) = 'text' "
        f"AND trim({value_sql}, {RUNTIME_STATUS_ASCII_WHITESPACE_SQL}) != '' "
        f"THEN {value_sql} END"
    )


PICKER_START_EXECUTION_ID_SQL = _nonblank_text_sql(SAFE_EXECUTION_ID_SQL)
# Session identity is the start event's aggregate identity.  Current producers
# do not duplicate it in the payload, and legacy EventStore history has always
# used this aggregate as the orchestration session key.
PICKER_START_SESSION_ID_SQL = _nonblank_text_sql("aggregate_id")
PICKER_CANONICAL_LINK_ID_SQL = (
    "CASE "
    f"WHEN typeof({SAFE_EXECUTION_ID_SQL}) = 'text' "
    f"AND trim({SAFE_EXECUTION_ID_SQL}, {RUNTIME_STATUS_ASCII_WHITESPACE_SQL}) != '' "
    f"THEN {SAFE_EXECUTION_ID_SQL} "
    f"WHEN typeof({SAFE_SESSION_ID_SQL}) = 'text' "
    f"AND trim({SAFE_SESSION_ID_SQL}, {RUNTIME_STATUS_ASCII_WHITESPACE_SQL}) != '' "
    f"THEN {SAFE_SESSION_ID_SQL} "
    "ELSE aggregate_id END"
)
WORKFLOW_SNAPSHOT_SQL = _workflow_snapshot_sql("payload")
RUNNING_PROGRESS_SQL = _running_progress_sql("payload")

DIRECT_EVENT_INDEX = "ix_events_picker_canonical_link_event_v1"
PICKER_GAP_INDEX = "ix_events_picker_projection_gap_v1"
START_EVENT_INDEX = "ix_events_picker_session_start_v1"
AGGREGATE_EVENT_INDEX = "ix_events_picker_aggregate_event_valid_v1"
RUNNING_PROGRESS_INDEX = "ix_events_picker_running_progress_v1"
WORKFLOW_SNAPSHOT_INDEX = "ix_events_picker_workflow_snapshot_v1"
OBSOLETE_PICKER_INDEX_NAMES: tuple[str, ...] = (
    "ix_events_picker_direct_aggregate_event_v1",
    "ix_dashboard_picker_starts_session_v2",
    START_EVENT_INDEX,
    AGGREGATE_EVENT_INDEX,
    RUNNING_PROGRESS_INDEX,
    WORKFLOW_SNAPSHOT_INDEX,
)

PICKER_START_TABLE = "dashboard_picker_starts_v1"
PICKER_START_SESSION_TABLE = "dashboard_picker_start_sessions_v1"
PICKER_PROGRESS_TABLE = "dashboard_picker_progress_v1"
PICKER_META_TABLE = "dashboard_picker_projection_meta_v1"
PICKER_PROJECTION_VERSION = 3
PICKER_START_EXECUTION_INDEX = "ix_dashboard_picker_starts_execution_v2"

DIRECT_EVENT_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {DIRECT_EVENT_INDEX} "
    f"ON events ({PICKER_CANONICAL_LINK_ID_SQL}, event_type) "
    f"WHERE {PICKER_DIRECT_INDEX_SCOPE_SQL}"
)
PICKER_GAP_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {PICKER_GAP_INDEX} "
    "ON events (event_type) "
    f"WHERE {PICKER_PROJECTION_SCOPE_SQL} "
    f"AND picker_projection_version IS NOT {PICKER_PROJECTION_VERSION}"
)
PICKER_START_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PICKER_START_TABLE} ("
    "event_rowid INTEGER PRIMARY KEY, execution_id TEXT, session_id TEXT)"
)
PICKER_START_EXECUTION_INDEX_DDL = (
    f"CREATE INDEX IF NOT EXISTS {PICKER_START_EXECUTION_INDEX} "
    f"ON {PICKER_START_TABLE} (execution_id) WHERE execution_id IS NOT NULL"
)
PICKER_START_SESSION_TABLE_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PICKER_START_SESSION_TABLE} ("
    "session_id TEXT NOT NULL, execution_id TEXT NOT NULL, event_rowid INTEGER NOT NULL, "
    "PRIMARY KEY (session_id, execution_id)) WITHOUT ROWID"
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
    f"contract_version INTEGER PRIMARY KEY CHECK (contract_version = {PICKER_PROJECTION_VERSION}), "
    "backfilled_through_rowid INTEGER NOT NULL "
    "CHECK (backfilled_through_rowid >= 0))"
)

PICKER_CONTRACT_DDL_BY_NAME: dict[str, str] = {
    DIRECT_EVENT_INDEX: DIRECT_EVENT_INDEX_DDL,
    PICKER_GAP_INDEX: PICKER_GAP_INDEX_DDL,
    PICKER_START_TABLE: PICKER_START_TABLE_DDL,
    PICKER_START_EXECUTION_INDEX: PICKER_START_EXECUTION_INDEX_DDL,
    PICKER_START_SESSION_TABLE: PICKER_START_SESSION_TABLE_DDL,
    PICKER_PROGRESS_TABLE: PICKER_PROGRESS_TABLE_DDL,
    PICKER_META_TABLE: PICKER_META_TABLE_DDL,
}
PICKER_CONTRACT_NAMES: tuple[str, ...] = tuple(PICKER_CONTRACT_DDL_BY_NAME)
PICKER_INDEX_NAMES: tuple[str, ...] = (
    DIRECT_EVENT_INDEX,
    PICKER_GAP_INDEX,
    PICKER_START_EXECUTION_INDEX,
)
PICKER_INDEX_DDL: tuple[str, ...] = (
    DIRECT_EVENT_INDEX_DDL,
    PICKER_GAP_INDEX_DDL,
    PICKER_START_EXECUTION_INDEX_DDL,
)
PICKER_INDEX_DDL_BY_NAME = {
    DIRECT_EVENT_INDEX: DIRECT_EVENT_INDEX_DDL,
    PICKER_GAP_INDEX: PICKER_GAP_INDEX_DDL,
    PICKER_START_EXECUTION_INDEX: PICKER_START_EXECUTION_INDEX_DDL,
}

_EXPECTED_TABLE_COLUMNS: dict[str, tuple[tuple[object, ...], ...]] = {
    PICKER_START_TABLE: (
        ("event_rowid", "INTEGER", 0, 1, 0),
        ("execution_id", "TEXT", 0, 0, 0),
        ("session_id", "TEXT", 0, 0, 0),
    ),
    PICKER_START_SESSION_TABLE: (
        ("session_id", "TEXT", 1, 1, 0),
        ("execution_id", "TEXT", 1, 2, 0),
        ("event_rowid", "INTEGER", 1, 0, 0),
    ),
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
                DIRECT_EVENT_INDEX: (None, "event_type"),
                PICKER_GAP_INDEX: ("event_type",),
                PICKER_START_EXECUTION_INDEX: ("execution_id",),
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
    "PICKER_CANONICAL_LINK_ID_SQL",
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
    "PICKER_RELEVANT_EVENT_TYPES",
    "PICKER_PROGRESS_TABLE",
    "PICKER_PROGRESS_TABLE_DDL",
    "PICKER_PROJECTION_VERSION",
    "PICKER_START_SCOPE_SQL",
    "PICKER_START_EXECUTION_ID_SQL",
    "PICKER_START_EXECUTION_INDEX",
    "PICKER_START_EXECUTION_INDEX_DDL",
    "PICKER_START_SESSION_ID_SQL",
    "PICKER_START_SESSION_TABLE",
    "PICKER_START_SESSION_TABLE_DDL",
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
