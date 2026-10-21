"""Integration test: trigger_consensus asks Stage 3 for a second opinion.

The reporter's scenario from #230 (Stage 2 scores 0.72, trigger_consensus=True)
still runs Stage 3. Since #2449, model review is advisory: Stage 3 can withhold
approval and its votes are reported, but a consensus approval cannot lift a
Stage 2 block or grant acceptance without executed Stage 1 evidence.

See: https://github.com/Q00/ouroboros/issues/230
See: https://github.com/Q00/ouroboros/issues/2449
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    AcceptanceState,
    CheckResult,
    CheckType,
    ConsensusResult,
    EvaluationContext,
    MechanicalResult,
    SemanticResult,
    Vote,
)

EXECUTED_STAGE1 = MechanicalResult(
    passed=True,
    checks=(CheckResult(check_type=CheckType.TEST, passed=True, message="ok", executed=True),),
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig


def _make_semantic_result(score: float, ac_compliance: bool) -> SemanticResult:
    return SemanticResult(
        score=score,
        ac_compliance=ac_compliance,
        goal_alignment=score,
        drift_score=0.1,
        uncertainty=0.2,
        reasoning="test",
    )


def _make_consensus_result(approved: bool) -> ConsensusResult:
    votes = (
        Vote(model="m1", approved=approved, confidence=0.9, reasoning="test"),
        Vote(model="m2", approved=approved, confidence=0.8, reasoning="test"),
        Vote(model="m3", approved=not approved, confidence=0.6, reasoning="dissent"),
    )
    return ConsensusResult(
        approved=approved,
        votes=votes,
        majority_ratio=0.67 if approved else 0.33,
    )


def _make_pipeline(
    *,
    stage2_score: float = 0.72,
    stage2_compliance: bool = True,
    consensus_approved: bool = True,
) -> EvaluationPipeline:
    """Build a pipeline with mocked stage evaluators."""
    config = PipelineConfig(stage1_enabled=False, stage2_enabled=True, stage3_enabled=True)
    pipeline = EvaluationPipeline(llm_adapter=MagicMock(), config=config)

    # Mock Stage 2
    semantic_result = _make_semantic_result(stage2_score, stage2_compliance)
    pipeline._semantic = MagicMock()
    pipeline._semantic.evaluate = AsyncMock(return_value=Result.ok((semantic_result, [])))

    # Mock Stage 3
    consensus_result = _make_consensus_result(consensus_approved)
    pipeline._consensus = MagicMock()
    pipeline._consensus.evaluate = AsyncMock(return_value=Result.ok((consensus_result, [])))

    return pipeline


class TestTriggerConsensusIntegration:
    """Pipeline integration: trigger_consensus overrides Stage 2 gate."""

    @pytest.mark.asyncio
    async def test_low_score_rejected_without_trigger(self) -> None:
        """Score 0.72 < 0.8 threshold → REJECTED, Stage 3 never called."""
        pipeline = _make_pipeline(stage2_score=0.72)
        context = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=False,
        )

        result = await pipeline.evaluate(context)
        assert result.is_ok
        assert result.value.final_approved is False
        pipeline._consensus.evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_low_score_consensus_approval_cannot_lift_stage2_block(self) -> None:
        """Score 0.72 + trigger_consensus=True → Stage 3 runs, but approval is withheld."""
        pipeline = _make_pipeline(stage2_score=0.72, consensus_approved=True)
        context = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=True,
        )

        result = await pipeline.evaluate(context, stage1_result=EXECUTED_STAGE1)
        assert result.is_ok
        pipeline._consensus.evaluate.assert_called_once()
        assert result.value.stage3_result is not None
        assert result.value.stage3_result.approved is True
        assert result.value.final_approved is False
        assert result.value.acceptance_state is AcceptanceState.REJECTED
        assert "semantic score 0.72" in (result.value.failure_reason or "")

    @pytest.mark.asyncio
    async def test_consensus_approval_without_executed_evidence_is_unverified(self) -> None:
        """A compliant Stage 2 plus an approving Stage 3 still cannot grant acceptance."""
        pipeline = _make_pipeline(stage2_score=0.9, consensus_approved=True)
        context = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=True,
        )

        result = await pipeline.evaluate(context)
        assert result.is_ok
        pipeline._consensus.evaluate.assert_called_once()
        assert result.value.final_approved is False
        assert result.value.acceptance_state is AcceptanceState.UNVERIFIED

    @pytest.mark.asyncio
    async def test_consensus_approval_with_executed_evidence_is_approved(self) -> None:
        """Executed evidence grants; neither model stage withheld."""
        pipeline = _make_pipeline(stage2_score=0.9, consensus_approved=True)
        context = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=True,
        )

        result = await pipeline.evaluate(context, stage1_result=EXECUTED_STAGE1)
        assert result.is_ok
        assert result.value.final_approved is True
        assert result.value.acceptance_state is AcceptanceState.APPROVED

    @pytest.mark.asyncio
    async def test_compliance_fail_bypassed_with_trigger_consensus(self) -> None:
        """ac_compliance=False + trigger_consensus=True → Stage 3 still runs."""
        pipeline = _make_pipeline(
            stage2_score=0.50,
            stage2_compliance=False,
            consensus_approved=False,
        )
        context = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=True,
        )

        result = await pipeline.evaluate(context)
        assert result.is_ok
        # Stage 3 ran (consensus decided, even if it rejected)
        pipeline._consensus.evaluate.assert_called_once()
        # Consensus rejected → final is False
        assert result.value.final_approved is False

    @pytest.mark.asyncio
    async def test_compliance_fail_rejected_without_trigger(self) -> None:
        """ac_compliance=False + trigger_consensus=False → early return, no Stage 3."""
        pipeline = _make_pipeline(stage2_score=0.50, stage2_compliance=False)
        context = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=False,
        )

        result = await pipeline.evaluate(context)
        assert result.is_ok
        assert result.value.final_approved is False
        pipeline._consensus.evaluate.assert_not_called()
