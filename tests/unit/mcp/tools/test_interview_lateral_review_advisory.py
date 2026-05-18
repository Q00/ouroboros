"""Milestone-transition lateral review advisory tests for #817."""

from unittest.mock import AsyncMock, MagicMock

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.events.interview import interview_lateral_review_recommended
from ouroboros.mcp.tools.authoring_handlers import (
    InterviewHandler,
    _maybe_record_lateral_review_advisory,
)


def _score(value: float) -> AmbiguityScore:
    clarity = 1.0 - value
    return AmbiguityScore(
        overall_score=value,
        breakdown=ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=clarity,
                weight=0.4,
                justification="test",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=clarity,
                weight=0.3,
                justification="test",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=clarity,
                weight=0.3,
                justification="test",
            ),
        ),
    )


def test_forward_milestone_transition_records_advisory_meta() -> None:
    state = InterviewState(interview_id="interview_test")

    meta = _maybe_record_lateral_review_advisory(
        state,
        previous_milestone="initial",
        score=_score(0.35),
    )

    assert meta == {
        "lateral_review_recommended": True,
        "lateral_review_milestone": "progress",
        "lateral_review_from_milestone": "initial",
        "lateral_review_reason": "first_forward_milestone_transition",
    }
    assert state.lateral_review_advised_milestones == []


def test_duplicate_milestone_transition_is_suppressed() -> None:
    state = InterviewState(
        interview_id="interview_test",
        lateral_review_advised_milestones=["progress"],
    )

    meta = _maybe_record_lateral_review_advisory(
        state,
        previous_milestone="initial",
        score=_score(0.35),
    )

    assert meta is None
    assert state.lateral_review_advised_milestones == ["progress"]


def test_backward_transition_is_not_advisory() -> None:
    state = InterviewState(interview_id="interview_test")

    meta = _maybe_record_lateral_review_advisory(
        state,
        previous_milestone="refined",
        score=_score(0.35),
    )

    assert meta is None
    assert state.lateral_review_advised_milestones == []


def test_same_milestone_transition_is_not_advisory() -> None:
    state = InterviewState(interview_id="interview_test")

    meta = _maybe_record_lateral_review_advisory(
        state,
        previous_milestone="progress",
        score=_score(0.35),
    )

    assert meta is None
    assert state.lateral_review_advised_milestones == []


def test_only_three_forward_milestones_can_be_advised() -> None:
    state = InterviewState(interview_id="interview_test")

    meta = _maybe_record_lateral_review_advisory(
        state, previous_milestone="initial", score=_score(0.35)
    )
    assert meta
    state.note_lateral_review_advisory(meta["lateral_review_milestone"])
    meta = _maybe_record_lateral_review_advisory(
        state, previous_milestone="progress", score=_score(0.25)
    )
    assert meta
    state.note_lateral_review_advisory(meta["lateral_review_milestone"])
    meta = _maybe_record_lateral_review_advisory(
        state, previous_milestone="refined", score=_score(0.15)
    )
    assert meta
    state.note_lateral_review_advisory(meta["lateral_review_milestone"])

    assert state.lateral_review_advised_milestones == ["progress", "refined", "ready"]
    assert len(state.lateral_review_advised_milestones) == 3


def test_advisory_event_is_structured_and_non_blocking() -> None:
    event = interview_lateral_review_recommended(
        "interview_test",
        from_milestone="progress",
        to_milestone="refined",
        ambiguity_score=0.25,
        round_number=4,
    )

    assert event.type == "interview.lateral_review.recommended"
    assert event.aggregate_type == "interview"
    assert event.aggregate_id == "interview_test"
    assert event.data == {
        "from_milestone": "progress",
        "to_milestone": "refined",
        "ambiguity_score": 0.25,
        "round_number": 4,
        "reason": "first_forward_milestone_transition",
    }


