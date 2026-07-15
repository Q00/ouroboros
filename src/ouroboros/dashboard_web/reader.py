"""Read-only tail of the EventStore SQLite file (stdlib ``sqlite3``).

The EventStore is a plain SQLite DB (``~/.ouroboros/ouroboros.db`` by default); a
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
    return Path.home() / ".ouroboros" / "ouroboros.db"


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
    """Most-recently-active execution ids (for the CLI/picker).

    Sources execution ids from ``orchestrator.session.started`` (present for EVERY
    run, simple or decomposed) and counts AC nodes from ``execution.node.created``
    when available — so simple, non-decomposed runs are listed too.
    """
    path = Path(db_path).expanduser()
    if not path.exists():
        return []
    sql = (
        "SELECT eid, MAX(last_row) AS last_row, SUM(n) AS n FROM ("
        "  SELECT json_extract(payload, '$.execution_id') AS eid, "
        "         rowid AS last_row, 0 AS n "
        "  FROM events WHERE event_type = 'orchestrator.session.started' "
        "  UNION ALL "
        "  SELECT json_extract(payload, '$.execution_id') AS eid, "
        "         rowid AS last_row, 1 AS n "
        "  FROM events WHERE event_type = 'execution.node.created' "
        ") WHERE eid IS NOT NULL "
        "GROUP BY eid ORDER BY last_row DESC LIMIT ?"
    )
    conn = _connect_readonly(path)
    try:
        rows = conn.execute(sql, [limit]).fetchall()
    finally:
        conn.close()
    return [{"execution_id": r["eid"], "node_count": int(r["n"] or 0)} for r in rows if r["eid"]]


__all__ = ["EventTail", "default_db_path", "list_recent_executions"]
