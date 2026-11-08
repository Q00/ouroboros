"""Malformed model scores must not make a favorable semantic verdict."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.evaluation.models import CheckResult, CheckType, EvaluationContext, MechanicalResult
from ouroboros.evaluation.pipeline import EvaluationPipeline, PipelineConfig
from ouroboros.evaluation.semantic import parse_semantic_response
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _response(**overrides: object) -> str:
    data: dict[str, object] = {
        "score": 0.9,
        "ac_compliance": True,
        "goal_alignment": 0.9,
        "drift_score": 0.1,
        "uncertainty": 0.1,
        "reward_hacking_risk": 0.0,
        "reasoning": "synthetic test",
    }
    data.update(overrides)
    return json.dumps(data)


@pytest.mark.parametrize(
    "field", ["score", "goal_alignment", "drift_score", "uncertainty", "reward_hacking_risk"]
)
@pytest.mark.parametrize(
    "value", [True, False, float("nan"), float("inf"), -float("inf"), "NaN", "Infinity"]
)
def test_rejects_boolean_and_nonfinite_semantic_values(field: str, value: object) -> None:
    result = parse_semantic_response(_response(**{field: value}))
    assert result.is_err


def test_finite_numeric_strings_and_existing_clamping_remain_supported() -> None:
    result = parse_semantic_response(_response(score="0.85", goal_alignment="2.0"))
    assert result.is_ok
    assert result.value.score == 0.85
    assert result.value.goal_alignment == 1.0


def test_oversized_integer_fails_without_escaping_parser() -> None:
    result = parse_semantic_response(_response(score=10**400))
    assert result.is_err


@pytest.mark.parametrize("value", [True, float("nan"), "NaN"])
async def test_pipeline_cannot_approve_a_malformed_score(value: object) -> None:
    class Judge:
        async def complete(self, messages: list[Any], config: Any) -> Any:
            return Result.ok(
                CompletionResponse(
                    content=_response(score=value),
                    model="synthetic",
                    usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            )

    pipeline = EvaluationPipeline(
        llm_adapter=Judge(),  # type: ignore[arg-type]
        config=PipelineConfig(stage3_enabled=False),
    )
    context = EvaluationContext(
        execution_id="exec", seed_id="seed", current_ac="works", artifact="synthetic"
    )
    stage1 = MechanicalResult(
        passed=True,
        checks=(CheckResult(CheckType.TEST, True, "synthetic check passed", executed=True),),
    )
    result = await pipeline.evaluate(context, stage1_result=stage1)
    assert result.is_err
