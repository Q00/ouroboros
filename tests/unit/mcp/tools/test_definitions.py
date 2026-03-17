"""Tests for Ouroboros tool definitions."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import _is_interview_completion_signal
from ouroboros.mcp.tools.definitions import (
    OUROBOROS_TOOLS,
    CancelExecutionHandler,
    CancelJobHandler,
    EvaluateHandler,
    EvolveRewindHandler,
    EvolveStepHandler,
    ExecuteSeedHandler,
    GenerateSeedHandler,
    InterviewHandler,
    JobResultHandler,
    JobStatusHandler,
    JobWaitHandler,
    LateralThinkHandler,
    LineageStatusHandler,
    MeasureDriftHandler,
    QueryEventsHandler,
    SessionStatusHandler,
    StartEvolveStepHandler,
    StartExecuteSeedHandler,
    evaluate_handler,
    execute_seed_handler,
    generate_seed_handler,
    get_ouroboros_tools,
    interview_handler,
)
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.types import ToolInputType
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
    DELEGATED_PARENT_SESSION_ID_ARG,
)
from ouroboros.orchestrator.runner import OrchestratorResult
from ouroboros.orchestrator.session import SessionTracker


def create_mock_live_ambiguity_score(
    score: float,
    *,
    seed_ready: bool,
) -> MagicMock:
    """Create a mock ambiguity score object for interview handler tests."""
    return MagicMock(
        overall_score=score,
        is_ready_for_seed=seed_ready,
        breakdown=MagicMock(
            model_dump=MagicMock(
                return_value={
                    "goal_clarity": {
                        "name": "Goal Clarity",
                        "clarity_score": 1.0 - score,
                        "weight": 0.4,
                        "justification": "Mock goal clarity",
                    },
                    "constraint_clarity": {
                        "name": "Constraint Clarity",
                        "clarity_score": 1.0 - score,
                        "weight": 0.3,
                        "justification": "Mock constraint clarity",
                    },
                    "success_criteria_clarity": {
                        "name": "Success Criteria Clarity",
                        "clarity_score": 1.0 - score,
                        "weight": 0.3,
                        "justification": "Mock success clarity",
                    },
                }
            )
        ),
    )


class TestExecuteSeedHandler:
    """Test ExecuteSeedHandler class."""

    def test_definition_name(self) -> None:
        """ExecuteSeedHandler has correct name."""
        handler = ExecuteSeedHandler()
        assert handler.definition.name == "ouroboros_execute_seed"

    def test_definition_accepts_seed_content_or_seed_path(self) -> None:
        """ExecuteSeedHandler accepts either inline content or a seed path."""
        handler = ExecuteSeedHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "seed_content" in param_names
        assert "seed_path" in param_names

        seed_param = next(p for p in defn.parameters if p.name == "seed_content")
        assert seed_param.required is False
        assert seed_param.type == ToolInputType.STRING

        seed_path_param = next(p for p in defn.parameters if p.name == "seed_path")
        assert seed_path_param.required is False
        assert seed_path_param.type == ToolInputType.STRING

    def test_definition_has_optional_parameters(self) -> None:
        """ExecuteSeedHandler has optional parameters."""
        handler = ExecuteSeedHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "cwd" in param_names
        assert "session_id" in param_names
        assert "model_tier" in param_names
        assert "max_iterations" in param_names

    def test_definition_excludes_internal_delegation_parameters(self) -> None:
        """Internal parent-session propagation must not change the public tool schema."""
        handler = ExecuteSeedHandler()
        param_names = {p.name for p in handler.definition.parameters}

        assert DELEGATED_PARENT_SESSION_ID_ARG not in param_names
        assert DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG not in param_names

    async def test_handle_requires_seed_content_or_seed_path(self) -> None:
        """handle returns error when neither seed_content nor seed_path is provided."""
        handler = ExecuteSeedHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "seed_content or seed_path is required" in str(result.error)

    def test_execute_seed_handler_factory_accepts_runtime_backend(self) -> None:
        """Factory helper preserves explicit runtime backend selection."""
        handler = execute_seed_handler(runtime_backend="codex")
        assert handler.agent_runtime_backend == "codex"

    def test_execute_seed_handler_factory_accepts_llm_backend(self) -> None:
        """Factory helper preserves explicit llm backend selection."""
        handler = execute_seed_handler(runtime_backend="opencode", llm_backend="opencode")
        assert handler.agent_runtime_backend == "opencode"
        assert handler.llm_backend == "opencode"

    async def test_handle_uses_runtime_factory_defaults(self) -> None:
        """ExecuteSeed relies on runtime factory defaults instead of hardcoded permissions."""
        handler = ExecuteSeedHandler()
        mock_runtime = MagicMock()
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_runner = MagicMock()
        mock_runner.prepare_session = AsyncMock(
            return_value=Result.err(RuntimeError("execution failed"))
        )
        mock_runner.execute_precreated_session = AsyncMock()
        mock_runner.resume_session = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                return_value=mock_runtime,
            ) as mock_create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.EventStore",
                return_value=mock_event_store,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.OrchestratorRunner",
                return_value=mock_runner,
            ),
        ):
            await handler.handle({"seed_content": VALID_SEED_YAML})
            background_tasks = tuple(handler._background_tasks)
            await asyncio.gather(*background_tasks)

        assert mock_create_runtime.call_args.kwargs["backend"] is None
        assert mock_create_runtime.call_args.kwargs["llm_backend"] is None
        assert "permission_mode" not in mock_create_runtime.call_args.kwargs

    async def test_handle_forwards_llm_backend_to_runtime_factory(self) -> None:
        """ExecuteSeed forwards explicit llm backend selection into the runtime factory."""
        handler = ExecuteSeedHandler(
            agent_runtime_backend="opencode",
            llm_backend="opencode",
        )
        mock_runtime = MagicMock()
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_runner = MagicMock()
        mock_runner.prepare_session = AsyncMock(
            return_value=Result.err(RuntimeError("execution failed"))
        )
        mock_runner.execute_precreated_session = AsyncMock()
        mock_runner.resume_session = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                return_value=mock_runtime,
            ) as mock_create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.EventStore",
                return_value=mock_event_store,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.OrchestratorRunner",
                return_value=mock_runner,
            ),
        ):
            await handler.handle({"seed_content": VALID_SEED_YAML})
            background_tasks = tuple(handler._background_tasks)
            await asyncio.gather(*background_tasks)

        assert mock_create_runtime.call_args.kwargs["backend"] == "opencode"
        assert mock_create_runtime.call_args.kwargs["llm_backend"] == "opencode"

    async def test_handle_resolves_relative_seed_path_against_cwd(self, tmp_path: Path) -> None:
        """Relative seed paths from `ooo run` resolve against the intercepted working directory."""
        handler = ExecuteSeedHandler()
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text(VALID_SEED_YAML, encoding="utf-8")

        mock_runtime = MagicMock()
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_runner = MagicMock()
        mock_runner.prepare_session = AsyncMock(
            return_value=Result.err(RuntimeError("execution failed"))
        )
        mock_runner.execute_precreated_session = AsyncMock()
        mock_runner.resume_session = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                return_value=mock_runtime,
            ) as mock_create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.EventStore",
                return_value=mock_event_store,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.OrchestratorRunner",
                return_value=mock_runner,
            ),
        ):
            await handler.handle({"seed_path": "seed.yaml", "cwd": str(tmp_path)})
            background_tasks = tuple(handler._background_tasks)
            await asyncio.gather(*background_tasks)

        assert mock_create_runtime.call_args.kwargs["cwd"] == tmp_path
        called_seed = mock_runner.prepare_session.await_args.args[0]
        assert called_seed.goal == "Test task"

    async def test_handle_success(self) -> None:
        """handle returns an immediate launched response with valid YAML seed input."""
        handler = ExecuteSeedHandler()
        mock_runtime = MagicMock()
        mock_runtime._runtime_backend = "codex"
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_exec_result = MagicMock(
            success=True,
            session_id="sess-success",
            execution_id="exec-success",
            messages_processed=1,
            duration_seconds=0.2,
            final_message="[TASK_COMPLETE]",
            summary={},
        )
        mock_runner = MagicMock()
        prepared_tracker = SessionTracker.create(
            "exec-success",
            "test-seed-123",
            session_id="sess-success",
        )
        mock_runner.prepare_session = AsyncMock(return_value=Result.ok(prepared_tracker))
        mock_runner.execute_precreated_session = AsyncMock(return_value=Result.ok(mock_exec_result))
        mock_runner.resume_session = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                return_value=mock_runtime,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.EventStore",
                return_value=mock_event_store,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.OrchestratorRunner",
                return_value=mock_runner,
            ),
        ):
            result = await handler.handle(
                {
                    "seed_content": VALID_SEED_YAML,
                    "model_tier": "medium",
                    "skip_qa": True,
                }
            )
            background_tasks = tuple(handler._background_tasks)
            await asyncio.gather(*background_tasks)

        assert result.is_ok
        assert "Seed Execution LAUNCHED" in result.value.text_content
        assert "Session ID: sess-success" in result.value.text_content
        assert "Execution ID: exec-success" in result.value.text_content
        assert result.value.meta["seed_id"] == "test-seed-123"
        assert result.value.meta["session_id"] == "sess-success"
        assert result.value.meta["execution_id"] == "exec-success"
        assert result.value.meta["status"] == "running"

    async def test_handle_reads_seed_from_seed_path(self, tmp_path: Path) -> None:
        """handle loads seed YAML from seed_path and launches execution in the background."""
        seed_file = tmp_path / "seed.yaml"
        seed_file.write_text(VALID_SEED_YAML, encoding="utf-8")

        handler = ExecuteSeedHandler()
        mock_runtime = MagicMock()
        mock_runtime._runtime_backend = "codex"
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_exec_result = MagicMock(
            success=True,
            session_id="sess-123",
            execution_id="exec-456",
            messages_processed=4,
            duration_seconds=1.2,
            final_message="Execution finished",
            summary={},
        )
        mock_runner = MagicMock()
        prepared_tracker = SessionTracker.create(
            "exec-456",
            "test-seed-123",
            session_id="sess-123",
        )
        mock_runner.prepare_session = AsyncMock(return_value=Result.ok(prepared_tracker))
        mock_runner.execute_precreated_session = AsyncMock(return_value=Result.ok(mock_exec_result))
        mock_runner.resume_session = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                return_value=mock_runtime,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.EventStore",
                return_value=mock_event_store,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.OrchestratorRunner",
                return_value=mock_runner,
            ),
        ):
            result = await handler.handle(
                {"seed_path": str(seed_file), "cwd": str(tmp_path), "skip_qa": True}
            )
            background_tasks = tuple(handler._background_tasks)
            await asyncio.gather(*background_tasks)

        assert result.is_ok
        mock_runner.prepare_session.assert_awaited_once()
        mock_runner.execute_precreated_session.assert_awaited_once()
        assert "Seed Execution LAUNCHED" in result.value.text_content
        assert "Session ID: sess-123" in result.value.text_content
        assert "Execution ID: exec-456" in result.value.text_content
        assert "Runtime Backend:" in result.value.text_content
        assert result.value.meta["seed_id"] == "test-seed-123"
        assert result.value.meta["session_id"] == "sess-123"
        assert result.value.meta["execution_id"] == "exec-456"
        assert result.value.meta["launched"] is True
        assert result.value.meta["status"] == "running"
        assert result.value.meta["runtime_backend"] in ("claude", "codex")
        assert result.value.meta["resume_requested"] is False

    async def test_handle_launches_background_execution_with_opencode_runtime(self) -> None:
        """OpenCode selections should launch the existing orchestrator pipeline in background."""
        handler = ExecuteSeedHandler(
            agent_runtime_backend="opencode",
            llm_backend="opencode",
        )
        mock_runtime = MagicMock()
        mock_runtime._runtime_backend = "opencode"
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_exec_result = MagicMock(
            success=True,
            session_id="sess-opencode",
            execution_id="exec-opencode",
            messages_processed=6,
            duration_seconds=1.4,
            final_message="[TASK_COMPLETE]",
            summary={},
        )
        mock_runner = MagicMock()
        prepared_tracker = SessionTracker.create(
            "exec-opencode",
            "test-seed-123",
            session_id="sess-opencode",
        )
        mock_runner.prepare_session = AsyncMock(return_value=Result.ok(prepared_tracker))
        mock_runner.execute_precreated_session = AsyncMock(return_value=Result.ok(mock_exec_result))
        mock_runner.resume_session = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                return_value=mock_runtime,
            ) as mock_create_runtime,
            patch(
                "ouroboros.mcp.tools.execution_handlers.EventStore",
                return_value=mock_event_store,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.OrchestratorRunner",
                return_value=mock_runner,
            ),
        ):
            result = await handler.handle({"seed_content": VALID_SEED_YAML, "skip_qa": True})
            background_tasks = tuple(handler._background_tasks)
            await asyncio.gather(*background_tasks)

        assert result.is_ok
        assert "Runtime Backend: opencode" in result.value.text_content
        assert result.value.meta["runtime_backend"] == "opencode"
        assert result.value.meta["llm_backend"] == "opencode"
        assert result.value.meta["resume_requested"] is False
        assert result.value.meta["session_id"] == "sess-opencode"
        assert result.value.meta["execution_id"] == "exec-opencode"
        assert mock_create_runtime.call_args.kwargs["backend"] == "opencode"
        assert mock_create_runtime.call_args.kwargs["llm_backend"] == "opencode"
        mock_runner.prepare_session.assert_awaited_once()
        mock_runner.execute_precreated_session.assert_awaited_once()
        assert mock_runner.execute_precreated_session.await_args.kwargs["parallel"] is True
        mock_runner.resume_session.assert_not_awaited()

    async def test_handle_launches_background_resume_for_existing_session(self) -> None:
        """Resuming through MCP should reuse the current orchestrator resume path."""
        handler = ExecuteSeedHandler(
            agent_runtime_backend="opencode",
            llm_backend="opencode",
        )
        mock_runtime = MagicMock()
        mock_runtime._runtime_backend = "opencode"
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_exec_result = MagicMock(
            success=True,
            session_id="sess-resume",
            execution_id="exec-resume",
            messages_processed=8,
            duration_seconds=1.8,
            final_message="[TASK_COMPLETE]",
            summary={},
        )
        mock_runner = MagicMock()
        mock_runner.prepare_session = AsyncMock()
        mock_runner.execute_precreated_session = AsyncMock()
        mock_runner.resume_session = AsyncMock(return_value=Result.ok(mock_exec_result))
        resumed_tracker = SessionTracker.create(
            "exec-resume",
            "test-seed-123",
            session_id="sess-resume",
        )

        with (
            patch(
                "ouroboros.mcp.tools.execution_handlers.create_agent_runtime",
                return_value=mock_runtime,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.EventStore",
                return_value=mock_event_store,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.OrchestratorRunner",
                return_value=mock_runner,
            ),
            patch(
                "ouroboros.mcp.tools.execution_handlers.SessionRepository.reconstruct_session",
                new=AsyncMock(return_value=Result.ok(resumed_tracker)),
            ),
        ):
            result = await handler.handle(
                {
                    "seed_content": VALID_SEED_YAML,
                    "session_id": "sess-resume",
                    "skip_qa": True,
                }
            )
            background_tasks = tuple(handler._background_tasks)
            await asyncio.gather(*background_tasks)

        assert result.is_ok
        assert result.value.meta["resume_requested"] is True
        assert result.value.meta["runtime_backend"] == "opencode"
        assert result.value.meta["session_id"] == "sess-resume"
        assert result.value.meta["execution_id"] == "exec-resume"
        mock_runner.resume_session.assert_awaited_once()
        assert mock_runner.resume_session.await_args.args[0] == "sess-resume"
        mock_runner.prepare_session.assert_not_awaited()
        mock_runner.execute_precreated_session.assert_not_awaited()

    async def test_handle_passes_inherited_parent_context_to_runner(self) -> None:
        """New delegated executions should receive inherited runtime and effective tools."""
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()
        mock_runner = MagicMock()
        mock_runner.execute_seed = AsyncMock(
            return_value=Result.ok(
                OrchestratorResult(
                    success=True,
                    session_id="orch_child",
                    execution_id="exec_child",
                    final_message="Done",
                )
            )
        )

        with (
            patch("ouroboros.mcp.tools.definitions.EventStore", return_value=mock_event_store),
            patch("ouroboros.mcp.tools.definitions.ClaudeAgentAdapter") as adapter_cls,
            patch(
                "ouroboros.mcp.tools.definitions.OrchestratorRunner",
                return_value=mock_runner,
            ) as runner_cls,
        ):
            handler = ExecuteSeedHandler()
            result = await handler.handle(
                {
                    "seed_content": VALID_SEED_YAML,
                    DELEGATED_PARENT_SESSION_ID_ARG: "sess_parent",
                    "model_tier": "medium",
                    "skip_qa": True,
                    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG: [
                        "Read",
                        "mcp__chrome-devtools__click",
                    ],
                    DELEGATED_PARENT_PERMISSION_MODE_ARG: "bypassPermissions",
                },
                execution_id="exec_child",
                session_id_override="orch_child",
            )

        assert result.is_ok
        adapter_cls.assert_called_once_with(permission_mode="bypassPermissions")
        runner_kwargs = runner_cls.call_args.kwargs
        inherited_handle = runner_kwargs["inherited_runtime_handle"]
        assert inherited_handle is not None
        assert inherited_handle.native_session_id == "sess_parent"
        assert inherited_handle.approval_mode == "bypassPermissions"
        assert inherited_handle.metadata["fork_session"] is True
        assert runner_kwargs["inherited_tools"] == ["Read", "mcp__chrome-devtools__click"]


class TestSessionStatusHandler:
    """Test SessionStatusHandler class."""

    def test_definition_name(self) -> None:
        """SessionStatusHandler has correct name."""
        handler = SessionStatusHandler()
        assert handler.definition.name == "ouroboros_session_status"

    def test_definition_requires_session_id(self) -> None:
        """SessionStatusHandler requires session_id parameter."""
        handler = SessionStatusHandler()
        defn = handler.definition

        assert len(defn.parameters) == 1
        assert defn.parameters[0].name == "session_id"
        assert defn.parameters[0].required is True

    async def test_handle_requires_session_id(self) -> None:
        """handle returns error when session_id is missing."""
        handler = SessionStatusHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "session_id is required" in str(result.error)

    async def test_handle_success(self) -> None:
        """handle returns session status or not found error."""
        handler = SessionStatusHandler()
        result = await handler.handle({"session_id": "test-session"})

        # Handler now queries actual event store, so non-existent sessions return error
        # This is expected behavior - the handler correctly reports "session not found"
        if result.is_ok:
            # If session exists, verify it contains session info
            assert (
                "test-session" in result.value.text_content
                or "session" in result.value.text_content.lower()
            )
        else:
            # If session doesn't exist (expected for test data), verify proper error
            assert (
                "not found" in str(result.error).lower() or "no events" in str(result.error).lower()
            )

    @pytest.mark.parametrize(
        "status_value,expected_terminal",
        [
            ("running", "False"),
            ("paused", "False"),
            ("completed", "True"),
            ("failed", "True"),
            ("cancelled", "True"),
        ],
    )
    async def test_terminal_line_matches_status(
        self, status_value: str, expected_terminal: str
    ) -> None:
        """Terminal line in text output accurately reflects session status.

        Prevents false-positive detection where callers match 'completed'
        against the entire text body instead of a structured field.
        """
        from ouroboros.orchestrator.session import SessionRepository, SessionStatus, SessionTracker

        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()

        handler = SessionStatusHandler(event_store=mock_event_store)
        handler._initialized = True

        mock_tracker = MagicMock(spec=SessionTracker)
        mock_tracker.session_id = "sess-terminal-test"
        mock_tracker.status = SessionStatus(status_value)
        mock_tracker.execution_id = "exec-1"
        mock_tracker.seed_id = "seed-1"
        mock_tracker.messages_processed = 5
        mock_tracker.start_time = MagicMock(isoformat=MagicMock(return_value="2026-01-01T00:00:00"))
        mock_tracker.last_message_time = None
        mock_tracker.progress = {}
        mock_tracker.is_active = status_value in ("running", "paused")
        mock_tracker.is_completed = status_value == "completed"
        mock_tracker.is_failed = status_value == "failed"

        mock_repo = AsyncMock(spec=SessionRepository)
        mock_repo.reconstruct_session = AsyncMock(
            return_value=MagicMock(is_ok=True, is_err=False, value=mock_tracker)
        )
        handler._session_repo = mock_repo

        result = await handler.handle({"session_id": "sess-terminal-test"})

        assert result.is_ok
        text = result.value.text_content

        # Parse the Terminal line specifically
        terminal_line = [line for line in text.split("\n") if line.startswith("Terminal:")]
        assert len(terminal_line) == 1, f"Expected exactly one Terminal: line, got: {terminal_line}"
        assert terminal_line[0] == f"Terminal: {expected_terminal}"

        # Also verify Status line
        status_line = [line for line in text.split("\n") if line.startswith("Status:")]
        assert len(status_line) == 1
        assert status_line[0] == f"Status: {status_value}"

        # Verify meta dict
        assert result.value.meta["status"] == status_value
        assert result.value.meta["is_completed"] == (status_value == "completed")
        assert result.value.meta["is_failed"] == (status_value == "failed")


class TestQueryEventsHandler:
    """Test QueryEventsHandler class."""

    def test_definition_name(self) -> None:
        """QueryEventsHandler has correct name."""
        handler = QueryEventsHandler()
        assert handler.definition.name == "ouroboros_query_events"

    def test_definition_has_optional_filters(self) -> None:
        """QueryEventsHandler has optional filter parameters."""
        handler = QueryEventsHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "session_id" in param_names
        assert "event_type" in param_names
        assert "limit" in param_names
        assert "offset" in param_names

        # All should be optional
        for param in defn.parameters:
            assert param.required is False

    async def test_handle_success_no_filters(self) -> None:
        """handle returns success without filters."""
        handler = QueryEventsHandler()
        result = await handler.handle({})

        assert result.is_ok
        assert "Event Query Results" in result.value.text_content

    async def test_handle_with_filters(self) -> None:
        """handle accepts filter parameters."""
        handler = QueryEventsHandler()
        result = await handler.handle(
            {
                "session_id": "test-session",
                "event_type": "execution",
                "limit": 10,
            }
        )

        assert result.is_ok
        assert "test-session" in result.value.text_content

    async def test_handle_with_session_id_includes_related_parallel_execution_events(self) -> None:
        """session_id queries should include execution and child AC aggregates."""
        from ouroboros.events.base import BaseEvent
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        await event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="orch_parallel_123",
                data={
                    "execution_id": "exec_parallel_123",
                    "seed_id": "seed_parallel_123",
                    "start_time": "2026-03-13T09:00:00+00:00",
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_parallel_123",
                data={
                    "session_id": "orch_parallel_123",
                    "completed_count": 1,
                    "total_count": 3,
                    "messages_count": 5,
                    "tool_calls_count": 2,
                    "acceptance_criteria": [],
                },
            )
        )
        await event_store.append(
            BaseEvent(
                type="execution.session.started",
                aggregate_type="execution",
                aggregate_id="exec_parallel_123_sub_ac_0_0",
                data={
                    "session_id": "native-codex-session",
                    "session_scope_id": "exec_parallel_123_sub_ac_0_0",
                },
            )
        )

        handler = QueryEventsHandler(event_store=event_store)
        result = await handler.handle({"session_id": "orch_parallel_123", "limit": 20})

        assert result.is_ok
        text = result.value.text_content
        assert "workflow.progress.updated" in text
        assert "execution.session.started" in text
        assert "exec_parallel_123_sub_ac_0_0" in text


class TestOuroborosTools:
    """Test OUROBOROS_TOOLS constant."""

    def test_ouroboros_tools_contains_all_handlers(self) -> None:
        """OUROBOROS_TOOLS contains all standard handlers."""
        assert len(OUROBOROS_TOOLS) == 19

        handler_types = {type(h) for h in OUROBOROS_TOOLS}
        assert ExecuteSeedHandler in handler_types
        assert StartExecuteSeedHandler in handler_types
        assert SessionStatusHandler in handler_types
        assert JobStatusHandler in handler_types
        assert JobWaitHandler in handler_types
        assert JobResultHandler in handler_types
        assert CancelJobHandler in handler_types
        assert QueryEventsHandler in handler_types
        assert GenerateSeedHandler in handler_types
        assert MeasureDriftHandler in handler_types
        assert InterviewHandler in handler_types
        assert EvaluateHandler in handler_types
        assert LateralThinkHandler in handler_types
        assert EvolveStepHandler in handler_types
        assert StartEvolveStepHandler in handler_types
        assert LineageStatusHandler in handler_types
        assert EvolveRewindHandler in handler_types
        assert CancelExecutionHandler in handler_types

    def test_all_tools_have_unique_names(self) -> None:
        """All tools have unique names."""
        names = [h.definition.name for h in OUROBOROS_TOOLS]
        assert len(names) == len(set(names))

    def test_all_tools_have_descriptions(self) -> None:
        """All tools have non-empty descriptions."""
        for handler in OUROBOROS_TOOLS:
            assert handler.definition.description
            assert len(handler.definition.description) > 10

    def test_get_ouroboros_tools_can_inject_runtime_backend(self) -> None:
        """Tool factory can build execute_seed with a specific runtime backend."""
        tools = get_ouroboros_tools(runtime_backend="codex")
        assert len(tools) == 19
        execute_handler = next(h for h in tools if isinstance(h, ExecuteSeedHandler))
        assert execute_handler.agent_runtime_backend == "codex"

    def test_get_ouroboros_tools_can_inject_llm_backend(self) -> None:
        """Tool factory propagates llm backend to LLM-only handlers."""
        tools = get_ouroboros_tools(runtime_backend="codex", llm_backend="litellm")
        execute_handler = next(h for h in tools if isinstance(h, ExecuteSeedHandler))
        generate_handler = next(h for h in tools if isinstance(h, GenerateSeedHandler))
        interview_handler_instance = next(h for h in tools if isinstance(h, InterviewHandler))
        evaluate_handler_instance = next(h for h in tools if isinstance(h, EvaluateHandler))
        qa_handler = next(h for h in tools if isinstance(h, QAHandler))

        assert execute_handler.agent_runtime_backend == "codex"
        assert execute_handler.llm_backend == "litellm"
        assert generate_handler.llm_backend == "litellm"
        assert interview_handler_instance.llm_backend == "litellm"
        assert evaluate_handler_instance.llm_backend == "litellm"
        assert qa_handler.llm_backend == "litellm"

    def test_llm_handler_factories_preserve_backend_selection(self) -> None:
        """Convenience factories preserve explicit llm backend selection."""
        assert generate_seed_handler(llm_backend="litellm").llm_backend == "litellm"
        assert interview_handler(llm_backend="litellm").llm_backend == "litellm"
        assert evaluate_handler(llm_backend="litellm").llm_backend == "litellm"

    async def test_interview_handler_uses_interview_use_case(self) -> None:
        """Interview fallback requests the interview-specific permission policy."""
        handler = InterviewHandler(llm_backend="codex")
        mock_adapter = MagicMock()
        mock_engine = MagicMock()
        mock_start = AsyncMock()
        mock_start.return_value.is_err = True
        mock_start.return_value.error.message = "failed"
        mock_engine.start_interview = mock_start
        mock_engine.load_state = AsyncMock()
        mock_engine.record_response = AsyncMock()
        mock_engine.complete_interview = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=mock_adapter,
            ) as mock_create_adapter,
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
        ):
            await handler.handle({"initial_context": "Build a tool"})

        assert mock_create_adapter.call_args.kwargs["backend"] == "codex"
        assert mock_create_adapter.call_args.kwargs["use_case"] == "interview"

    async def test_generate_seed_handler_passes_llm_backend_to_model_lookup(self) -> None:
        """GenerateSeedHandler should resolve model defaults with the active LLM backend."""
        handler = GenerateSeedHandler(llm_backend="codex")
        mock_adapter = MagicMock()
        mock_interview_engine = MagicMock()
        mock_interview_engine.load_state = AsyncMock(return_value=Result.ok(MagicMock()))
        mock_seed_generator = MagicMock()
        mock_seed_generator.generate = AsyncMock(return_value=Result.err(RuntimeError("boom")))

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_interview_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.SeedGenerator",
                return_value=mock_seed_generator,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.get_clarification_model",
                return_value="default",
            ) as mock_get_model,
        ):
            await handler.handle({"session_id": "sess-123", "ambiguity_score": 0.1})

        assert mock_get_model.call_args_list == [call("codex"), call("codex")]

    async def test_evaluate_handler_passes_llm_backend_to_semantic_model_lookup(self) -> None:
        """EvaluateHandler should derive semantic model defaults from the active backend."""
        handler = EvaluateHandler(llm_backend="codex")
        mock_adapter = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.evaluate = AsyncMock(return_value=Result.err(RuntimeError("semantic failed")))
        seed_content = """\
