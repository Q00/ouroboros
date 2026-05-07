"""Deterministic classifier for auto-mode goal strings.

Most ``ooo auto`` goals are exploratory ideas that benefit from a Socratic
interview before Seed generation.  A growing class of goals, however, are
*operational*: they target an existing artifact (a PR or issue URL) and ask
for a concrete action (merge, review, fix, close, rebase).  For those goals
the interview adds only latency and surface area for ``interview.start``
timeouts (#686, #689).

This module is a pure function that turns a free-form goal string into a
``GoalClassification``.  It performs no IO, network calls, or model
invocations and therefore cannot itself stall the auto pipeline.  Callers
decide whether to act on the classification (PR-C wires routing); landing
this module changes no observable behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re


class SideEffectRisk(StrEnum):
    """Coarse risk tier for the action implied by an operational goal."""

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


_RISK_RANK: dict[SideEffectRisk, int] = {
    SideEffectRisk.NONE: 0,
    SideEffectRisk.LOW: 1,
    SideEffectRisk.MEDIUM: 2,
    SideEffectRisk.HIGH: 3,
}


def _max_risk(*risks: SideEffectRisk) -> SideEffectRisk:
    return max(risks, key=lambda r: _RISK_RANK[r])


@dataclass(frozen=True, slots=True)
class GoalClassification:
    """Structured summary of a free-form auto goal string.

    Attributes:
        interview_required: True when the goal lacks enough concrete anchor
            (a PR/issue URL plus an operational verb) to skip the Socratic
            interview safely.
        direct_run_allowed: True when the auto pipeline may bypass the
            interview phase and hand off directly to a bounded operational
            run.  Mutually opposite of ``interview_required`` in the
            current schema, but kept as a separate field so future
            classifiers can add states (for example "interview optional")
            without breaking serialized records.
        side_effect_risk: Coarse risk tier of the implied action.  ``high``
            covers destructive or hard-to-reverse operations (merge, push
            --force, branch deletion).  Always pessimistic: only lowered
            when no risky verb is present.
        requires_confirmation: True when the implied action is irreversible
            enough that the auto pipeline must obtain explicit confirmation
            (policy gate, CI status, reviewer approval) before performing
            it.  Always True when ``side_effect_risk`` is ``high``.
        reason: Short human-readable string explaining the verdict.  Stored
            in the ledger and surfaced in CLI output.
        matched_signals: Snippets that influenced the verdict, in
            discovery order.  Used both for debug output and for tests
            asserting that a particular pattern triggered.
    """

    interview_required: bool
    direct_run_allowed: bool
    side_effect_risk: SideEffectRisk
    requires_confirmation: bool
    reason: str
    matched_signals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, object]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "interview_required": self.interview_required,
            "direct_run_allowed": self.direct_run_allowed,
            "side_effect_risk": self.side_effect_risk.value,
            "requires_confirmation": self.requires_confirmation,
            "reason": self.reason,
            "matched_signals": list(self.matched_signals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GoalClassification:
        """Deserialize from a JSON-compatible dictionary.

        Rejects malformed records eagerly so persisted state cannot smuggle
        unexpected types past the auto pipeline.
        """
        if not isinstance(data, dict):
            msg = "goal classification must be an object"
            raise ValueError(msg)
        required = {
            "interview_required",
            "direct_run_allowed",
            "side_effect_risk",
            "requires_confirmation",
            "reason",
        }
        missing = sorted(required - data.keys())
        if missing:
            msg = (
                "goal classification is missing required fields: "
                f"{', '.join(missing)}"
            )
            raise ValueError(msg)
        for bool_field in (
            "interview_required",
            "direct_run_allowed",
            "requires_confirmation",
        ):
            if not isinstance(data[bool_field], bool):
                msg = f"goal classification {bool_field} must be a boolean"
                raise ValueError(msg)
        risk_raw = data["side_effect_risk"]
        if not isinstance(risk_raw, str):
            msg = "goal classification side_effect_risk must be a string"
            raise ValueError(msg)
        try:
            risk = SideEffectRisk(risk_raw)
        except ValueError as exc:
            msg = f"goal classification side_effect_risk is unknown: {risk_raw}"
            raise ValueError(msg) from exc
        reason = data["reason"]
        if not isinstance(reason, str) or not reason.strip():
            msg = "goal classification reason must be a non-empty string"
            raise ValueError(msg)
        signals_raw = data.get("matched_signals", [])
        if not isinstance(signals_raw, list) or not all(
            isinstance(item, str) for item in signals_raw
        ):
            msg = "goal classification matched_signals must be a list of strings"
            raise ValueError(msg)
        if data["requires_confirmation"] is False and risk is SideEffectRisk.HIGH:
            msg = (
                "goal classification requires_confirmation must be True when "
                "side_effect_risk is high"
            )
            raise ValueError(msg)
        return cls(
            interview_required=data["interview_required"],
            direct_run_allowed=data["direct_run_allowed"],
            side_effect_risk=risk,
            requires_confirmation=data["requires_confirmation"],
            reason=reason,
            matched_signals=tuple(signals_raw),
        )


# A GitHub PR URL can take either of two shapes:
#   https://github.com/<owner>/<repo>/pull/<number>
#   https://github.com/<owner>/<repo>/pulls            (list page)
# Both are sufficient anchors for an operational goal, but ``/pulls`` is
# always treated as ambiguous merge target — the user must still narrow
# the action through interview unless paired with an explicit verb.
_GITHUB_PR_URL = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)\b",
    re.IGNORECASE,
)
_GITHUB_PR_LIST_URL = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pulls\b",
    re.IGNORECASE,
)
_GITHUB_ISSUE_URL = re.compile(
    r"https?://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/issues/(?P<number>\d+)\b",
    re.IGNORECASE,
)


# Operational verbs map to a risk tier.  English and Korean variants are
# both included because ``ooo auto`` is invoked from Korean shells in the
# observed incident (auto_78c98678de5d).
_OPERATIONAL_VERBS: tuple[tuple[re.Pattern[str], SideEffectRisk], ...] = (
    # High-risk: destructive or hard-to-reverse mutations.
    (re.compile(r"\bmerge\b", re.IGNORECASE), SideEffectRisk.HIGH),
    (re.compile(r"\bsquash\b", re.IGNORECASE), SideEffectRisk.HIGH),
    (re.compile(r"\bforce[-\s]?push\b", re.IGNORECASE), SideEffectRisk.HIGH),
    (re.compile(r"\bdelete[\s-]+branch\b", re.IGNORECASE), SideEffectRisk.HIGH),
    (re.compile(r"머지", re.IGNORECASE), SideEffectRisk.HIGH),
    (re.compile(r"머지\s*가능", re.IGNORECASE), SideEffectRisk.HIGH),
    (re.compile(r"merge 가능", re.IGNORECASE), SideEffectRisk.HIGH),
    # Medium-risk: writes to the PR but reversible (closing a PR can be
    # reopened; rebasing rewrites history that has not yet been merged).
    (re.compile(r"\bclose\b", re.IGNORECASE), SideEffectRisk.MEDIUM),
    (re.compile(r"\breopen\b", re.IGNORECASE), SideEffectRisk.MEDIUM),
    (re.compile(r"\brebase\b", re.IGNORECASE), SideEffectRisk.MEDIUM),
    (re.compile(r"\bfix\b", re.IGNORECASE), SideEffectRisk.MEDIUM),
    (re.compile(r"수정", re.IGNORECASE), SideEffectRisk.MEDIUM),
    # Low-risk: read-only or comment-only operations.
    (re.compile(r"\breview\b", re.IGNORECASE), SideEffectRisk.LOW),
    (re.compile(r"\bcomment\b", re.IGNORECASE), SideEffectRisk.LOW),
    (re.compile(r"\banalyz[ei]\b", re.IGNORECASE), SideEffectRisk.LOW),
    (re.compile(r"\bsummariz[ei]\b", re.IGNORECASE), SideEffectRisk.LOW),
    (re.compile(r"리뷰", re.IGNORECASE), SideEffectRisk.LOW),
    (re.compile(r"분석", re.IGNORECASE), SideEffectRisk.LOW),
    (re.compile(r"개선", re.IGNORECASE), SideEffectRisk.MEDIUM),
)


# Verbs/patterns that indicate the goal is *not* yet operational and an
# interview should still run, even if a URL was mentioned for context.
_AMBIGUOUS_INTENT = (
    re.compile(r"\b(plan|design|brainstorm|investigate|explore)\b", re.IGNORECASE),
    re.compile(r"\b(어떻게|어떨까|고민)\b", re.IGNORECASE),
)


def classify_goal(goal: str) -> GoalClassification:
    """Classify a free-form ``ooo auto`` goal string.

    The classifier is deterministic: same input → same verdict.  It does
    not consult external state, environment, repository facts, or
    backends.  Callers needing repository state (CI status, mergeability)
    should fetch it separately and feed the policy gate (PR-D), keeping
    classification cheap and side-effect free.
    """
    if not isinstance(goal, str):
        msg = f"goal must be a string, got {type(goal).__name__}"
        raise TypeError(msg)
    text = goal.strip()
    if not text:
        return GoalClassification(
            interview_required=True,
            direct_run_allowed=False,
            side_effect_risk=SideEffectRisk.NONE,
            requires_confirmation=False,
            reason="empty goal",
            matched_signals=(),
        )

    signals: list[str] = []
    pr_match = _GITHUB_PR_URL.search(text)
    issue_match = _GITHUB_ISSUE_URL.search(text)
    pr_list_match = _GITHUB_PR_LIST_URL.search(text)
    if pr_match:
        signals.append(f"pr_url:{pr_match.group(0)}")
    if issue_match:
        signals.append(f"issue_url:{issue_match.group(0)}")
    if pr_list_match and not pr_match:
        signals.append(f"pr_list_url:{pr_list_match.group(0)}")

    verb_risk = SideEffectRisk.NONE
    for pattern, risk in _OPERATIONAL_VERBS:
        match = pattern.search(text)
        if match:
            signals.append(f"verb:{match.group(0).lower()}={risk.value}")
            verb_risk = _max_risk(verb_risk, risk)

    ambiguous_signals: list[str] = []
    for pattern in _AMBIGUOUS_INTENT:
        match = pattern.search(text)
        if match:
            ambiguous_signals.append(f"ambiguous:{match.group(0).lower()}")

    has_concrete_anchor = bool(pr_match or issue_match)
    has_operational_verb = verb_risk is not SideEffectRisk.NONE

    if ambiguous_signals:
        # Even if an operational verb is present, an ambiguous intent
        # ("plan how we should fix this") needs interview clarity.
        signals.extend(ambiguous_signals)
        return GoalClassification(
            interview_required=True,
            direct_run_allowed=False,
            side_effect_risk=verb_risk,
            requires_confirmation=verb_risk is SideEffectRisk.HIGH,
            reason="goal mixes operational verb with ambiguous planning intent",
            matched_signals=tuple(signals),
        )

    if has_concrete_anchor and has_operational_verb:
        return GoalClassification(
            interview_required=False,
            direct_run_allowed=True,
            side_effect_risk=verb_risk,
            requires_confirmation=verb_risk is SideEffectRisk.HIGH,
            reason=(
                "concrete PR/issue URL paired with operational verb "
                f"({verb_risk.value} risk)"
            ),
            matched_signals=tuple(signals),
        )

    if pr_list_match and has_operational_verb:
        # ``/pulls`` is a list page, not a single PR.  Allow direct path
        # only if the verb is read-only (review, analyze).  Anything that
        # mutates needs the interview to narrow the target.
        if verb_risk in {SideEffectRisk.LOW, SideEffectRisk.NONE}:
            return GoalClassification(
                interview_required=False,
                direct_run_allowed=True,
                side_effect_risk=verb_risk,
                requires_confirmation=False,
                reason="PR list URL with read-only verb permits direct review",
                matched_signals=tuple(signals),
            )
        return GoalClassification(
            interview_required=True,
            direct_run_allowed=False,
            side_effect_risk=verb_risk,
            requires_confirmation=verb_risk is SideEffectRisk.HIGH,
            reason="PR list URL needs interview to narrow target before mutating",
            matched_signals=tuple(signals),
        )

    if has_concrete_anchor:
        return GoalClassification(
            interview_required=True,
            direct_run_allowed=False,
            side_effect_risk=SideEffectRisk.NONE,
            requires_confirmation=False,
            reason="PR/issue URL present but no operational verb",
            matched_signals=tuple(signals),
        )

    if has_operational_verb:
        return GoalClassification(
            interview_required=True,
            direct_run_allowed=False,
            side_effect_risk=verb_risk,
            requires_confirmation=verb_risk is SideEffectRisk.HIGH,
            reason="operational verb present but no concrete PR/issue anchor",
            matched_signals=tuple(signals),
        )

    return GoalClassification(
        interview_required=True,
        direct_run_allowed=False,
        side_effect_risk=SideEffectRisk.NONE,
        requires_confirmation=False,
        reason="no operational signals detected",
        matched_signals=tuple(signals),
    )


__all__ = ["GoalClassification", "SideEffectRisk", "classify_goal"]
