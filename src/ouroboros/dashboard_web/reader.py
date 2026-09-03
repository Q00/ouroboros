"""Read-only tail of the EventStore SQLite file (stdlib ``sqlite3``).

The EventStore is a plain SQLite DB at the configured runtime path; a
separate process can read it concurrently without touching the async writer. We
open it strictly read-only (``mode=ro``) so the dashboard can NEVER corrupt a
live run, and page by SQLite's implicit ``rowid`` — the same cursor dimension the
in-process ``EventStore.get_events_after`` uses.

We deliberately avoid SQLAlchemy/aiosqlite here: the dashboard must run as a tiny
dependency-free subprocess/thread, and reads are simple.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any, NamedTuple
from urllib.parse import quote

from ouroboros.config.models import resolve_event_store_path
from ouroboros.dashboard.board import reduce_board
from ouroboros.persistence.picker_indexes import (
    DIRECT_EVENT_INDEX,
    PICKER_CANONICAL_LINK_ID_SQL,
    PICKER_CONTRACT_NAMES,
    PICKER_DIRECT_EVENT_TYPES,
    PICKER_DIRECT_INDEX_SCOPE_SQL,
    PICKER_DIRECT_SCOPE_SQL,
    PICKER_GAP_INDEX,
    PICKER_PROGRESS_EVENT_TYPES,
    PICKER_PROGRESS_TABLE,
    PICKER_PROJECTION_EVENT_TYPES,
    PICKER_PROJECTION_SCOPE_SQL,
    PICKER_PROJECTION_VERSION,
    PICKER_RELEVANT_EVENT_TYPES,
    PICKER_START_EXECUTION_ID_SQL,
    PICKER_START_EXECUTION_INDEX,
    PICKER_START_SESSION_ID_SQL,
    PICKER_START_SESSION_TABLE,
    PICKER_START_TABLE,
    RUNNING_PROGRESS_SQL,
    RUNTIME_STATUS_ASCII_WHITESPACE,
    VALID_JSON_SQL,
    WORKFLOW_PROGRESS_SCOPE_SQL,
    WORKFLOW_SNAPSHOT_SQL,
    matching_picker_contract,
)

# EventTail and the bounded recent-run picker share one persistence-owned family
# contract so the summary reducer cannot silently lose newly supported events.
_RELEVANT_EVENT_TYPES = PICKER_RELEVANT_EVENT_TYPES

_PICKER_EVENT_TYPES = PICKER_DIRECT_EVENT_TYPES
_PICKER_PROGRESS_EVENT_TYPES = PICKER_PROGRESS_EVENT_TYPES
_AC_STATE_EVENT_TYPES = frozenset(
    {
        "execution.node.created",
        "execution.node.updated",
        "execution.subtask.updated",
        "execution.session.started",
        "execution.ac.completed",
        "workflow.progress.updated",
        # Terminal-acceptance flips (final workspace revalidation / acceptance
        # finalization) change card status after the last progress snapshot.
        "execution.verify.failed",
        "execution.ac.acceptance_finalized",
    }
)

# SQLite evaluates json_extract before Python can apply _decode_payload.  The
# shared picker-index expressions wrap extraction so one malformed row remains
# local instead of aborting the whole picker/SSE request.


def default_db_path() -> Path:
    """The EventStore path ``EventStore()`` uses when no URL is given."""
    return resolve_event_store_path()


def _connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    # Percent-encode the path (keeping ``/`` separators) so a path containing
    # ``?`` or ``#`` can't be misparsed as the URI's query/fragment. Without this
    # a DB path like ``/tmp/a?b/ouroboros.db`` would truncate at the ``?``.
    encoded = quote(str(Path(db_path).expanduser()), safe="/")
    uri = f"file:{encoded}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


class PickerIndexContractError(RuntimeError):
    """Raised when bounded picker reads cannot be guaranteed read-only."""

    def __init__(self, missing: frozenset[str], *, detail: str | None = None) -> None:
        self.missing = missing
        if detail is None:
            joined = ", ".join(sorted(missing))
            detail = f"contract unavailable: {joined}"
        super().__init__(f"dashboard picker projection {detail}")


_EXPLICIT_TERMINAL_PRECEDENCE = {
    "completed": 1,
    "cancelled": 2,
    "failed": 3,
}


def _explicit_lifecycle_status(event: dict[str, Any]) -> str | None:
    """Return status carried by an authoritative session lifecycle event."""
    return {
        "orchestrator.session.completed": "completed",
        "orchestrator.session.failed": "failed",
        "orchestrator.session.paused": "paused",
        "orchestrator.session.cancelled": "cancelled",
    }.get(event["event_type"])


def _progress_acknowledges_running(event: dict[str, Any]) -> bool:
    """Return whether progress durably acknowledges pause-to-running resume.

    Runtime progress describes the latest agent turn, not the whole orchestration,
    so terminal-looking values must not author a global run status.  ``running``
    is the sole exception because no ``orchestrator.session.resumed`` event exists.
    The two production producers place it under ``progress`` and ``last_update``;
    top-level remains supported for legacy rows.  This mirrors the canonical TUI
    lifecycle projection without coupling the dependency-free reader to the TUI.
    """
    if event["event_type"] not in {
        "orchestrator.progress.updated",
        "workflow.progress.updated",
    }:
        return False

    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    candidates: list[object] = []
    for container_key in ("progress", "last_update"):
        container = payload.get(container_key)
        if isinstance(container, dict):
            candidates.append(container.get("runtime_status"))
    candidates.append(payload.get("runtime_status"))
    return any(
        isinstance(candidate, str)
        and candidate.strip(RUNTIME_STATUS_ASCII_WHITESPACE).lower() == "running"
        for candidate in candidates
    )


def _advances_ac_state(event: dict[str, Any]) -> bool:
    """Return whether an event carries a durable acceptance-criteria snapshot.

    Workflow progress also carries per-turn runtime metadata.  A status-only
    workflow row must not supersede an earlier running acknowledgement merely
    because it shares the same event type as full AC snapshots.
    """
    if event["event_type"] != "workflow.progress.updated":
        return event["event_type"] in _AC_STATE_EVENT_TYPES
    payload = event.get("payload")
    return isinstance(payload, dict) and isinstance(payload.get("acceptance_criteria"), list)


def _summary_status(
    events: list[dict[str, Any]],
    counts: dict[str, int],
) -> str:
    """Project one truthful run status from durable terminal and AC evidence.

    Explicit failure wins over every successful-looking recovery signal, and
    cancellation remains its own terminal state. Pauses are resumable: a later
    running progress checkpoint replaces them, while a true session terminal is
    absorbing. Without lifecycle evidence, AC counts become authoritative only
    after no work remains in flight; mixed recovery must never look successful.
    """
    explicit_terminal: str | None = None
    active_status: str | None = None
    active_status_row = 0
    latest_ac_state_row = 0
    for event in events:
        event_type = event["event_type"]
        rowid = int(event.get("rowid") or 0)
        if _advances_ac_state(event):
            latest_ac_state_row = max(latest_ac_state_row, rowid)
        status = _explicit_lifecycle_status(event)
        if event_type in {
            "orchestrator.session.completed",
            "orchestrator.session.failed",
            "orchestrator.session.cancelled",
        }:
            assert status is not None
            if (
                explicit_terminal is None
                or _EXPLICIT_TERMINAL_PRECEDENCE[status]
                > _EXPLICIT_TERMINAL_PRECEDENCE[explicit_terminal]
            ):
                explicit_terminal = status
            continue
        if explicit_terminal is not None:
            continue
        if status == "paused":
            active_status = status
            active_status_row = rowid
        elif _progress_acknowledges_running(event):
            # A durable running checkpoint is meaningful even without a prior
            # pause.  In particular, post-AC synthesis/recovery can continue
            # after every card currently looks settled.  Only a newer AC-state
            # row or an explicit terminal may supersede it.
            active_status = "running"
            active_status_row = rowid

    if explicit_terminal in {"failed", "cancelled"}:
        return explicit_terminal

    no_work_in_flight = counts["executing"] == 0 and counts["pending"] == 0
    projected_status = explicit_terminal or active_status
    if counts["failed"] and projected_status == "completed":
        return "failed"
    if projected_status == "paused" or explicit_terminal is not None:
        return projected_status
    if projected_status == "running" and active_status_row >= latest_ac_state_row:
        return "running"
    if no_work_in_flight and counts["failed"]:
        return "failed"
    if no_work_in_flight and counts["completed"]:
        return "completed"
    return "running"


class _ResolvedRunCluster(NamedTuple):
    """One validated execution and every session that durably belongs to it."""

    execution_id: str | None
    session_ids: tuple[str, ...]
    start_rows: tuple[sqlite3.Row, ...]

    @property
    def selected_ids(self) -> tuple[str, ...]:
        values = set(self.session_ids)
        if self.execution_id:
            values.add(self.execution_id)
        return tuple(sorted(values))

    @property
    def identity(self) -> tuple[str | None, tuple[str, ...]]:
        return self.execution_id, self.session_ids


def _execution_start_rows(conn: sqlite3.Connection, execution_id: str) -> list[sqlite3.Row]:
    """Load and validate every projected start for one execution identity."""
    rows = conn.execute(
        "SELECT projected.event_rowid AS projected_rowid, "
        "projected.execution_id AS projected_execution_id, "
        "projected.session_id AS projected_session_id, "
        "events.rowid, events.aggregate_id, events.event_type, events.payload, "
        f"{PICKER_START_EXECUTION_ID_SQL} AS eid, "
        f"{PICKER_START_SESSION_ID_SQL} AS sid "
        f"FROM {PICKER_START_TABLE} AS projected "
        f"INDEXED BY {PICKER_START_EXECUTION_INDEX} "
        "LEFT JOIN events ON events.rowid = projected.event_rowid "
        "WHERE projected.execution_id = ?",
        [execution_id],
    ).fetchall()
    for row in rows:
        valid_pointer = (
            row["projected_rowid"] is not None
            and row["rowid"] is not None
            and row["event_type"] == "orchestrator.session.started"
            and int(row["rowid"]) == int(row["projected_rowid"])
            and row["projected_execution_id"] == row["eid"]
            and row["projected_session_id"] == row["sid"]
        )
        if not valid_pointer:
            raise PickerIndexContractError(
                frozenset(),
                detail=f"start identity pointer mismatch: {row['projected_rowid']}",
            )
    return rows


def _session_start_rows(conn: sqlite3.Connection, session_id: str) -> list[sqlite3.Row]:
    """Load and validate every projected execution mapping for one session."""
    rows = conn.execute(
        "SELECT projected.event_rowid AS projected_rowid, "
        "projected.execution_id AS projected_execution_id, "
        "projected.session_id AS projected_session_id, "
        "lookup.event_rowid AS lookup_rowid, "
        "lookup.execution_id AS lookup_execution_id, "
        "lookup.session_id AS lookup_session_id, "
        "events.rowid, events.aggregate_id, events.event_type, events.payload, "
        f"{PICKER_START_EXECUTION_ID_SQL} AS eid, "
        f"{PICKER_START_SESSION_ID_SQL} AS sid "
        f"FROM {PICKER_START_SESSION_TABLE} AS lookup "
        f"LEFT JOIN {PICKER_START_TABLE} AS projected "
        "ON projected.event_rowid = lookup.event_rowid "
        "LEFT JOIN events ON events.rowid = projected.event_rowid "
        "WHERE lookup.session_id = ?",
        [session_id],
    ).fetchall()
    for row in rows:
        valid_pointer = (
            row["projected_rowid"] is not None
            and row["lookup_rowid"] is not None
            and row["rowid"] is not None
            and row["event_type"] == "orchestrator.session.started"
            and int(row["rowid"]) == int(row["projected_rowid"])
            and int(row["lookup_rowid"]) == int(row["projected_rowid"])
            and row["lookup_execution_id"] == (row["projected_execution_id"] or "")
            and row["lookup_session_id"] == row["projected_session_id"]
            and row["projected_execution_id"] == row["eid"]
            and row["projected_session_id"] == row["sid"]
        )
        if not valid_pointer:
            raise PickerIndexContractError(
                frozenset(),
                detail=f"start identity pointer mismatch: {row['projected_rowid']}",
            )
    return rows


def _resolve_run_cluster(conn: sqlite3.Connection, run_id: str) -> _ResolvedRunCluster:
    """Resolve both identity namespaces to exactly one canonical run cluster.

    A token may legally be looked up as either an execution id or a session id.
    Querying only the first matching namespace lets one token join unrelated
    runs, so every candidate is expanded and their canonical identities must
    agree before any dashboard surface may read events.
    """
    execution_cache: dict[str, _ResolvedRunCluster | None] = {}
    session_cache: dict[str, list[sqlite3.Row]] = {}

    def session_rows(session_id: str) -> list[sqlite3.Row]:
        if session_id not in session_cache:
            session_cache[session_id] = _session_start_rows(conn, session_id)
        return session_cache[session_id]

    def execution_cluster(execution_id: str) -> _ResolvedRunCluster | None:
        if execution_id in execution_cache:
            return execution_cache[execution_id]
        rows = _execution_start_rows(conn, execution_id)
        if not rows:
            execution_cache[execution_id] = None
            return None
        session_ids = tuple(sorted({row["sid"] for row in rows if row["sid"]}))
        for session_id in session_ids:
            mapped_rows = session_rows(session_id)
            if not mapped_rows:
                raise PickerIndexContractError(
                    frozenset(),
                    detail=f"start identity pointer mismatch: missing for {session_id}",
                )
            mapped_execution_ids = {row["lookup_execution_id"] for row in mapped_rows}
            if mapped_execution_ids != {execution_id}:
                raise PickerIndexContractError(
                    frozenset(), detail=f"ambiguous selected run ids: {[session_id]}"
                )
        cluster = _ResolvedRunCluster(execution_id, session_ids, tuple(rows))
        execution_cache[execution_id] = cluster
        return cluster

    candidates: list[_ResolvedRunCluster] = []
    by_execution = execution_cluster(run_id)
    if by_execution is not None:
        candidates.append(by_execution)

    mapped_rows = session_rows(run_id)
    mapped_execution_ids = {row["lookup_execution_id"] for row in mapped_rows}
    if "" in mapped_execution_ids:
        candidates.append(_ResolvedRunCluster(None, (run_id,), tuple(mapped_rows)))
    for execution_id in sorted(mapped_execution_ids - {""}):
        mapped_cluster = execution_cluster(execution_id)
        if mapped_cluster is None:
            raise PickerIndexContractError(
                frozenset(), detail=f"start identity pointer mismatch: missing for {run_id}"
            )
        candidates.append(mapped_cluster)

    if not candidates:
        raise PickerIndexContractError(
            frozenset(), detail=f"start identity pointer mismatch: missing for {run_id}"
        )
    identities = {candidate.identity for candidate in candidates}
    if len(identities) != 1:
        raise PickerIndexContractError(
            frozenset(), detail=f"ambiguous selected run ids: {[run_id]}"
        )
    canonical = candidates[0]
    for selected_id in canonical.selected_ids:
        namespace_identities: set[tuple[str | None, tuple[str, ...]]] = {canonical.identity}
        selected_execution = execution_cluster(selected_id)
        if selected_execution is not None:
            namespace_identities.add(selected_execution.identity)
        selected_map_rows = session_rows(selected_id)
        selected_execution_ids = {
            row["lookup_execution_id"] for row in selected_map_rows if row["lookup_execution_id"]
        }
        if any(not row["lookup_execution_id"] for row in selected_map_rows):
            namespace_identities.add((None, (selected_id,)))
        for execution_id in selected_execution_ids:
            selected_cluster = execution_cluster(execution_id)
            if selected_cluster is None:
                raise PickerIndexContractError(
                    frozenset(),
                    detail=f"start identity pointer mismatch: missing for {selected_id}",
                )
            namespace_identities.add(selected_cluster.identity)
        if namespace_identities != {canonical.identity}:
            raise PickerIndexContractError(
                frozenset(), detail=f"ambiguous selected run ids: {[selected_id]}"
            )
    return canonical


class EventTail:
    """Cursor-based read-only tail of one run's events.

    A single run carries TWO ids — an ``execution_id`` (``exec_…``) and an
    orchestrator ``session_id`` (``orch_…``). Start/progress rows stay aggregate
    scoped; lower-volume rows use the exact canonical picker identity (nonblank
    text execution_id, else nonblank text session_id, else aggregate_id). Pass
    either run id — the start event resolves the same selected-id cluster first.
    """

    def __init__(self, db_path: str | Path, run_id: str) -> None:
        self._db_path = Path(db_path).expanduser()
        self._run_id = run_id
        self._cursor = 0

    @property
    def db_path(self) -> Path:
        return self._db_path

    def reset(self) -> None:
        self._cursor = 0

    def _resolve_ids(self, conn: sqlite3.Connection) -> list[str]:
        """Recover the current bounded execution/session cluster for the run."""
        return list(_resolve_run_cluster(conn, self._run_id).selected_ids)

    def fetch_new(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Return events appended since the last call (advances the cursor)."""
        if not self._db_path.exists():
            return []
        conn = _connect_readonly(self._db_path)
        try:
            conn.execute("BEGIN")
            matching_contract = matching_picker_contract(conn)
            missing_contract = frozenset(PICKER_CONTRACT_NAMES) - matching_contract
            if missing_contract:
                raise PickerIndexContractError(missing_contract)
            if (
                conn.execute(
                    "SELECT 1 FROM events "
                    f"INDEXED BY {PICKER_GAP_INDEX} "
                    f"WHERE {PICKER_PROJECTION_SCOPE_SQL} "
                    f"AND picker_projection_version IS NOT {PICKER_PROJECTION_VERSION} LIMIT 1"
                ).fetchone()
                is not None
            ):
                raise PickerIndexContractError(
                    frozenset(), detail="contains unprojected relevant events"
                )
            ids = self._resolve_ids(conn)
            projection_type_ph = ",".join("?" for _ in PICKER_PROJECTION_EVENT_TYPES)
            projection_sql = (
                "SELECT rowid, event_type, payload "
                "FROM events "
                "WHERE rowid > ? "
                f"AND event_type IN ({projection_type_ph}) "
                "AND aggregate_id = ? "
                "ORDER BY rowid "
                "LIMIT ?"
            )
            rows_by_rowid: dict[int, sqlite3.Row] = {}
            for aggregate_id in ids:
                rows = conn.execute(
                    projection_sql,
                    [self._cursor, *PICKER_PROJECTION_EVENT_TYPES, aggregate_id, limit],
                ).fetchall()
                for row in rows:
                    rows_by_rowid[int(row["rowid"])] = row
            canonical_sql = (
                "SELECT rowid, event_type, payload "
                f"FROM events INDEXED BY {DIRECT_EVENT_INDEX} "
                "WHERE rowid > ? "
                "AND event_type = ? "
                f"AND {PICKER_DIRECT_INDEX_SCOPE_SQL} "
                f"AND {PICKER_DIRECT_SCOPE_SQL} "
                f"AND {PICKER_CANONICAL_LINK_ID_SQL} = ? "
                "ORDER BY rowid "
                "LIMIT ?"
            )
            for selected_id in ids:
                for event_type in PICKER_DIRECT_EVENT_TYPES:
                    rows = conn.execute(
                        canonical_sql,
                        [self._cursor, event_type, selected_id, limit],
                    ).fetchall()
                    for row in rows:
                        rows_by_rowid[int(row["rowid"])] = row
            rows = sorted(rows_by_rowid.values(), key=lambda row: int(row["rowid"]))[:limit]
        finally:
            conn.close()

        events: list[dict[str, Any]] = []
        for row in rows:
            self._cursor = max(self._cursor, int(row["rowid"]))
            payload = row["payload"]
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    continue
            events.append(
                {
                    "rowid": row["rowid"],
                    "event_type": row["event_type"],
                    "payload": payload,
                }
            )
        return events


