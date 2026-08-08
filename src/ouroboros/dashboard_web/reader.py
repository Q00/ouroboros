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
from typing import Any
from urllib.parse import quote

from ouroboros.config.models import resolve_event_store_path
from ouroboros.dashboard.board import reduce_board
from ouroboros.persistence.picker_indexes import (
    AGGREGATE_EVENT_INDEX,
    PICKER_DIRECT_EVENT_TYPES,
    PICKER_INDEX_NAMES,
    PICKER_PROGRESS_EVENT_TYPES,
    PICKER_PROGRESS_SCOPE_SQL,
    RUNNING_PROGRESS_INDEX,
    RUNNING_PROGRESS_SQL,
    RUNTIME_STATUS_ASCII_WHITESPACE,
    SAFE_EXECUTION_ID_SQL,
    SAFE_SESSION_ID_SQL,
    VALID_JSON_SQL,
    WORKFLOW_PROGRESS_SCOPE_SQL,
    WORKFLOW_SNAPSHOT_INDEX,
    WORKFLOW_SNAPSHOT_SQL,
    matching_picker_indexes,
)

# Events relevant to the execution Kanban. Filtering at the SQL layer keeps the
# tail cheap even on a mult-hundred-MB DB shared by many runs.
_RELEVANT_EVENT_TYPES: tuple[str, ...] = (
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
    # Carries the run-level runtime_backend (provider) — lets the board tag the
    # provider on SIMPLE runs that emit no per-worker execution.session.started.
    "orchestrator.session.started",
    # Frugality telemetry: per-AC model tier/model routing, per-AC runtime token
    # spend, and the run-end frugality proof — already emitted, previously filtered
    # out of the Kanban tail.
    "execution.ac.model_routed",
    "execution.ac.token_attribution.reported",
    "execution.frugality_proof.evaluated",
    "execution.frugality_retrospective.reported",
)

