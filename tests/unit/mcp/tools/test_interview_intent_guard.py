from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.mcp.tools.definitions import InterviewHandler

VIDEO_GOAL = (
    "I want to make a video harness when I put a video to harness, "
    "the harness will make some shorts and long form video with transcript"
)

REVIEW_ONLY_QUESTION = (
    "Should this be --mode auto exporting MP4s or --mode review with review-only "
    "package output?"
)


def _pending_video_state() -> InterviewState:
    return InterviewState(
        interview_id="interview_intentguard1",
        initial_context=VIDEO_GOAL,
        rounds=[
            InterviewRound(
                round_number=1,
                question=REVIEW_ONLY_QUESTION,
                user_response=None,
            )
        ],
    )


async def test_interview_handler_blocks_generated_review_only_answer_before_recording() -> None:
    state = _pending_video_state()
    mock_engine = MagicMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
    mock_engine.record_response = AsyncMock()
    mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))

    handler = InterviewHandler(interview_engine=mock_engine, llm_adapter=MagicMock())
    handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "[from-auto][conservative_default] Use review-only mode.",
        }
    )

    assert result.is_err
    assert "IntentGuard blocked interview answer" in str(result.error)
    mock_engine.record_response.assert_not_called()
    assert state.rounds[-1].user_response is None


async def test_interview_handler_records_human_contract_change_with_intent_guard_warning() -> None:
    state = _pending_video_state()

    async def record_response(
        current_state: InterviewState, answer: str, question: str
    ) -> Result[InterviewState, object]:
        current_state.rounds.append(
            InterviewRound(
                round_number=current_state.current_round_number,
                question=question,
                user_response=answer,
            )
        )
        return Result.ok(current_state)

    mock_engine = MagicMock()
    mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
    mock_engine.record_response = AsyncMock(side_effect=record_response)
    mock_engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))
    mock_engine.ask_next_question = AsyncMock(return_value=Result.ok("What should be exported?"))

    handler = InterviewHandler(interview_engine=mock_engine, llm_adapter=MagicMock())
    handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]

    result = await handler.handle(
        {
            "session_id": state.interview_id,
            "answer": "Let's make it review-only for now.",
        }
    )

    assert result.is_ok
    assert result.value.meta["intent_guard"]["status"] == "warn"
    assert any(
        check["code"] == "user_contract_change"
        for check in result.value.meta["intent_guard"]["checks"]
    )
    mock_engine.record_response.assert_awaited_once()
