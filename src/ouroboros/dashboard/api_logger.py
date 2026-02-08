"""Gemini API Call/Response Logging System.

This module provides comprehensive logging for all Gemini 3 API interactions,
enabling visualization of the decision-making process in the Streamlit dashboard.

Features:
- Request/Response logging with timestamps
- Token usage tracking
- Latency measurement
- Structured log storage for visualization
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from collections.abc import AsyncGenerator

import aiosqlite
import structlog

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class APILogEntry:
    """Single API log entry.

    Attributes:
        request_id: Unique identifier for this request
        timestamp: When the request was made
        model: Model used (e.g., gemini-2.5-pro)
        request_type: Type of request (analysis/critique/etc.)
        prompt_preview: First 500 chars of the prompt
        prompt_tokens: Number of tokens in the prompt
        response_preview: First 500 chars of the response
        completion_tokens: Number of tokens in the response
        total_tokens: Total tokens used
        latency_ms: Time taken in milliseconds
        status: "success" or "error"
        error_message: Error message if status is "error"
        metadata: Additional metadata
    """
    request_id: str
    timestamp: datetime
    model: str
    request_type: str
    prompt_preview: str
    prompt_tokens: int
    response_preview: str
    completion_tokens: int
    total_tokens: int
    latency_ms: float
    status: str
    error_message: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class GeminiAPILogger:
    """Comprehensive API logger for Gemini 3 interactions.

    This logger stores all API interactions in SQLite for persistence
    and provides real-time streaming for the Streamlit dashboard.

    Usage:
        logger = GeminiAPILogger()
        await logger.initialize()

        # Log a request
        await logger.log_request(
            request_id="req-123",
            model="gemini-2.5-pro",
            messages=[...],
            token_estimate=50000,
        )

        # Log the response
        await logger.log_response(
            request_id="req-123",
            response="...",
            usage={"prompt_tokens": 50000, "completion_tokens": 1000},
            elapsed_ms=2500.0,
        )

        # Stream logs for real-time dashboard
        async for entry in logger.stream_logs():
            print(f"[{entry.timestamp}] {entry.request_id}: {entry.status}")
    """

    def __init__(
        self,
        *,
        db_path: str | Path | None = None,
        preview_length: int = 500,
    ) -> None:
        """Initialize the API logger.

        Args:
            db_path: Path to SQLite database. Defaults to ~/.ouroboros/gemini_logs.db
            preview_length: Max chars to store for previews.
        """
        if db_path is None:
            db_path = Path.home() / ".ouroboros" / "gemini_logs.db"
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._preview_length = preview_length
        self._connection: aiosqlite.Connection | None = None
        self._pending_requests: dict[str, dict[str, Any]] = {}
        self._subscribers: list[asyncio.Queue[APILogEntry]] = []

    async def initialize(self) -> None:
        """Initialize the database connection and create tables."""
        self._connection = await aiosqlite.connect(str(self._db_path))

        await self._connection.execute("""
            CREATE TABLE IF NOT EXISTS api_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL,
                model TEXT NOT NULL,
                request_type TEXT NOT NULL,
                prompt_preview TEXT,
                prompt_tokens INTEGER,
                response_preview TEXT,
                completion_tokens INTEGER,
                total_tokens INTEGER,
                latency_ms REAL,
                status TEXT NOT NULL,
                error_message TEXT,
                metadata TEXT
            )
        """)

        await self._connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_api_logs_timestamp
            ON api_logs (timestamp DESC)
        """)

        await self._connection.commit()

        log.info("gemini_logger.initialized", db_path=str(self._db_path))

    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    def _truncate(self, text: str) -> str:
        """Truncate text to preview length."""
        if len(text) <= self._preview_length:
            return text
        return text[:self._preview_length] + "..."

    def _extract_request_type(self, messages: list[dict[str, str]]) -> str:
        """Extract request type from messages."""
        if not messages:
            return "unknown"

        content = messages[0].get("content", "").lower()
        if "devil's advocate" in content:
            return "devil_advocate"
        if "complete iteration history" in content:
            return "full_history_analysis"
        if "pattern" in content:
            return "pattern_analysis"
        return "general"

    async def log_request(
        self,
        *,
        request_id: str,
        model: str,
        messages: list[dict[str, str]],
        token_estimate: int,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Log an outgoing API request.

        Args:
            request_id: Unique identifier for this request.
            model: Model being called.
            messages: List of message dicts.
            token_estimate: Estimated token count.
            metadata: Additional metadata.
        """
        prompt_preview = ""
        if messages:
            prompt_preview = self._truncate(messages[0].get("content", ""))

        request_type = self._extract_request_type(messages)

        # Store pending request for later completion
        self._pending_requests[request_id] = {
            "timestamp": datetime.now(),
            "model": model,
            "request_type": request_type,
            "prompt_preview": prompt_preview,
            "prompt_tokens": token_estimate,
            "metadata": metadata or {},
            "start_time": time.perf_counter(),
        }

        log.debug(
            "gemini_logger.request_logged",
            request_id=request_id,
            model=model,
            request_type=request_type,
            token_estimate=token_estimate,
        )

    async def log_response(
        self,
        *,
        request_id: str,
        response: str,
        usage: dict[str, int],
        elapsed_ms: float,
    ) -> None:
        """Log a successful API response.

        Args:
            request_id: Matching request ID.
            response: Response content.
            usage: Token usage dict.
            elapsed_ms: Time taken in milliseconds.
        """
        if request_id not in self._pending_requests:
            log.warning(
                "gemini_logger.orphan_response",
                request_id=request_id,
            )
            return

        pending = self._pending_requests.pop(request_id)

        entry = APILogEntry(
            request_id=request_id,
            timestamp=pending["timestamp"],
            model=pending["model"],
            request_type=pending["request_type"],
            prompt_preview=pending["prompt_preview"],
            prompt_tokens=usage.get("prompt_tokens", pending["prompt_tokens"]),
            response_preview=self._truncate(response),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            latency_ms=elapsed_ms,
            status="success",
            metadata=pending["metadata"],
        )

        await self._persist_entry(entry)
        await self._notify_subscribers(entry)

        log.info(
            "gemini_logger.response_logged",
            request_id=request_id,
            total_tokens=entry.total_tokens,
            latency_ms=elapsed_ms,
        )

    async def log_error(
        self,
        *,
        request_id: str,
        error: str,
        elapsed_ms: float,
    ) -> None:
        """Log a failed API request.

        Args:
            request_id: Matching request ID.
            error: Error message.
            elapsed_ms: Time taken before failure.
        """
        if request_id not in self._pending_requests:
            log.warning(
                "gemini_logger.orphan_error",
                request_id=request_id,
            )
            return

        pending = self._pending_requests.pop(request_id)

        entry = APILogEntry(
            request_id=request_id,
            timestamp=pending["timestamp"],
            model=pending["model"],
            request_type=pending["request_type"],
            prompt_preview=pending["prompt_preview"],
            prompt_tokens=pending["prompt_tokens"],
            response_preview="",
            completion_tokens=0,
            total_tokens=0,
            latency_ms=elapsed_ms,
            status="error",
            error_message=error,
            metadata=pending["metadata"],
        )

        await self._persist_entry(entry)
        await self._notify_subscribers(entry)

        log.warning(
            "gemini_logger.error_logged",
            request_id=request_id,
            error=error,
        )

    async def _persist_entry(self, entry: APILogEntry) -> None:
        """Persist a log entry to the database."""
        if not self._connection:
            log.warning("gemini_logger.not_initialized")
            return

        await self._connection.execute(
            """
            INSERT INTO api_logs (
                request_id, timestamp, model, request_type,
                prompt_preview, prompt_tokens,
                response_preview, completion_tokens, total_tokens,
                latency_ms, status, error_message, metadata
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.request_id,
                entry.timestamp.isoformat(),
                entry.model,
                entry.request_type,
                entry.prompt_preview,
                entry.prompt_tokens,
                entry.response_preview,
                entry.completion_tokens,
                entry.total_tokens,
                entry.latency_ms,
                entry.status,
                entry.error_message,
                json.dumps(entry.metadata),
            ),
        )
        await self._connection.commit()

    async def _notify_subscribers(self, entry: APILogEntry) -> None:
        """Notify all subscribers of a new entry."""
        for queue in self._subscribers:
            try:
                queue.put_nowait(entry)
            except asyncio.QueueFull:
                # Drop oldest if queue is full
                try:
                    queue.get_nowait()
                    queue.put_nowait(entry)
                except asyncio.QueueEmpty:
                    pass

    async def get_recent_logs(
        self,
        limit: int = 100,
        request_type: str | None = None,
    ) -> list[APILogEntry]:
        """Get recent log entries.

        Args:
            limit: Maximum entries to return.
            request_type: Optional filter by request type.

        Returns:
            List of log entries, most recent first.
        """
        if not self._connection:
            return []

        query = "SELECT * FROM api_logs"
        params: list[Any] = []

        if request_type:
            query += " WHERE request_type = ?"
            params.append(request_type)

        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)

        async with self._connection.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        return [
            APILogEntry(
                request_id=row[1],
                timestamp=datetime.fromisoformat(row[2]),
                model=row[3],
                request_type=row[4],
                prompt_preview=row[5] or "",
                prompt_tokens=row[6] or 0,
                response_preview=row[7] or "",
                completion_tokens=row[8] or 0,
                total_tokens=row[9] or 0,
                latency_ms=row[10] or 0,
                status=row[11],
                error_message=row[12] or "",
                metadata=json.loads(row[13]) if row[13] else {},
            )
            for row in rows
        ]

    async def get_token_usage_summary(self) -> dict[str, Any]:
        """Get summary of token usage.

        Returns:
            Dictionary with usage statistics.
        """
        if not self._connection:
            return {}

        async with self._connection.execute("""
            SELECT
                COUNT(*) as total_requests,
                SUM(prompt_tokens) as total_prompt_tokens,
                SUM(completion_tokens) as total_completion_tokens,
                SUM(total_tokens) as total_tokens,
                AVG(latency_ms) as avg_latency_ms,
                MIN(latency_ms) as min_latency_ms,
                MAX(latency_ms) as max_latency_ms,
                SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) as error_count
            FROM api_logs
        """) as cursor:
            row = await cursor.fetchone()

        if not row:
            return {}

        return {
            "total_requests": row[0] or 0,
            "total_prompt_tokens": row[1] or 0,
            "total_completion_tokens": row[2] or 0,
            "total_tokens": row[3] or 0,
            "avg_latency_ms": row[4] or 0,
            "min_latency_ms": row[5] or 0,
            "max_latency_ms": row[6] or 0,
            "success_count": row[7] or 0,
            "error_count": row[8] or 0,
            "success_rate": (row[7] / row[0] * 100) if row[0] else 0,
        }

    def subscribe(self) -> asyncio.Queue[APILogEntry]:
        """Subscribe to real-time log updates.

        Returns:
            Queue that receives new log entries.
        """
        queue: asyncio.Queue[APILogEntry] = asyncio.Queue(maxsize=100)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[APILogEntry]) -> None:
        """Unsubscribe from log updates.

        Args:
            queue: Queue to remove from subscribers.
        """
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    async def stream_logs(self) -> AsyncGenerator[APILogEntry, None]:
        """Stream log entries in real-time.

        Yields:
            New log entries as they arrive.
        """
        queue = self.subscribe()
        try:
            while True:
                entry = await queue.get()
                yield entry
        finally:
            self.unsubscribe(queue)


__all__ = [
    "APILogEntry",
    "GeminiAPILogger",
]
