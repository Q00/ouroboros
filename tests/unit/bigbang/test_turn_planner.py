"""Tests for atomic interview turn planning."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from ouroboros.bigbang.ambiguity import AmbiguityScorer
from ouroboros.bigbang.interview import InterviewEngine, InterviewRound, InterviewState
from ouroboros.bigbang.turn_planner import InterviewTurnPlanner
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.providers.base import CompletionResponse, UsageInfo


def _response(payload: dict[str, object]) -> CompletionResponse:
    return CompletionResponse(
        content=json.dumps(payload),
        model="test-model",
        usage=UsageInfo(prompt_tokens=10, completion_tokens=10, total_tokens=20),
        finish_reason="stop",
    )


def _payload() -> dict[str, object]:
    return {
        "next_question": "What observable result proves this is complete?",
        "goal_clarity_score": 0.8,
        "goal_clarity_justification": "The goal names the user outcome.",
        "constraint_clarity_score": 0.7,
        "constraint_clarity_justification": "Core constraints are explicit.",
        "success_criteria_clarity_score": 0.6,
        "success_criteria_clarity_justification": "Verification needs one more decision.",
    }


def _state() -> InterviewState:
    return InterviewState(
        interview_id="interview_turn_plan_1",
        initial_context="Build a report generator",
        rounds=[
            InterviewRound(round_number=1, question="Who uses it?", user_response="Analysts"),
            InterviewRound(round_number=2, question="What output?", user_response="A CSV report"),
            InterviewRound(
                round_number=3,
                question="What is constrained?",
                user_response="No network calls",
            ),
        ],
    )


async def test_plan_returns_question_and_score_from_one_completion(tmp_path) -> None:
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(_response(_payload())))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    planner = InterviewTurnPlanner(
        engine=engine,
        scorer=AmbiguityScorer(llm_adapter=adapter, model="test-model"),
    )
    result = await planner.plan(_state())

    assert result.is_ok
    assert result.value.question == "What observable result proves this is complete?"
    assert result.value.ambiguity is not None
    assert result.value.ambiguity.overall_score == 0.29
    adapter.complete.assert_awaited_once()
    config = adapter.complete.call_args.args[1]
    assert config.response_format is None


async def test_plan_keeps_question_context_and_scoring_view_separate(tmp_path) -> None:
    adapter = MagicMock()
    payload = {**_payload(), "category": "planning"}
    adapter.complete = AsyncMock(return_value=Result.ok(_response(payload)))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")

    planner = InterviewTurnPlanner(
        engine=engine,
        scorer=AmbiguityScorer(llm_adapter=adapter, model="test-model"),
    )
    score_view = _state().model_copy(deep=True)
    score_view.rounds[-1].user_response = None

    result = await planner.plan(
        _state(),
        scoring_state=score_view,
        extra_response_contract="Also include category for PM routing.",
    )

    assert result.is_ok
    assert result.value.raw_payload["category"] == "planning"
    messages = adapter.complete.call_args.args[0]
    assert "Also include category for PM routing." in messages[0].content
    assert "No network calls" in "\n".join(message.content for message in messages)
    assert "answers in rounds 3 are observations" in messages[0].content
    assert "do not count them as resolved requirements" in messages[0].content


async def test_plan_fails_recoverably_when_question_is_missing(tmp_path) -> None:
    adapter = MagicMock()
    payload = _payload()
    payload.pop("next_question")
    adapter.complete = AsyncMock(return_value=Result.ok(_response(payload)))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")

    planner = InterviewTurnPlanner(
        engine=engine,
        scorer=AmbiguityScorer(llm_adapter=adapter, model="test-model"),
    )
    result = await planner.plan(_state())

    assert result.is_err
    assert "missing next_question" in str(result.error)


async def test_plan_returns_question_when_score_fields_are_invalid(tmp_path) -> None:
    adapter = MagicMock()
    adapter.complete = AsyncMock(
        return_value=Result.ok(_response({"next_question": "Which constraint matters most?"}))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    planner = InterviewTurnPlanner(
        engine=engine,
        scorer=AmbiguityScorer(llm_adapter=adapter, model="test-model"),
    )

    result = await planner.plan(_state())

    assert result.is_ok
    assert result.value.question == "Which constraint matters most?"
    assert result.value.ambiguity is None


async def test_interview_handler_uses_one_atomic_completion_after_three_answers(tmp_path) -> None:
    adapter = MagicMock()
    adapter.complete = AsyncMock(return_value=Result.ok(_response(_payload())))
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="test-model")
    state = InterviewState(
        interview_id="interview_atomic_handler",
        initial_context="Build a report generator",
        rounds=[
            InterviewRound(round_number=1, question="Who uses it?", user_response="Analysts"),
            InterviewRound(round_number=2, question="What output?", user_response="CSV"),
            InterviewRound(round_number=3, question="What is constrained?", user_response=None),
        ],
    )
    assert (await engine.save_state(state)).is_ok
    handler = InterviewHandler(
        interview_engine=engine,
        llm_adapter=adapter,
        data_dir=tmp_path,
    )

    result = await handler.handle({"session_id": state.interview_id, "answer": "No network calls"})

    assert result.is_ok
    assert "What observable result proves this is complete?" in result.value.text_content
    assert result.value.meta["ambiguity_score"] == 0.29
    adapter.complete.assert_awaited_once()
