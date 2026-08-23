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
from ouroboros.cli.commands.init import SeedGenerationResult, _run_interview_loop
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

    final_state = (await _run_interview_loop(engine, state, scorer=scorer)).state

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
async def test_seed_approval_crosses_the_loop_boundary(prompts: list[str]) -> None:
    """Approving at the score-aware prompt must not be asked again outside.

    The loop returns the decision, not only the completed state, so the outer
    flow can start generation instead of re-asking "proceed to generate Seed
    specification?" — a second prompt would let the user decline the action
    they just approved.
    """
    state = _state_at_confirmation_round()
    scorer = FakeScorer(Result.ok(_score(0.15)))

    outcome = await _run_interview_loop(FakeEngine(), state, scorer=scorer)

    assert outcome.seed_generation_approved
    assert outcome.state.is_complete
    # Carried so generation reuses it instead of sampling a second score that
    # could disagree and drop the user into the "too ambiguous" menu.
    assert outcome.approved_score is not None
    assert outcome.approved_score.overall_score == 0.15


@pytest.mark.asyncio
async def test_stopping_questions_without_a_qualifying_score_is_not_seed_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Answering "no" to more questions ends the interview but approves nothing."""

    def fake_confirm(prompt: str, **_kwargs) -> bool:
        return False

    async def fake_prompt(_prompt: str) -> str:
        return "Deadlines are an optional card attribute."

    monkeypatch.setattr("ouroboros.cli.commands.init.Confirm.ask", fake_confirm)
    monkeypatch.setattr("ouroboros.cli.commands.init.multiline_prompt_async", fake_prompt)

    state = _state_at_confirmation_round()
    scorer = FakeScorer(Result.ok(_score(0.45, clarity=0.5)))

    outcome = await _run_interview_loop(FakeEngine(), state, scorer=scorer)

    assert outcome.state.is_complete
    assert not outcome.seed_generation_approved


@pytest.mark.asyncio
async def test_approved_score_starts_generation_without_a_second_score(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """One affirmative answer must actually start generation.

    Without carrying the approved score, generation samples an independent
    second score; a disagreeing sample drops the user into the "too ambiguous"
    menu immediately after they approved generation.
    """
    from unittest.mock import AsyncMock, MagicMock

    from ouroboros.cli.commands import init as init_module

    scorer_constructed = False

    def _scorer_factory(*_args, **_kwargs):
        nonlocal scorer_constructed
        scorer_constructed = True
        raise AssertionError("generation must not sample a second score")

    generator = MagicMock()
    generator.generate = AsyncMock(return_value=Result.err(ProviderError("stop here")))
    monkeypatch.setattr(init_module, "AmbiguityScorer", _scorer_factory)
    monkeypatch.setattr(init_module, "SeedGenerator", lambda *_args, **_kwargs: generator)

    engine = MagicMock()
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    state = _state_at_confirmation_round()

    seed_path, result = await init_module._generate_seed_from_interview(
        state,
        MagicMock(),
        engine=engine,
        approved_score=_score(0.15),
    )

    assert not scorer_constructed
    assert seed_path is None
    # Generation was reached (and failed on the stubbed generator), which is the
    # point: approval was not re-litigated by a second score.
    assert result == SeedGenerationResult.CANCELLED
    generator.generate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "expected"),
    [("3", SeedGenerationResult.CANCELLED), ("1", SeedGenerationResult.CONTINUE_INTERVIEW)],
)
async def test_failed_component_floor_blocks_generation_despite_low_overall(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    choice: str,
    expected: SeedGenerationResult,
) -> None:
    """A score the loop rejected must not walk into generation unforced.

    Overall 0.15 is under the threshold while every component floor fails. The
    loop asks "Continue with more questions?" for exactly this score, so the
    generation boundary must reach the same verdict instead of generating
    silently on the overall number alone.
    """
    from unittest.mock import AsyncMock, MagicMock

    from ouroboros.cli.commands import init as init_module

    generator = MagicMock()
    generator.generate = AsyncMock(side_effect=AssertionError("must not generate unforced"))
    monkeypatch.setattr(init_module, "SeedGenerator", lambda *_args, **_kwargs: generator)
    monkeypatch.setattr(init_module.Prompt, "ask", lambda *_args, **_kwargs: choice)

    warnings: list[str] = []
    monkeypatch.setattr(init_module, "print_warning", warnings.append)

    engine = MagicMock()
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))
    state = _state_at_confirmation_round()

    seed_path, result = await init_module._generate_seed_from_interview(
        state,
        MagicMock(),
        engine=engine,
        approved_score=_score(0.15, clarity=0.5),
    )

    assert seed_path is None
    assert result == expected
    generator.generate.assert_not_awaited()
    # The overall score is under the threshold, so naming it would read as a
    # contradiction. The blocked dimensions are the reason.
    assert warnings and "dimensions are still unclear" in warnings[0]
    assert "Goal Clarity" in warnings[0]


@pytest.mark.asyncio
async def test_failed_component_floor_forces_generation_when_user_overrides(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """Choosing "generate anyway" must actually force past the same gate."""
    from unittest.mock import AsyncMock, MagicMock

    from ouroboros.cli.commands import init as init_module

    generator = MagicMock()
    generator.generate = AsyncMock(return_value=Result.err(ProviderError("stop here")))
    monkeypatch.setattr(init_module, "SeedGenerator", lambda *_args, **_kwargs: generator)
    monkeypatch.setattr(init_module.Prompt, "ask", lambda *_args, **_kwargs: "2")
    monkeypatch.setattr(init_module, "print_warning", lambda *_args, **_kwargs: None)

    engine = MagicMock()
    engine.save_state = AsyncMock(return_value=Result.ok(tmp_path / "state.json"))

    await init_module._generate_seed_from_interview(
        _state_at_confirmation_round(),
        MagicMock(),
        engine=engine,
        approved_score=_score(0.15, clarity=0.5),
    )

    assert generator.generate.await_args.kwargs["force"] is True


def test_recording_an_answer_invalidates_the_previous_snapshot() -> None:
    """A durable save must never pair round N+1 with the score from round N.

    ``record_answer`` clears the snapshot where the inputs change, so an
    interruption between the answer's save and the rescore resumes with no
    score rather than a stale one that could activate the seed-closer.
    """
    state = _state_at_confirmation_round()
    state.store_ambiguity(score=0.18, breakdown={"from": "revision N"})

    state.record_answer("Q3", "A3")

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
