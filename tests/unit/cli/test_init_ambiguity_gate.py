"""The CLI interview loop must ask the question its ambiguity score implies.

Before this gate existed the loop asked "Continue with more questions?" every
round without ever measuring ambiguity, so a seed-ready interview looked
identical to an unclear one and the stored snapshot that activates the
seed-closer perspective was never written.
"""

from __future__ import annotations

import pytest

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.cli.commands.init import _run_interview_loop
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result

_SEED_PROMPT = "[bold cyan]Ambiguity is low enough — proceed to Seed generation?[/]"
_MORE_QUESTIONS_PROMPT = "Continue with more questions?"


def _score(overall: float, *, clarity: float = 0.9) -> AmbiguityScore:
    def component(name: str) -> ComponentScore:
        return ComponentScore(
            name=name,
            clarity_score=clarity,
            weight=1 / 3,
            justification="test",
        )

    return AmbiguityScore(
        overall_score=overall,
        breakdown=ScoreBreakdown(
            goal_clarity=component("goal_clarity"),
            constraint_clarity=component("constraint_clarity"),
            success_criteria_clarity=component("success_criteria_clarity"),
        ),
    )


class FakeScorer:
    def __init__(self, result) -> None:
        self._result = result
        self.calls = 0

    async def score(self, state: InterviewState):
        self.calls += 1
        return self._result


class FakeEngine:
    """Engine stub that keeps the interview in progress until completed."""

    def __init__(self) -> None:
        self.completed = False

    async def ask_next_question(self, state: InterviewState):
        return Result.ok("What should it do?")

    async def record_response(
        self,
        state: InterviewState,
        user_response: str,
        question: str,
    ):
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=user_response,
            )
        )
        return Result.ok(state)

    async def save_state(self, state: InterviewState):
        return Result.ok(None)

    async def complete_interview(self, state: InterviewState):
        self.completed = True
        state.status = InterviewStatus.COMPLETED
        return Result.ok(state)


def _state_at_confirmation_round() -> InterviewState:
    """State with enough answered rounds that the loop reaches the prompt."""
    state = InterviewState(interview_id="interview_gate")
    for round_number in range(1, 3):
        state.rounds.append(
            InterviewRound(
                round_number=round_number,
                question=f"Q{round_number}",
                user_response=f"A{round_number}",
            )
        )
    return state


@pytest.fixture
def prompts(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every confirmation prompt, answering the one that ends the loop."""
    asked: list[str] = []

    def fake_confirm(prompt: str, **_kwargs) -> bool:
        asked.append(prompt)
        # "proceed to Seed?" -> yes; "more questions?" -> no. Either answer
        # completes the interview, so the loop terminates in one round.
        return prompt == _SEED_PROMPT

    async def fake_prompt(_prompt: str) -> str:
        return "Deadlines are an optional card attribute."

    monkeypatch.setattr("ouroboros.cli.commands.init.Confirm.ask", fake_confirm)
    monkeypatch.setattr("ouroboros.cli.commands.init.multiline_prompt_async", fake_prompt)
    return asked


@pytest.mark.asyncio
async def test_seed_ready_score_offers_seed_instead_of_more_questions(
    prompts: list[str],
) -> None:
    state = _state_at_confirmation_round()
    engine = FakeEngine()
    scorer = FakeScorer(Result.ok(_score(0.15)))

    final_state = await _run_interview_loop(engine, state, scorer=scorer)

    assert scorer.calls == 1
    assert prompts == [_SEED_PROMPT]
    assert engine.completed
    assert final_state.is_complete
    assert final_state.ambiguity_score == 0.15


@pytest.mark.asyncio
async def test_high_score_keeps_the_more_questions_prompt_and_stores_snapshot(
    prompts: list[str],
) -> None:
    state = _state_at_confirmation_round()
    engine = FakeEngine()
    scorer = FakeScorer(Result.ok(_score(0.45, clarity=0.5)))

    await _run_interview_loop(engine, state, scorer=scorer)

    assert scorer.calls == 1
    assert prompts == [_MORE_QUESTIONS_PROMPT]
    # The stored snapshot is what activates the seed-closer perspective and the
    # ambiguity block in the next round's question prompt.
    assert state.ambiguity_score == 0.45


@pytest.mark.asyncio
async def test_component_floor_failure_does_not_offer_seed(prompts: list[str]) -> None:
    """A low overall score with a weak component is not seed-ready."""
    state = _state_at_confirmation_round()
    engine = FakeEngine()
    scorer = FakeScorer(Result.ok(_score(0.15, clarity=0.5)))

    await _run_interview_loop(engine, state, scorer=scorer)

    assert prompts == [_MORE_QUESTIONS_PROMPT]


@pytest.mark.asyncio
async def test_scoring_failure_clears_snapshot_and_falls_back(prompts: list[str]) -> None:
    state = _state_at_confirmation_round()
    state.store_ambiguity(score=0.1, breakdown={"stale": True})
    engine = FakeEngine()
    scorer = FakeScorer(Result.err(ProviderError("scorer unavailable")))

    await _run_interview_loop(engine, state, scorer=scorer)

    assert scorer.calls == 1
    assert prompts == [_MORE_QUESTIONS_PROMPT]
    # A stale snapshot must not survive a failed rescore.
    assert state.ambiguity_score is None
    assert state.ambiguity_breakdown is None


@pytest.mark.asyncio
async def test_no_scorer_keeps_legacy_prompt(prompts: list[str]) -> None:
    """Callers without a scorer (existing tests, embedders) are unaffected."""
    state = _state_at_confirmation_round()
    engine = FakeEngine()

    await _run_interview_loop(engine, state)

    assert prompts == [_MORE_QUESTIONS_PROMPT]
    assert state.ambiguity_score is None
