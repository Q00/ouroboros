"""Deterministic A-grade gate for auto-generated Seeds and ledgers."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import re
from typing import Any

from ouroboros.auto.gap_detector import GapDetector
from ouroboros.auto.ledger import LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.core.seed import Seed


class SeedGrade(StrEnum):
    """Supported quality grades for auto-mode execution gates."""

    A = "A"
    B = "B"
    C = "C"


VAGUE_TERMS = (
    "easy",
    "intuitive",
    "robust",
    "scalable",
    "better",
    "improve",
    "optimized",
    "user-friendly",
    "seamless",
)
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
    "artifact",
    "non-zero",
    "stdout",
    "stderr",
    "exits",
    "exit code",
    "http",
    "200",
)


@dataclass(frozen=True, slots=True)
class GradeFinding:
    """A single deterministic grading finding."""

    code: str
    severity: str
    message: str
    target: str = ""
    repair_instruction: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "target": self.target,
            "repair_instruction": self.repair_instruction,
        }


@dataclass(frozen=True, slots=True)
class GradeResult:
    """Structured result returned by GradeGate."""

    grade: SeedGrade
    scores: dict[str, float]
    findings: list[GradeFinding] = field(default_factory=list)
    blockers: list[GradeFinding] = field(default_factory=list)
    can_repair: bool = True
    may_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "grade": self.grade.value,
            "scores": self.scores,
            "findings": [finding.to_dict() for finding in self.findings],
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "can_repair": self.can_repair,
            "may_run": self.may_run,
        }


class GradeGate:
    """Deterministic gate that prevents B/C Seeds from running."""

    def __init__(self, gap_detector: GapDetector | None = None) -> None:
        self.gap_detector = gap_detector or GapDetector()

    def grade_ledger(self, ledger: SeedDraftLedger) -> GradeResult:
        """Grade a Seed Draft Ledger before Seed generation."""
        findings: list[GradeFinding] = []
        blockers: list[GradeFinding] = []
        gaps = self.gap_detector.detect(ledger)
        for gap in gaps:
            finding = GradeFinding(
                code=f"{gap.section}_{gap.state.value}",
                severity="high" if not gap.repairable else "medium",
                message=gap.message,
                target=gap.section,
                repair_instruction="Resolve via confirmed fact, conservative default, assumption, or non-goal.",
            )
            if gap.repairable:
                findings.append(finding)
            else:
                blockers.append(finding)

        summary = ledger.summary()
        missing_count = len(summary["open_gaps"])
        coverage = max(0.0, 1.0 - (missing_count / 10))
        assumption_count = len(summary["assumptions"])
        risk = min(1.0, 0.05 * assumption_count + 0.15 * len(blockers))
        scores = {
            "coverage": round(coverage, 2),
            "ambiguity": round(1.0 - coverage, 2),
            "testability": 0.85 if "acceptance_criteria" not in summary["open_gaps"] else 0.4,
            "execution_feasibility": 0.85 if "runtime_context" not in summary["open_gaps"] else 0.5,
            "risk": round(risk, 2),
        }
        return self._result(scores=scores, findings=findings, blockers=blockers)

    def grade_seed(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> GradeResult:
        """Grade a generated Seed deterministically."""
        findings: list[GradeFinding] = []
        blockers: list[GradeFinding] = []

        if not seed.goal.strip():
            blockers.append(GradeFinding("missing_goal", "high", "Seed goal is empty", "goal"))
        elif ledger is not None and not _seed_goal_matches_ledger(seed.goal, ledger):
            blockers.append(
                GradeFinding(
                    "seed_goal_mismatch",
                    "high",
                    "Seed goal does not match the converged interview goal",
                    "goal",
                    "Regenerate or repair the Seed so its goal matches the auto interview ledger goal.",
                )
            )
        if seed.metadata.ambiguity_score > 0.20:
            blockers.append(
                GradeFinding(
                    "high_ambiguity_score",
                    "high",
                    f"Seed ambiguity score is too high for auto execution: {seed.metadata.ambiguity_score:.2f}",
                    "metadata.ambiguity_score",
                    "Continue interview or repair until ambiguity_score <= 0.20.",
                )
            )
        if not seed.constraints:
            findings.append(
                GradeFinding(
                    "missing_constraints",
                    "medium",
                    "Seed has no constraints",
                    "constraints",
                    "Add explicit execution and scope constraints.",
                )
            )
        if not seed.acceptance_criteria:
            findings.append(
                GradeFinding(
                    "missing_acceptance_criteria",
                    "high",
                    "Seed has no acceptance criteria",
                    "acceptance_criteria",
                    "Add observable acceptance criteria.",
                )
            )
        for index, criterion in enumerate(seed.acceptance_criteria):
            if _is_vague(criterion):
                findings.append(
                    GradeFinding(
                        "vague_acceptance_criteria",
                        "high",
                        f"Acceptance criterion is vague: {criterion}",
                        f"acceptance_criteria[{index}]",
                        "Replace with observable behavior or artifact.",
                    )
                )
            if not _is_observable(criterion):
                findings.append(
                    GradeFinding(
                        "untestable_acceptance_criteria",
                        "high",
                        f"Acceptance criterion is not clearly observable: {criterion}",
                        f"acceptance_criteria[{index}]",
                        "Mention command output, file/artifact, API response, or test result.",
                    )
                )

        non_goals = []
        if ledger is not None:
            open_gaps = ledger.open_gaps()
            for gap in open_gaps:
                blockers.append(
                    GradeFinding(
                        "ledger_open_gap",
                        "high",
                        f"Ledger required section is unresolved: {gap}",
                        gap,
                        "Resolve the ledger section before allowing auto execution.",
                    )
                )
            non_goal_section = ledger.sections.get("non_goals")
            non_goals = (
                [entry.value for entry in non_goal_section.entries] if non_goal_section else []
            )
        if ledger is not None and not non_goals:
            findings.append(
                GradeFinding(
                    "missing_non_goals",
                    "medium",
                    "Auto-generated Seed has no explicit non-goals",
                    "non_goals",
                    "Add MVP non-goals to bound scope.",
                )
            )

        high_risk_assumptions = 0
        if ledger is not None:
            high_risk_assumptions = _high_risk_assumption_count(ledger)
            if high_risk_assumptions:
                blockers.append(
                    GradeFinding(
                        "high_risk_assumptions",
                        "high",
                        "Ledger contains high-risk assumptions",
                        "assumptions",
                        "Replace high-risk assumptions with blockers or user confirmation.",
                    )
                )

        untestable_count = sum(1 for finding in findings if "acceptance_criteria" in finding.code)
        scores = {
            "coverage": _score_threshold(len(findings), len(blockers), base=0.95),
            "ambiguity": min(1.0, 0.05 + 0.08 * len(findings) + 0.2 * len(blockers)),
            "testability": max(0.0, 0.95 - 0.25 * untestable_count),
            "execution_feasibility": 0.85 if not blockers else 0.4,
            "risk": min(
                1.0, 0.05 * len(findings) + 0.3 * len(blockers) + 0.2 * high_risk_assumptions
            ),
        }
        return self._result(scores=scores, findings=findings, blockers=blockers)

    def _result(
        self,
        *,
        scores: dict[str, float],
        findings: list[GradeFinding],
        blockers: list[GradeFinding],
    ) -> GradeResult:
        grade = SeedGrade.A
        if blockers:
            grade = SeedGrade.C
        elif (
            scores["coverage"] < 0.90
            or scores["ambiguity"] > 0.20
            or scores["testability"] < 0.85
            or scores["execution_feasibility"] < 0.80
            or scores["risk"] > 0.25
            or findings
        ):
            grade = SeedGrade.B
        return GradeResult(
            grade=grade,
            scores={name: round(value, 2) for name, value in scores.items()},
            findings=findings,
            blockers=blockers,
            can_repair=not blockers,
            may_run=grade == SeedGrade.A and not blockers,
        )


def _seed_goal_matches_ledger(seed_goal: str, ledger: SeedDraftLedger) -> bool:
    goal_section = ledger.sections.get("goal")
    if goal_section is None:
        return True
    inactive = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    goals = [entry.value for entry in goal_section.entries if entry.status not in inactive]
    if not goals:
        return True
    seed_tokens = _goal_tokens(seed_goal)
    if not seed_tokens:
        return False
    for goal in goals:
        ledger_tokens = _goal_tokens(goal)
        if not ledger_tokens:
            continue
        shared = seed_tokens & ledger_tokens
        if ledger_tokens <= seed_tokens:
            return True
        if len(shared) / max(len(ledger_tokens), 1) >= 0.6:
            return True
    return False


def _goal_tokens(value: str) -> set[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "app",
        "application",
        "build",
        "create",
        "for",
        "make",
        "the",
        "to",
    }
    tokens = {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 2 and token not in stopwords
    }
    if "cli" in tokens:
        tokens.update({"command", "line", "interface"})
    if {"command", "line", "interface"} <= tokens:
        tokens.add("cli")
    return tokens


def _is_vague(value: str) -> bool:
    lowered = value.lower()
    return any(re.search(rf"\b{re.escape(term)}\b", lowered) for term in VAGUE_TERMS)


def _is_observable(value: str) -> bool:
    lowered = value.lower()
    if not any(hint in lowered for hint in _OBSERVABLE_HINTS):
        return False
    observable_patterns = (
        r"`[^`]+`\s+(prints|returns|creates|writes|exits|displays)",
        r"\b(prints|returns|creates|writes|exits|displays|contains)\b.+\b(stdout|stderr|file|artifact|status|response|output|non-zero|exit code)\b",
        r"\b(stdout|stderr|file|artifact|status|response|output|non-zero|exit code)\b.+\b(contains|equals|includes|is|exists|created|written)\b",
        r"\b(test|check)\b.+\b(passes|fails|asserts|verifies)\b",
        r"\b(api|endpoint|request)\b.+\b(returns|responds|status)\b",
        r"\b(cli|command|process)\b.+\b(exits|returns)\b\s+(with\s+)?(exit\s+code\s+)?0\b",
        r"\b(exit\s+code|status)\s+0\b",
        r"\b(get|post|put|patch|delete)\b.+\b(returns|responds|status)\b\s+(with\s+)?(http\s+)?2\d\d\b",
        r"\b(http\s+)?status\s+2\d\d\b",
    )
    return any(re.search(pattern, lowered) for pattern in observable_patterns)


def _high_risk_assumption_count(ledger: SeedDraftLedger) -> int:
    risky_terms = ("credential", "api key", "production", "payment", "legal", "medical")
    inactive_statuses = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    return sum(
        1
        for section in ledger.sections.values()
        for entry in section.entries
        if entry.source == LedgerSource.ASSUMPTION
        and entry.status not in inactive_statuses
        and any(term in entry.value.lower() for term in risky_terms)
    )


def _score_threshold(finding_count: int, blocker_count: int, *, base: float) -> float:
    return max(0.0, min(1.0, base - 0.08 * finding_count - 0.25 * blocker_count))
