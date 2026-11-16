"""A refused explicit close must keep the real pending question pending.

When the user says "done" while the closure gate is not met, the server
refuses with a "Cannot complete yet" (or stability-check) message. The console
(``gui/harness.py`` through ``HandlerInterviewBackend``) and text hosts treat a
non-error response's text as the next question and echo it back as
``last_question``. The next real answer must still be recorded against the
question the server issued, not against the refusal text.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.auto.adapters import HandlerInterviewBackend
from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.interview_refused_close import echo_carries_pending
from ouroboros.providers.base import CompletionResponse, UsageInfo

FIRST_QUESTION = "Which tasks should the tool track first?"
NEXT_QUESTION = "How should finished tasks be shown?"
ANSWER = "Personal chores and errands"
SCORER = "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer"


def _score(value: float) -> AmbiguityScore:
    clarity = 1.0 - value
    return AmbiguityScore(
        overall_score=value,
        breakdown=ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity", clarity_score=clarity, weight=0.4, justification="g"
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity", clarity_score=clarity, weight=0.3, justification="c"
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=clarity,
                weight=0.3,
                justification="s",
            ),
        ),
    )


def _adapter(*questions: str) -> MagicMock:
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        side_effect=[
            Result.ok(
                CompletionResponse(
                    content=text,
                    model="test",
                    usage=UsageInfo(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                )
            )
            for text in questions
        ]
    )
    return adapter


def _wire(tmp_path, *questions: str) -> tuple[InterviewEngine, InterviewHandler]:
    adapter = _adapter(*(questions or (FIRST_QUESTION, NEXT_QUESTION)))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    handler = InterviewHandler(interview_engine=engine, llm_adapter=adapter, data_dir=tmp_path)
    handler._create_interview_engine = lambda: (engine, adapter)
    handler._emit_event = AsyncMock()
    return engine, handler


def _scorer(score: AmbiguityScore) -> MagicMock:
    scorer = MagicMock()
    scorer.score = AsyncMock(return_value=Result.ok(score))
    return scorer


async def _first_round(engine: InterviewEngine, session_id: str) -> tuple[str, str | None]:
    state = (await engine.load_state(session_id)).value
    return state.rounds[0].question, state.rounds[0].user_response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("score", "refusal_head"),
    [(0.55, "Cannot complete yet"), (0.14, "Ambiguity looks low")],
    ids=["ambiguity_gate", "stability_shortfall"],
)
async def test_console_answer_after_refused_done_is_recorded_against_pending_question(
    tmp_path, score: float, refusal_head: str
) -> None:
    """Drive the handler the way ``gui/harness.py`` does: echo ``turn.question``."""
    engine, handler = _wire(tmp_path)
    backend = HandlerInterviewBackend(handler, cwd=str(tmp_path))

    with patch(SCORER, return_value=_scorer(_score(score))):
        started = await backend.start("Build a small todo CLI", cwd=str(tmp_path))
        assert started.question.endswith(FIRST_QUESTION)

        refused = await backend.answer(started.session_id, "done", last_question=started.question)
        # The explanation is still shown, followed by the question still pending.
        assert refused.question.startswith(refusal_head)
        assert refused.question.endswith(f"\n\n{FIRST_QUESTION}")
        assert not refused.completed
        assert await _first_round(engine, started.session_id) == (FIRST_QUESTION, None)

        # The harness stores ``refused.question`` as ``last_question``.
        answered = await backend.answer(started.session_id, ANSWER, last_question=refused.question)

    assert answered.question.endswith(NEXT_QUESTION)
    assert await _first_round(engine, started.session_id) == (FIRST_QUESTION, ANSWER)


@pytest.mark.asyncio
@pytest.mark.parametrize("echo", ["whole_response", "question_only", "omitted"])
async def test_host_answer_after_refused_closure_is_recorded_against_pending_question(
    tmp_path, echo: str
) -> None:
    """A text host may echo the whole refusal, the question alone, or nothing."""
    engine, handler = _wire(tmp_path)

    with patch(SCORER, return_value=_scorer(_score(0.55))):
        started = await handler.handle({"initial_context": "Build a small todo CLI"})
        session_id = started.value.meta["session_id"]
        refused = await handler.handle(
            {"session_id": session_id, "answer": "[from-user][refined][closure] Stop here."}
        )
        text = refused.value.content[0].text
        echoes = {"whole_response": text, "question_only": FIRST_QUESTION, "omitted": None}
        arguments = {"session_id": session_id, "answer": f"[from-user] {ANSWER}"}
        if echoes[echo] is not None:
            arguments["last_question"] = echoes[echo]
        answered = await handler.handle(arguments)

    meta = refused.value.meta
    assert not refused.value.is_error
    assert text.startswith("Cannot complete yet")
    assert text.endswith(f"\n\n{FIRST_QUESTION}")
    assert meta["session_id"] == session_id
    assert meta["seed_ready"] is False
    assert meta["ambiguity_score"] == 0.55
    assert meta["interview_reasoning"]["phase"] == "completion_gate"
    assert meta["interview_reasoning"]["pending_question"] is True
    assert answered.is_ok
    assert await _first_round(engine, session_id) == (FIRST_QUESTION, f"[from-user] {ANSWER}")


@pytest.mark.asyncio
async def test_resume_after_refused_done_presents_the_issued_question(tmp_path) -> None:
    """Persisted state after a refusal resumes on the issued question, not the refusal."""
    engine, handler = _wire(tmp_path, FIRST_QUESTION)

    with patch(SCORER, return_value=_scorer(_score(0.55))):
        started = await handler.handle({"initial_context": "Build a small todo CLI"})
        session_id = started.value.meta["session_id"]
        await handler.handle({"session_id": session_id, "answer": "done"})

        # A fresh server process reads the same state directory.
        resumed_engine, resumed_handler = _wire(tmp_path, NEXT_QUESTION)
        resumed = await resumed_handler.handle({"session_id": session_id})
        resumed_text = resumed.value.content[0].text
        answered = await resumed_handler.handle({"session_id": session_id, "answer": ANSWER})

    assert resumed_text.startswith(f"Session {session_id}\n\n(ambiguity: 0.55) {FIRST_QUESTION}")
    assert "Cannot complete yet" not in resumed_text
    assert resumed.value.meta["interview_reasoning"]["phase"] == "resume_pending"
    assert answered.is_ok
    assert await _first_round(resumed_engine, session_id) == (FIRST_QUESTION, ANSWER)


def test_echo_carries_pending_only_for_a_re_presentation() -> None:
    refusal = f"Cannot complete yet — reason. Current ambiguity: 0.55.\n\n{FIRST_QUESTION}"
    assert echo_carries_pending(refusal, FIRST_QUESTION)
    assert echo_carries_pending(f"  {refusal}\n", f"{FIRST_QUESTION} ")
    # A question alone, an ambiguity-prefixed echo and a repair keep their handling.
    assert not echo_carries_pending(FIRST_QUESTION, FIRST_QUESTION)
    assert not echo_carries_pending(f"(ambiguity: 0.55) {FIRST_QUESTION}", FIRST_QUESTION)
    assert not echo_carries_pending("What is the real repaired question?", FIRST_QUESTION)
    assert not echo_carries_pending(f"{FIRST_QUESTION}\n\nMore context.", FIRST_QUESTION)
    assert not echo_carries_pending(None, FIRST_QUESTION)
    assert not echo_carries_pending("Anything\n\n", "")
