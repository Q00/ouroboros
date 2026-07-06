"""A1 provenance gate (#1485): source-tagged ledger decisions + low-ambiguity gate.

Covers the decision-origin axis (:class:`DecisionProvenance`) layered onto the
existing content-authority ``LedgerSource``: derivation for unstamped/legacy
entries, explicit stamping at the deadline/timeout backstops, the histogram
surface on ``SeedMetadata``, and the ``unverified_provenance`` grading finding
that makes the #1485 degraded-seed (question-text pollution) structurally
impossible to auto-run.
"""

from __future__ import annotations

import json

from ouroboros.auto.auto_fill import AutoFillProposal, auto_fill_remaining
from ouroboros.auto.grading import GradeGate, SeedGrade
from ouroboros.auto.ledger import (
    DecisionProvenance,
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import partial_seed_from_evidence, synthesize_seed_from_ledger
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata


def _populate_complete_ledger(goal: str = "Build a CLI tool that prints hello.") -> SeedDraftLedger:
    ledger = SeedDraftLedger.from_goal(goal)
    fillers = {
        "actors": "End user invoking the CLI.",
        "inputs": "A single positional argument provided on the command line.",
        "outputs": "stdout text greeting the user.",
        "constraints": "Pure Python; no external network calls.",
        "non_goals": "Long-running daemon mode.",
        "acceptance_criteria": "CLI exits with code 0 and prints the greeting.",
        "verification_plan": "Run the CLI with a sample arg and assert stdout/exit code.",
        "failure_modes": "Missing argument raises a typed error.",
        "runtime_context": "Local developer shell on POSIX.",
    }
    for section, value in fillers.items():
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
            ),
        )
    return ledger


def _entry(
    value: str, *, source: LedgerSource, provenance: DecisionProvenance | None = None
) -> LedgerEntry:
    return LedgerEntry(
        key="section.key",
        value=value,
        source=source,
        confidence=0.7,
        status=LedgerStatus.INFERRED,
        provenance=provenance,
    )


class TestEffectiveProvenance:
    def test_explicit_stamp_wins(self) -> None:
        entry = _entry(
            "x", source=LedgerSource.USER_GOAL, provenance=DecisionProvenance.TIMEOUT_DEFAULT
        )
        assert entry.effective_provenance is DecisionProvenance.TIMEOUT_DEFAULT

    def test_user_sources_derive_to_user_confirmed(self) -> None:
        for source in (
            LedgerSource.USER_GOAL,
            LedgerSource.USER_PREFERENCE,
            LedgerSource.NON_GOAL,
            LedgerSource.REPO_FACT,
            LedgerSource.EXISTING_CONVENTION,
        ):
            assert _entry("x", source=source).effective_provenance is (
                DecisionProvenance.USER_CONFIRMED
            )

    def test_model_sources_derive_to_model_inferred(self) -> None:
        for source in (
            LedgerSource.INFERENCE,
            LedgerSource.ASSUMPTION,
            LedgerSource.AUTO_FILL_INFERENCE,
        ):
            assert _entry("x", source=source).effective_provenance is (
                DecisionProvenance.MODEL_INFERRED
            )

    def test_conservative_default_derives_to_maintainer_policy(self) -> None:
        assert _entry("x", source=LedgerSource.CONSERVATIVE_DEFAULT).effective_provenance is (
            DecisionProvenance.MAINTAINER_POLICY
        )

    def test_unmapped_source_falls_back_to_model_inferred(self) -> None:
        # BLOCKER has no derivation; the safe fallback keeps it gated, not exempt.
        assert _entry("x", source=LedgerSource.BLOCKER).effective_provenance is (
            DecisionProvenance.MODEL_INFERRED
        )


class TestLedgerEntrySerialization:
    def test_legacy_entry_round_trips_without_provenance_key(self) -> None:
        data = _entry("x", source=LedgerSource.USER_GOAL).to_dict()
        assert "provenance" not in data
        assert LedgerEntry.from_dict(data).provenance is None

    def test_stamped_entry_round_trips(self) -> None:
        data = _entry(
            "x", source=LedgerSource.ASSUMPTION, provenance=DecisionProvenance.TIMEOUT_DEFAULT
        ).to_dict()
        assert data["provenance"] == "timeout_default"
        assert LedgerEntry.from_dict(data).provenance is DecisionProvenance.TIMEOUT_DEFAULT

    def test_invalid_provenance_rejected(self) -> None:
        data = _entry("x", source=LedgerSource.USER_GOAL).to_dict()
        data["provenance"] = "not_a_real_origin"
        try:
            LedgerEntry.from_dict(data)
        except ValueError as exc:
            assert "provenance" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected ValueError for invalid provenance")


