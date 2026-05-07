"""Unit tests for the auto-mode goal classifier."""

from __future__ import annotations

import pytest

from ouroboros.auto.goal_classifier import (
    GoalClassification,
    SideEffectRisk,
    classify_goal,
)


def test_empty_goal_requires_interview() -> None:
    result = classify_goal("")
    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.NONE
    assert result.requires_confirmation is False
    assert "empty" in result.reason


def test_whitespace_goal_requires_interview() -> None:
    result = classify_goal("   \n\t  ")
    assert result.interview_required is True
    assert result.direct_run_allowed is False


def test_single_pr_url_with_merge_verb_allows_direct_path_with_high_risk() -> None:
    goal = (
        "https://github.com/shaun0927/opensafari/pull/42 의 PR을 면밀히 해석해 "
        "merge 가능한 수준이라면 merge 진행해줘."
    )
    result = classify_goal(goal)

    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True
    assert any(signal.startswith("pr_url:") for signal in result.matched_signals)
    assert any("merge" in signal for signal in result.matched_signals)


def test_pr_url_with_review_verb_is_low_risk_direct_path() -> None:
    goal = "https://github.com/Q00/ouroboros/pull/689 review and summarize"
    result = classify_goal(goal)

    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert result.side_effect_risk is SideEffectRisk.LOW
    assert result.requires_confirmation is False


def test_issue_url_with_pr_only_verb_routes_to_interview() -> None:
    """``fix`` mutates a PR; an issue URL alone is the wrong anchor type."""
    goal = "fix https://github.com/Q00/ouroboros/issues/689"
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.MEDIUM
    assert "requires a PR URL anchor" in result.reason


def test_issue_url_with_merge_verb_routes_to_interview() -> None:
    """Issues cannot be merged; classifier must not authorize the wrong target."""
    goal = "merge https://github.com/Q00/ouroboros/issues/689"
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True


def test_issue_url_with_rebase_verb_routes_to_interview() -> None:
    goal = "rebase https://github.com/Q00/ouroboros/issues/689"
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.MEDIUM


def test_issue_url_with_read_only_verb_allows_direct_path() -> None:
    """Read-only verbs (review/analyze) are well-defined for issues too."""
    goal = "analyze https://github.com/Q00/ouroboros/issues/689"
    result = classify_goal(goal)

    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert result.side_effect_risk is SideEffectRisk.LOW


def test_pr_url_with_fix_verb_is_medium_risk_direct_path() -> None:
    goal = "fix https://github.com/Q00/ouroboros/pull/689"
    result = classify_goal(goal)

    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert result.side_effect_risk is SideEffectRisk.MEDIUM
    assert result.requires_confirmation is False


def test_pulls_list_url_with_merge_verb_still_routes_to_interview() -> None:
    """List URL must not authorize a destructive verb without narrowing."""
    goal = (
        "https://github.com/shaun0927/opensafari/pulls 의 열린 pr을 면밀히 해석해 "
        "merge 가능한 수준까지 반복 개선해줘. merge 가능한 수준이라면 merge 진행해줘."
    )
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True
    assert any(signal.startswith("pr_list_url:") for signal in result.matched_signals)


def test_pulls_list_url_with_only_review_verb_permits_direct_path() -> None:
    goal = "review https://github.com/Q00/ouroboros/pulls"
    result = classify_goal(goal)

    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert result.side_effect_risk is SideEffectRisk.LOW
    assert result.requires_confirmation is False


def test_url_without_verb_routes_to_interview() -> None:
    goal = "look at https://github.com/Q00/ouroboros/pull/489"
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.NONE


def test_verb_without_url_routes_to_interview() -> None:
    goal = "merge the bug-fix PR for me"
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True


def test_planning_verb_with_url_keeps_interview_even_with_operational_verb() -> None:
    goal = "plan how we should fix https://github.com/Q00/ouroboros/issues/692 before any merge"
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    # Highest verb in goal is "merge" (high), classifier keeps risk
    # pessimistic for downstream policy gates.
    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True


def test_idea_goal_routes_to_interview() -> None:
    result = classify_goal("Build me a CLI tool that tracks daily habits")
    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.NONE
    assert result.matched_signals == ()


