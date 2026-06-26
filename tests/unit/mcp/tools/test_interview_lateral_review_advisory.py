"""Milestone-transition lateral review advisory tests for #817."""

from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.events.interview import interview_lateral_review_recommended
from ouroboros.mcp.tools.authoring_handlers import (
    InterviewHandler,
    _build_interview_lateral_review_orchestration,
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


async def test_handler_surfaces_runnable_lateral_review_dispatch() -> None:
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
    assert result.value.meta["lateral_review_required"] is True
    assert result.value.meta["lateral_review_from_milestone"] == "initial"
    assert result.value.meta["lateral_review_milestone"] == "progress"
    assert result.value.meta["lateral_review_reason"] == "first_forward_milestone_transition"
    assert result.value.meta["lateral_review_tool"] == "ouroboros_lateral_think"
    assert result.value.meta["lateral_review_personas"] == [
        "researcher",
        "contrarian",
        "simplifier",
    ]
    tool_args = result.value.meta["lateral_review_tool_args"]
    assert tool_args["personas"] == ["researcher", "contrarian", "simplifier"]
    assert "Milestone: initial -> progress" in tool_args["problem_context"]
    assert "Next interview question: What edge case remains?" in tool_args["problem_context"]
    orchestration = result.value.meta["lateral_review_orchestration"]
    assert orchestration["kind"] == "mcp_directive"
    assert orchestration["directive"] == "run_lateral_persona_panel"
    assert orchestration["mcp_tool"] == "ouroboros_lateral_think"
    assert orchestration["tool_args"] == tool_args

    panel = orchestration["panel"]
    assert panel["panel_id"] == "lateral_persona_panel.v1"
    assert panel["mcp_tool"] == "ouroboros_lateral_think"
    assert panel["dispatch_modes"] == ["plugin", "inline_fallback"]
    assert panel["parallel_preference"] == "parallel_when_runtime_supports_subagents"
    assert panel["sequential_fallback"] == {
        "supported": True,
        "mode": "sequential_persona_payload_dispatch",
        "trigger": "runtime_has_no_native_parallel_subagent_primitive",
    }
    assert [persona["persona_id"] for persona in panel["personas"]] == [
        "researcher",
        "contrarian",
        "simplifier",
    ]
    assert panel["response_payload_refs"]["requires_prose_parsing"] is False
    assert "structured payload" in panel["runtime_instruction"]

    runtime_handling = orchestration["runtime_handling"]
    assert runtime_handling == {
        "call_mcp_tool_first": True,
        "parallel_capable_execution_mode": "parallel_subagent_panel",
        "sequential_fallback_mode": "sequential_persona_payload_dispatch",
        "sequential_fallback_trigger": "runtime_has_no_native_parallel_subagent_primitive",
        "result_correlation_key": "context.persona",
        "requires_prose_parsing": False,
        "synthesize_before_interview_continuation": True,
    }
    assert result.value.meta["question_advisory_recommended"] is True
    advisory = result.value.meta["question_advisory_request"]
    assert advisory["session_id"] == "sess-817"
    assert advisory["question"] == "What edge case remains?"
    assert advisory["phase"] == "answer"
    assert advisory["user_question_first"] is True
    assert advisory["allowed_capabilities"] == [
        "inspect_code",
        "web_research",
        "run_lateral_review",
    ]
    assert {lane["lane_id"] for lane in advisory["lanes"]} == {
        "code_context",
        "web_context",
        "ambiguity_contrarian",
        "answer_simplifier",
        "architecture_implications",
    }
    assert advisory["code_investigation_request"]["question"] == "What edge case remains?"
    advisory_subagents = result.value.meta["question_advisory_subagents"]
    assert [subagent["context"]["lane_id"] for subagent in advisory_subagents] == [
        "code_context",
        "web_context",
        "ambiguity_contrarian",
        "answer_simplifier",
        "architecture_implications",
    ]
    assert result.value.meta["question_advisory_preserve_content"] is True
    content_text = result.value.content[0].text
    assert content_text.startswith("Session sess-817\n\n")
    assert "What edge case remains?" in content_text
    assert "Lateral review queued:" in content_text
    assert content_text.index("What edge case remains?") < content_text.index(
        "Lateral review queued:"
    )
    assert "Session sess-817" in content_text
    assert state.lateral_review_advised_milestones == ["progress"]
    handler._emit_event_bg.assert_called()


def test_lateral_orchestration_falls_back_to_skill_prose_when_panel_metadata_absent() -> None:
    tool_args = {
        "problem_context": (
            "Session sess-legacy\n"
            "Milestone: initial -> progress\n"
            "Next interview question: What edge case remains?"
        ),
        "current_approach": "Route the next interview turn.",
        "personas": ["researcher", "contrarian", "simplifier"],
        "failed_attempts": [],
    }

    with patch(
        "ouroboros.mcp.tools.authoring_handlers."
        "lateral_persona_panel_metadata_from_capability_definitions",
        return_value={},
    ):
        orchestration = _build_interview_lateral_review_orchestration(tool_args=tool_args)

    assert orchestration["kind"] == "mcp_directive"
    assert orchestration["directive"] == "run_lateral_persona_panel"
    assert orchestration["mcp_tool"] == "ouroboros_lateral_think"
    assert orchestration["tool_args"] == tool_args
    assert orchestration["structured_lateral_panel_metadata_available"] is False
    assert orchestration["fallback"] == "skill_prose_instructions"
    assert orchestration["prose_instruction_source"] == "skills/interview/SKILL.md"
    assert (
        "call ouroboros_lateral_think with meta.lateral_review_tool_args"
        in (orchestration["prose_instruction"])
    )
    assert (
        'personas=["researcher","contrarian","simplifier"]' in (orchestration["prose_instruction"])
    )
    assert "panel" not in orchestration
    assert orchestration["runtime_handling"] == {
        "call_mcp_tool_first": True,
        "result_correlation_key": None,
        "requires_prose_parsing": True,
        "synthesize_before_interview_continuation": True,
    }


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