class TestProvenanceHistogram:
    def test_counts_active_entries_by_origin(self) -> None:
        ledger = _populate_complete_ledger()
        histogram = ledger.provenance_histogram()
        # Every filler + goal echo is USER_GOAL-sourced -> user_confirmed.
        assert histogram.get("user_confirmed", 0) >= 9
        assert "model_inferred" not in histogram

    def test_excludes_inactive_entries(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal.")
        ledger.add_entry(
            "constraints",
            LedgerEntry(
                key="constraints.weak",
                value="ignored",
                source=LedgerSource.ASSUMPTION,
                confidence=0.5,
                status=LedgerStatus.WEAK,
            ),
        )
        assert "model_inferred" not in ledger.provenance_histogram()


class TestStampSites:
    def test_auto_fill_stamps_timeout_default(self) -> None:
        ledger = SeedDraftLedger.from_goal("Build a thing.")

        def fill_slot(section: str, _ledger: SeedDraftLedger) -> AutoFillProposal:
            return AutoFillProposal(value=f"Deadline default for {section}.", confidence=0.6)

        auto_fill_remaining(ledger, fill_slot=fill_slot)
        stamped = [
            entry
            for section in ledger.sections.values()
            for entry in section.entries
            if entry.provenance is DecisionProvenance.TIMEOUT_DEFAULT
        ]
        assert stamped, "auto-fill must stamp timeout_default"

    def test_safe_default_finalization_stamps_timeout_default(self) -> None:
        ledger = SeedDraftLedger.from_goal("Build a CLI that prints hello.")
        finalize_safe_defaultable_gaps(
            ledger, goal="Build a CLI that prints hello.", provenance="auto interview max_rounds=5"
        )
        stamped = [
            entry
            for section in ledger.sections.values()
            for entry in section.entries
            if entry.provenance is DecisionProvenance.TIMEOUT_DEFAULT
        ]
        assert stamped, "safe-default finalization must stamp timeout_default"


class TestUnverifiedProvenanceGate:
    def _ledger_with_gated_acceptance(self, value: str) -> SeedDraftLedger:
        ledger = _populate_complete_ledger()
        for entry in ledger.sections["acceptance_criteria"].entries:
            entry.status = LedgerStatus.WEAK
        ledger.add_entry(
            "acceptance_criteria",
            LedgerEntry(
                key="acceptance_criteria.inferred",
                value=value,
                source=LedgerSource.INFERENCE,  # -> model_inferred, gated
                confidence=0.7,
                status=LedgerStatus.CONFIRMED,
            ),
        )
        return ledger

    def test_clean_model_inferred_entry_passes(self) -> None:
        ledger = self._ledger_with_gated_acceptance(
            "CLI exits with code 0 and prints the greeting to stdout."
        )
        seed = synthesize_seed_from_ledger(ledger)
        result = GradeGate().grade_seed(seed, ledger=ledger)
        assert not any(f.code == "unverified_provenance" for f in result.findings)

    def test_question_text_pollution_produces_finding(self) -> None:
        # The #1485 signature: raw interview question text in a contract field.
        ledger = self._ledger_with_gated_acceptance(
            "What should happen when the argument is missing?"
        )
        seed = synthesize_seed_from_ledger(ledger)
        result = GradeGate().grade_seed(seed, ledger=ledger)
        provenance_findings = [f for f in result.findings if f.code == "unverified_provenance"]
        assert provenance_findings
        assert provenance_findings[0].severity == "medium"
        # Repairable finding, never a hard blocker -> feeds the repair loop.
        assert result.can_repair
        assert not any(b.code == "unverified_provenance" for b in result.blockers)
        assert result.grade is not SeedGrade.A

    def test_gate_inert_without_ledger(self) -> None:
        # Legacy/direct grade_seed callers pass no ledger -> no provenance check.
        seed = synthesize_seed_from_ledger(_populate_complete_ledger())
        result = GradeGate().grade_seed(seed)
        assert not any(f.code == "unverified_provenance" for f in result.findings)


class TestSeedMetadataSurface:
    def test_synthesized_seed_carries_histogram(self) -> None:
        seed = synthesize_seed_from_ledger(_populate_complete_ledger())
        assert seed.metadata.decision_provenance.get("user_confirmed", 0) >= 9

    def test_partial_seed_carries_histogram(self) -> None:
        ledger = SeedDraftLedger.from_goal("Build a CLI that prints hello.")
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        assert seed.metadata.decision_provenance  # non-empty (goal + hydrated sections)

    def test_empty_histogram_omitted_from_json(self) -> None:
        seed = Seed(
            goal="A bare seed with no ledger provenance.",
            ontology_schema=OntologySchema(name="Bare", description="No ledger."),
            metadata=SeedMetadata(ambiguity_score=0.15),
        )
        assert seed.metadata.decision_provenance == {}
        assert "decision_provenance" not in json.dumps(seed.to_dict())
