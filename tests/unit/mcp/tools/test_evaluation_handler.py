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

        handler = EvaluateHandler()

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

    async def test_always_creates_fresh_adapter(self):
        """Evaluation always creates its own adapter via create_llm_adapter.

        The handler owns its adapter lifecycle — no external adapter is
        injected. This test verifies create_llm_adapter is called with a
        sufficient max_turns budget for multi-turn spec file reads.
        """
        captured: dict = {}

        def _capture(**kwargs):
            captured.update(kwargs)
            return _make_mock_adapter()

        handler = EvaluateHandler()

        with (
            patch(
                "ouroboros.mcp.tools.evaluation_handlers.create_llm_adapter",
                side_effect=_capture,
            ),
            _patch_pipeline(),
        ):
            await handler.handle(_BASE_ARGUMENTS)

        assert "max_turns" in captured, (
            "create_llm_adapter was not called — the handler must build a "
            "fresh adapter for evaluation."
        )
        assert captured["max_turns"] >= 10, f"max_turns={captured['max_turns']} is too low."

    async def test_adapter_has_sufficient_turns_in_pipeline(self):
        """The adapter passed to EvaluationPipeline has sufficient max_turns.

        Guards against a future refactor lowering the turn budget below
        what the semantic evaluator needs for spec file reads.
        """
        handler = EvaluateHandler()

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
