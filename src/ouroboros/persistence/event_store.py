"""EventStore implementation for event sourcing.

Provides async methods for appending and replaying events using SQLAlchemy Core
with aiosqlite backend.
"""

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.schema import events_table, metadata


class EventStore:
    """Event store for persisting and replaying events.

    Uses SQLAlchemy Core with aiosqlite for async database operations.
    All operations are transactional for atomicity.

    Usage:
        store = EventStore("sqlite+aiosqlite:///events.db")
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
                         If not provided, defaults to ~/.ouroboros/events.db
                         For async SQLite: "sqlite+aiosqlite:///path/to/db.sqlite"
                         If not provided, defaults to ~/.ouroboros/events.db
        """
        if database_url is None:
            db_path = Path.home() / ".ouroboros" / "events.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite+aiosqlite:///{db_path}"
        self._database_url = database_url
        self._engine: AsyncEngine | None = None

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
            )

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

        try:
            async with self._engine.begin() as conn:
                await conn.execute(events_table.insert().values(**event.to_db_dict()))
        except Exception as e:
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

        try:
            async with self._engine.begin() as conn:
                # Insert all events in a single statement within one transaction
                await conn.execute(
                    events_table.insert(),
                    [event.to_db_dict() for event in events],
                )
        except Exception as e:
            raise PersistenceError(
                f"Failed to append event batch: {e}",
                operation="insert_batch",
                table="events",
                details={
                    "batch_size": len(events),
                    "event_ids": [e.id for e in events[:5]],  # First 5 for debugging
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
