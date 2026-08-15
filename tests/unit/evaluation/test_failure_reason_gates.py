"""The failure reason must name the gate that actually decided.

Three gates can reject a run and two of them can be true at once, so the
branch order carries meaning. These cases pin the ones where naming the
wrong gate sends an operator to the wrong place.
"""

from __future__ import annotations

import pytest

from ouroboros.evaluation.models import (
    REWARD_HACKING_VETO_THRESHOLD,
    SEMANTIC_APPROVAL_SCORE,
    ConsensusResult,
    SemanticResult,
    Vote,
    build_failure_reason,
)


def semantic(score: float, ac_compliance: bool, risk: float = 0.0) -> SemanticResult:
    """Build a Stage 2 result varying only the fields the gates read."""
    return SemanticResult(
        score=score,
        ac_compliance=ac_compliance,
        goal_alignment=0.9,
        drift_score=0.1,
        uncertainty=0.1,
        reasoning="fixture",
        reward_hacking_risk=risk,
    )


def consensus(approved: bool, ratio: float = 1.0) -> ConsensusResult:
    """Build a Stage 3 result with one vote matching the verdict."""
    return ConsensusResult(
        approved=approved,
        votes=(Vote(model="m", approved=approved, confidence=0.9, reasoning="fixture"),),
        majority_ratio=ratio,
    )


def rejected(**kwargs: object) -> str:
    """Call the builder for a rejected run and assert it produced a reason."""
    reason = build_failure_reason(final_approved=False, stage1_result=None, **kwargs)  # type: ignore[arg-type]
    assert reason is not None
    return reason


def test_veto_overriding_an_approving_consensus_names_the_veto() -> None:
    """The veto decided, so non-compliance must not take the credit.

    Stage 3 approved, the veto flipped it, and Stage 2 also happens to be
    non-compliant. Reporting non-compliance here names a gate that did not
    decide, because the pipeline had already moved past Stage 2.
    """
    reason = rejected(
        stage2_result=semantic(0.92, ac_compliance=False, risk=0.95),
        stage3_result=consensus(True),
    )

    assert reason.startswith("Stage 2 veto:")
    assert "AC non-compliance" not in reason


def test_score_gate_rejection_is_named_rather_than_unknown() -> None:
    """``ac_compliance`` passes and the score gate rejects, with no consensus."""
    reason = rejected(
        stage2_result=semantic(SEMANTIC_APPROVAL_SCORE - 0.01, ac_compliance=True),
        stage3_result=None,
    )

    assert "semantic score" in reason
    assert f"{SEMANTIC_APPROVAL_SCORE:.2f}" in reason
    assert reason != "Unknown failure"


def test_consensus_rejection_wins_over_a_high_risk_score() -> None:
    """A rejected consensus was never approved, so the veto did not decide it."""
    reason = rejected(
        stage2_result=semantic(0.9, ac_compliance=True, risk=0.95),
        stage3_result=consensus(False, ratio=0.33),
    )

    assert reason.startswith("Stage 3 failed:")


def test_non_compliance_still_wins_when_the_veto_could_not_have_decided() -> None:
    """Without consensus, a non-compliant Stage 2 was never approved.

    The veto only flips approve to reject, so it cannot be the deciding gate
    on a run that Stage 2 had already rejected.
    """
    reason = rejected(
        stage2_result=semantic(0.92, ac_compliance=False, risk=REWARD_HACKING_VETO_THRESHOLD + 0.1),
        stage3_result=None,
    )

    assert "AC non-compliance" in reason
    assert not reason.startswith("Stage 2 veto:")


@pytest.mark.parametrize(
    ("score", "ac_compliance"),
    [(0.92, False), (0.79, True)],
)
def test_approved_runs_have_no_reason(score: float, ac_compliance: bool) -> None:
    """``final_approved`` short-circuits before any gate is inspected."""
    assert (
        build_failure_reason(
            final_approved=True,
            stage1_result=None,
            stage2_result=semantic(score, ac_compliance=ac_compliance),
            stage3_result=None,
        )
        is None
    )
