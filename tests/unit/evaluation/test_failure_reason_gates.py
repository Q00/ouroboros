"""The failure reason must name the gate that actually decided.

Three model gates can withhold approval and two of them can be true at once,
so the branch order carries meaning. These cases pin the ones where naming the
wrong gate sends an operator to the wrong place. They run with executed,
passing Stage 1 evidence, the only case in which a model gate decides; without
it the result is unverified and says so first.
"""

from __future__ import annotations

import pytest

from ouroboros.evaluation.models import (
    REWARD_HACKING_VETO_THRESHOLD,
    SEMANTIC_APPROVAL_SCORE,
    CheckResult,
    CheckType,
    ConsensusResult,
    MechanicalResult,
    SemanticResult,
    Vote,
    build_failure_reason,
)

EXECUTED_STAGE1 = MechanicalResult(
    passed=True,
    checks=(CheckResult(check_type=CheckType.TEST, passed=True, message="ok", executed=True),),
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
    reason = build_failure_reason(final_approved=False, stage1_result=EXECUTED_STAGE1, **kwargs)  # type: ignore[arg-type]
    assert reason is not None
    return reason


def test_approving_consensus_does_not_lift_a_stage2_block() -> None:
    """Model review is advisory, so a consensus approval cannot grant anything.

    Stage 3 approved while Stage 2 was non-compliant. Stage 2 still withholds,
    and it is named rather than the veto, which is later in the order.
    """
    reason = rejected(
        stage2_result=semantic(0.92, ac_compliance=False, risk=0.95),
        stage3_result=consensus(True),
    )

    assert reason.startswith("Stage 2 failed: AC non-compliance")


def test_veto_is_named_when_it_is_the_only_withholding_gate() -> None:
    """Compliant, high-scoring, consensus-approved, and gamed: the veto decided."""
    reason = rejected(
        stage2_result=semantic(0.92, ac_compliance=True, risk=0.95),
        stage3_result=consensus(True),
    )

    assert reason.startswith("Stage 2 veto:")


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


def test_unverified_reason_leads_and_carries_the_model_review() -> None:
    """Without executed evidence the reason says unverified before the model gate."""
    reason = build_failure_reason(
        final_approved=False,
        stage1_result=None,
        stage2_result=semantic(0.4, ac_compliance=False),
        stage3_result=None,
    )

    assert reason is not None
    assert reason.startswith("Not approved: unverified.")
    assert "Stage 1 did not run" in reason
    assert "AC non-compliance" in reason


def test_unverified_reason_for_a_favorable_review_is_not_a_rejection() -> None:
    """A favorable model review with nothing executed is unverified, not approved."""
    skipped_only = MechanicalResult(
        passed=True,
        checks=(
            CheckResult(
                check_type=CheckType.TEST,
                passed=True,
                message="skipped",
                details={"skipped": True},
            ),
        ),
    )
    reason = build_failure_reason(
        final_approved=False,
        stage1_result=skipped_only,
        stage2_result=semantic(0.95, ac_compliance=True),
        stage3_result=None,
    )

    assert reason is not None
    assert reason.startswith("Not approved: unverified.")
    assert "Stage 1 ran no configured check" in reason
    assert "cannot grant acceptance" in reason
