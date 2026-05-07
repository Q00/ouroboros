"""Unit tests for the merge-policy gate (#689 PR-D)."""

from __future__ import annotations

import pytest

from ouroboros.auto.goal_classifier import (
    GoalClassification,
    SideEffectRisk,
    classify_goal,
)
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.auto.merge_policy import (
    CIState,
    MergeAction,
    MergePolicy,
    PullRequestStatus,
    ReviewState,
    evaluate_merge,
    record_decision_on_ledger,
)


def _ok_classification() -> GoalClassification:
    return classify_goal("merge https://github.com/Q00/ouroboros/pull/689 once CI is green")


def _ok_status(**overrides) -> PullRequestStatus:
    base: dict = {
        "repo": "Q00/ouroboros",
        "number": 689,
        "target_branch": "main",
        "head_sha": "0123456789abcdef0123456789abcdef01234567",
        "mergeable": True,
        "ci_state": CIState.SUCCESS,
        "review_states": (ReviewState.APPROVED,),
        "has_write_permission": True,
        "is_draft": False,
    }
    base.update(overrides)
    return PullRequestStatus(**base)


def test_clean_status_with_high_risk_classification_is_allowed() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(),
    )
    assert decision.allowed is True
    assert decision.blocking_reasons == ()
    assert decision.action is MergeAction.MERGE


def test_high_risk_without_requires_confirmation_fails_closed() -> None:
    fabricated = GoalClassification(
        interview_required=False,
        direct_run_allowed=True,
        side_effect_risk=SideEffectRisk.HIGH,
        requires_confirmation=False,
        reason="fabricated",
        matched_signals=(),
    )
    decision = evaluate_merge(classification=fabricated, status=_ok_status())
    assert decision.allowed is False
    assert any("HIGH side-effect risk" in reason for reason in decision.blocking_reasons)


def test_missing_write_permission_blocks() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(has_write_permission=False),
    )
    assert decision.allowed is False
    assert any("write permission" in reason for reason in decision.blocking_reasons)


def test_disallowed_target_branch_blocks() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(target_branch="release/2025-Q4"),
    )
    assert decision.allowed is False
    assert any("not in allowed set" in reason for reason in decision.blocking_reasons)


def test_target_branch_policy_extension_unblocks_merge() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(target_branch="release/2025-Q4"),
        policy=MergePolicy(allowed_target_branches=frozenset({"main", "release/2025-Q4"})),
    )
    assert decision.allowed is True


def test_draft_pr_is_blocked_by_default() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(is_draft=True),
    )
    assert decision.allowed is False
    assert any("draft" in reason for reason in decision.blocking_reasons)


def test_unmergeable_pr_blocks() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(mergeable=False),
    )
    assert decision.allowed is False
    assert any("not mergeable" in reason for reason in decision.blocking_reasons)


def test_unknown_mergeability_blocks_with_wait_guidance() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(mergeable=None),
    )
    assert decision.allowed is False
    assert any(
        "mergeability check has not finished" in reason for reason in decision.blocking_reasons
    )
    assert any("Wait for GitHub" in action for action in decision.required_actions)


def test_failing_ci_blocks() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(ci_state=CIState.FAILURE),
    )
    assert decision.allowed is False
    assert any("CI state is failure" in reason for reason in decision.blocking_reasons)


def test_pending_ci_blocks_until_success() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(ci_state=CIState.PENDING),
    )
    assert decision.allowed is False
    assert any("CI state is pending" in reason for reason in decision.blocking_reasons)


def test_changes_requested_blocks_merge() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(review_states=(ReviewState.CHANGES_REQUESTED, ReviewState.APPROVED)),
    )
    assert decision.allowed is False
    assert any("requested changes" in reason for reason in decision.blocking_reasons)


def test_no_approving_reviews_blocks_merge() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(review_states=(ReviewState.COMMENTED,)),
    )
    assert decision.allowed is False
    assert any("approving reviews" in reason for reason in decision.blocking_reasons)


