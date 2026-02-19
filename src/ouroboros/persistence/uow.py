"""Unit of Work pattern for phase-based persistence.

This module provides:
- UnitOfWork: Accumulate events and persist at phase boundaries
- Transactional coordination between EventStore and CheckpointStore

The UnitOfWork pattern ensures that all related persistence operations
(events and checkpoints) are committed atomically at phase boundaries.
"""

from collections.abc import Sequence

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore
from ouroboros.persistence.event_store import EventStore


class UnitOfWork:
    """Unit of Work for coordinating event and checkpoint persistence.

    Accumulates events during a phase and persists both events and checkpoints
    atomically at phase boundaries. Provides transactional semantics for
    persistence operations.

    Usage:
        uow = UnitOfWork(event_store, checkpoint_store)

        # Accumulate events during phase
        uow.add_event(event1)
        uow.add_event(event2)

        # Commit at phase boundary
        checkpoint = CheckpointData.create("seed-123", "planning", state)
        result = await uow.commit(checkpoint)
        if result.is_ok:
            # All events and checkpoint persisted
            pass
    """

    def __init__(self, event_store: EventStore, checkpoint_store: CheckpointStore) -> None:
        """Initialize unit of work.

        Args:
            event_store: EventStore for persisting events.
            checkpoint_store: CheckpointStore for persisting checkpoints.
        """
        self._event_store = event_store
        self._checkpoint_store = checkpoint_store
        self._pending_events: list[BaseEvent] = []

    def add_event(self, event: BaseEvent) -> None:
        """Add event to pending events for later commit.

        Args:
            event: Event to add to the unit of work.
        """
        self._pending_events.append(event)

    def add_events(self, events: Sequence[BaseEvent]) -> None:
        """Add multiple events to pending events.

        Args:
            events: Sequence of events to add.
        """
        self._pending_events.extend(events)

    async def commit(
        self, checkpoint: CheckpointData | None = None
    ) -> Result[None, PersistenceError]:
        """Commit all pending events and optional checkpoint.

        Persists all accumulated events to EventStore and optionally saves
        a checkpoint. Operations are performed in order:
        1. Persist all events
        2. Save checkpoint (if provided)

        On failure, the operation stops and returns an error. Already-persisted
        events remain in the store (event sourcing is append-only).

        Args:
            checkpoint: Optional checkpoint to save after events.

        Returns:
            Result.ok(None) on success,
            Result.err(PersistenceError) on failure.
        """
        try:
            # Persist all pending events atomically in a single batch
            if self._pending_events:
                await self._event_store.append_batch(self._pending_events)

            # Save checkpoint if provided
            if checkpoint is not None:
                checkpoint_result = self._checkpoint_store.save(checkpoint)
                if checkpoint_result.is_err:
                    return checkpoint_result

            # Clear pending events after successful commit
            self._pending_events.clear()

            return Result.ok(None)

        except PersistenceError as e:
            # PersistenceError from event store, re-raise as Result
            return Result.err(e)
        except Exception as e:
            # Unexpected error
            return Result.err(
                PersistenceError(
                    f"Unit of work commit failed: {e}",
                    operation="commit",
                    details={"pending_events": len(self._pending_events)},
                )
            )

    def rollback(self) -> None:
        """Rollback by discarding all pending events.

        This is useful when an error occurs during phase execution
        and you want to discard uncommitted events.

        Note: This only affects pending events. Already-committed events
        cannot be rolled back (event sourcing is append-only).
        """
        self._pending_events.clear()

    @property
    def pending_event_count(self) -> int:
        """Get count of pending events awaiting commit.

        Returns:
            Number of events in the unit of work.
        """
        return len(self._pending_events)

    def has_pending_events(self) -> bool:
        """Check if there are pending events.

        Returns:
            True if there are uncommitted events.
        """
        return len(self._pending_events) > 0


class PhaseTransaction:
    """Context manager for phase-based transactions.

    Provides convenient context manager for phase execution with automatic
    commit or rollback based on success/failure.

    Usage:
        async with PhaseTransaction(uow, seed_id, "planning", state) as tx:
            # Execute phase logic
            tx.add_event(event1)
            tx.add_event(event2)
            # Auto-commits on success, rolls back on exception
    """

    def __init__(
        self,
        uow: UnitOfWork,
        seed_id: str,
        phase: str,
        state: dict,
    ) -> None:
        """Initialize phase transaction.

        Args:
            uow: UnitOfWork instance to use.
            seed_id: Seed identifier for checkpoint.
            phase: Current phase name.
            state: State data for checkpoint.
        """
        self._uow = uow
        self._seed_id = seed_id
        self._phase = phase
        self._state = state
        self._committed = False

    def add_event(self, event: BaseEvent) -> None:
        """Add event to the transaction.

        Args:
            event: Event to add.
        """
        self._uow.add_event(event)

    def add_events(self, events: Sequence[BaseEvent]) -> None:
        """Add multiple events to the transaction.

        Args:
            events: Sequence of events to add.
        """
        self._uow.add_events(events)

    async def __aenter__(self) -> PhaseTransaction:
        """Enter context manager."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Exit context manager with auto-commit or rollback.

        Args:
            exc_type: Exception type if an exception occurred.
            exc_val: Exception value if an exception occurred.
            exc_tb: Exception traceback if an exception occurred.

        Returns:
            False to propagate exceptions (we don't suppress them).
        """
        if exc_type is None and not self._committed:
            # Success path: commit events and checkpoint
            checkpoint = CheckpointData.create(self._seed_id, self._phase, self._state)
            result = await self._uow.commit(checkpoint)
            if result.is_err:
                # Commit failed, raise the error
                raise result.error
            self._committed = True
        elif exc_type is not None:
            # Error path: rollback pending events
            self._uow.rollback()

        # Don't suppress exceptions
        return False
