"""SQLite-backed persistence for OpenClaw workflow state."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Any


def default_openclaw_db_path() -> Path:
    """Return the default SQLite database path for OpenClaw workflow state."""
    return Path.home() / ".ouroboros" / "ouroboros.db"


class OpenClawStateStore:
    """Small SQLite store for channel repos and workflows.

    Keeps the current OpenClaw workflow surface synchronous while moving state
    off JSON files and into a transactional local database.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = db_path or default_openclaw_db_path()
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self._initialized = True
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self._ensure_initialized()
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialize(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS openclaw_channel_repos (
                    channel_key TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT,
                    repo TEXT NOT NULL,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS openclaw_workflows (
                    workflow_id TEXT PRIMARY KEY,
                    channel_key TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    guild_id TEXT,
                    user_id TEXT,
                    repo TEXT NOT NULL,
                    request_message TEXT NOT NULL,
                    entry_point TEXT NOT NULL,
                    request_fingerprint TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    interview_session_id TEXT,
                    seed_id TEXT,
                    seed_content TEXT,
                    seed_path TEXT,
                    job_id TEXT,
                    last_job_cursor INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT,
                    execution_id TEXT,
                    pr_url TEXT,
                    final_result TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS ix_openclaw_workflows_channel_stage
                    ON openclaw_workflows(channel_key, stage);
                CREATE INDEX IF NOT EXISTS ix_openclaw_workflows_channel_queued_at
                    ON openclaw_workflows(channel_key, queued_at);
                CREATE INDEX IF NOT EXISTS ix_openclaw_workflows_fingerprint
                    ON openclaw_workflows(channel_key, request_fingerprint);
                CREATE INDEX IF NOT EXISTS ix_openclaw_workflows_job_id
                    ON openclaw_workflows(job_id);

                CREATE TABLE IF NOT EXISTS openclaw_processed_events (
                    event_key TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    processed_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                """
            )

    # ---- channel repo mapping -------------------------------------------------

    def get_repo(self, channel_key: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT repo FROM openclaw_channel_repos WHERE channel_key = ?",
                (channel_key,),
            ).fetchone()
            return str(row["repo"]) if row is not None else None

    def set_repo(
        self, *, channel_key: str, channel_id: str, guild_id: str | None, repo: str
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO openclaw_channel_repos(channel_key, channel_id, guild_id, repo)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(channel_key) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    guild_id = excluded.guild_id,
                    repo = excluded.repo,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (channel_key, channel_id, guild_id, repo),
            )

    def remove_repo(self, channel_key: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM openclaw_channel_repos WHERE channel_key = ?",
                (channel_key,),
            )

    # ---- workflows -----------------------------------------------------------

    def upsert_workflow(self, payload: dict[str, Any]) -> None:
        columns = [
            "workflow_id",
            "channel_key",
            "channel_id",
            "guild_id",
            "user_id",
            "repo",
            "request_message",
            "entry_point",
            "request_fingerprint",
            "stage",
            "queued_at",
            "started_at",
            "completed_at",
            "interview_session_id",
            "seed_id",
            "seed_content",
            "seed_path",
            "job_id",
            "last_job_cursor",
            "session_id",
            "execution_id",
            "pr_url",
            "final_result",
            "error",
        ]
        values = [payload.get(column) for column in columns]
        assignments = ", ".join(f"{column}=excluded.{column}" for column in columns[1:])
        with self._connection() as conn:
            conn.execute(
                f"""
                INSERT INTO openclaw_workflows ({", ".join(columns)})
                VALUES ({", ".join("?" for _ in columns)})
                ON CONFLICT(workflow_id) DO UPDATE SET
                    {assignments}
                """,
                values,
            )

    def get_workflow(self, workflow_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM openclaw_workflows WHERE workflow_id = ?",
                (workflow_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    def latest_for_channel(self, channel_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM openclaw_workflows
                WHERE channel_key = ?
                ORDER BY queued_at DESC
                LIMIT 1
                """,
                (channel_key,),
            ).fetchone()
            return dict(row) if row is not None else None

    def latest_terminal_for_channel(self, channel_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM openclaw_workflows
                WHERE channel_key = ?
                  AND stage IN ('completed', 'failed')
                ORDER BY queued_at DESC
                LIMIT 1
                """,
                (channel_key,),
            ).fetchone()
            return dict(row) if row is not None else None

    def active_for_channel(self, channel_key: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM openclaw_workflows
                WHERE channel_key = ?
                  AND stage IN ('interviewing', 'seed_generation', 'executing')
                ORDER BY started_at DESC, queued_at DESC
                LIMIT 1
                """,
                (channel_key,),
            ).fetchone()
            return dict(row) if row is not None else None

    def queued_for_channel(self, channel_key: str) -> list[dict[str, Any]]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM openclaw_workflows
                WHERE channel_key = ?
                  AND stage = 'queued'
                ORDER BY queued_at ASC
                """,
                (channel_key,),
            ).fetchall()
            return [dict(row) for row in rows]

    def inflight_duplicate(
        self, channel_key: str, request_fingerprint: str
    ) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM openclaw_workflows
                WHERE channel_key = ?
                  AND request_fingerprint = ?
                  AND stage IN ('queued', 'interviewing', 'seed_generation', 'executing')
                ORDER BY queued_at DESC
                LIMIT 1
                """,
                (channel_key, request_fingerprint),
            ).fetchone()
            return dict(row) if row is not None else None

    def find_by_job_id(self, job_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM openclaw_workflows WHERE job_id = ? LIMIT 1",
                (job_id,),
            ).fetchone()
            return dict(row) if row is not None else None

    # ---- event dedup ----------------------------------------------------------

    def is_event_processed(self, event_key: str) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM openclaw_processed_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            return row is not None

    def mark_event_processed(self, event_key: str, workflow_id: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO openclaw_processed_events (event_key, workflow_id)
                VALUES (?, ?)
                """,
                (event_key, workflow_id),
            )
