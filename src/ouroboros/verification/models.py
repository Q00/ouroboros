"""Data models for spec verification.

Verification tiers classify ACs by how they can be independently verified:
- T1: Constants/config values — regex extraction from source
- T2: Structural — file/class/function existence grep
- T3: Behavioral — requires test execution or LLM analysis
- T4: Unverifiable — subjective criteria, skip
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, StrictInt, model_validator


class VerificationTier(StrEnum):
    """How an AC can be independently verified."""

    T1_CONSTANT = "t1_constant"
    T2_STRUCTURAL = "t2_structural"
    T3_BEHAVIORAL = "t3_behavioral"
    T4_UNVERIFIABLE = "t4_unverifiable"


class VerificationOutcome(StrEnum):
    """What independent verification actually established.

    ``UNVERIFIABLE`` means the verifier had no usable evidence surface (for
    example, no candidate files or no usable pattern). ``SKIPPED`` means the
    assertion belongs to a tier this source scanner deliberately does not run.
    Neither outcome is a successful verification.
    """

    VERIFIED = "verified"
    DISCREPANCY = "discrepancy"
    UNVERIFIABLE = "unverifiable"
    SKIPPED = "skipped"


class SpecAssertion(BaseModel, frozen=True):
    """A verifiable assertion extracted from an acceptance criterion.

    The extractor converts human-readable ACs into structured assertions
    that the verifier can check against actual source code.
    """

    ac_index: StrictInt = Field(ge=0)
    ac_text: str
    tier: VerificationTier
    pattern: str = ""
    expected_value: str = ""
    file_hint: str = ""
    description: str = ""


class SpecVerificationResult(BaseModel, frozen=True):
    """Result of verifying a single assertion against source code.

    The boolean fields remain serialized so existing callers and persisted
    payloads keep working. New code should use ``outcome``. Legacy payloads
    without it are inferred fail-closed, then every boolean is normalized from
    the canonical outcome. When an outcome is supplied explicitly it is always
    authoritative, even if legacy booleans contradict it.
    """

    assertion: SpecAssertion
    outcome: VerificationOutcome = VerificationOutcome.DISCREPANCY
    verified: bool = False
    actual_value: str = ""
    file_path: str = ""
    discrepancy: bool = False
    unverifiable: bool = False
    skipped: bool = False
    detail: str = ""

    @model_validator(mode="after")
    def _normalize_legacy_booleans(self) -> SpecVerificationResult:
        fields_set = self.model_fields_set
        if "outcome" not in fields_set:
            # ``verified`` / ``discrepancy`` were the only historical flags.
            # A false legacy result remains a discrepancy even when its old
            # ``discrepancy`` bit was omitted or contradictory. The two newer
            # flags let transitional callers construct the richer outcomes
            # without supplying the enum yet.
            # Contradictory legacy flags are untrusted persisted input. Prefer
            # the strongest non-success evidence instead of allowing a stale
            # ``verified=True`` bit to launder it into a PASS.
            if self.discrepancy:
                inferred = VerificationOutcome.DISCREPANCY
            elif self.unverifiable:
                inferred = VerificationOutcome.UNVERIFIABLE
            elif self.skipped:
                inferred = VerificationOutcome.SKIPPED
            elif self.verified:
                inferred = VerificationOutcome.VERIFIED
            else:
                inferred = VerificationOutcome.DISCREPANCY
            object.__setattr__(self, "outcome", inferred)

        expected_verified = self.outcome is VerificationOutcome.VERIFIED
        expected_discrepancy = self.outcome is VerificationOutcome.DISCREPANCY
        expected_unverifiable = self.outcome is VerificationOutcome.UNVERIFIABLE
        expected_skipped = self.outcome is VerificationOutcome.SKIPPED
        object.__setattr__(self, "verified", expected_verified)
        object.__setattr__(self, "discrepancy", expected_discrepancy)
        object.__setattr__(self, "unverifiable", expected_unverifiable)
        object.__setattr__(self, "skipped", expected_skipped)
        return self


class ACVerificationReport(BaseModel, frozen=True):
    """Verification report for a single acceptance criterion.

    An AC may produce multiple assertions (e.g., "WARMUP=10 and FPS=30"
    yields two T1 assertions). The AC passes only if ALL assertions pass.
    """

    ac_index: StrictInt = Field(ge=0)
    ac_text: str
    results: tuple[SpecVerificationResult, ...] = ()
    agent_reported_pass: bool = True

    @model_validator(mode="after")
    def _validate_assertion_identity(self) -> ACVerificationReport:
        for position, result in enumerate(self.results):
            assertion = result.assertion
            if assertion.ac_index != self.ac_index:
                raise ValueError(
                    f"result {position + 1} assertion ac_index must match parent report"
                )
            if assertion.ac_text != self.ac_text:
                raise ValueError(f"result {position + 1} assertion text must match parent report")
        return self

    @property
    def verified_pass(self) -> bool:
        """True if all assertions verified successfully."""
        if not self.results:
            return self.agent_reported_pass
        return all(r.outcome is VerificationOutcome.VERIFIED for r in self.results)

    @property
    def has_discrepancy(self) -> bool:
        """Legacy signal that an agent PASS was not independently verified.

        This intentionally retains the pre-outcome API semantics. Use
        ``has_confirmed_discrepancy`` when missing evidence must stay distinct
        from evidence that contradicts the assertion.
        """
        return self.agent_reported_pass and not self.verified_pass

    @property
    def has_confirmed_discrepancy(self) -> bool:
        """True if usable evidence contradicted an agent-reported PASS."""
        return self.agent_reported_pass and any(
            result.outcome is VerificationOutcome.DISCREPANCY for result in self.results
        )

    @property
    def has_unverifiable(self) -> bool:
        """True if any assertion lacked a usable independent evidence surface."""
        return any(result.outcome is VerificationOutcome.UNVERIFIABLE for result in self.results)

    @property
    def has_skipped(self) -> bool:
        """True if any assertion was deliberately outside this verifier's tiers."""
        return any(result.outcome is VerificationOutcome.SKIPPED for result in self.results)