goal: Test task
constraints: []
acceptance_criteria:
  - Pass
ontology_schema:
  name: Test
  description: Test
  fields: []
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: seed-123
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.create_llm_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.get_semantic_model",
                return_value="default",
            ) as mock_get_model,
            patch(
                "ouroboros.evaluation.build_mechanical_config",
                return_value=MagicMock(),
            ),
            patch(
                "ouroboros.evaluation.EvaluationPipeline",
                return_value=mock_pipeline,
            ),
        ):
            await handler.handle(
                {
                    "session_id": "sess-123",
                    "artifact": "print('hi')",
                    "artifact_type": "code",
                    "seed_content": seed_content,
                }
            )

        mock_get_model.assert_called_once_with("codex")

    async def test_qa_handler_passes_llm_backend_to_qa_model_lookup(self) -> None:
        """QAHandler should derive QA model defaults from the active backend."""
        handler = QAHandler(llm_backend="codex")
        mock_adapter = MagicMock()
        mock_adapter.complete = AsyncMock(return_value=Result.err(RuntimeError("llm failed")))

        with (
            patch(
                "ouroboros.mcp.tools.qa.create_llm_adapter",
                return_value=mock_adapter,
            ),
            patch(
                "ouroboros.mcp.tools.qa.get_qa_model",
                return_value="default",
            ) as mock_get_model,
            patch(
                "ouroboros.mcp.tools.qa._get_qa_system_prompt",
                return_value="judge",
            ),
        ):
            await handler.handle(
                {
                    "artifact": "print('hi')",
                    "quality_bar": "code should compile",
                    "artifact_type": "code",
                }
            )

        mock_get_model.assert_called_once_with("codex")


