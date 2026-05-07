"""Merge-policy gate contract for the direct operational path (#689 PR-D).

Operational goals can request destructive actions (merge a PR, delete a
branch).  The classifier (PR-A) recognizes the *shape* of the goal but
never inspects repository state — it cannot tell whether a particular
PR is mergeable, whether CI is green, or whether the caller has
sufficient permission.  This module is the in-process **gate** that
turns observed repository facts into an explicit ``allowed/blocked``
decision before the auto pipeline performs anything destructive.

Design constraints:

- **No IO**: the gate is a pure function.  Tests run without network or
  GitHub credentials.  PR-E provides the adapter that fetches
  ``PullRequestStatus`` from ``gh`` CLI.
- **Fail-closed**: every check defaults to *blocked* when evidence is
  missing.  A reviewer who is reading the diff should never have to
  reason about an implicit allow path.
- **Audit trail**: every decision (allow or block) is recordable on
  ``SeedDraftLedger.record_merge_policy_decision`` so resume can show
  exactly which checks fired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from ouroboros.auto.goal_classifier import GoalClassification, SideEffectRisk


class CIState(StrEnum):
    """Coarse CI status used by the gate."""

    SUCCESS = "success"
    PENDING = "pending"
    FAILURE = "failure"
    UNKNOWN = "unknown"


class ReviewState(StrEnum):
    """GitHub review decision states the gate cares about."""

    APPROVED = "approved"
    CHANGES_REQUESTED = "changes_requested"
    COMMENTED = "commented"
    PENDING = "pending"


@dataclass(frozen=True, slots=True)
class PullRequestStatus:
    """Observed facts about the PR a destructive action targets.

    The shape is the minimum the gate needs.  PR-E will populate this
    from ``gh`` CLI output; tests construct it directly.
    """

    repo: str
    number: int
    target_branch: str
    head_sha: str
    mergeable: bool | None
    ci_state: CIState
    review_states: tuple[ReviewState, ...]
    has_write_permission: bool
    is_draft: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary for ledger persistence."""
        return {
            "repo": self.repo,
            "number": self.number,
            "target_branch": self.target_branch,
            "head_sha": self.head_sha,
            "mergeable": self.mergeable,
            "ci_state": self.ci_state.value,
            "review_states": [state.value for state in self.review_states],
            "has_write_permission": self.has_write_permission,
            "is_draft": self.is_draft,
        }


class MergeAction(StrEnum):
    """Destructive actions the gate evaluates."""

    MERGE = "merge"
    CLOSE = "close"
    REBASE = "rebase"
    DELETE_BRANCH = "delete_branch"


# Minimum classification risk required to authorize each destructive
# action.  The merge action is the most destructive and demands a HIGH
# verb in the goal (``merge``, ``머지`` …); CLOSE/REBASE only need a
# MEDIUM verb (``close``, ``rebase``, ``fix``).  A LOW (read-only)
# classification cannot authorize any of these actions: a goal like
# ``review https://.../pull/689`` must never trigger a default-action
# merge just because the repo state happens to be clean.
_ACTION_MIN_RISK: dict[MergeAction, SideEffectRisk] = {
    MergeAction.MERGE: SideEffectRisk.HIGH,
    MergeAction.DELETE_BRANCH: SideEffectRisk.HIGH,
    MergeAction.CLOSE: SideEffectRisk.MEDIUM,
    MergeAction.REBASE: SideEffectRisk.MEDIUM,
}

_RISK_RANK: dict[SideEffectRisk, int] = {
    SideEffectRisk.NONE: 0,
    SideEffectRisk.LOW: 1,
    SideEffectRisk.MEDIUM: 2,
    SideEffectRisk.HIGH: 3,
}


@dataclass(frozen=True, slots=True)
class MergePolicyDecision:
    """Outcome of evaluating ``PullRequestStatus`` against the gate."""

    allowed: bool
    action: MergeAction
    blocking_reasons: tuple[str, ...] = ()
    required_actions: tuple[str, ...] = ()
    matched_signals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "allowed": self.allowed,
            "action": self.action.value,
            "blocking_reasons": list(self.blocking_reasons),
            "required_actions": list(self.required_actions),
            "matched_signals": list(self.matched_signals),
        }


@dataclass(frozen=True, slots=True)
class MergePolicy:
    """Configurable thresholds for the gate."""

    allowed_target_branches: frozenset[str] = field(
        default_factory=lambda: frozenset({"main", "master"})
    )
    require_approval: bool = True
    require_passing_ci: bool = True
    block_on_draft: bool = True
    # When the classifier marked the goal high-risk but did not also
    # require confirmation, fail closed: the gate refuses to evaluate
    # without an explicit confirmation signal because that combination
    # would mean the classifier mis-rated the destructiveness.
    require_confirmation_for_high_risk: bool = True
    # When the requested action exceeds the goal's classified intent
    # (e.g. action=MERGE for a goal classified LOW), fail closed so a
    # clean PR plus a read-only goal cannot authorize a destructive
    # default action.  Disable only in tests that want to exercise
    # repository-only checks in isolation.
    require_action_classification_match: bool = True