def _fetch_direct_rows(
    conn: sqlite3.Connection,
    ids: list[str],
    event_types: tuple[str, ...],
) -> list[sqlite3.Row]:
    """Fetch non-progress rows through the canonical run-link index."""
    if not ids or not event_types:
        return []
    id_ph = ",".join("?" for _ in ids)
    rows: list[sqlite3.Row] = []
    sql = (
        "SELECT rowid, aggregate_id, event_type, payload, "
        f"{PICKER_CANONICAL_LINK_ID_SQL} AS link_id FROM events "
        f"INDEXED BY {DIRECT_EVENT_INDEX} "
        f"WHERE {PICKER_CANONICAL_LINK_ID_SQL} IN ({id_ph}) "
        "AND event_type = ? "
        f"AND {PICKER_DIRECT_INDEX_SCOPE_SQL} "
        f"AND {PICKER_DIRECT_SCOPE_SQL}"
    )
    for event_type in event_types:
        rows.extend(conn.execute(sql, [*ids, event_type]).fetchall())
    return rows


def _fetch_latest_progress_rows(
    conn: sqlite3.Connection,
    aggregate_ids: list[str],
    event_types: tuple[str, ...] = _PICKER_PROGRESS_EVENT_TYPES,
) -> list[sqlite3.Row]:
    """Fetch only picker-relevant checkpoints from explicit progress heads.

    The latest valid progress row preserves truthful ``last_row`` ordering even
    when its per-turn status is ignored.  The latest durable running row is also
    kept so a subsequent completed/failed turn cannot erase resume evidence.
    Writable EventStore initialization installs exact valid/running indexes, so
    the lookups return at most three rows per family and selected aggregate
    without scanning historical JSON payloads.
    """
    rows_by_rowid: dict[int, sqlite3.Row] = {}
    for aggregate_id in aggregate_ids:
        for event_type in event_types:
            head = conn.execute(
                "SELECT latest_valid_rowid, latest_running_rowid, latest_snapshot_rowid "
                f"FROM {PICKER_PROGRESS_TABLE} "
                "WHERE aggregate_id = ? AND event_type = ?",
                [aggregate_id, event_type],
            ).fetchone()
            if head is None:
                continue
            pointers = (
                ("valid", head["latest_valid_rowid"]),
                ("running", head["latest_running_rowid"]),
                ("snapshot", head["latest_snapshot_rowid"]),
            )
            for kind, raw_rowid in pointers:
                if raw_rowid is None:
                    continue
                row = conn.execute(
                    "SELECT rowid, aggregate_id, event_type, payload, "
                    f"{PICKER_CANONICAL_LINK_ID_SQL} AS link_id, "
                    f"{VALID_JSON_SQL} AS is_valid, "
                    f"{RUNNING_PROGRESS_SQL} AS is_running, "
                    f"({WORKFLOW_PROGRESS_SCOPE_SQL} AND {WORKFLOW_SNAPSHOT_SQL}) "
                    "AS is_snapshot FROM events WHERE rowid = ?",
                    [raw_rowid],
                ).fetchone()
                valid_pointer = (
                    row is not None
                    and row["aggregate_id"] == aggregate_id
                    and row["event_type"] == event_type
                    and bool(row["is_valid"])
                    and (kind != "running" or bool(row["is_running"]))
                    and (kind != "snapshot" or bool(row["is_snapshot"]))
                )
                if not valid_pointer:
                    raise PickerIndexContractError(
                        frozenset(),
                        detail=(
                            f"pointer mismatch: {aggregate_id}/{event_type}/{kind}/{raw_rowid}"
                        ),
                    )
                rows_by_rowid[int(row["rowid"])] = row
    return list(rows_by_rowid.values())


