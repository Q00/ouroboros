"""Explicit required-exit-condition evidence for auto Seed grading and repair."""

from __future__ import annotations

from dataclasses import dataclass
import re

from ouroboros.auto.ledger import LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.core.seed import ExitCondition, Seed

_EXPLICIT_SOURCES = frozenset({LedgerSource.USER_GOAL, LedgerSource.USER_PREFERENCE})
_INACTIVE_STATUSES = frozenset({LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED})
_EXIT_CONDITION_HEADER_RE = re.compile(
    r"(?P<header>[^\n:.]{0,120}\bexit[_\s-]*conditions?\b[^:\n]{0,120})\s*:\s*",
    re.IGNORECASE,
)
_EXIT_CONDITION_MENTION_RE = re.compile(r"\bexit[_\s-]*conditions?\b", re.IGNORECASE)
_IDENTIFIER_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
_QUOTED_NAME_RE = re.compile(r"`([^`]+)`|['\"]([^'\"]+)['\"]")
_COUNT_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


@dataclass(frozen=True, slots=True)
class RequiredExitConditions:
    """Explicitly named exit conditions recovered from authoritative text."""

    names: tuple[str, ...]
    exact: bool = False


def required_exit_conditions(
    seed: Seed,
    ledger: SeedDraftLedger | None = None,
) -> RequiredExitConditions | None:
    """Return explicit required names without inferring them from vague prose.

    A supplied ledger is the source of truth, so only active user-goal or
    user-preference entries are considered. Direct callers without a ledger
    may still use an explicitly enumerated requirement in ``seed.goal``.
    """
    evidence: list[str] = []
    if ledger is not None:
        for section in ledger.sections.values():
            for entry in section.entries:
                if entry.source not in _EXPLICIT_SOURCES:
                    continue
                if entry.status in _INACTIVE_STATUSES or not entry.value.strip():
                    continue
                evidence.append(entry.value)
    else:
        evidence.append(seed.goal)

    requirements = [
        requirement
        for text in dict.fromkeys(evidence)
        if (requirement := _required_exit_conditions_from_text(text)) is not None
    ]
    if not requirements:
        return None

    exact_requirements = [requirement for requirement in requirements if requirement.exact]
    if exact_requirements:
        # Hydrated ledger sections can repeat the same goal requirement.
        # Conflicting exact lists are not safe to merge or guess through.
        longest = max(exact_requirements, key=lambda requirement: len(requirement.names))
        longest_set = set(longest.names)
        if any(set(requirement.names) != longest_set for requirement in exact_requirements):
            return None
        return longest

    names = tuple(dict.fromkeys(name for requirement in requirements for name in requirement.names))
    return RequiredExitConditions(names=names)


def qa_requests_exit_condition_repair(*feedback: str) -> bool:
    """Return whether QA explicitly targets the structural Seed field."""
    return any(_EXIT_CONDITION_MENTION_RE.search(item) for item in feedback if item)


def repair_exit_conditions(
    seed: Seed,
    requirement: RequiredExitConditions,
) -> Seed:
    """Reconcile ``seed.exit_conditions`` with explicitly required names."""
    existing_by_name = {condition.name: condition for condition in seed.exit_conditions}
    repaired: list[ExitCondition] = []
    for name in requirement.names:
        condition = existing_by_name.get(name)
        if condition is None:
            condition = ExitCondition(
                name=name,
                description=f"Explicitly required exit condition `{name}` is satisfied.",
                evaluation_criteria=f"Verification evidence confirms `{name}` is satisfied.",
            )
        repaired.append(condition)

    if not requirement.exact:
        required_names = set(requirement.names)
        repaired.extend(
            condition for condition in seed.exit_conditions if condition.name not in required_names
        )
    repaired_tuple = tuple(repaired)
    if repaired_tuple == seed.exit_conditions:
        return seed
    return seed.model_copy(update={"exit_conditions": repaired_tuple})


def _required_exit_conditions_from_text(text: str) -> RequiredExitConditions | None:
    """Parse only colon-delimited, explicitly enumerated condition names."""
    for match in _EXIT_CONDITION_HEADER_RE.finditer(text):
        header = match.group("header").strip()
        body = _requirement_body(text[match.end() :])
        names = _explicit_names(body)
        if not names:
            continue
        expected_count = _explicit_count(header)
        if expected_count is not None and len(names) != expected_count:
            continue
        exact = bool(re.search(r"\b(?:exactly|only)\b", f"{header} {body}", re.IGNORECASE))
        return RequiredExitConditions(names=names, exact=exact)
    return None


def _requirement_body(remainder: str) -> str:
    lines = remainder[:1600].splitlines()
    if not lines:
        return ""
    body = lines[0].split(".", 1)[0].strip()
    bullets: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if not stripped:
            break
        if not re.match(r"^(?:[-*]|\d+[.)])\s+", stripped):
            break
        bullets.append(stripped)
    return "\n".join(part for part in (body, *bullets) if part)


def _explicit_names(body: str) -> tuple[str, ...]:
    quoted = tuple(
        name.strip()
        for match in _QUOTED_NAME_RE.finditer(body)
        if (name := next(group for group in match.groups() if group is not None).strip())
        and _IDENTIFIER_RE.fullmatch(name)
    )
    if quoted:
        return tuple(dict.fromkeys(quoted))

    normalized = re.sub(r"^(?:\[|\()|(?:\]|\))$", "", body.strip())
    chunks = re.split(r"\s*(?:,|\||;|\n|\band\b)\s*", normalized, flags=re.IGNORECASE)
    names: list[str] = []
    for chunk in chunks:
        candidate = re.sub(r"^(?:[-*]|\d+[.)])\s*", "", chunk.strip())
        candidate = re.sub(r"\s+(?:only|and\s+no\s+others?)$", "", candidate, flags=re.I)
        candidate = candidate.strip(" `\"'[]()")
        if _IDENTIFIER_RE.fullmatch(candidate):
            names.append(candidate)
    return tuple(dict.fromkeys(names))


def _explicit_count(header: str) -> int | None:
    match = re.search(
        r"\b(?:exactly|only)\s+(?P<count>\d+|" + "|".join(_COUNT_WORDS) + r")\b",
        header,
        re.IGNORECASE,
    )
    if match is None:
        return None
    count = match.group("count").casefold()
    return int(count) if count.isdigit() else _COUNT_WORDS[count]


__all__ = [
    "RequiredExitConditions",
    "qa_requests_exit_condition_repair",
    "repair_exit_conditions",
    "required_exit_conditions",
]