class TestAsyncJobHandlers:
    """Test async background job MCP handler definitions."""

    def test_start_execute_seed_definition_name(self) -> None:
        handler = StartExecuteSeedHandler()
        assert handler.definition.name == "ouroboros_start_execute_seed"

    def test_job_status_definition_name(self) -> None:
        handler = JobStatusHandler()
        assert handler.definition.name == "ouroboros_job_status"

    def test_job_wait_definition_has_expected_params(self) -> None:
        handler = JobWaitHandler()
        param_names = {p.name for p in handler.definition.parameters}
        assert param_names == {"job_id", "cursor", "timeout_seconds"}

    def test_job_result_definition_name(self) -> None:
        handler = JobResultHandler()
        assert handler.definition.name == "ouroboros_job_result"

    def test_cancel_job_definition_name(self) -> None:
        handler = CancelJobHandler()
        assert handler.definition.name == "ouroboros_cancel_job"

    def test_start_evolve_step_definition_name(self) -> None:
        handler = StartEvolveStepHandler()
        assert handler.definition.name == "ouroboros_start_evolve_step"


VALID_SEED_YAML = """\
goal: Test task
constraints:
  - Python 3.14+
acceptance_criteria:
  - Task completes successfully
ontology_schema:
  name: TestOntology
  description: Test ontology
  fields:
    - name: test_field
      field_type: string
      description: A test field
evaluation_principles: []
exit_conditions: []
metadata:
  seed_id: test-seed-123
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""


class TestMeasureDriftHandler:
    """Test MeasureDriftHandler class."""

    def test_definition_name(self) -> None:
        """MeasureDriftHandler has correct name."""
        handler = MeasureDriftHandler()
        assert handler.definition.name == "ouroboros_measure_drift"

    def test_definition_requires_session_id_and_output_and_seed(self) -> None:
        """MeasureDriftHandler requires session_id, current_output, seed_content."""
        handler = MeasureDriftHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "session_id" in param_names
        assert "current_output" in param_names
        assert "seed_content" in param_names

        for name in ("session_id", "current_output", "seed_content"):
            param = next(p for p in defn.parameters if p.name == name)
            assert param.required is True

    async def test_handle_requires_session_id(self) -> None:
        """handle returns error when session_id is missing."""
        handler = MeasureDriftHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "session_id is required" in str(result.error)

    async def test_handle_requires_current_output(self) -> None:
        """handle returns error when current_output is missing."""
        handler = MeasureDriftHandler()
        result = await handler.handle({"session_id": "test"})

        assert result.is_err
        assert "current_output is required" in str(result.error)

    async def test_handle_requires_seed_content(self) -> None:
        """handle returns error when seed_content is missing."""
        handler = MeasureDriftHandler()
        result = await handler.handle(
            {
                "session_id": "test",
                "current_output": "some output",
            }
        )

        assert result.is_err
        assert "seed_content is required" in str(result.error)

    async def test_handle_success_with_real_drift(self) -> None:
        """handle returns real drift metrics with valid inputs."""
        handler = MeasureDriftHandler()
        result = await handler.handle(
            {
                "session_id": "test-session",
                "current_output": "Built a test task with Python 3.14",
                "seed_content": VALID_SEED_YAML,
                "constraint_violations": [],
                "current_concepts": ["test_field"],
            }
        )

        assert result.is_ok
        text = result.value.text_content
        assert "Drift Measurement Report" in text
        assert "test-seed-123" in text

        meta = result.value.meta
        assert "goal_drift" in meta
        assert "constraint_drift" in meta
        assert "ontology_drift" in meta
        assert "combined_drift" in meta
        assert isinstance(meta["is_acceptable"], bool)

    async def test_handle_invalid_seed_yaml(self) -> None:
        """handle returns error for invalid seed YAML."""
        handler = MeasureDriftHandler()
        result = await handler.handle(
            {
                "session_id": "test",
                "current_output": "output",
                "seed_content": "not: valid: yaml: [[[",
            }
        )

        assert result.is_err


class TestEvaluateHandler:
    """Test EvaluateHandler class."""

    def test_definition_name(self) -> None:
        """EvaluateHandler has correct name."""
        handler = EvaluateHandler()
        assert handler.definition.name == "ouroboros_evaluate"

    def test_definition_requires_session_id_and_artifact(self) -> None:
        """EvaluateHandler requires session_id and artifact parameters."""
        handler = EvaluateHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "session_id" in param_names
        assert "artifact" in param_names

        session_param = next(p for p in defn.parameters if p.name == "session_id")
        assert session_param.required is True
        assert session_param.type == ToolInputType.STRING

        artifact_param = next(p for p in defn.parameters if p.name == "artifact")
        assert artifact_param.required is True
        assert artifact_param.type == ToolInputType.STRING

    def test_definition_has_optional_trigger_consensus(self) -> None:
        """EvaluateHandler has optional trigger_consensus parameter."""
        handler = EvaluateHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "trigger_consensus" in param_names
        assert "seed_content" in param_names
        assert "acceptance_criterion" in param_names

        trigger_param = next(p for p in defn.parameters if p.name == "trigger_consensus")
        assert trigger_param.required is False
        assert trigger_param.type == ToolInputType.BOOLEAN
        assert trigger_param.default is False

    async def test_handle_requires_session_id(self) -> None:
        """handle returns error when session_id is missing."""
        handler = EvaluateHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "session_id is required" in str(result.error)

    async def test_handle_requires_artifact(self) -> None:
        """handle returns error when artifact is missing."""
        handler = EvaluateHandler()
        result = await handler.handle({"session_id": "test-session"})

        assert result.is_err
        assert "artifact is required" in str(result.error)

    async def test_handle_success(self) -> None:
        """handle returns success with valid session_id and artifact."""
        from ouroboros.evaluation.models import (
            CheckResult,
            CheckType,
            EvaluationResult,
            MechanicalResult,
            SemanticResult,
        )

        # Create mock results with all required attributes
        mock_check = CheckResult(
            check_type=CheckType.TEST,
            passed=True,
            message="All tests passed",
        )
        mock_stage1 = MechanicalResult(
            passed=True,
            checks=(mock_check,),
            coverage_score=0.85,
        )
        mock_stage2 = SemanticResult(
            score=0.9,
            ac_compliance=True,
            goal_alignment=0.95,
            drift_score=0.1,
            uncertainty=0.2,
            reasoning="Artifact meets all acceptance criteria and aligns with goals.",
        )

        mock_eval_result = EvaluationResult(
            execution_id="test-session",
            stage1_result=mock_stage1,
            stage2_result=mock_stage2,
            stage3_result=None,
            final_approved=True,
        )

        # Create mock pipeline result
        mock_pipeline_result = MagicMock()
        mock_pipeline_result.is_err = False
        mock_pipeline_result.is_ok = True
        mock_pipeline_result.value = mock_eval_result

        # Mock EventStore to avoid real I/O
        mock_store = AsyncMock()
        mock_store.initialize = AsyncMock()

        with (
            patch("ouroboros.evaluation.EvaluationPipeline") as MockPipeline,
            patch("ouroboros.persistence.event_store.EventStore", return_value=mock_store),
        ):
            mock_pipeline_instance = AsyncMock()
            mock_pipeline_instance.evaluate = AsyncMock(return_value=mock_pipeline_result)
            MockPipeline.return_value = mock_pipeline_instance

            handler = EvaluateHandler()
            result = await handler.handle(
                {
                    "session_id": "test-session",
                    "artifact": "def hello(): return 'world'",
                    "trigger_consensus": False,
                }
            )

        assert result.is_ok
        assert "Evaluation Results" in result.value.text_content


class TestEvaluateHandlerCodeChanges:
    """Tests for code-change detection and contextual Stage 1 output."""

    def _make_handler(self):
        return EvaluateHandler()

    def _make_stage1(self, *, passed: bool):
        from ouroboros.evaluation.models import CheckResult, CheckType, MechanicalResult

        check = CheckResult(
            check_type=CheckType.TEST,
            passed=passed,
            message="tests passed" if passed else "tests failed",
        )
        return MechanicalResult(passed=passed, checks=(check,), coverage_score=None)

    def _make_eval_result(self, *, stage1_passed: bool, final_approved: bool):
        from ouroboros.evaluation.models import EvaluationResult

        return EvaluationResult(
            execution_id="test-session",
            stage1_result=self._make_stage1(passed=stage1_passed),
            stage2_result=None,
            stage3_result=None,
            final_approved=final_approved,
        )

    def test_format_result_stage1_fail_with_code_changes(self) -> None:
        """Stage 1 failure + code changes shows real-failure warning."""
        handler = self._make_handler()
        result = self._make_eval_result(stage1_passed=False, final_approved=False)
        text = handler._format_evaluation_result(result, code_changes=True)

        assert "real build/test failures" in text
        assert "No code changes detected" not in text

    def test_format_result_stage1_fail_no_code_changes(self) -> None:
        """Stage 1 failure + no code changes shows dry-check note."""
        handler = self._make_handler()
        result = self._make_eval_result(stage1_passed=False, final_approved=False)
        text = handler._format_evaluation_result(result, code_changes=False)

        assert "No code changes detected" in text
        assert "ooo run" in text
        assert "real build/test failures" not in text

    def test_format_result_stage1_fail_detection_none(self) -> None:
        """Stage 1 failure + None detection leaves output unchanged."""
        handler = self._make_handler()
        result = self._make_eval_result(stage1_passed=False, final_approved=False)
        text = handler._format_evaluation_result(result, code_changes=None)

        assert "real build/test failures" not in text
        assert "No code changes detected" not in text

    def test_format_result_stage1_pass_no_annotation(self) -> None:
        """Passing Stage 1 never shows annotation regardless of code_changes."""
        handler = self._make_handler()
        result = self._make_eval_result(stage1_passed=True, final_approved=True)
        text = handler._format_evaluation_result(result, code_changes=True)

        assert "real build/test failures" not in text
        assert "No code changes detected" not in text

    async def test_has_code_changes_true(self) -> None:
        """_has_code_changes returns True when git reports modifications."""
        handler = self._make_handler()
        from ouroboros.evaluation.mechanical import CommandResult

        mock_result = CommandResult(return_code=0, stdout=" M src/main.py\n", stderr="")
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_run:
            result = await handler._has_code_changes(Path("/fake"))

        assert result is True
        mock_run.assert_awaited_once()

    async def test_has_code_changes_false(self) -> None:
        """_has_code_changes returns False for a clean working tree."""
        handler = self._make_handler()
        from ouroboros.evaluation.mechanical import CommandResult

        mock_result = CommandResult(return_code=0, stdout="", stderr="")
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await handler._has_code_changes(Path("/fake"))

        assert result is False

    async def test_has_code_changes_not_git_repo(self) -> None:
        """_has_code_changes returns None when git fails (not a repo)."""
        handler = self._make_handler()
        from ouroboros.evaluation.mechanical import CommandResult

        mock_result = CommandResult(
            return_code=128, stdout="", stderr="fatal: not a git repository"
        )
        with patch(
            "ouroboros.evaluation.mechanical.run_command",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            result = await handler._has_code_changes(Path("/fake"))

        assert result is None


class TestInterviewHandlerCwd:
    """Test InterviewHandler cwd parameter."""

    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ("done", True),
            ("Yes. Close now.", True),
            ("Correct. No remaining ambiguity. Close the interview.", True),
            ("Yes. Lock it. Documentation-only outcomes. Done.", True),
            ("Not done yet.", False),
        ],
    )
    def test_interview_completion_signal_detection(self, answer: str, expected: bool) -> None:
        """Completion detection should accept natural closure phrases without over-triggering."""
        assert _is_interview_completion_signal(answer) is expected

    def test_interview_definition_has_cwd_param(self) -> None:
        """Interview tool definition includes the cwd parameter."""
        handler = InterviewHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "cwd" in param_names

        cwd_param = next(p for p in defn.parameters if p.name == "cwd")
        assert cwd_param.required is False
        assert cwd_param.type == ToolInputType.STRING

    async def test_interview_handle_passes_cwd(self, tmp_path) -> None:
        """handle passes cwd to engine.start_interview."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='demo'\n")

        mock_engine = MagicMock()
        mock_state = MagicMock()
        mock_state.interview_id = "test-123"
        mock_state.rounds = []
        mock_state.mark_updated = MagicMock()

        mock_engine.start_interview = AsyncMock(
            return_value=MagicMock(is_ok=True, is_err=False, value=mock_state)
        )
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_ok=True, is_err=False, value="First question?")
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_score = create_mock_live_ambiguity_score(0.67, seed_ready=False)
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))

        handler = InterviewHandler(interview_engine=mock_engine, llm_adapter=MagicMock())
        with patch(
            "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
            return_value=mock_scorer,
        ):
            result = await handler.handle(
                {"initial_context": "Add a feature", "cwd": str(tmp_path)}
            )

        mock_engine.start_interview.assert_awaited_once()
        call_kwargs = mock_engine.start_interview.call_args
        assert call_kwargs[1]["cwd"] == str(tmp_path)
        assert "(ambiguity: 0.67) First question?" in result.value.content[0].text

    async def test_interview_handle_clears_stored_ambiguity_after_new_answer(self) -> None:
        """Interview answers should refresh the ambiguity snapshot after rescoring."""
        handler = InterviewHandler(llm_adapter=MagicMock())
        state = InterviewState(
            interview_id="sess-123",
            ambiguity_score=0.14,
            ambiguity_breakdown={"goal_clarity": {"name": "goal_clarity"}},
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )
        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.record_response = AsyncMock(return_value=Result.ok(state))
        mock_engine.ask_next_question = AsyncMock(
            return_value=MagicMock(is_ok=True, is_err=False, value="Next question?"),
        )
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_score = create_mock_live_ambiguity_score(0.44, seed_ready=False)
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
                return_value=mock_scorer,
            ),
        ):
            result = await handler.handle({"session_id": "sess-123", "answer": "Manage tasks"})

        assert result.is_ok
        assert state.ambiguity_score == 0.44
        assert state.ambiguity_breakdown is not None
        assert "(ambiguity: 0.44) Next question?" in result.value.content[0].text

    async def test_interview_handle_done_completes_without_new_question(self) -> None:
        """Explicit completion signals should stop the interview instead of asking again."""
        handler = InterviewHandler()
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-123",
            ambiguity_score=0.14,
            ambiguity_breakdown={"goal_clarity": {"name": "goal_clarity"}},
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )

        async def complete_state(
            current_state: InterviewState,
        ) -> Result[InterviewState, Exception]:
            current_state.status = InterviewStatus.COMPLETED
            return Result.ok(current_state)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock(side_effect=complete_state)
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
            return_value=mock_engine,
        ):
            result = await handler.handle({"session_id": "sess-123", "answer": "done"})

        assert result.is_ok
        assert state.status == InterviewStatus.COMPLETED
        assert state.rounds == []
        assert state.ambiguity_score is None
        assert state.ambiguity_breakdown is None
        mock_engine.ask_next_question.assert_not_called()
        assert result.value.meta["completed"] is True

    async def test_interview_handle_auto_completes_when_live_ambiguity_is_low(self) -> None:
        """Low live ambiguity should end the interview without another question."""
        handler = InterviewHandler(llm_adapter=MagicMock())
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-123",
            rounds=[
                InterviewRound(round_number=1, question="Q1", user_response="A1"),
                InterviewRound(round_number=2, question="Q2", user_response="A2"),
                InterviewRound(round_number=3, question="Q3", user_response=None),
            ],
        )

        async def complete_state(
            current_state: InterviewState,
        ) -> Result[InterviewState, Exception]:
            current_state.status = InterviewStatus.COMPLETED
            return Result.ok(current_state)

        async def record_answer(
            current_state: InterviewState,
            answer: str,
            question: str,
        ) -> Result[InterviewState, Exception]:
            current_state.rounds.append(
                InterviewRound(
                    round_number=3,
                    question=question,
                    user_response=answer,
                )
            )
            return Result.ok(current_state)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.record_response = AsyncMock(side_effect=record_answer)
        mock_engine.complete_interview = AsyncMock(side_effect=complete_state)
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()
        mock_score = create_mock_live_ambiguity_score(0.18, seed_ready=True)
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
                return_value=mock_scorer,
            ),
        ):
            result = await handler.handle({"session_id": "sess-123", "answer": "A3"})

        assert result.is_ok
        assert state.status == InterviewStatus.COMPLETED
        assert result.value.meta["completed"] is True
        assert result.value.meta["ambiguity_score"] == 0.18
        assert "(ambiguity: 0.18) Ready for Seed generation." in result.value.content[0].text
        mock_engine.ask_next_question.assert_not_called()


