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
#
# Use a negative-lookahead ``(?!\d)`` after the captured digit run instead of
# ``\b``: Python's ``\b`` treats Hangul (and other non-ASCII word characters)
# as word characters, so ``/pull/1을 ...`` had no boundary between ``1`` and
# ``을`` and the match silently failed. ``(?!\d)`` only forbids another digit,
# which is the actual semantic we want.
_PR_URL_RE = re.compile(
    r"https?://github\.com/[\w\-.]+/[\w\-.]+/pull/(\d+)(?!\d)",
    re.IGNORECASE,
)
# Index URL ends in literal ``pulls`` (no trailing digit), so an optional
# URL-continuation group is enough; non-ASCII suffixes are not URL-safe and
# implicitly terminate the match.
_PR_INDEX_URL_RE = re.compile(
    r"https?://github\.com/[\w\-.]+/[\w\-.]+/pulls(?:[/?#][^\s)]*)?",
    re.IGNORECASE,
)
_ISSUE_URL_RE = re.compile(
    r"https?://github\.com/[\w\-.]+/[\w\-.]+/issues/(\d+)(?!\d)",
    re.IGNORECASE,
)

# Intent keywords (English + Korean). Whole-word match for English; substring
# match acceptable for Korean since the language doesn't word-break the same
# way and the words are unambiguous in this domain.
_MERGE_EN_RE = re.compile(r"\b(merge|merging|merged|squash|rebase\s+merge)\b", re.IGNORECASE)
_MERGE_KO = ("머지", "병합")
# ``close`` is a polysemous English word — "take a close look", "close call",
# "close to", "close eye on" are NOT destructive intents. Exclude common
# adjectival/non-imperative uses with a negative lookahead so the classifier
# does not promote a benign goal to ``destructive_close``. (Bot-flagged in
# #721 review.)
_CLOSE_EN_RE = re.compile(
    r"\b(?:close|closes|closing)\b"
    r"(?!\s+(?:look|call|to|eye|enough|attention|range|reading|"
    r"relationship|cousin|encounter|second|game|race|by|with))",
    re.IGNORECASE,
)
_CLOSE_KO = ("닫", "종료")
_REVIEW_EN_RE = re.compile(
    r"\b(review|reviewing|fix(?:es|ed|ing)?|address|resolve|improve)\b",
    re.IGNORECASE,
)
_REVIEW_KO = ("리뷰", "검토", "개선", "수정")

# ``owner/repo`` style identifier. The classifier accepts this as a target
# alongside full URLs so operational asks like ``fix the failing tests in
# owner/repo`` are recognized without requiring a fully-qualified URL.
# (Bot-flagged in #719 review.)
#
# The regex is intentionally permissive — both halves only require a single
# alphanumeric character so legitimate short repo names (``owner/r``,
# ``o/repo``) match. Common English idioms with the same surface shape
# (``and/or``, ``and/or``, ``yes/no``, ``foo/bar``) are filtered out at the
# value level by ``_REPO_PROSE_FALSE_POSITIVES`` below — that has a much
# better signal-to-noise ratio than chasing every common pattern in regex.
_REPO_PATH_RE = re.compile(
    r"(?<![\w/])([A-Za-z0-9][\w\-.]*/[A-Za-z0-9][\w\-.]*)(?![\w/])",
)

# Phrases that share the ``a/b`` shape but are English idioms or placeholder
# names rather than repo identifiers. Compared case-insensitively. Adding to
# this list is the standard way to handle a new false positive flagged by a
# review without re-tuning the regex. (Bot-flagged in #719 / #721 review.)
_REPO_PROSE_FALSE_POSITIVES: Final[frozenset[str]] = frozenset(
    {
        # Common English connectives.
        "and/or",
        "or/and",
        "either/or",
        "neither/nor",
        "to/from",
        "from/to",
        "yes/no",
        "true/false",
        "on/off",
        "pass/fail",
        "input/output",
        "front/back",
        "this/that",
        "today/tomorrow",
        "stop/start",
        "start/stop",
        "n/a",
        # Placeholder / metasyntactic identifiers — rarely real repo names
        # and almost always appear as examples in prose.
        "foo/bar",
        "foo/baz",
        "bar/baz",
        "lorem/ipsum",
    }
)


def _is_repo_prose_false_positive(value: str) -> bool:
    return value.lower() in _REPO_PROSE_FALSE_POSITIVES


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


def _extract_targets(
    goal: str,
) -> tuple[tuple[str, ...], bool, bool, bool, bool]:
    """Return (targets, has_pr, has_pr_index, has_issue, has_repo) in
    first-seen order.

    Implementation note: matches from every URL class plus the
    ``owner/repo`` identifier pattern are collected together and sorted by
    their offset in the goal string. This guarantees first-appearance order
    across types and avoids double-counting — a ``/pull/1`` URL already
    contains ``owner/repo`` substring, so the URL match shadows the bare
    identifier when their start offsets line up.
    """
    matches: list[tuple[int, str, str]] = []  # (start, kind, value)

    # URL classes first — their matches take precedence over a bare repo
    # identifier embedded inside them.
    for kind, pattern in (
        ("pr", _PR_URL_RE),
        ("pr_index", _PR_INDEX_URL_RE),
        ("issue", _ISSUE_URL_RE),
    ):
        for match in pattern.finditer(goal):
            matches.append((match.start(), kind, match.group(0)))

    # Repo identifiers — only added when not already covered by a URL match
    # at the same span AND not a common-prose false positive. Use a coverage
    # check on the spans we already have.
    covered = [(start, start + len(val)) for start, _kind, val in matches]
    for match in _REPO_PATH_RE.finditer(goal):
        m_start, m_end = match.start(), match.end()
        if any(start <= m_start and m_end <= end for start, end in covered):
            continue
        value = match.group(1)
        if _is_repo_prose_false_positive(value):
            continue
        matches.append((m_start, "repo", value))

    matches.sort(key=lambda triple: triple[0])

    seen: list[str] = []
    has_pr = False
    has_pr_index = False
    has_issue = False
    has_repo = False
    for _start, kind, value in matches:
        if value not in seen:
            seen.append(value)
        if kind == "pr":
            has_pr = True
        elif kind == "pr_index":
            has_pr_index = True
        elif kind == "issue":
            has_issue = True
        else:
            has_repo = True
    return tuple(seen), has_pr, has_pr_index, has_issue, has_repo


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

    targets, has_pr, has_pr_index, has_issue, has_repo = _extract_targets(goal)

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
    if has_repo:
        reasons.append("repo_path")
    if has_merge:
        reasons.append("merge_keyword")
    if has_close:
        reasons.append("close_keyword")
    if has_review:
        reasons.append("review_keyword")

    operational = (has_pr or has_pr_index or has_issue or has_repo) or (
        has_merge or has_close or has_review
    )
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

    # The operational path is only safe to skip the interview when the goal
    # carries a concrete target — either a full URL OR an ``owner/repo``
    # identifier paired with a review/fix intent (so the pipeline knows
    # what to act on). A destructive verb on its own ("merge it once CI is
    # green", "close it") is not actionable; an ``owner/repo`` reference
    # without any verb is also too thin. (Bot-flagged in #719 review.)
    has_url_target = has_pr or has_pr_index or has_issue
    has_actionable_repo = has_repo and (has_review or has_merge or has_close)
    has_target = has_url_target or has_actionable_repo
    interview_required = not has_target

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