def test_korean_review_signal_detected() -> None:
    goal = "https://github.com/Q00/ouroboros/pull/689 리뷰 부탁드립니다"
    result = classify_goal(goal)

    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert result.side_effect_risk is SideEffectRisk.LOW


def test_korean_merge_signal_classified_as_high() -> None:
    goal = "https://github.com/Q00/ouroboros/pull/689 머지 가능하면 진행해줘"
    result = classify_goal(goal)

    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True


def test_classification_round_trips_through_dict() -> None:
    original = classify_goal("https://github.com/Q00/ouroboros/pull/689 merge once CI is green")
    restored = GoalClassification.from_dict(original.to_dict())
    assert restored == original


def test_classification_from_dict_rejects_inconsistent_high_risk_record() -> None:
    payload = {
        "interview_required": False,
        "direct_run_allowed": True,
        "side_effect_risk": "high",
        "requires_confirmation": False,
        "reason": "fabricated",
        "matched_signals": [],
    }
    with pytest.raises(ValueError, match="requires_confirmation"):
        GoalClassification.from_dict(payload)


def test_classification_from_dict_rejects_unknown_risk_tier() -> None:
    payload = {
        "interview_required": True,
        "direct_run_allowed": False,
        "side_effect_risk": "catastrophic",
        "requires_confirmation": True,
        "reason": "bad",
        "matched_signals": [],
    }
    with pytest.raises(ValueError, match="side_effect_risk"):
        GoalClassification.from_dict(payload)


def test_classify_goal_rejects_non_string_input() -> None:
    with pytest.raises(TypeError):
        classify_goal(None)  # type: ignore[arg-type]


def test_multiple_pr_urls_force_interview() -> None:
    """Two PR URLs paired with a destructive verb is an ambiguous target."""
    goal = (
        "compare https://github.com/Q00/ouroboros/pull/694 and "
        "https://github.com/Q00/ouroboros/pull/697, then merge the better one"
    )
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True
    assert "multiple PR/issue URLs" in result.reason


def test_multiple_issue_urls_force_interview_for_review() -> None:
    """Multiple issue URLs are still ambiguous even for read-only verbs."""
    goal = (
        "review https://github.com/Q00/ouroboros/issues/689 "
        "and https://github.com/Q00/ouroboros/issues/692"
    )
    result = classify_goal(goal)

    assert result.interview_required is True
    assert result.direct_run_allowed is False


def test_one_pr_url_and_one_issue_url_with_pr_verb_is_direct() -> None:
    """A single PR URL + a single referenced issue URL still pinpoints the target."""
    goal = (
        "merge https://github.com/Q00/ouroboros/pull/694 "
        "(closes https://github.com/Q00/ouroboros/issues/689)"
    )
    result = classify_goal(goal)

    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert result.side_effect_risk is SideEffectRisk.HIGH
    assert result.requires_confirmation is True


def test_classification_from_dict_rejects_contradictory_invariant() -> None:
    """Both flags True is not a meaningful classification."""
    payload = {
        "interview_required": True,
        "direct_run_allowed": True,
        "side_effect_risk": "low",
        "requires_confirmation": False,
        "reason": "fabricated",
        "matched_signals": [],
    }
    with pytest.raises(ValueError, match="opposite booleans"):
        GoalClassification.from_dict(payload)


def test_classification_from_dict_rejects_both_false_invariant() -> None:
    """Both flags False is also rejected: routing flags must be opposites."""
    payload = {
        "interview_required": False,
        "direct_run_allowed": False,
        "side_effect_risk": "low",
        "requires_confirmation": False,
        "reason": "fabricated",
        "matched_signals": [],
    }
    with pytest.raises(ValueError, match="opposite booleans"):
        GoalClassification.from_dict(payload)


def test_observed_incident_goal_keeps_interview_path() -> None:
    """Regression: the auto_78c98678de5d goal must NOT silently merge."""
    goal = (
        "https://github.com/shaun0927/opensafari/pulls의 열린 pr을 면밀히 해석해 "
        "merge 가능한 수준까지 반복 개선해줘. merge 가능한 수준이라면 merge 진행해줘."
    )
    result = classify_goal(goal)
    # With only a /pulls list URL and a high-risk verb, the safe default
    # is to keep interview-first so the user clarifies which PR to merge.
    assert result.direct_run_allowed is False
    assert result.requires_confirmation is True
