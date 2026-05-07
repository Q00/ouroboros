"""GitHub CLI adapter that produces ``PullRequestStatus`` for the gate.

The merge-policy gate (PR-D) is a pure function over an observed
``PullRequestStatus``.  This module is the only place where the auto
pipeline talks to GitHub for that status, and it talks via the
``gh`` CLI rather than a HTTP client because:

- ``gh`` already handles authentication via the user's existing
  credential store.
- The CLI is what operators run interactively, so the auth surface is
  identical between manual and automated invocations.
- Tests can inject a fake ``CommandRunner`` instead of monkey-patching
  ``subprocess`` or running an HTTP server.

The adapter is **read-only**.  It never invokes a destructive ``gh
pr merge`` or ``gh pr close``; the gate decides whether such an
action is allowed and the actual mutation lives in PR-F's wiring.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import subprocess
from typing import Any

from ouroboros.auto.merge_policy import (
    CIState,
    PullRequestStatus,
    ReviewState,
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Captured output of a ``gh`` CLI invocation.

    Adapter consumers only need the parsed JSON or the captured stderr
    on failure, so we keep the wrapper minimal.
    """

    returncode: int
    stdout: str
    stderr: str


CommandRunner = Callable[[Sequence[str]], CommandResult]


def _default_runner(args: Sequence[str]) -> CommandResult:
    """Run ``args`` synchronously with a short timeout.

    A 30-second cap is generous for ``gh pr view`` and short enough to
    fail loudly when the network or auth has melted.  No retries: a
    transient failure should be visible to the operator who can re-run
    the auto session.
    """
    completed = subprocess.run(  # noqa: S603 - args are already a list, no shell
        list(args),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


_REVIEW_STATE_MAP: dict[str, ReviewState] = {
    "APPROVED": ReviewState.APPROVED,
    "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
    "COMMENTED": ReviewState.COMMENTED,
    "PENDING": ReviewState.PENDING,
}


_CI_STATE_MAP: dict[str, CIState] = {
    "SUCCESS": CIState.SUCCESS,
    "PENDING": CIState.PENDING,
    "FAILURE": CIState.FAILURE,
    "ERROR": CIState.FAILURE,
    "CANCELLED": CIState.FAILURE,
    "TIMED_OUT": CIState.FAILURE,
    "ACTION_REQUIRED": CIState.FAILURE,
    "EXPECTED": CIState.PENDING,
    "QUEUED": CIState.PENDING,
    "IN_PROGRESS": CIState.PENDING,
    "WAITING": CIState.PENDING,
    "NEUTRAL": CIState.SUCCESS,
    "SKIPPED": CIState.SUCCESS,
    "STALE": CIState.PENDING,
    "STARTUP_FAILURE": CIState.FAILURE,
}


_PR_FIELDS = (
    "mergeable",
    "mergeStateStatus",
    "headRefOid",
    "baseRefName",
    "isDraft",
    "reviewDecision",
    "latestReviews",
    "statusCheckRollup",
)


@dataclass(frozen=True, slots=True)
class GhPrProvider:
    """Read-only adapter that turns ``gh`` CLI output into gate input."""

    runner: CommandRunner = _default_runner

    def fetch_status(self, repo: str, number: int) -> PullRequestStatus:
        """Return the gate-relevant facts for ``repo`` PR ``number``.

        Raises ``GhProviderError`` when the CLI is unavailable, the
        caller is not authenticated, or the response cannot be parsed.
        Routine "missing data" cases (mergeability still computing, no
        statuses reported yet) are encoded in the returned status, not
        raised, so the gate can produce its own actionable block.
        """
        if not isinstance(repo, str) or repo.count("/") != 1:
            msg = f"repo must be in the form owner/repo; got {repo!r}"
            raise ValueError(msg)
        if not isinstance(number, int) or number <= 0:
            msg = f"PR number must be a positive integer; got {number!r}"
            raise ValueError(msg)

        pr_payload = self._gh_pr_view(repo, number)
        permission_payload = self._gh_repo_permission(repo)

        return _build_status(repo=repo, number=number, pr=pr_payload, perm=permission_payload)

    def _gh_pr_view(self, repo: str, number: int) -> dict[str, Any]:
        result = self.runner(
            (
                "gh",
                "pr",
                "view",
                str(number),
                "--repo",
                repo,
                "--json",
                ",".join(_PR_FIELDS),
            )
        )
        if result.returncode != 0:
            msg = (
                f"gh pr view failed for {repo}#{number}: "
                f"{result.stderr.strip() or 'no stderr output'}"
            )
            raise GhProviderError(msg)
        return _parse_json(result.stdout, context=f"gh pr view {repo}#{number}")

    def _gh_repo_permission(self, repo: str) -> dict[str, Any]:
        result = self.runner(("gh", "api", f"repos/{repo}", "--jq", ".permissions"))
        if result.returncode != 0:
            msg = (
                f"gh api repos/{repo} (permissions) failed: "
                f"{result.stderr.strip() or 'no stderr output'}"
            )
            raise GhProviderError(msg)
        # ``--jq .permissions`` may emit ``null`` if the user has no
        # explicit permissions block; default to denying write so the
        # gate fails closed without a noisy error.
        text = result.stdout.strip()
        if not text or text == "null":
            return {}
        return _parse_json(text, context=f"gh api repos/{repo} permissions")


class GhProviderError(RuntimeError):
    """Raised when the ``gh`` CLI cannot produce a usable status payload."""


def _parse_json(text: str, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{context} returned non-JSON output: {exc}"
        raise GhProviderError(msg) from exc
    if not isinstance(payload, dict):
        msg = f"{context} returned a non-object payload: {type(payload).__name__}"
        raise GhProviderError(msg)
    return payload


def _coerce_mergeable(mergeable_field: object, merge_state: object) -> bool | None:
    """Normalize gh's mergeable field into a tri-state bool.

    ``mergeable`` from gh is either ``"MERGEABLE"``, ``"CONFLICTING"`` or
    ``"UNKNOWN"``.  ``mergeStateStatus`` adds ``"BLOCKED"`` (rules block
    merge), ``"BEHIND"`` (needs rebase) which both mean "not mergeable
    right now".  Anything we cannot map reliably is reported as ``None``
    so the gate produces a "wait and re-run" block.
    """
    mergeable_text = str(mergeable_field or "").upper()
    state_text = str(merge_state or "").upper()
    if mergeable_text == "MERGEABLE" and state_text in {"CLEAN", "UNSTABLE", "HAS_HOOKS"}:
        return True
    if mergeable_text == "CONFLICTING" or state_text in {"DIRTY", "BLOCKED", "BEHIND"}:
        return False
    return None


def _coerce_ci_state(rollup: object) -> CIState:
    """Map gh's ``statusCheckRollup`` into a single coarse CI tier.

    Pessimistic merge: any failure dominates; a single pending check
    keeps the rollup pending; only an entirely-success list yields
    ``SUCCESS``.

    Fail-closed shape handling:

    - A non-list payload (the ``gh`` schema changed or returned ``null``)
      maps to ``UNKNOWN`` so the gate cannot be silently passed by a
      malformed response.
    - An empty list is treated as ``SUCCESS`` because GitHub uses an
      empty rollup to signal "no required checks defined".
    - A list whose items are all non-dict (or otherwise unparseable)
      maps to ``UNKNOWN`` rather than ``SUCCESS``.
    """
    if not isinstance(rollup, list):
        return CIState.UNKNOWN
    if not rollup:
        return CIState.SUCCESS
    states: list[CIState] = []
    for item in rollup:
        if not isinstance(item, dict):
            continue
        # ``state`` is set for status contexts; ``conclusion`` is set
        # for completed check runs; ``status`` is "QUEUED"/"IN_PROGRESS"
        # for in-flight check runs.  Take the most informative value.
        raw = item.get("conclusion") or item.get("state") or item.get("status")
        if not isinstance(raw, str) or not raw:
            states.append(CIState.UNKNOWN)
            continue
        states.append(_CI_STATE_MAP.get(raw.upper(), CIState.UNKNOWN))
    if not states:
        # The list contained items but none were parseable.  Treat this
        # as a malformed payload, not as an empty rollup.
        return CIState.UNKNOWN
    if any(state is CIState.FAILURE for state in states):
        return CIState.FAILURE
    if any(state is CIState.PENDING for state in states):
        return CIState.PENDING
    if any(state is CIState.UNKNOWN for state in states):
        return CIState.UNKNOWN
    return CIState.SUCCESS


def _coerce_reviews(reviews: object, decision: object) -> tuple[ReviewState, ...]:
    """Combine ``latestReviews`` with the aggregate ``reviewDecision``.

    GitHub's ``reviewDecision`` is the authoritative aggregate that the
    repository UI displays: an approver who later leaves a non-blocking
    ``COMMENTED`` review keeps the PR's aggregate at ``APPROVED`` even
    though the reviewer's *latest* review state changed.  The previous
    implementation rebuilt approval state from ``latestReviews`` alone
    and would block such PRs.

    Rules (the gate then enforces "≥1 APPROVED and 0 CHANGES_REQUESTED"
    on its own):

    - ``decision == "APPROVED"`` → return a single synthetic
      ``APPROVED`` so the gate sees the aggregate, ignoring noise from
      ``COMMENTED`` reviews.
    - ``decision == "CHANGES_REQUESTED"`` → use ``latestReviews`` so the
      gate sees the actual blocking reviewers.
    - ``decision in {"REVIEW_REQUIRED", None, ""}`` → use
      ``latestReviews`` so the gate decides on raw reviewer state.
    """
    decision_text = (decision or "").upper() if isinstance(decision, str) else ""
    if decision_text == "APPROVED":
        return (ReviewState.APPROVED,)
    if not isinstance(reviews, list):
        return ()
    out: list[ReviewState] = []
    for item in reviews:
        if not isinstance(item, dict):
            continue
        raw = item.get("state")
        if not isinstance(raw, str):
            continue
        mapped = _REVIEW_STATE_MAP.get(raw.upper())
        if mapped is not None:
            out.append(mapped)
    return tuple(out)


def _coerce_permission(payload: dict[str, Any]) -> bool:
    """Treat ``push``/``maintain``/``admin`` as sufficient for merge."""
    return any(bool(payload.get(key)) for key in ("admin", "maintain", "push"))


def _build_status(
    *,
    repo: str,
    number: int,
    pr: dict[str, Any],
    perm: dict[str, Any],
) -> PullRequestStatus:
    target_branch = pr.get("baseRefName")
    head_sha = pr.get("headRefOid")
    if not isinstance(target_branch, str) or not target_branch.strip():
        msg = f"gh pr view payload missing baseRefName for {repo}#{number}"
        raise GhProviderError(msg)
    if not isinstance(head_sha, str) or not head_sha.strip():
        msg = f"gh pr view payload missing headRefOid for {repo}#{number}"
        raise GhProviderError(msg)
    return PullRequestStatus(
        repo=repo,
        number=number,
        target_branch=target_branch,
        head_sha=head_sha,
        mergeable=_coerce_mergeable(pr.get("mergeable"), pr.get("mergeStateStatus")),
        ci_state=_coerce_ci_state(pr.get("statusCheckRollup")),
        review_states=_coerce_reviews(pr.get("latestReviews"), pr.get("reviewDecision")),
        has_write_permission=_coerce_permission(perm),
        is_draft=bool(pr.get("isDraft")),
    )


__all__ = [
    "CommandResult",
    "CommandRunner",
    "GhPrProvider",
    "GhProviderError",
]
