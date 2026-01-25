"""TODO Registry for capturing improvements during execution.

This module implements Story 7-1: TODO Registry - capturing discovered
improvements without disrupting the primary execution flow.

Design Principles:
- Non-blocking registration: TODOs are registered asynchronously
- Event sourcing: All state changes are persisted as events
- Immutable data: TODO items are frozen dataclasses
- Result type: Expected failures use Result, not exceptions

Usage:
    from ouroboros.secondary import TodoRegistry, Todo, Priority, TodoStatus
    from ouroboros.persistence import EventStore

    store = EventStore("sqlite+aiosqlite:///events.db")
    await store.initialize()

    registry = TodoRegistry(store)

    # Register a TODO (non-blocking)
    await registry.register(
        description="Refactor authentication module",
        context="execution-123",
        priority=Priority.MEDIUM,
    )

    # Get pending TODOs
    result = await registry.get_pending()
    if result.is_ok:
        for todo in result.value:
            print(f"{todo.priority}: {todo.description}")
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.observability.logging import get_logger
from ouroboros.persistence.event_store import EventStore

log = get_logger(__name__)


class Priority(StrEnum):
    """Priority levels for TODO items.

    Attributes:
        HIGH: Critical improvements that should be addressed first
        MEDIUM: Standard improvements with moderate impact
        LOW: Nice-to-have improvements with minimal urgency
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def sort_order(self) -> int:
        """Return numeric sort order (lower = higher priority)."""
        orders = {Priority.HIGH: 0, Priority.MEDIUM: 1, Priority.LOW: 2}
        return orders[self]


class TodoStatus(StrEnum):
    """Lifecycle status of a TODO item.

    Attributes:
        PENDING: Awaiting processing in secondary loop
        IN_PROGRESS: Currently being processed
        DONE: Successfully completed
        FAILED: Processing attempted but failed
        SKIPPED: Intentionally skipped (user decision or timeout)
    """

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"

    @property
    def is_terminal(self) -> bool:
        """Return True if this is a terminal state."""
        return self in (TodoStatus.DONE, TodoStatus.FAILED, TodoStatus.SKIPPED)


@dataclass(frozen=True, slots=True)
class Todo:
    """Immutable TODO item representing a discovered improvement.

    Attributes:
        id: Unique identifier (UUID)
        description: Human-readable description of the improvement
        context: Where the TODO was discovered (e.g., execution ID, file path)
        priority: Importance level for processing order
        created_at: When the TODO was registered (UTC)
        status: Current lifecycle status
        error_message: Error details if status is FAILED
    """

    id: str
    description: str
    context: str
    priority: Priority
    created_at: datetime
    status: TodoStatus = TodoStatus.PENDING
    error_message: str | None = None

    @classmethod
    def create(
        cls,
        description: str,
        context: str,
        priority: Priority = Priority.MEDIUM,
    ) -> "Todo":
        """Factory method to create a new TODO.

        Args:
            description: What improvement is needed
            context: Where it was discovered
            priority: How urgently it should be addressed

        Returns:
            New Todo instance with generated ID and timestamp
        """
        return cls(
            id=str(uuid4()),
            description=description,
            context=context,
            priority=priority,
            created_at=datetime.now(UTC),
            status=TodoStatus.PENDING,
        )

    def with_status(
        self,
        status: TodoStatus,
        error_message: str | None = None,
    ) -> "Todo":
        """Return a new Todo with updated status.

        Args:
            status: New status
            error_message: Error details if status is FAILED

        Returns:
            New Todo instance with updated status
        """
        return Todo(
            id=self.id,
            description=self.description,
            context=self.context,
            priority=self.priority,
            created_at=self.created_at,
            status=status,
            error_message=error_message,
        )


# Event type constants
EVENT_TODO_CREATED = "todo.created"
EVENT_TODO_STATUS_CHANGED = "todo.status.changed"


def _create_todo_event(todo: Todo) -> BaseEvent:
    """Create a TODO creation event.

    Args:
        todo: The TODO that was created

    Returns:
        BaseEvent for persistence
    """
    return BaseEvent(
        type=EVENT_TODO_CREATED,
        aggregate_type="todo",
        aggregate_id=todo.id,
        data={
            "description": todo.description,
            "context": todo.context,
            "priority": todo.priority.value,
            "created_at": todo.created_at.isoformat(),
            "status": todo.status.value,
        },
    )


def _create_status_change_event(
    todo: Todo,
    old_status: TodoStatus,
    new_status: TodoStatus,
    error_message: str | None = None,
) -> BaseEvent:
    """Create a TODO status change event.

    Args:
        todo: The TODO being updated
        old_status: Previous status
        new_status: New status
        error_message: Error details if transitioning to FAILED

    Returns:
        BaseEvent for persistence
    """
    data = {
        "old_status": old_status.value,
        "new_status": new_status.value,
    }
    if error_message:
        data["error_message"] = error_message

    return BaseEvent(
        type=EVENT_TODO_STATUS_CHANGED,
        aggregate_type="todo",
        aggregate_id=todo.id,
        data=data,
    )


def _reconstruct_todo_from_events(events: list[BaseEvent]) -> Todo | None:
    """Reconstruct a Todo from its event history.

    Args:
        events: List of events for a single TODO aggregate

    Returns:
        Reconstructed Todo or None if no events
    """
    if not events:
        return None

    # Find the creation event
    creation_event = None
    for event in events:
        if event.type == EVENT_TODO_CREATED:
            creation_event = event
            break

    if creation_event is None:
        return None

    data = creation_event.data
    todo = Todo(
        id=creation_event.aggregate_id,
        description=data["description"],
        context=data["context"],
        priority=Priority(data["priority"]),
        created_at=datetime.fromisoformat(data["created_at"]),
        status=TodoStatus(data["status"]),
    )

    # Apply status change events
    for event in events:
        if event.type == EVENT_TODO_STATUS_CHANGED:
            todo = todo.with_status(
                TodoStatus(event.data["new_status"]),
                event.data.get("error_message"),
            )

    return todo


