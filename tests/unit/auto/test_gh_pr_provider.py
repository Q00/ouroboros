"""Tests for the gh-CLI provider adapter (#689 PR-E)."""

from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from ouroboros.auto.gh_pr_provider import (
    CommandResult,
    GhPrProvider,
    GhProviderError,
)
from ouroboros.auto.merge_policy import CIState, ReviewState


class _FakeRunner:
    """Tiny scripted ``gh`` CLI replacement.

    Maps invoked argument tuples to canned ``CommandResult`` objects so
    tests can describe the exact set of CLI calls they expect.
    """

    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> CommandResult:
        key = tuple(args)
        self.calls.append(key)
        try:
            return self.responses[key]
        except KeyError as exc:  # pragma: no cover - test misconfiguration
            msg = f"unexpected gh invocation: {key}"
            raise AssertionError(msg) from exc


def _pr_view_payload(**overrides) -> dict:
    base = {
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": "deadbeef",
        "baseRefName": "main",
        "isDraft": False,
        "reviewDecision": "APPROVED",
        "latestReviews": [{"state": "APPROVED"}],
        "statusCheckRollup": [{"conclusion": "SUCCESS"}],
    }
    base.update(overrides)
    return base


def _make_provider(
    *,
    pr: dict | None = None,
    pr_returncode: int = 0,
    pr_stderr: str = "",
    permissions: dict | str | None = None,
    perm_returncode: int = 0,
    perm_stderr: str = "",
    repo: str = "Q00/ouroboros",
    number: int = 689,
) -> tuple[GhPrProvider, _FakeRunner]:
    pr_payload = json.dumps(pr if pr is not None else _pr_view_payload())
    perm_payload = (
        json.dumps(permissions)
        if isinstance(permissions, dict)
        else (permissions if isinstance(permissions, str) else json.dumps({"push": True}))
    )
    runner = _FakeRunner(
        {
            (
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                "mergeable,mergeStateStatus,headRefOid,baseRefName,"
                "isDraft,reviewDecision,latestReviews,statusCheckRollup",
            ): CommandResult(
                returncode=pr_returncode, stdout=pr_payload, stderr=pr_stderr
            ),
            ("gh", "api", f"repos/{repo}", "--jq", ".permissions"): CommandResult(
                returncode=perm_returncode, stdout=perm_payload, stderr=perm_stderr
            ),
        }
    )
    return GhPrProvider(runner=runner), runner


def test_fetch_status_returns_clean_status_for_clean_payload() -> None:
    provider, runner = _make_provider()
    status = provider.fetch_status("Q00/ouroboros", 689)

    assert status.repo == "Q00/ouroboros"
    assert status.number == 689
    assert status.target_branch == "main"
    assert status.head_sha == "deadbeef"
    assert status.mergeable is True
    assert status.ci_state is CIState.SUCCESS
    assert status.review_states == (ReviewState.APPROVED,)
    assert status.has_write_permission is True
    assert status.is_draft is False
    # Both subcommands are invoked exactly once.
    assert len(runner.calls) == 2