async def test_handler_surfaces_advisory_in_meta_without_question_text_leak() -> None:
    state = InterviewState(
        interview_id="sess-817",
        # No stored ambiguity yet: this is the normal first live scoring path
        # after MIN_ROUNDS_BEFORE_EARLY_EXIT. The handler should treat it as
        # crossing from the implicit ``initial`` milestone.
        ambiguity_score=None,
        rounds=[
            InterviewRound(round_number=1, question="Q1?", user_response="A1"),
            InterviewRound(round_number=2, question="Q2?", user_response="A2"),
            InterviewRound(round_number=3, question="Q3?", user_response=None),
        ],
    )

    async def record_response(
        state: InterviewState, user_response: str, question: str
    ) -> Result[InterviewState, object]:
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=user_response,
            )
        )
        return Result.ok(state)

    mock_engine = MagicMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
    mock_engine.record_response = AsyncMock(side_effect=record_response)
    mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))
    mock_engine.ask_next_question = AsyncMock(return_value=Result.ok("What edge case remains?"))

    handler = InterviewHandler(interview_engine=mock_engine)
    handler.llm_adapter = MagicMock()
    handler._score_interview_state = AsyncMock(return_value=_score(0.35))  # type: ignore[method-assign]
    handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]

    result = await handler.handle({"session_id": "sess-817", "answer": "A3"})

    assert result.is_ok
    assert result.value.meta["lateral_review_recommended"] is True
    assert result.value.meta["lateral_review_from_milestone"] == "initial"
    assert result.value.meta["lateral_review_milestone"] == "progress"
    assert result.value.meta["lateral_review_reason"] == "first_forward_milestone_transition"
    assert "lateral" not in result.value.content[0].text.lower()
    assert state.lateral_review_advised_milestones == ["progress"]
    handler._emit_event_bg.assert_called()


async def test_handler_does_not_record_advisory_before_auto_completion() -> None:
    """Auto-completion should not consume a lateral advisory milestone."""
    handler = InterviewHandler(llm_adapter=MagicMock())
    handler._emit_event_bg = MagicMock()
    state = InterviewState(
        interview_id="sess-auto-complete",
        ambiguity_score=0.45,
        completion_candidate_streak=2,
        rounds=[
            InterviewRound(round_number=1, question="Q1", user_response="A1"),
            InterviewRound(round_number=2, question="Q2", user_response="A2"),
            InterviewRound(round_number=3, question="Ready?", user_response=None),
        ],
    )
    ready_score = _score(0.15)

    async def record_response(
        state: InterviewState, user_response: str, question: str
    ) -> Result[InterviewState, object]:
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=user_response,
            )
        )
        return Result.ok(state)

    mock_engine = MagicMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
    mock_engine.record_response = AsyncMock(side_effect=record_response)
    mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))
    mock_engine.complete_interview = AsyncMock(return_value=Result.ok(state))
    mock_engine.ask_next_question = AsyncMock()

    handler = InterviewHandler(interview_engine=mock_engine)
    handler.llm_adapter = MagicMock()
    handler._score_interview_state = AsyncMock(return_value=ready_score)  # type: ignore[method-assign]
    handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]

    result = await handler.handle({"session_id": "sess-auto-complete", "answer": "This is ready."})

    assert result.is_ok
    assert result.value.meta["completed"] is True
    assert state.lateral_review_advised_milestones == []
    mock_engine.complete_interview.assert_awaited_once()
    mock_engine.ask_next_question.assert_not_called()
    assert not any(
        call.args[0].type == "interview.lateral_review.recommended"
        for call in handler._emit_event_bg.call_args_list
    )


async def test_handler_does_not_record_advisory_when_question_generation_fails() -> None:
    state = InterviewState(
        interview_id="sess-818",
        ambiguity_score=None,
        rounds=[
            InterviewRound(round_number=1, question="Q1?", user_response="A1"),
            InterviewRound(round_number=2, question="Q2?", user_response="A2"),
            InterviewRound(round_number=3, question="Q3?", user_response=None),
        ],
    )

    async def record_response(
        state: InterviewState, user_response: str, question: str
    ) -> Result[InterviewState, object]:
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=user_response,
            )
        )
        return Result.ok(state)

    mock_engine = MagicMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
    mock_engine.record_response = AsyncMock(side_effect=record_response)
    mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))
    mock_engine.ask_next_question = AsyncMock(
        return_value=Result.err(Exception("empty response from provider"))
    )

    handler = InterviewHandler(interview_engine=mock_engine)
    handler.llm_adapter = MagicMock()
    handler._score_interview_state = AsyncMock(return_value=_score(0.35))  # type: ignore[method-assign]
    handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]

    result = await handler.handle({"session_id": "sess-818", "answer": "A3"})

    assert result.is_ok
    assert result.value.is_error is True
    assert "lateral_review_recommended" not in result.value.meta
    assert state.lateral_review_advised_milestones == []
    assert not any(
        call.args[0].type == "interview.lateral_review.recommended"
        for call in handler._emit_event_bg.call_args_list
    )
