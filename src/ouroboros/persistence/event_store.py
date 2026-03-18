"""EventStore implementation for event sourcing.

Provides async methods for appending and replaying events using SQLAlchemy Core
with aiosqlite backend.
"""

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path

from sqlalchemy import event, or_, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = logging.getLogger(__name__)

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.schema import events_table, metadata

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

    def __init__(self, database_url: str | None = None) -> None:
        """Initialize EventStore with database URL.

        Args:
            database_url: SQLAlchemy database URL.
                         For async SQLite: "sqlite+aiosqlite:///path/to/db.sqlite"
                         If not provided, defaults to ~/.ouroboros/ouroboros.db
        """
        if database_url is None:
            db_path = Path.home() / ".ouroboros" / "ouroboros.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite+aiosqlite:///{db_path}"
        self._database_url = database_url
        self._engine: AsyncEngine | None = None

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

    async def initialize(self) -> None:
        """Initialize the database connection and create tables if needed.

        This method is idempotent - calling it multiple times is safe.

        For aiosqlite, uses StaticPool (default) which maintains a single
        connection. This avoids connection accumulation while supporting
        :memory: databases in tests.
        """
        if self._engine is None:
            self._engine = create_async_engine(
                self._database_url,
                echo=False,
                connect_args={"timeout": 30},
            )

            # Enable WAL mode and set busy timeout on every new connection
            @event.listens_for(self._engine.sync_engine, "connect")
            def _set_sqlite_pragmas(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.close()

        # Create all tables defined in metadata
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

        last_err: Exception | None = None
        for attempt in range(3):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        events_table.insert().values(**event.to_db_dict())
                    )
                return
            except Exception as e:
                last_err = e
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
        """Get all session start events.

        This method retrieves all events of type 'orchestrator.session.started'
        to identify every session recorded in the event store.

        Returns:
            List of session start events, ordered by timestamp descending.

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
                    .where(events_table.c.event_type == "orchestrator.session.started")
                    .order_by(events_table.c.timestamp.desc())
                )

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get all sessions: {e}",
                operation="select",
                table="events",
                details={"event_type": "orchestrator.session.started"},
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
        - ``execution/<execution_id>_*`` for AC/Sub-AC runtime scopes
        - ``execution/<execution_id>:*`` for coordinator level scopes

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

        conditions = [events_table.c.aggregate_id == session_id]
        if resolved_execution_id:
            escaped_execution_id = (
                resolved_execution_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            conditions.extend(
                [
                    events_table.c.aggregate_id == resolved_execution_id,
                    events_table.c.aggregate_id.like(
                        f"{escaped_execution_id}\\_%",
                        escape="\\",
                    ),
                    events_table.c.aggregate_id.like(f"{resolved_execution_id}:%"),
                ]
            )

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
