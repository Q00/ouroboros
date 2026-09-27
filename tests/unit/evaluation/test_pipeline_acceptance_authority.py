"""Acceptance authority: executed verification grants, model review only withholds.

Issue #2449. Stage 2 (semantic) and Stage 3 (consensus) are advisory: they can
block approval and supply feedback, never grant it. Approval requires Stage 1
to have run at least one configured check with every check passing. Without
that, the result is unverified and the model review is attached as feedback.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.mechanical import MechanicalConfig
from ouroboros.evaluation.models import (
    AcceptanceState,
    CheckResult,
    CheckType,
    ConsensusResult,
    EvaluationContext,
    EvaluationResult,
    MechanicalResult,
    SemanticResult,
    Vote,
    decide_final_approval,
)
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig

EXECUTED_PASS = MechanicalResult(
    passed=True,
    checks=(
        CheckResult(check_type=CheckType.TEST, passed=True, message="ok", executed=True),
        CheckResult(
            check_type=CheckType.LINT,
            passed=True,
            message="skipped",
            details={"skipped": True},
        ),
    ),
)
SKIPPED_ONLY = MechanicalResult(
    passed=True,
    checks=tuple(
        CheckResult(
            check_type=check_type,
            passed=True,
            message="skipped",
            details={"skipped": True},
        )
        for check_type in CheckType
    ),
)
EXECUTED_FAIL = MechanicalResult(
    passed=False,
    checks=(CheckResult(check_type=CheckType.TEST, passed=False, message="failed", executed=True),),
)


def _semantic(*, approve: bool, reasoning: str = "review") -> SemanticResult:
    return SemanticResult(
        score=0.95 if approve else 0.3,
        ac_compliance=approve,
        goal_alignment=0.9,
        drift_score=0.1,
        uncertainty=0.1,
        reasoning=reasoning,
    )


def _consensus(approved: bool) -> ConsensusResult:
    return ConsensusResult(
        approved=approved,
        votes=(Vote(model="m", approved=approved, confidence=0.9, reasoning="vote"),),
        majority_ratio=1.0 if approved else 0.0,
    )


def _context(*, trigger_consensus: bool = False) -> EvaluationContext:
    return EvaluationContext(
        execution_id="authority-exec",
        seed_id="seed",
        current_ac="The CLI prints the version",
        artifact="print('1.0')",
        trigger_consensus=trigger_consensus,
    )


def _pipeline(
    *,
    semantic: SemanticResult | None,
    consensus: ConsensusResult | None = None,
    mechanical: MechanicalConfig | None = None,
    stage1_enabled: bool = False,
) -> EvaluationPipeline:
    pipeline = EvaluationPipeline(
        llm_adapter=MagicMock(),
        config=PipelineConfig(
            stage1_enabled=stage1_enabled,
            stage2_enabled=semantic is not None,
            stage3_enabled=consensus is not None,
            mechanical=mechanical,
        ),
    )
    pipeline._semantic = MagicMock()
    pipeline._semantic.evaluate = AsyncMock(return_value=Result.ok((semantic, [])))
    pipeline._consensus = MagicMock()
    pipeline._consensus.evaluate = AsyncMock(return_value=Result.ok((consensus, [])))
    return pipeline


async def _evaluate(
    pipeline: EvaluationPipeline,
    *,
    stage1: MechanicalResult | None = None,
    trigger_consensus: bool = False,
) -> EvaluationResult:
    result = await pipeline.evaluate(
        _context(trigger_consensus=trigger_consensus), stage1_result=stage1
    )
    assert result.is_ok
    return result.value


class TestRequiredScenarios:
    """The four scenarios the issue names, through the real pipeline."""

    @pytest.mark.asyncio
    async def test_semantic_approve_without_executed_evidence_is_not_approved(self) -> None:
        result = await _evaluate(_pipeline(semantic=_semantic(approve=True)))

        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.UNVERIFIED
        assert result.has_executed_evidence is False
        # The favorable review is kept as feedback, not discarded.
        assert result.stage2_result is not None
        assert result.stage2_result.ac_compliance is True
        assert (result.failure_reason or "").startswith("Not approved: unverified.")

    @pytest.mark.asyncio
    async def test_semantic_reject_is_not_approved(self) -> None:
        result = await _evaluate(_pipeline(semantic=_semantic(approve=False)), stage1=EXECUTED_PASS)

        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.REJECTED
        assert "AC non-compliance" in (result.failure_reason or "")

    @pytest.mark.asyncio
    async def test_mechanical_pass_and_semantic_approve_is_approved(self) -> None:
        result = await _evaluate(_pipeline(semantic=_semantic(approve=True)), stage1=EXECUTED_PASS)

        assert result.final_approved is True
        assert result.acceptance_state is AcceptanceState.APPROVED
        assert result.failure_reason is None

    @pytest.mark.asyncio
    async def test_mechanical_pass_and_semantic_reject_is_not_approved(self) -> None:
        result = await _evaluate(_pipeline(semantic=_semantic(approve=False)), stage1=EXECUTED_PASS)

        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.REJECTED


class TestExecutedEvidence:
    @pytest.mark.asyncio
    async def test_skipped_only_stage1_is_not_evidence(self) -> None:
        """Every check skipped reports ``passed`` but grants nothing."""
        result = await _evaluate(_pipeline(semantic=_semantic(approve=True)), stage1=SKIPPED_ONLY)

        assert SKIPPED_ONLY.passed is True
        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.UNVERIFIED
        assert "Stage 1 ran no configured check" in (result.failure_reason or "")

    @pytest.mark.asyncio
    async def test_failed_executed_check_is_rejected_before_model_review(self) -> None:
        pipeline = _pipeline(semantic=_semantic(approve=True))
        result = await _evaluate(pipeline, stage1=EXECUTED_FAIL)

        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.REJECTED
        assert (result.failure_reason or "").startswith("Stage 1 failed")
        pipeline._semantic.evaluate.assert_not_called()

    @pytest.mark.asyncio
    async def test_executed_evidence_without_model_review_is_approved(self) -> None:
        result = await _evaluate(_pipeline(semantic=None), stage1=EXECUTED_PASS)

        assert result.final_approved is True
        assert result.acceptance_state is AcceptanceState.APPROVED

    @pytest.mark.asyncio
    async def test_real_mechanical_verifier_marks_run_and_skipped_checks(self) -> None:
        """The verifier, not a fixture, decides what counts as executed."""
        pipeline = _pipeline(
            semantic=_semantic(approve=True),
            stage1_enabled=True,
            mechanical=MechanicalConfig(test_command=(sys.executable, "-c", "pass")),
        )
        result = await _evaluate(pipeline)

        assert result.stage1_result is not None
        executed = {check.check_type for check in result.stage1_result.executed_checks}
        assert executed == {CheckType.TEST}
        assert result.final_approved is True

    @pytest.mark.asyncio
    async def test_real_mechanical_verifier_with_no_commands_is_unverified(self) -> None:
        pipeline = _pipeline(
            semantic=_semantic(approve=True),
            stage1_enabled=True,
            mechanical=MechanicalConfig(),
        )
        result = await _evaluate(pipeline)

        assert result.stage1_result is not None
        assert result.stage1_result.passed is True
        assert result.stage1_result.executed_checks == ()
        assert result.acceptance_state is AcceptanceState.UNVERIFIED

    @pytest.mark.asyncio
    async def test_completion_event_records_acceptance_state(self) -> None:
        result = await _evaluate(_pipeline(semantic=_semantic(approve=True)))

        completed = [e for e in result.events if e.type == "evaluation.pipeline.completed"]
        assert len(completed) == 1
        assert completed[0].data["final_approved"] is False
        assert completed[0].data["acceptance_state"] == "unverified"


class TestConsensusIsAdvisory:
    @pytest.mark.asyncio
    async def test_consensus_approve_without_executed_evidence_is_not_approved(self) -> None:
        result = await _evaluate(
            _pipeline(semantic=_semantic(approve=True), consensus=_consensus(True)),
            trigger_consensus=True,
        )

        assert result.stage3_result is not None
        assert result.stage3_result.approved is True
        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.UNVERIFIED

    @pytest.mark.asyncio
    async def test_consensus_approve_cannot_lift_semantic_block(self) -> None:
        result = await _evaluate(
            _pipeline(semantic=_semantic(approve=False), consensus=_consensus(True)),
            stage1=EXECUTED_PASS,
            trigger_consensus=True,
        )

        assert result.stage3_result is not None
        assert result.final_approved is False
        assert result.acceptance_state is AcceptanceState.REJECTED

    @pytest.mark.asyncio
    async def test_consensus_reject_withholds_executed_approval(self) -> None:
        result = await _evaluate(
            _pipeline(semantic=_semantic(approve=True), consensus=_consensus(False)),
            stage1=EXECUTED_PASS,
            trigger_consensus=True,
        )

        assert result.final_approved is False
        assert (result.failure_reason or "").startswith("Stage 3 failed")


def test_decide_final_approval_never_grants_without_stage1() -> None:
    """No combination of model verdicts approves when Stage 1 is absent."""
    for semantic in (None, _semantic(approve=True)):
        for consensus in (None, _consensus(True)):
            assert (
                decide_final_approval(
                    stage1_result=None,
                    stage2_result=semantic,
                    stage3_result=consensus,
                )
                is False
            )


def test_check_result_defaults_to_not_executed() -> None:
    """A check that does not declare execution never counts as evidence."""
    check = CheckResult(check_type=CheckType.TEST, passed=True, message="ok")
    assert check.executed is False
    assert MechanicalResult(passed=True, checks=(check,)).has_executed_evidence is False
