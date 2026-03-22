"""Unit tests for OrchestratorRunner."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.runner import (
    OrchestratorError,
    OrchestratorResult,
    OrchestratorRunner,
    build_system_prompt,
    build_task_prompt,
)
from ouroboros.orchestrator.session import SessionStatus, SessionTracker


@pytest.fixture
def sample_seed() -> Seed:
    """Create a sample seed for testing."""
    return Seed(
        goal="Build a task management CLI",
        constraints=("Python 3.14+", "No external database"),
        acceptance_criteria=(
            "Tasks can be created",
            "Tasks can be listed",
            "Tasks can be deleted",
        ),
        ontology_schema=OntologySchema(
            name="TaskManager",
            description="Task management ontology",
            fields=(
                OntologyField(
                    name="tasks",
                    field_type="array",
                    description="List of tasks",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="completeness",
                description="All requirements are met",
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="all_criteria_met",
                description="All acceptance criteria satisfied",
                evaluation_criteria="100% criteria pass",
            ),
        ),
        metadata=SeedMetadata(ambiguity_score=0.15),
    )


class TestBuildSystemPrompt:
    """Tests for build_system_prompt function."""

    def test_includes_goal(self, sample_seed: Seed) -> None:
        """Test that system prompt includes the goal."""
        prompt = build_system_prompt(sample_seed)
        assert sample_seed.goal in prompt

    def test_includes_constraints(self, sample_seed: Seed) -> None:
        """Test that system prompt includes constraints."""
        prompt = build_system_prompt(sample_seed)
        assert "Python 3.14+" in prompt
        assert "No external database" in prompt

    def test_includes_evaluation_principles(self, sample_seed: Seed) -> None:
        """Test that system prompt includes evaluation principles."""
        prompt = build_system_prompt(sample_seed)
        assert "completeness" in prompt
        assert "All requirements are met" in prompt

    def test_handles_empty_constraints(self) -> None:
        """Test handling seed with no constraints."""
        seed = Seed(
            goal="Test goal",
            constraints=(),
            acceptance_criteria=("AC1",),
            ontology_schema=OntologySchema(
                name="Test",
                description="Test",
            ),
            metadata=SeedMetadata(ambiguity_score=0.1),
        )
        prompt = build_system_prompt(seed)
        assert "None" in prompt or "Constraints" in prompt


class TestBuildTaskPrompt:
    """Tests for build_task_prompt function."""

    def test_includes_goal(self, sample_seed: Seed) -> None:
        """Test that task prompt includes the goal."""
        prompt = build_task_prompt(sample_seed)
        assert sample_seed.goal in prompt

    def test_includes_acceptance_criteria(self, sample_seed: Seed) -> None:
        """Test that task prompt includes all acceptance criteria."""
        prompt = build_task_prompt(sample_seed)
        assert "Tasks can be created" in prompt
        assert "Tasks can be listed" in prompt
        assert "Tasks can be deleted" in prompt

    def test_numbers_acceptance_criteria(self, sample_seed: Seed) -> None:
        """Test that acceptance criteria are numbered."""
        prompt = build_task_prompt(sample_seed)
        assert "1." in prompt
        assert "2." in prompt
        assert "3." in prompt


class TestOrchestratorResult:
    """Tests for OrchestratorResult dataclass."""

    def test_create_successful_result(self) -> None:
        """Test creating a successful result."""
        result = OrchestratorResult(
            success=True,
            session_id="sess_123",
            execution_id="exec_456",
            summary={"tasks_completed": 3},
            messages_processed=50,
            final_message="All tasks completed",
            duration_seconds=120.5,
        )

        assert result.success is True
        assert result.session_id == "sess_123"
        assert result.execution_id == "exec_456"
        assert result.summary["tasks_completed"] == 3
        assert result.messages_processed == 50
        assert result.duration_seconds == 120.5

    def test_result_is_frozen(self) -> None:
        """Test that OrchestratorResult is immutable."""
        result = OrchestratorResult(
            success=True,
            session_id="s",
            execution_id="e",
        )
        with pytest.raises(AttributeError):
            result.success = False  # type: ignore


class TestOrchestratorRunner:
    """Tests for OrchestratorRunner."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create a mock Claude agent adapter."""
        adapter = MagicMock()
        return adapter

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock Rich console."""
        return MagicMock()

    @pytest.fixture
    def runner(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> OrchestratorRunner:
        """Create a runner with mocked dependencies."""
        return OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

    @pytest.mark.asyncio
    async def test_execute_seed_success(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test successful seed execution."""

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(type="tool", content="Reading", tool_name="Read")
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        # Mock session creation using Result type
        from ouroboros.core.types import Result

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", mock_mark_completed):
                result = await runner.execute_seed(sample_seed)

        assert result.is_ok
        assert result.value.success is True
        # Parallel executor: 3 ACs × 3 messages each = 9 total
        assert result.value.messages_processed == 9

    @pytest.mark.asyncio
    async def test_execute_seed_failure(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Test handling of failed execution."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(
                type="result",
                content="Task failed: connection error",
                data={"subtype": "error"},
            )

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        async def mock_mark_failed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_failed", mock_mark_failed):
                result = await runner.execute_seed(sample_seed)

        assert result.is_ok
        assert result.value.success is False
        assert "failed" in result.value.final_message.lower()

    @pytest.mark.asyncio
    async def test_execute_seed_session_creation_fails(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Test handling when session creation fails."""
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        with patch.object(
            runner._session_repo,
            "create_session",
            return_value=Result.err(PersistenceError("DB error")),
        ):
            result = await runner.execute_seed(sample_seed)

        assert result.is_err
        assert "session" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_resume_session_already_completed(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Test that resuming completed session fails."""
        from ouroboros.core.types import Result

        completed_tracker = SessionTracker.create("exec", "seed").with_status(
            SessionStatus.COMPLETED
        )

        with patch.object(
            runner._session_repo,
            "reconstruct_session",
            return_value=Result.ok(completed_tracker),
        ):
            result = await runner.resume_session("sess_123", sample_seed)

        assert result.is_err
        assert "terminal state" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_resume_session_not_found(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Test handling when session not found."""
        from ouroboros.core.errors import PersistenceError
        from ouroboros.core.types import Result

        with patch.object(
            runner._session_repo,
            "reconstruct_session",
            return_value=Result.err(PersistenceError("Session not found")),
        ):
            result = await runner.resume_session("nonexistent", sample_seed)

        assert result.is_err

    def test_deserialize_runtime_handle_supports_legacy_progress(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test legacy Claude session progress still reconstructs a runtime handle."""
        handle = runner._deserialize_runtime_handle({"agent_session_id": "sess_legacy"})

        assert handle == RuntimeHandle(backend="claude", native_session_id="sess_legacy")

    @pytest.mark.asyncio
    async def test_resume_session_uses_runtime_handle(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Test resume_session passes normalized runtime handles to the adapter."""
        from ouroboros.core.types import Result

        runtime_handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_runtime",
        )
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.RUNNING
        )
        running_tracker = running_tracker.with_progress({"runtime": runtime_handle.to_dict()})

        captured_kwargs: dict[str, Any] = {}

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            captured_kwargs.update(kwargs)
            yield AgentMessage(
                type="result",
                content="Resumed successfully",
                data={"subtype": "success", "session_id": "sess_runtime"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.resume_session("sess_resume", sample_seed)

        assert result.is_ok
        assert captured_kwargs["resume_handle"] == runtime_handle

    @pytest.mark.asyncio
    async def test_execute_seed_uses_inherited_runtime_handle(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Delegated child runs should fork from the inherited parent runtime handle."""
        inherited_handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_parent",
            metadata={"fork_session": True},
        )
        mock_adapter = MagicMock()
        captured_kwargs: dict[str, Any] = {}

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            captured_kwargs.update(kwargs)
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            inherited_runtime_handle=inherited_handle,
            inherited_tools=["mcp__chrome-devtools__click"],
        )

        from ouroboros.core.types import Result

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert captured_kwargs["resume_handle"] == inherited_handle
        assert "mcp__chrome-devtools__click" in captured_kwargs["tools"]

    @pytest.mark.asyncio
    async def test_execute_parallel_passes_inherited_runtime_handle_to_executor(
        self,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Parallel delegated runs should propagate inherited runtime/tool context."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph
        from ouroboros.orchestrator.parallel_executor import (
            ACExecutionResult,
            ParallelExecutionResult,
        )

        inherited_handle = RuntimeHandle(
            backend="claude",
            native_session_id="sess_parent",
            metadata={"fork_session": True},
        )
        mock_adapter = MagicMock()
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            inherited_runtime_handle=inherited_handle,
        )
        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        captured_init: dict[str, Any] = {}
        captured_execute: dict[str, Any] = {}

        class FakeParallelExecutor:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                captured_init.update(kwargs)

            async def execute_parallel(self, **kwargs: Any) -> ParallelExecutionResult:
                captured_execute.update(kwargs)
                return ParallelExecutionResult(
                    results=tuple(
                        ACExecutionResult(
                            ac_index=index,
                            ac_content=ac,
                            success=True,
                            final_message="[TASK_COMPLETE]",
                        )
                        for index, ac in enumerate(sample_seed.acceptance_criteria)
                    ),
                    success_count=len(sample_seed.acceptance_criteria),
                    failure_count=0,
                    total_messages=3,
                    total_duration_seconds=0.1,
                )

        dependency_graph = DependencyGraph(
            nodes=tuple(
                ACNode(index=index, content=ac)
                for index, ac in enumerate(sample_seed.acceptance_criteria)
            ),
            execution_levels=(tuple(range(len(sample_seed.acceptance_criteria))),),
        )

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor",
                FakeParallelExecutor,
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo, "mark_completed", AsyncMock(return_value=Result.ok(None))
            ),
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read", "mcp__chrome-devtools__click"],
                system_prompt="system",
                start_time=datetime.now(UTC),
            )

        assert result.is_ok
        assert captured_init["inherited_runtime_handle"] == inherited_handle
        assert captured_execute["tools"] == ["Read", "mcp__chrome-devtools__click"]


