"""Regression tests for reopening a seed-ready interview via Seed-ready Acceptance Guard.

These tests pin the handler-side contract for reopening a completed interview
when the main session sends a follow-up answer to a probe question that MCP
did not generate (post-seed-ready challenge per skills/interview/SKILL.md).

The handler must:
1. Reject the answer if no ``last_question`` is supplied (the previously-
   answered last round's question must NOT be reused — that would corrupt
   the transcript by binding the new answer to the wrong question).
2. Accept the answer when ``last_question`` is supplied, appending a new
   round with the caller-provided probe question and the user's answer.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.tools.definitions import InterviewHandler


def _seed_ready_completed_state() -> InterviewState:
    return InterviewState(
        interview_id="sess-reopen",
        status=InterviewStatus.COMPLETED,
        completion_candidate_streak=2,
        ambiguity_score=0.15,
        ambiguity_breakdown={"goal_clarity": {"clarity_score": 0.9}},
        rounds=[
            InterviewRound(
                round_number=1,
                question="What should the racer do?",
                user_response="Race around tracks with items",
            ),
            InterviewRound(
                round_number=2,
                question="What items are available?",
                user_response="boost / shell / banana",
            ),
        ],
    )


class TestSeedReadyReopen:
    async def test_reopen_without_last_question_is_rejected(self) -> None:
        """Reopening a seed-ready interview without ``last_question`` must fail.

        Reusing ``state.rounds[-1].question`` would bind the caller's new
        answer to the previously-answered probe, corrupting the transcript.
        """
        handler = InterviewHandler()
        handler._emit_event = AsyncMock()
        state = _seed_ready_completed_state()

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.record_response = AsyncMock()

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
            return_value=mock_engine,
        ):
            result = await handler.handle(
                {
                    "session_id": "sess-reopen",
                    "answer": "Item boxes on track; pickup by collision",
                }
            )

        assert result.is_err
        assert "last_question" in str(result.error)
        # Engine must not be invoked when the handler rejects up front.
        mock_engine.record_response.assert_not_called()
        # Transcript must be untouched.
        assert len(state.rounds) == 2
        assert state.rounds[-1].user_response == "boost / shell / banana"

    async def test_reopen_with_last_question_appends_fresh_round(self) -> None:
        """With ``last_question`` supplied, a new round is appended cleanly.

        The reopened round must carry the caller-provided probe question, NOT
        the prior answered question. Engine-side reopen logic (verified in
        tests/unit/bigbang/test_interview.py) clears ambiguity and the streak.
        """
        handler = InterviewHandler()
        handler._emit_event = AsyncMock()
        state = _seed_ready_completed_state()

        async def fake_record_response(
            current_state: InterviewState, answer: str, question: str
        ) -> Result[InterviewState, Exception]:
            # Mirror the production engine's reopen contract so the handler
            # branch under test sees realistic post-conditions.
            current_state.status = InterviewStatus.IN_PROGRESS
            current_state.clear_stored_ambiguity()
            current_state.completion_candidate_streak = 0
            current_state.rounds.append(
                InterviewRound(
                    round_number=len(current_state.rounds) + 1,
                    question=question,
                    user_response=answer,
                )
            )
            return Result.ok(current_state)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.record_response = AsyncMock(side_effect=fake_record_response)
        mock_engine.ask_next_question = AsyncMock(return_value=Result.ok("Next question?"))

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch.object(handler, "_score_interview_state", AsyncMock(return_value=None)),
        ):
            result = await handler.handle(
                {
                    "session_id": "sess-reopen",
                    "answer": "Item boxes on track; pickup by collision",
                    "last_question": "How are items acquired?",
                }
            )

        assert result.is_ok
        # The handler must pass the caller-provided probe question (NOT the
        # prior already-answered question) into record_response.
        mock_engine.record_response.assert_called_once()
        record_args = mock_engine.record_response.call_args
        assert record_args.args[1] == "Item boxes on track; pickup by collision"
        assert record_args.args[2] == "How are items acquired?"
        # The prior answered round is preserved verbatim — not overwritten.
        assert state.rounds[1].question == "What items are available?"
        assert state.rounds[1].user_response == "boost / shell / banana"
        # The newly-recorded round carries the caller-provided question text.
        recorded_round = next(
            r for r in state.rounds if r.user_response == "Item boxes on track; pickup by collision"
        )
        assert recorded_round.question == "How are items acquired?"
        # Engine reopen contract executed (status flipped, streak reset). The
        # ambiguity_score post-handler depends on the inline rescore
        # (mocked here to None) — engine-level invalidation is pinned in
        # tests/unit/bigbang/test_interview.py.
        assert state.completion_candidate_streak == 0
        assert state.status == InterviewStatus.IN_PROGRESS
