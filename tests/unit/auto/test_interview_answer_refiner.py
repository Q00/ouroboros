"""Tests for the AI answer refiner (``AutoInterviewDriver._refine_answer``).

The deterministic ``AutoAnswerer`` owns routing + safety; when an
``answer_refiner`` is wired, a *generic* CONSERVATIVE_DEFAULT / ASSUMPTION answer
is upgraded to a concrete, goal-specific one (the lever that drives interview
ambiguity down). These tests pin the refinement contract directly.

Matcher-independent → no ``_legacy_unsafe_bank`` opt-in.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerSource
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.state import AutoPipelineState, AutoStore

_CONCRETE = "Treat every CSV cell as a string; a missing file writes to stderr and exits 1."


def _driver(tmp_path, refiner):
    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("q", "s")

    async def answer(session_id: str, text: str, *, last_question=None) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("q", session_id)

    return AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=1,
        answer_refiner=refiner,
    )


def _answer(
    source: AutoAnswerSource, *, text: str = "generic placeholder", section: str = "constraints"
) -> AutoAnswer:
    entry = LedgerEntry(
        key=f"{section}.x",
        value=text,
        source=LedgerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        status=LedgerStatus.DEFAULTED,
    )
    return AutoAnswer(text=text, source=source, confidence=0.8, ledger_updates=[(section, entry)])


@pytest.mark.asyncio
async def test_refines_generic_answer_into_ledger_and_transcript(tmp_path) -> None:
    calls: list[tuple] = []

    async def refiner(goal: str, question: str, section: str, generic: str) -> str:
        calls.append((goal, question, section, generic))
        return _CONCRETE

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="Build a CSV to JSON CLI", cwd=str(tmp_path))
    out = await driver._refine_answer(
        _answer(AutoAnswerSource.CONSERVATIVE_DEFAULT),
        "What are the constraints?",
        state,
        SeedDraftLedger.from_goal(state.goal),
    )

    # Concrete text replaces the generic in BOTH the transcript text and the ledger value.
    assert out.text == _CONCRETE
    assert out.ledger_updates[0][1].value == _CONCRETE
    assert _CONCRETE in out.prefixed_text
    # Source/structure preserved (safety routing untouched).
    assert out.source == AutoAnswerSource.CONSERVATIVE_DEFAULT
    # Refiner saw the goal + section + generic placeholder.
    assert calls and calls[0][0] == "Build a CSV to JSON CLI" and calls[0][2] == "constraints"


@pytest.mark.asyncio
async def test_assumption_source_is_also_refined(tmp_path) -> None:
    async def refiner(*_a) -> str:
        return _CONCRETE

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    out = await driver._refine_answer(
        _answer(AutoAnswerSource.ASSUMPTION), "q", state, SeedDraftLedger.from_goal("g")
    )
    assert out.text == _CONCRETE


@pytest.mark.asyncio
async def test_multi_section_answer_is_refined_per_section(tmp_path) -> None:
    """A multi-ledger-update answer is refined PER SECTION. The refiner is
    section-aware, so each entry gets its own concrete value and the transcript
    stays in sync with the ledger. This is the common case (every real
    ``AutoAnswerer`` route emits >=2 updates); skipping it left the refiner inert
    and interview ambiguity unconverged."""

    calls: list[str] = []

    async def refiner(goal: str, question: str, section: str, generic: str) -> str:  # noqa: ARG001
        calls.append(section)
        return f"concrete::{section}"

    def _entry(section: str, value: str) -> LedgerEntry:
        return LedgerEntry(
            key=f"{section}.x",
            value=value,
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        )

    multi = AutoAnswer(
        text="generic verification text",
        source=AutoAnswerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        ledger_updates=[
            ("verification_plan", _entry("verification_plan", "run tests")),
            ("acceptance_criteria", _entry("acceptance_criteria", "command prints output")),
        ],
    )

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    out = await driver._refine_answer(multi, "q", state, SeedDraftLedger.from_goal("g"))

    # Each section refined with its own concrete value; transcript rebuilt from them.
    assert calls == ["verification_plan", "acceptance_criteria"]
    assert out.ledger_updates[0][1].value == "concrete::verification_plan"
    assert out.ledger_updates[1][1].value == "concrete::acceptance_criteria"
    assert out.text == "concrete::verification_plan concrete::acceptance_criteria"


@pytest.mark.asyncio
async def test_partial_section_refine_keeps_failed_section_in_sync(tmp_path) -> None:
    """If one section's refiner call fails/blanks, that entry keeps its original
    value while the others are upgraded — never a desync."""

    async def refiner(goal: str, question: str, section: str, generic: str) -> str | None:  # noqa: ARG001
        return "concrete::constraints" if section == "constraints" else None

    def _entry(section: str, value: str) -> LedgerEntry:
        return LedgerEntry(
            key=f"{section}.x",
            value=value,
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        )

    multi = AutoAnswer(
        text="generic",
        source=AutoAnswerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        ledger_updates=[
            ("constraints", _entry("constraints", "orig constraints")),
            ("acceptance_criteria", _entry("acceptance_criteria", "orig acceptance")),
        ],
    )

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    out = await driver._refine_answer(multi, "q", state, SeedDraftLedger.from_goal("g"))

    assert out.ledger_updates[0][1].value == "concrete::constraints"
    assert out.ledger_updates[1][1].value == "orig acceptance"  # untouched, still in sync
    assert out.text == "concrete::constraints"


@pytest.mark.asyncio
async def test_non_generic_answer_is_not_refined(tmp_path) -> None:
    async def refiner(*_a) -> str:
        raise AssertionError("must not refine a grounded answer")

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    out = await driver._refine_answer(
        _answer(AutoAnswerSource.REPO_FACT, text="grounded fact"),
        "q",
        state,
        SeedDraftLedger.from_goal("g"),
    )
    assert out.text == "grounded fact"


@pytest.mark.asyncio
async def test_no_refiner_returns_original(tmp_path) -> None:
    driver = _driver(tmp_path, None)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    out = await driver._refine_answer(
        _answer(AutoAnswerSource.CONSERVATIVE_DEFAULT, text="orig"),
        "q",
        state,
        SeedDraftLedger.from_goal("g"),
    )
    assert out.text == "orig"


@pytest.mark.asyncio
async def test_refiner_failure_degrades_to_original(tmp_path) -> None:
    async def refiner(*_a) -> str:
        raise RuntimeError("provider down")

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    out = await driver._refine_answer(
        _answer(AutoAnswerSource.CONSERVATIVE_DEFAULT, text="orig"),
        "q",
        state,
        SeedDraftLedger.from_goal("g"),
    )
    assert out.text == "orig"


@pytest.mark.asyncio
async def test_empty_refiner_output_keeps_original(tmp_path) -> None:
    async def refiner(*_a) -> str:
        return "   "

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    out = await driver._refine_answer(
        _answer(AutoAnswerSource.CONSERVATIVE_DEFAULT, text="orig"),
        "q",
        state,
        SeedDraftLedger.from_goal("g"),
    )
    assert out.text == "orig"
