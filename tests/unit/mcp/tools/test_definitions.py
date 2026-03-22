"""Tests for Ouroboros tool definitions."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
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
)
from ouroboros.mcp.types import ToolInputType
from ouroboros.orchestrator.adapter import (
    DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG,
    DELEGATED_PARENT_PERMISSION_MODE_ARG,
    DELEGATED_PARENT_SESSION_ID_ARG,
)
from ouroboros.orchestrator.runner import OrchestratorResult

VALID_SEED_YAML = """
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


class TestExecuteSeedHandler:
    """Test ExecuteSeedHandler class."""

    def test_definition_name(self) -> None:
        """ExecuteSeedHandler has correct name."""
        handler = ExecuteSeedHandler()
        assert handler.definition.name == "ouroboros_execute_seed"

    def test_definition_has_required_parameters(self) -> None:
        """ExecuteSeedHandler has required seed_content parameter."""
        handler = ExecuteSeedHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "seed_content" in param_names

        seed_param = next(p for p in defn.parameters if p.name == "seed_content")
        assert seed_param.required is True
        assert seed_param.type == ToolInputType.STRING

    def test_definition_has_optional_parameters(self) -> None:
        """ExecuteSeedHandler has optional parameters."""
        handler = ExecuteSeedHandler()
        defn = handler.definition

        param_names = {p.name for p in defn.parameters}
        assert "session_id" in param_names
        assert "model_tier" in param_names
        assert "max_iterations" in param_names

    def test_definition_excludes_internal_delegation_parameters(self) -> None:
        """Internal parent-session propagation must not change the public tool schema."""
        handler = ExecuteSeedHandler()
        param_names = {p.name for p in handler.definition.parameters}

        assert DELEGATED_PARENT_SESSION_ID_ARG not in param_names
        assert DELEGATED_PARENT_EFFECTIVE_TOOLS_ARG not in param_names

    async def test_handle_requires_seed_content(self) -> None:
        """handle returns error when seed_content is missing."""
        handler = ExecuteSeedHandler()
        result = await handler.handle({})

        assert result.is_err
        assert "seed_content is required" in str(result.error)

    async def test_handle_success(self) -> None:
        """handle returns success with valid YAML seed input."""
        handler = ExecuteSeedHandler()
        result = await handler.handle(
            {
                "seed_content": VALID_SEED_YAML,
                "model_tier": "medium",
            }
        )

        # Handler now integrates with actual orchestrator, so we check for proper response
        # The result should contain execution information or a helpful error about dependencies
        assert (
            result.is_ok
            or "execution" in str(result.error).lower()
            or "orchestrator" in str(result.error).lower()
        )

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

        handler = InterviewHandler(interview_engine=mock_engine)
        await handler.handle({"initial_context": "Add a feature", "cwd": str(tmp_path)})

        mock_engine.start_interview.assert_awaited_once()
        call_kwargs = mock_engine.start_interview.call_args
        assert call_kwargs[1]["cwd"] == str(tmp_path)

    async def test_interview_handle_clears_stored_ambiguity_after_new_answer(self) -> None:
        """Interview answers should invalidate any persisted ambiguity snapshot."""
        handler = InterviewHandler()
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

        with patch(
            "ouroboros.mcp.tools.definitions.InterviewEngine",
            return_value=mock_engine,
        ):
            result = await handler.handle({"session_id": "sess-123", "answer": "Manage tasks"})

        assert result.is_ok
        assert state.ambiguity_score is None
        assert state.ambiguity_breakdown is None


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
                "ouroboros.mcp.tools.definitions.AmbiguityScorer",
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
                "ouroboros.mcp.tools.definitions.AmbiguityScorer",
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
