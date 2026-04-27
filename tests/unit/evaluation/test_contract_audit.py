"""Tests for Seed contract audit gates."""

from __future__ import annotations

from ouroboros.evaluation.contract_audit import contract_audit_failure
from ouroboros.evaluation.models import SemanticResult


def _semantic(**overrides) -> SemanticResult:
    data = {
        "score": 0.9,
        "ac_compliance": True,
        "goal_alignment": 0.9,
        "drift_score": 0.1,
        "uncertainty": 0.1,
        "reasoning": "Strong contract alignment",
        "reward_hacking_risk": 0.0,
    }
    data.update(overrides)
    return SemanticResult(**data)


def test_contract_audit_passes_strong_semantic_result() -> None:
    assert contract_audit_failure(_semantic()) is None


def test_contract_audit_fails_when_semantic_result_missing() -> None:
    failure = contract_audit_failure(None)

    assert failure == "Seed contract audit failed: semantic evaluation did not run"


def test_contract_audit_fails_ac_non_compliance() -> None:
    failure = contract_audit_failure(_semantic(ac_compliance=False))

    assert failure is not None
    assert "acceptance criteria were not semantically satisfied" in failure


def test_contract_audit_fails_low_score() -> None:
    failure = contract_audit_failure(_semantic(score=0.7))

    assert failure is not None
    assert "score 0.70 < 0.80" in failure


def test_contract_audit_fails_low_goal_alignment() -> None:
    failure = contract_audit_failure(_semantic(goal_alignment=0.7))

    assert failure is not None
    assert "goal alignment 0.70 < 0.80" in failure


def test_contract_audit_fails_high_drift_even_when_ac_passes() -> None:
    failure = contract_audit_failure(_semantic(drift_score=0.7))

    assert failure is not None
    assert "drift score" in failure


def test_contract_audit_fails_high_uncertainty() -> None:
    failure = contract_audit_failure(_semantic(uncertainty=0.7))

    assert failure is not None
    assert "uncertainty 0.70 > 0.30" in failure


def test_contract_audit_fails_reward_hacking_risk() -> None:
    failure = contract_audit_failure(_semantic(reward_hacking_risk=0.8))

    assert failure is not None
    assert "reward hacking risk" in failure
