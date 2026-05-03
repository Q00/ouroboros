"""EventStore implementation for event sourcing.

Provides async methods for appending and replaying events using SQLAlchemy Core
with aiosqlite backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Any

from sqlalchemy import and_, event, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.schema import events_table, metadata

logger = logging.getLogger(__name__)

_RAW_SUBSCRIBED_EVENT_TYPE_KEYS = frozenset({"type", "event", "kind", "name"})
_RAW_SUBSCRIBED_EVENT_SIGNAL_KEYS = frozenset(
    {
        "args",
        "arguments",
        "command",
        "content",
        "delta",
        "error",
        "input",
        "message",
        "params",
        "path",
        "payload",
        "result",
        "run_id",
        "server_run_id",
        "server_session_id",
        "session",
        "session_id",
        "summary",
        "text",
        "thread_id",
        "tool",
        "tool_name",
    }
)


def _normalized_mapping_keys(value: Mapping[object, object]) -> set[str]:
    """Return normalized string keys for mapping inspection."""
    return {str(key).strip().lower().replace("-", "_") for key in value}


def _looks_like_raw_subscribed_event_payload(value: object) -> bool:
    """Return True when the value resembles a subscribed runtime stream event."""
    if not isinstance(value, Mapping):
        return False

    normalized_keys = _normalized_mapping_keys(value)
    if {"aggregate_type", "aggregate_id", "data"} <= normalized_keys:
        return False

    if not (_RAW_SUBSCRIBED_EVENT_TYPE_KEYS & normalized_keys):
        return False

    return bool(_RAW_SUBSCRIBED_EVENT_SIGNAL_KEYS & normalized_keys)


def _session_related_event_conditions(
    session_id: str,
    execution_id: str | None,
) -> list[Any]:
    """Build aggregate-id predicates for a session and its execution scopes."""
    conditions: list[Any] = [
        events_table.c.aggregate_id == session_id,
        func.json_extract(events_table.c.payload, "$.session_id") == session_id,
    ]
    if not execution_id:
        return conditions

    conditions.append(events_table.c.aggregate_id == execution_id)
    conditions.append(func.json_extract(events_table.c.payload, "$.execution_id") == execution_id)
    conditions.append(
        func.json_extract(events_table.c.payload, "$.parent_execution_id") == execution_id
    )

    return conditions


class EventStore:
    """Event store for persisting and replaying events.

    Uses SQLAlchemy Core with aiosqlite for async database operations.
    All operations are transactional for atomicity.

    Usage:
        store = EventStore("sqlite+aiosqlite:///ouroboros.db")
        await store.initialize()

        # Append event
        await store.append(event)

        # Replay events for an aggregate
        events = await store.replay("seed", "seed-123")

        # Close when done
        await store.close()
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        """Initialize EventStore with database URL.

        Args:
            database_url: SQLAlchemy database URL.
                         For async SQLite: "sqlite+aiosqlite:///path/to/db.sqlite"
                         If not provided, defaults to ~/.ouroboros/ouroboros.db
            read_only: When True, open the underlying SQLite database in true
                read-only mode by rewriting the URL into the ``file:<path>?mode=ro&uri=true``
                form and passing ``connect_args={"uri": True}`` to aiosqlite.
                This enforces the read-only contract at the connection layer
                so *any* accidental write path (including library/future code
                paths we don't control) fails fast with
                ``sqlite3.OperationalError: attempt to write a readonly database``.
                Callers that opt in should also skip schema creation by calling
                ``initialize(create_schema=False)`` — this is the default when
                ``read_only=True``. ``read_only`` is a no-op for non-SQLite URLs.
        """
        if database_url is None:
            db_path = Path.home() / ".ouroboros" / "ouroboros.db"
            if not read_only:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite+aiosqlite:///{db_path}"

        self._read_only = read_only
        if read_only:
            database_url = self._coerce_to_readonly_url(database_url)
        self._database_url = database_url
        self._engine: AsyncEngine | None = None

    @staticmethod
    def _coerce_to_readonly_url(database_url: str) -> str:
        """Rewrite a plain aiosqlite URL into a ``mode=ro`` URI form.

        Leaves non-SQLite URLs untouched. Already-URI forms (starting with
        ``file:``) are returned as-is so explicit callers keep full control.
        """
        prefix = "sqlite+aiosqlite:///"
        if not database_url.startswith(prefix):
            return database_url

        path_part = database_url[len(prefix) :]
        if path_part.startswith("file:"):
            # Caller already provided a URI form — respect it verbatim.
            return database_url

        # ``:memory:`` has no filesystem and cannot be opened read-only
        # meaningfully; leave it alone.
        if path_part in (":memory:", ""):
            return database_url

        return f"{prefix}file:{path_part}?mode=ro&uri=true"

    def _raise_invalid_append_input(
        self,
        event: object,
        *,
        operation: str,
        index: int | None = None,
    ) -> None:
        """Raise a persistence error for invalid append inputs."""
        details = {"received_type": type(event).__name__}
        if index is not None:
            details["event_index"] = index

        if isinstance(event, Mapping):
            details["received_keys"] = sorted(_normalized_mapping_keys(event))[:12]
            if _looks_like_raw_subscribed_event_payload(event):
                raise PersistenceError(
                    "EventStore rejects raw subscribed event stream payloads. "
                    "Normalize them into BaseEvent records before persistence.",
                    operation=operation,
                    details=details,
                )

        raise PersistenceError(
            "EventStore only persists BaseEvent instances.",
            operation=operation,
            details=details,
        )

    async def initialize(self, *, create_schema: bool | None = None) -> None:
        """Initialize the database connection and create tables if needed.

        This method is idempotent - calling it multiple times is safe.

        Args:
            create_schema: When True run ``metadata.create_all`` so missing
                tables are created. Read-only consumers (for example diagnostic
                CLI commands that must not mutate the store) can pass ``False``
                to skip schema creation entirely. When ``None`` (default), the
                value follows ``read_only``: stores constructed with
                ``read_only=True`` skip schema creation and all others create
                it, preserving the prior default behaviour.

        For aiosqlite, uses StaticPool (default) which maintains a single
        connection. This avoids connection accumulation while supporting
        :memory: databases in tests.
        """
        if create_schema is None:
            create_schema = not self._read_only

        if self._read_only and create_schema:
            raise PersistenceError(
                "Cannot create schema on a read-only EventStore.",
                operation="initialize",
                details={"read_only": True},
            )

        if self._engine is None:
            connect_args: dict[str, object] = {"timeout": 30}
            if self._read_only:
                # aiosqlite forwards unknown kwargs to sqlite3.connect — the
                # ``uri=True`` flag is what turns the ``file:...?mode=ro`` form
                # into a real read-only connection.
                connect_args["uri"] = True

            self._engine = create_async_engine(
                self._database_url,
                echo=False,
                connect_args=connect_args,
            )

            # Enable WAL mode and set busy timeout on every new connection.
            # Skipped for read-only consumers: ``PRAGMA journal_mode=WAL`` is
            # itself a write and would trip SQLite's read-only guard.
            if not self._read_only:

                @event.listens_for(self._engine.sync_engine, "connect")
                def _set_sqlite_pragmas(dbapi_conn, _connection_record):
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.close()

        # Create all tables defined in metadata (skipped for read-only consumers)
        if create_schema:
            async with self._engine.begin() as conn:
                await conn.run_sync(metadata.create_all)

    async def append(self, event: BaseEvent) -> None:
        """Append an event to the store.

        The operation is wrapped in a transaction for atomicity.
        If the insert fails, the transaction is rolled back.

        Args:
            event: The event to append.

        Raises:
            PersistenceError: If the append operation fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="append",
            )
        if not isinstance(event, BaseEvent):
            self._raise_invalid_append_input(event, operation="append")

        for attempt in range(3):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(events_table.insert().values(**event.to_db_dict()))
                return
            except Exception as e:
                if "database is locked" in str(e) and attempt < 2:
                    logger.warning(
                        "event_store.append.retry",
                        extra={"attempt": attempt + 1, "event_id": event.id},
                    )
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise PersistenceError(
                    f"Failed to append event: {e}",
                    operation="insert",
                    table="events",
                    details={"event_id": event.id, "event_type": event.type},
                ) from e

    async def append_batch(self, events: list[BaseEvent]) -> None:
        """Append multiple events atomically in a single transaction.

        All events are inserted in a single transaction. If any insert fails,
        the entire batch is rolled back, ensuring atomicity.

        This is more efficient than calling append() multiple times and
        guarantees that either all events are persisted or none are.

        Args:
            events: List of events to append.

        Raises:
            PersistenceError: If the batch operation fails. No events
                             will be persisted if this is raised.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="append_batch",
            )

        if not events:
            return  # Nothing to do
        invalid_events = [
            (index, event) for index, event in enumerate(events) if not isinstance(event, BaseEvent)
        ]
        if invalid_events:
            invalid_index, invalid_event = invalid_events[0]
            self._raise_invalid_append_input(
                invalid_event,
                operation="append_batch",
                index=invalid_index,
            )

        for attempt in range(3):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        events_table.insert(),
                        [event.to_db_dict() for event in events],
                    )
                return
            except Exception as e:
                if "database is locked" in str(e) and attempt < 2:
                    logger.warning(
                        "event_store.append_batch.retry",
                        extra={"attempt": attempt + 1, "batch_size": len(events)},
                    )
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise PersistenceError(
                    f"Failed to append event batch: {e}",
                    operation="insert_batch",
                    table="events",
                    details={
                        "batch_size": len(events),
                        "event_ids": [e.id for e in events[:5]],
                    },
                ) from e

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        """Replay all events for a specific aggregate.

        The operation uses a transaction for read consistency.

        Args:
            aggregate_type: The type of aggregate (e.g., "seed", "execution").
            aggregate_id: The unique identifier of the aggregate.

        Returns:
            List of events for the aggregate, ordered by timestamp.

        Raises:
            PersistenceError: If the replay operation fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="replay",
            )

        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    select(events_table)
                    .where(events_table.c.aggregate_type == aggregate_type)
                    .where(events_table.c.aggregate_id == aggregate_id)
                    # Order by timestamp + id for deterministic replay when
                    # multiple events share the same timestamp resolution.
                    .order_by(events_table.c.timestamp, events_table.c.id)
                )
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to replay events: {e}",
                operation="select",
                table="events",
                details={
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                },
            ) from e

    async def get_events_after(
        self,
        aggregate_type: str,
        aggregate_id: str,
        last_row_id: int = 0,
    ) -> tuple[list[BaseEvent], int]:
        """Get events for an aggregate after a given row ID.

        Incremental fetch that only returns new events since the last poll,
        avoiding the O(n) cost of replaying the full event history.

        Args:
            aggregate_type: The type of aggregate (e.g., "execution").
            aggregate_id: The unique identifier of the aggregate.
            last_row_id: The SQLite rowid of the last event processed.
                         Pass 0 to get all events from the beginning.

        Returns:
            Tuple of (list of new events, max rowid seen).
            The max rowid should be passed back as last_row_id on the next call.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_events_after",
            )

        try:
            async with self._engine.begin() as conn:
                # Use SQLite's implicit rowid for efficient cursor-based pagination.
                # This avoids deserializing all prior events just to slice the tail.
                rowid_col = text("rowid")
                result = await conn.execute(
                    select(events_table, rowid_col)
                    .where(events_table.c.aggregate_type == aggregate_type)
                    .where(events_table.c.aggregate_id == aggregate_id)
                    .where(text("rowid > :last_id").bindparams(last_id=last_row_id))
                    .order_by(events_table.c.timestamp, events_table.c.id)
                )
                rows = result.mappings().all()
                if not rows:
                    return [], last_row_id
                events = [BaseEvent.from_db_row(dict(row)) for row in rows]
                max_rowid = max(row["rowid"] for row in rows)
                return events, max_rowid
        except Exception as e:
            raise PersistenceError(
                f"Failed to get events after rowid {last_row_id}: {e}",
                operation="select",
                table="events",
                details={
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "last_row_id": last_row_id,
                },
            ) from e

    async def get_current_rowid(self) -> int:
        """Return the current maximum event-store rowid.

        Callers can use this as a global cursor baseline before starting work,
        then query ``rowid > baseline`` across any aggregate discovered later.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_current_rowid",
            )

        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    select(func.coalesce(func.max(text("rowid")), 0)).select_from(events_table)
                )
                return int(result.scalar_one() or 0)
        except Exception as e:
            raise PersistenceError(
                f"Failed to get current rowid: {e}",
                operation="select",
                table="events",
            ) from e

    async def get_recent_events(
        self, event_type: str | None = None, limit: int = 100
    ) -> list[BaseEvent]:
        """Get recent events, optionally filtered by type.

        Args:
            event_type: Optional event type to filter by.
            limit: Maximum number of events to return.

        Returns:
            List of recent events, ordered by timestamp descending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_recent_events",
            )

        try:
            async with self._engine.begin() as conn:
                query = select(events_table).order_by(events_table.c.timestamp.desc()).limit(limit)

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get recent events: {e}",
                operation="select",
                table="events",
            ) from e

    async def get_all_sessions(self) -> list[BaseEvent]:
        """Get all session lifecycle events.

        Returns all ``orchestrator.session.*`` events ordered by timestamp
        ascending so callers can replay them to reconstruct current status.

        Returns:
            List of session events, ordered by timestamp ascending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_all_sessions",
            )

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(events_table)
                    .where(events_table.c.event_type.like("orchestrator.session.%"))
                    .order_by(events_table.c.timestamp.asc())
                )

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get all sessions: {e}",
                operation="select",
                table="events",
                details={"event_type": "orchestrator.session.%"},
            ) from e

    async def get_session_activity_snapshots(self) -> list[SessionActivitySnapshot]:
        """Return one session snapshot row per session aggregate.

        The snapshot includes session identity from the start event, the most
        recent session activity timestamp, and the latest status-bearing event
        or runtime_status payload when present. This avoids replaying every
        event for every session just to detect stale active sessions.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_session_activity_snapshots",
            )

        status_expr = func.coalesce(
            func.json_extract(events_table.c.payload, "$.progress.runtime_status"),
            func.json_extract(events_table.c.payload, "$.runtime_status"),
        )

        first_session_event_ranked = (
            select(
                events_table.c.aggregate_id.label("session_id"),
                func.json_extract(events_table.c.payload, "$.execution_id").label("execution_id"),
                func.json_extract(events_table.c.payload, "$.seed_id").label("seed_id"),
                func.coalesce(
                    func.json_extract(events_table.c.payload, "$.start_time"),
                    events_table.c.timestamp,
                ).label("start_time"),
                func.row_number()
                .over(
                    partition_by=events_table.c.aggregate_id,
                    order_by=(events_table.c.timestamp.asc(), events_table.c.id.asc()),
                )
                .label("rn"),
            )
            .where(events_table.c.aggregate_type == "session")
            .subquery()
        )

        latest_activity_ranked = (
            select(
                events_table.c.aggregate_id.label("session_id"),
                events_table.c.timestamp.label("last_activity"),
                func.row_number()
                .over(
                    partition_by=events_table.c.aggregate_id,
                    order_by=(events_table.c.timestamp.desc(), events_table.c.id.desc()),
                )
                .label("rn"),
            )
            .where(events_table.c.aggregate_type == "session")
            .subquery()
        )

        latest_status_ranked = (
            select(
                events_table.c.aggregate_id.label("session_id"),
                events_table.c.event_type.label("status_event_type"),
                status_expr.label("runtime_status"),
                func.row_number()
                .over(
                    partition_by=events_table.c.aggregate_id,
                    order_by=(events_table.c.timestamp.desc(), events_table.c.id.desc()),
                )
                .label("rn"),
            )
            .where(events_table.c.aggregate_type == "session")
            .where(
                or_(
                    events_table.c.event_type.in_(
                        (
                            "orchestrator.session.completed",
                            "orchestrator.session.failed",
                            "orchestrator.session.paused",
                            "orchestrator.session.cancelled",
                        )
                    ),
                    and_(
                        events_table.c.event_type.in_(
                            (
                                "orchestrator.progress.updated",
                                "workflow.progress.updated",
                            )
                        ),
                        status_expr.is_not(None),
                    ),
                )
            )
            .subquery()
        )

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(
                        first_session_event_ranked.c.session_id,
                        first_session_event_ranked.c.execution_id,
                        first_session_event_ranked.c.seed_id,
                        first_session_event_ranked.c.start_time,
                        latest_activity_ranked.c.last_activity,
                        latest_status_ranked.c.status_event_type,
                        latest_status_ranked.c.runtime_status,
                    )
                    .select_from(first_session_event_ranked)
                    .join(
                        latest_activity_ranked,
                        and_(
                            latest_activity_ranked.c.session_id
                            == first_session_event_ranked.c.session_id,
                            latest_activity_ranked.c.rn == 1,
                        ),
                    )
                    .outerjoin(
                        latest_status_ranked,
                        and_(
                            latest_status_ranked.c.session_id
                            == first_session_event_ranked.c.session_id,
                            latest_status_ranked.c.rn == 1,
                        ),
                    )
                    .where(first_session_event_ranked.c.rn == 1)
                    .order_by(first_session_event_ranked.c.session_id.asc())
                )

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [
                    SessionActivitySnapshot(
                        session_id=row["session_id"],
                        execution_id=row.get("execution_id"),
                        seed_id=row.get("seed_id"),
                        start_time=row.get("start_time"),
                        last_activity=row.get("last_activity"),
                        status_event_type=row.get("status_event_type"),
                        runtime_status=row.get("runtime_status"),
                    )
                    for row in rows
                ]
        except Exception as e:
            raise PersistenceError(
                f"Failed to fetch session activity snapshots: {e}",
                operation="select",
                table="events",
            ) from e

    async def query_events(
        self,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        """Query events with optional filters.

        Args:
            aggregate_id: Optional aggregate ID to filter by (e.g., session_id).
            event_type: Optional event type to filter by.
            limit: Maximum number of events to return.
            offset: Number of events to skip for pagination.

        Returns:
            List of events matching the criteria, ordered by timestamp descending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_events",
            )

        try:
            async with self._engine.begin() as conn:
                query = select(events_table).order_by(events_table.c.timestamp.desc())

                if aggregate_id:
                    query = query.where(events_table.c.aggregate_id == aggregate_id)

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                query = query.limit(limit).offset(offset)

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to query events: {e}",
                operation="select",
                table="events",
                details={
                    "aggregate_id": aggregate_id,
                    "event_type": event_type,
                    "limit": limit,
                    "offset": offset,
                },
            ) from e

    async def query_session_related_events(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        """Query events across the session aggregate and related parallel scopes.

        Parallel execution stores activity in several aggregate families:
        - ``session/<session_id>`` for top-level session state
        - ``execution/<execution_id>`` for workflow progress
        - ``execution/*`` runtime scopes whose payload references the session,
          exact execution ID, or exact parent execution ID

        Related execution scopes are matched through persisted ``session_id`` /
        ``execution_id`` / ``parent_execution_id`` payload fields instead of
        normalized aggregate-name prefixes. Runtime aggregate names intentionally
        normalize dynamic IDs for filesystem/path safety, so using those lossy
        names as join keys can collide across distinct executions.

        Args:
            session_id: Orchestrator session ID.
            execution_id: Optional execution ID. If omitted, it is resolved from
                the session's start event when possible.
            event_type: Optional event-type filter.
            limit: Maximum number of events to return. ``None`` returns all.
            offset: Number of events to skip for pagination.

        Returns:
            Matching events ordered by timestamp descending.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_session_related_events",
            )

        resolved_execution_id = execution_id or await self._resolve_execution_id_for_session(
            session_id,
        )

        conditions = _session_related_event_conditions(session_id, resolved_execution_id)

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(events_table)
                    .where(or_(*conditions))
                    .order_by(events_table.c.timestamp.desc())
                )

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                if limit is not None:
                    query = query.limit(limit).offset(offset)
                elif offset:
                    query = query.offset(offset)

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to query session-related events: {e}",
                operation="select",
                table="events",
                details={
                    "session_id": session_id,
                    "execution_id": resolved_execution_id,
                    "event_type": event_type,
                    "limit": limit,
                    "offset": offset,
                },
            ) from e

    async def query_session_related_events_after(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        last_row_id: int = 0,
    ) -> tuple[list[BaseEvent], int]:
        """Incrementally query events across a session and related execution scopes.

        This is the multi-aggregate equivalent of ``get_events_after``. It uses
        the same exact session/execution payload predicates as
        ``query_session_related_events`` while advancing a single rowid cursor.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_session_related_events_after",
            )

        resolved_execution_id = execution_id or await self._resolve_execution_id_for_session(
            session_id,
        )
        conditions = _session_related_event_conditions(session_id, resolved_execution_id)

        try:
            async with self._engine.begin() as conn:
                rowid_col = text("rowid")
                query = (
                    select(events_table, rowid_col)
                    .where(or_(*conditions))
                    .where(text("rowid > :last_id").bindparams(last_id=last_row_id))
                    .order_by(events_table.c.timestamp, events_table.c.id)
                )

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                result = await conn.execute(query)
                rows = result.mappings().all()
                if not rows:
                    return [], last_row_id

                events = [BaseEvent.from_db_row(dict(row)) for row in rows]
                max_rowid = max(row["rowid"] for row in rows)
                return events, max_rowid
        except Exception as e:
            raise PersistenceError(
                f"Failed to query session-related events after rowid {last_row_id}: {e}",
                operation="select",
                table="events",
                details={
                    "session_id": session_id,
                    "execution_id": resolved_execution_id,
                    "event_type": event_type,
                    "last_row_id": last_row_id,
                },
            ) from e

    async def _resolve_execution_id_for_session(self, session_id: str) -> str | None:
        """Return the execution ID referenced by a session start event, if present."""
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="resolve_execution_id_for_session",
            )

        async with self._engine.begin() as conn:
            query = (
                select(events_table)
                .where(events_table.c.aggregate_type == "session")
                .where(events_table.c.aggregate_id == session_id)
                .where(events_table.c.event_type == "orchestrator.session.started")
                .order_by(events_table.c.timestamp.asc())
                .limit(1)
            )
            result = await conn.execute(query)
            row = result.mappings().first()
            if row is None:
                return None
            payload = row.get("payload")
            if isinstance(payload, Mapping):
                execution_id = payload.get("execution_id")
                if isinstance(execution_id, str) and execution_id:
                    return execution_id
            return None

    async def get_all_lineages(self) -> list[BaseEvent]:
        """Get all lineage creation events.

        Retrieves all events of type 'lineage.created' to identify every
        evolutionary lineage recorded in the event store.

        Returns:
            List of lineage creation events, ordered by timestamp descending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_all_lineages",
            )

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(events_table)
                    .where(events_table.c.event_type == "lineage.created")
                    .order_by(events_table.c.timestamp.desc())
                )

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get all lineages: {e}",
                operation="select",
                table="events",
                details={"event_type": "lineage.created"},
            ) from e

    async def replay_lineage(self, lineage_id: str) -> list[BaseEvent]:
        """Replay all events for a lineage aggregate.

        Convenience method for evolutionary loop lineage reconstruction.

        Args:
            lineage_id: The unique identifier of the lineage.

        Returns:
            List of lineage events, ordered by timestamp.

        Raises:
            PersistenceError: If the replay operation fails.
        """
        return await self.replay("lineage", lineage_id)

    async def close(self) -> None:
        """Close the database connection."""
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None


@dataclass(frozen=True, slots=True)
class SessionActivitySnapshot:
    """Session start/activity/status summary used by orphan detection."""

    session_id: str
    execution_id: str | None
    seed_id: str | None
    start_time: str | None
    last_activity: object
    status_event_type: str | None
    runtime_status: str | None
