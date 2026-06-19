"""Behaviour tests for stagnation-driven lateral concretization (interview).

When the auto interview ambiguity plateaus above the closure threshold and a
``lateral_thinker`` is wired, the driver invokes a lateral persona to force one
concrete decision and pushes it into the transcript so the next round's scorer
reflects the resolved gap (see
``AutoInterviewDriver._maybe_concretize_on_stagnation``). This module pins:

1. A plateau triggers exactly one lateral concretization after the patience
   window, the decision is pushed through the backend, and the now-lower
   ambiguity closes the interview.
2. No ``lateral_thinker`` → no concretization (pre-feature behaviour preserved).
3. Steadily improving ambiguity never triggers a concretization.

These tests are matcher-independent, so they intentionally do NOT opt into the
``_legacy_unsafe_bank`` fixture.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from ouroboros.auto.adapters import LateralResult
from ouroboros.auto.interview_driver import (
    AutoInterviewDriver,
    FunctionInterviewBackend,
    InterviewTurn,
)
from ouroboros.auto.ledger import LedgerEntry, LedgerSource, LedgerStatus, SeedDraftLedger
from ouroboros.auto.state import AutoPipelineState, AutoStore
from ouroboros.resilience.lateral import ThinkingPersona

_STAGNATION_MARKER = "[stagnation-concretization"


def _ready_ledger(goal: str = "Build a CSV to JSON CLI") -> SeedDraftLedger:
    """A structurally complete ledger so closure hinges only on backend ambiguity."""
    ledger = SeedDraftLedger.from_goal(goal)
    for section, value in {
        "actors": "Single local CLI user",
        "inputs": "Command-line arguments",
        "outputs": "Stable stdout and a JSON file",
        "constraints": "Use existing project patterns; no new dependencies",
        "non_goals": "No cloud sync",
        "acceptance_criteria": "Command prints stable output; bad input exits non-zero",
        "verification_plan": "Run command-level tests",
        "failure_modes": "Invalid input exits non-zero",
        "runtime_context": "Existing repository runtime",
    }.items():
        source = (
            LedgerSource.NON_GOAL if section == "non_goals" else LedgerSource.CONSERVATIVE_DEFAULT
        )
        ledger.add_entry(
            section,
            LedgerEntry(
                key=f"{section}.test",
                value=value,
                source=source,
                confidence=0.85,
                status=LedgerStatus.DEFAULTED,
            ),
        )
    return ledger


def _plateau_backend(*, converge_on_concretization: bool) -> FunctionInterviewBackend:
    """Backend whose ambiguity stays at 0.50 until a concretization push arrives."""

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else?", "interview_plateau", ambiguity_score=0.50)

    async def answer(
        session_id: str, text: str, *, last_question: str | None = None
    ) -> InterviewTurn:  # noqa: ARG001
        if converge_on_concretization and _STAGNATION_MARKER in text:
            return InterviewTurn(
                "done", session_id, seed_ready=True, completed=True, ambiguity_score=0.15
            )
        return InterviewTurn("What else?", session_id, seed_ready=False, ambiguity_score=0.50)

    return FunctionInterviewBackend(start, answer)


def _recording_lateral_thinker() -> tuple[object, list[dict]]:
    calls: list[dict] = []

    async def _thinker(
        *,
        persona: ThinkingPersona,
        qa_differences: Sequence[str],
        qa_suggestions: Sequence[str],
        run_artifact: str,
    ) -> LateralResult:
        calls.append(
            {
                "persona": persona,
                "differences": tuple(qa_differences),
                "suggestions": tuple(qa_suggestions),
                "artifact": run_artifact,
            }
        )
        return LateralResult(
            persona=persona.value,
            approach_summary="commit one concrete parsing contract",
            text="Treat every CSV cell as a string and error on column-count mismatch.",
        )

    return _thinker, calls


@pytest.mark.asyncio
async def test_stagnation_triggers_lateral_concretization_and_converges(tmp_path) -> None:
    ledger = _ready_ledger()
    state = AutoPipelineState(goal="Build a CSV to JSON CLI", cwd=str(tmp_path))
    thinker, calls = _recording_lateral_thinker()
    driver = AutoInterviewDriver(
        _plateau_backend(converge_on_concretization=True),
        store=AutoStore(tmp_path),
        max_rounds=10,
        timeout_seconds=5,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    # The concretization lowered ambiguity below threshold → interview closes.
    assert result.status == "seed_ready"
    # Exactly one lateral concretization fired (after the patience window).
    assert len(calls) == 1
    # The plateau signal was passed to the persona.
    assert any("plateaued" in d for d in calls[0]["differences"])
    assert calls[0]["artifact"].strip()
    # The persona was recorded for chain progression / resume.
    assert state.personas_invoked
    assert state.last_lateral_text


@pytest.mark.asyncio
async def test_no_lateral_thinker_means_no_concretization(tmp_path) -> None:
    ledger = _ready_ledger()
    state = AutoPipelineState(goal="Build a CSV to JSON CLI", cwd=str(tmp_path))
    driver = AutoInterviewDriver(
        _plateau_backend(converge_on_concretization=True),
        store=AutoStore(tmp_path),
        max_rounds=4,
        timeout_seconds=5,
        # lateral_thinker omitted → None
    )

    result = await driver.run(state, ledger)

    # Without a lateral thinker the plateau never converges; the bounded loop
    # falls through to max_rounds closure (safe-default), not mutual agreement.
    assert result.status in {"seed_ready", "blocked"}
    assert state.interview_closure_mode != "mutual_agreement"
    assert not state.personas_invoked
    assert not state.last_lateral_text


@pytest.mark.asyncio
async def test_stagnation_interventions_are_capped(tmp_path) -> None:
    """A plateau that never converges still bounds concretizations to the cap.

    Repeated independent concretizations can pile up contradictory decisions, so
    interventions are capped (``_STAGNATION_MAX_INTERVENTIONS``). After the cap
    the bounded loop falls through to max_rounds closure instead of intervening
    every subsequent stagnant round.
    """
    from ouroboros.auto.interview_driver import _STAGNATION_MAX_INTERVENTIONS

    ledger = _ready_ledger()
    state = AutoPipelineState(goal="Build a CSV to JSON CLI", cwd=str(tmp_path))
    thinker, calls = _recording_lateral_thinker()
    driver = AutoInterviewDriver(
        # Ambiguity stays at 0.50 forever — concretization never lowers it.
        _plateau_backend(converge_on_concretization=False),
        store=AutoStore(tmp_path),
        max_rounds=20,
        timeout_seconds=5,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    # The loop terminated (no infinite intervention) and interventions are capped.
    assert result.status in {"seed_ready", "blocked"}
    assert len(calls) <= _STAGNATION_MAX_INTERVENTIONS
    assert len(state.personas_invoked) <= _STAGNATION_MAX_INTERVENTIONS


@pytest.mark.asyncio
async def test_improving_ambiguity_never_triggers_concretization(tmp_path) -> None:
    ledger = _ready_ledger()
    state = AutoPipelineState(goal="Build a CSV to JSON CLI", cwd=str(tmp_path))
    thinker, calls = _recording_lateral_thinker()

    scores = iter([0.50, 0.40, 0.30, 0.18])

    async def start(goal: str, cwd: str) -> InterviewTurn:  # noqa: ARG001
        return InterviewTurn("What else?", "interview_improving", ambiguity_score=0.55)

    async def answer(
        session_id: str, text: str, *, last_question: str | None = None
    ) -> InterviewTurn:  # noqa: ARG001
        score = next(scores, 0.15)
        seed_ready = score <= 0.20
        return InterviewTurn(
            "What else?",
            session_id,
            seed_ready=seed_ready,
            completed=seed_ready,
            ambiguity_score=score,
        )

    driver = AutoInterviewDriver(
        FunctionInterviewBackend(start, answer),
        store=AutoStore(tmp_path),
        max_rounds=10,
        timeout_seconds=5,
        lateral_thinker=thinker,
    )

    result = await driver.run(state, ledger)

    assert result.status == "seed_ready"
    # Ambiguity improved every round, so no stagnation intervention was needed.
    assert calls == []
