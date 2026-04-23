"""Regression tests for explicit "done" streak + shortfall handling (PR #428, fixes #405).

These tests exercise the 'ooo interview ... done' code path in
InterviewHandler to pin the two-signal completion contract: a lucky low
ambiguity score alone never closes the interview; only a qualifying
streak (AUTO_COMPLETE_STREAK_REQUIRED) does. The tests also pin the
shortfall persistence guarantee and the orthogonal advance_streak /
reset_on_failure flags on _score_interview_state().
"""

from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.core.types import Result
from ouroboros.mcp.tools.definitions import InterviewHandler


def create_mock_live_ambiguity_score(
    score: float,
    *,
    seed_ready: bool,
) -> AmbiguityScore:
    """Create an ambiguity score object for interview handler tests."""
    clarity_score = 1.0 - score
    result = AmbiguityScore(
        overall_score=score,
        breakdown=ScoreBreakdown(
            goal_clarity=ComponentScore(
                name="Goal Clarity",
                clarity_score=clarity_score,
                weight=0.4,
                justification="Mock goal clarity",
            ),
            constraint_clarity=ComponentScore(
                name="Constraint Clarity",
                clarity_score=clarity_score,
                weight=0.3,
                justification="Mock constraint clarity",
            ),
            success_criteria_clarity=ComponentScore(
                name="Success Criteria Clarity",
                clarity_score=clarity_score,
                weight=0.3,
                justification="Mock success clarity",
            ),
        ),
    )
    assert result.is_ready_for_seed is seed_ready
    return result


