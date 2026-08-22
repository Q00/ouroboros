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

    def test_complete_ledger_preserves_explicit_document_task_type(self) -> None:
        for goal in (
            "Create a plan document; task_type must be document.",
            "task_type must be document without changing repository files.",
            "Use task_type: document although we must not produce code.",
            "Use task_type: document because source code must not change.",
            "Do not modify source code because task_type must be document.",
            "Rather than change source code, write a plan; task_type: document.",
            "The task type is document.",
            "Set the task type to document.",
            "The task type must remain document.",
            "Keep the task type as document.",
            "Use document as the task type.",
            "The task type must be a document.",
            "The task type should be a document.",
            "Whether to include charts or tables is undecided, but task_type: document.",
            "Use task_type: document for an optional appendix.",
            "task_type must be code. Actually, task_type must be document.",
            "Use task_type: document. Mention task_type: code in the appendix.",
            "Use task_type: document. Compare it with task_type: code.",
            "Use task_type: document. This document mentions task_type: code in prose.",
            "task_type: document should be used.",
        ):
            ledger = _populate_complete_ledger(goal)

            seed = synthesize_seed_from_ledger(ledger)

            assert seed.task_type == "document"

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

    def test_partial_seed_preserves_explicit_document_task_type(self) -> None:
        for goal in (
            "문서형 계획을 만든다. task_type은 document여야 한다.",
            "task_type must be document without changing repository files.",
            "Use task_type: document although we must not produce code.",
            "Use task_type: document because source code must not change.",
            "Do not modify source code because task_type must be document.",
            "The task type must remain document.",
            "Keep the task type as document.",
            "Use document as the task type.",
            "The task type must be a document.",
            "The task type should be a document.",
            "task_type must be code. Actually, task_type must be document.",
            "Use task_type: document. Mention task_type: code in the appendix.",
            "Use task_type: document. Compare it with task_type: code.",
            "Use task_type: document. This document mentions task_type: code in prose.",
            "task_type: document should be used.",
        ):
            ledger = SeedDraftLedger.from_goal(goal)

            seed = partial_seed_from_evidence(ledger, reason="interview_phase_deadline")

            assert seed.task_type == "document"

    def test_complete_and_partial_ledgers_respect_retraction_and_reconfirmation(self) -> None:
        rejected_goals = (
            "No, task_type: document.",
            "task_type: document, but not anymore.",
            "task_type: document, but we decided against it.",
            "task_type: document, but scratch that.",
            "task_type: document. Actually, scratch that.",
            "task_type: document. We decided against it.",
            "task_type: document. That requirement was rejected.",
            "task_type: document. Never mind.",
            "task_type: document. Forget that.",
            "task_type: document. Cancel that requirement.",
            "task_type: document. I take that back.",
            "task_type: document. However, cancel that requirement.",
            "task_type: document. But cancel that requirement.",
            "task_type: document. That was only an example.",
            "task_type: document. I was only giving an example.",
            "task_type: document. Please disregard that requirement.",
            "task_type: document. Withdraw that requirement.",
            "task_type: document. Retract that requirement.",
            "task_type: document. Cancel this requirement.",
        )
        corrected = (
            "task_type: code. Actually, task_type: document. Confirmed: task_type: document."
        )

        for goal in rejected_goals:
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )
            assert complete.task_type == "code"
            assert partial.task_type == "code"

        complete = synthesize_seed_from_ledger(_populate_complete_ledger(corrected))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(corrected), reason="interview_phase_deadline"
        )
        assert complete.task_type == "document"
        assert partial.task_type == "document"

    def test_complete_and_partial_ledgers_scope_mixed_contract_retractions(self) -> None:
        goal = "task_type: document. Inherit seed_old. Cancel that requirement."
        complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
        )

        assert complete.task_type == "document"
        assert partial.task_type == "document"

    def test_complete_and_partial_ledgers_scope_unrelated_modal_clause(self) -> None:
        goal = "We may revise the title, and task_type: document."
        complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
        )
        assert complete.task_type == "document"
        assert partial.task_type == "document"

    def test_complete_and_partial_ledgers_honor_semicolon_retraction(self) -> None:
        for goal in (
            "task_type: document; cancel that requirement.",
            "Inherit seed_old; cancel that requirement.",
            "Inherit seed_good. Use SVG. Cancel the parent requirement.",
            "Inherit seed_good. Use SVG. Retract the parent contract.",
        ):
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )
            if goal.startswith("task_type"):
                assert complete.task_type == "code"
                assert partial.task_type == "code"
            else:
                assert complete.metadata.parent_seed_id is None
                assert partial.metadata.parent_seed_id is None

    def test_complete_and_partial_ledgers_accept_replacement_after_cancellation(self) -> None:
        goal = "task_type: code. Cancel that requirement. task_type: document."

        complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
        )

        assert complete.task_type == "document"
        assert partial.task_type == "document"

    def test_complete_and_partial_ledgers_ignore_unrelated_cancellation_and_output_prose(
        self,
    ) -> None:
        goals = (
            "Use task_type: document. Cancel lunch.",
            "Write a validator that emits the line task_type: document.",
        )

        for goal in goals:
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )
            expected = "document" if "Use task_type" in goal else "code"
            assert complete.task_type == expected
            assert partial.task_type == expected

    def test_complete_and_partial_ledgers_fail_closed_on_validation_content(self) -> None:
        goals = (
            "Build a parser that validates task_type: document.",
            "Write a validator that parses parent_seed_id: seed_bad.",
            "Write unit tests asserting task_type: document.",
            "Create documentation showing how to set task_type: document.",
            "Generate a YAML example containing task_type: document.",
            "Return JSON with task_type: document.",
            "The generated manifest must set task_type: document.",
            "Add a CLI flag whose help text says task_type: document.",
            "Implement a validator whose error message says task_type: document.",
            "Create docs with the sentence task_type: document.",
            "Add support for task_type: document in the Seed API.",
            "Update the parser to accept task_type: document.",
            "Add a test for task_type: document.",
            "Add support for parent_seed_id: seed_demo in the API.",
            "The schema must accept parent_seed_id: seed_demo.",
            "Add a test for parent_seed_id: seed_demo.",
            "Fix handling when users inherit seed_demo.",
            "Analyze why the API accepts task_type: document.",
            "Analyze why the API accepts parent_seed_id: seed_old.",
            "Rename task_type: document to artifact in the API.",
            "Refactor task_type: document handling.",
            "Deprecate task_type: document.",
            "Rename parent_seed_id: seed_old to predecessor_id.",
            "Remove parent_seed_id: seed_old from the API.",
        )

        positive = "Implement this as task_type: document."
        complete = synthesize_seed_from_ledger(_populate_complete_ledger(positive))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(positive), reason="interview_phase_deadline"
        )
        assert complete.task_type == "document"
        assert partial.task_type == "document"
        for goal in goals:
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )
            assert complete.task_type == "code"
            assert partial.task_type == "code"

    def test_complete_and_partial_ledgers_ignore_unrelated_bare_retractions(self) -> None:
        goal = "Use task_type: document. Never mind the earlier color choice."

        complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
        )

        assert complete.task_type == "document"
        assert partial.task_type == "document"

    def test_complete_and_partial_ledgers_preserve_binding_before_content_example(self) -> None:
        for goal in (
            "Use task_type: document. The report should say task_type: code.",
            "Use task_type: document. A test fixture contains task_type: code.",
        ):
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )
            assert complete.task_type == "document"
            assert partial.task_type == "document"

    def test_ledgers_ignore_comma_prefixed_reference_task_types(self) -> None:
        goal = "The task type must be document. For example, task_type: code."

        complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
        )

        assert complete.task_type == "document"
        assert partial.task_type == "document"

        rejected = "Do not, under any circumstances, use task_type: document."
        assert synthesize_seed_from_ledger(_populate_complete_ledger(rejected)).task_type == "code"
        assert (
            partial_seed_from_evidence(
                SeedDraftLedger.from_goal(rejected), reason="interview_phase_deadline"
            ).task_type
            == "code"
        )

    def test_ledgers_preserve_descriptive_task_type_contracts(self) -> None:
        for goal in (
            "Create a guide explaining setup with task_type: document.",
            "Summarize the rejected proposal with task_type: document.",
            "Use task_type: document. As a reference, task_type: code appears in old docs.",
        ):
            assert (
                synthesize_seed_from_ledger(_populate_complete_ledger(goal)).task_type == "document"
            )
            assert (
                partial_seed_from_evidence(
                    SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
                ).task_type
                == "document"
            )

    def test_ledgers_ignore_api_schema_and_parser_task_type_content(self) -> None:
        for goal in (
            "The API returns a Seed where task_type: document.",
            "The schema property task_type is document.",
            "Ensure the parser preserves strings where task_type: document.",
            "Persist task_type: document in the database.",
            "Store task_type: document in the session state.",
            "Expose task_type: document in the CLI output.",
            "The config field task_type: document controls rendering.",
            "Route task_type: document requests to artifact workers.",
            "Map PDFs to task_type: document in the routing table.",
            "Read task_type: document from the config.",
            "When the user asks for a document, set task_type: document in the generated Seed.",
            "When task_type: document, render Markdown output in the existing Python service.",
            "Handle task_type: document by rendering Markdown while keeping this Python CLI implementation.",
        ):
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )
            assert complete.task_type == partial.task_type == "code"

    def test_ledgers_preserve_clause_local_document_contract(self) -> None:
        goal = "Without changing the existing task type, task_type must remain document."
        assert synthesize_seed_from_ledger(_populate_complete_ledger(goal)).task_type == "document"
        assert (
            partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            ).task_type
            == "document"
        )

    def test_ledgers_preserve_contracts_with_unrelated_same_clause_details(self) -> None:
        goal = (
            "Use task_type: document to explain why the old proposal was rejected. "
            "Inherit seed_good for either a PDF or DOCX migration note."
        )
        complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
        partial = partial_seed_from_evidence(
            SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
        )
        assert complete.task_type == partial.task_type == "document"

    def test_historical_task_type_does_not_override_ledger_default(self) -> None:
        for goal in (
            "We discussed task_type: document in the rejected proposal. Build a CLI.",
            "The task_type: document requirement was rejected.",
            "Use the default code task instead of task_type: document.",
            "Rather than use task_type: document, keep the code task.",
            "We no longer use task_type: document.",
            "In the previous proposal, task_type: document.",
            "It is not true that task_type: document.",
            "We are not using task_type: document.",
            "task_type: document will not be used.",
            "Build a Python CLI; task_type: document is not required for this Seed.",
            "task_type: document isn't required for this Seed.",
            "task_type: document is unnecessary for this Seed.",
            "task_type: document, but not required.",
            "task_type: document, but no longer needed.",
            "task_type: document, but unnecessary.",
            "Use task_type: document, pending approval.",
            "Use task_type: document, unless the user asks for code.",
            "Use task_type: document, but maybe code.",
            "Use task_type: document. Forget it.",
            "Use task_type: document. I take it back.",
            "Use task_type: document. Cancel it.",
            "task_type: document need not be used.",
            "The task type is document only if requested.",
            "The task type is document only when explicitly requested.",
            "task_type: document is no longer required.",
            "We won't use task_type: document.",
            "We won’t use task_type: document.",
            "task_type: document should be avoided.",
            "Choose between task_type: document or code later.",
            "We did not select task_type: document.",
            "We have not selected task_type: document.",
            "There is no requirement that task_type: document.",
            "The team declined to use task_type: document.",
            "We didn't select task_type: document.",
            "We decided not to use task_type: document.",
            "The requirement to use task_type: document was declined.",
            "We rejected task_type: document.",
            "We abandoned task_type: document.",
            "task_type: document, which was rejected.",
            "We ruled out task_type: document.",
            "We opted not to use task_type: document.",
            "Build a CLI whose README says task_type: document.",
            "Implement a validator whose error says task_type: document.",
            "Generate a TOML file with task_type: document.",
            "Write tests where the expected string is task_type: document.",
            "The YAML file must set task_type: document.",
            "Write a README saying task_type: document.",
        ):
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )

            assert complete.task_type == "code"
            assert partial.task_type == "code"

    def test_contract_scope_is_preserved_through_complete_and_partial_ledgers(self) -> None:
        positive_goals = (
            "We'll use task_type: document for the final plan.",
            "Use task_type: document for the historical archive.",
            "Rather than change source code, write a plan; task_type: document.",
            "The task type is document.",
            "Set the task type to document.",
            "The task type must remain document.",
            "Keep the task type as document.",
            "Use document as the task type.",
            "Use task_type: document for the final proposal.",
            "task_type: document. For reference, task_type: code is an example.",
            "Implement the requested document with task_type: document.",
        )
        for goal in positive_goals:
            complete = synthesize_seed_from_ledger(_populate_complete_ledger(goal))
            partial = partial_seed_from_evidence(
                SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
            )
            assert complete.task_type == "document"
            assert partial.task_type == "document"

        historical = (
            "The previous proposal was rejected, but its task_type: document "
            "should be recorded for audit."
        )
        assert (
            synthesize_seed_from_ledger(_populate_complete_ledger(historical)).task_type == "code"
        )
        assert (
            partial_seed_from_evidence(
                SeedDraftLedger.from_goal(historical),
                reason="interview_phase_deadline",
            ).task_type
            == "code"
        )

        rejected = (
            "task_type: document will not be used.",
            "Build a Python CLI; task_type: document is not required for this Seed.",
            "task_type: document isn't required for this Seed.",
            "task_type: document is unnecessary for this Seed.",
            "task_type: document need not be used.",
            "Use task_type: document pending approval.",
            "Use task_type: document after approval.",
            "Use task_type: document once approved.",
            "Use task_type: document upon approval.",
            "Use task_type: document assuming approval.",
            "Use task_type: document contingent on approval.",
            "Use task_type: document subject to approval.",
            "The task type is document only if requested.",
            "The task type is document only when explicitly requested.",
            "task_type: document is no longer required.",
            "We won't use task_type: document.",
            "We won’t use task_type: document.",
        )
        for goal in rejected:
            assert synthesize_seed_from_ledger(_populate_complete_ledger(goal)).task_type == "code"
            assert (
                partial_seed_from_evidence(
                    SeedDraftLedger.from_goal(goal), reason="interview_phase_deadline"
                ).task_type
                == "code"
            )

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
