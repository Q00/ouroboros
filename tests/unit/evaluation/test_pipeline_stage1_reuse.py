"""Pipeline-level regression tests for stage1_result= reuse (#422).

These tests exercise the real EvaluationPipeline.evaluate() method —
not handler-level mocks — to ensure future Stage 1 changes cannot
break the shared-result invariant silently.

Requested by @Q00 in PR #422 review.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    CheckResult,
    CheckType,
    EvaluationContext,
    MechanicalResult,
    SemanticResult,
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig


def _make_context(*, execution_id: str = "test-exec") -> EvaluationContext:
    """Build a minimal EvaluationContext for testing."""
    return EvaluationContext(
        execution_id=execution_id,
        seed_id="seed-1",
        current_ac="Test AC",
        artifact="def f(): pass",
        artifact_type="code",
        goal="Test goal",
        constraints=(),
        trigger_consensus=False,
    )


def _passing_stage1() -> MechanicalResult:
    return MechanicalResult(
        passed=True,
        checks=(CheckResult(check_type=CheckType.LINT, passed=True, message="ok"),),
    )


def _failing_stage1() -> MechanicalResult:
    return MechanicalResult(
        passed=False,
        checks=(CheckResult(check_type=CheckType.LINT, passed=False, message="lint failed"),),
    )


def _passing_semantic() -> SemanticResult:
    return SemanticResult(
        score=0.9,
        ac_compliance=True,
        goal_alignment=0.9,
        drift_score=0.1,
        uncertainty=0.1,
        reasoning="AC met",
        reward_hacking_risk=0.0,
        questions_used=(),
        evidence=(),
    )


class TestStage1ResultReuse:
    """Verify the stage1_result= parameter on EvaluationPipeline.evaluate()."""

    @pytest.fixture()
    def mock_llm(self) -> AsyncMock:
        return AsyncMock()

    @pytest.fixture()
    def pipeline(self, mock_llm: AsyncMock) -> EvaluationPipeline:
        return EvaluationPipeline(mock_llm, PipelineConfig())

    async def test_injected_passing_stage1_skips_mechanical_verify(
        self, pipeline: EvaluationPipeline
    ) -> None:
        """When stage1_result is provided and passing, MechanicalVerifier.verify
        must NOT be called, and Stage 2 should proceed.
        """
        stage1 = _passing_stage1()
        semantic_return = (_passing_semantic(), [])

        with (
            patch(
                "ouroboros.evaluation.mechanical.MechanicalVerifier.verify",
                new_callable=AsyncMock,
            ) as mock_verify,
            patch(
                "ouroboros.evaluation.semantic.SemanticEvaluator.evaluate",
                new_callable=AsyncMock,
                return_value=Result.ok(semantic_return),
            ) as mock_semantic,
        ):
            result = await pipeline.evaluate(
                _make_context(),
                stage1_result=stage1,
            )

        mock_verify.assert_not_called()
        mock_semantic.assert_called_once()
        assert result.is_ok
        assert result.value.final_approved is True
        assert result.value.stage1_result is stage1

    async def test_no_stage1_result_calls_mechanical_verify(
        self, pipeline: EvaluationPipeline
    ) -> None:
        """When stage1_result is None (default), MechanicalVerifier.verify
        MUST be called.
        """
        mech_return = (_passing_stage1(), [])
        semantic_return = (_passing_semantic(), [])

        with (
            patch(
                "ouroboros.evaluation.mechanical.MechanicalVerifier.verify",
                new_callable=AsyncMock,
                return_value=Result.ok(mech_return),
            ) as mock_verify,
            patch(
                "ouroboros.evaluation.semantic.SemanticEvaluator.evaluate",
                new_callable=AsyncMock,
                return_value=Result.ok(semantic_return),
            ),
        ):
            result = await pipeline.evaluate(_make_context())

        mock_verify.assert_called_once()
        assert result.is_ok
        assert result.value.final_approved is True

    async def test_injected_failing_stage1_causes_early_exit(
        self, pipeline: EvaluationPipeline
    ) -> None:
        """When stage1_result is provided and failing, pipeline exits early
        with final_approved=False and SemanticEvaluator.evaluate is NOT called.
        """
        stage1 = _failing_stage1()

        with (
            patch(
                "ouroboros.evaluation.mechanical.MechanicalVerifier.verify",
                new_callable=AsyncMock,
            ) as mock_verify,
            patch(
                "ouroboros.evaluation.semantic.SemanticEvaluator.evaluate",
                new_callable=AsyncMock,
            ) as mock_semantic,
        ):
            result = await pipeline.evaluate(
                _make_context(),
                stage1_result=stage1,
            )

        mock_verify.assert_not_called()
        mock_semantic.assert_not_called()
        assert result.is_ok
        assert result.value.final_approved is False
        assert result.value.stage1_result is stage1

    async def test_injected_passing_stage1_allows_stage2(
        self, pipeline: EvaluationPipeline
    ) -> None:
        """When stage1_result is provided and passing, Stage 2
        (SemanticEvaluator.evaluate) MUST be called.
        """
        stage1 = _passing_stage1()
        semantic_return = (_passing_semantic(), [])

        with (
            patch(
                "ouroboros.evaluation.semantic.SemanticEvaluator.evaluate",
                new_callable=AsyncMock,
                return_value=Result.ok(semantic_return),
            ) as mock_semantic,
        ):
            result = await pipeline.evaluate(
                _make_context(),
                stage1_result=stage1,
            )

        mock_semantic.assert_called_once()
        assert result.is_ok
        assert result.value.stage2_result is not None
