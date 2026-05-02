"""Bounded repair loop for auto-generated Seeds."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import re
from uuid import uuid4

from ouroboros.auto.grading import VAGUE_TERMS, SeedGrade
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
        repaired_acceptance_indices: set[int] = set()

        for finding in review.findings:
            if finding.code in {"vague_acceptance_criteria", "untestable_acceptance_criteria"}:
                index = _target_index(finding.target)
                if index is not None and index < len(acceptance):
                    if index not in repaired_acceptance_indices:
                        acceptance[index] = _observable_preserving_replacement(
                            acceptance[index], index=index
                        )
                        repaired_acceptance_indices.add(index)
                else:
                    acceptance.append(
                        "A command/API check returns stable observable output or artifacts proving the task goal."
                    )
                applied.append(finding.fingerprint)
            elif finding.code == "missing_acceptance_criteria":
                acceptance.append(
                    "A command/API check returns stable observable output or artifacts proving the task goal."
                )
                applied.append(finding.fingerprint)
            elif finding.code == "missing_constraints":
                constraints.append(
                    "Use existing project patterns and avoid new dependencies unless required by acceptance criteria."
                )
                applied.append(finding.fingerprint)
            elif finding.code == "missing_non_goals" and ledger is not None:
                ledger.add_entry(
                    "non_goals",
                    LedgerEntry(
                        key="non_goals.auto_mvp",
                        value=_safe_auto_mvp_non_goal(ledger),
                        source=LedgerSource.NON_GOAL,
                        confidence=0.86,
                        status=LedgerStatus.DEFAULTED,
                        rationale="Repair loop bounded scope without contradicting the requested goal.",
                    ),
                )
                applied.append(finding.fingerprint)
            else:
                unresolved.append(finding)

        changed = bool(applied)
        updated_seed = seed
        if changed:
            updated_seed = seed.model_copy(
                update={
                    "constraints": tuple(dict.fromkeys(constraints)),
                    "acceptance_criteria": tuple(dict.fromkeys(acceptance)),
                    "metadata": seed.metadata.model_copy(
                        update={
                            "seed_id": f"seed_{uuid4().hex[:12]}",
                            "created_at": datetime.now(UTC),
                            "parent_seed_id": seed.metadata.seed_id,
                        }
                    ),
                }
            )
        return RepairResult(
            changed=changed,
            seed=updated_seed,
            applied_repairs=tuple(applied),
            unresolved_findings=tuple(unresolved),
        )

    def converge(
        self, seed: Seed, *, ledger: SeedDraftLedger | None = None
    ) -> tuple[Seed, SeedReview, list[RepairResult]]:
        """Review/repair until A-grade or bounded stop."""
        history: list[RepairResult] = []
        previous_high_fingerprints: set[str] = set()
        current = seed
        review = self.reviewer.review(current, ledger=ledger)
        for _ in range(self.max_repair_rounds):
            if review.grade_result.grade == SeedGrade.A and review.may_run:
                return current, review, history
            high = {
                finding.fingerprint for finding in review.findings if finding.severity == "high"
            }
            repair = self.repair_once(current, review, ledger=ledger)
            history.append(repair)
            if repair.blocker or not repair.changed:
                return current, review, history
            current = repair.seed
            if high and high == previous_high_fingerprints:
                review = self.reviewer.review(current, ledger=ledger)
                return current, review, history
            previous_high_fingerprints = high
            review = self.reviewer.review(current, ledger=ledger)
        return current, review, history


def _observable_preserving_replacement(criterion: str, *, index: int) -> str:
    """Make a criterion observable without erasing the original feature subject."""
    normalized = criterion.strip().rstrip(".")
    for term in VAGUE_TERMS:
        normalized = re.sub(rf"\b{re.escape(term)}\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\b(should be|is|are|be)\b", "", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\s+(and|or)\s*$", "", normalized.strip(), flags=re.IGNORECASE)
    normalized = re.sub(r"\s{2,}", " ", normalized).strip().strip("-:;,. ").strip()
    subject = normalized or f"acceptance criterion {index + 1}"
    return (
        "A command/API check returns stable observable output or artifacts "
        f"proving the original requirement for {subject}."
    )


def _target_index(target: str) -> int | None:
    if "[" not in target or "]" not in target:
        return None
    try:
        return int(target.split("[", 1)[1].split("]", 1)[0])
    except ValueError:
        return None


def _safe_auto_mvp_non_goal(ledger: SeedDraftLedger) -> str:
    goal = _latest_resolved_goal(ledger).lower()
    excluded = ["cloud sync", "paid services"]
    if not re.search(r"\b(auth|authentication|login|sign[- ]?in|signup|password)\b", goal):
        excluded.append("authentication")
    if not re.search(r"\b(production|prod|deploy|deployment|release|publish)\b", goal):
        excluded.append("production deployment")
    if not excluded:
        return "No scope outside the explicitly requested goal is included in auto MVP scope."
    return f"For auto MVP scope, {', '.join(excluded)} are non-goals unless explicitly requested."


def _latest_resolved_goal(ledger: SeedDraftLedger) -> str:
    section = ledger.sections.get("goal")
    if section is None:
        return ""
    inactive = {LedgerStatus.WEAK, LedgerStatus.CONFLICTING, LedgerStatus.BLOCKED}
    for entry in reversed(section.entries):
        if entry.status not in inactive and entry.value.strip():
            return entry.value
    return ""