# Session lifecycle/progress rows are canonically stored under one of the run's
# selected orchestrator/execution aggregates.  Do not include them in the
# JSON-linked fallback query: progress is the highest-volume event family and
# scanning every run's progress history to find a different execution made the
# picker O(global history * visible runs).
_SESSION_SCOPED_EVENT_TYPES = frozenset(
    {
        "orchestrator.session.started",
        "orchestrator.session.completed",
        "orchestrator.session.failed",
        "orchestrator.session.paused",
        "orchestrator.session.cancelled",
        "orchestrator.progress.updated",
        "workflow.progress.updated",
    }
)
_PAYLOAD_LINKED_EVENT_TYPES: tuple[str, ...] = tuple(
    event_type
    for event_type in _RELEVANT_EVENT_TYPES
    if event_type not in _SESSION_SCOPED_EVENT_TYPES
)
_PICKER_EVENT_TYPES = PICKER_DIRECT_EVENT_TYPES
_PICKER_PROGRESS_EVENT_TYPES = PICKER_PROGRESS_EVENT_TYPES
_PICKER_PAYLOAD_LINKED_EVENT_TYPES: tuple[str, ...] = tuple(
    event_type
    for event_type in _PICKER_EVENT_TYPES
    if event_type not in _SESSION_SCOPED_EVENT_TYPES
)
_AC_STATE_EVENT_TYPES = frozenset(
    {
        "execution.node.created",
        "execution.node.updated",
        "execution.subtask.updated",
        "execution.session.started",
        "execution.ac.completed",
        "workflow.progress.updated",
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

    def __init__(self, missing: frozenset[str]) -> None:
        self.missing = missing
        joined = ", ".join(sorted(missing))
        super().__init__(f"dashboard picker index contract unavailable: {joined}")


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


class EventTail:
    """Cursor-based read-only tail of one run's events.

    A single run carries TWO ids — an ``execution_id`` (``exec_…``) and an
    orchestrator ``session_id`` (``orch_…``) — and its events are split across
    them (per-worker node events under the execution_id; the AC snapshot in
    ``workflow.progress.updated`` under the session_id). So we first resolve the
    run's full id CLUSTER from ``orchestrator.session.started`` (which carries
    both), then match any event filed under either id via ``aggregate_id`` /
    ``payload.execution_id`` / ``payload.session_id``. Pass either id as
    ``run_id`` — the cluster is recovered the same way.
    """

    def __init__(self, db_path: str | Path, run_id: str) -> None:
        self._db_path = Path(db_path).expanduser()
        self._run_id = run_id
        self._cursor = 0
        self._ids: list[str] | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    def reset(self) -> None:
        self._cursor = 0
        self._ids = None

    def _resolve_ids(self, conn: sqlite3.Connection) -> list[str]:
        """Recover {execution_id, session_id} for the run (cached)."""
        if self._ids is not None:
            return self._ids
        ids = {self._run_id}
        rows = conn.execute(
            f"SELECT aggregate_id, {SAFE_EXECUTION_ID_SQL} AS eid "
            "FROM events WHERE event_type = 'orchestrator.session.started' "
            f"AND (aggregate_id = ? OR {SAFE_EXECUTION_ID_SQL} = ?)",
            [self._run_id, self._run_id],
        ).fetchall()
        for row in rows:
            if row["aggregate_id"]:
                ids.add(row["aggregate_id"])
            if row["eid"]:
                ids.add(row["eid"])
        self._ids = sorted(ids)
        return self._ids

    def fetch_new(self, *, limit: int = 5000) -> list[dict[str, Any]]:
        """Return events appended since the last call (advances the cursor)."""
        if not self._db_path.exists():
            return []
        conn = _connect_readonly(self._db_path)
        try:
            ids = self._resolve_ids(conn)
            id_ph = ",".join("?" for _ in ids)
            type_ph = ",".join("?" for _ in _RELEVANT_EVENT_TYPES)
            direct_sql = (
                "SELECT rowid, event_type, payload "
                "FROM events "
                "WHERE rowid > ? "
                f"AND event_type IN ({type_ph}) "
                "AND aggregate_id = ? "
                "ORDER BY rowid "
                "LIMIT ?"
            )
            direct_rows: list[sqlite3.Row] = []
            for aggregate_id in ids:
                direct_rows.extend(
                    conn.execute(
                        direct_sql,
                        [self._cursor, *_RELEVANT_EVENT_TYPES, aggregate_id, limit],
                    ).fetchall()
                )
            linked_sql = (
                "SELECT rowid, event_type, payload "
                "FROM events "
                "WHERE rowid > ? "
                "AND event_type = ? "
                f"AND aggregate_id NOT IN ({id_ph}) "
                f"AND ({SAFE_EXECUTION_ID_SQL} IN ({id_ph}) "
                f"     OR {SAFE_SESSION_ID_SQL} IN ({id_ph})) "
                "ORDER BY rowid "
                "LIMIT ?"
            )
            linked_rows: list[sqlite3.Row] = []
            for event_type in _PAYLOAD_LINKED_EVENT_TYPES:
                linked_rows.extend(
                    conn.execute(
                        linked_sql,
                        [self._cursor, event_type, *ids, *ids, *ids, limit],
                    ).fetchall()
                )
            rows = sorted((*direct_rows, *linked_rows), key=lambda row: int(row["rowid"]))[:limit]
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
    """Fetch non-progress rows through the aggregate/event picker index."""
    if not ids or not event_types:
        return []
    rows: list[sqlite3.Row] = []
    for aggregate_id in ids:
        for event_type in event_types:
            rows.extend(
                conn.execute(
                    "SELECT rowid, aggregate_id, event_type, payload FROM events "
                    "WHERE aggregate_id = ? AND event_type = ?",
                    [aggregate_id, event_type],
                ).fetchall()
            )
    return rows


def _fetch_payload_linked_rows(
    conn: sqlite3.Connection,
    ids: list[str],
    event_types: tuple[str, ...],
) -> list[sqlite3.Row]:
    """Seek payload-linked families, excluding already fetched aggregates."""
    if not ids or not event_types:
        return []
    id_ph = ",".join("?" for _ in ids)
    rows: list[sqlite3.Row] = []
    for event_type in event_types:
        rows.extend(
            conn.execute(
                "SELECT rowid, aggregate_id, event_type, payload FROM events "
                "WHERE event_type = ? "
                f"AND aggregate_id NOT IN ({id_ph}) "
                f"AND ({SAFE_EXECUTION_ID_SQL} IN ({id_ph}) "
                f"     OR {SAFE_SESSION_ID_SQL} IN ({id_ph}))",
                [event_type, *ids, *ids, *ids],
            ).fetchall()
        )
    return rows


def _fetch_latest_progress_rows(
    conn: sqlite3.Connection,
    aggregate_ids: list[str],
    event_types: tuple[str, ...] = _PICKER_PROGRESS_EVENT_TYPES,
) -> list[sqlite3.Row]:
    """Fetch only picker-relevant checkpoints from high-volume progress logs.

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
            latest = conn.execute(
                "SELECT rowid, aggregate_id, event_type, payload FROM events "
                f"INDEXED BY {AGGREGATE_EVENT_INDEX} "
                "WHERE aggregate_id = ? AND event_type = ? "
                f"AND {PICKER_PROGRESS_SCOPE_SQL} AND {VALID_JSON_SQL} "
                "ORDER BY rowid DESC LIMIT 1",
                [aggregate_id, event_type],
            ).fetchone()
            if latest is not None:
                rows_by_rowid[int(latest["rowid"])] = latest
            latest_running = conn.execute(
                "SELECT rowid, aggregate_id, event_type, payload FROM events "
                f"INDEXED BY {RUNNING_PROGRESS_INDEX} "
                "WHERE aggregate_id = ? AND event_type = ? "
                f"AND {PICKER_PROGRESS_SCOPE_SQL} AND {VALID_JSON_SQL} "
                f"AND {RUNNING_PROGRESS_SQL} ORDER BY rowid DESC LIMIT 1",
                [aggregate_id, event_type],
            ).fetchone()
            if latest_running is not None:
                rows_by_rowid[int(latest_running["rowid"])] = latest_running
            if event_type == "workflow.progress.updated":
                latest_snapshot = conn.execute(
                    "SELECT rowid, aggregate_id, event_type, payload FROM events "
                    f"INDEXED BY {WORKFLOW_SNAPSHOT_INDEX} "
                    "WHERE aggregate_id = ? AND event_type = ? "
                    f"AND {WORKFLOW_PROGRESS_SCOPE_SQL} AND {VALID_JSON_SQL} "
                    f"AND {WORKFLOW_SNAPSHOT_SQL} ORDER BY rowid DESC LIMIT 1",
                    [aggregate_id, event_type],
                ).fetchone()
                if latest_snapshot is not None:
                    rows_by_rowid[int(latest_snapshot["rowid"])] = latest_snapshot
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
    start_sql = (
        "SELECT rowid, aggregate_id, payload "
        "FROM events WHERE event_type = 'orchestrator.session.started' "
        "ORDER BY rowid DESC"
    )
    conn = _connect_readonly(path)
    try:
        # The checkpoint queries force these indexes because ANALYZE may prefer
        # the broader legacy aggregate index and restore an O(history) scan.
        # Validate full sqlite_master SQL plus index_xinfo first: a missing or
        # stale same-name definition must fail before any EventStore read.
        matching_indexes = matching_picker_indexes(conn)
        missing_indexes = frozenset(PICKER_INDEX_NAMES) - matching_indexes
        if missing_indexes:
            raise PickerIndexContractError(missing_indexes)
        starts = conn.execute(start_sql)
        run_specs: list[tuple[sqlite3.Row, dict[str, Any], str, str]] = []
        seen_execution_ids: set[str] = set()
        target_count = max(1, limit)
        for start in starts:
            start_payload = _decode_payload(start["payload"])
            if not isinstance(start_payload, dict):
                continue
            execution_id = start_payload.get("execution_id")
            if not isinstance(execution_id, str) or not execution_id:
                continue
            if execution_id in seen_execution_ids:
                continue
            seen_execution_ids.add(execution_id)
            raw_session_id = start["aggregate_id"] or start_payload.get("session_id")
            session_id = raw_session_id if isinstance(raw_session_id, str) else ""
            run_specs.append((start, start_payload, execution_id, session_id))
            if len(run_specs) >= target_count:
                break

        all_ids = sorted(
            {
                value
                for _start, _payload, execution_id, session_id in run_specs
                for value in (execution_id, session_id)
                if value
            }
        )
        session_ids = sorted({spec[3] for spec in run_specs if spec[3]})
        event_rows = _fetch_direct_rows(
            conn,
            all_ids,
            _PICKER_EVENT_TYPES,
        )
        if all_ids:
            event_rows.extend(
                _fetch_payload_linked_rows(
                    conn,
                    all_ids,
                    _PICKER_PAYLOAD_LINKED_EVENT_TYPES,
                )
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

        events_by_execution: dict[str, list[dict[str, Any]]] = {
            execution_id: [] for _start, _payload, execution_id, _session_id in run_specs
        }
        executions_by_id: dict[str, set[str]] = {}
        for _start, _payload, execution_id, session_id in run_specs:
            executions_by_id.setdefault(execution_id, set()).add(execution_id)
            if session_id:
                executions_by_id.setdefault(session_id, set()).add(execution_id)
        for row in event_rows:
            payload = _decode_payload(row["payload"])
            if not isinstance(payload, dict):
                continue
            linked_ids = {row["aggregate_id"]}
            for key in ("execution_id", "session_id"):
                value = payload.get(key)
                if isinstance(value, str) and value:
                    linked_ids.add(value)
            matched_executions: set[str] = set()
            for linked_id in linked_ids:
                if isinstance(linked_id, str):
                    matched_executions.update(executions_by_id.get(linked_id, ()))
            event = {
                "rowid": row["rowid"],
                "event_type": row["event_type"],
                "payload": payload,
            }
            for execution_id in matched_executions:
                events_by_execution[execution_id].append(event)

        summaries: list[dict[str, Any]] = []
        for start, start_payload, execution_id, session_id in run_specs:
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
