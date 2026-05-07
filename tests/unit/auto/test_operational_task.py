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
    result = classify_operational_task("review and improve https://github.com/owner/repo/pull/1")
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
    result = classify_operational_task("close https://github.com/owner/repo/pull/2 — superseded")
    assert result.side_effect_risk == RISK_DESTRUCTIVE_CLOSE
    assert result.requires_confirmation is True


def test_issue_url_alone_allows_direct_run() -> None:
    result = classify_operational_task("look at https://github.com/owner/repo/issues/42")
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


# ---------------------------------------------------------------------------
# Bot review follow-ups (#719)
# ---------------------------------------------------------------------------


def test_targetless_merge_intent_falls_back_to_interview() -> None:
    """`merge it once CI is green` has no URL — pipeline cannot act without a
    target, so this MUST require the interview rather than entering the
    direct path. (Bot-flagged in #719 review.)"""
    result = classify_operational_task("merge it once CI is green")
    assert result.kind == MERGE_INTENT
    assert result.requires_confirmation is True
    assert result.interview_required is True
    assert result.direct_run_allowed is False


def test_targetless_close_intent_falls_back_to_interview() -> None:
    result = classify_operational_task("close it — superseded")
    assert result.kind == MERGE_INTENT
    assert result.side_effect_risk == RISK_DESTRUCTIVE_CLOSE
    assert result.requires_confirmation is True
    assert result.interview_required is True
    assert result.direct_run_allowed is False


def test_pr_url_recognized_with_korean_particle_suffix() -> None:
    """A canonical PR URL immediately followed by a Hangul particle (which
    Python regex treats as a word character) MUST still be detected — the
    same boundary fix applied to /pulls index URLs is now applied to
    /pull/<n>. (Bot-flagged in #719 review.)"""
    result = classify_operational_task("https://github.com/o/r/pull/1을 리뷰해줘")
    assert result.kind == PR_URL
    assert result.targets == ("https://github.com/o/r/pull/1",)
    assert result.interview_required is False


def test_issue_url_recognized_with_korean_particle_suffix() -> None:
    """Same boundary fix for /issues/<n> URLs."""
    result = classify_operational_task("https://github.com/o/r/issues/42를 확인해줘")
    assert result.kind == ISSUE_URL
    assert result.targets == ("https://github.com/o/r/issues/42",)
    assert result.interview_required is False


def test_pr_url_does_not_match_partial_digit_run() -> None:
    """The negative lookahead prevents `pull/1` from matching inside
    `pull/12345` so the captured number is exactly the canonical id."""
    result = classify_operational_task("review https://github.com/o/r/pull/12345")
    assert result.targets == ("https://github.com/o/r/pull/12345",)


def test_targets_preserve_first_seen_order_across_types() -> None:
    """A goal mixing /issues/ and /pull/ URLs returns targets in actual
    first-appearance order — the previous per-class loop returned them in
    the wrong order. (Bot-flagged in #719 review.)"""
    goal = (
        "first https://github.com/o/r/issues/9 then "
        "https://github.com/o/r/pull/1 and https://github.com/o/r/pulls"
    )
    result = classify_operational_task(goal)
    assert result.targets == (
        "https://github.com/o/r/issues/9",
        "https://github.com/o/r/pull/1",
        "https://github.com/o/r/pulls",
    )


# ---------------------------------------------------------------------------
# Bot review follow-ups (#719 + #721, second round)
# ---------------------------------------------------------------------------


def test_repo_path_with_review_intent_is_operational_no_url_required() -> None:
    """Module docstring lists ``fix the failing tests in <repo>`` as an
    operational ask; an ``owner/repo`` identifier paired with a review/fix
    intent must skip the interview rather than fall back to it.
    (Bot-flagged in #719 review, second round.)"""
    result = classify_operational_task("fix the failing tests in shaun0927/opensafari")
    assert result.interview_required is False
    assert result.direct_run_allowed is True
    assert "shaun0927/opensafari" in result.targets
    assert "repo_path" in result.reasons