def evaluate_merge(
    *,
    classification: GoalClassification,
    status: PullRequestStatus,
    policy: MergePolicy | None = None,
    action: MergeAction = MergeAction.MERGE,
) -> MergePolicyDecision:
    """Run the full set of merge-policy checks.

    The function is total: it never raises for routine "blocked"
    outcomes.  It only raises ``TypeError`` when the caller passed
    structurally-invalid input.
    """
    if not isinstance(classification, GoalClassification):
        msg = "evaluate_merge requires a GoalClassification"
        raise TypeError(msg)
    if not isinstance(status, PullRequestStatus):
        msg = "evaluate_merge requires a PullRequestStatus"
        raise TypeError(msg)
    rules = policy or MergePolicy()
    blocking_reasons: list[str] = []
    required_actions: list[str] = []
    matched_signals: list[str] = []

    if (
        rules.require_confirmation_for_high_risk
        and classification.side_effect_risk is SideEffectRisk.HIGH
        and not classification.requires_confirmation
    ):
        blocking_reasons.append(
            "classification reports HIGH side-effect risk without requires_confirmation; "
            "gate fails closed to prevent silent destructive action"
        )
        required_actions.append(
            "Re-classify the goal or update GoalClassification semantics so a HIGH risk "
            "always forces requires_confirmation=True."
        )

    if rules.require_action_classification_match:
        required_min = _ACTION_MIN_RISK.get(action)
        if required_min is None:
            blocking_reasons.append(
                f"action {action.value!r} has no policy-defined minimum risk; "
                "gate fails closed for unrecognized actions"
            )
            required_actions.append(
                "Add the action to _ACTION_MIN_RISK in merge_policy.py before "
                "wiring it into the auto pipeline."
            )
            matched_signals.append(f"action:{action.value}=unmapped")
        elif _RISK_RANK[classification.side_effect_risk] < _RISK_RANK[required_min]:
            blocking_reasons.append(
                f"goal classified as {classification.side_effect_risk.value} risk "
                f"does not authorize action {action.value!r} "
                f"(requires {required_min.value} risk)"
            )
            required_actions.append(
                "Restate the goal with a verb that matches the requested action "
                f"(for {action.value!r}, use a {required_min.value}-risk verb), "
                "or invoke the gate with an action consistent with the goal."
            )
            matched_signals.append(
                f"action:{action.value}=needs:{required_min.value}"
                f"(have:{classification.side_effect_risk.value})"
            )
        else:
            matched_signals.append(f"action:{action.value}=ok")

    if not status.has_write_permission:
        blocking_reasons.append("caller lacks repository write permission")
        required_actions.append(
            "Run with credentials that have push/merge access to the target repository."
        )
        matched_signals.append("permission:none")
    else:
        matched_signals.append("permission:write")

    if status.target_branch not in rules.allowed_target_branches:
        allowed = ", ".join(sorted(rules.allowed_target_branches))
        blocking_reasons.append(
            f"target branch {status.target_branch!r} not in allowed set ({allowed})"
        )
        required_actions.append(
            "Re-target the PR onto an allowed branch or extend the merge policy."
        )
        matched_signals.append(f"branch:{status.target_branch}")
    else:
        matched_signals.append(f"branch:{status.target_branch}")

    if rules.block_on_draft and status.is_draft:
        blocking_reasons.append("PR is in draft state")
        required_actions.append("Mark the PR ready for review before merging.")
        matched_signals.append("draft:true")

    if status.mergeable is False:
        blocking_reasons.append("GitHub reports the PR is not mergeable")
        required_actions.append(
            "Resolve conflicts on the PR head branch, then re-run with --resume."
        )
        matched_signals.append("mergeable:false")
    elif status.mergeable is None:
        blocking_reasons.append("GitHub mergeability check has not finished yet")
        required_actions.append(
            "Wait for GitHub to compute mergeability, then re-run with --resume."
        )
        matched_signals.append("mergeable:unknown")
    else:
        matched_signals.append("mergeable:true")

    if rules.require_passing_ci and status.ci_state is not CIState.SUCCESS:
        blocking_reasons.append(
            f"CI state is {status.ci_state.value}; require success before merging"
        )
        required_actions.append("Wait for CI to finish; investigate failures before re-running.")
        matched_signals.append(f"ci:{status.ci_state.value}")
    else:
        matched_signals.append(f"ci:{status.ci_state.value}")

    if rules.require_approval and action is MergeAction.MERGE:
        approved = sum(state is ReviewState.APPROVED for state in status.review_states)
        changes_requested = sum(
            state is ReviewState.CHANGES_REQUESTED for state in status.review_states
        )
        if changes_requested:
            blocking_reasons.append(f"{changes_requested} reviewer(s) requested changes")
            required_actions.append("Address review feedback and re-request review.")
            matched_signals.append(f"reviews:changes_requested:{changes_requested}")
        if approved == 0:
            blocking_reasons.append("no approving reviews on the PR")
            required_actions.append("Obtain at least one approving review.")
            matched_signals.append("reviews:approved:0")
        else:
            matched_signals.append(f"reviews:approved:{approved}")

    return MergePolicyDecision(
        allowed=not blocking_reasons,
        action=action,
        blocking_reasons=tuple(blocking_reasons),
        required_actions=tuple(required_actions),
        matched_signals=tuple(matched_signals),
    )


def record_decision_on_ledger(
    ledger: object,
    decision: MergePolicyDecision,
    status: PullRequestStatus,
) -> dict[str, Any]:
    """Append a structured audit record to ``ledger.merge_policy_decisions``.

    Accepts the ledger duck-typed (it must expose
    ``record_merge_policy_decision``) so this module does not import
    ``SeedDraftLedger`` and can be reused by the MCP-side handler later
    without circular imports.
    """
    record = {
        "recorded_at": datetime.now(UTC).isoformat(),
        "status": status.to_dict(),
        "decision": decision.to_dict(),
    }
    recorder = getattr(ledger, "record_merge_policy_decision", None)
    if recorder is None:
        msg = (
            "ledger object must implement record_merge_policy_decision; "
            "got " + type(ledger).__name__
        )
        raise TypeError(msg)
    recorder(record)
    return record


__all__ = [
    "CIState",
    "MergeAction",
    "MergePolicy",
    "MergePolicyDecision",
    "PullRequestStatus",
    "ReviewState",
    "evaluate_merge",
    "record_decision_on_ledger",
]
