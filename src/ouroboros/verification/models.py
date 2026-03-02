"""Data models for spec verification.

Verification tiers classify ACs by how they can be independently verified:
- T1: Constants/config values — regex extraction from source
- T2: Structural — file/class/function existence grep
- T3: Behavioral — requires test execution or LLM analysis
- T4: Unverifiable — subjective criteria, skip
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class VerificationTier(StrEnum):
    """How an AC can be independently verified."""

    T1_CONSTANT = "t1_constant"
    T2_STRUCTURAL = "t2_structural"
    T3_BEHAVIORAL = "t3_behavioral"
    T4_UNVERIFIABLE = "t4_unverifiable"


class SpecAssertion(BaseModel, frozen=True):
    """A verifiable assertion extracted from an acceptance criterion.

    The extractor converts human-readable ACs into structured assertions
    that the verifier can check against actual source code.
    """

    ac_index: int
    ac_text: str
    tier: VerificationTier
    pattern: str = ""
    expected_value: str = ""
    file_hint: str = ""
    description: str = ""


class SpecVerificationResult(BaseModel, frozen=True):
    """Result of verifying a single assertion against source code."""

    assertion: SpecAssertion
    verified: bool
    actual_value: str = ""
    file_path: str = ""
    discrepancy: bool = False
    detail: str = ""


class ACVerificationReport(BaseModel, frozen=True):
    """Verification report for a single acceptance criterion.

    An AC may produce multiple assertions (e.g., "WARMUP=10 and FPS=30"
    yields two T1 assertions). The AC passes only if ALL assertions pass.
    """

    ac_index: int
    ac_text: str
    results: tuple[SpecVerificationResult, ...] = ()
    agent_reported_pass: bool = True

    @property
    def verified_pass(self) -> bool:
        """True if all assertions verified successfully."""
        if not self.results:
            return self.agent_reported_pass
        return all(r.verified for r in self.results)

    @property
    def has_discrepancy(self) -> bool:
        """True if agent reported PASS but verification found FAIL."""
        return self.agent_reported_pass and not self.verified_pass


class SpecVerificationSummary(BaseModel, frozen=True):
    """Summary of spec verification across all ACs."""

    reports: tuple[ACVerificationReport, ...] = ()
    project_dir: str = ""
    total_assertions: int = 0
    verified_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    discrepancy_count: int = 0

    @property
    def has_discrepancies(self) -> bool:
        """True if any AC has a discrepancy (agent lied)."""
        return self.discrepancy_count > 0

    @property
    def override_approval(self) -> bool | None:
        """Whether to override the mechanical approval.

        Returns False if discrepancies found, None if no override needed.
        """
        if self.has_discrepancies:
            return False
        return None

    @staticmethod
    def from_reports(
        reports: tuple[ACVerificationReport, ...],
        project_dir: str = "",
    ) -> SpecVerificationSummary:
        """Build summary from individual AC reports."""
        total = sum(len(r.results) for r in reports)
        verified = sum(sum(1 for v in r.results if v.verified) for r in reports)
        failed = sum(sum(1 for v in r.results if not v.verified) for r in reports)
        skipped = sum(1 for r in reports if not r.results)
        discrepancies = sum(1 for r in reports if r.has_discrepancy)

        return SpecVerificationSummary(
            reports=reports,
            project_dir=project_dir,
            total_assertions=total,
            verified_count=verified,
            failed_count=failed,
            skipped_count=skipped,
            discrepancy_count=discrepancies,
        )