def test_low_risk_review_classification_does_not_authorize_merge() -> None:
    """A read-only goal (``review ...``) must never authorize MERGE.

    Without the action/classification check, a clean PR + a review-only
    goal would have allowed the gate to authorize a merge that the user
    never asked for.
    """
    review_classification = classify_goal("review https://github.com/Q00/ouroboros/pull/689")
    decision = evaluate_merge(
        classification=review_classification,
        status=_ok_status(),
        action=MergeAction.MERGE,
    )

    assert decision.allowed is False
    assert any("does not authorize action 'merge'" in r for r in decision.blocking_reasons)
    assert any("action:merge=needs:high" in s for s in decision.matched_signals)


def test_low_risk_review_classification_does_not_authorize_close() -> None:
    """Read-only verbs do not authorize CLOSE either (CLOSE needs MEDIUM)."""
    review_classification = classify_goal("review https://github.com/Q00/ouroboros/pull/689")
    decision = evaluate_merge(
        classification=review_classification,
        status=_ok_status(),
        action=MergeAction.CLOSE,
    )

    assert decision.allowed is False
    assert any("does not authorize action 'close'" in r for r in decision.blocking_reasons)


def test_high_risk_classification_authorizes_close_via_min_risk_floor() -> None:
    """HIGH ≥ MEDIUM, so a merge-classified goal can also authorize close."""
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(),
        action=MergeAction.CLOSE,
    )

    assert decision.allowed is True


def test_action_classification_match_can_be_disabled_for_isolation() -> None:
    """Tests of repository-only checks may opt out of the consistency rule."""
    decision = evaluate_merge(
        classification=classify_goal("review https://github.com/Q00/ouroboros/pull/689"),
        status=_ok_status(),
        action=MergeAction.MERGE,
        policy=MergePolicy(require_action_classification_match=False),
    )

    assert decision.allowed is True


def test_review_requirement_does_not_apply_to_close_action() -> None:
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(review_states=()),
        action=MergeAction.CLOSE,
    )
    # CI/permission still apply, but missing reviews must not block close.
    blocking = " ".join(decision.blocking_reasons)
    assert "approving reviews" not in blocking


def test_evaluate_merge_rejects_invalid_input() -> None:
    with pytest.raises(TypeError):
        evaluate_merge(  # type: ignore[arg-type]
            classification="bad", status=_ok_status()
        )
    with pytest.raises(TypeError):
        evaluate_merge(  # type: ignore[arg-type]
            classification=_ok_classification(), status="bad"
        )


def test_record_decision_appends_audit_entry_to_ledger() -> None:
    ledger = SeedDraftLedger.from_goal("merge https://github.com/Q00/ouroboros/pull/689")
    decision = evaluate_merge(
        classification=_ok_classification(),
        status=_ok_status(ci_state=CIState.PENDING),
    )

    record = record_decision_on_ledger(ledger, decision, _ok_status(ci_state=CIState.PENDING))

    assert record["decision"]["allowed"] is False
    assert record in ledger.merge_policy_decisions
    # Round-trip through dict to ensure JSON-safe.
    serialized = ledger.to_dict()
    restored = SeedDraftLedger.from_dict(serialized)
    assert restored.merge_policy_decisions == ledger.merge_policy_decisions


def test_record_decision_rejects_object_without_required_method() -> None:
    decision = evaluate_merge(classification=_ok_classification(), status=_ok_status())

    class _Stub:
        pass

    with pytest.raises(TypeError, match="record_merge_policy_decision"):
        record_decision_on_ledger(_Stub(), decision, _ok_status())


def test_pull_request_status_serializes_to_dict() -> None:
    status = _ok_status()
    payload = status.to_dict()
    assert payload["repo"] == "Q00/ouroboros"
    assert payload["number"] == 689
    assert payload["ci_state"] == "success"
    assert payload["review_states"] == ["approved"]


def test_legacy_ledger_loads_without_merge_policy_decisions_field() -> None:
    payload = SeedDraftLedger.from_goal("Build a CLI").to_dict()
    payload.pop("merge_policy_decisions", None)
    restored = SeedDraftLedger.from_dict(payload)
    assert restored.merge_policy_decisions == []


def test_ledger_rejects_malformed_decision_list() -> None:
    payload = SeedDraftLedger.from_goal("Build a CLI").to_dict()
    payload["merge_policy_decisions"] = ["not a dict"]
    with pytest.raises(ValueError, match="merge_policy_decisions"):
        SeedDraftLedger.from_dict(payload)
