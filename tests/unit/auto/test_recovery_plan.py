"""Tests for typed auto recovery plan artifacts."""

from __future__ import annotations

import pytest

from ouroboros.auto.recovery_plan import (
    AutoRecoveryPlan,
    RecoveryPlanAction,
    build_lateral_recovery_plan,
    build_manual_recovery_plan,
)


def test_lateral_recovery_plan_round_trips() -> None:
    plan = build_lateral_recovery_plan(
        qa_score=0.42,
        qa_verdict="fail",
        differences=("missing CLI output",),
        suggestions=("add a smoke test",),
        persona="hacker",
        approach_summary="Use a smaller verification path",
        lateral_text="Run the CLI directly and capture stdout.",
    )

    assert plan.action is RecoveryPlanAction.RALPH_REDISPATCH
    assert plan.safe_to_redispatch is True
    assert "Run the CLI" in plan.instruction
    assert AutoRecoveryPlan.from_dict(plan.to_dict()) == plan


def test_manual_plan_is_not_auto_dispatchable() -> None:
    plan = build_manual_recovery_plan(
        qa_score=0.25,
        qa_verdict="fail",
        differences=("missing auth decision",),
        suggestions=("ask the user",),
    )

    assert plan.action is RecoveryPlanAction.MANUAL_INTERVENTION
    assert plan.safe_to_redispatch is False
    assert plan.instruction == "ask the user"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "action": "ralph_redispatch",
            "safe_to_redispatch": False,
            "reason": "x",
            "qa_score": 0.1,
            "qa_verdict": "fail",
        },
        {
            "action": "manual_intervention",
            "safe_to_redispatch": True,
            "reason": "x",
            "qa_score": 0.1,
            "qa_verdict": "fail",
        },
        {
            "action": "mystery",
            "safe_to_redispatch": False,
            "reason": "x",
            "qa_score": 0.1,
            "qa_verdict": "fail",
        },
    ],
)
def test_invalid_plan_payloads_raise(payload) -> None:
    with pytest.raises(ValueError):
        AutoRecoveryPlan.from_dict(payload)
