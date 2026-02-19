"""Secondary Loop Scheduler for batch TODO processing.

This module implements Story 7-2: Secondary Loop Batch Processing - automatic
TODO processing after primary goal achievement.

Design Principles:
- Activate only after primary goal completion
- Process TODOs in priority order
- Non-blocking failures: one failed TODO doesn't stop others
- Batch summary with success/failure counts
- Optional: user can skip via --skip-secondary flag

Usage:
    from ouroboros.secondary import SecondaryLoopScheduler, TodoRegistry
    from ouroboros.persistence import EventStore

    store = EventStore("sqlite+aiosqlite:///events.db")
    registry = TodoRegistry(store)
    scheduler = SecondaryLoopScheduler(registry)

    # Check if secondary loop should run
    if scheduler.should_activate(primary_completed=True, skip_flag=False):
        result = await scheduler.process_batch()
        if result.is_ok:
            summary = result.value
            print(f"Processed: {summary.total}, Success: {summary.success_count}")
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from ouroboros.core.errors import OuroborosError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.secondary.todo_registry import (
    Todo,
    TodoRegistry,
    TodoStatus,
)

log = get_logger(__name__)


class BatchStatus(StrEnum):
    """Status of the batch processing run.

    Attributes:
        COMPLETED: All TODOs processed (some may have failed)
        PARTIAL: Processing stopped early (e.g., timeout)
        SKIPPED: User chose to skip secondary loop
        NO_TODOS: No pending TODOs to process
    """

    COMPLETED = "completed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    NO_TODOS = "no_todos"


@dataclass(frozen=True, slots=True)
class TodoResult:
    """Result of processing a single TODO.

    Attributes:
        todo_id: ID of the processed TODO
        description: TODO description for reporting
        success: Whether processing succeeded
        error_message: Error details if failed
        duration_ms: Processing duration in milliseconds
    """

    todo_id: str
    description: str
    success: bool
    error_message: str | None = None
    duration_ms: int = 0


@dataclass(frozen=True, slots=True)
class BatchSummary:
    """Summary of a batch processing run.

    Attributes:
        status: Overall batch status
        total: Total TODOs processed
        success_count: Number of successful TODOs
        failure_count: Number of failed TODOs
        skipped_count: Number of skipped TODOs
        results: Individual TODO results
        started_at: When batch processing started
        completed_at: When batch processing completed
    """

    status: BatchStatus
    total: int
    success_count: int
    failure_count: int
    skipped_count: int
    results: tuple[TodoResult, ...]
    started_at: datetime
    completed_at: datetime

    @property
    def duration_ms(self) -> int:
        """Total batch duration in milliseconds."""
        delta = self.completed_at - self.started_at
        return int(delta.total_seconds() * 1000)

    @property
    def success_rate(self) -> float:
        """Ratio of successful TODOs (0.0-1.0)."""
        if self.total == 0:
            return 1.0
        return self.success_count / self.total

    @property
    def failed_todos(self) -> tuple[TodoResult, ...]:
        """Return only failed TODO results."""
        return tuple(r for r in self.results if not r.success)

    def to_dict(self) -> dict:
        """Convert to dictionary for logging/reporting."""
        return {
            "status": self.status.value,
            "total": self.total,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "skipped_count": self.skipped_count,
            "duration_ms": self.duration_ms,
            "success_rate": f"{self.success_rate:.1%}",
        }


# Type alias for TODO executor function
TodoExecutor = Callable[[Todo], Awaitable[Result[None, OuroborosError]]]


async def _default_executor(todo: Todo) -> Result[None, OuroborosError]:
    """Default no-op executor for testing.

    In production, this would route the TODO through the execution pipeline.

    Args:
        todo: The TODO to execute

    Returns:
        Result indicating success (always Ok for default)
    """
    log.info(
        "todo.executed.noop",
        todo_id=todo.id,
        description=todo.description,
    )
    return Result.ok(None)


@dataclass
class SecondaryLoopScheduler:
    """Scheduler for processing TODOs after primary goal completion.

    Orchestrates batch processing of TODOs with priority ordering,
    resilient error handling, and comprehensive reporting.

    Attributes:
        _registry: TodoRegistry for TODO access
        _executor: Function to execute individual TODOs
        _max_todos_per_batch: Maximum TODOs to process in one batch
    """

    _registry: TodoRegistry
    _executor: TodoExecutor = field(default=_default_executor)
    _max_todos_per_batch: int = 50

    def should_activate(
        self,
        primary_completed: bool,
        skip_flag: bool = False,
    ) -> bool:
        """Determine if secondary loop should activate.

        Args:
            primary_completed: Whether primary goal was achieved
            skip_flag: User's --skip-secondary flag

        Returns:
            True if secondary loop should run
        """
        if skip_flag:
            log.info("secondary_loop.skipped.user_flag")
            return False

        if not primary_completed:
            log.info("secondary_loop.skipped.primary_incomplete")
            return False

        return True

    async def process_batch(
        self,
        limit: int | None = None,
    ) -> Result[BatchSummary, OuroborosError]:
        """Process pending TODOs in a batch.

        TODOs are processed in priority order. Failed TODOs are marked
        as FAILED but don't block other TODOs from processing.

        Args:
            limit: Maximum TODOs to process (defaults to _max_todos_per_batch)

        Returns:
            Result containing BatchSummary or OuroborosError
        """
        started_at = datetime.now(UTC)
        batch_limit = limit or self._max_todos_per_batch

        log.info(
            "secondary_loop.batch.started",
            max_todos=batch_limit,
        )

        # Get pending TODOs
        pending_result = await self._registry.get_pending(limit=batch_limit)
        if pending_result.is_err:
            return Result.err(pending_result.error)

        todos = pending_result.value

        if not todos:
            log.info("secondary_loop.batch.no_todos")
            return Result.ok(
                BatchSummary(
                    status=BatchStatus.NO_TODOS,
                    total=0,
                    success_count=0,
                    failure_count=0,
                    skipped_count=0,
                    results=(),
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                )
            )

        # Process each TODO
        results: list[TodoResult] = []
        success_count = 0
        failure_count = 0

        for todo in todos:
            result = await self._process_single_todo(todo)
            results.append(result)

            if result.success:
                success_count += 1
            else:
                failure_count += 1

        completed_at = datetime.now(UTC)

        summary = BatchSummary(
            status=BatchStatus.COMPLETED,
            total=len(todos),
            success_count=success_count,
            failure_count=failure_count,
            skipped_count=0,
            results=tuple(results),
            started_at=started_at,
            completed_at=completed_at,
        )

        log.info(
            "secondary_loop.batch.completed",
            **summary.to_dict(),
        )

        return Result.ok(summary)

    async def _process_single_todo(self, todo: Todo) -> TodoResult:
        """Process a single TODO with error handling.

        Args:
            todo: The TODO to process

        Returns:
            TodoResult with success/failure status
        """
        start_time = datetime.now(UTC)

        # Mark as in progress
        await self._registry.update_status(todo.id, TodoStatus.IN_PROGRESS)

        log.info(
            "todo.processing.started",
            todo_id=todo.id,
            priority=todo.priority.value,
            description=todo.description[:50],
        )

        try:
            # Execute the TODO
            exec_result = await self._executor(todo)

            end_time = datetime.now(UTC)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)

            if exec_result.is_ok:
                # Mark as done
                await self._registry.update_status(todo.id, TodoStatus.DONE)

                log.info(
                    "todo.processing.completed",
                    todo_id=todo.id,
                    duration_ms=duration_ms,
                )

                return TodoResult(
                    todo_id=todo.id,
                    description=todo.description,
                    success=True,
                    duration_ms=duration_ms,
                )
            else:
                # Mark as failed with error
                error_msg = str(exec_result.error)
                await self._registry.update_status(
                    todo.id,
                    TodoStatus.FAILED,
                    error_msg,
                )

                log.warning(
                    "todo.processing.failed",
                    todo_id=todo.id,
                    error=error_msg,
                    duration_ms=duration_ms,
                )

                return TodoResult(
                    todo_id=todo.id,
                    description=todo.description,
                    success=False,
                    error_message=error_msg,
                    duration_ms=duration_ms,
                )

        except Exception as e:
            # Catch unexpected exceptions
            end_time = datetime.now(UTC)
            duration_ms = int((end_time - start_time).total_seconds() * 1000)
            error_msg = f"Unexpected error: {e}"

            await self._registry.update_status(
                todo.id,
                TodoStatus.FAILED,
                error_msg,
            )

            log.error(
                "todo.processing.exception",
                todo_id=todo.id,
                error=str(e),
                duration_ms=duration_ms,
            )

            return TodoResult(
                todo_id=todo.id,
                description=todo.description,
                success=False,
                error_message=error_msg,
                duration_ms=duration_ms,
            )

    async def skip_all_pending(
        self,
        reason: str = "User requested skip",
    ) -> Result[BatchSummary, OuroborosError]:
        """Skip all pending TODOs.

        Used when user explicitly chooses to skip secondary loop
        or when deferring to next session.

        Args:
            reason: Reason for skipping

        Returns:
            Result containing BatchSummary or OuroborosError
        """
        started_at = datetime.now(UTC)

        log.info(
            "secondary_loop.skip_all.started",
            reason=reason,
        )

        pending_result = await self._registry.get_pending()
        if pending_result.is_err:
            return Result.err(pending_result.error)

        todos = pending_result.value
        results: list[TodoResult] = []

        for todo in todos:
            await self._registry.update_status(
                todo.id,
                TodoStatus.SKIPPED,
                reason,
            )
            results.append(
                TodoResult(
                    todo_id=todo.id,
                    description=todo.description,
                    success=True,  # Skipping is not a failure
                )
            )

        completed_at = datetime.now(UTC)

        summary = BatchSummary(
            status=BatchStatus.SKIPPED,
            total=len(todos),
            success_count=0,
            failure_count=0,
            skipped_count=len(todos),
            results=tuple(results),
            started_at=started_at,
            completed_at=completed_at,
        )

        log.info(
            "secondary_loop.skip_all.completed",
            skipped_count=len(todos),
        )

        return Result.ok(summary)

    async def get_status_report(self) -> Result[dict, OuroborosError]:
        """Get a status report of TODO processing.

        Returns:
            Result containing status dict with counts and pending items
        """
        stats_result = await self._registry.get_stats()
        if stats_result.is_err:
            return Result.err(stats_result.error)

        stats = stats_result.value

        pending_result = await self._registry.get_pending(limit=10)
        if pending_result.is_err:
            return Result.err(pending_result.error)

        pending = pending_result.value

        return Result.ok(
            {
                "stats": stats,
                "pending_count": stats.get("pending", 0),
                "next_pending": [
                    {
                        "id": t.id,
                        "description": t.description[:50],
                        "priority": t.priority.value,
                    }
                    for t in pending[:5]
                ],
            }
        )
