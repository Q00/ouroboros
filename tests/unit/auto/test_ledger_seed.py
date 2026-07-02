"""Tests for :mod:`ouroboros.auto.ledger_seed`.

Covers the legacy ``synthesize_seed_from_ledger`` path and the new
``partial_seed_from_evidence`` degraded-recovery substrate added for #1257
PR-A. The substrate must:

* preserve the strict legacy contract (no behavior change when the ledger is
  complete),
* produce a *valid* Seed from an incomplete ledger,
* surface unresolved sections through ``SeedMetadata.unresolved_slots`` and the
  constraints list so downstream gates can convert them into next-step hints,
* refuse to synthesize when goal itself is missing (structural defect, not a
  deadline-recovery case), and
* perform no model/external IO (implicitly: only deterministic operations).
"""

from __future__ import annotations

import pytest

from ouroboros.auto.ledger import (
    LedgerEntry,
    LedgerSource,
    LedgerStatus,
    SeedDraftLedger,
)
from ouroboros.auto.ledger_seed import (
    PARTIAL_SEED_GENERATION_MODE,
    partial_seed_from_evidence,
    synthesize_seed_from_ledger,
)
from ouroboros.core.seed import Seed, ac_texts


def _populate_complete_ledger(goal: str = "Build a CLI tool that prints hello.") -> SeedDraftLedger:
    """Build a ledger where every required section has a CONFIRMED entry."""
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


class TestSynthesizeSeedFromLedgerUnchanged:
    """PR-A is additive: the strict legacy path must remain bit-for-bit equivalent."""

    def test_complete_ledger_still_produces_normal_seed(self) -> None:
        ledger = _populate_complete_ledger()
        seed = synthesize_seed_from_ledger(ledger, interview_id="iv-1")

        assert isinstance(seed, Seed)
        assert seed.goal.startswith("Build a CLI tool")
        # New SeedMetadata fields keep their defaults — no regression for callers
        # that never touched ``generation_mode`` / ``degraded`` / etc.
        assert seed.metadata.generation_mode == "normal"
        assert seed.metadata.degraded is False
        assert seed.metadata.unresolved_slots == ()
        assert seed.metadata.recovery_reason is None
        assert seed.metadata.interview_id == "iv-1"

    def test_incomplete_ledger_still_raises_on_strict_path(self) -> None:
        # Goal-only ledger is intentionally not Seed-ready; legacy contract is
        # to refuse rather than fabricate.
        ledger = SeedDraftLedger.from_goal("Some goal.")
        with pytest.raises(ValueError, match="incomplete ledger"):
            synthesize_seed_from_ledger(ledger)


