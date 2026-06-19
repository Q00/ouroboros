"""Tests for the AI answer refiner (``AutoInterviewDriver._refine_answer``).

The deterministic ``AutoAnswerer`` owns routing + safety; when an
``answer_refiner`` is wired, a *generic* CONSERVATIVE_DEFAULT / ASSUMPTION answer
is upgraded to a concrete, goal-specific one (the lever that drives interview
ambiguity down). These tests pin the refinement contract directly.

Matcher-independent → no ``_legacy_unsafe_bank`` opt-in.
"""

from __future__ import annotations

import pytest

from ouroboros.auto.answerer import AutoAnswer, AutoAnswerer, AutoAnswerSource
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

    async def refiner(goal: str, question: str, section: str, generic: str, committed=()) -> str:  # noqa: ARG001
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

    async def refiner(goal: str, question: str, section: str, generic: str, committed=()) -> str:  # noqa: ARG001
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
async def test_partial_section_refine_keeps_transcript_and_ledger_in_sync(tmp_path) -> None:
    """If one section's refiner call fails/blanks, that entry keeps its original
    value AND that original value stays in the rebuilt transcript. The text the
    backend acknowledges must describe the same committed content as every
    persisted ledger entry — a refined section must never leave its peer absent
    from the transcript (the desync invariant the refiner must preserve)."""

    async def refiner(  # noqa: ARG001
        goal: str, question: str, section: str, generic: str, committed=()
    ) -> str | None:
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

    # Ledger: refined where it succeeded, original where it failed.
    assert out.ledger_updates[0][1].value == "concrete::constraints"
    assert out.ledger_updates[1][1].value == "orig acceptance"
    # Transcript represents BOTH ledger entries — no desync.
    assert out.text == "concrete::constraints orig acceptance"
    assert "orig acceptance" in out.prefixed_text


@pytest.mark.asyncio
async def test_refined_transcript_covers_every_applied_ledger_entry(tmp_path) -> None:
    """Boundary invariant across the refine -> apply seam: the text the backend
    acknowledges — ``prefixed_text``, which the driver passes verbatim to
    ``backend.answer`` and which ``apply`` records into the ledger transcript —
    must contain every ledger entry value that ``apply`` persists, including a
    fallback (unrefined) section. This observes the transcript/backend boundary
    where a partial-refinement desync would actually surface, not just the
    helper's return shape."""

    async def refiner(  # noqa: ARG001
        goal: str, question: str, section: str, generic: str, committed=()
    ) -> str | None:
        # Partial: refine constraints, fail acceptance_criteria.
        if section == "constraints":
            return "Store todos in ~/.todo-cli.json; ids are 1-based list positions."
        return None

    def _entry(section: str, value: str) -> LedgerEntry:
        return LedgerEntry(
            key=f"{section}.x",
            value=value,
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        )

    answer = AutoAnswer(
        text="generic product behavior",
        source=AutoAnswerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        ledger_updates=[
            ("constraints", _entry("constraints", "Preserve the requested product behavior.")),
            (
                "acceptance_criteria",
                _entry("acceptance_criteria", "A command check exits 0 with evidence."),
            ),
        ],
    )

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="build a todo CLI", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal(state.goal)
    refined = await driver._refine_answer(answer, "Where should todos be stored?", state, ledger)

    # Apply persists ledger_updates and records prefixed_text into the transcript.
    AutoAnswerer().apply(refined, ledger, question="Where should todos be stored?")

    backend_text = refined.prefixed_text  # exactly what backend.answer(session_id, ...) receives
    for section, entry in refined.ledger_updates:
        assert entry.value in backend_text, f"{section} value absent from acknowledged transcript"


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


def _update(section: str, key: str, value: str) -> AutoAnswer:
    """An incoming generic answer that targets one explicit ``(section, key)``."""
    entry = LedgerEntry(
        key=key,
        value=value,
        source=LedgerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        status=LedgerStatus.DEFAULTED,
    )
    return AutoAnswer(
        text=value,
        source=AutoAnswerSource.CONSERVATIVE_DEFAULT,
        confidence=0.8,
        ledger_updates=[(section, entry)],
    )


