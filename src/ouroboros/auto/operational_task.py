"""Classify ``ooo auto`` goals as operational vs ideation.

Operational goals — concrete asks like "review PR <url>", "merge PR <url>",
"fix the failing tests in <repo>" — already carry enough execution context
that the Socratic interview is at best a tax and at worst the wrong tool
(a single first-question timeout strands the entire session, see #692).

This module is **pure**: a deterministic function over a goal string
returning a structured classification. The pipeline-side rewiring lives in
the follow-up PR (#689 part 2) so the classifier can be reviewed and
exercised under test in isolation.

The classification carries the *intent* the pipeline needs to choose a
direct path or an interview-first path, plus a side-effect risk label so
later code can require an explicit confirmation gate before any destructive
action (merge, close).
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final

# A closed set of intent kinds. Keep this tight — adding a kind requires a
# matching update to the pipeline path that consumes the classification.
GENERAL: Final[str] = "general"
PR_URL: Final[str] = "pr_url"
ISSUE_URL: Final[str] = "issue_url"
MERGE_INTENT: Final[str] = "merge_intent"
REVIEW_INTENT: Final[str] = "review_intent"

# Side-effect risk labels in increasing severity.
RISK_NONE: Final[str] = "none"
RISK_LOW: Final[str] = "low"
RISK_DESTRUCTIVE_MERGE: Final[str] = "destructive_merge"
RISK_DESTRUCTIVE_CLOSE: Final[str] = "destructive_close"


# Match GitHub PR / issue URLs. Capture only the canonical form to avoid
# silently classifying e.g. doc links to ``/pulls`` index pages as PRs.
_PR_URL_RE = re.compile(
    r"https?://github\.com/[\w\-.]+/[\w\-.]+/pull/(\d+)\b",
    re.IGNORECASE,
)
# Use an explicit URL-terminator group so the match works when the goal text
# concatenates non-ASCII characters (e.g. Korean particles) directly after the
# URL — Python's ``\b`` treats Hangul as a word character, so it would not
# count as a boundary against ``s|의`` and the match would silently fail.
_PR_INDEX_URL_RE = re.compile(
    r"https?://github\.com/[\w\-.]+/[\w\-.]+/pulls(?:[/?#][^\s)]*)?",
    re.IGNORECASE,
)
_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/[\w\-.]+/[\w\-.]+/issues/(\d+)\b",
    re.IGNORECASE,
)

# Intent keywords (English + Korean). Whole-word match for English; substring
# match acceptable for Korean since the language doesn't word-break the same
# way and the words are unambiguous in this domain.
_MERGE_EN_RE = re.compile(r"\b(merge|merging|merged|squash|rebase\s+merge)\b", re.IGNORECASE)
_MERGE_KO = ("머지", "병합")
_CLOSE_EN_RE = re.compile(r"\b(close|closes|closing)\b", re.IGNORECASE)
_CLOSE_KO = ("닫", "종료")
_REVIEW_EN_RE = re.compile(
    r"\b(review|reviewing|fix(?:es|ed|ing)?|address|resolve|improve)\b",
    re.IGNORECASE,
)
_REVIEW_KO = ("리뷰", "검토", "개선", "수정")


@dataclass(frozen=True, slots=True)
class OperationalTaskClassification:
    """Structured intent extracted from an auto goal string.

    Attributes mirror the contract specified in #689:

    * ``interview_required`` — when True, the pipeline must run the Socratic
      interview before doing anything else (the goal is too vague to act on).
    * ``direct_run_allowed`` — when True, the operational path is a candidate.
      The pipeline may still fall back to interview-first based on policy.
    * ``side_effect_risk`` — coarse risk label; ``destructive_*`` REQUIRES the
      pipeline to gate execution on a confirmation matrix.
    * ``requires_confirmation`` — explicit shortcut for the destructive cases.
    * ``targets`` — canonical URLs extracted from the goal (deduplicated, in
      first-appearance order).
    * ``reasons`` — short labels explaining the classification, useful for
      logging/ledger entries.
    """

    kind: str
    interview_required: bool
    direct_run_allowed: bool
    side_effect_risk: str
    requires_confirmation: bool
    targets: tuple[str, ...]
    reasons: tuple[str, ...]


def _has_korean_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    return any(keyword in text for keyword in keywords)


def _extract_targets(goal: str) -> tuple[tuple[str, ...], bool, bool, bool]:
    """Return (urls, has_pr, has_pr_index, has_issue) preserving first-seen order."""
    seen: list[str] = []
    has_pr = False
    has_pr_index = False
    has_issue = False
    for match in _PR_URL_RE.finditer(goal):
        url = match.group(0)
        if url not in seen:
            seen.append(url)
        has_pr = True
    for match in _PR_INDEX_URL_RE.finditer(goal):
        url = match.group(0)
        if url not in seen:
            seen.append(url)
        has_pr_index = True
    for match in _ISSUE_URL_RE.finditer(goal):
        url = match.group(0)
        if url not in seen:
            seen.append(url)
        has_issue = True
    return tuple(seen), has_pr, has_pr_index, has_issue


def classify_operational_task(goal: str) -> OperationalTaskClassification:
    """Classify ``goal`` for the auto pipeline's path-selection step.

    Returns a default ``GENERAL`` classification with ``interview_required``
    when the goal is empty or carries no operational signal — so callers can
    safely use this as the only entry point.
    """
    if not goal or not goal.strip():
        return OperationalTaskClassification(
            kind=GENERAL,
            interview_required=True,
            direct_run_allowed=False,
            side_effect_risk=RISK_NONE,
            requires_confirmation=False,
            targets=(),
            reasons=("empty goal",),
        )

    targets, has_pr, has_pr_index, has_issue = _extract_targets(goal)

    has_merge = bool(_MERGE_EN_RE.search(goal)) or _has_korean_keyword(goal, _MERGE_KO)
    has_close = bool(_CLOSE_EN_RE.search(goal)) or _has_korean_keyword(goal, _CLOSE_KO)
    has_review = bool(_REVIEW_EN_RE.search(goal)) or _has_korean_keyword(goal, _REVIEW_KO)

    reasons: list[str] = []
    if has_pr:
        reasons.append("pr_url")
    if has_pr_index:
        reasons.append("pr_index_url")
    if has_issue:
        reasons.append("issue_url")
    if has_merge:
        reasons.append("merge_keyword")
    if has_close:
        reasons.append("close_keyword")
    if has_review:
        reasons.append("review_keyword")

    operational = (has_pr or has_pr_index or has_issue) or (has_merge or has_close or has_review)
    if not operational:
        return OperationalTaskClassification(
            kind=GENERAL,
            interview_required=True,
            direct_run_allowed=False,
            side_effect_risk=RISK_NONE,
            requires_confirmation=False,
            targets=targets,
            reasons=tuple(reasons) or ("no operational signal",),
        )

    if has_merge:
        kind = MERGE_INTENT
        risk = RISK_DESTRUCTIVE_MERGE
        requires_confirmation = True
    elif has_close:
        kind = MERGE_INTENT  # close shares the destructive shape; pipeline distinguishes
        risk = RISK_DESTRUCTIVE_CLOSE
        requires_confirmation = True
    elif has_pr:
        kind = PR_URL
        risk = RISK_LOW if has_review else RISK_NONE
        requires_confirmation = False
    elif has_issue:
        kind = ISSUE_URL
        risk = RISK_LOW if has_review else RISK_NONE
        requires_confirmation = False
    elif has_pr_index:
        kind = PR_URL
        risk = RISK_LOW if has_review else RISK_NONE
        requires_confirmation = False
    else:
        kind = REVIEW_INTENT
        risk = RISK_LOW
        requires_confirmation = False

    # Operational signal alone reduces but does not eliminate the case for an
    # interview: the pipeline owns that policy decision. We expose
    # ``direct_run_allowed`` so it can choose; ``interview_required`` is only
    # set when the operational signal is too thin to act on (no concrete
    # target URL AND no destructive intent that demands confirmation).
    interview_required = (
        not (has_pr or has_pr_index or has_issue)
        and not has_merge
        and not has_close
    )

    return OperationalTaskClassification(
        kind=kind,
        interview_required=interview_required,
        direct_run_allowed=not interview_required,
        side_effect_risk=risk,
        requires_confirmation=requires_confirmation,
        targets=targets,
        reasons=tuple(reasons),
    )


__all__ = [
    "GENERAL",
    "ISSUE_URL",
    "MERGE_INTENT",
    "OperationalTaskClassification",
    "PR_URL",
    "REVIEW_INTENT",
    "RISK_DESTRUCTIVE_CLOSE",
    "RISK_DESTRUCTIVE_MERGE",
    "RISK_LOW",
    "RISK_NONE",
    "classify_operational_task",
]