class TestPartialSeedFromEvidence:
    """Substrate for #1257 PR-B's interview-deadline closure ladder."""

    def test_returns_valid_seed_when_ledger_incomplete(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal that survived the deadline.")
        seed = partial_seed_from_evidence(
            ledger,
            reason="interview_phase_deadline",
            interview_id="iv-partial",
        )

        # Pydantic validity is implicit in successful construction, but assert
        # the contract surface explicitly.
        assert isinstance(seed, Seed)
        assert seed.goal == "Goal that survived the deadline."
        assert seed.metadata.generation_mode == PARTIAL_SEED_GENERATION_MODE
        assert seed.metadata.degraded is True
        assert seed.metadata.recovery_reason == "interview_phase_deadline"
        assert seed.metadata.interview_id == "iv-partial"

    def test_unresolved_slots_match_open_gaps(self) -> None:
        ledger = SeedDraftLedger.from_goal("A bare goal.")
        # ``from_goal`` only resolves the goal section; every other required
        # section is MISSING and therefore an open gap.
        open_gaps = set(ledger.open_gaps())
        # Goal itself is resolved by ``from_goal``.
        assert "goal" not in open_gaps

        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

        # Every open gap is surfaced verbatim — including ``goal`` when the
        # aggregate goal status itself is unresolved (see
        # ``test_blocked_goal_entry_surfaced_in_unresolved_slots``).
        assert set(seed.metadata.unresolved_slots) == open_gaps
        # And every unresolved slot is surfaced through constraints so the
        # executor cannot silently assume completeness.
        for slot in seed.metadata.unresolved_slots:
            assert any(
                slot in constraint and "Known unresolved slot" in constraint
                for constraint in seed.constraints
            ), f"missing unresolved-slot constraint for {slot}"

    def test_blocked_goal_entry_surfaced_in_unresolved_slots(self) -> None:
        """A CONFIRMED-then-BLOCKED goal section is degraded with goal provenance.

        Regression for the #1269 review blocker: ``open_gaps()`` reports
        ``"goal"`` whenever the aggregate goal-section status is
        MISSING / WEAK / CONFLICTING / BLOCKED, but ``_latest_value`` still
        returns the active CONFIRMED value. Earlier revisions filtered
        ``goal`` out of ``unresolved_slots`` unconditionally, leaving
        ``degraded=True`` with ``unresolved_slots=()`` and no
        ``"Known unresolved slot (goal)"`` constraint — a silent provenance
        loss that PR-C gates would have mistaken for a fully resolved goal.
        """
        ledger = SeedDraftLedger.from_goal("Original goal that survived.")
        # A later same-section different-key BLOCKED entry tips the aggregate
        # status to BLOCKED without invalidating the CONFIRMED active value.
        ledger.add_entry(
            "goal",
            LedgerEntry(
                key="goal.review_blocker",
                value="Reviewer raised a blocker on the goal interpretation.",
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.BLOCKED,
            ),
        )

        # Active goal value is still available — the deadline can still
        # recover into *something* — but ``goal`` is in ``open_gaps``.
        assert "goal" in set(ledger.open_gaps())

        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

        assert seed.goal == "Original goal that survived."
        assert seed.metadata.degraded is True
        assert "goal" in seed.metadata.unresolved_slots, (
            "BLOCKED goal aggregate must be surfaced as unresolved provenance, not silently dropped"
        )
        assert any(
            "Known unresolved slot (goal)" in constraint for constraint in seed.constraints
        ), "constraints must carry the goal-unresolved next-step hint"

    def test_conflicting_goal_entry_surfaced_in_unresolved_slots(self) -> None:
        """A CONFIRMED-plus-CONFLICTING goal section is degraded with goal provenance.

        Sibling regression to the BLOCKED case: ``LedgerSection.status()``
        returns CONFLICTING when no entry is BLOCKED but at least one is
        CONFLICTING. ``_latest_value`` still returns the latest non-inactive
        (CONFIRMED) goal, so the deadline has something to recover into —
        but the aggregate goal status is contested and the recovery contract
        must surface that.
        """
        ledger = SeedDraftLedger.from_goal("Primary goal still in scope.")
        ledger.add_entry(
            "goal",
            LedgerEntry(
                key="goal.alt_interpretation",
                value="An alternate goal phrasing the interview never resolved.",
                source=LedgerSource.USER_GOAL,
                confidence=0.7,
                status=LedgerStatus.CONFLICTING,
            ),
        )

        assert "goal" in set(ledger.open_gaps())

        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

        assert seed.goal == "Primary goal still in scope."
        assert seed.metadata.degraded is True
        assert "goal" in seed.metadata.unresolved_slots
        assert any("Known unresolved slot (goal)" in constraint for constraint in seed.constraints)

    def test_complete_ledger_marks_seed_non_degraded(self) -> None:
        ledger = _populate_complete_ledger()
        seed = partial_seed_from_evidence(ledger, reason="forced_review")

        assert seed.metadata.degraded is False
        assert seed.metadata.unresolved_slots == ()
        # Still tagged with the partial generation_mode so audit can tell this
        # Seed came from the recovery path even though no gap existed.
        assert seed.metadata.generation_mode == PARTIAL_SEED_GENERATION_MODE
        assert seed.metadata.recovery_reason == "forced_review"

    def test_missing_goal_raises_structural_error(self) -> None:
        # An empty goal short-circuits ``from_goal`` and leaves the goal
        # section in WEAK state without an active value.
        ledger = SeedDraftLedger.from_goal("")
        with pytest.raises(ValueError, match="structural defect"):
            partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

    def test_blank_reason_rejected(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal.")
        with pytest.raises(ValueError, match="non-empty reason"):
            partial_seed_from_evidence(ledger, reason="   ")

    def test_defaults_fill_missing_acceptance_and_verification(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal only.")
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

        assert seed.acceptance_criteria, "acceptance must be populated from defaults"
        assert len(seed.exit_conditions) >= 1
        verification = seed.exit_conditions[0].evaluation_criteria
        assert "smoke" in verification.lower()

    def test_ambiguity_score_elevated_for_degraded_seed(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal only.")
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        assert seed.metadata.ambiguity_score >= 0.6, (
            "degraded seed must carry an elevated ambiguity floor so downstream "
            "observers can see the deadline-driven uncertainty without inspecting "
            "the recovery_reason field"
        )

    def test_existing_ledger_evidence_preserved(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal with partial evidence.")
        ledger.add_entry(
            "constraints",
            LedgerEntry(
                key="constraints.partial",
                value="Must run offline.",
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
            ),
        )

        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

        # ``_lines_from_section`` strips trailing punctuation as part of the
        # legacy normalization shared with ``synthesize_seed_from_ledger`` —
        # match that contract instead of the raw entry value.
        assert "Must run offline" in seed.constraints


def _complete_ledger_with_acceptance(value: str) -> SeedDraftLedger:
    """Complete ledger whose acceptance_criteria entry carries ``value`` verbatim."""
    ledger = _populate_complete_ledger()
    # Deactivate the default acceptance entry (WEAK is an inactive status) so
    # only ``value`` contributes to the synthesized acceptance_criteria.
    section = ledger.sections.get("acceptance_criteria")
    if section is not None:
        for entry in section.entries:
            entry.status = LedgerStatus.WEAK
    ledger.add_entry(
        "acceptance_criteria",
        LedgerEntry(
            key="acceptance_criteria.custom",
            value=value,
            source=LedgerSource.USER_GOAL,
            confidence=0.9,
            status=LedgerStatus.CONFIRMED,
        ),
    )
    return ledger


class TestAcceptanceCriteriaNotExplodedOnSemicolons:
    """Over-atomization guard: a semicolon-rich AC value must stay one criterion.

    Each acceptance criterion becomes a full agent session at execution time, so
    mechanically splitting one outcome on every ``;`` multiplies token cost with
    no benefit. Bullet/newline markers remain a legitimate split signal.
    """

    def test_semicolon_joined_acceptance_stays_single_criterion(self) -> None:
        ledger = _complete_ledger_with_acceptance(
            "Tasks persist to a file; the data survives a process restart"
        )
        seed = synthesize_seed_from_ledger(ledger, interview_id="iv-semicolon")
        assert ac_texts(seed.acceptance_criteria) == (
            "Tasks persist to a file; the data survives a process restart",
        )

    def test_bullet_joined_acceptance_still_splits(self) -> None:
        ledger = _complete_ledger_with_acceptance(
            "- Tasks can be created\n- Tasks can be listed\n- Tasks persist"
        )
        seed = synthesize_seed_from_ledger(ledger, interview_id="iv-bullets")
        assert ac_texts(seed.acceptance_criteria) == (
            "Tasks can be created",
            "Tasks can be listed",
            "Tasks persist",
        )

    def test_partial_path_also_preserves_semicolon_clauses(self) -> None:
        ledger = SeedDraftLedger.from_goal("Goal with a clause-rich AC.")
        ledger.add_entry(
            "acceptance_criteria",
            LedgerEntry(
                key="acceptance_criteria.partial",
                value="API returns 200; payload is valid JSON",
                source=LedgerSource.USER_GOAL,
                confidence=0.9,
                status=LedgerStatus.CONFIRMED,
            ),
        )
        seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")
        assert "API returns 200; payload is valid JSON" in ac_texts(seed.acceptance_criteria)