@pytest.mark.asyncio
async def test_committed_facet_is_frozen_and_skips_refiner(tmp_path) -> None:
    """A facet already committed (active) earlier this interview is reused
    VERBATIM and the refiner is never consulted for it. This is the
    deterministic backstop that stops output-contract oscillation: a
    non-deterministic refiner can no longer re-decide a settled facet."""
    called: list = []

    async def refiner(*a) -> str:
        called.append(a)
        return "a DIFFERENT, contradictory contract"

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal("g")
    ledger.add_entry(
        "constraints",
        LedgerEntry(
            key="constraints.output_contract",
            value='todo add "X" prints exactly "Added #1: X\\n"',
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )

    out = await driver._refine_answer(
        _update("constraints", "constraints.output_contract", "generic placeholder"),
        "a rephrased output-format question",
        state,
        ledger,
    )

    assert out.ledger_updates[0][1].value == 'todo add "X" prints exactly "Added #1: X\\n"'
    assert called == []  # decided facet never reaches the model


@pytest.mark.asyncio
async def test_refiner_receives_committed_contract_anchor(tmp_path) -> None:
    """A genuinely NEW facet (unseen key) still goes to the refiner, but the
    refiner is handed the committed-contract snapshot so it can stay consistent
    with prior rounds instead of contradicting them."""
    seen: dict = {}

    async def refiner(goal, question, section, generic, committed=()) -> str:  # noqa: ARG001
        seen["committed"] = list(committed)
        return "concrete new facet"

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal("g")
    ledger.add_entry(
        "outputs",
        LedgerEntry(
            key="outputs.add",
            value="Added #1: X",
            source=LedgerSource.CONSERVATIVE_DEFAULT,
            confidence=0.8,
            status=LedgerStatus.DEFAULTED,
        ),
    )

    out = await driver._refine_answer(
        _update("constraints", "constraints.new_facet", "generic"),
        "q",
        state,
        ledger,
    )

    assert out.ledger_updates[0][1].value == "concrete new facet"
    assert ("outputs", "outputs.add", "Added #1: X") in seen["committed"]


@pytest.mark.asyncio
async def test_frozen_facet_prevents_conflict_under_nondeterministic_refiner(tmp_path) -> None:
    """End-to-end convergence: a refiner that drifts to a different contract on
    every call (modeling LLM non-determinism) cannot make the ledger CONFLICTING
    for an already-decided facet, because round 2 freezes it to round 1's value
    (matching-prior reconciliation clears any conflict)."""
    drift = iter(["Added #1: X", "added 1", "Added 1: x"])

    async def refiner(*_a) -> str:
        return next(drift)

    driver = _driver(tmp_path, refiner)
    state = AutoPipelineState(goal="g", cwd=str(tmp_path))
    ledger = SeedDraftLedger.from_goal("g")
    answerer = AutoAnswerer()

    # Round 1: facet undecided -> refiner concretizes it; apply commits it.
    r1 = await driver._refine_answer(
        _update("constraints", "constraints.output_contract", "generic placeholder"),
        "q1",
        state,
        ledger,
    )
    answerer.apply(r1, ledger, question="q1")
    v1 = r1.ledger_updates[0][1].value
    assert v1 == "Added #1: X"

    # Round 2: same facet re-asked (rephrased) -> frozen to v1, NOT the drift.
    r2 = await driver._refine_answer(
        _update("constraints", "constraints.output_contract", "generic placeholder"),
        "q2 rephrased differently",
        state,
        ledger,
    )
    answerer.apply(r2, ledger, question="q2 rephrased differently")

    assert r2.ledger_updates[0][1].value == v1  # did not drift to "added 1"
    assert ledger.sections["constraints"].status() != LedgerStatus.CONFLICTING
