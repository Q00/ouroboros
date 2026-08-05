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
    "workflow.progress.updated",
    "execution.session.completed",
    "orchestrator.session.completed",
    "orchestrator.session.failed",
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
            "SELECT aggregate_id, json_extract(payload, '$.execution_id') AS eid "
            "FROM events WHERE event_type = 'orchestrator.session.started' "
            "AND (aggregate_id = ? OR json_extract(payload, '$.execution_id') = ?)",
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
            sql = (
                "SELECT rowid, event_type, payload "
                "FROM events "
                "WHERE rowid > ? "
                f"AND event_type IN ({type_ph}) "
                f"AND (aggregate_id IN ({id_ph}) "
                f"     OR json_extract(payload, '$.execution_id') IN ({id_ph}) "
                f"     OR json_extract(payload, '$.session_id') IN ({id_ph})) "
                "ORDER BY rowid "
                "LIMIT ?"
            )
            params: list[Any] = [
                self._cursor,
                *_RELEVANT_EVENT_TYPES,
                *ids,
                *ids,
                *ids,
                limit,
            ]
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        events: list[dict[str, Any]] = []
        for row in rows:
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
            self._cursor = max(self._cursor, int(row["rowid"]))
        return events


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
        "ORDER BY rowid DESC LIMIT ?"
    )
    event_types = (
        "orchestrator.session.started",
        "orchestrator.session.completed",
        "orchestrator.session.failed",
        "orchestrator.session.cancelled",
        "execution.node.created",
        "execution.node.updated",
        "execution.subtask.updated",
        "execution.session.started",
        "execution.ac.completed",
        "workflow.progress.updated",
    )
    conn = _connect_readonly(path)
    try:
        starts = conn.execute(start_sql, [max(1, limit)]).fetchall()
        summaries: list[dict[str, Any]] = []
        seen_execution_ids: set[str] = set()
        type_ph = ",".join("?" for _ in event_types)
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
            session_id = start["aggregate_id"] or start_payload.get("session_id")
            ids = [value for value in (execution_id, session_id) if value]
            id_ph = ",".join("?" for _ in ids)
            scope = (
                f"(aggregate_id IN ({id_ph}) "
                f"OR json_extract(payload, '$.execution_id') IN ({id_ph}) "
                f"OR json_extract(payload, '$.session_id') IN ({id_ph}))"
            )
            event_rows = conn.execute(
                "SELECT rowid, event_type, payload FROM events "
                f"WHERE event_type IN ({type_ph}) AND {scope} ORDER BY rowid",
                [*event_types, *ids, *ids, *ids],
            ).fetchall()
            events = [
                {
                    "rowid": row["rowid"],
                    "event_type": row["event_type"],
                    "payload": payload,
                }
                for row in event_rows
                if isinstance((payload := _decode_payload(row["payload"])), dict)
            ]
            board = reduce_board(events, execution_id=execution_id)
            columns = board["columns"]
            counts = {
                key: len(columns.get(key, []))
                for key in ("pending", "executing", "completed", "failed")
            }
            status = "running"
            for event in events:
                event_type = event["event_type"]
                if event_type == "orchestrator.session.completed":
                    status = "completed"
                elif event_type in {
                    "orchestrator.session.failed",
                    "orchestrator.session.cancelled",
                }:
                    status = "failed"
            if status == "running" and counts["executing"] == 0 and counts["pending"] == 0:
                if counts["completed"]:
                    status = "completed"
                elif counts["failed"]:
                    status = "failed"
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


__all__ = ["EventTail", "default_db_path", "list_recent_executions"]
