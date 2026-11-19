"""Deterministic Seed measures that outlived the auto Seed grade gate.

The A/B/C grade gate that once ran between Seed generation and the run was
removed (#3064). Two of its measures are still used elsewhere and live here:

* :func:`deterministic_floor` raises a generated Seed's ``ambiguity_score`` to
  what the interview ledger objectively shows (``pipeline`` and
  ``ledger_seed``);
* :func:`_is_observable` is the observable-criterion check behind the coding
  domain profile's verifiable predicates, which the auto answerer uses during
  the interview (``profiles/coding.py``).
"""

from __future__ import annotations

import re

from ouroboros.auto.ledger import REQUIRED_SECTIONS, SeedDraftLedger
from ouroboros.core.seed import AcceptanceCriterionSpec

_OBSERVABLE_HINTS = (
    "command",
    "exit",
    "prints",
    "returns",
    "creates",
    "writes",
    "file",
    "test",
    "api",
    "status",
    "displays",
    "contains",
    "includes",
    "artifact",
    "report",
    "non-zero",
    "stdout",
    "stderr",
    "exits",
    "exit code",
    "http",
    "200",
)
# Korean statements of the evidence the English patterns accept: command output
# that appears, a test run that passes, an API response, a file that is created
# or written, an exit code. Korean has no word boundaries and the English hints
# never occur in it, so each pattern pairs the evidence noun with the verb that
# observes it instead of matching a lone keyword.
_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_KOREAN_OBSERVABLE_PATTERNS = (
    r"`[^`]+`.*(출력|반환|생성|종료)",
    r"(출력|stdout|stderr).*(나오|나타나|출력되|출력한|표시되|표시한|포함)",
    r"테스트.*(통과|성공)",
    r"(api|엔드포인트|http).*(응답|반환|상태\s*코드)",
    r"파일.*(생성|작성|저장|존재|포함)",
    r"종료\s*코드.{0,12}\d|\d.{0,12}종료\s*코드",
)
_FINAL_REPORT_REQUIRED_FIELDS = (
    "auto session id",
    "seed id",
    "files changed",
    "exact test command",
    "test result",
)


def _is_observable(value: str | AcceptanceCriterionSpec) -> bool:
    if isinstance(value, AcceptanceCriterionSpec):
        if value.verify_command or value.expected_artifacts:
            return True
        value = value.description
    lowered = value.lower()
    if _HANGUL_RE.search(lowered) and any(
        re.search(pattern, lowered) for pattern in _KOREAN_OBSERVABLE_PATTERNS
    ):
        return True
    if not any(hint in lowered for hint in _OBSERVABLE_HINTS):
        return False
    if _is_concrete_final_report_observation(lowered):
        return True
    observable_patterns = (
        r"`[^`]+`\s+(prints|returns|creates|writes|exits|displays)",
        r"\b(prints|returns|creates|writes|exits|displays|contains)\b.+\b(stdout|stderr|file|artifact|status|response|output|non-zero|exit code)\b",
        r"\b(stdout|stderr|file|artifact|status|response|output|non-zero|exit code)\b.+\b(contains|equals|includes|is|exists|created|written)\b",
        r"\b(test|check)\b.+\b(passes|fails|asserts|verifies)\b",
        r"\btargeted command\b.+\bpytest\b.+\b[^\s]+\.py(?:::[^\s]+)?(?=\s).+\bpasses\b",
        r"\b[\w.]+\([^)]*\)\s+returns\s+[`\"'][^`\"']+[`\"']",
        r"\b(api|endpoint|request)\b.+\b(returns|responds|status)\b",
        r"\b(cli|command|process)\b.+\b(exits|returns)\b\s+(with\s+)?(exit\s+code\s+)?0\b",
        r"\b(exit\s+code|status)\s+0\b",
        r"\b(get|post|put|patch|delete)\b.+\b(returns|responds|status)\b\s+(with\s+)?(http\s+)?2\d\d\b",
        r"\b(http\s+)?status\s+2\d\d\b",
    )
    return any(re.search(pattern, lowered) for pattern in observable_patterns)


def _is_concrete_final_report_observation(value: str) -> bool:
    if "final report" not in value or not re.search(r"\bincludes?\b", value):
        return False
    if not all(field in value for field in _FINAL_REPORT_REQUIRED_FIELDS):
        return False
    return not _contradicts_required_final_report_fields(value)


def _contradicts_required_final_report_fields(value: str) -> bool:
    optional_terms = r"optional|not required|may be omitted|can be omitted"
    missing_terms = (
        r"omit|omits|without|missing|excludes|does not include|doesn['’]t include|not include"
    )
    for required_field in _FINAL_REPORT_REQUIRED_FIELDS:
        field_pattern = re.escape(required_field)
        if re.search(rf"\b{field_pattern}\b.{{0,60}}\b({optional_terms})\b", value):
            return True
        if re.search(rf"\b({missing_terms})\b.{{0,60}}\b{field_pattern}\b", value):
            return True
    return False


def deterministic_floor(ledger: SeedDraftLedger) -> float:
    """Return a code-computable lower bound for ``ambiguity_score``.

    The auto pipeline calls this with the converged interview ledger and uses
    ``max(llm_reported_score, deterministic_floor(ledger))`` so the LLM cannot
    under-report ambiguity below what code can objectively measure: how many
    required sections remain open, how many entries are flagged as conflicting,
    and what fraction of resolved sections rest on assumption/inference only.

    Formula:

    - ``0.05`` per open required section (gap pressure)
    - ``0.10`` per active CONFLICTING entry (contradiction pressure)
    - ``0.05 * (assumption_only_sections / total_required)`` (evidence dilution)

    Result is clamped to ``[0.0, 1.0]``.
    """
    summary = ledger.summary()
    open_gap_count = len(summary.get("open_gaps", ()))
    conflicting_count = ledger.count_active_conflicting_entries()
    assumption_only_count = len(summary.get("assumption_only_sections", ()))
    total_required = len(REQUIRED_SECTIONS)
    assumption_ratio = assumption_only_count / total_required if total_required else 0.0
    floor = 0.05 * open_gap_count + 0.10 * conflicting_count + 0.05 * assumption_ratio
    return min(1.0, max(0.0, floor))
