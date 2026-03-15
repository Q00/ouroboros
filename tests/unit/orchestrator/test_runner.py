"""Unit tests for OrchestratorRunner."""

from __future__ import annotations

from collections.abc import AsyncIterator
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
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.dependency_analyzer import ACNode, DependencyGraph

# TODO: uncomment when OpenCode runtime is shipped
# from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime
from ouroboros.orchestrator.parallel_executor import ACExecutionResult, ParallelExecutionResult
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
        adapter.runtime_backend = "opencode"
        adapter.working_directory = "/tmp/project"
        adapter.permission_mode = "acceptEdits"
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
    async def test_prepare_session_forwards_seed_goal(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """prepare_session reserves a session with the seed goal persisted."""
        tracker = SessionTracker.create(
            "exec_prepared",
            sample_seed.metadata.seed_id,
            session_id="orch_prepared",
        )
        create_session = AsyncMock(return_value=Result.ok(tracker))

        with patch.object(runner._session_repo, "create_session", create_session):
            result = await runner.prepare_session(
                sample_seed,
                execution_id="exec_prepared",
                session_id="orch_prepared",
            )

        assert result.is_ok
        assert result.value is tracker
        create_session.assert_awaited_once_with(
            execution_id="exec_prepared",
            seed_id=sample_seed.metadata.seed_id,
            session_id="orch_prepared",
            seed_goal=sample_seed.goal,
        )

    @pytest.mark.asyncio
    async def test_execute_seed_delegates_to_precreated_session(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """execute_seed should reserve IDs first, then run the precreated session."""
        tracker = SessionTracker.create(
            "exec_delegated",
            sample_seed.metadata.seed_id,
            session_id="orch_delegated",
        )
        orchestrator_result = OrchestratorResult(
            success=True,
            session_id=tracker.session_id,
            execution_id=tracker.execution_id,
        )
        prepare_session = AsyncMock(return_value=Result.ok(tracker))
        execute_precreated = AsyncMock(return_value=Result.ok(orchestrator_result))

        with (
            patch.object(runner, "prepare_session", prepare_session),
            patch.object(runner, "execute_precreated_session", execute_precreated),
        ):
            result = await runner.execute_seed(sample_seed, execution_id="exec_delegated")

        assert result.is_ok
        assert result.value == orchestrator_result
        prepare_session.assert_awaited_once_with(sample_seed, execution_id="exec_delegated")
        execute_precreated.assert_awaited_once_with(
            seed=sample_seed,
            tracker=tracker,
            parallel=True,
        )

    @pytest.mark.asyncio
    async def test_execute_seed_seeds_startup_tool_catalog_on_runtime_handle(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Initial runtime startup should expose the merged tool catalog before tool calls."""
        from ouroboros.core.types import Result

        captured_kwargs: dict[str, Any] = {}
        mock_adapter._runtime_handle_backend = "opencode"
        mock_adapter._cwd = "/tmp/project"
        mock_adapter._permission_mode = "acceptEdits"

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            captured_kwargs.update(kwargs)
            resume_handle = kwargs["resume_handle"]
            assert isinstance(resume_handle, RuntimeHandle)
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success"},
                resume_handle=resume_handle,
            )

        mock_adapter.execute_task = mock_execute

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
        resume_handle = captured_kwargs["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == "opencode"
        assert resume_handle.cwd == "/tmp/project"
        assert resume_handle.metadata["tool_catalog"][0]["name"] == "Read"
        assert resume_handle.metadata["tool_catalog"][0]["id"] == "builtin:Read"
        assert "Edit" in {tool["name"] for tool in resume_handle.metadata["tool_catalog"]}

    def test_build_progress_update_serializes_opencode_tool_result_metadata(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """OpenCode tool/result metadata should survive into persisted progress state."""
        from ouroboros.orchestrator.mcp_tools import (
            normalize_runtime_tool_definition,
            normalize_runtime_tool_result,
        )

        runtime_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-1",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "server_session_id": "server-42",
                "runtime_event_type": "tool.completed",
            },
        )
        message = AgentMessage(
            type="assistant",
            content="Updated src/app.py",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/app.py"},
                "tool_definition": normalize_runtime_tool_definition(
                    "Edit",
                    {"file_path": "src/app.py"},
                ),
                "tool_result": normalize_runtime_tool_result("Updated src/app.py"),
            },
            resume_handle=runtime_handle,
        )

        progress = runner._build_progress_update(message, 3)

        assert progress["last_message_type"] == "tool_result"
        assert progress["messages_processed"] == 3
        assert progress["runtime_backend"] == "opencode"
        assert progress["runtime_event_type"] == "tool.completed"
        assert progress["tool_name"] == "Edit"
        assert progress["tool_input"] == {"file_path": "src/app.py"}
        assert progress["tool_definition"]["name"] == "Edit"
        assert progress["tool_result"]["text_content"] == "Updated src/app.py"
        assert progress["runtime"] == {
            "backend": "opencode",
            "kind": "agent_runtime",
            "native_session_id": "oc-session-1",
            "cwd": "/tmp/project",
            "approval_mode": "acceptEdits",
            "metadata": {
                "server_session_id": "server-42",
            },
        }

    def test_build_progress_update_projects_empty_tool_result_content(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Projected tool-result text should drive persisted progress previews."""
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        message = AgentMessage(
            type="assistant",
            content="",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
            },
        )

        progress = runner._build_progress_update(message, 4)
        progress_event = runner._build_progress_event("sess_123", message, step=4)

        assert progress["last_message_type"] == "tool_result"
        assert progress["content_preview"] == "[AC_COMPLETE: 1] Done!"
        assert progress_event.data["content_preview"] == "[AC_COMPLETE: 1] Done!"
        assert progress_event.data["progress"]["last_content_preview"] == "[AC_COMPLETE: 1] Done!"

    def test_build_progress_update_extracts_ac_tracking_from_tool_result_payload(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Persisted progress should keep AC markers from normalized tool-result payloads."""
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        message = AgentMessage(
            type="assistant",
            content="Tool completed successfully.",
            data={
                "subtype": "tool_result",
                "tool_name": "Edit",
                "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
            },
        )

        progress = runner._build_progress_update(message, 4)
        progress_event = runner._build_progress_event("sess_123", message, step=4)

        assert progress["content_preview"] == "Tool completed successfully."
        assert progress["ac_tracking"] == {"started": [], "completed": [1]}
        assert progress_event.data["content_preview"] == "Tool completed successfully."
        assert progress_event.data["ac_tracking"] == {"started": [], "completed": [1]}
        assert progress_event.data["progress"]["ac_tracking"] == {
            "started": [],
            "completed": [1],
        }

    def test_build_progress_event_serializes_ac_tracking_metadata(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """AC marker metadata should survive into persisted progress events."""
        message = AgentMessage(
            type="assistant",
            content="[AC_START: 2] Implementing the second acceptance criterion.",
            data={"ac_tracking": {"started": [2], "completed": []}},
            resume_handle=RuntimeHandle(backend="opencode", native_session_id="oc-session-1"),
        )

        progress = runner._build_progress_update(message, 4)
        progress_event = runner._build_progress_event("sess_123", message)

        assert progress["ac_tracking"] == {"started": [2], "completed": []}
        assert progress_event.data["ac_tracking"] == {"started": [2], "completed": []}
        assert progress_event.data["progress"]["ac_tracking"] == {
            "started": [2],
            "completed": [],
        }

    @pytest.mark.asyncio
    async def test_execute_seed_emits_enriched_opencode_tool_and_progress_events(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """OpenCode-backed runs should reuse the standard tool/progress event stream."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.mcp_tools import (
            normalize_runtime_tool_definition,
            normalize_runtime_tool_result,
        )

        runtime_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-1",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={
                "server_session_id": "server-42",
                "runtime_event_type": "session.started",
            },
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="system",
                content="OpenCode session initialized",
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="assistant",
                content="Calling tool: Edit: src/app.py",
                tool_name="Edit",
                data={
                    "tool_input": {"file_path": "src/app.py"},
                    "tool_definition": normalize_runtime_tool_definition(
                        "Edit",
                        {"file_path": "src/app.py"},
                    ),
                },
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="assistant",
                content="Updated src/app.py",
                data={
                    "subtype": "tool_result",
                    "tool_name": "Edit",
                    "tool_definition": normalize_runtime_tool_definition("Edit"),
                    "tool_result": normalize_runtime_tool_result("Updated src/app.py"),
                },
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="result",
                content="Task completed successfully",
                data={"subtype": "success"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

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

        appended_events = [call.args[0] for call in mock_event_store.append.await_args_list]
        tool_event = next(
            event for event in appended_events if event.type == "orchestrator.tool.called"
        )
        progress_events = [
            event
            for event in appended_events
            if event.type == "orchestrator.progress.updated" and event.data.get("message_type")
        ]

        assert tool_event.data["tool_name"] == "Edit"
        assert tool_event.data["tool_input_preview"] == "file_path: src/app.py"
        assert tool_event.data["tool_input"] == {"file_path": "src/app.py"}
        assert tool_event.data["tool_definition"]["name"] == "Edit"
        assert tool_event.data["runtime_backend"] == "opencode"

        system_event = next(
            event for event in progress_events if event.data["message_type"] == "system"
        )
        tool_result_event = next(
            event for event in progress_events if event.data["message_type"] == "tool_result"
        )

        assert system_event.data["runtime_backend"] == "opencode"
        assert system_event.data["session_id"] == "oc-session-1"
        assert system_event.data["server_session_id"] == "server-42"
        assert system_event.data["resume_session_id"] == "oc-session-1"
        assert system_event.data["runtime"]["native_session_id"] == "oc-session-1"
        assert tool_result_event.data["tool_name"] == "Edit"
        assert tool_result_event.data["resume_session_id"] == "oc-session-1"
        assert tool_result_event.data["tool_result"]["text_content"] == "Updated src/app.py"

    @pytest.mark.asyncio
    async def test_execute_seed_emits_workflow_progress_with_projected_last_update(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Workflow progress updates should carry the normalized latest runtime artifact."""
        from ouroboros.core.types import Result
        from ouroboros.orchestrator.mcp_tools import normalize_runtime_tool_result

        runtime_handle = RuntimeHandle(
            backend="opencode",
            native_session_id="oc-session-1",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            metadata={"server_session_id": "server-42"},
        )

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="assistant",
                content="Tool completed successfully.",
                data={
                    "subtype": "tool_result",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/app.py"},
                    "tool_result": normalize_runtime_tool_result("[AC_COMPLETE: 1] Done!"),
                    "runtime_event_type": "tool.completed",
                },
                resume_handle=runtime_handle,
            )
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success", "runtime_event_type": "result.completed"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute

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

        workflow_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "workflow.progress.updated"
        ]
        tool_result_workflow_event = next(
            event
            for event in workflow_events
            if event.data.get("last_update", {}).get("message_type") == "tool_result"
        )

        assert tool_result_workflow_event.data["completed_count"] == 1
        assert tool_result_workflow_event.data["current_ac_index"] == 2
        last_update = tool_result_workflow_event.data["last_update"]
        assert last_update["message_type"] == "tool_result"
        assert last_update["content_preview"] == "Tool completed successfully."
        assert last_update["tool_name"] == "Edit"
        assert last_update["tool_input"] == {"file_path": "src/app.py"}
        assert last_update["tool_result"]["text_content"] == "[AC_COMPLETE: 1] Done!"
        assert last_update["tool_result"]["is_error"] is False
        assert last_update["tool_result"]["meta"] == {}
        assert last_update["tool_result"]["content"][0]["type"] == "text"
        assert last_update["tool_result"]["content"][0]["text"] == "[AC_COMPLETE: 1] Done!"
        assert last_update["runtime_signal"] == "tool_completed"
        assert last_update["runtime_status"] == "running"
        assert last_update["ac_tracking"] == {"started": [], "completed": [1]}

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
    async def test_execute_seed_exception_marks_session_failed(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Unexpected execution exceptions should mark the session as failed."""
        from ouroboros.core.types import Result

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            if False:
                yield AgentMessage(type="assistant", content="never")
            raise RuntimeError("coordinator crash")

        mock_adapter.execute_task = mock_execute

        async def mock_create_session(*args: Any, **kwargs: Any):
            return Result.ok(SessionTracker.create("exec", sample_seed.metadata.seed_id))

        mark_failed = AsyncMock(return_value=Result.ok(None))

        with patch.object(runner._session_repo, "create_session", mock_create_session):
            with patch.object(runner._session_repo, "mark_failed", mark_failed):
                result = await runner.execute_seed(sample_seed, parallel=False)

        assert result.is_err
        assert "coordinator crash" in str(result.error)
        mark_failed.assert_awaited_once()
        assert mark_failed.await_args.args[1] == "coordinator crash"

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

    def test_deserialize_runtime_handle_falls_back_from_invalid_runtime_payload(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Malformed runtime payloads should not block the legacy session-id fallback."""
        handle = runner._deserialize_runtime_handle(
            {
                "runtime": {
                    "native_session_id": "sess_ignored",
                    "metadata": {"server_session_id": "server-42"},
                },
                "agent_session_id": "sess_legacy",
                "runtime_backend": "claude",
            }
        )

        assert handle == RuntimeHandle(backend="claude", native_session_id="sess_legacy")

    def test_deserialize_runtime_handle_returns_none_when_invalid_payload_has_no_fallback(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Malformed runtime payloads without legacy fallback data should be ignored."""
        handle = runner._deserialize_runtime_handle(
            {
                "runtime": {
                    "native_session_id": "sess_ignored",
                    "metadata": {"server_session_id": "server-42"},
                }
            }
        )

        assert handle is None

    def test_build_progress_update_round_trips_persisted_opencode_resume_handle(
        self,
        runner: OrchestratorRunner,
    ) -> None:
        """Persisted OpenCode progress should preserve the reconnect handle exactly."""
        runtime_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            cwd="/tmp/project",
            approval_mode="acceptEdits",
            updated_at="2026-03-13T00:00:00+00:00",
            metadata={
                "server_session_id": "server-42",
                "session_scope_id": "ac_1",
                "session_state_path": "execution.acceptance_criteria.ac_1.implementation_session",
                "session_role": "implementation",
                "retry_attempt": 0,
            },
        )
        message = AgentMessage(
            type="system",
            content="OpenCode session bound",
            resume_handle=runtime_handle,
        )

        progress = runner._build_progress_update(message, 2)
        restored = runner._deserialize_runtime_handle(progress)

        assert progress["runtime"] == runtime_handle.to_session_state_dict()
        assert progress["runtime_backend"] == "opencode"
        assert progress["server_session_id"] == "server-42"
        assert progress["resume_session_id"] == "server-42"
        assert restored is not None
        assert restored.backend == runtime_handle.backend
        assert restored.kind == runtime_handle.kind
        assert restored.cwd == runtime_handle.cwd
        assert restored.approval_mode == runtime_handle.approval_mode
        assert restored.metadata == runtime_handle.metadata

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
        resume_handle = captured_kwargs["resume_handle"]
        assert isinstance(resume_handle, RuntimeHandle)
        assert resume_handle.backend == runtime_handle.backend
        assert resume_handle.native_session_id == runtime_handle.native_session_id
        assert resume_handle.metadata["tool_catalog"][0]["name"] == "Read"

    @pytest.mark.asyncio
    @pytest.mark.skip(reason="OpenCode runtime not yet shipped")
    async def test_resume_session_reconnects_opencode_runtime_from_persisted_handle(
        self,
        tmp_path,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        sample_seed: Seed,
    ) -> None:
        """Interrupted OpenCode runs should resume from the stored runtime handle."""

        class _FakeStream:
            def __init__(self, text: str = "") -> None:
                self._buffer = text.encode("utf-8")
                self._drained = False

            async def read(self, _chunk_size: int = 16384) -> bytes:
                if self._drained:
                    return b""
                self._drained = True
                return self._buffer

        class _FakeProcess:
            def __init__(self, stdout_text: str, *, returncode: int = 0) -> None:
                self.stdout = _FakeStream(stdout_text)
                self.stderr = _FakeStream("")
                self.stdin = None
                self._returncode = returncode

            async def wait(self) -> int:
                return self._returncode

        runtime = OpenCodeRuntime(  # noqa: F821
            cli_path="/tmp/opencode",
            permission_mode="acceptEdits",
            cwd=tmp_path,
        )
        runner = OrchestratorRunner(runtime, mock_event_store, mock_console)

        persisted_handle = RuntimeHandle(
            backend="opencode",
            kind="implementation_session",
            cwd=str(tmp_path),
            approval_mode="acceptEdits",
            updated_at="2026-03-13T00:00:00+00:00",
            metadata={
                "server_session_id": "server-42",
                "session_scope_id": "ac_0",
                "session_state_path": ("execution.acceptance_criteria.ac_0.implementation_session"),
                "session_role": "implementation",
                "retry_attempt": 0,
            },
        )
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.RUNNING
        )
        running_tracker = running_tracker.with_progress(
            {
                "runtime": persisted_handle.to_dict(),
                "runtime_backend": "opencode",
                "messages_processed": 4,
            }
        )

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        recorded_commands: list[tuple[str, ...]] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            recorded_commands.append(tuple(command))
            output_index = command.index("--output-last-message") + 1
            output_path = kwargs.get("cwd")
            assert output_path == str(tmp_path)
            from pathlib import Path

            Path(command[output_index]).write_text("Resume pass complete.", encoding="utf-8")
            stdout_text = (
                '{"type":"session.resumed","server_session_id":"server-42",'
                '"session":{"id":"oc-session-123"}}\n'
                '{"type":"assistant.message.delta","delta":{"text":"Reconnected to the'
                ' interrupted OpenCode session."}}\n'
            )
            return _FakeProcess(stdout_text)

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
            patch(
                "ouroboros.orchestrator.codex_cli_runtime.asyncio.create_subprocess_exec",
                side_effect=fake_create_subprocess_exec,
            ),
        ):
            result = await runner.resume_session("sess_resume", sample_seed)

        assert result.is_ok
        assert result.value.success is True
        assert recorded_commands
        assert recorded_commands[0][:2] == ("/tmp/opencode", "run")
        assert "--resume" in recorded_commands[0]
        assert recorded_commands[0][recorded_commands[0].index("--resume") + 1] == "server-42"
        progress_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "orchestrator.progress.updated"
        ]
        assert any(
            event.data.get("progress", {}).get("runtime", {}).get("native_session_id")
            == "oc-session-123"
            for event in progress_events
        )

    @pytest.mark.asyncio
    async def test_resume_session_replays_persisted_progress_into_workflow_state(
        self,
        runner: OrchestratorRunner,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        sample_seed: Seed,
    ) -> None:
        """Resume should rebuild workflow state from persisted progress before streaming."""
        runtime_handle = RuntimeHandle(backend="opencode", native_session_id="oc-session-123")
        running_tracker = SessionTracker.create("exec_resume", "seed_resume").with_status(
            SessionStatus.RUNNING
        )
        running_tracker = running_tracker.with_progress(
            {
                "runtime": runtime_handle.to_dict(),
                "messages_processed": 4,
            }
        )

        async def mock_reconstruct(*args: Any, **kwargs: Any):
            return Result.ok(running_tracker)

        async def mock_mark_completed(*args: Any, **kwargs: Any):
            return Result.ok(None)

        async def mock_execute(*args: Any, **kwargs: Any) -> AsyncIterator[AgentMessage]:
            yield AgentMessage(
                type="result",
                content="[TASK_COMPLETE]",
                data={"subtype": "success"},
                resume_handle=runtime_handle,
            )

        mock_adapter.execute_task = mock_execute
        mock_event_store.replay.return_value = [
            BaseEvent(
                type="orchestrator.progress.updated",
                aggregate_type="session",
                aggregate_id="sess_resume",
                data={
                    "message_type": "assistant",
                    "content_preview": "[AC_COMPLETE: 1] Finished the first criterion.",
                    "ac_tracking": {"started": [], "completed": [1]},
                    "progress": {
                        "last_message_type": "assistant",
                        "last_content_preview": "[AC_COMPLETE: 1] Finished the first criterion.",
                    },
                },
            )
        ]

        with (
            patch.object(runner._session_repo, "reconstruct_session", mock_reconstruct),
            patch.object(runner._session_repo, "mark_completed", mock_mark_completed),
        ):
            result = await runner.resume_session("sess_resume", sample_seed)

        assert result.is_ok
        workflow_events = [
            call.args[0]
            for call in mock_event_store.append.await_args_list
            if getattr(call.args[0], "type", None) == "workflow.progress.updated"
        ]
        assert workflow_events
        assert workflow_events[0].data["completed_count"] == 1
        assert workflow_events[0].data["current_ac_index"] == 2

    @pytest.mark.asyncio
    async def test_execute_parallel_passes_staged_execution_plan(
        self,
        runner: OrchestratorRunner,
        sample_seed: Seed,
    ) -> None:
        """Parallel execution should pass a staged plan into the executor."""
        from ouroboros.orchestrator.mcp_tools import assemble_session_tool_catalog

        tracker = SessionTracker.create("exec_parallel", sample_seed.metadata.seed_id)
        dependency_graph = DependencyGraph(
            nodes=(
                ACNode(index=0, content=sample_seed.acceptance_criteria[0]),
                ACNode(index=1, content=sample_seed.acceptance_criteria[1]),
                ACNode(index=2, content=sample_seed.acceptance_criteria[2], depends_on=(0, 1)),
            ),
            execution_levels=((0, 1), (2,)),
        )
        parallel_result = ParallelExecutionResult(
            results=(
                ACExecutionResult(
                    ac_index=0,
                    ac_content=sample_seed.acceptance_criteria[0],
                    success=True,
                    final_message="done",
                ),
                ACExecutionResult(
                    ac_index=1,
                    ac_content=sample_seed.acceptance_criteria[1],
                    success=True,
                    final_message="done",
                ),
                ACExecutionResult(
                    ac_index=2,
                    ac_content=sample_seed.acceptance_criteria[2],
                    success=True,
                    final_message="done",
                ),
            ),
            success_count=3,
            failure_count=0,
            total_messages=3,
        )

        with (
            patch(
                "ouroboros.orchestrator.dependency_analyzer.DependencyAnalyzer.analyze",
                AsyncMock(return_value=Result.ok(dependency_graph)),
            ),
            patch.object(runner, "_check_cancellation", AsyncMock(return_value=False)),
            patch.object(
                runner._session_repo,
                "mark_completed",
                AsyncMock(return_value=Result.ok(None)),
            ),
            patch(
                "ouroboros.orchestrator.parallel_executor.ParallelACExecutor.execute_parallel",
                AsyncMock(return_value=parallel_result),
            ) as mock_execute_parallel,
        ):
            result = await runner._execute_parallel(
                seed=sample_seed,
                exec_id="exec_parallel",
                tracker=tracker,
                merged_tools=["Read"],
                tool_catalog=assemble_session_tool_catalog(["Read"]),
                system_prompt="system",
                start_time=tracker.start_time,
            )

        assert result.is_ok
        kwargs = mock_execute_parallel.await_args.kwargs
        execution_plan = kwargs["execution_plan"]
        assert execution_plan.execution_levels == dependency_graph.execution_levels
        assert execution_plan.total_stages == 2
        assert kwargs["session_id"] == tracker.session_id


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
        adapter.runtime_backend = "opencode"
        adapter.working_directory = "/tmp/project"
        adapter.permission_mode = "acceptEdits"
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

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        assert merged_tools == DEFAULT_TOOLS
        assert provider is None
        assert [tool.name for tool in tool_catalog.tools] == DEFAULT_TOOLS

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

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        # Should include DEFAULT_TOOLS + MCP tools
        assert all(t in merged_tools for t in DEFAULT_TOOLS)
        assert "external_tool" in merged_tools
        assert provider is not None
        assert tool_catalog.attached_tools[0].name == "external_tool"

    @pytest.mark.asyncio
    async def test_get_merged_tools_uses_deterministic_session_catalog_order(
        self,
        mock_adapter: MagicMock,
        mock_event_store: AsyncMock,
        mock_console: MagicMock,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Merged tool order should come from the normalized session catalog."""
        from ouroboros.mcp.types import MCPToolDefinition

        class _Strategy:
            def get_tools(self) -> list[str]:
                return ["Write", "Read"]

        mock_mcp_manager.list_all_tools = AsyncMock(
            return_value=[
                MCPToolDefinition(
                    name="search",
                    description="Search from server-b",
                    server_name="server-b",
                ),
                MCPToolDefinition(
                    name="Read",
                    description="Conflicting read tool",
                    server_name="server-shadow",
                ),
                MCPToolDefinition(
                    name="alpha",
                    description="Alpha tool",
                    server_name="server-a",
                ),
                MCPToolDefinition(
                    name="search",
                    description="Search from server-a",
                    server_name="server-a",
                ),
            ]
        )

        runner = OrchestratorRunner(
            mock_adapter,
            mock_event_store,
            mock_console,
            mcp_manager=mock_mcp_manager,
        )

        merged_tools, provider, tool_catalog = await runner._get_merged_tools(
            "session_123",
            strategy=_Strategy(),
        )

        assert merged_tools == ["Write", "Read", "alpha", "search"]
        assert provider is not None
        assert [tool.name for tool in provider.session_catalog.tools] == merged_tools
        assert [tool.name for tool in tool_catalog.tools] == merged_tools

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

        merged_tools, provider, tool_catalog = await runner._get_merged_tools("session_123")

        # Should still return DEFAULT_TOOLS on failure
        assert merged_tools == DEFAULT_TOOLS
        # Provider is still returned (error is handled gracefully inside MCPToolProvider)
        # This allows callers to retry or check provider state
        assert provider is not None
        # No MCP tools should have been added
        assert len(merged_tools) == len(DEFAULT_TOOLS)
        assert tool_catalog.attached_tools == ()

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
        adapter = MagicMock()
        adapter.runtime_backend = "opencode"
        adapter.working_directory = "/tmp/project"
        adapter.permission_mode = "acceptEdits"
        return adapter

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

        await request_cancellation("sess_inmem")
        try:
            # Should return True without even querying the event store
            result = await runner._check_cancellation("sess_inmem")
            assert result is True
            # Event store query should NOT have been called (fast path)
            mock_event_store.query_events.assert_not_called()
        finally:
            await clear_cancellation("sess_inmem")

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

        await request_cancellation("sess_clear")

        with patch.object(runner._session_repo, "mark_cancelled", AsyncMock(return_value=None)):
            await runner._handle_cancellation(
                session_id="sess_clear",
                execution_id="exec_clear",
                messages_processed=5,
                start_time=datetime.now(UTC),
            )

        assert await is_cancellation_requested("sess_clear") is False


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

    @pytest.mark.asyncio
    async def test_request_cancellation_adds_session(self) -> None:
        """Test that request_cancellation adds the session ID to the registry."""
        from ouroboros.orchestrator.runner import (
            is_cancellation_requested,
            request_cancellation,
        )

        assert await is_cancellation_requested("sess_1") is False
        await request_cancellation("sess_1")
        assert await is_cancellation_requested("sess_1") is True

    @pytest.mark.asyncio
    async def test_clear_cancellation_removes_session(self) -> None:
        """Test that clear_cancellation removes the session ID."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            is_cancellation_requested,
            request_cancellation,
        )

        await request_cancellation("sess_2")
        assert await is_cancellation_requested("sess_2") is True
        await clear_cancellation("sess_2")
        assert await is_cancellation_requested("sess_2") is False

    @pytest.mark.asyncio
    async def test_clear_cancellation_is_idempotent(self) -> None:
        """Test that clearing a non-existent session does not raise."""
        from ouroboros.orchestrator.runner import clear_cancellation

        # Should not raise
        await clear_cancellation("nonexistent_session")

    @pytest.mark.asyncio
    async def test_get_pending_cancellations_returns_frozenset(self) -> None:
        """Test that get_pending_cancellations returns a frozenset snapshot."""
        from ouroboros.orchestrator.runner import (
            get_pending_cancellations,
            request_cancellation,
        )

        await request_cancellation("sess_a")
        await request_cancellation("sess_b")

        pending = await get_pending_cancellations()
        assert isinstance(pending, frozenset)
        assert pending == frozenset({"sess_a", "sess_b"})

    @pytest.mark.asyncio
    async def test_get_pending_cancellations_is_snapshot(self) -> None:
        """Test that the returned frozenset is a snapshot, not a live view."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            get_pending_cancellations,
            request_cancellation,
        )

        await request_cancellation("sess_snap")
        snapshot = await get_pending_cancellations()
        await clear_cancellation("sess_snap")

        # Snapshot should still contain the session
        assert "sess_snap" in snapshot
        # But the registry should not
        new_snapshot = await get_pending_cancellations()
        assert "sess_snap" not in new_snapshot

    @pytest.mark.asyncio
    async def test_multiple_sessions_tracked_independently(self) -> None:
        """Test that multiple sessions can be tracked independently."""
        from ouroboros.orchestrator.runner import (
            clear_cancellation,
            is_cancellation_requested,
            request_cancellation,
        )

        await request_cancellation("sess_x")
        await request_cancellation("sess_y")

        assert await is_cancellation_requested("sess_x") is True
        assert await is_cancellation_requested("sess_y") is True

        await clear_cancellation("sess_x")
        assert await is_cancellation_requested("sess_x") is False
        assert await is_cancellation_requested("sess_y") is True

    @pytest.mark.asyncio
    async def test_request_cancellation_is_idempotent(self) -> None:
        """Test that requesting cancellation twice is safe."""
        from ouroboros.orchestrator.runner import (
            get_pending_cancellations,
            request_cancellation,
        )

        await request_cancellation("sess_dup")
        await request_cancellation("sess_dup")

        assert len(await get_pending_cancellations()) == 1


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
