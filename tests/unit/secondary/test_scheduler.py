"""Tests for Secondary Loop Scheduler (Story 7-2)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.errors import OuroborosError, PersistenceError
from ouroboros.core.types import Result
from ouroboros.secondary.scheduler import (
    BatchStatus,
    BatchSummary,
    SecondaryLoopScheduler,
    TodoResult,
    _default_executor,
)
from ouroboros.secondary.todo_registry import (
    Priority,
    Todo,
    TodoRegistry,
    TodoStatus,
)


class TestBatchStatus:
    """Tests for BatchStatus enum."""

    def test_status_values(self) -> None:
        """Verify all batch status values."""
        assert BatchStatus.COMPLETED == "completed"
        assert BatchStatus.PARTIAL == "partial"
        assert BatchStatus.SKIPPED == "skipped"
        assert BatchStatus.NO_TODOS == "no_todos"


class TestTodoResult:
    """Tests for TodoResult dataclass."""

    def test_success_result(self) -> None:
        """Create successful TODO result."""
        result = TodoResult(
            todo_id="todo-123",
            description="Refactor module",
            success=True,
            duration_ms=150,
        )
        assert result.success is True
        assert result.error_message is None
        assert result.duration_ms == 150

    def test_failure_result(self) -> None:
        """Create failed TODO result."""
        result = TodoResult(
            todo_id="todo-456",
            description="Fix bug",
            success=False,
            error_message="Connection timeout",
            duration_ms=5000,
        )
        assert result.success is False
        assert result.error_message == "Connection timeout"

    def test_immutability(self) -> None:
        """TodoResult should be immutable."""
        result = TodoResult(todo_id="id", description="desc", success=True)
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore[misc]


class TestBatchSummary:
    """Tests for BatchSummary dataclass."""

    def test_creation(self) -> None:
        """Create batch summary."""
        started = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        completed = datetime(2024, 1, 1, 12, 0, 10, tzinfo=UTC)

        summary = BatchSummary(
            status=BatchStatus.COMPLETED,
            total=5,
            success_count=4,
            failure_count=1,
            skipped_count=0,
            results=(),
            started_at=started,
            completed_at=completed,
        )

        assert summary.status == BatchStatus.COMPLETED
        assert summary.total == 5
        assert summary.success_count == 4
        assert summary.failure_count == 1

    def test_duration_ms(self) -> None:
        """Test duration calculation."""
        started = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        completed = datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC)  # 5 seconds later

        summary = BatchSummary(
            status=BatchStatus.COMPLETED,
            total=1,
            success_count=1,
            failure_count=0,
            skipped_count=0,
            results=(),
            started_at=started,
            completed_at=completed,
        )

        assert summary.duration_ms == 5000

    def test_success_rate(self) -> None:
        """Test success rate calculation."""
        started = datetime.now(UTC)

        # 80% success rate
        summary = BatchSummary(
            status=BatchStatus.COMPLETED,
            total=10,
            success_count=8,
            failure_count=2,
            skipped_count=0,
            results=(),
            started_at=started,
            completed_at=started,
        )
        assert summary.success_rate == 0.8

    def test_success_rate_no_todos(self) -> None:
        """Success rate is 1.0 when no TODOs."""
        started = datetime.now(UTC)

        summary = BatchSummary(
            status=BatchStatus.NO_TODOS,
            total=0,
            success_count=0,
            failure_count=0,
            skipped_count=0,
            results=(),
            started_at=started,
            completed_at=started,
        )
        assert summary.success_rate == 1.0

    def test_failed_todos_property(self) -> None:
        """Test failed_todos filter."""
        results = (
            TodoResult("id-1", "Task 1", success=True),
            TodoResult("id-2", "Task 2", success=False, error_message="Error"),
            TodoResult("id-3", "Task 3", success=True),
            TodoResult("id-4", "Task 4", success=False, error_message="Error"),
        )
        started = datetime.now(UTC)

        summary = BatchSummary(
            status=BatchStatus.COMPLETED,
            total=4,
            success_count=2,
            failure_count=2,
            skipped_count=0,
            results=results,
            started_at=started,
            completed_at=started,
        )

        failed = summary.failed_todos
        assert len(failed) == 2
        assert all(not r.success for r in failed)

    def test_to_dict(self) -> None:
        """Test dict conversion for logging."""
        started = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
        completed = datetime(2024, 1, 1, 12, 0, 1, tzinfo=UTC)

        summary = BatchSummary(
            status=BatchStatus.COMPLETED,
            total=10,
            success_count=9,
            failure_count=1,
            skipped_count=0,
            results=(),
            started_at=started,
            completed_at=completed,
        )

        d = summary.to_dict()
        assert d["status"] == "completed"
        assert d["total"] == 10
        assert d["success_rate"] == "90.0%"
        assert d["duration_ms"] == 1000


class TestDefaultExecutor:
    """Tests for default executor function."""

    @pytest.mark.asyncio
    async def test_default_executor_returns_ok(self) -> None:
        """Default executor always returns Ok."""
        todo = Todo.create("Test", "ctx")
        result = await _default_executor(todo)
        assert result.is_ok


class TestSecondaryLoopScheduler:
    """Tests for SecondaryLoopScheduler class."""

    @pytest.fixture
    def mock_registry(self) -> MagicMock:
        """Create mock TodoRegistry."""
        registry = MagicMock(spec=TodoRegistry)
        registry.get_pending = AsyncMock(return_value=Result.ok([]))
        registry.get_stats = AsyncMock(return_value=Result.ok({}))
        registry.update_status = AsyncMock(return_value=Result.ok(None))
        return registry

    @pytest.fixture
    def scheduler(self, mock_registry: MagicMock) -> SecondaryLoopScheduler:
        """Create scheduler with mock registry."""
        return SecondaryLoopScheduler(_registry=mock_registry)

    def test_should_activate_primary_complete(self, scheduler: SecondaryLoopScheduler) -> None:
        """Activate when primary is complete and no skip flag."""
        assert scheduler.should_activate(primary_completed=True, skip_flag=False)

    def test_should_not_activate_skip_flag(self, scheduler: SecondaryLoopScheduler) -> None:
        """Don't activate when skip flag is set."""
        assert not scheduler.should_activate(primary_completed=True, skip_flag=True)

    def test_should_not_activate_primary_incomplete(
        self, scheduler: SecondaryLoopScheduler
    ) -> None:
        """Don't activate when primary is incomplete."""
        assert not scheduler.should_activate(primary_completed=False, skip_flag=False)

    @pytest.mark.asyncio
    async def test_process_batch_no_todos(
        self,
        scheduler: SecondaryLoopScheduler,
        mock_registry: MagicMock,
    ) -> None:
        """Return NO_TODOS status when no pending TODOs."""
        mock_registry.get_pending.return_value = Result.ok([])

        result = await scheduler.process_batch()

        assert result.is_ok
        summary = result.value
        assert summary.status == BatchStatus.NO_TODOS
        assert summary.total == 0

    @pytest.mark.asyncio
    async def test_process_batch_success(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Process batch successfully."""
        todos = [
            Todo.create("Task 1", "ctx", Priority.HIGH),
            Todo.create("Task 2", "ctx", Priority.MEDIUM),
        ]
        mock_registry.get_pending.return_value = Result.ok(todos)

        async def success_executor(todo: Todo) -> Result[None, OuroborosError]:
            return Result.ok(None)

        scheduler = SecondaryLoopScheduler(
            _registry=mock_registry,
            _executor=success_executor,
        )

        result = await scheduler.process_batch()

        assert result.is_ok
        summary = result.value
        assert summary.status == BatchStatus.COMPLETED
        assert summary.total == 2
        assert summary.success_count == 2
        assert summary.failure_count == 0

    @pytest.mark.asyncio
    async def test_process_batch_with_failures(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Process batch with some failures - failures don't block others."""
        todos = [
            Todo.create("Success task", "ctx", Priority.HIGH),
            Todo.create("Failing task", "ctx", Priority.MEDIUM),
            Todo.create("Another success", "ctx", Priority.LOW),
        ]
        mock_registry.get_pending.return_value = Result.ok(todos)

        call_count = 0

        async def mixed_executor(todo: Todo) -> Result[None, OuroborosError]:
            nonlocal call_count
            call_count += 1
            if "Failing" in todo.description:
                return Result.err(OuroborosError("Simulated failure"))
            return Result.ok(None)

        scheduler = SecondaryLoopScheduler(
            _registry=mock_registry,
            _executor=mixed_executor,
        )

        result = await scheduler.process_batch()

        assert result.is_ok
        summary = result.value
        assert summary.status == BatchStatus.COMPLETED
        assert summary.total == 3
        assert summary.success_count == 2
        assert summary.failure_count == 1
        # Verify all TODOs were processed (non-blocking)
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_process_batch_handles_exception(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Handle unexpected exceptions during TODO execution."""
        todos = [Todo.create("Crashy task", "ctx")]
        mock_registry.get_pending.return_value = Result.ok(todos)

        async def crashing_executor(todo: Todo) -> Result[None, OuroborosError]:
            raise RuntimeError("Unexpected crash!")

        scheduler = SecondaryLoopScheduler(
            _registry=mock_registry,
            _executor=crashing_executor,
        )

        result = await scheduler.process_batch()

        assert result.is_ok
        summary = result.value
        assert summary.failure_count == 1
        assert "Unexpected error" in summary.results[0].error_message

    @pytest.mark.asyncio
    async def test_process_batch_respects_limit(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Process batch respects limit parameter."""
        todos = [Todo.create(f"Task {i}", "ctx") for i in range(10)]
        mock_registry.get_pending.return_value = Result.ok(todos[:3])

        scheduler = SecondaryLoopScheduler(_registry=mock_registry)

        await scheduler.process_batch(limit=3)

        # Verify get_pending was called with limit
        mock_registry.get_pending.assert_called_once_with(limit=3)

    @pytest.mark.asyncio
    async def test_process_batch_updates_status(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Verify TODO status is updated during processing."""
        todo = Todo.create("Test task", "ctx")
        mock_registry.get_pending.return_value = Result.ok([todo])

        scheduler = SecondaryLoopScheduler(_registry=mock_registry)
        await scheduler.process_batch()

        # Should have called update_status twice: IN_PROGRESS and DONE
        assert mock_registry.update_status.call_count == 2

        calls = mock_registry.update_status.call_args_list
        assert calls[0][0][1] == TodoStatus.IN_PROGRESS
        assert calls[1][0][1] == TodoStatus.DONE

    @pytest.mark.asyncio
    async def test_skip_all_pending(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Skip all pending TODOs."""
        todos = [
            Todo.create("Task 1", "ctx"),
            Todo.create("Task 2", "ctx"),
        ]
        mock_registry.get_pending.return_value = Result.ok(todos)

        scheduler = SecondaryLoopScheduler(_registry=mock_registry)
        result = await scheduler.skip_all_pending(reason="User requested")

        assert result.is_ok
        summary = result.value
        assert summary.status == BatchStatus.SKIPPED
        assert summary.skipped_count == 2
        assert summary.total == 2

        # Verify each TODO was marked as SKIPPED
        assert mock_registry.update_status.call_count == 2
        for call in mock_registry.update_status.call_args_list:
            assert call[0][1] == TodoStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_get_status_report(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Get status report."""
        mock_registry.get_stats.return_value = Result.ok(
            {
                "pending": 5,
                "done": 10,
                "failed": 2,
            }
        )

        pending_todos = [
            Todo.create("High priority", "ctx", Priority.HIGH),
            Todo.create("Medium priority", "ctx", Priority.MEDIUM),
        ]
        mock_registry.get_pending.return_value = Result.ok(pending_todos)

        scheduler = SecondaryLoopScheduler(_registry=mock_registry)
        result = await scheduler.get_status_report()

        assert result.is_ok
        report = result.value
        assert report["pending_count"] == 5
        assert len(report["next_pending"]) == 2

    @pytest.mark.asyncio
    async def test_process_batch_persistence_error(
        self,
        mock_registry: MagicMock,
    ) -> None:
        """Handle persistence error from registry."""
        mock_registry.get_pending.return_value = Result.err(
            PersistenceError("Database unavailable")
        )

        scheduler = SecondaryLoopScheduler(_registry=mock_registry)
        result = await scheduler.process_batch()

        assert result.is_err


class TestSecondaryLoopSchedulerIntegration:
    """Integration tests with real registry."""

    @pytest.fixture
    async def event_store(self, tmp_path):
        """Create real EventStore."""
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
    async def test_full_batch_processing(self, registry: TodoRegistry) -> None:
        """Test complete batch processing flow."""
        # Register some TODOs
        await registry.register("High priority task", "ctx", Priority.HIGH)
        await registry.register("Low priority task", "ctx", Priority.LOW)
        await registry.register("Medium priority task", "ctx", Priority.MEDIUM)

        # Create scheduler with success executor
        async def executor(todo: Todo) -> Result[None, OuroborosError]:
            return Result.ok(None)

        scheduler = SecondaryLoopScheduler(
            _registry=registry,
            _executor=executor,
        )

        # Process
        result = await scheduler.process_batch()

        assert result.is_ok
        summary = result.value
        assert summary.total == 3
        assert summary.success_count == 3

        # Verify all TODOs are now DONE
        all_result = await registry.get_all()
        assert all_result.is_ok
        for todo in all_result.value:
            assert todo.status == TodoStatus.DONE

    @pytest.mark.asyncio
    async def test_failure_doesnt_block_others(self, registry: TodoRegistry) -> None:
        """Verify failure of one TODO doesn't block others."""
        await registry.register("Will succeed", "ctx", Priority.HIGH)
        await registry.register("Will fail", "ctx", Priority.MEDIUM)
        await registry.register("Will also succeed", "ctx", Priority.LOW)

        async def selective_executor(todo: Todo) -> Result[None, OuroborosError]:
            if "fail" in todo.description.lower():
                return Result.err(OuroborosError("Intentional failure"))
            return Result.ok(None)

        scheduler = SecondaryLoopScheduler(
            _registry=registry,
            _executor=selective_executor,
        )

        result = await scheduler.process_batch()

        assert result.is_ok
        summary = result.value
        assert summary.success_count == 2
        assert summary.failure_count == 1

        # Check individual statuses
        all_todos = (await registry.get_all()).value
        statuses = {t.description: t.status for t in all_todos}
        assert statuses["Will succeed"] == TodoStatus.DONE
        assert statuses["Will fail"] == TodoStatus.FAILED
        assert statuses["Will also succeed"] == TodoStatus.DONE

    @pytest.mark.asyncio
    async def test_skip_preserves_todos(self, registry: TodoRegistry) -> None:
        """Skipping marks TODOs as skipped, not deleted."""
        await registry.register("Task 1", "ctx")
        await registry.register("Task 2", "ctx")

        scheduler = SecondaryLoopScheduler(_registry=registry)
        await scheduler.skip_all_pending(reason="Deferred")

        # Check all are skipped
        all_todos = (await registry.get_all()).value
        assert len(all_todos) == 2
        for todo in all_todos:
            assert todo.status == TodoStatus.SKIPPED
