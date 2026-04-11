"""Tests for EvaluateHandler — adapter creation parameters (max_turns).

Regression coverage for the bug where ooo evaluate always fails with
``error_max_turns`` or ``exit code 1`` when invoked inside an active Claude
Code session:

  - Bug: ``max_turns=1`` caused the evaluator subprocess to hit the agentic
    loop limit on the very first tool call (``Read`` to inspect a spec file).
    Each AC check requires at least one file read, so ``max_turns=1`` was
    never sufficient.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
)
from ouroboros.mcp.tools.evaluation_handlers import EvaluateHandler
from ouroboros.providers.base import CompletionResponse, UsageInfo

# ── Helpers ──────────────────────────────────────────────────────────────────

_MINIMAL_SEED = """\
goal: Verify the system works correctly.
acceptance_criteria:
  - AC-01: Output exists
"""

_BASE_ARGUMENTS: dict = {
    "session_id": "test-exec-001",
    "artifact": "stub evaluation artifact",
    "artifact_type": "docs",
    "seed_content": _MINIMAL_SEED,  # goal present → skips EventStore DB lookup
    "acceptance_criterion": None,
    "working_dir": None,
    "trigger_consensus": False,
}


def _make_mock_adapter() -> MagicMock:
    """Return a mock LLMAdapter with a successful complete() response."""
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=Result.ok(
            CompletionResponse(
                content="{}",
                model="test-model",
                usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
            )
        )
    )
    return adapter


def _make_approved_eval_result() -> EvaluationResult:
    """Return a minimal approved EvaluationResult for pipeline mocking."""
    mechanical = MechanicalResult(
        passed=True,
        checks=(
            CheckResult(
                check_type=CheckType.LINT,
                passed=True,
                message="skipped",
                details={"skipped": True},
            ),
        ),
    )
    semantic = SemanticResult(
        score=0.9,
        ac_compliance=True,
        goal_alignment=0.9,
        drift_score=0.05,
        uncertainty=0.1,
        reasoning="All ACs verified.",
    )
    return EvaluationResult(
        execution_id="test-exec-001",
        final_approved=True,
        stage1_result=mechanical,
        stage2_result=semantic,
        stage3_result=None,
    )


def _patch_pipeline():
    """Context manager: patch EvaluationPipeline to return an approved result."""
    mock_pipeline = MagicMock()
    mock_pipeline.evaluate = AsyncMock(return_value=Result.ok(_make_approved_eval_result()))
    return patch(
        "ouroboros.evaluation.EvaluationPipeline",
        return_value=mock_pipeline,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestEvaluateHandlerAdapterCreation:
    """Verify that the handler creates an adapter suited for headless evaluation."""

    async def test_creates_adapter_with_sufficient_max_turns(self):
        """Adapter must allow enough turns for the evaluator to read spec files.

        Regression: max_turns=1 caused error_max_turns on the first tool call.
        Evaluating N acceptance criteria requires at least N Read tool calls,
        so max_turns must be well above 1.
        """
        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return _make_mock_adapter()

        handler = EvaluateHandler(llm_adapter=None)

        with (
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
                side_effect=_capture,
            ),
            _patch_pipeline(),
        ):
            await handler.handle(_BASE_ARGUMENTS)

        assert "max_turns" in captured, "create_llm_adapter was not called"
        assert captured["max_turns"] >= 10, (
            f"max_turns={captured['max_turns']} is too low — the evaluator needs "
            "at least one turn per AC file read. Use max_turns >= 10."
        )

    async def test_injected_adapter_is_ignored_for_evaluation(self):
        """Evaluation ALWAYS creates a fresh adapter, even when one is injected.

        Regression for issue #305 / related max_turns fix:

        The shared adapter wired up in ``build_mcp_server``
        (``mcp/server/adapter.py``) is constructed with ``max_turns=1`` because
        interview and seed-generation paths only need a single-shot response.
        If the evaluator reuses that adapter, the very first ``Read`` tool
        call issued by the Stage 2 semantic evaluator hits ``error_max_turns``
        and surfaces as ``Command failed with exit code 1`` at the MCP tool
        boundary.

        To prevent that, the handler now ignores ``self.llm_adapter`` and
        always constructs a fresh adapter with ``max_turns=20``.
        """
        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return _make_mock_adapter()

        # Simulate the shared adapter that build_mcp_server would inject.
        shared_adapter = _make_mock_adapter()
        handler = EvaluateHandler(llm_adapter=shared_adapter)

        with (
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
                side_effect=_capture,
            ),
            _patch_pipeline(),
        ):
            await handler.handle(_BASE_ARGUMENTS)

        # Fresh adapter was created with a sufficient max_turns budget, even
        # though one was already injected on the handler.
        assert "max_turns" in captured, (
            "create_llm_adapter was not called — the handler must build a "
            "fresh adapter for evaluation instead of reusing the shared "
            "interview-tuned adapter."
        )
        assert captured["max_turns"] >= 10, f"max_turns={captured['max_turns']} is too low."

    async def test_injected_max_turns_1_adapter_does_not_leak(self):
        """Regression for issue #305: injecting a max_turns=1 adapter must not
        cause evaluation to inherit the pathological ceiling.

        This guards against a future refactor re-introducing the
        ``self.llm_adapter or create_llm_adapter(...)`` short-circuit pattern.
        """
        # The shared MCP adapter is built with max_turns=1; verify that the
        # handler does not forward that adapter into the evaluation pipeline.
        low_turn_adapter = _make_mock_adapter()
        low_turn_adapter._max_turns = 1  # mirror ClaudeCodeAdapter attribute
        handler = EvaluateHandler(llm_adapter=low_turn_adapter)

        captured_pipeline_adapters: list = []

        def _capture_pipeline(llm_adapter, config):  # noqa: ARG001
            mock_pipeline = MagicMock()
            mock_pipeline.evaluate = AsyncMock(
                return_value=Result.ok(_make_approved_eval_result()),
            )
            captured_pipeline_adapters.append(llm_adapter)
            return mock_pipeline

        with (
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
                return_value=_make_mock_adapter(),
            ),
            patch(
                "ouroboros.evaluation.EvaluationPipeline",
                side_effect=_capture_pipeline,
            ),
        ):
            await handler.handle(_BASE_ARGUMENTS)

        assert captured_pipeline_adapters, "EvaluationPipeline was not constructed"
        pipeline_adapter = captured_pipeline_adapters[0]
        assert pipeline_adapter is not low_turn_adapter, (
            "EvaluationPipeline was constructed with the injected max_turns=1 "
            "adapter — this is the bug described in issue #305. The handler "
            "must build a fresh adapter with a higher max_turns budget."
        )