class TestGenerateSeedHandlerAmbiguity:
    """Test ambiguity persistence behavior in GenerateSeedHandler."""

    async def test_generate_seed_handler_calculates_and_persists_ambiguity_when_missing(
        self,
    ) -> None:
        """GenerateSeedHandler should score the interview and persist the snapshot when absent."""
        state = InterviewState(
            interview_id="sess-123",
            initial_context="Build a tool",
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response="Manage tasks",
                )
            ],
        )
        mock_adapter = MagicMock()
        mock_interview_engine = MagicMock()
        mock_interview_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_interview_engine.save_state = AsyncMock(
            return_value=MagicMock(is_ok=True, is_err=False),
        )
        mock_seed_generator = MagicMock()
        mock_seed_generator.generate = AsyncMock(return_value=Result.err(RuntimeError("boom")))
        mock_score = MagicMock(
            overall_score=0.12,
            breakdown=MagicMock(
                model_dump=MagicMock(
                    return_value={
                        "goal_clarity": {
                            "name": "goal_clarity",
                            "clarity_score": 0.9,
                            "weight": 0.4,
                            "justification": "Clear goal",
                        },
                        "constraint_clarity": {
                            "name": "constraint_clarity",
                            "clarity_score": 0.9,
                            "weight": 0.3,
                            "justification": "Clear constraints",
                        },
                        "success_criteria_clarity": {
                            "name": "success_criteria_clarity",
                            "clarity_score": 0.85,
                            "weight": 0.3,
                            "justification": "Measurable success",
                        },
                    }
                )
            ),
        )
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=Result.ok(mock_score))
        handler = GenerateSeedHandler(
            llm_adapter=mock_adapter,
            interview_engine=mock_interview_engine,
            seed_generator=mock_seed_generator,
        )

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
                return_value=mock_scorer,
            ) as mock_scorer_cls,
        ):
            await handler.handle({"session_id": "sess-123"})

        mock_scorer_cls.assert_called_once()
        mock_scorer.score.assert_awaited_once_with(state)
        mock_interview_engine.save_state.assert_awaited_once_with(state)
        assert state.ambiguity_score == 0.12
        assert state.ambiguity_breakdown is not None
        generate_call = mock_seed_generator.generate.await_args
        assert generate_call.args[0] == state
        assert generate_call.args[1].overall_score == 0.12

    async def test_generate_seed_handler_reuses_stored_ambiguity_snapshot(self) -> None:
        """GenerateSeedHandler should not rescore when the interview state already has a snapshot."""
        state = InterviewState(
            interview_id="sess-123",
            initial_context="Build a tool",
            ambiguity_score=0.11,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "goal_clarity",
                    "clarity_score": 0.92,
                    "weight": 0.4,
                    "justification": "Clear goal",
                },
                "constraint_clarity": {
                    "name": "constraint_clarity",
                    "clarity_score": 0.88,
                    "weight": 0.3,
                    "justification": "Clear constraints",
                },
                "success_criteria_clarity": {
                    "name": "success_criteria_clarity",
                    "clarity_score": 0.87,
                    "weight": 0.3,
                    "justification": "Clear success criteria",
                },
            },
        )
        mock_adapter = MagicMock()
        mock_interview_engine = MagicMock()
        mock_interview_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_interview_engine.save_state = AsyncMock(
            return_value=MagicMock(is_ok=True, is_err=False),
        )
        mock_seed_generator = MagicMock()
        mock_seed_generator.generate = AsyncMock(return_value=Result.err(RuntimeError("boom")))
        handler = GenerateSeedHandler(
            llm_adapter=mock_adapter,
            interview_engine=mock_interview_engine,
            seed_generator=mock_seed_generator,
        )

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
            ) as mock_scorer_cls,
        ):
            await handler.handle({"session_id": "sess-123"})

        mock_scorer_cls.assert_not_called()
        assert mock_interview_engine.save_state.await_count == 0
        generate_call = mock_seed_generator.generate.await_args
        assert generate_call.args[1].overall_score == 0.11