def test_fetch_status_treats_unknown_mergeability_as_none() -> None:
    provider, _ = _make_provider(
        pr=_pr_view_payload(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN")
    )
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.mergeable is None


def test_fetch_status_marks_conflicting_pr_unmergeable() -> None:
    provider, _ = _make_provider(
        pr=_pr_view_payload(mergeable="CONFLICTING", mergeStateStatus="DIRTY")
    )
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.mergeable is False


def test_fetch_status_marks_blocked_state_unmergeable() -> None:
    provider, _ = _make_provider(
        pr=_pr_view_payload(mergeable="MERGEABLE", mergeStateStatus="BLOCKED")
    )
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.mergeable is False


def test_ci_rollup_pending_dominates_when_no_failures() -> None:
    provider, _ = _make_provider(
        pr=_pr_view_payload(
            statusCheckRollup=[
                {"conclusion": "SUCCESS"},
                {"status": "IN_PROGRESS"},
            ]
        )
    )
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.ci_state is CIState.PENDING


def test_ci_rollup_failure_dominates_pending_and_success() -> None:
    provider, _ = _make_provider(
        pr=_pr_view_payload(
            statusCheckRollup=[
                {"conclusion": "SUCCESS"},
                {"status": "IN_PROGRESS"},
                {"conclusion": "FAILURE"},
            ]
        )
    )
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.ci_state is CIState.FAILURE


def test_no_status_checks_treats_ci_as_success() -> None:
    provider, _ = _make_provider(pr=_pr_view_payload(statusCheckRollup=[]))
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.ci_state is CIState.SUCCESS


def test_review_state_mapping_filters_unknown_values() -> None:
    provider, _ = _make_provider(
        pr=_pr_view_payload(
            latestReviews=[
                {"state": "APPROVED"},
                {"state": "CHANGES_REQUESTED"},
                {"state": "DISMISSED"},  # not in our enum, dropped
            ]
        )
    )
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.review_states == (
        ReviewState.APPROVED,
        ReviewState.CHANGES_REQUESTED,
    )


def test_permission_admin_grants_write() -> None:
    provider, _ = _make_provider(permissions={"admin": True})
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.has_write_permission is True


def test_permission_pull_only_denies_write() -> None:
    provider, _ = _make_provider(permissions={"pull": True})
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.has_write_permission is False


def test_permission_null_payload_denies_write() -> None:
    provider, _ = _make_provider(permissions="null")
    status = provider.fetch_status("Q00/ouroboros", 689)
    assert status.has_write_permission is False


def test_pr_view_failure_raises_provider_error() -> None:
    provider, _ = _make_provider(
        pr_returncode=1, pr_stderr="HTTP 404: Not Found"
    )
    with pytest.raises(GhProviderError, match="HTTP 404"):
        provider.fetch_status("Q00/ouroboros", 689)


def test_permission_failure_raises_provider_error() -> None:
    provider, _ = _make_provider(
        perm_returncode=1, perm_stderr="auth required"
    )
    with pytest.raises(GhProviderError, match="auth required"):
        provider.fetch_status("Q00/ouroboros", 689)


def test_invalid_json_raises_provider_error() -> None:
    runner = _FakeRunner(
        {
            (
                "gh",
                "pr",
                "view",
                "689",
                "--repo",
                "Q00/ouroboros",
                "--json",
                "mergeable,mergeStateStatus,headRefOid,baseRefName,"
                "isDraft,reviewDecision,latestReviews,statusCheckRollup",
            ): CommandResult(returncode=0, stdout="not json", stderr=""),
        }
    )
    provider = GhPrProvider(runner=runner)
    with pytest.raises(GhProviderError, match="non-JSON output"):
        provider.fetch_status("Q00/ouroboros", 689)


def test_missing_target_branch_raises() -> None:
    payload = _pr_view_payload(baseRefName="")
    provider, _ = _make_provider(pr=payload)
    with pytest.raises(GhProviderError, match="baseRefName"):
        provider.fetch_status("Q00/ouroboros", 689)


def test_missing_head_sha_raises() -> None:
    payload = _pr_view_payload(headRefOid="")
    provider, _ = _make_provider(pr=payload)
    with pytest.raises(GhProviderError, match="headRefOid"):
        provider.fetch_status("Q00/ouroboros", 689)


def test_invalid_repo_format_rejected() -> None:
    provider, _ = _make_provider()
    with pytest.raises(ValueError, match="owner/repo"):
        provider.fetch_status("not-a-repo", 689)


def test_invalid_pr_number_rejected() -> None:
    provider, _ = _make_provider()
    with pytest.raises(ValueError, match="positive integer"):
        provider.fetch_status("Q00/ouroboros", 0)


def test_provider_status_feeds_gate_unchanged() -> None:
    """End-to-end smoke: provider output → gate decision is allow."""
    from ouroboros.auto.goal_classifier import classify_goal
    from ouroboros.auto.merge_policy import evaluate_merge

    provider, _ = _make_provider()
    status = provider.fetch_status("Q00/ouroboros", 689)
    classification = classify_goal(
        "merge https://github.com/Q00/ouroboros/pull/689 once CI is green"
    )

    decision = evaluate_merge(classification=classification, status=status)
    assert decision.allowed is True
