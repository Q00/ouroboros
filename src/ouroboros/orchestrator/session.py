"""Session tracking for orchestrator execution.

This module provides session management through event sourcing:
- SessionTracker: Immutable session state (frozen dataclass)
- SessionRepository: Event-based persistence and reconstruction

Sessions are tracked entirely through events in the EventStore,
following the principle that events are the single source of truth.

Usage:
    repo = SessionRepository(event_store)

    # Create and track session
    tracker = SessionTracker.create(execution_id, seed_id)
    await repo.track_progress(tracker.session_id, {"step": 1})

    # Reconstruct session from events
    result = await repo.reconstruct_session(session_id)
    if result.is_ok:
        tracker = result.value
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)


# =============================================================================
# Session Status
# =============================================================================


class SessionStatus(StrEnum):
    """Status of an orchestrator session."""

    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


# =============================================================================
# Session Tracker (Immutable)
# =============================================================================


@dataclass(frozen=True, slots=True)
class SessionTracker:
    """Immutable session state for orchestrator execution.

    This dataclass tracks the current state of an orchestrator session.
    Updates create new instances via with_* methods (immutable pattern).

    Attributes:
        session_id: Unique identifier for this session.
        execution_id: Associated workflow execution ID.
        seed_id: ID of the seed being executed.
        status: Current session status.
        start_time: When the session started.
        progress: Progress data (message count, current step, etc.).
        messages_processed: Number of messages processed so far.
        last_message_time: Timestamp of last processed message.
    """

    session_id: str
    execution_id: str
    seed_id: str
    status: SessionStatus
    start_time: datetime
    progress: dict[str, Any] = field(default_factory=dict)
    messages_processed: int = 0
    last_message_time: datetime | None = None

    @classmethod
    def create(
        cls,
        execution_id: str,
        seed_id: str,
        session_id: str | None = None,
    ) -> SessionTracker:
        """Create a new session tracker.

        Args:
            execution_id: Workflow execution ID.
            seed_id: Seed ID being executed.
            session_id: Optional custom session ID.

        Returns:
            New SessionTracker instance.
        """
        return cls(
            session_id=session_id or f"orch_{uuid4().hex[:12]}",
            execution_id=execution_id,
            seed_id=seed_id,
            status=SessionStatus.RUNNING,
            start_time=datetime.now(UTC),
        )

    def with_progress(self, update: dict[str, Any]) -> SessionTracker:
        """Return new tracker with updated progress.

        Args:
            update: Progress data to merge.

        Returns:
            New SessionTracker with merged progress.
        """
        merged_progress = {**self.progress, **update}
        return replace(
            self,
            progress=merged_progress,
            messages_processed=self.messages_processed + 1,
            last_message_time=datetime.now(UTC),
        )

    def with_status(self, status: SessionStatus) -> SessionTracker:
        """Return new tracker with updated status.

        Args:
            status: New session status.

        Returns:
            New SessionTracker with updated status.
        """
        return replace(self, status=status)

    @property
    def is_active(self) -> bool:
        """Return True if session is still active (running or paused)."""
        return self.status in (SessionStatus.RUNNING, SessionStatus.PAUSED)

    @property
    def is_completed(self) -> bool:
        """Return True if session completed successfully."""
        return self.status == SessionStatus.COMPLETED

    @property
    def is_failed(self) -> bool:
        """Return True if session failed."""
        return self.status == SessionStatus.FAILED

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization.

        Returns:
            Dictionary representation.
        """
        return {
            "session_id": self.session_id,
            "execution_id": self.execution_id,
            "seed_id": self.seed_id,
            "status": self.status.value,
            "start_time": self.start_time.isoformat(),
            "progress": self.progress,
            "messages_processed": self.messages_processed,
            "last_message_time": self.last_message_time.isoformat()
            if self.last_message_time
            else None,
        }


# =============================================================================
# Session Repository (Event-based)
# =============================================================================


