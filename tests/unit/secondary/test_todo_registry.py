"""Tests for TODO Registry (Story 7-1)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.secondary.todo_registry import (
    EVENT_TODO_CREATED,
    EVENT_TODO_STATUS_CHANGED,
    Priority,
    Todo,
    TodoRegistry,
    TodoStatus,
    _create_status_change_event,
    _create_todo_event,
    _reconstruct_todo_from_events,
)


class TestPriority:
    """Tests for Priority enum."""

    def test_priority_values(self) -> None:
        """Verify all priority values."""
        assert Priority.HIGH == "high"
        assert Priority.MEDIUM == "medium"
        assert Priority.LOW == "low"

    def test_priority_is_str_enum(self) -> None:
        """Priority should be usable as string."""
        assert f"Priority: {Priority.HIGH}" == "Priority: high"

    def test_sort_order(self) -> None:
        """HIGH should sort before MEDIUM before LOW."""
        assert Priority.HIGH.sort_order < Priority.MEDIUM.sort_order
        assert Priority.MEDIUM.sort_order < Priority.LOW.sort_order


class TestTodoStatus:
    """Tests for TodoStatus enum."""

    def test_status_values(self) -> None:
        """Verify all status values."""
        assert TodoStatus.PENDING == "pending"
        assert TodoStatus.IN_PROGRESS == "in_progress"
        assert TodoStatus.DONE == "done"
        assert TodoStatus.FAILED == "failed"
        assert TodoStatus.SKIPPED == "skipped"

    def test_terminal_states(self) -> None:
        """DONE, FAILED, SKIPPED are terminal states."""
        assert not TodoStatus.PENDING.is_terminal
        assert not TodoStatus.IN_PROGRESS.is_terminal
        assert TodoStatus.DONE.is_terminal
        assert TodoStatus.FAILED.is_terminal
        assert TodoStatus.SKIPPED.is_terminal


class TestTodo:
    """Tests for Todo dataclass."""

    def test_create_with_defaults(self) -> None:
        """Create TODO with default priority."""
        todo = Todo.create(
            description="Refactor module",
            context="exec-123",
        )
        assert todo.description == "Refactor module"
        assert todo.context == "exec-123"
        assert todo.priority == Priority.MEDIUM
        assert todo.status == TodoStatus.PENDING
        assert todo.error_message is None
        assert todo.id is not None
        assert todo.created_at is not None

    def test_create_with_priority(self) -> None:
        """Create TODO with explicit priority."""
        todo = Todo.create(
            description="Critical fix",
            context="exec-456",
            priority=Priority.HIGH,
        )
        assert todo.priority == Priority.HIGH

    def test_immutability(self) -> None:
        """Todo should be immutable."""
        todo = Todo.create(description="Test", context="ctx")
        with pytest.raises(AttributeError):
            todo.description = "Modified"  # type: ignore[misc]

    def test_with_status(self) -> None:
        """with_status creates new Todo with updated status."""
        original = Todo.create(description="Test", context="ctx")
        updated = original.with_status(TodoStatus.IN_PROGRESS)

        assert original.status == TodoStatus.PENDING
        assert updated.status == TodoStatus.IN_PROGRESS
        assert original.id == updated.id
        assert original.description == updated.description

    def test_with_status_error_message(self) -> None:
        """with_status can include error message."""
        todo = Todo.create(description="Test", context="ctx")
        failed = todo.with_status(TodoStatus.FAILED, "Connection timeout")

        assert failed.status == TodoStatus.FAILED
        assert failed.error_message == "Connection timeout"


class TestEventFactories:
    """Tests for event factory functions."""

    def test_create_todo_event(self) -> None:
        """Test TODO creation event."""
        todo = Todo.create(
            description="Test improvement",
            context="exec-123",
            priority=Priority.HIGH,
        )
        event = _create_todo_event(todo)

        assert event.type == EVENT_TODO_CREATED
        assert event.aggregate_type == "todo"
        assert event.aggregate_id == todo.id
        assert event.data["description"] == "Test improvement"
        assert event.data["context"] == "exec-123"
        assert event.data["priority"] == "high"
        assert event.data["status"] == "pending"

    def test_create_status_change_event(self) -> None:
        """Test status change event."""
        todo = Todo.create(description="Test", context="ctx")
        event = _create_status_change_event(
            todo,
            TodoStatus.PENDING,
            TodoStatus.IN_PROGRESS,
        )

        assert event.type == EVENT_TODO_STATUS_CHANGED
        assert event.aggregate_type == "todo"
        assert event.aggregate_id == todo.id
        assert event.data["old_status"] == "pending"
        assert event.data["new_status"] == "in_progress"

    def test_create_status_change_event_with_error(self) -> None:
        """Test status change event with error message."""
        todo = Todo.create(description="Test", context="ctx")
        event = _create_status_change_event(
            todo,
            TodoStatus.IN_PROGRESS,
            TodoStatus.FAILED,
            "Task failed: timeout",
        )

        assert event.data["error_message"] == "Task failed: timeout"


class TestReconstructTodo:
    """Tests for event reconstruction."""

    def test_reconstruct_from_creation_event(self) -> None:
        """Reconstruct TODO from creation event only."""
        todo = Todo.create(description="Test", context="ctx", priority=Priority.LOW)
        event = _create_todo_event(todo)

        reconstructed = _reconstruct_todo_from_events([event])

        assert reconstructed is not None
        assert reconstructed.id == todo.id
        assert reconstructed.description == todo.description
        assert reconstructed.context == todo.context
        assert reconstructed.priority == Priority.LOW
        assert reconstructed.status == TodoStatus.PENDING

    def test_reconstruct_with_status_changes(self) -> None:
        """Reconstruct TODO with status change events."""
        todo = Todo.create(description="Test", context="ctx")
        creation_event = _create_todo_event(todo)
        status_event = _create_status_change_event(
            todo,
            TodoStatus.PENDING,
            TodoStatus.DONE,
        )

        reconstructed = _reconstruct_todo_from_events([creation_event, status_event])

        assert reconstructed is not None
        assert reconstructed.status == TodoStatus.DONE

    def test_reconstruct_empty_events(self) -> None:
        """Return None for empty event list."""
        result = _reconstruct_todo_from_events([])
        assert result is None

    def test_reconstruct_no_creation_event(self) -> None:
        """Return None if no creation event found."""
        todo = Todo.create(description="Test", context="ctx")
        status_event = _create_status_change_event(
            todo,
            TodoStatus.PENDING,
            TodoStatus.DONE,
        )

        result = _reconstruct_todo_from_events([status_event])
        assert result is None


class TestTodoRegistry:
    """Tests for TodoRegistry class."""

    @pytest.fixture
    def mock_event_store(self) -> MagicMock:
        """Create mock EventStore."""
        store = MagicMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def registry(self, mock_event_store: MagicMock) -> TodoRegistry:
        """Create TodoRegistry with mock store."""
        return TodoRegistry(_event_store=mock_event_store)

    @pytest.mark.asyncio
    async def test_register_success(
        self,
        registry: TodoRegistry,
        mock_event_store: MagicMock,
    ) -> None:
        """Successfully register a TODO."""
        result = await registry.register(
            description="Refactor auth",
            context="exec-123",
            priority=Priority.HIGH,
        )

        assert result.is_ok
        todo = result.value
        assert todo.description == "Refactor auth"
        assert todo.priority == Priority.HIGH
        assert todo.status == TodoStatus.PENDING

        # Verify event was appended
        mock_event_store.append.assert_called_once()
        event = mock_event_store.append.call_args[0][0]
        assert event.type == EVENT_TODO_CREATED

    @pytest.mark.asyncio
    async def test_register_persistence_error(
        self,
        registry: TodoRegistry,
        mock_event_store: MagicMock,
    ) -> None:
        """Handle persistence error during registration."""
        mock_event_store.append.side_effect = PersistenceError(
            "Database error",
            operation="insert",
        )

        result = await registry.register(
            description="Test",
            context="ctx",
        )

        assert result.is_err
        assert "Database error" in str(result.error)

    @pytest.mark.asyncio
    async def test_get_by_id_found(
        self,
        registry: TodoRegistry,
        mock_event_store: MagicMock,
    ) -> None:
        """Retrieve TODO by ID when it exists."""
        todo = Todo.create(description="Test", context="ctx")
        event = _create_todo_event(todo)
        mock_event_store.replay.return_value = [event]

        result = await registry.get_by_id(todo.id)

        assert result.is_ok
        assert result.value is not None
        assert result.value.id == todo.id

    @pytest.mark.asyncio
    async def test_get_by_id_not_found(
        self,
        registry: TodoRegistry,
        mock_event_store: MagicMock,
    ) -> None:
        """Return None when TODO not found."""
        mock_event_store.replay.return_value = []

        result = await registry.get_by_id("nonexistent")

        assert result.is_ok
        assert result.value is None

    @pytest.mark.asyncio
    async def test_update_status_success(
        self,
        registry: TodoRegistry,
        mock_event_store: MagicMock,
    ) -> None:
        """Successfully update TODO status."""
        # First register a TODO
        reg_result = await registry.register(
            description="Test",
            context="ctx",
        )
        todo = reg_result.value

        # Setup mock to return the TODO on replay
        creation_event = _create_todo_event(todo)
        mock_event_store.replay.return_value = [creation_event]

        # Update status
        result = await registry.update_status(
            todo.id,
            TodoStatus.DONE,
        )

        assert result.is_ok
        assert result.value.status == TodoStatus.DONE

    @pytest.mark.asyncio
    async def test_update_status_todo_not_found(
        self,
        registry: TodoRegistry,
        mock_event_store: MagicMock,
    ) -> None:
        """Error when updating nonexistent TODO."""
        mock_event_store.replay.return_value = []

        result = await registry.update_status(
            "nonexistent",
            TodoStatus.DONE,
        )

        assert result.is_err
        assert "not found" in str(result.error)

    @pytest.mark.asyncio
    async def test_get_pending_sorted_by_priority(
        self,
        mock_event_store: MagicMock,
    ) -> None:
        """Pending TODOs should be sorted by priority."""
        registry = TodoRegistry(_event_store=mock_event_store)

        # Register TODOs with different priorities
        todos = [
            Todo.create("Low task", "ctx", Priority.LOW),
            Todo.create("High task", "ctx", Priority.HIGH),
            Todo.create("Medium task", "ctx", Priority.MEDIUM),
        ]

        for todo in todos:
            _create_todo_event(todo)
            mock_event_store.append.return_value = None
            await registry.register(todo.description, todo.context, todo.priority)

        # Setup replay to return events
        def mock_replay(aggregate_type: str, aggregate_id: str) -> list:
            for todo in todos:
                if todo.id == aggregate_id:
                    return [_create_todo_event(todo)]
            return []

        # We need to track registered IDs
        registry._todo_ids = {todos[0].id, todos[1].id, todos[2].id}

        # Mock replay for each TODO
        mock_event_store.replay.side_effect = [
            [_create_todo_event(todos[0])],  # LOW
            [_create_todo_event(todos[1])],  # HIGH
            [_create_todo_event(todos[2])],  # MEDIUM
        ]

        result = await registry.get_pending()

        assert result.is_ok
        pending = result.value
        # HIGH should be first, then MEDIUM, then LOW
        assert pending[0].priority == Priority.HIGH
        assert pending[1].priority == Priority.MEDIUM
        assert pending[2].priority == Priority.LOW

    @pytest.mark.asyncio
    async def test_get_pending_with_limit(
        self,
        mock_event_store: MagicMock,
    ) -> None:
        """get_pending respects limit parameter."""
        registry = TodoRegistry(_event_store=mock_event_store)

        # Create multiple TODOs
        todos = [Todo.create(f"Task {i}", "ctx", Priority.MEDIUM) for i in range(5)]
        registry._todo_ids = {t.id for t in todos}

        mock_event_store.replay.side_effect = [[_create_todo_event(t)] for t in todos]

        result = await registry.get_pending(limit=3)

        assert result.is_ok
        assert len(result.value) == 3

    @pytest.mark.asyncio
    async def test_get_stats(
        self,
        mock_event_store: MagicMock,
    ) -> None:
        """get_stats returns status counts."""
        registry = TodoRegistry(_event_store=mock_event_store)

        # Create TODOs with different statuses
        pending = Todo.create("Pending", "ctx")
        done = Todo.create("Done", "ctx").with_status(TodoStatus.DONE)

        registry._todo_ids = {pending.id, done.id}

        # Create events for each TODO
        pending_event = _create_todo_event(pending)
        _create_todo_event(
            Todo.create("Done", "ctx")  # Original creation
        )
        _create_todo_event(done)

        mock_event_store.replay.side_effect = [
            [pending_event],
            [
                _create_todo_event(Todo.create("Done", "ctx")),
                _create_status_change_event(done, TodoStatus.PENDING, TodoStatus.DONE),
            ],
        ]

        result = await registry.get_stats()

        assert result.is_ok
        stats = result.value
        assert stats["pending"] == 1
        assert stats["done"] == 1

    def test_count_pending(self, registry: TodoRegistry) -> None:
        """count_pending returns number of tracked IDs."""
        assert registry.count_pending() == 0

        registry._todo_ids.add("id-1")
        registry._todo_ids.add("id-2")

        assert registry.count_pending() == 2


class TestTodoRegistryIntegration:
    """Integration tests for TodoRegistry with real EventStore."""

    @pytest.fixture
    async def event_store(self, tmp_path):  # -> EventStore
        """Create real EventStore with temp database."""
        from ouroboros.persistence.event_store import EventStore

        db_path = tmp_path / "test.db"
        store = EventStore(f"sqlite+aiosqlite:///{db_path}")
        await store.initialize()
        yield store
        await store.close()

    @pytest.fixture
    def registry(self, event_store) -> TodoRegistry:
        """Create TodoRegistry with real store."""
        return TodoRegistry(_event_store=event_store)

    @pytest.mark.asyncio
    async def test_full_lifecycle(self, registry: TodoRegistry) -> None:
        """Test complete TODO lifecycle: create -> update -> retrieve."""
        # Register
        reg_result = await registry.register(
            description="Improve error handling",
            context="execution-456",
            priority=Priority.HIGH,
        )
        assert reg_result.is_ok
        todo = reg_result.value

        # Verify initial state
        get_result = await registry.get_by_id(todo.id)
        assert get_result.is_ok
        assert get_result.value.status == TodoStatus.PENDING

        # Update to IN_PROGRESS
        update_result = await registry.update_status(todo.id, TodoStatus.IN_PROGRESS)
        assert update_result.is_ok
        assert update_result.value.status == TodoStatus.IN_PROGRESS

        # Complete
        done_result = await registry.update_status(todo.id, TodoStatus.DONE)
        assert done_result.is_ok
        assert done_result.value.status == TodoStatus.DONE

        # Verify final state
        final_result = await registry.get_by_id(todo.id)
        assert final_result.is_ok
        assert final_result.value.status == TodoStatus.DONE

    @pytest.mark.asyncio
    async def test_failed_todo_with_error_message(self, registry: TodoRegistry) -> None:
        """Test marking TODO as failed with error message."""
        reg_result = await registry.register(
            description="Flaky task",
            context="ctx",
        )
        todo = reg_result.value

        # Mark as failed
        fail_result = await registry.update_status(
            todo.id,
            TodoStatus.FAILED,
            "Connection refused",
        )
        assert fail_result.is_ok
        assert fail_result.value.status == TodoStatus.FAILED
        assert fail_result.value.error_message == "Connection refused"

    @pytest.mark.asyncio
    async def test_multiple_todos_priority_sorting(self, registry: TodoRegistry) -> None:
        """Test multiple TODOs are sorted by priority."""
        # Register in mixed order
        await registry.register("Low task", "ctx", Priority.LOW)
        await registry.register("High task", "ctx", Priority.HIGH)
        await registry.register("Medium task", "ctx", Priority.MEDIUM)

        result = await registry.get_pending()
        assert result.is_ok

        todos = result.value
        assert len(todos) == 3
        assert todos[0].description == "High task"
        assert todos[1].description == "Medium task"
        assert todos[2].description == "Low task"
