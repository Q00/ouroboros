"""Read the durable state of an Interview chat's background run."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ouroboros.mcp.job_snapshot import JobSnapshot


def job_terminal(db_path: str, job_id: str) -> dict[str, str] | None:
    """Read a persisted terminal event without initializing the job manager."""
    uri = Path(db_path).absolute().as_uri()
    try:
        with closing(sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=1)) as conn:
            row = conn.execute(
                "SELECT event_type, payload FROM events WHERE aggregate_id = ? "
                "AND event_type IN ('mcp.job.failed', 'mcp.job.completed', "
                "'mcp.job.cancelled', 'mcp.job.interrupted') "
                "ORDER BY rowid DESC LIMIT 1",
                (job_id,),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    try:
        payload = json.loads(row[1]) if isinstance(row[1], str) else {}
    except ValueError:
        payload = {}
    status = row[0].rsplit(".", 1)[-1]
    message = payload.get("message") if isinstance(payload, dict) else None
    return {"status": status, "message": str(message or status)}


async def read_job(job_id: str) -> JobSnapshot | None:
    """Resolve a known ID, including terminal state the job manager can recover."""
    from ouroboros.dashboard_web.reader import default_db_path

    path = default_db_path()
    if not path.is_file():
        raise FileNotFoundError(path)
    job = await _read_snapshot(path, job_id, read_only=True)
    if job is not None and job.is_terminal and job_terminal(str(path), job_id) is None:
        # The manager inferred a terminal state from the linked run or dead owner.
        # Settle it durably before the next attempt can use it as its predecessor.
        return await _read_snapshot(path, job_id, read_only=False)
    return job


async def _read_snapshot(path: Path, job_id: str, *, read_only: bool) -> JobSnapshot | None:
    from sqlalchemy.exc import SQLAlchemyError

    from ouroboros.core.errors import PersistenceError
    from ouroboros.mcp.job_manager import JobManager
    from ouroboros.mcp.job_read_state import JobNotFoundError
    from ouroboros.persistence.event_store import EventStore, sqlite_database_url

    store = EventStore(sqlite_database_url(path), read_only=read_only)
    try:
        return await JobManager(store).get_snapshot(job_id)
    except JobNotFoundError:
        return None
    except SQLAlchemyError as exc:
        raise PersistenceError(
            "Could not read the run's job.", operation="restore_console_run"
        ) from exc
    finally:
        await store.close()