@dataclass
class TodoRegistry:
    """Registry for TODO items with event-sourced persistence.

    Provides non-blocking registration of TODOs during execution
    and retrieval for secondary loop processing.

    Attributes:
        _event_store: EventStore for persistence
        _todo_ids: In-memory index of registered TODO IDs
    """

    _event_store: EventStore
    _todo_ids: set[str] = field(default_factory=set)

    async def register(
        self,
        description: str,
        context: str,
        priority: Priority = Priority.MEDIUM,
    ) -> Result[Todo, PersistenceError]:
        """Register a new TODO item.

        This is a non-blocking operation that persists the TODO
        and returns immediately without waiting for processing.

        Args:
            description: What improvement is needed
            context: Where it was discovered (e.g., execution ID)
            priority: How urgently it should be addressed

        Returns:
            Result containing the created Todo or PersistenceError
        """
        todo = Todo.create(description, context, priority)
        event = _create_todo_event(todo)

        try:
            await self._event_store.append(event)
            self._todo_ids.add(todo.id)

            log.info(
                "todo.registered",
                todo_id=todo.id,
                priority=priority.value,
                context=context,
            )

            return Result.ok(todo)

        except PersistenceError as e:
            log.error(
                "todo.registration.failed",
                todo_id=todo.id,
                error=str(e),
            )
            return Result.err(e)

    async def update_status(
        self,
        todo_id: str,
        new_status: TodoStatus,
        error_message: str | None = None,
    ) -> Result[Todo, PersistenceError]:
        """Update the status of a TODO item.

        Args:
            todo_id: ID of the TODO to update
            new_status: New status to set
            error_message: Error details if transitioning to FAILED

        Returns:
            Result containing the updated Todo or PersistenceError
        """
        # Get current state
        result = await self.get_by_id(todo_id)
        if result.is_err:
            return Result.err(result.error)

        todo = result.value
        if todo is None:
            return Result.err(
                PersistenceError(
                    f"TODO not found: {todo_id}",
                    operation="update_status",
                    details={"todo_id": todo_id},
                )
            )

        old_status = todo.status
        event = _create_status_change_event(todo, old_status, new_status, error_message)

        try:
            await self._event_store.append(event)
            updated_todo = todo.with_status(new_status, error_message)

            log.info(
                "todo.status.updated",
                todo_id=todo_id,
                old_status=old_status.value,
                new_status=new_status.value,
            )

            return Result.ok(updated_todo)

        except PersistenceError as e:
            log.error(
                "todo.status.update.failed",
                todo_id=todo_id,
                error=str(e),
            )
            return Result.err(e)

    async def get_by_id(self, todo_id: str) -> Result[Todo | None, PersistenceError]:
        """Retrieve a TODO by its ID.

        Args:
            todo_id: The TODO's unique identifier

        Returns:
            Result containing the Todo (or None if not found) or PersistenceError
        """
        try:
            events = await self._event_store.replay("todo", todo_id)
            todo = _reconstruct_todo_from_events(events)
            return Result.ok(todo)

        except PersistenceError as e:
            log.error(
                "todo.retrieval.failed",
                todo_id=todo_id,
                error=str(e),
            )
            return Result.err(e)

    async def get_pending(
        self,
        limit: int | None = None,
    ) -> Result[list[Todo], PersistenceError]:
        """Retrieve all pending TODOs sorted by priority.

        Args:
            limit: Maximum number of TODOs to return (None = all)

        Returns:
            Result containing list of pending Todos sorted by priority
        """
        todos: list[Todo] = []

        for todo_id in self._todo_ids:
            result = await self.get_by_id(todo_id)
            if result.is_err:
                return Result.err(result.error)

            todo = result.value
            if todo is not None and todo.status == TodoStatus.PENDING:
                todos.append(todo)

        # Sort by priority (HIGH first) then by creation time (oldest first)
        todos.sort(key=lambda t: (t.priority.sort_order, t.created_at))

        if limit is not None:
            todos = todos[:limit]

        return Result.ok(todos)

    async def get_all(self) -> Result[list[Todo], PersistenceError]:
        """Retrieve all TODOs regardless of status.

        Returns:
            Result containing list of all Todos
        """
        todos: list[Todo] = []

        for todo_id in self._todo_ids:
            result = await self.get_by_id(todo_id)
            if result.is_err:
                return Result.err(result.error)

            todo = result.value
            if todo is not None:
                todos.append(todo)

        # Sort by creation time (newest first)
        todos.sort(key=lambda t: t.created_at, reverse=True)
        return Result.ok(todos)

    def count_pending(self) -> int:
        """Return count of tracked TODO IDs.

        Note: This is the count of IDs in memory, which may include
        non-pending items. For accurate pending count, use get_pending().

        Returns:
            Number of TODO IDs tracked in memory
        """
        return len(self._todo_ids)

    async def get_stats(self) -> Result[dict[str, int], PersistenceError]:
        """Get statistics about TODOs by status.

        Returns:
            Result containing dict mapping status to count
        """
        stats: dict[str, int] = {status.value: 0 for status in TodoStatus}

        for todo_id in self._todo_ids:
            result = await self.get_by_id(todo_id)
            if result.is_err:
                return Result.err(result.error)

            todo = result.value
            if todo is not None:
                stats[todo.status.value] += 1

        return Result.ok(stats)