class TestInterviewDoneStreakAndShortfall:
    """Explicit-done path: streak enforcement + shortfall persistence."""

    async def test_explicit_done_advances_streak(self) -> None:
        """Explicit 'done' with qualifying score but streak=0 must advance streak.

        Regression test for #405: previously the handler refused completion
        when streak < required but never advanced the streak, leaving the
        user stuck with no path forward — 'done' would loop indefinitely.
        """
        handler = InterviewHandler()
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-123",
            completion_candidate_streak=0,
            ambiguity_score=0.14,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "Goal Clarity",
                    "clarity_score": 0.90,
                    "weight": 0.4,
                    "justification": "Goal is clear.",
                },
                "constraint_clarity": {
                    "name": "Constraint Clarity",
                    "clarity_score": 0.86,
                    "weight": 0.3,
                    "justification": "Constraints are clear.",
                },
                "success_criteria_clarity": {
                    "name": "Success Criteria Clarity",
                    "clarity_score": 0.88,
                    "weight": 0.3,
                    "justification": "Success criteria are clear.",
                },
            },
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock()
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
            return_value=mock_engine,
        ):
            result = await handler.handle({"session_id": "sess-123", "answer": "done"})

        assert result.is_ok
        assert state.status == InterviewStatus.IN_PROGRESS
        # The streak must advance by 1 on explicit 'done' with qualifying score.
        assert state.completion_candidate_streak == 1
        assert result.value.meta["seed_ready"] is False
        assert result.value.meta["completion_candidate_streak"] == 1
        assert "Stability check: 1/2" in result.value.content[0].text
        assert "Type 'done' once more" in result.value.content[0].text
        mock_engine.complete_interview.assert_not_called()
        mock_engine.ask_next_question.assert_not_called()
        mock_engine.save_state.assert_called()

    async def test_explicit_done_completes_when_threshold_reached(self) -> None:
        """Explicit 'done' with streak=1 and qualifying score should complete interview."""
        handler = InterviewHandler()
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-123",
            completion_candidate_streak=1,
            ambiguity_score=0.14,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "Goal Clarity",
                    "clarity_score": 0.90,
                    "weight": 0.4,
                    "justification": "Goal is clear.",
                },
                "constraint_clarity": {
                    "name": "Constraint Clarity",
                    "clarity_score": 0.86,
                    "weight": 0.3,
                    "justification": "Constraints are clear.",
                },
                "success_criteria_clarity": {
                    "name": "Success Criteria Clarity",
                    "clarity_score": 0.88,
                    "weight": 0.3,
                    "justification": "Success criteria are clear.",
                },
            },
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )

        async def complete_state(
            current_state: InterviewState,
        ) -> Result[InterviewState, Exception]:
            current_state.status = InterviewStatus.COMPLETED
            return Result.ok(current_state)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock(side_effect=complete_state)
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
            return_value=mock_engine,
        ):
            result = await handler.handle({"session_id": "sess-123", "answer": "done"})

        assert result.is_ok
        assert state.status == InterviewStatus.COMPLETED
        # Explicit 'done' advanced the streak from 1 to 2, meeting the threshold.
        assert state.completion_candidate_streak == 2
        assert result.value.meta["completed"] is True
        mock_engine.complete_interview.assert_called_once()
        mock_engine.ask_next_question.assert_not_called()

    async def test_explicit_done_no_infinite_loop(self) -> None:
        """Two sequential 'done' commands must complete the interview (no infinite loop).

        Regression test for #405 infinite-loop bug: the first 'done' advanced
        the streak to 1, and the second 'done' completed the interview.
        """
        handler = InterviewHandler()
        handler._emit_event = AsyncMock()

        breakdown = {
            "goal_clarity": {
                "name": "Goal Clarity",
                "clarity_score": 0.90,
                "weight": 0.4,
                "justification": "Goal is clear.",
            },
            "constraint_clarity": {
                "name": "Constraint Clarity",
                "clarity_score": 0.86,
                "weight": 0.3,
                "justification": "Constraints are clear.",
            },
            "success_criteria_clarity": {
                "name": "Success Criteria Clarity",
                "clarity_score": 0.88,
                "weight": 0.3,
                "justification": "Success criteria are clear.",
            },
        }

        state = InterviewState(
            interview_id="sess-123",
            completion_candidate_streak=0,
            ambiguity_score=0.14,
            ambiguity_breakdown=breakdown,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )

        async def complete_state(
            current_state: InterviewState,
        ) -> Result[InterviewState, Exception]:
            current_state.status = InterviewStatus.COMPLETED
            return Result.ok(current_state)

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock(side_effect=complete_state)
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
            return_value=mock_engine,
        ):
            first = await handler.handle({"session_id": "sess-123", "answer": "done"})
            # After first 'done': streak advanced, interview still in progress.
            assert first.is_ok
            assert state.status == InterviewStatus.IN_PROGRESS
            assert state.completion_candidate_streak == 1

            second = await handler.handle({"session_id": "sess-123", "answer": "done"})

        # Second 'done' must complete the interview — proving no infinite loop.
        assert second.is_ok
        assert state.status == InterviewStatus.COMPLETED
        assert state.completion_candidate_streak == 2
        assert second.value.meta["completed"] is True
        mock_engine.complete_interview.assert_called_once()

    async def test_explicit_done_live_rescore_advances_streak_exactly_once(self) -> None:
        """Regression for #405: live-rescore path must not double-bump the streak.

        Prior bug: ``_score_interview_state`` silently advanced the streak
        inside ``_update_completion_candidate_streak``, and the explicit-done
        branch then advanced it again — a single ``done`` with no stored
        score walked 0 → 1 → 2 and auto-completed the interview, bypassing
        the 2-signal gate.

        With the fix, the scorer is called with ``advance_streak=False``
        in this branch, so exactly one advance happens (streak = 1) and
        the interview stays in progress, asking the user to confirm
        again.
        """
        handler = InterviewHandler(llm_adapter=MagicMock())
        handler._emit_event = AsyncMock()
        # No stored ambiguity snapshot — forces the live-rescore branch.
        state = InterviewState(
            interview_id="sess-405",
            completion_candidate_streak=0,
            ambiguity_score=None,
            ambiguity_breakdown=None,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )

        # Qualifying live-score result — with the old double-bump bug this
        # would be enough to auto-complete on the first ``done``.
        rescored = AmbiguityScore(
            overall_score=0.14,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal Clarity",
                    clarity_score=0.90,
                    weight=0.4,
                    justification="Goal is clear.",
                ),
                constraint_clarity=ComponentScore(
                    name="Constraint Clarity",
                    clarity_score=0.86,
                    weight=0.3,
                    justification="Constraints are clear.",
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success Criteria Clarity",
                    clarity_score=0.88,
                    weight=0.3,
                    justification="Success criteria are clear.",
                ),
            ),
        )

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock()
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()

        with (
            patch.object(
                handler, "_score_interview_state", AsyncMock(return_value=rescored)
            ) as mock_score,
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
        ):
            result = await handler.handle({"session_id": "sess-405", "answer": "done"})

        assert result.is_ok
        # Interview MUST stay in progress; one 'done' must not bypass the gate.
        assert state.status == InterviewStatus.IN_PROGRESS
        assert state.completion_candidate_streak == 1, (
            "Explicit 'done' with live re-score must advance the streak exactly "
            "once. Previous bug: streak jumped 0 → 1 → 2 (double-bumped inside "
            "_score_interview_state and again in the explicit-done branch)."
        )
        mock_engine.complete_interview.assert_not_called()
        # Scorer was invoked for the exit score, but must not have advanced
        # the streak itself.
        mock_score.assert_called_once()
        _args, kwargs = mock_score.call_args
        assert kwargs.get("advance_streak") is False, (
            "Explicit-done branch must call _score_interview_state with "
            "advance_streak=False to own the single streak advance itself."
        )
        assert kwargs.get("reset_on_failure") is True, (
            "Explicit-done branch must keep the shared stale-streak "
            "invalidation contract by passing reset_on_failure=True."
        )

    async def test_explicit_done_nonqualifying_live_rescore_resets_stale_streak(self) -> None:
        """Explicit ``done`` must not preserve a stale streak after a weak live rescore.

        Regression for the follow-up owner-review note on 60e1d2e: the
        explicit-done path disables the scorer-owned increment with
        ``advance_streak=False``, but a weak live rescore still needs to
        reset an existing streak via ``reset_on_failure=True``. Otherwise
        a stale ``completion_candidate_streak=1``
        can survive a failing rescore and let the next qualifying signal
        complete the interview after only one real post-failure confirmation.
        """
        handler = InterviewHandler(llm_adapter=MagicMock())
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-405-reset",
            completion_candidate_streak=1,
            ambiguity_score=None,
            ambiguity_breakdown=None,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="Pending closure question?",
                    user_response=None,
                )
            ],
        )

        weak_score = create_mock_live_ambiguity_score(0.55, seed_ready=False)
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=Result.ok(weak_score))

        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock()
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
                return_value=mock_scorer,
            ),
        ):
            result = await handler.handle({"session_id": "sess-405-reset", "answer": "done"})

        assert result.is_ok
        assert "Cannot complete yet" in result.value.content[0].text
        assert state.status == InterviewStatus.IN_PROGRESS
        assert state.completion_candidate_streak == 0, (
            "A non-qualifying live rescore must clear any stale streak even when "
            "explicit-done owns the qualifying increment."
        )
        assert result.value.meta["seed_ready"] is False
        mock_engine.complete_interview.assert_not_called()
        mock_engine.save_state.assert_awaited()

    async def test_ambiguity_gate_persist_failure_surfaces_error_not_swallowed(self) -> None:
        """Pinned: stale-streak reset on the ambiguity-gate branch must durably land.

        Design-note regression for the ouroboros-agent[bot] BLOCKING finding
        on 227f4cb. The explicit-done path calls ``_score_interview_state(
        advance_streak=False, reset_on_failure=True)`` which may clear an
        existing ``completion_candidate_streak`` in memory. If the subsequent
        ``save_state()`` silently fails, the next request reloads the
        pre-reset streak from disk and one new qualifying signal finalizes
        the interview after only a single post-reset confirmation — the
        exact two-signal violation PR #428 exists to prevent.

        The refuse branch must therefore surface persist failures as a hard
        error, mirroring the shortfall branch's contract.
        """
        from ouroboros.core.errors import ValidationError

        handler = InterviewHandler(llm_adapter=MagicMock())
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-428-refuse-persist",
            completion_candidate_streak=1,  # stale from an earlier qualifying signal
            ambiguity_score=None,  # forces the live rescore path
            ambiguity_breakdown=None,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="Pending closure question?",
                    user_response=None,
                )
            ],
        )

        weak_score = create_mock_live_ambiguity_score(0.55, seed_ready=False)
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=Result.ok(weak_score))

        save_failure = Result.err(
            ValidationError(
                "disk full",
                details={"interview_id": "sess-428-refuse-persist"},
            )
        )
        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock()
        mock_engine.save_state = AsyncMock(return_value=save_failure)
        mock_engine.ask_next_question = AsyncMock()

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
                return_value=mock_scorer,
            ),
        ):
            result = await handler.handle(
                {"session_id": "sess-428-refuse-persist", "answer": "done"}
            )

        # MUST NOT return the normal "Cannot complete yet" / ambiguity-gate
        # response — that would leave the stale streak on disk and violate
        # the two-signal contract on the next request.
        assert result.is_err, (
            "Ambiguity-gate refuse branch must surface save_state failures as "
            "an error, not swallow them behind the normal refuse response."
        )
        assert result.error.tool_name == "ouroboros_interview"
        assert "persist" in str(result.error).lower()
        mock_engine.save_state.assert_called()
        mock_engine.complete_interview.assert_not_called()
        mock_engine.ask_next_question.assert_not_called()

    async def test_explicit_done_shortfall_preserves_pending_round(self) -> None:
        """Regression for #405 follow-up: shortfall must not pop the pending round.

        When streak advances but still falls short of the threshold, the
        response tells the user they can "answer the pending question" —
        so the pending round must still exist. The previous implementation
        popped the pending round before any streak check, leaving no live
        question for the user's next plain answer to attach to.
        """
        handler = InterviewHandler(llm_adapter=MagicMock())
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-405-shortfall",
            completion_candidate_streak=0,
            ambiguity_score=None,
            ambiguity_breakdown=None,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="Q1",
                    user_response="A1",
                ),
                InterviewRound(
                    round_number=2,
                    question="Still-pending question?",
                    user_response=None,
                ),
            ],
        )
        rescored = AmbiguityScore(
            overall_score=0.14,
            breakdown=ScoreBreakdown(
                goal_clarity=ComponentScore(
                    name="Goal Clarity",
                    clarity_score=0.90,
                    weight=0.4,
                    justification="Goal is clear.",
                ),
                constraint_clarity=ComponentScore(
                    name="Constraint Clarity",
                    clarity_score=0.86,
                    weight=0.3,
                    justification="Constraints are clear.",
                ),
                success_criteria_clarity=ComponentScore(
                    name="Success Criteria Clarity",
                    clarity_score=0.88,
                    weight=0.3,
                    justification="Success criteria are clear.",
                ),
            ),
        )
        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock()
        mock_engine.save_state = AsyncMock(return_value=MagicMock(is_ok=True, is_err=False))
        mock_engine.ask_next_question = AsyncMock()

        with (
            patch.object(handler, "_score_interview_state", AsyncMock(return_value=rescored)),
            patch(
                "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
                return_value=mock_engine,
            ),
        ):
            result = await handler.handle({"session_id": "sess-405-shortfall", "answer": "done"})

        assert result.is_ok
        # Shortfall path: still in progress, pending round untouched, message
        # matches what the user can actually do next.
        assert state.status == InterviewStatus.IN_PROGRESS
        assert len(state.rounds) == 2, "pending round must not be popped on shortfall"
        assert state.rounds[-1].user_response is None
        assert state.rounds[-1].question == "Still-pending question?"
        assert "answer the pending question" in result.value.content[0].text
        mock_engine.complete_interview.assert_not_called()

    async def test_explicit_done_shortfall_returns_error_when_persist_fails(self) -> None:
        """Regression for #405 follow-up design note on 3c2531d.

        The explicit-'done' shortfall branch makes persistence part of the
        correctness contract: the streak advance must durably land so the
        next 'done' continues from the advanced value. If the backing
        save fails, returning the "almost there" success message would
        leave the user in a silent infinite loop — every 'done' re-
        advances from 0 because the previous advance was never written.

        Treat a save failure on this branch as a hard error surfaced to
        the caller rather than a swallowed best-effort write.
        """
        from ouroboros.core.errors import ValidationError

        handler = InterviewHandler()
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-405-persist",
            completion_candidate_streak=0,
            ambiguity_score=0.14,
            ambiguity_breakdown={
                "goal_clarity": {
                    "name": "Goal Clarity",
                    "clarity_score": 0.90,
                    "weight": 0.4,
                    "justification": "Goal is clear.",
                },
                "constraint_clarity": {
                    "name": "Constraint Clarity",
                    "clarity_score": 0.86,
                    "weight": 0.3,
                    "justification": "Constraints are clear.",
                },
                "success_criteria_clarity": {
                    "name": "Success Criteria Clarity",
                    "clarity_score": 0.88,
                    "weight": 0.3,
                    "justification": "Success criteria are clear.",
                },
            },
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )

        save_failure = Result.err(
            ValidationError(
                "disk full",
                details={"interview_id": "sess-405-persist"},
            )
        )
        mock_engine = MagicMock()
        mock_engine.load_state = AsyncMock(return_value=Result.ok(state))
        mock_engine.complete_interview = AsyncMock()
        mock_engine.save_state = AsyncMock(return_value=save_failure)
        mock_engine.ask_next_question = AsyncMock()

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.InterviewEngine",
            return_value=mock_engine,
        ):
            result = await handler.handle({"session_id": "sess-405-persist", "answer": "done"})

        # The handler must NOT silently return the "almost there" success
        # message when the streak advance failed to persist.
        assert result.is_err, (
            "Shortfall branch must surface persist failures as an error, "
            "not return the 'Stability check' success message with an "
            "un-persisted streak."
        )
        assert result.error.tool_name == "ouroboros_interview"
        assert "persist" in str(result.error).lower()
        mock_engine.complete_interview.assert_not_called()
        mock_engine.ask_next_question.assert_not_called()
        mock_engine.save_state.assert_called()

    async def test_advance_streak_false_still_resets_on_failure(self) -> None:
        """Pinned: ``advance_streak=False`` must not disable the failure reset.

        Design-note regression for PR #428 (60e1d2e): the original
        single ``update_streak`` knob was too broad — passing False on
        the explicit-done branch disabled BOTH the qualifying-score
        increment AND the non-qualifying reset. Split into
        ``advance_streak`` (increment) and ``reset_on_failure`` (stale
        invalidation), the explicit-done branch uses ``advance_streak=
        False, reset_on_failure=True`` so a failing live score still
        clears a stale streak.
        """
        handler = InterviewHandler(llm_adapter=MagicMock())
        handler._emit_event = AsyncMock()
        state = InterviewState(
            interview_id="sess-428-advfalse",
            completion_candidate_streak=1,  # pre-existing stale value
            ambiguity_score=None,
            ambiguity_breakdown=None,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What should it do?",
                    user_response=None,
                )
            ],
        )

        # Simulate scorer *error* (score_result.is_err) — this path
        # triggers the clear_stored_ambiguity + reset_on_failure branch.
        scorer_failure = Result.err(
            ValueError("live ambiguity scoring failed"),
        )
        mock_scorer = MagicMock()
        mock_scorer.score = AsyncMock(return_value=scorer_failure)

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
            return_value=mock_scorer,
        ):
            returned_score = await handler._score_interview_state(
                MagicMock(),
                state,
                advance_streak=False,
                reset_on_failure=True,
            )

        assert returned_score is None
        assert state.completion_candidate_streak == 0, (
            "advance_streak=False must NOT disable the shared stale-streak "
            "reset on failure — that would make the two-signal completion "
            "contract stateful across flows."
        )

        # Second arm: non-qualifying (but successful) rescore still
        # resets stale streak even though advance is disabled.
        state.completion_candidate_streak = 1
        state.ambiguity_score = None
        state.ambiguity_breakdown = None
        weak_score = create_mock_live_ambiguity_score(0.55, seed_ready=False)
        mock_scorer.score = AsyncMock(return_value=Result.ok(weak_score))

        with patch(
            "ouroboros.mcp.tools.authoring_handlers.AmbiguityScorer",
            return_value=mock_scorer,
        ):
            returned_score = await handler._score_interview_state(
                MagicMock(),
                state,
                advance_streak=False,
                reset_on_failure=True,
            )

        assert returned_score is not None
        assert state.completion_candidate_streak == 0, (
            "Non-qualifying live rescore must reset stale streak even when advance_streak=False."
        )