class SpecVerificationSummary(BaseModel, frozen=True):
    """Summary of spec verification across all ACs."""

    reports: tuple[ACVerificationReport, ...] = ()
    project_dir: str = ""
    total_assertions: int = 0
    verified_count: int = 0
    failed_count: int = 0
    unverifiable_count: int = 0
    skipped_count: int = 0
    discrepancy_count: int = 0
    confirmed_discrepancy_count: int = 0
    strict: bool = True

    @model_validator(mode="after")
    def _canonicalize_derived_counts(self) -> SpecVerificationSummary:
        if self.reports:
            report_indices = [report.ac_index for report in self.reports]
            if len(report_indices) != len(set(report_indices)):
                raise ValueError("duplicate report ac_index values are not authoritative")
            # Reports contain canonicalized result outcomes, including when
            # their source payload used only legacy booleans. Every persisted
            # count is derived compatibility data once reports exist, so never
            # let contradictory counters suppress or manufacture authority.
            counts = self._counts_from_reports(self.reports)
            for field_name, value in counts.items():
                object.__setattr__(self, field_name, value)
        elif "confirmed_discrepancy_count" not in self.model_fields_set and self.discrepancy_count:
            # Report-less legacy summaries have no stronger evidence surface.
            # Preserve their historical fail-closed discrepancy override.
            object.__setattr__(
                self,
                "confirmed_discrepancy_count",
                self.discrepancy_count,
            )
        return self

    @staticmethod
    def _counts_from_reports(
        reports: tuple[ACVerificationReport, ...],
    ) -> dict[str, int]:
        total = sum(len(report.results) for report in reports)
        verified = sum(
            sum(1 for result in report.results if result.outcome is VerificationOutcome.VERIFIED)
            for report in reports
        )
        confirmed_discrepancies = sum(
            sum(1 for result in report.results if result.outcome is VerificationOutcome.DISCREPANCY)
            for report in reports
        )
        unverifiable = sum(
            sum(
                1 for result in report.results if result.outcome is VerificationOutcome.UNVERIFIABLE
            )
            for report in reports
        )
        failed = confirmed_discrepancies + unverifiable
        skipped = sum(1 for report in reports if not report.results) + sum(
            sum(1 for result in report.results if result.outcome is VerificationOutcome.SKIPPED)
            for report in reports
        )
        discrepancies = sum(1 for report in reports if report.has_discrepancy)
        return {
            "total_assertions": total,
            "verified_count": verified,
            "failed_count": failed,
            "unverifiable_count": unverifiable,
            "skipped_count": skipped,
            "discrepancy_count": discrepancies,
            "confirmed_discrepancy_count": confirmed_discrepancies,
        }

    @property
    def has_discrepancies(self) -> bool:
        """Legacy signal that one or more claimed passes were not verified."""
        return self.discrepancy_count > 0

    @property
    def has_confirmed_discrepancies(self) -> bool:
        """True if usable evidence contradicted one or more claimed passes."""
        return self.confirmed_discrepancy_count > 0

    @property
    def has_unverifiable(self) -> bool:
        """True if verification could not run for one or more assertions."""
        return self.unverifiable_count > 0

    @property
    def has_skipped(self) -> bool:
        """True if one or more assertions were outside the scanner's tiers."""
        return self.skipped_count > 0

    @property
    def has_incomplete_verification(self) -> bool:
        """True if any assertion lacks a genuine VERIFIED outcome."""
        return self.has_unverifiable or self.has_skipped

    @property
    def override_approval(self) -> bool | None:
        """Whether to override the mechanical approval.

        Returns False if discrepancies found, None if no override needed.
        """
        if self.has_confirmed_discrepancies or (self.strict and self.has_incomplete_verification):
            return False
        return None

    @staticmethod
    def from_reports(
        reports: tuple[ACVerificationReport, ...],
        project_dir: str = "",
        *,
        strict: bool = True,
    ) -> SpecVerificationSummary:
        """Build summary from individual AC reports."""
        counts = SpecVerificationSummary._counts_from_reports(reports)
        return SpecVerificationSummary(
            reports=reports,
            project_dir=project_dir,
            strict=strict,
            **counts,
        )