class TestCancelExecutionHandler:
    """Test CancelExecutionHandler class."""

    def test_definition_name(self) -> None:
        """CancelExecutionHandler has correct tool name."""
        handler = CancelExecutionHandler()
        assert handler.definition.name == "ouroboros_cancel_execution"

    def test_definition_requires_execution_id(self) -> None:
        """CancelExecutionHandler requires execution_id parameter."""
        handler = CancelExecutionHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "execution_id" in param_names

        exec_param = next(p for p in defn.parameters if p.name == "execution_id")
        assert exec_param.required is True
        assert exec_param.type == ToolInputType.STRING

    def test_definition_has_optional_reason(self) -> None:
        """CancelExecutionHandler has optional reason parameter."""
        handler = CancelExecutionHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "reason" in param_names

        reason_param = next(p for p in defn.parameters if p.name == "reason")
        assert reason_param.required is False

    def test_definition_description_mentions_cancel(self) -> None:
        """CancelExecutionHandler description mentions cancellation."""
        handler = CancelExecutionHandler()
        assert "cancel" in handler.definition.description.lower()

    async def test_handle_requires_execution_id(self) -> None:
        """handle returns error when execution_id is missing."""
        handler = CancelExecutionHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "execution_id is required" in str(result.error)

    async def test_handle_requires_execution_id_nonempty(self) -> None:
        """handle returns error when execution_id is empty string."""
        handler = CancelExecutionHandler()
        result = await handler.handle({"execution_id": ""})

        assert result.is_err
        assert "execution_id is required" in str(result.error)

    async def test_handle_not_found(self) -> None:
        """handle returns error when execution does not exist."""
        handler = CancelExecutionHandler()
        result = await handler.handle({"execution_id": "nonexistent-id"})

        assert result.is_err
        assert "not found" in str(result.error).lower() or "no events" in str(result.error).lower()

    async def test_handle_cancels_running_session(self) -> None:
        """handle successfully cancels a running session."""
        from ouroboros.orchestrator.session import SessionRepository, SessionStatus
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        # Create a running session via the repository
        repo = SessionRepository(event_store)
        create_result = await repo.create_session(
            execution_id="exec_cancel_123",
            seed_id="test-seed",
            session_id="orch_cancel_123",
        )
        assert create_result.is_ok

        # Now cancel via handler (passing execution_id, not session_id)
        handler = CancelExecutionHandler(event_store=event_store)
        result = await handler.handle(
            {
                "execution_id": "exec_cancel_123",
                "reason": "Test cancellation",
            }
        )

        assert result.is_ok
        assert "cancelled" in result.value.text_content.lower()
        assert result.value.meta["execution_id"] == "exec_cancel_123"
        assert result.value.meta["previous_status"] == "running"
        assert result.value.meta["new_status"] == "cancelled"
        assert result.value.meta["reason"] == "Test cancellation"
        assert result.value.meta["cancelled_by"] == "mcp_tool"

        # Verify session is now cancelled
        reconstructed = await repo.reconstruct_session("orch_cancel_123")
        assert reconstructed.is_ok
        assert reconstructed.value.status == SessionStatus.CANCELLED

    async def test_handle_rejects_completed_session(self) -> None:
        """handle returns error when session is already completed."""
        from ouroboros.orchestrator.session import SessionRepository
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        repo = SessionRepository(event_store)
        await repo.create_session(
            execution_id="exec_completed_123",
            seed_id="test-seed",
            session_id="orch_completed_123",
        )
        await repo.mark_completed("orch_completed_123")

        handler = CancelExecutionHandler(event_store=event_store)
        result = await handler.handle({"execution_id": "exec_completed_123"})

        assert result.is_err
        assert "terminal state" in str(result.error).lower()
        assert "completed" in str(result.error).lower()

    async def test_handle_rejects_failed_session(self) -> None:
        """handle returns error when session has already failed."""
        from ouroboros.orchestrator.session import SessionRepository
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        repo = SessionRepository(event_store)
        await repo.create_session(
            execution_id="exec_failed_123",
            seed_id="test-seed",
            session_id="orch_failed_123",
        )
        await repo.mark_failed("orch_failed_123", error_message="some error")

        handler = CancelExecutionHandler(event_store=event_store)
        result = await handler.handle({"execution_id": "exec_failed_123"})

        assert result.is_err
        assert "terminal state" in str(result.error).lower()
        assert "failed" in str(result.error).lower()

    async def test_handle_rejects_already_cancelled_session(self) -> None:
        """handle returns error when session is already cancelled."""
        from ouroboros.orchestrator.session import SessionRepository
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        repo = SessionRepository(event_store)
        await repo.create_session(
            execution_id="exec_cancelled_123",
            seed_id="test-seed",
            session_id="orch_cancelled_123",
        )
        await repo.mark_cancelled("orch_cancelled_123", reason="first cancel")

        handler = CancelExecutionHandler(event_store=event_store)
        result = await handler.handle({"execution_id": "exec_cancelled_123"})

        assert result.is_err
        assert "terminal state" in str(result.error).lower()
        assert "cancelled" in str(result.error).lower()

    async def test_handle_default_reason(self) -> None:
        """handle uses default reason when none provided."""
        from ouroboros.orchestrator.session import SessionRepository
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        repo = SessionRepository(event_store)
        await repo.create_session(
            execution_id="exec_default_reason_123",
            seed_id="test-seed",
            session_id="orch_default_reason_123",
        )

        handler = CancelExecutionHandler(event_store=event_store)
        result = await handler.handle({"execution_id": "exec_default_reason_123"})

        assert result.is_ok
        assert result.value.meta["reason"] == "Cancelled by user"

    async def test_handle_cancel_idempotent_state_after_cancel(self) -> None:
        """Cancellation is reflected in event store; second cancel attempt rejected."""
        from ouroboros.orchestrator.session import SessionRepository
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        repo = SessionRepository(event_store)
        await repo.create_session(
            execution_id="exec_double_cancel_123",
            seed_id="test-seed",
            session_id="orch_double_cancel_123",
        )

        handler = CancelExecutionHandler(event_store=event_store)

        # First cancel succeeds
        result1 = await handler.handle(
            {
                "execution_id": "exec_double_cancel_123",
                "reason": "first attempt",
            }
        )
        assert result1.is_ok

        # Second cancel is rejected (already in terminal state)
        result2 = await handler.handle(
            {
                "execution_id": "exec_double_cancel_123",
                "reason": "second attempt",
            }
        )
        assert result2.is_err
        assert "terminal state" in str(result2.error).lower()

    async def test_handle_cancel_preserves_execution_id_in_response(self) -> None:
        """Cancellation response meta contains all expected fields."""
        from ouroboros.orchestrator.session import SessionRepository
        from ouroboros.persistence.event_store import EventStore

        event_store = EventStore("sqlite+aiosqlite:///:memory:")
        await event_store.initialize()

        repo = SessionRepository(event_store)
        await repo.create_session(
            execution_id="exec_meta_fields_123",
            seed_id="test-seed",
            session_id="orch_meta_fields_123",
        )

        handler = CancelExecutionHandler(event_store=event_store)
        result = await handler.handle(
            {
                "execution_id": "exec_meta_fields_123",
                "reason": "checking meta",
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert "execution_id" in meta
        assert "previous_status" in meta
        assert "new_status" in meta
        assert "reason" in meta
        assert "cancelled_by" in meta

    async def test_handle_cancel_event_store_error_graceful(self) -> None:
        """Handler gracefully handles event store errors during cancellation."""
        from ouroboros.orchestrator.session import SessionRepository, SessionStatus, SessionTracker

        # Use a mock to simulate event store failure during mark_cancelled
        mock_event_store = AsyncMock()
        mock_event_store.initialize = AsyncMock()

        handler = CancelExecutionHandler(event_store=mock_event_store)
        handler._initialized = True

        # Mock reconstruct to return a running session
        mock_tracker = MagicMock(spec=SessionTracker)
        mock_tracker.status = SessionStatus.RUNNING
        mock_repo = AsyncMock(spec=SessionRepository)
        mock_repo.reconstruct_session = AsyncMock(
            return_value=MagicMock(is_ok=True, is_err=False, value=mock_tracker)
        )
        mock_repo.mark_cancelled = AsyncMock(
            return_value=MagicMock(
                is_ok=False,
                is_err=True,
                error=MagicMock(message="Database write failed"),
            )
        )
        handler._session_repo = mock_repo

        result = await handler.handle(
            {
                "execution_id": "test-error",
                "reason": "testing error handling",
            }
        )

        assert result.is_err
        assert "failed to cancel" in str(result.error).lower()
