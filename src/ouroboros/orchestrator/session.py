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
from datetime import UTC, datetime, timedelta
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
    CANCELLED = "cancelled"


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

        The ``messages_processed`` counter is set from the update dict when
        present, otherwise it is incremented by one.  This avoids the double-
        increment that would occur when the caller also tracks a separate
        counter and stores it in the update.

        Args:
            update: Progress data to merge.

        Returns:
            New SessionTracker with merged progress.
        """
        merged_progress = {**self.progress, **update}
        new_count = update.get("messages_processed")
        if isinstance(new_count, int):
            messages_processed = new_count
        else:
            messages_processed = self.messages_processed + 1
        return replace(
            self,
            progress=merged_progress,
            messages_processed=messages_processed,
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
        - orchestrator.session.cancelled: Session cancelled
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

    async def mark_paused(
        self,
        session_id: str,
        reason: str,
        messages_processed: int = 0,
    ) -> Result[None, PersistenceError]:
        """Mark session as paused (e.g. after Ctrl+C interruption).

        Args:
            session_id: Session to pause.
            reason: Why the session was paused.
            messages_processed: Number of messages processed before pause.

        Returns:
            Result indicating success or failure.
        """
        event = BaseEvent(
            type="orchestrator.session.paused",
            aggregate_type="session",
            aggregate_id=session_id,
            data={
                "reason": reason,
                "paused_at": datetime.now(UTC).isoformat(),
                "messages_processed": messages_processed,
            },
        )

        try:
            await self._event_store.append(event)
            log.info(
                "orchestrator.session.paused",
                session_id=session_id,
                reason=reason,
            )
            return Result.ok(None)
        except Exception as e:
            log.exception(
                "orchestrator.session.pause_failed",
                session_id=session_id,
                error=str(e),
            )
            return Result.err(
                PersistenceError(
                    message=f"Failed to mark session paused: {e}",
                    details={"session_id": session_id},
                )
            )

    async def mark_cancelled(
        self,
        session_id: str,
        reason: str,
        cancelled_by: str = "user",
    ) -> Result[None, PersistenceError]:
        """Mark session as cancelled.

        Args:
            session_id: Session to cancel.
            reason: Why the session was cancelled.
            cancelled_by: Who/what initiated cancellation ("user", "auto_cleanup").

        Returns:
            Result indicating success or failure.
        """
        event = BaseEvent(
            type="orchestrator.session.cancelled",
            aggregate_type="session",
            aggregate_id=session_id,
            data={
                "reason": reason,
                "cancelled_by": cancelled_by,
                "cancelled_at": datetime.now(UTC).isoformat(),
            },
        )

        try:
            await self._event_store.append(event)
            log.info(
                "orchestrator.session.cancelled",
                session_id=session_id,
                reason=reason,
                cancelled_by=cancelled_by,
            )
            return Result.ok(None)
        except Exception as e:
            log.exception(
                "orchestrator.session.cancel_failed",
                session_id=session_id,
                error=str(e),
            )
            return Result.err(
                PersistenceError(
                    message=f"Failed to mark session cancelled: {e}",
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
                    progress_update = event.data.get("progress", {})
                    if not isinstance(progress_update, dict):
                        continue
                    last_progress = {**last_progress, **progress_update}
                    persisted_messages = progress_update.get("messages_processed")
                    if isinstance(persisted_messages, int):
                        messages_processed = persisted_messages
                    else:
                        messages_processed += 1
                elif event.type == "orchestrator.session.completed":
                    tracker = tracker.with_status(SessionStatus.COMPLETED)
                elif event.type == "orchestrator.session.failed":
                    tracker = tracker.with_status(SessionStatus.FAILED)
                elif event.type == "orchestrator.session.paused":
                    tracker = tracker.with_status(SessionStatus.PAUSED)
                elif event.type == "orchestrator.session.cancelled":
                    tracker = tracker.with_status(SessionStatus.CANCELLED)

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

    async def find_orphaned_sessions(
        self,
        staleness_threshold: timedelta = timedelta(hours=1),
    ) -> list[SessionTracker]:
        """Find orphaned sessions that are still running but have gone stale.

        A session is considered orphaned if:
        1. Its current status is RUNNING (or PAUSED)
        2. Its last activity timestamp (last event) is older than the staleness threshold

        This is used by auto-cleanup on MCP server startup to detect and cancel
        executions that were left in a running state (e.g., due to a crash).

        Args:
            staleness_threshold: How long since last activity before a session
                is considered orphaned. Defaults to 1 hour.

        Returns:
            List of SessionTracker instances for orphaned sessions.
        """
        now = datetime.now(UTC)
        orphaned: list[SessionTracker] = []

        try:
            # Get all session start events to enumerate sessions
            start_events = await self._event_store.get_all_sessions()

            for start_event in start_events:
                session_id = start_event.aggregate_id

                # Replay all events for this session
                try:
                    events = await self._event_store.replay("session", session_id)
                except Exception:
                    log.warning(
                        "orchestrator.orphan_detection.replay_failed",
                        session_id=session_id,
                    )
                    continue

                if not events:
                    continue

                # Determine current status by replaying events
                status = SessionStatus.RUNNING
                for event in events:
                    if event.type == "orchestrator.session.completed":
                        status = SessionStatus.COMPLETED
                    elif event.type == "orchestrator.session.failed":
                        status = SessionStatus.FAILED
                    elif event.type == "orchestrator.session.paused":
                        status = SessionStatus.PAUSED
                    elif event.type == "orchestrator.session.cancelled":
                        status = SessionStatus.CANCELLED

                # Only consider active sessions (RUNNING or PAUSED)
                if status not in (SessionStatus.RUNNING, SessionStatus.PAUSED):
                    continue

                # Check the last event's timestamp for staleness
                last_event = events[-1]
                last_activity = last_event.timestamp
                if last_activity is None:
                    # If no timestamp, use start_time from event data as fallback
                    start_time_str = start_event.data.get("start_time")
                    if start_time_str:
                        last_activity = datetime.fromisoformat(start_time_str)
                    else:
                        continue

                # Ensure timezone-aware comparison
                if last_activity.tzinfo is None:
                    last_activity = last_activity.replace(tzinfo=UTC)

                if (now - last_activity) > staleness_threshold:
                    # Reconstruct full tracker for the orphaned session
                    result = await self.reconstruct_session(session_id)
                    if result.is_ok:
                        orphaned.append(result.value)

            log.info(
                "orchestrator.orphan_detection.complete",
                total_sessions=len(start_events),
                orphaned_count=len(orphaned),
            )

        except Exception as e:
            log.exception(
                "orchestrator.orphan_detection.failed",
                error=str(e),
            )

        return orphaned

    async def cancel_orphaned_sessions(
        self,
        staleness_threshold: timedelta = timedelta(hours=1),
    ) -> list[SessionTracker]:
        """Detect and cancel orphaned sessions.

        This is the auto-cleanup routine intended to run during MCP server
        startup. It finds all sessions that are still active (RUNNING/PAUSED)
        but have had no activity for longer than the staleness threshold,
        cancels each one, and logs the cancellations to stderr.

        Args:
            staleness_threshold: How long since last activity before a session
                is considered orphaned. Defaults to 1 hour.

        Returns:
            List of SessionTracker instances that were cancelled.
        """
        import sys

        orphaned = await self.find_orphaned_sessions(staleness_threshold)

        if not orphaned:
            log.info("orchestrator.auto_cleanup.no_orphans")
            return []

        cancelled: list[SessionTracker] = []

        for tracker in orphaned:
            result = await self.mark_cancelled(
                session_id=tracker.session_id,
                reason=(
                    f"Auto-cancelled on startup: session was {tracker.status.value} "
                    f"with no activity for over {staleness_threshold}"
                ),
                cancelled_by="auto_cleanup",
            )

            if result.is_ok:
                cancelled.append(tracker)
                # Log to stderr so it's visible in MCP stdio mode
                print(
                    f"[ouroboros] Auto-cancelled orphaned session "
                    f"{tracker.session_id} (execution={tracker.execution_id}, "
                    f"previous_status={tracker.status.value})",
                    file=sys.stderr,
                )
                log.info(
                    "orchestrator.auto_cleanup.cancelled",
                    session_id=tracker.session_id,
                    execution_id=tracker.execution_id,
                    previous_status=tracker.status.value,
                )
            else:
                log.warning(
                    "orchestrator.auto_cleanup.cancel_failed",
                    session_id=tracker.session_id,
                    error=str(result.error),
                )

        log.info(
            "orchestrator.auto_cleanup.complete",
            orphaned_count=len(orphaned),
            cancelled_count=len(cancelled),
        )

        return cancelled


__all__ = [
    "SessionRepository",
    "SessionStatus",
    "SessionTracker",
]
