"""Adversarial review primitives for auto-generated Seeds."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib

from ouroboros.auto.grading import GradeGate, GradeResult
from ouroboros.auto.ledger import SeedDraftLedger
from ouroboros.core.seed import Seed


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    """Stable review finding used by repair convergence guards."""

    code: str
    target: str
    severity: str
    message: str
    repair_instruction: str
    fingerprint: str

    @classmethod
    def from_parts(
        cls,
        *,
        code: str,
        target: str,
        severity: str,
        message: str,
        repair_instruction: str,
    ) -> ReviewFinding:
        raw = f"{code}|{target}|{message}|{repair_instruction}"
        fingerprint = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return cls(code, target, severity, message, repair_instruction, fingerprint)


@dataclass(frozen=True, slots=True)
class SeedReview:
    """Review result with grade and stable findings."""

    grade_result: GradeResult
    findings: tuple[ReviewFinding, ...]

    @property
    def may_run(self) -> bool:
        return self.grade_result.may_run


class SeedReviewer:
    """Review Seeds using deterministic GradeGate findings."""

    def __init__(self, grade_gate: GradeGate | None = None) -> None:
        self.grade_gate = grade_gate or GradeGate()

    def review(self, seed: Seed, *, ledger: SeedDraftLedger | None = None) -> SeedReview:
        """Return structured review findings for ``seed``."""
        grade = self.grade_gate.grade_seed(seed, ledger=ledger)
        findings = tuple(
            ReviewFinding.from_parts(
                code=finding.code,
                target=finding.target,
                severity=finding.severity,
                message=finding.message,
                repair_instruction=finding.repair_instruction,
            )
            for finding in [*grade.findings, *grade.blockers]
        )
        return SeedReview(grade_result=grade, findings=findings)
