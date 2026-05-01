"""Gap detection for auto-mode Seed Draft Ledgers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ouroboros.auto.ledger import REQUIRED_SECTIONS, LedgerStatus, SeedDraftLedger


class GapType(StrEnum):
    """Known Seed gap types."""

    GOAL = "goal_gap"
    ACTOR = "actor_gap"
    INPUT = "input_gap"
    OUTPUT = "output_gap"
    CONSTRAINT = "constraint_gap"
    NON_GOAL = "non_goal_gap"
    ACCEPTANCE_CRITERIA = "acceptance_criteria_gap"
    VERIFICATION = "verification_gap"
    FAILURE_MODE = "failure_mode_gap"
    RUNTIME_CONTEXT = "runtime_context_gap"
    RISK = "risk_gap"


SECTION_TO_GAP = {
    "goal": GapType.GOAL,
    "actors": GapType.ACTOR,
    "inputs": GapType.INPUT,
    "outputs": GapType.OUTPUT,
    "constraints": GapType.CONSTRAINT,
    "non_goals": GapType.NON_GOAL,
    "acceptance_criteria": GapType.ACCEPTANCE_CRITERIA,
    "verification_plan": GapType.VERIFICATION,
    "failure_modes": GapType.FAILURE_MODE,
    "runtime_context": GapType.RUNTIME_CONTEXT,
    "risks": GapType.RISK,
}


@dataclass(frozen=True, slots=True)
class Gap:
    """A structured gap detected in a ledger."""

    section: str
    gap_type: GapType
    state: LedgerStatus
    message: str
    repairable: bool = True


class GapDetector:
    """Detect missing, conflicting, and blocked auto-mode Seed sections."""

    def detect(self, ledger: SeedDraftLedger) -> list[Gap]:
        """Return structured gaps for ``ledger``."""
        statuses = ledger.section_statuses()
        gaps: list[Gap] = []
        for section in REQUIRED_SECTIONS:
            status = statuses[section]
            if status in {
                LedgerStatus.MISSING,
                LedgerStatus.CONFLICTING,
                LedgerStatus.BLOCKED,
                LedgerStatus.WEAK,
            }:
                gaps.append(
                    Gap(
                        section=section,
                        gap_type=SECTION_TO_GAP[section],
                        state=status,
                        message=f"{section} is {status.value}",
                        repairable=status != LedgerStatus.BLOCKED,
                    )
                )
        return gaps
