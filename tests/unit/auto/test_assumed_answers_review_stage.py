"""Auto's assumed answers do not change the Seed review, grade or Seed QA repair (#3059).

The answers auto chose in its interview now reach the worker as
``Seed.sourced_scope.assumptions`` (see ``test_assumed_answers_reach_worker.py``).
They never bind, so the deterministic grade and review read nothing new: the
same ledger and Seed grade, score and find exactly as when the questions were
kept open, and a Seed QA repair instruction never lands in them.

These checks touch only the review stage, kept apart so a change to that stage
does not touch the worker-path tests.
"""

from __future__ import annotations

from typing import Any

import pytest

from ouroboros.auto.grading import GradeGate
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.ledger_seed import synthesize_seed_from_ledger, with_ledger_carryover
from ouroboros.auto.safe_defaults import finalize_safe_defaultable_gaps
from ouroboros.auto.seed_qa_repair import contains_seed_qa_repair_instruction
from ouroboros.auto.seed_reviewer import SeedReviewer
from ouroboros.core.seed import Seed

GOAL = "Build a particle demo web app with sliders for speed and particle count"
SPEED_QUESTION = "What range and default should the speed slider have?"
SPEED = "The speed slider spans 0.1 to 5 with default 1"
SPEED_FAILURE = "A speed outside 0.1-5 is clamped to the nearest end"
KO_GALAXY_GOAL = "아름다운 3D 은하를 만들어 주세요"
SCOPE_QUESTION = "구현 범위는 어디까지로 할까요?"


def _entry(key: str, value: str, question: str, *, refined: bool = True) -> LedgerEntry:
    return LedgerEntry(
        key=key,
        value=value,
        source=LedgerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        status=LedgerStatus.DEFAULTED,
        question=question,
        refined=refined,
    )


def _ledger(goal: str, answers: tuple[tuple[str, str], ...], question: str) -> SeedDraftLedger:
    """A ledger holding refined answers to one interviewer question, gaps closed."""
    ledger = SeedDraftLedger.from_goal(goal)
    for section, value in answers:
        ledger.add_entry(section, _entry(f"{section}.k.investigated.1", value, question))
    finalize_safe_defaultable_gaps(ledger, goal=goal, provenance="test: close the other gaps")
    return ledger


def _slider_ledger() -> SeedDraftLedger:
    return _ledger(GOAL, (("constraints", SPEED), ("failure_modes", SPEED_FAILURE)), SPEED_QUESTION)


def _galaxy_ledger() -> SeedDraftLedger:
    """Scope-shrinking refined defaults, shaped like the #3061 A/B run's."""
    return _ledger(
        KO_GALAXY_GOAL,
        (
            ("constraints", "최소 구현으로 제한"),
            ("failure_modes", "MVP 범위를 넘는 기능 추가"),
            ("non_goals", "배경 별과 성운은 범위 밖"),
        ),
        SCOPE_QUESTION,
    )


def _without_assumed_answers(ledger: SeedDraftLedger) -> SeedDraftLedger:
    """The same ledger routed as before #3059: no entry counts as refined."""
    copy = SeedDraftLedger.from_dict(ledger.to_dict())
    for section in copy.sections.values():
        for entry in section.entries:
            entry.refined = False
    return copy


def _generated_seed(goal: str) -> Seed:
    return Seed.from_dict(
        {
            "goal": goal,
            "acceptance_criteria": [
                "`npm test` exits 0 and its output shows both sliders change the animation."
            ],
            "ontology_schema": {"name": "Demo", "description": "d"},
            "metadata": {"ambiguity_score": 0.1},
        }
    )


def _seeds(ledger: SeedDraftLedger, goal: str) -> tuple[Seed, Seed]:
    """The generator's Seed and the ledger Seed, as the pipeline records them."""
    return (
        with_ledger_carryover(_generated_seed(goal), ledger),
        with_ledger_carryover(synthesize_seed_from_ledger(ledger), ledger),
    )


@pytest.mark.parametrize(
    ("ledger_factory", "goal"), [(_slider_ledger, GOAL), (_galaxy_ledger, KO_GALAXY_GOAL)]
)
def test_grading_is_unchanged_by_the_assumed_answers(ledger_factory: Any, goal: str) -> None:
    ledger = ledger_factory()
    before = _without_assumed_answers(ledger)
    gate = GradeGate()
    assert gate.grade_ledger(ledger) == gate.grade_ledger(before)

    for after_seed, before_seed in zip(_seeds(ledger, goal), _seeds(before, goal), strict=True):
        assert after_seed.sourced_scope is not None and after_seed.sourced_scope.assumptions
        assert before_seed.sourced_scope is not None and not before_seed.sourced_scope.assumptions
        after = SeedReviewer(gate).review(after_seed, ledger=ledger, closure_mode="ledger_only")
        prior = SeedReviewer(gate).review(before_seed, ledger=before, closure_mode="ledger_only")
        assert after.grade_result.grade == prior.grade_result.grade
        assert after.grade_result.may_run == prior.grade_result.may_run
        assert after.grade_result.scores == prior.grade_result.scores
        assert [finding.fingerprint for finding in after.findings] == [
            finding.fingerprint for finding in prior.findings
        ]
    # The ledger Seed still reaches a runnable grade with its assumptions on board.
    assert after.may_run


@pytest.mark.parametrize("ledger_factory", [_slider_ledger, _galaxy_ledger])
def test_no_seed_qa_repair_instruction_reaches_constraints_or_assumptions(
    ledger_factory: Any,
) -> None:
    ledger = ledger_factory()
    for seed in _seeds(ledger, GOAL):
        assert seed.sourced_scope is not None
        for text in (
            *seed.constraints,
            *seed.sourced_scope.constraints,
            *seed.sourced_scope.assumptions,
        ):
            assert not contains_seed_qa_repair_instruction(text)