def list_recent_executions(db_path: str | Path, *, limit: int = 10) -> list[dict[str, Any]]:
    """Return recent execution summaries for the dashboard run picker.

    Sources execution ids from ``orchestrator.session.started`` (present for EVERY
    run, simple or decomposed). The start event also owns the unmodified
    ``seed_goal`` shown by the list view. Each selected run is reduced from its
    read-only event cluster so concurrent runs can be compared without opening
    every SSE stream.
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        return []
    start_page_sql = (
        "SELECT projected.event_rowid AS projected_rowid, events.rowid, "
        "projected.execution_id AS projected_execution_id, "
        "projected.session_id AS projected_session_id, "
        "events.aggregate_id, events.event_type, events.payload "
        f"FROM {PICKER_START_TABLE} AS projected "
        "LEFT JOIN events ON events.rowid = projected.event_rowid "
        "WHERE projected.event_rowid <= ? "
        "ORDER BY projected.event_rowid DESC LIMIT ?"
    )
    conn = _connect_readonly(path)
    try:
        # Pin one SQLite snapshot across contract validation, the rolling-writer
        # gap fence, and every projected/history read. Without an explicit read
        # transaction, a legacy writer can commit after the gap probe and make
        # later queries observe a different snapshot, bypassing fail-closed.
        conn.execute("BEGIN")
        # The checkpoint queries force these indexes because ANALYZE may prefer
        # the broader legacy aggregate index and restore an O(history) scan.
        # Validate full sqlite_master SQL plus index_xinfo first: a missing or
        # stale same-name definition must fail before any EventStore read.
        matching_contract = matching_picker_contract(conn)
        missing_contract = frozenset(PICKER_CONTRACT_NAMES) - matching_contract
        if missing_contract:
            raise PickerIndexContractError(missing_contract)
        if (
            conn.execute(
                "SELECT 1 FROM events "
                f"INDEXED BY {PICKER_GAP_INDEX} "
                f"WHERE {PICKER_PROJECTION_SCOPE_SQL} "
                f"AND picker_projection_version IS NOT {PICKER_PROJECTION_VERSION} LIMIT 1"
            ).fetchone()
            is not None
        ):
            raise PickerIndexContractError(
                frozenset(), detail="contains unprojected relevant events"
            )
        run_specs: list[tuple[sqlite3.Row, dict[str, Any], str, str, _ResolvedRunCluster]] = []
        seen_execution_ids: set[str] = set()
        target_count = max(1, limit)
        page_size = max(16, target_count * 2)
        before_rowid = 2**63 - 1
        while len(run_specs) < target_count:
            start_page = conn.execute(
                start_page_sql,
                [before_rowid, page_size],
            ).fetchall()
            if not start_page:
                break
            invalid_start = next(
                (
                    start
                    for start in start_page
                    if start["rowid"] is None
                    or start["event_type"] != "orchestrator.session.started"
                    or int(start["rowid"]) != int(start["projected_rowid"])
                ),
                None,
            )
            if invalid_start is not None:
                raise PickerIndexContractError(
                    frozenset(),
                    detail=f"start pointer mismatch: {invalid_start['projected_rowid']}",
                )
            oldest_rowid = min(int(start["rowid"]) for start in start_page)
            exhausted_rowid_range = oldest_rowid == -(2**63)
            if not exhausted_rowid_range:
                before_rowid = oldest_rowid - 1
            for start in start_page:
                start_payload = _decode_payload(start["payload"])
                raw_execution_id = (
                    start_payload.get("execution_id") if isinstance(start_payload, dict) else None
                )
                expected_execution_id = (
                    raw_execution_id
                    if isinstance(raw_execution_id, str)
                    and raw_execution_id.strip(RUNTIME_STATUS_ASCII_WHITESPACE)
                    else None
                )
                raw_session_id = start["aggregate_id"]
                expected_session_id = (
                    raw_session_id
                    if isinstance(raw_session_id, str)
                    and raw_session_id.strip(RUNTIME_STATUS_ASCII_WHITESPACE)
                    else None
                )
                if (
                    start["projected_execution_id"] != expected_execution_id
                    or start["projected_session_id"] != expected_session_id
                ):
                    raise PickerIndexContractError(
                        frozenset(),
                        detail=f"start identity pointer mismatch: {start['projected_rowid']}",
                    )
                if not isinstance(start_payload, dict):
                    continue
                execution_id = expected_execution_id
                if execution_id is None:
                    continue
                session_id = expected_session_id
                if session_id is None:
                    continue
                if execution_id in seen_execution_ids:
                    continue
                seen_execution_ids.add(execution_id)
                cluster = _resolve_run_cluster(conn, execution_id)
                if cluster.execution_id != execution_id:
                    raise PickerIndexContractError(
                        frozenset(), detail=f"canonical execution mismatch: {execution_id}"
                    )
                run_specs.append((start, start_payload, execution_id, session_id, cluster))
                if len(run_specs) >= target_count:
                    break
            if exhausted_rowid_range:
                break

        all_ids = sorted(
            {
                value
                for _start, _payload, _execution_id, _session_id, cluster in run_specs
                for value in cluster.selected_ids
                if value
            }
        )
        session_ids = sorted(
            {
                session_id
                for _start, _payload, _execution_id, _session_id, cluster in run_specs
                for session_id in cluster.session_ids
            }
        )
        event_rows = _fetch_direct_rows(
            conn,
            all_ids,
            _PICKER_EVENT_TYPES,
        )
        event_rows.extend(
            _fetch_latest_progress_rows(
                conn,
                session_ids,
                ("orchestrator.progress.updated",),
            )
        )
        event_rows.extend(
            _fetch_latest_progress_rows(
                conn,
                all_ids,
                ("workflow.progress.updated",),
            )
        )
        event_rows.sort(key=lambda row: int(row["rowid"]))

        events_by_execution: dict[str, list[dict[str, Any]]] = {}
        for _start, _start_payload, execution_id, _session_id, cluster in run_specs:
            start_events: list[dict[str, Any]] = []
            for cluster_start in cluster.start_rows:
                cluster_payload = _decode_payload(cluster_start["payload"])
                if not isinstance(cluster_payload, dict):
                    continue
                start_events.append(
                    {
                        "rowid": cluster_start["rowid"],
                        "event_type": cluster_start["event_type"],
                        "payload": cluster_payload,
                    }
                )
            events_by_execution[execution_id] = start_events
        executions_by_id: dict[str, set[str]] = {}
        for _start, _payload, execution_id, _session_id, cluster in run_specs:
            for selected_id in cluster.selected_ids:
                executions_by_id.setdefault(selected_id, set()).add(execution_id)
        ambiguous_ids = sorted(
            selected_id
            for selected_id, execution_ids in executions_by_id.items()
            if len(execution_ids) != 1
        )
        if ambiguous_ids:
            raise PickerIndexContractError(
                frozenset(), detail=f"ambiguous selected run ids: {ambiguous_ids}"
            )
        for row in event_rows:
            payload = _decode_payload(row["payload"])
            if not isinstance(payload, dict):
                continue
            link_id = row["link_id"]
            matched_executions = (
                executions_by_id.get(link_id, set()) if isinstance(link_id, str) else set()
            )
            if len(matched_executions) != 1:
                raise PickerIndexContractError(
                    frozenset(), detail=f"canonical link mismatch: {row['rowid']}/{link_id}"
                )
            for key in ("execution_id", "session_id"):
                value = payload.get(key)
                if not isinstance(value, str) or not value or value not in executions_by_id:
                    continue
                if executions_by_id[value] != matched_executions:
                    raise PickerIndexContractError(
                        frozenset(),
                        detail=(
                            f"conflicting selected payload ids: {row['rowid']}/{link_id}/{value}"
                        ),
                    )
            event = {
                "rowid": row["rowid"],
                "event_type": row["event_type"],
                "payload": payload,
            }
            for execution_id in matched_executions:
                events_by_execution[execution_id].append(event)
        for execution_events in events_by_execution.values():
            execution_events.sort(key=lambda event: int(event["rowid"]))

        summaries: list[dict[str, Any]] = []
        for start, start_payload, execution_id, session_id, _cluster in run_specs:
            events = events_by_execution[execution_id]
            board = reduce_board(events, execution_id=execution_id)
            columns = board["columns"]
            counts = {
                key: len(columns.get(key, []))
                for key in ("pending", "executing", "completed", "failed")
            }
            status = _summary_status(events, counts)
            meta = board["meta"]
            goal = start_payload.get("seed_goal")
            if not isinstance(goal, str):
                goal = meta.get("goal") if isinstance(meta.get("goal"), str) else None
            summaries.append(
                {
                    "execution_id": execution_id,
                    "session_id": session_id,
                    "goal": goal,
                    "status": status,
                    "node_count": sum(counts.values()),
                    "completed_count": counts["completed"],
                    "total_count": meta.get("total") or sum(counts.values()),
                    "pending_count": counts["pending"],
                    "executing_count": counts["executing"],
                    "failed_count": counts["failed"],
                    "phase": meta.get("phase"),
                    "activity": meta.get("activity"),
                    "provider": meta.get("provider"),
                    "total_tokens": meta.get("total_tokens", 0.0),
                    "start_time": start_payload.get("start_time"),
                    "last_row": max(
                        (int(event["rowid"]) for event in events), default=int(start["rowid"])
                    ),
                }
            )
    finally:
        conn.close()
    return summaries


def _decode_payload(payload: object) -> Any:
    """Decode one SQLite JSON payload without allowing malformed rows to break the picker."""
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            return None
    return payload


__all__ = [
    "EventTail",
    "PickerIndexContractError",
    "default_db_path",
    "list_recent_executions",
]
