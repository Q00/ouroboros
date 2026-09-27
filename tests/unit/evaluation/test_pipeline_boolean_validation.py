"""Exercise boolean validation through real evaluators and final pipeline decisions."""

import json
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import ValidationError
from ouroboros.core.types import Result
from ouroboros.evaluation.consensus import ConsensusConfig
from ouroboros.evaluation.models import CheckResult, CheckType, EvaluationContext, MechanicalResult
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _completion(payload: dict[str, object]) -> CompletionResponse:
    return CompletionResponse(content=json.dumps(payload), model="test", usage=UsageInfo(0, 0, 0))


def _semantic_response(
    ac_compliance: bool | str, *, uncertainty: float = 0.05
) -> CompletionResponse:
    return _completion(
        {
            "score": 0.95,
            "ac_compliance": ac_compliance,
            "goal_alignment": 0.9,
            "drift_score": 0.0,
            "uncertainty": uncertainty,
            "reasoning": "Fixed semantic response",
            "reward_hacking_risk": 0.0,
        }
    )


def _context(*, trigger_consensus: bool = False) -> EvaluationContext:
    return EvaluationContext(
        execution_id="boolean-exec",
        seed_id="boolean-seed",
        current_ac="The artifact satisfies the acceptance criterion",
        artifact="Test artifact",
        trigger_consensus=trigger_consensus,
    )


@pytest.fixture
def passing_stage1() -> MechanicalResult:
    return MechanicalResult(
        passed=True,
        checks=(CheckResult(check_type=CheckType.LINT, passed=True, message="Passed"),),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ac_compliance", [True, False])
async def test_semantic_boolean_controls_final_approval(
    ac_compliance: bool, passing_stage1: MechanicalResult
) -> None:
    llm = AsyncMock()
    llm.complete.return_value = Result.ok(_semantic_response(ac_compliance))
    pipeline = EvaluationPipeline(llm)

    result = await pipeline.evaluate(_context(), stage1_result=passing_stage1)

    assert result.is_ok
    assert result.value.final_approved is ac_compliance
    assert result.value.stage1_result is passing_stage1
    assert result.value.stage2_result is not None
    assert result.value.stage2_result.ac_compliance is ac_compliance
    assert result.value.stage3_result is None
    llm.complete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger_consensus", [False, True])
async def test_invalid_semantic_boolean_cannot_reach_consensus(
    trigger_consensus: bool, passing_stage1: MechanicalResult
) -> None:
    """Even an explicit second opinion cannot override invalid Stage 2 data."""
    llm = AsyncMock()
    llm.complete.return_value = Result.ok(_semantic_response("false"))
    pipeline = EvaluationPipeline(llm)

    result = await pipeline.evaluate(
        _context(trigger_consensus=trigger_consensus), stage1_result=passing_stage1
    )

    assert result.is_err
    assert isinstance(result.error, ValidationError)
    assert result.error.field == "ac_compliance"
    llm.complete.assert_awaited_once()
    assert llm.complete.await_args.args[1].role == "semantic_evaluation"


@pytest.mark.asyncio
@pytest.mark.parametrize("single_model", [False, True], ids=["multi-model", "multi-perspective"])
@pytest.mark.parametrize(
    ("approvals", "expected_approved", "expected_ratio"),
    [
        ((True, True, "false"), True, 1.0),
        ((True, False, "false"), False, 0.5),
        ((True, "false", "false"), None, None),
        (("false", "false", "false"), None, None),
    ],
    ids=["two-approvals", "split-valid-votes", "one-valid-vote", "no-valid-votes"],
)
async def test_invalid_consensus_booleans_preserve_quorum_and_final_decision(
    single_model: bool,
    approvals: tuple[bool | str, ...],
    expected_approved: bool | None,
    expected_ratio: float | None,
    passing_stage1: MechanicalResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only valid votes count, using each mode's existing two-vote quorum."""
    monkeypatch.setattr(
        "ouroboros.evaluation.consensus._has_multi_model_credentials", lambda: False
    )
    models = (
        ("openrouter/m1", "openrouter/m2", "openrouter/m3") if single_model else ("m1", "m2", "m3")
    )
    llm = AsyncMock()
    llm.complete.side_effect = [
        Result.ok(_semantic_response(True, uncertainty=0.31)),
        *[
            Result.ok(_completion({"approved": approval, "confidence": 0.9, "reasoning": "Vote"}))
            for approval in approvals
        ],
    ]
    pipeline = EvaluationPipeline(llm, PipelineConfig(consensus=ConsensusConfig(models=models)))

    result = await pipeline.evaluate(_context(), stage1_result=passing_stage1)

    calls = llm.complete.await_args_list
    assert len(calls) == 4
    assert calls[0].args[1].role == "semantic_evaluation"
    expected_role = "consensus_perspective" if single_model else "consensus_vote"
    assert [call.args[1].role for call in calls[1:]] == [expected_role] * 3
    assert [call.args[1].model for call in calls[1:]] == (
        [""] * 3 if single_model else list(models)
    )

    valid_approvals = tuple(approval for approval in approvals if isinstance(approval, bool))
    if expected_approved is None:
        assert result.is_err
        assert isinstance(result.error, ValidationError)
        assert "Not enough" in result.error.message
        assert f"{len(valid_approvals)}/3" in result.error.message
        errors = result.error.details["errors"]
        assert len(errors) == 3 - len(valid_approvals)
        assert all("approved" in error for error in errors)
    else:
        assert result.is_ok
        assert result.value.final_approved is expected_approved
        assert result.value.stage1_result is passing_stage1
        consensus = result.value.stage3_result
        assert consensus is not None
        assert consensus.approved is expected_approved
        assert consensus.is_single_model is single_model
        assert consensus.majority_ratio == expected_ratio
        assert tuple(vote.approved for vote in consensus.votes) == valid_approvals
