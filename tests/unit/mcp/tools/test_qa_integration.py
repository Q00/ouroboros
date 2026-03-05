"""Tests for QA integration into ExecuteSeedHandler and EvolveStepHandler.

Verifies that:
1. QA is called after successful execution (when skip_qa=False)
2. QA verdict is appended to response text
3. QA meta is included in response meta
4. skip_qa=True bypasses QA
5. QA failure degrades gracefully (no crash, just no QA in output)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.core.types import Result
from ouroboros.mcp.tools.definitions import EvolveStepHandler, ExecuteSeedHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult

# ---------------------------------------------------------------------------
# Fixtures: minimal seed YAML
# ---------------------------------------------------------------------------

VALID_SEED_YAML = """\
goal: Test task
constraints:
  - Python 3.14+
acceptance_criteria:
  - All tests pass
  - No lint errors
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
  seed_id: test-seed-qa
  version: "1.0.0"
  created_at: "2024-01-01T00:00:00Z"
  ambiguity_score: 0.1
  interview_id: null
"""

# Fake QA result that QAHandler.handle() would return
FAKE_QA_RESULT = Result.ok(
    MCPToolResult(
        content=(
            MCPContentItem(
                type=ContentType.TEXT,
                text=(
                    "QA Verdict [Iteration 1]\n"
                    "============================================================\n"
                    "Session: qa-test123\n"
                    "Score: 0.85 / 1.00 [PASS]\n"
                    "Verdict: pass\n"
                    "Threshold: 0.80\n"
                    "\n"
                    "Loop Action: done"
                ),
            ),
        ),
        is_error=False,
        meta={
            "qa_session_id": "qa-test123",
            "iteration": 1,
            "score": 0.85,
            "verdict": "pass",
            "loop_action": "done",
            "pass_threshold": 0.80,
            "passed": True,
            "dimensions": {},
            "differences": [],
            "suggestions": [],
            "reasoning": "",
            "iteration_entry": {
                "iteration": 1,
                "score": 0.85,
                "verdict": "pass",
                "loop_action": "done",
            },
        },
    )
)


# ---------------------------------------------------------------------------
# ExecuteSeedHandler tests
# ---------------------------------------------------------------------------


@dataclass
class FakeExecResult:
    """Minimal orchestrator result for testing."""

    success: bool = True
    session_id: str = "sess-test"
    execution_id: str = "exec-test"
    messages_processed: int = 5
    duration_seconds: float = 1.0
    final_message: str = "All tests passed successfully."
    summary: dict = field(default_factory=dict)


class TestExecuteSeedHandlerQA:
    """Test QA integration in ExecuteSeedHandler."""

    async def test_qa_called_on_success(self) -> None:
        """QA is called after successful execution and result includes verdict."""
        handler = ExecuteSeedHandler()

        fake_exec = FakeExecResult()
        mock_runner = AsyncMock()
        mock_runner.execute_seed = AsyncMock(return_value=Result.ok(fake_exec))

        with (
            patch("ouroboros.mcp.tools.definitions.ClaudeAgentAdapter"),
            patch("ouroboros.mcp.tools.definitions.EventStore") as mock_es_cls,
            patch(
                "ouroboros.mcp.tools.definitions.OrchestratorRunner",
                return_value=mock_runner,
            ),
            patch(
                "ouroboros.mcp.tools.qa.QAHandler.handle",
                new_callable=AsyncMock,
                return_value=FAKE_QA_RESULT,
            ) as mock_qa_handle,
        ):
            mock_es_cls.return_value.initialize = AsyncMock()

            result = await handler.handle({"seed_content": VALID_SEED_YAML})

        assert result.is_ok, f"Expected ok, got: {result.error}"

        # QA handler was called
        mock_qa_handle.assert_awaited_once()
        qa_args = mock_qa_handle.call_args[0][0]
        assert qa_args["artifact_type"] == "test_output"
        assert "All tests pass" in qa_args["quality_bar"]
        assert "No lint errors" in qa_args["quality_bar"]

        # Response text includes QA verdict
        text = result.value.content[0].text
        assert "QA Verdict" in text
        assert "Score: 0.85" in text

        # Meta includes QA
        assert "qa" in result.value.meta
        assert result.value.meta["qa"]["score"] == 0.85
        assert result.value.meta["qa"]["verdict"] == "pass"

    async def test_skip_qa_bypasses_qa(self) -> None:
        """skip_qa=True prevents QA from running."""
        handler = ExecuteSeedHandler()

        fake_exec = FakeExecResult()
        mock_runner = AsyncMock()
        mock_runner.execute_seed = AsyncMock(return_value=Result.ok(fake_exec))

        with (
            patch("ouroboros.mcp.tools.definitions.ClaudeAgentAdapter"),
            patch("ouroboros.mcp.tools.definitions.EventStore") as mock_es_cls,
            patch(
                "ouroboros.mcp.tools.definitions.OrchestratorRunner",
                return_value=mock_runner,
            ),
            patch(
                "ouroboros.mcp.tools.qa.QAHandler.handle",
                new_callable=AsyncMock,
            ) as mock_qa_handle,
        ):
            mock_es_cls.return_value.initialize = AsyncMock()

            result = await handler.handle(
                {
                    "seed_content": VALID_SEED_YAML,
                    "skip_qa": True,
                }
            )

        assert result.is_ok
        mock_qa_handle.assert_not_awaited()
        assert "qa" not in result.value.meta

    async def test_qa_not_called_on_failure(self) -> None:
        """QA is not called when execution fails."""
        handler = ExecuteSeedHandler()

        fake_exec = FakeExecResult(success=False, final_message="Build failed")
        mock_runner = AsyncMock()
        mock_runner.execute_seed = AsyncMock(return_value=Result.ok(fake_exec))

        with (
            patch("ouroboros.mcp.tools.definitions.ClaudeAgentAdapter"),
            patch("ouroboros.mcp.tools.definitions.EventStore") as mock_es_cls,
            patch(
                "ouroboros.mcp.tools.definitions.OrchestratorRunner",
                return_value=mock_runner,
            ),
            patch(
                "ouroboros.mcp.tools.qa.QAHandler.handle",
                new_callable=AsyncMock,
            ) as mock_qa_handle,
        ):
            mock_es_cls.return_value.initialize = AsyncMock()

            result = await handler.handle({"seed_content": VALID_SEED_YAML})

        assert result.is_ok  # returns ok with is_error=True in MCPToolResult
        mock_qa_handle.assert_not_awaited()

    async def test_qa_failure_degrades_gracefully(self) -> None:
        """If QA handler returns error, execution result is still returned."""
        handler = ExecuteSeedHandler()

        fake_exec = FakeExecResult()
        mock_runner = AsyncMock()
        mock_runner.execute_seed = AsyncMock(return_value=Result.ok(fake_exec))

        from ouroboros.mcp.errors import MCPToolError

        qa_error = Result.err(MCPToolError("LLM failed", tool_name="ouroboros_qa"))

        with (
            patch("ouroboros.mcp.tools.definitions.ClaudeAgentAdapter"),
            patch("ouroboros.mcp.tools.definitions.EventStore") as mock_es_cls,
            patch(
                "ouroboros.mcp.tools.definitions.OrchestratorRunner",
                return_value=mock_runner,
            ),
            patch(
                "ouroboros.mcp.tools.qa.QAHandler.handle",
                new_callable=AsyncMock,
                return_value=qa_error,
            ),
        ):
            mock_es_cls.return_value.initialize = AsyncMock()

            result = await handler.handle({"seed_content": VALID_SEED_YAML})

        # Execution result is still returned successfully, just without QA
        assert result.is_ok
        assert "qa" not in result.value.meta
        assert "Seed Execution SUCCESS" in result.value.content[0].text

    def test_derive_quality_bar(self) -> None:
        """_derive_quality_bar extracts AC from seed."""
        import yaml

        from ouroboros.core.seed import Seed

        seed = Seed.from_dict(yaml.safe_load(VALID_SEED_YAML))
        bar = ExecuteSeedHandler._derive_quality_bar(seed)

        assert "All tests pass" in bar
        assert "No lint errors" in bar
        assert "acceptance criteria" in bar.lower()


# ---------------------------------------------------------------------------
# EvolveStepHandler tests
# ---------------------------------------------------------------------------


class FakeConvergenceSignal:
    ontology_similarity = 0.85
    reason = "improving"
    converged = False
    failed_acs: list = []


class FakeEvalSummary:
    final_approved = True
    score = 0.90
    drift_score = 0.05
    failure_reason = None
    ac_results = []


class FakeGeneration:
    generation_number = 3
    phase = MagicMock(value="reflect")
    execution_output = "All 5 tests passed."
    evaluation_summary = FakeEvalSummary()
    wonder_output = None
    validation_output = None
    ontology_delta = None


class FakeLineage:
    lineage_id = "lin_test"
    current_generation = 3


class FakeStepResult:
    action = MagicMock(value="continue")
    generation_result = FakeGeneration()
    convergence_signal = FakeConvergenceSignal()
    lineage = FakeLineage()
    next_generation = 4


class TestEvolveStepHandlerQA:
    """Test QA integration in EvolveStepHandler."""

    async def test_qa_called_on_continue_with_execute(self) -> None:
        """QA is called when action=continue and execute=True."""
        mock_loop = AsyncMock()
        mock_loop.event_store.initialize = AsyncMock()
        mock_loop.evolve_step = AsyncMock(return_value=Result.ok(FakeStepResult()))

        handler = EvolveStepHandler(evolutionary_loop=mock_loop)

        with patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ) as mock_qa:
            result = await handler.handle(
                {
                    "lineage_id": "lin_test",
                    "seed_content": VALID_SEED_YAML,
                    "execute": True,
                }
            )

        assert result.is_ok, f"Expected ok, got: {result.error}"
        mock_qa.assert_awaited_once()

        # QA verdict in text
        text = result.value.content[0].text
        assert "### QA Verdict" in text
        assert "Score: 0.85" in text

        # QA in meta
        assert "qa" in result.value.meta
        assert result.value.meta["qa"]["score"] == 0.85

    async def test_skip_qa_bypasses_evolve_qa(self) -> None:
        """skip_qa=True prevents QA in evolve_step."""
        mock_loop = AsyncMock()
        mock_loop.event_store.initialize = AsyncMock()
        mock_loop.evolve_step = AsyncMock(return_value=Result.ok(FakeStepResult()))

        handler = EvolveStepHandler(evolutionary_loop=mock_loop)

        with patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
        ) as mock_qa:
            result = await handler.handle(
                {
                    "lineage_id": "lin_test",
                    "execute": True,
                    "skip_qa": True,
                }
            )

        assert result.is_ok
        mock_qa.assert_not_awaited()
        assert "qa" not in result.value.meta

    async def test_no_qa_when_execute_false(self) -> None:
        """QA is not called when execute=False (ontology-only mode)."""
        mock_loop = AsyncMock()
        mock_loop.event_store.initialize = AsyncMock()
        mock_loop.evolve_step = AsyncMock(return_value=Result.ok(FakeStepResult()))

        handler = EvolveStepHandler(evolutionary_loop=mock_loop)

        with patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
        ) as mock_qa:
            result = await handler.handle(
                {
                    "lineage_id": "lin_test",
                    "execute": False,
                }
            )

        assert result.is_ok
        mock_qa.assert_not_awaited()

    async def test_qa_uses_seed_ac_for_quality_bar(self) -> None:
        """When seed is provided, QA quality bar is derived from AC."""
        mock_loop = AsyncMock()
        mock_loop.event_store.initialize = AsyncMock()
        mock_loop.evolve_step = AsyncMock(return_value=Result.ok(FakeStepResult()))

        handler = EvolveStepHandler(evolutionary_loop=mock_loop)

        with patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ) as mock_qa:
            await handler.handle(
                {
                    "lineage_id": "lin_test",
                    "seed_content": VALID_SEED_YAML,
                    "execute": True,
                }
            )

        qa_args = mock_qa.call_args[0][0]
        assert "All tests pass" in qa_args["quality_bar"]
        assert "No lint errors" in qa_args["quality_bar"]

    async def test_qa_without_seed_uses_default_bar(self) -> None:
        """Without seed, QA uses default quality bar."""
        mock_loop = AsyncMock()
        mock_loop.event_store.initialize = AsyncMock()
        mock_loop.evolve_step = AsyncMock(return_value=Result.ok(FakeStepResult()))

        handler = EvolveStepHandler(evolutionary_loop=mock_loop)

        with patch(
            "ouroboros.mcp.tools.qa.QAHandler.handle",
            new_callable=AsyncMock,
            return_value=FAKE_QA_RESULT,
        ) as mock_qa:
            await handler.handle(
                {
                    "lineage_id": "lin_test",
                    "execute": True,
                }
            )

        qa_args = mock_qa.call_args[0][0]
        assert "improve upon previous" in qa_args["quality_bar"]
