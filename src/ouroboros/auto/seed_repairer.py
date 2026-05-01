"""Bounded repair loop for auto-generated Seeds."""

from __future__ import annotations

from dataclasses import dataclass, field

from ouroboros.auto.grading import SeedGrade
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.seed_reviewer import ReviewFinding, SeedReview, SeedReviewer
from ouroboros.core.seed import Seed


@dataclass(frozen=True, slots=True)
class RepairResult:
    """Result from one repair attempt."""

    changed: bool
    seed: Seed
    applied_repairs: tuple[str, ...] = ()
    unresolved_findings: tuple[ReviewFinding, ...] = ()
    blocker: str | None = None


@dataclass(slots=True)
class SeedRepairer:
    """Deterministically repair common A-grade failures."""

    reviewer: SeedReviewer = field(default_factory=SeedReviewer)
    max_repair_rounds: int = 5

    def repair_once(
        self,
        seed: Seed,
        review: SeedReview,
        *,
        ledger: SeedDraftLedger | None = None,
    ) -> RepairResult:
        """Apply one deterministic repair pass."""
        if review.grade_result.blockers:
            return RepairResult(
                changed=False,
                seed=seed,
                unresolved_findings=review.findings,
                blocker="hard blocker present in Seed review",
            )

        constraints = list(seed.constraints)
        acceptance = list(seed.acceptance_criteria)
        applied: list[str] = []
        unresolved: list[ReviewFinding] = []

        for finding in review.findings:
            if finding.code in {"vague_acceptance_criteria", "untestable_acceptance_criteria"}:
                index = _target_index(finding.target)
                replacement = "A command/API check returns stable observable output or artifacts proving this requirement."
                if index is not None and index < len(acceptance):
                    acceptance[index] = replacement
                else:
                    acceptance.append(replacement)
                applied.append(finding.fingerprint)
            elif finding.code == "missing_acceptance_criteria":
                acceptance.append("A command/API check returns stable observable output or artifacts proving the task goal.")
                applied.append(finding.fingerprint)
            elif finding.code == "missing_constraints":
                constraints.append("Use existing project patterns and avoid new dependencies unless required by acceptance criteria.")
                applied.append(finding.fingerprint)
            elif finding.code == "missing_non_goals" and ledger is not None:
                ledger.add_entry(
                    "non_goals",
                    LedgerEntry(
                        key="non_goals.auto_mvp",
                        value="No cloud sync, authentication, paid services, or production deployment in auto MVP scope.",
                        source=LedgerSource.NON_GOAL,
                        confidence=0.86,
                        status=LedgerStatus.DEFAULTED,
                        rationale="Repair loop bounded scope for A-grade auto Seed.",
                    ),
                )
                applied.append(finding.fingerprint)
            else:
                unresolved.append(finding)

        changed = bool(applied)
        updated_seed = seed.model_copy(
            update={
                "constraints": tuple(dict.fromkeys(constraints)),
                "acceptance_criteria": tuple(dict.fromkeys(acceptance)),
            }
        )
        return RepairResult(changed=changed, seed=updated_seed, applied_repairs=tuple(applied), unresolved_findings=tuple(unresolved))

    def converge(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> tuple[Seed, SeedReview, list[RepairResult]]:
        """Review/repair until A-grade or bounded stop."""
        history: list[RepairResult] = []
        previous_high_fingerprints: set[str] = set()
        current = seed
        review = self.reviewer.review(current, ledger=ledger)
        for _ in range(self.max_repair_rounds):
            if review.grade_result.grade == SeedGrade.A and review.may_run:
                return current, review, history
            high = {finding.fingerprint for finding in review.findings if finding.severity == "high"}
            repair = self.repair_once(current, review, ledger=ledger)
            history.append(repair)
            if repair.blocker or not repair.changed:
                return current, review, history
            if high and high == previous_high_fingerprints:
                return current, review, history
            previous_high_fingerprints = high
            current = repair.seed
            review = self.reviewer.review(current, ledger=ledger)
        return current, review, history


def _target_index(target: str) -> int | None:
    if "[" not in target or "]" not in target:
        return None
    try:
        return int(target.split("[", 1)[1].split("]", 1)[0])
    except ValueError:
        return None
