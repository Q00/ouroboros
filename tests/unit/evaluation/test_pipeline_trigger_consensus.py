"""Integration test: trigger_consensus bypasses Stage 2 threshold gate.

Verifies the reporter's exact scenario from #230:
  Stage 2 scores 0.72 → normally REJECTED → with trigger_consensus=True → Stage 3 runs.

See: https://github.com/Q00/ouroboros/issues/230
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.models import (
    ConsensusResult,
    EvaluationContext,
    SemanticResult,
    Vote,
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
    async def test_low_score_approved_with_trigger_consensus(self) -> None:
        """Score 0.72 + trigger_consensus=True → Stage 3 runs → APPROVED."""
        pipeline = _make_pipeline(stage2_score=0.72, consensus_approved=True)
        context = EvaluationContext(
            execution_id="e1",
            seed_id="s1",
            current_ac="ac1",
            artifact="code",
            trigger_consensus=True,
        )

        result = await pipeline.evaluate(context)
        assert result.is_ok
        assert result.value.final_approved is True
        pipeline._consensus.evaluate.assert_called_once()

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
