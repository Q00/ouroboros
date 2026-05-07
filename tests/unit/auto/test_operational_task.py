from __future__ import annotations

import pytest

from ouroboros.auto.operational_task import (
    GENERAL,
    ISSUE_URL,
    MERGE_INTENT,
    PR_URL,
    REVIEW_INTENT,
    RISK_DESTRUCTIVE_CLOSE,
    RISK_DESTRUCTIVE_MERGE,
    RISK_LOW,
    RISK_NONE,
    classify_operational_task,
)


def test_general_goal_requires_interview() -> None:
    result = classify_operational_task("Build a CLI tool that tracks daily habits")
    assert result.kind == GENERAL
    assert result.interview_required is True
    assert result.direct_run_allowed is False
    assert result.side_effect_risk == RISK_NONE
    assert result.requires_confirmation is False
    assert result.targets == ()


def test_empty_goal_requires_interview() -> None:
    result = classify_operational_task("   ")
    assert result.kind == GENERAL
    assert result.interview_required is True
    assert "empty goal" in result.reasons


def test_pr_url_alone_allows_direct_run() -> None:
    result = classify_operational_task(
        "Take a look at https://github.com/shaun0927/opensafari/pull/12"
    )
    assert result.kind == PR_URL
    assert result.direct_run_allowed is True
    assert result.interview_required is False
    assert result.side_effect_risk == RISK_NONE
    assert result.targets == ("https://github.com/shaun0927/opensafari/pull/12",)


def test_pr_url_with_review_keyword_marks_low_risk() -> None:
    result = classify_operational_task(
        "review and improve https://github.com/owner/repo/pull/1"
    )
    assert result.kind == PR_URL
    assert result.side_effect_risk == RISK_LOW
    assert result.requires_confirmation is False


def test_pr_url_with_merge_keyword_marks_destructive_and_requires_confirmation() -> None:
    result = classify_operational_task(
        "merge https://github.com/owner/repo/pull/1 once CI is green"
    )
    assert result.kind == MERGE_INTENT
    assert result.side_effect_risk == RISK_DESTRUCTIVE_MERGE
    assert result.requires_confirmation is True
    assert result.direct_run_allowed is True


def test_korean_merge_intent_detected_with_pr_index_url() -> None:
    """Mirrors the auto_78c98678de5d incident goal shape."""
    result = classify_operational_task(
        "https://github.com/shaun0927/opensafari/pulls의 열린 pr을 면밀히 해석해 "
        "merge 가능한 수준까지 반복 개선해줘. merge 가능한 수준이라면 머지 진행해줘."
    )
    assert result.kind == MERGE_INTENT
    assert result.side_effect_risk == RISK_DESTRUCTIVE_MERGE
    assert result.requires_confirmation is True
    assert result.direct_run_allowed is True
    assert "https://github.com/shaun0927/opensafari/pulls" in result.targets
    assert "merge_keyword" in result.reasons
    assert "pr_index_url" in result.reasons


def test_close_keyword_marked_destructive_close() -> None:
    result = classify_operational_task(
        "close https://github.com/owner/repo/pull/2 — superseded"
    )
    assert result.side_effect_risk == RISK_DESTRUCTIVE_CLOSE
    assert result.requires_confirmation is True


def test_issue_url_alone_allows_direct_run() -> None:
    result = classify_operational_task(
        "look at https://github.com/owner/repo/issues/42"
    )
    assert result.kind == ISSUE_URL
    assert result.direct_run_allowed is True


def test_review_keyword_without_url_still_operational() -> None:
    result = classify_operational_task("리뷰만 해줘 (url 없음)")
    assert result.kind == REVIEW_INTENT
    assert result.side_effect_risk == RISK_LOW
    # No URL means the pipeline still needs the interview to find a target.
    assert result.interview_required is True
    assert result.direct_run_allowed is False


def test_targets_deduplicated_in_first_seen_order() -> None:
    goal = (
        "see https://github.com/o/r/pull/1 and https://github.com/o/r/pull/2 "
        "and again https://github.com/o/r/pull/1"
    )
    result = classify_operational_task(goal)
    assert result.targets == (
        "https://github.com/o/r/pull/1",
        "https://github.com/o/r/pull/2",
    )


def test_pr_url_pattern_does_not_match_pulls_index() -> None:
    """PR-URL pattern must require /pull/<n> to avoid mis-classifying the
    /pulls index as a single-PR target."""
    result = classify_operational_task("see https://github.com/o/r/pulls")
    # Index URL is recognized via has_pr_index, not as a single PR.
    assert result.kind == PR_URL
    assert "pr_index_url" in result.reasons
    assert "pr_url" not in result.reasons


def test_classification_is_pure_and_repeatable() -> None:
    goal = "merge https://github.com/owner/repo/pull/1"
    a = classify_operational_task(goal)
    b = classify_operational_task(goal)
    assert a == b


@pytest.mark.parametrize(
    "goal,expected_kind",
    [
        ("merge https://github.com/o/r/pull/1", MERGE_INTENT),
        ("close https://github.com/o/r/pull/1", MERGE_INTENT),
        ("https://github.com/o/r/pull/1 only", PR_URL),
        ("https://github.com/o/r/issues/9", ISSUE_URL),
        ("just review some code", REVIEW_INTENT),
        ("nothing actionable here", GENERAL),
    ],
)
def test_classification_kind_table(goal: str, expected_kind: str) -> None:
    assert classify_operational_task(goal).kind == expected_kind
