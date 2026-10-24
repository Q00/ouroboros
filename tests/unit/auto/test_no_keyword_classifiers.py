"""The auto interview decides nothing from keywords in text.

The intent guard no longer runs on auto answers: an auto answer that narrows
the user's output contract cannot bind, so it cannot override the contract.
"""

from __future__ import annotations

from ouroboros.auto.answer_binding import answer_provenance
from ouroboros.auto.answerer import AutoAnswer, AutoAnswerer, AutoAnswerSource
from ouroboros.auto.interview_driver import AutoInterviewDriver
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.ledger_seed import synthesize_seed_from_ledger
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps

GOAL = "Generate report.pdf from the CSV file with --mode auto"
NARROWING = "Use --mode review only: print the plan and write no report."


def test_driver_has_no_intent_guard_step() -> None:
    assert not hasattr(AutoInterviewDriver, "_guard_intent_answer")


def test_a_generated_narrowing_answer_never_binds() -> None:
    ledger = SeedDraftLedger.from_goal(GOAL)
    question = "Should the CLI run in --mode auto or --mode review?"
    answer = AutoAnswer(
        NARROWING,
        AutoAnswerSource.ASSUMPTION,
        0.5,
        [
            (
                "outputs",
                LedgerEntry(
                    key="outputs.mode",
                    value=NARROWING,
                    source=LedgerSource.ASSUMPTION,
                    confidence=0.5,
                    status=LedgerStatus.DEFAULTED,
                ),
            )
        ],
    )

    AutoAnswerer().apply(answer, ledger, question=question, asked=question)
    finalize_safe_defaultable_gaps(ledger, goal=GOAL, provenance="test")
    seed = synthesize_seed_from_ledger(ledger)

    assert answer_provenance(answer) == "assumption"
    assert not any(entry.binds_seed for entry in ledger.sections["outputs"].entries)
    bound = [*seed.constraints, *(str(item) for item in seed.acceptance_criteria)]
    bound += [field.description for field in seed.ontology_schema.fields]
    assert not any("review only" in text for text in bound)
    assert seed.open_questions == (question,)
    assert not any(question in text for text in seed.assumptions)
