"""Unit tests for OrchestratorRunner."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, AsyncIterator
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
from ouroboros.orchestrator.adapter import AgentMessage
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
        assert result.value.messages_processed == 3

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

    def test_format_progress_text_assistant(self, runner: OrchestratorRunner) -> None:
        """Test formatting assistant message."""
        msg = AgentMessage(type="assistant", content="I am analyzing the code for bugs")
        text = runner._format_progress_text(msg, 5)

        assert "(5)" in text
        assert "analyzing" in text.lower()

    def test_format_progress_text_tool(self, runner: OrchestratorRunner) -> None:
        """Test formatting tool call message."""
        msg = AgentMessage(type="tool", content="Reading file", tool_name="Read")
        text = runner._format_progress_text(msg, 10)

        assert "(10)" in text
        assert "Read" in text

    def test_format_progress_text_result(self, runner: OrchestratorRunner) -> None:
        """Test formatting result message."""
        msg = AgentMessage(type="result", content="Done")
        text = runner._format_progress_text(msg, 20)

        assert "(20)" in text
        assert "Finalizing" in text

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
        assert "already completed" in str(result.error).lower()

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
        manager.list_all_tools = AsyncMock(return_value=[
            MCPToolDefinition(
                name="external_tool",
                description="An external MCP tool",
                server_name="test-server",
            ),
        ])
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
    async def test_get_merged_tools_mcp_failure(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test graceful handling when MCP tool listing fails."""
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

        mock_mcp_manager.list_all_tools = AsyncMock(
            side_effect=Exception("Connection lost")
        )

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
        from ouroboros.orchestrator.adapter import DEFAULT_TOOLS

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
