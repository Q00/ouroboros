"""Pipeline-level tests for the reward-hacking veto gate (PR-H).

The final Stage 2 approval gate now vetoes an otherwise-passing artifact when
``reward_hacking_risk >= REWARD_HACKING_VETO_THRESHOLD`` (0.7).  These tests
exercise the real ``EvaluationPipeline.evaluate()`` no-consensus path with a
mocked semantic stage, plus the ``EvaluationResult.failure_reason`` surface
that carries the veto explanation to the per-AC checklist.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    REWARD_HACKING_VETO_THRESHOLD,
    CheckResult,
    CheckType,
    EvaluationContext,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig


def _make_context() -> EvaluationContext:
    return EvaluationContext(
        execution_id="veto-exec",
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


def _semantic(*, reward_hacking_risk: float) -> SemanticResult:
    """A Stage 2 result that passes score + compliance, varying only risk."""
    return SemanticResult(
        score=0.95,
        ac_compliance=True,
        goal_alignment=0.9,
        drift_score=0.1,
        uncertainty=0.1,
        reasoning="AC met",
        reward_hacking_risk=reward_hacking_risk,
    )


def _make_pipeline() -> EvaluationPipeline:
    # Stage 3 disabled so the no-consensus gate (the veto site) is exercised.
    return EvaluationPipeline(AsyncMock(), PipelineConfig(stage3_enabled=False))


async def _run_with_risk(risk: float) -> EvaluationResult:
    pipeline = _make_pipeline()
    with patch(
        "ouroboros.evaluation.semantic.SemanticEvaluator.evaluate",
        new_callable=AsyncMock,
        return_value=Result.ok((_semantic(reward_hacking_risk=risk), [])),
    ):
        result = await pipeline.evaluate(_make_context(), stage1_result=_passing_stage1())
    assert result.is_ok
    return result.value


class TestRewardHackingVeto:
    async def test_high_risk_vetoes_passing_artifact(self) -> None:
        """reward_hacking_risk=0.9 with a passing score is NOT approved."""
        eval_result = await _run_with_risk(0.9)

        assert eval_result.final_approved is False
        # Stage 2 itself reported compliance + a high score — the block is the veto.
        assert eval_result.stage2_result is not None
        assert eval_result.stage2_result.ac_compliance is True
        assert eval_result.stage2_result.score >= 0.8

    async def test_veto_reason_explains_the_block(self) -> None:
        """The failure reason must name the reward-hacking veto, not 'Unknown'."""
        eval_result = await _run_with_risk(0.9)
        reason = eval_result.failure_reason

        assert reason is not None
        assert "reward-hacking" in reason.lower()
        assert "veto" in reason.lower()
        assert "0.90" in reason  # the offending risk value is surfaced
        assert reason != "Unknown failure"

    async def test_zero_risk_is_approved(self) -> None:
        """reward_hacking_risk=0.0 keeps today's behavior: approved."""
        eval_result = await _run_with_risk(0.0)

        assert eval_result.final_approved is True
        assert eval_result.failure_reason is None

    async def test_threshold_boundary_is_inclusive(self) -> None:
        """risk == threshold vetoes; risk just below the threshold approves."""
        vetoed = await _run_with_risk(REWARD_HACKING_VETO_THRESHOLD)
        assert vetoed.final_approved is False

        approved = await _run_with_risk(REWARD_HACKING_VETO_THRESHOLD - 0.01)
        assert approved.final_approved is True


class TestFailureReasonSurface:
    """The EvaluationResult.failure_reason property is what ACCheckItem reads."""

    def test_property_surfaces_veto_for_compliant_high_risk_result(self) -> None:
        result = EvaluationResult(
            execution_id="e1",
            stage1_result=_passing_stage1(),
            stage2_result=_semantic(reward_hacking_risk=0.85),
            final_approved=False,
        )
        reason = result.failure_reason
        assert reason is not None
        assert "reward-hacking" in reason.lower()
        assert "0.85" in reason

    def test_property_returns_none_when_approved(self) -> None:
        result = EvaluationResult(
            execution_id="e1",
            stage1_result=_passing_stage1(),
            stage2_result=_semantic(reward_hacking_risk=0.85),
            final_approved=True,
        )
        assert result.failure_reason is None