class SessionRepository:
    """Manages sessions via event store.

    Sessions are persisted entirely through events, following the
    event sourcing pattern. This avoids dual-write problems and
    keeps events as the single source of truth.

    Event Types:
        - orchestrator.session.started: Session created
        - orchestrator.progress.updated: Progress update
        - orchestrator.session.completed: Session finished successfully
        - orchestrator.session.failed: Session failed
        - orchestrator.session.paused: Session paused for resumption
    """

    def __init__(self, event_store: EventStore) -> None:
        """Initialize repository with event store.

        Args:
            event_store: Event store for persistence.
        """
        self._event_store = event_store

    async def create_session(
        self,
        execution_id: str,
        seed_id: str,
        session_id: str | None = None,
    ) -> Result[SessionTracker, PersistenceError]:
        """Create a new session and persist start event.

        Args:
            execution_id: Workflow execution ID.
            seed_id: Seed ID being executed.
            session_id: Optional custom session ID.

        Returns:
            Result containing new SessionTracker.
        """
        tracker = SessionTracker.create(execution_id, seed_id, session_id)

        event = BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id=tracker.session_id,
            data={
                "execution_id": execution_id,
                "seed_id": seed_id,
                "start_time": tracker.start_time.isoformat(),
            },
        )

        try:
            await self._event_store.append(event)
            log.info(
                "orchestrator.session.created",
                session_id=tracker.session_id,
                execution_id=execution_id,
            )
            return Result.ok(tracker)
        except Exception as e:
            log.exception(
                "orchestrator.session.create_failed",
                session_id=tracker.session_id,
                error=str(e),
            )
            return Result.err(
                PersistenceError(
                    message=f"Failed to create session: {e}",
                    details={"session_id": tracker.session_id},
                )
            )

    async def track_progress(
        self,
        session_id: str,
        progress: dict[str, Any],
    ) -> Result[None, PersistenceError]:
        """Emit progress event for session.

        Args:
            session_id: Session to update.
            progress: Progress data to record.

        Returns:
            Result indicating success or failure.
        """
        event = BaseEvent(
            type="orchestrator.progress.updated",
            aggregate_type="session",
            aggregate_id=session_id,
            data={
                "progress": progress,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )

        try:
            await self._event_store.append(event)
            return Result.ok(None)
        except Exception as e:
            log.warning(
                "orchestrator.progress.track_failed",
                session_id=session_id,
                error=str(e),
            )
            return Result.err(
                PersistenceError(
                    message=f"Failed to track progress: {e}",
                    details={"session_id": session_id},
                )
            )

    async def mark_completed(
        self,
        session_id: str,
        summary: dict[str, Any] | None = None,
    ) -> Result[None, PersistenceError]:
        """Mark session as completed.

        Args:
            session_id: Session to complete.
            summary: Optional completion summary.

        Returns:
            Result indicating success or failure.
        """
        event = BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id=session_id,
            data={
                "summary": summary or {},
                "completed_at": datetime.now(UTC).isoformat(),
            },
        )

        try:
            await self._event_store.append(event)
            log.info(
                "orchestrator.session.completed",
                session_id=session_id,
            )
            return Result.ok(None)
        except Exception as e:
            log.exception(
                "orchestrator.session.complete_failed",
                session_id=session_id,
                error=str(e),
            )
            return Result.err(
                PersistenceError(
                    message=f"Failed to mark session completed: {e}",
                    details={"session_id": session_id},
                )
            )

    async def mark_failed(
        self,
        session_id: str,
        error_message: str,
        error_details: dict[str, Any] | None = None,
    ) -> Result[None, PersistenceError]:
        """Mark session as failed.

        Args:
            session_id: Session that failed.
            error_message: Error description.
            error_details: Optional error details.

        Returns:
            Result indicating success or failure.
        """
        event = BaseEvent(
            type="orchestrator.session.failed",
            aggregate_type="session",
            aggregate_id=session_id,
            data={
                "error": error_message,
                "error_details": error_details or {},
                "failed_at": datetime.now(UTC).isoformat(),
            },
        )

        try:
            await self._event_store.append(event)
            log.error(
                "orchestrator.session.failed",
                session_id=session_id,
                error=error_message,
            )
            return Result.ok(None)
        except Exception as e:
            log.exception(
                "orchestrator.session.fail_failed",
                session_id=session_id,
                error=str(e),
            )
            return Result.err(
                PersistenceError(
                    message=f"Failed to mark session failed: {e}",
                    details={"session_id": session_id},
                )
            )

    async def reconstruct_session(
        self,
        session_id: str,
    ) -> Result[SessionTracker, PersistenceError]:
        """Reconstruct session state from events.

        Replays all events for the session to rebuild the current state.
        This is used for session resumption.

        Args:
            session_id: Session to reconstruct.

        Returns:
            Result containing reconstructed SessionTracker.
        """
        try:
            events = await self._event_store.replay("session", session_id)

            if not events:
                return Result.err(
                    PersistenceError(
                        message=f"No events found for session: {session_id}",
                        details={"session_id": session_id},
                    )
                )

            # Find the start event to get initial state
            start_event = next(
                (e for e in events if e.type == "orchestrator.session.started"),
                None,
            )

            if not start_event:
                return Result.err(
                    PersistenceError(
                        message=f"No start event found for session: {session_id}",
                        details={"session_id": session_id},
                    )
                )

            # Create initial tracker from start event
            tracker = SessionTracker(
                session_id=session_id,
                execution_id=start_event.data.get("execution_id", ""),
                seed_id=start_event.data.get("seed_id", ""),
                status=SessionStatus.RUNNING,
                start_time=datetime.fromisoformat(
                    start_event.data.get("start_time", datetime.now(UTC).isoformat())
                ),
            )

            # Replay subsequent events
            messages_processed = 0
            last_progress: dict[str, Any] = {}

            for event in events:
                if event.type == "orchestrator.progress.updated":
                    messages_processed += 1
                    last_progress = event.data.get("progress", {})
                elif event.type == "orchestrator.session.completed":
                    tracker = tracker.with_status(SessionStatus.COMPLETED)
                elif event.type == "orchestrator.session.failed":
                    tracker = tracker.with_status(SessionStatus.FAILED)
                elif event.type == "orchestrator.session.paused":
                    tracker = tracker.with_status(SessionStatus.PAUSED)

            # Apply accumulated progress
            tracker = replace(
                tracker,
                progress=last_progress,
                messages_processed=messages_processed,
            )

            log.info(
                "orchestrator.session.reconstructed",
                session_id=session_id,
                status=tracker.status.value,
                messages_processed=messages_processed,
            )

            return Result.ok(tracker)

        except Exception as e:
            log.exception(
                "orchestrator.session.reconstruct_failed",
                session_id=session_id,
                error=str(e),
            )
            return Result.err(
                PersistenceError(
                    message=f"Failed to reconstruct session: {e}",
                    details={"session_id": session_id},
                )
            )


__all__ = [
    "SessionRepository",
    "SessionStatus",
    "SessionTracker",
]