class TestOrchestratorError:
    """Tests for OrchestratorError."""

    def test_create_error(self) -> None:
        """Test creating an orchestrator error."""
        error = OrchestratorError(
            message="Execution failed",
            details={"session_id": "sess_123"},
        )
        assert "Execution failed" in str(error)

    def test_error_with_details(self) -> None:
        """Test error includes details."""
        error = OrchestratorError(
            message="Failed",
            details={"code": 500, "reason": "timeout"},
        )
        assert error.details is not None
        assert error.details["code"] == 500


class TestOrchestratorRunnerWithMCP:
    """Tests for OrchestratorRunner with MCP integration."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create a mock Claude agent adapter."""
        adapter = MagicMock()
        return adapter

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock Rich console."""
        return MagicMock()

    @pytest.fixture
    def mock_mcp_manager(self) -> MagicMock:
        """Create a mock MCP client manager."""
        from ouroboros.mcp.types import MCPToolDefinition

        manager = MagicMock()
        manager.list_all_tools = AsyncMock(
            return_value=[
                MCPToolDefinition(
                    name="external_tool",
                    description="An external MCP tool",
                    server_name="test-server",
                ),
            ]
        )
        return manager

    def test_init_with_mcp_manager(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test runner initialization with MCP manager."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        assert runner.mcp_manager is mock_mcp_manager

    def test_init_without_mcp_manager(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Test runner initialization without MCP manager."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
        )

        assert runner.mcp_manager is None

    def test_init_with_mcp_tool_prefix(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test runner initialization with MCP tool prefix."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
            mcp_tool_prefix="ext_",
        )

        assert runner._mcp_tool_prefix == "ext_"

    @pytest.mark.asyncio
    async def test_get_merged_tools_without_mcp(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Test getting merged tools without MCP manager."""
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
        )

        merged_tools, provider = await runner._get_merged_tools("session_123")

        assert merged_tools == DEFAULT_TOOLS
        assert provider is None

    @pytest.mark.asyncio
    async def test_get_merged_tools_with_mcp(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test getting merged tools with MCP manager."""
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        merged_tools, provider = await runner._get_merged_tools("session_123")

        # Should include DEFAULT_TOOLS + MCP tools
        assert all(t in merged_tools for t in DEFAULT_TOOLS)
        assert "external_tool" in merged_tools
        assert provider is not None

    @pytest.mark.asyncio
    async def test_get_merged_tools_includes_inherited_tools(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> None:
        """Delegated runners should merge inherited tools without duplicating built-ins."""
        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            inherited_tools=["Read", "mcp__chrome-devtools__click"],
        )

        merged_tools, provider = await runner._get_merged_tools("session_123")

        assert "mcp__chrome-devtools__click" in merged_tools
        assert merged_tools.count("Read") == 1
        assert provider is None

    @pytest.mark.asyncio
    async def test_get_merged_tools_mcp_failure(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test graceful handling when MCP tool listing fails."""
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

        mock_mcp_manager.list_all_tools = AsyncMock(side_effect=Exception("Connection lost"))

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        merged_tools, provider = await runner._get_merged_tools("session_123")

        # Should still return DEFAULT_TOOLS on failure
        assert merged_tools == DEFAULT_TOOLS
        # Provider is still returned (error is handled gracefully inside MCPToolProvider)
        # This allows callers to retry or check provider state
        assert provider is not None
        # No MCP tools should have been added
        assert len(merged_tools) == len(DEFAULT_TOOLS)

    @pytest.mark.asyncio
    async def test_execute_seed_with_mcp_tools(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Test seed execution uses merged tools."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="result",
                content="Done",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        # Mock session creation
        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_completed", mock_mark_completed):
                result = await runner.execute_seed(sample_seed)

        assert result.is_ok
        # MCP tools loaded event should have been emitted
        assert mock_event_store.append.called


class TestCancellationPolling:
    """Tests for cancellation detection in execution loops."""

    @pytest.fixture
    def mock_adapter(self) -> MagicMock:
        """Create a mock Claude agent adapter."""
        return MagicMock()

    @pytest.fixture
    def mock_event_store(self) -> AsyncMock:
        """Create a mock event store."""
        store = AsyncMock()
        store.append = AsyncMock()
        store.replay = AsyncMock(return_value=[])
        store.query_events = AsyncMock(return_value=[])
        return store

    @pytest.fixture
    def mock_console(self) -> MagicMock:
        """Create a mock Rich console."""
        return MagicMock()

    @pytest.fixture
    def runner(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
    ) -> OrchestratorRunner:
        """Create a runner with mocked dependencies."""
        return OrchestratorRunner(mock_adapter, mock_event_store, mock_console)

    @pytest.mark.asyncio
    async def test_check_cancellation_returns_false_when_no_event(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns False when no cancellation event exists."""
        mock_event_store.query_events = AsyncMock(return_value=[])
        result = await runner._check_cancellation("session_123")
        assert result is False
        mock_event_store.query_events.assert_called_once_with(
            aggregate_id="session_123",
            event_type="orchestrator.session.cancelled",
            limit=1,
        )

    @pytest.mark.asyncio
    async def test_check_cancellation_returns_true_when_event_exists(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns True when cancellation event exists."""
        from ouroboros.orchestrator.events import create_session_cancelled_event

        cancel_event = create_session_cancelled_event("session_123", "User requested")
        mock_event_store.query_events = AsyncMock(return_value=[cancel_event])
        result = await runner._check_cancellation("session_123")
        assert result is True

    @pytest.mark.asyncio
    async def test_check_cancellation_graceful_on_error(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns False on event store error (graceful degradation)."""
        mock_event_store.query_events = AsyncMock(side_effect=Exception("DB unavailable"))
        result = await runner._check_cancellation("session_123")
        assert result is False

    @pytest.mark.asyncio
    async def test_handle_cancellation_returns_result(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test _handle_cancellation returns a proper OrchestratorResult."""
        from datetime import UTC, datetime

        start_time = datetime.now(UTC)

        with patch.object(runner._session_repo, "mark_cancelled", AsyncMock(return_value=None)):
            result = await runner._handle_cancellation(
                session_id="sess_123",
                execution_id="exec_456",
                messages_processed=10,
                start_time=start_time,
            )

        assert result.is_ok
        assert result.value.success is False
        assert result.value.session_id == "sess_123"
        assert result.value.execution_id == "exec_456"
        assert result.value.messages_processed == 10
        assert "cancelled" in result.value.final_message.lower()
        assert result.value.summary.get("cancelled") is True

    @pytest.mark.asyncio
    async def test_execute_seed_stops_on_cancellation(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test that execute_seed detects cancellation and stops execution."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.events import create_session_cancelled_event
        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        messages_yielded = 0

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            nonlocal messages_yielded
            # Yield enough messages to trigger a cancellation check
            for i in range(CANCELLATION_CHECK_INTERVAL + 5):
                messages_yielded += 1
                yield AgentMessage(type="assistant", content=f"Message {i}")
            # This final message should never be reached
            yield AgentMessage(
                type="result",
                content="Should not reach here",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        # Return no cancellation initially, then return a cancellation event
        cancel_event = create_session_cancelled_event("session_123", "User requested")
        mock_event_store.query_events = AsyncMock(return_value=[cancel_event])

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        async def mock_mark_cancelled(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_cancelled", mock_mark_cancelled),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert result.value.success is False
        assert "cancelled" in result.value.final_message.lower()
        # Should have stopped at the cancellation check interval
        assert result.value.messages_processed == CANCELLATION_CHECK_INTERVAL

    @pytest.mark.asyncio
    async def test_execute_seed_no_cancellation_proceeds_normally(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test that execute_seed runs normally when no cancellation is issued."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(type="assistant", content="Working...")
            yield AgentMessage(type="tool", content="Reading", tool_name="Read")
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute
        # No cancellation events
        mock_event_store.query_events = AsyncMock(return_value=[])

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "create_session", mock_create_session),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_ok
        assert result.value.success is True

    @pytest.mark.asyncio
    async def test_resume_session_stops_on_cancellation(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Test that resume_session detects cancellation and stops."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.events import create_session_cancelled_event
        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            for i in range(CANCELLATION_CHECK_INTERVAL + 5):
                yield AgentMessage(type="assistant", content=f"Message {i}")
            yield AgentMessage(
                type="result",
                content="Should not reach",
                data={"subtype": "success"},
            )

        mock_adapter.execute_task = mock_execute

        cancel_event = create_session_cancelled_event("sess_resume", "User requested")
        mock_event_store.query_events = AsyncMock(return_value=[cancel_event])

        running_tracker = SessionTracker.create("exec_resume", "seed_1").with_status(
            SessionStatus.RUNNING
        )

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_cancelled(*args: Any, **kwargs: Any):
            return Result.ok(None)

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_cancelled", mock_mark_cancelled),
        ):
            result = await runner.resume_session("sess_resume", sample_seed)

        assert result.is_ok
        assert result.value.success is False
        assert "cancelled" in result.value.final_message.lower()

    @pytest.mark.asyncio
    async def test_cancellation_check_interval_constant(self) -> None:
        """Test that CANCELLATION_CHECK_INTERVAL is defined and reasonable."""
        from ouroboros.orchestrator.runner import CANCELLATION_CHECK_INTERVAL

        assert isinstance(CANCELLATION_CHECK_INTERVAL, int)
        assert CANCELLATION_CHECK_INTERVAL > 0
        assert CANCELLATION_CHECK_INTERVAL <= 20  # Reasonable upper bound

    @pytest.mark.asyncio
    async def test_check_cancellation_detects_in_memory_registry(
        self,
        runner: OrchestratorRunner,
        mock_event_store: AsyncMock,
    ) -> None:
        """Test _check_cancellation returns True when session is in the in-memory registry."""
        from ouroboros.orchestrator.runner import (
            _cancellation_registry,
            clear_cancellation,
            request_cancellation,
        )

        # Ensure clean state
        _cancellation_registry.discard("sess_inmem")

        request_cancellation("sess_inmem")
        try:
            # Should return True without even querying the event store
            result = await runner._check_cancellation("sess_inmem")
            assert result is True
            # Event store query should NOT have been called (fast path)
            mock_event_store.query_events.assert_not_called()
        finally:
            clear_cancellation("sess_inmem")

    @pytest.mark.asyncio
    async def test_handle_cancellation_clears_in_memory_registry(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Test _handle_cancellation clears the in-memory registry entry."""
        from datetime import UTC, datetime

        from ouroboros.orchestrator.runner import (
            is_cancellation_requested,
            request_cancellation,
        )

        request_cancellation("sess_clear")

        with patch.object(runner._session_repo, "mark_cancelled", AsyncMock(return_value=None)):
            await runner._handle_cancellation(
                session_id="sess_clear",
                execution_id="exec_clear",
                messages_processed=5,
                start_time=datetime.now(UTC),
            )

        assert is_cancellation_requested("sess_clear") is False


class TestCancellationRegistry:
    """Tests for the module-level in-memory cancellation registry functions."""

    def setup_method(self) -> None:
        """Clear the registry before each test."""
        from ouroboros.orchestrator.runner import _cancellation_registry

        _cancellation_registry.clear()

    def teardown_method(self) -> None:
        """Clear the registry after each test."""
        from ouroboros.orchestrator.runner import _cancellation_registry

        _cancellation_registry.clear()

    def test_request_cancellation_adds_session(self) -> None:
        """Test that request_cancellation adds the session ID to the registry."""
        from ouroboros.orchestrator.runner import (
            is_cancellation_requested,
            request_cancellation,
        )

        assert is_cancellation_requested("sess_1") is False
        request_cancellation("sess_1")
        assert is_cancellation_requested("sess_1") is True

    def test_clear_cancellation_removes_session(self) -> None:
        """Test that clear_cancellation removes the session ID."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            is_cancellation_requested,
            request_cancellation,
        )

        request_cancellation("sess_2")
        assert is_cancellation_requested("sess_2") is True
        clear_cancellation("sess_2")
        assert is_cancellation_requested("sess_2") is False

    def test_clear_cancellation_is_idempotent(self) -> None:
        """Test that clearing a non-existent session does not raise."""
        from ouroboros.orchestrator.runner import clear_cancellation

        # Should not raise
        clear_cancellation("nonexistent_session")

    def test_get_pending_cancellations_returns_frozenset(self) -> None:
        """Test that get_pending_cancellations returns a frozenset snapshot."""
        from ouroboros.orchestrator.runner import (
            get_pending_cancellations,
            request_cancellation,
        )

        request_cancellation("sess_a")
        request_cancellation("sess_b")

        pending = get_pending_cancellations()
        assert isinstance(pending, frozenset)
        assert pending == frozenset({"sess_a", "sess_b"})

    def test_get_pending_cancellations_is_snapshot(self) -> None:
        """Test that the returned frozenset is a snapshot, not a live view."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            get_pending_cancellations,
            request_cancellation,
        )

        request_cancellation("sess_snap")
        snapshot = get_pending_cancellations()
        clear_cancellation("sess_snap")

        # Snapshot should still contain the session
        assert "sess_snap" in snapshot
        # But the registry should not
        new_snapshot = get_pending_cancellations()
        assert "sess_snap" not in new_snapshot

    def test_multiple_sessions_tracked_independently(self) -> None:
        """Test that multiple sessions can be tracked independently."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            is_cancellation_requested,
            request_cancellation,
        )

        request_cancellation("sess_x")
        request_cancellation("sess_y")

        assert is_cancellation_requested("sess_x") is True
        assert is_cancellation_requested("sess_y") is True

        clear_cancellation("sess_x")
        assert is_cancellation_requested("sess_x") is False
        assert is_cancellation_requested("sess_y") is True

    def test_request_cancellation_is_idempotent(self) -> None:
        """Test that requesting cancellation twice is safe."""
        from ouroboros.orchestrator.runner import (
            get_pending_cancellations,
            request_cancellation,
        )

        request_cancellation("sess_dup")
        request_cancellation("sess_dup")

        assert len(get_pending_cancellations()) == 1


class TestExecutionCancelledError:
    """Tests for ExecutionCancelledError."""

    def test_create_with_defaults(self) -> None:
        """Test creating error with default reason."""
        from ouroboros.orchestrator.runner import ExecutionCancelledError

        error = ExecutionCancelledError(session_id="sess_123")
        assert error.session_id == "sess_123"
        assert error.reason == "Cancelled by user"
        assert "sess_123" in str(error)

    def test_create_with_custom_reason(self) -> None:
        """Test creating error with custom reason."""
        from ouroboros.orchestrator.runner import ExecutionCancelledError

        error = ExecutionCancelledError(session_id="sess_456", reason="Auto-cleanup: stale")
        assert error.session_id == "sess_456"
        assert error.reason == "Auto-cleanup: stale"
        assert "Auto-cleanup: stale" in str(error)