def test_repo_path_alone_without_intent_still_requires_interview() -> None:
    """A bare ``owner/repo`` without any review/merge intent is too thin to
    act on — keep interview-first."""
    result = classify_operational_task("look at owner/repo sometime")
    assert result.interview_required is True
    assert result.direct_run_allowed is False


def test_close_adjective_is_not_destructive_intent() -> None:
    """``take a close look at <url>`` MUST NOT classify as destructive_close.
    (Bot-flagged in #721 review.) The PR url alone keeps the kind PR_URL
    and the side-effect risk stays low (review keyword present)."""
    result = classify_operational_task("take a close look at https://github.com/o/r/pull/1")
    assert result.kind == PR_URL
    assert result.side_effect_risk != RISK_DESTRUCTIVE_CLOSE
    assert result.requires_confirmation is False
    assert "close_keyword" not in result.reasons


@pytest.mark.parametrize(
    "phrase",
    [
        "close look",
        "close call",
        "close to merging",
        "close eye on this",
        "close attention",
        "close enough",
    ],
)
def test_close_adjectival_phrases_do_not_trigger_destructive(phrase: str) -> None:
    """A range of common ``close <word>`` adjectival/idiomatic uses must NOT
    be promoted to ``destructive_close``."""
    result = classify_operational_task(f"please {phrase} on https://github.com/o/r/pull/1")
    assert result.side_effect_risk != RISK_DESTRUCTIVE_CLOSE
    assert "close_keyword" not in result.reasons


def test_close_imperative_still_marks_destructive_close() -> None:
    """Make sure the negative-lookahead refinement does NOT lose the real
    destructive-close case (``close <url>``)."""
    result = classify_operational_task("close https://github.com/o/r/pull/2")
    assert result.kind == MERGE_INTENT
    assert result.side_effect_risk == RISK_DESTRUCTIVE_CLOSE
    assert result.requires_confirmation is True


def test_repo_path_does_not_double_count_when_inside_url() -> None:
    """A PR URL contains an ``owner/repo`` substring; the targets list MUST
    NOT include the bare identifier in addition to the URL."""
    result = classify_operational_task("review https://github.com/owner/repo/pull/1")
    assert result.targets == ("https://github.com/owner/repo/pull/1",)
    # The repo identifier alone should not appear as an extra target.
    assert "owner/repo" not in result.targets


# Bot follow-up: false-positive prose phrases must NOT be classified as repos.
# (Bot-flagged in #719 / #721 review.)
@pytest.mark.parametrize(
    "phrase",
    [
        "and/or",
        "yes/no",
        "true/false",
        "input/output",
        "to/from",
        "either/or",
        "n/a",
        "foo/bar",
        "foo/baz",
        "lorem/ipsum",
    ],
)
def test_prose_slash_phrases_are_not_treated_as_repo_targets(phrase: str) -> None:
    """English idioms and metasyntactic placeholders that share the
    ``a/b`` shape MUST NOT enter ``targets`` and MUST NOT flip the
    pipeline into the direct-run path."""
    result = classify_operational_task(f"review and improve {phrase} docs")
    assert phrase not in result.targets
    assert "repo_path" not in result.reasons
    # Falls back to the interview because no actual target was identified.
    assert result.interview_required is True
    assert result.direct_run_allowed is False


@pytest.mark.parametrize(
    "repo",
    ["owner/r", "o/repo", "a/b1", "u-1/r-2", "u_1/r_2"],
)
def test_short_or_minimal_repo_names_are_recognized(repo: str) -> None:
    """Legitimate but short repo names like ``owner/r`` or ``o/repo`` MUST
    classify as repo targets (with a review/fix verb) instead of falling
    back to the interview path. (Bot-flagged in #719 review, third round.)"""
    result = classify_operational_task(f"fix the failing tests in {repo}")
    assert repo in result.targets
    assert result.interview_required is False
    assert result.direct_run_allowed is True


def test_repo_with_digits_or_dashes_recognized() -> None:
    """Realistic repo names with digits / dashes / dots (no idiom shape)
    classify as targets immediately."""
    result = classify_operational_task("fix failing tests in shaun0927/ouroboros-ai")
    assert "shaun0927/ouroboros-ai" in result.targets
    assert result.interview_required is False
