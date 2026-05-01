"""Tests for Ouroboros tool definitions."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.errors import ConfigError
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import _is_interview_completion_signal
from ouroboros.mcp.tools.brownfield_handler import BrownfieldHandler
from ouroboros.mcp.tools.definitions import (
    OUROBOROS_TOOLS,
    ACTreeHUDHandler,
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
    start_execute_seed_handler,
)
from ouroboros.mcp.tools.pm_handler import PMInterviewHandler
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.types import ToolInputType
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_SESSION_ID_ARG,
)
from ouroboros.orchestrator.session import SessionTracker
from ouroboros.persistence.event_store import EventStore
from ouroboros.resilience.lateral import ThinkingPersona


@pytest.fixture
async def memory_event_store() -> AsyncIterator[EventStore]:
    """Provide an initialized in-memory event store and dispose it after each test."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


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

    async def test_handle_reports_execution_handler_config_error(self) -> None:
        """Config failures should surface with execution-handler context."""
        handler = ExecuteSeedHandler()

        with patch(
            "ouroboros.mcp.tools.execution_handlers.get_max_parallel_workers",
            side_effect=ConfigError(
                "orchestrator.max_parallel_workers must be greater than 0",
                config_key="orchestrator.max_parallel_workers",
            ),
        ):
            result = await handler.handle({"seed_content": VALID_SEED_YAML})

        assert result.is_err
        assert "Execution handler config error" in str(result.error)
        assert "max_parallel_workers" in str(result.error)
        assert "Invalid parallel worker configuration" not in str(result.error)

    async def test_handle_reports_parse_config_error_with_same_prefix(self) -> None:
        """Non-worker config failures should use the same handler-context prefix."""
        handler = ExecuteSeedHandler()

        with patch(
            "ouroboros.mcp.tools.execution_handlers.get_max_parallel_workers",
            side_effect=ConfigError(
                "Failed to parse configuration file: invalid YAML",
            ),
        ):
            result = await handler.handle({"seed_content": VALID_SEED_YAML})

        assert result.is_err
        assert "Execution handler config error" in str(result.error)
        assert "Failed to parse configuration file" in str(result.error)
        assert "Invalid parallel worker configuration" not in str(result.error)

    def test_execute_seed_handler_factory_accepts_runtime_backend(self) -> None:
        """Factory helper preserves explicit runtime backend selection."""
        handler = execute_seed_handler(runtime_backend="codex")
        assert handler.agent_runtime_backend == "codex"

    def test_execute_seed_handler_factory_accepts_llm_backend(self) -> None:
        """Factory helper preserves explicit llm backend selection."""
        handler = execute_seed_handler(runtime_backend="opencode", llm_backend="opencode")
        assert handler.agent_runtime_backend == "opencode"
        assert handler.llm_backend == "opencode"


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
        from ouroboros.orchestrator.session import SessionRepository, SessionStatus

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

    async def test_handle_includes_sub_ac_progress(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Session status should expose Sub-AC progress alongside AC progress."""
        from ouroboros.events.base import BaseEvent

        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess-status-sub-ac",
                data={
                    "execution_id": "exec-status-sub-ac",
                    "seed_id": "seed-status-sub-ac",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec-status-sub-ac",
                data={
                    "session_id": "sess-status-sub-ac",
                    "completed_count": 0,
                    "total_count": 2,
                    "current_phase": "Deliver",
                    "acceptance_criteria": [
                        {"index": 1, "content": "First parent", "status": "executing"},
                        {"index": 2, "content": "Second parent", "status": "executing"},
                    ],
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.subtask.updated",
                aggregate_type="execution",
                aggregate_id="exec-status-sub-ac",
                data={
                    "ac_index": 1,
                    "sub_task_index": 1,
                    "sub_task_id": "ac_1_sub_1",
                    "content": "Child one",
                    "status": "completed",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="execution.subtask.updated",
                aggregate_type="execution",
                aggregate_id="exec-status-sub-ac",
                data={
                    "ac_index": 1,
                    "sub_task_index": 2,
                    "sub_task_id": "ac_1_sub_2",
                    "content": "Child two",
                    "status": "executing",
                },
            )
        )

        handler = SessionStatusHandler(event_store=memory_event_store)
        result = await handler.handle({"session_id": "sess-status-sub-ac"})

        assert result.is_ok
        assert "completed_count: 0" in result.value.text_content
        assert "sub_ac_progress: 1/2 complete · 1 working" in result.value.text_content
        assert result.value.meta["progress"]["sub_ac_total_count"] == 2


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

    async def test_handle_with_session_id_includes_related_parallel_execution_events(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """session_id queries should include execution and child AC aggregates."""
        from ouroboros.events.base import BaseEvent

        await memory_event_store.append(
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
        await memory_event_store.append(
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
        await memory_event_store.append(
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

        handler = QueryEventsHandler(event_store=memory_event_store)
        result = await handler.handle({"session_id": "orch_parallel_123", "limit": 20})

        assert result.is_ok
        text = result.value.text_content
        assert "workflow.progress.updated" in text
        assert "execution.session.started" in text
        assert "exec_parallel_123_sub_ac_0_0" in text


class TestOuroborosTools:
    """Test OUROBOROS_TOOLS constant."""

    EXPECTED_OUROBOROS_TOOL_NAMES = {
        "ouroboros_ac_tree_hud",
        "ouroboros_auto",
        "ouroboros_brownfield",
        "ouroboros_cancel_execution",
        "ouroboros_cancel_job",
        "ouroboros_checklist_verify",
        "ouroboros_evaluate",
        "ouroboros_evolve_rewind",
        "ouroboros_evolve_step",
        "ouroboros_execute_seed",
        "ouroboros_generate_seed",
        "ouroboros_interview",
        "ouroboros_job_result",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_lateral_think",
        "ouroboros_lineage_status",
        "ouroboros_measure_drift",
        "ouroboros_pm_interview",
        "ouroboros_qa",
        "ouroboros_query_events",
        "ouroboros_session_status",
        "ouroboros_start_evolve_step",
        "ouroboros_start_execute_seed",
    }

    def test_ouroboros_tools_contains_all_handlers(self) -> None:
        """OUROBOROS_TOOLS contains all standard handlers."""
        from ouroboros.mcp.tools.evaluation_handlers import ChecklistVerifyHandler

        names = {h.definition.name for h in OUROBOROS_TOOLS}
        assert names == self.EXPECTED_OUROBOROS_TOOL_NAMES

        handler_types = {type(h) for h in OUROBOROS_TOOLS}
        assert ACTreeHUDHandler in handler_types
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
        assert ChecklistVerifyHandler in handler_types
        assert LateralThinkHandler in handler_types
        assert EvolveStepHandler in handler_types
        assert StartEvolveStepHandler in handler_types
        assert LineageStatusHandler in handler_types
        assert EvolveRewindHandler in handler_types
        assert CancelExecutionHandler in handler_types
        assert BrownfieldHandler in handler_types
        assert PMInterviewHandler in handler_types
        assert QAHandler in handler_types

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
        assert {h.definition.name for h in tools} == self.EXPECTED_OUROBOROS_TOOL_NAMES | {
            "ouroboros_auto"
        }
        execute_handler = next(h for h in tools if isinstance(h, ExecuteSeedHandler))
        assert execute_handler.agent_runtime_backend == "codex"

    def test_get_ouroboros_tools_can_inject_llm_backend(self) -> None:
        """Tool factory propagates llm backend to LLM-only handlers."""
        tools = get_ouroboros_tools(runtime_backend="codex", llm_backend="litellm")
        execute_handler = next(h for h in tools if isinstance(h, ExecuteSeedHandler))
        start_execute_handler = next(h for h in tools if isinstance(h, StartExecuteSeedHandler))
        generate_handler = next(h for h in tools if isinstance(h, GenerateSeedHandler))
        interview_handler_instance = next(h for h in tools if isinstance(h, InterviewHandler))
        evaluate_handler_instance = next(h for h in tools if isinstance(h, EvaluateHandler))
        qa_handler = next(h for h in tools if isinstance(h, QAHandler))

        assert execute_handler.agent_runtime_backend == "codex"
        assert execute_handler.llm_backend == "litellm"
        assert start_execute_handler._execute_handler is execute_handler
        assert start_execute_handler._execute_handler.agent_runtime_backend == "codex"
        assert start_execute_handler._execute_handler.llm_backend == "litellm"
        assert generate_handler.llm_backend == "litellm"
        assert interview_handler_instance.llm_backend == "litellm"
        assert evaluate_handler_instance.llm_backend == "litellm"
        assert qa_handler.llm_backend == "litellm"

    def test_llm_handler_factories_preserve_backend_selection(self) -> None:
        """Convenience factories preserve explicit llm backend selection."""
        assert generate_seed_handler(llm_backend="litellm").llm_backend == "litellm"
        assert interview_handler(llm_backend="litellm").llm_backend == "litellm"
        assert evaluate_handler(llm_backend="litellm").llm_backend == "litellm"


class TestAsyncJobHandlers:
    """Test async background job MCP handler definitions."""

    def test_start_execute_seed_definition_name(self) -> None:
        handler = StartExecuteSeedHandler()
        assert handler.definition.name == "ouroboros_start_execute_seed"

    def test_start_execute_seed_definition_mentions_ac_tree_hud(self) -> None:
        handler = StartExecuteSeedHandler()
        assert "ouroboros_ac_tree_hud" in handler.definition.description
        assert "ouroboros_job_wait" not in handler.definition.description

    async def test_start_execute_seed_background_generates_ids_without_session(
        self,
    ) -> None:
        """Background path can generate execution/session IDs before dispatch."""

        class FakeEventStore:
            async def initialize(self) -> None:
                return None

        class FakeJobManager:
            async def start_job(self, *, job_type, initial_message, runner, links):
                runner.close()
                return SimpleNamespace(
                    job_id="job_test",
                    links=links,
                    status=SimpleNamespace(value="queued"),
                    cursor=1,
                )

        execute_handler = MagicMock()
        execute_handler.agent_runtime_backend = None
        execute_handler.llm_backend = None
        handler = StartExecuteSeedHandler(
            execute_handler=execute_handler,
            event_store=FakeEventStore(),
            job_manager=FakeJobManager(),
            agent_runtime_backend="codex",
            opencode_mode=None,
        )

        result = await handler.handle({"seed_content": "goal: test"})

        assert result.is_ok
        assert result.value.meta["execution_id"].startswith("exec_")
        assert result.value.meta["session_id"].startswith("orch_")

    def test_job_status_definition_name(self) -> None:
        handler = JobStatusHandler()
        assert handler.definition.name == "ouroboros_job_status"
        params = {p.name: p for p in handler.definition.parameters}
        param_names = set(params)
        assert param_names == {"job_id", "view"}
        assert params["view"].default == "full"

    def test_job_wait_definition_has_expected_params(self) -> None:
        handler = JobWaitHandler()
        params = {p.name: p for p in handler.definition.parameters}
        param_names = set(params)
        assert param_names == {"job_id", "cursor", "timeout_seconds", "view"}
        assert params["view"].default == "full"

    def test_job_result_definition_name(self) -> None:
        handler = JobResultHandler()
        assert handler.definition.name == "ouroboros_job_result"

    def test_ac_tree_hud_definition_has_expected_params(self) -> None:
        handler = ACTreeHUDHandler()
        params = {p.name: p for p in handler.definition.parameters}
        param_names = set(params)
        assert param_names == {"session_id", "cursor", "view", "max_nodes"}
        assert params["view"].default == "tree"

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


class TestLateralThinkHandler:
    """Test LateralThinkHandler argument normalization."""

    async def test_handle_treats_null_failed_attempts_as_empty(self) -> None:
        """Explicit null from MCP clients should behave like an omitted optional array."""
        handler = LateralThinkHandler()

        mock_lateral_result = MagicMock(
            approach_summary="Try a different angle",
            prompt="Consider an alternative path",
            questions=("What assumption can you invert?",),
            persona=MagicMock(value="contrarian"),
        )
        mock_thinker = MagicMock()
        mock_thinker.generate_alternative.return_value = Result.ok(mock_lateral_result)

        with patch(
            "ouroboros.resilience.lateral.LateralThinker",
            return_value=mock_thinker,
        ):
            result = await handler.handle(
                {
                    "problem_context": "tool crashes when optional arg is null",
                    "current_approach": "call ouroboros_lateral_think without failed_attempts",
                    "failed_attempts": None,
                }
            )

        assert result.is_ok
        mock_thinker.generate_alternative.assert_called_once_with(
            persona=ThinkingPersona.CONTRARIAN,
            problem_context="tool crashes when optional arg is null",
            current_approach="call ouroboros_lateral_think without failed_attempts",
            failed_attempts=(),
        )

    async def test_handle_filters_falsey_failed_attempts_entries(self) -> None:
        """Falsy entries should be dropped while valid entries are stringified."""
        handler = LateralThinkHandler()

        mock_lateral_result = MagicMock(
            approach_summary="Try a different angle",
            prompt="Consider an alternative path",
            questions=("What assumption can you invert?",),
            persona=MagicMock(value="architect"),
        )
        mock_thinker = MagicMock()
        mock_thinker.generate_alternative.return_value = Result.ok(mock_lateral_result)

        with patch(
            "ouroboros.resilience.lateral.LateralThinker",
            return_value=mock_thinker,
        ):
            result = await handler.handle(
                {
                    "problem_context": "problem",
                    "current_approach": "approach",
                    "persona": "architect",
                    "failed_attempts": ["first", None, "", 7],
                }
            )

        assert result.is_ok
        mock_thinker.generate_alternative.assert_called_once_with(
            persona=ThinkingPersona.ARCHITECT,
            problem_context="problem",
            current_approach="approach",
            failed_attempts=("first", "7"),
        )


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

    async def test_handle_none_optional_params(self) -> None:
        """handle succeeds when optional params are explicitly None (#275)."""
        handler = MeasureDriftHandler()
        result = await handler.handle(
            {
                "session_id": "test-session",
                "current_output": "Built a test task with Python 3.14",
                "seed_content": VALID_SEED_YAML,
                "constraint_violations": None,
                "current_concepts": None,
            }
        )

        assert result.is_ok
        meta = result.value.meta
        assert "combined_drift" in meta
        assert meta["constraint_drift"] == 0.0


class TestEvaluateHandler:
    """Test EvaluateHandler class."""

    def test_definition_name(self) -> None:
        """EvaluateHandler has correct name."""
        handler = EvaluateHandler()
        assert handler.definition.name == "ouroboros_evaluate"

    def test_handler_has_no_server_side_timeout(self) -> None:
        """Long-running evaluation should not inherit a fixed server timeout."""
        handler = EvaluateHandler()
        assert handler.TIMEOUT_SECONDS == 0

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

    async def test_handle_not_found(self, memory_event_store: EventStore) -> None:
        """handle returns error when execution does not exist."""
        handler = CancelExecutionHandler(event_store=memory_event_store)
        result = await handler.handle({"execution_id": "nonexistent-id"})

        assert result.is_err
        assert "not found" in str(result.error).lower() or "no events" in str(result.error).lower()

    async def test_handle_cancels_running_session(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """handle successfully cancels a running session."""
        from ouroboros.orchestrator.session import SessionRepository, SessionStatus

        # Create a running session via the repository
        repo = SessionRepository(memory_event_store)
        create_result = await repo.create_session(
            execution_id="exec_cancel_123",
            seed_id="test-seed",
            session_id="orch_cancel_123",
        )
        assert create_result.is_ok

        # Now cancel via handler (passing execution_id, not session_id)
        handler = CancelExecutionHandler(event_store=memory_event_store)
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

    async def test_handle_rejects_completed_session(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """handle returns error when session is already completed."""
        from ouroboros.orchestrator.session import SessionRepository

        repo = SessionRepository(memory_event_store)
        await repo.create_session(
            execution_id="exec_completed_123",
            seed_id="test-seed",
            session_id="orch_completed_123",
        )
        await repo.mark_completed("orch_completed_123")

        handler = CancelExecutionHandler(event_store=memory_event_store)
        result = await handler.handle({"execution_id": "exec_completed_123"})

        assert result.is_err
        assert "terminal state" in str(result.error).lower()
        assert "completed" in str(result.error).lower()

    async def test_handle_rejects_failed_session(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """handle returns error when session has already failed."""
        from ouroboros.orchestrator.session import SessionRepository

        repo = SessionRepository(memory_event_store)
        await repo.create_session(
            execution_id="exec_failed_123",
            seed_id="test-seed",
            session_id="orch_failed_123",
        )
        await repo.mark_failed("orch_failed_123", error_message="some error")

        handler = CancelExecutionHandler(event_store=memory_event_store)
        result = await handler.handle({"execution_id": "exec_failed_123"})

        assert result.is_err
        assert "terminal state" in str(result.error).lower()
        assert "failed" in str(result.error).lower()

    async def test_handle_rejects_already_cancelled_session(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """handle returns error when session is already cancelled."""
        from ouroboros.orchestrator.session import SessionRepository

        repo = SessionRepository(memory_event_store)
        await repo.create_session(
            execution_id="exec_cancelled_123",
            seed_id="test-seed",
            session_id="orch_cancelled_123",
        )
        await repo.mark_cancelled("orch_cancelled_123", reason="first cancel")

        handler = CancelExecutionHandler(event_store=memory_event_store)
        result = await handler.handle({"execution_id": "exec_cancelled_123"})

        assert result.is_err
        assert "terminal state" in str(result.error).lower()
        assert "cancelled" in str(result.error).lower()

    async def test_handle_default_reason(self, memory_event_store: EventStore) -> None:
        """handle uses default reason when none provided."""
        from ouroboros.orchestrator.session import SessionRepository

        repo = SessionRepository(memory_event_store)
        await repo.create_session(
            execution_id="exec_default_reason_123",
            seed_id="test-seed",
            session_id="orch_default_reason_123",
        )

        handler = CancelExecutionHandler(event_store=memory_event_store)
        result = await handler.handle({"execution_id": "exec_default_reason_123"})

        assert result.is_ok
        assert result.value.meta["reason"] == "Cancelled by user"

    async def test_handle_cancel_idempotent_state_after_cancel(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Cancellation is reflected in event store; second cancel attempt rejected."""
        from ouroboros.orchestrator.session import SessionRepository

        repo = SessionRepository(memory_event_store)
        await repo.create_session(
            execution_id="exec_double_cancel_123",
            seed_id="test-seed",
            session_id="orch_double_cancel_123",
        )

        handler = CancelExecutionHandler(event_store=memory_event_store)

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

    async def test_handle_cancel_preserves_execution_id_in_response(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Cancellation response meta contains all expected fields."""
        from ouroboros.orchestrator.session import SessionRepository

        repo = SessionRepository(memory_event_store)
        await repo.create_session(
            execution_id="exec_meta_fields_123",
            seed_id="test-seed",
            session_id="orch_meta_fields_123",
        )

        handler = CancelExecutionHandler(event_store=memory_event_store)
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
        from ouroboros.orchestrator.session import SessionRepository, SessionStatus

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


class TestStartExecuteSeedHandlerBackendPropagation:
    """Review finding #5: start_execute_seed_handler must propagate backends."""

    def test_factory_passes_backends_to_execute_handler(self):
        handler = start_execute_seed_handler(
            runtime_backend="codex",
            llm_backend="codex",
        )
        inner = handler._execute_handler
        assert inner.agent_runtime_backend == "codex"
        assert inner.llm_backend == "codex"

    def test_factory_defaults_to_none(self):
        handler = start_execute_seed_handler()
        inner = handler._execute_handler
        assert inner.agent_runtime_backend is None
        assert inner.llm_backend is None


class TestInterviewHandlerDrain:
    """Test that close() drains pending background event tasks."""

    async def test_close_drains_pending_bg_tasks(self) -> None:
        """close() should await all pending bg tasks before closing the event store."""
        mock_store = AsyncMock()
        handler = InterviewHandler(event_store=mock_store)
        handler._owns_event_store = True

        completed = asyncio.Event()

        async def slow_emit() -> None:
            await asyncio.sleep(0.05)
            completed.set()

        task = asyncio.create_task(slow_emit())
        handler._bg_tasks.add(task)
        task.add_done_callback(handler._bg_tasks.discard)

        await handler.close()

        assert completed.is_set()
        assert len(handler._bg_tasks) == 0
        mock_store.close.assert_awaited_once()

    async def test_close_cancels_stuck_tasks_on_timeout(self) -> None:
        """close() should cancel tasks that exceed the drain timeout."""
        mock_store = AsyncMock()
        handler = InterviewHandler(event_store=mock_store)
        handler._owns_event_store = True

        async def stuck_emit() -> None:
            await asyncio.sleep(999)

        task = asyncio.create_task(stuck_emit())
        handler._bg_tasks.add(task)
        task.add_done_callback(handler._bg_tasks.discard)

        await handler._drain_bg_tasks(timeout=0.05)

        assert task.cancelled()
        assert len(handler._bg_tasks) == 0

    async def test_close_without_bg_tasks_is_noop(self) -> None:
        """close() with no pending tasks should just close the store."""
        mock_store = AsyncMock()
        handler = InterviewHandler(event_store=mock_store)
        handler._owns_event_store = True

        await handler.close()

        assert len(handler._bg_tasks) == 0
        mock_store.close.assert_awaited_once()

    async def test_emit_event_bg_after_close_is_noop(self) -> None:
        """_emit_event_bg() after close() must not create tasks or re-initialize."""
        mock_store = AsyncMock()
        handler = InterviewHandler(event_store=mock_store)
        handler._owns_event_store = True

        await handler.close()
        assert handler._closed is True

        # Reset the mock to track post-close calls only
        mock_store.initialize.reset_mock()
        mock_store.append.reset_mock()

        handler._emit_event_bg({"type": "late_event"})

        # Give any accidentally created tasks a chance to run
        await asyncio.sleep(0.05)

        assert len(handler._bg_tasks) == 0
        mock_store.initialize.assert_not_awaited()
        mock_store.append.assert_not_awaited()

    async def test_close_sets_closed_flag(self) -> None:
        """close() must set _closed before draining tasks."""
        mock_store = AsyncMock()
        handler = InterviewHandler(event_store=mock_store)
        handler._owns_event_store = True

        assert handler._closed is False
        await handler.close()
        assert handler._closed is True
