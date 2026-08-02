"""Milestone-transition lateral review advisory tests for #817."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.events.interview import interview_lateral_review_recommended
from ouroboros.mcp.tools.advisory_dispatch import append_question_advisory_dispatch
from ouroboros.mcp.tools.authoring_handlers import (
    InterviewHandler,
    _attach_question_assist_requests,
    _build_interview_lateral_review_orchestration,
    _maybe_record_lateral_review_advisory,
)
from ouroboros.mcp.tools.subagent import SubagentDispatchMode
from ouroboros.orchestrator.capabilities.interview_schemas import (
    _interview_question_advisory_fanout_metadata,
)

# Derived, not hardcoded: these tests care that every declared lane is
# dispatched, not how many lanes there happen to be.
_ADVISORY_LANE_COUNT = len(_interview_question_advisory_fanout_metadata()["lanes"])


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
    assert panel["dispatch_modes"] == ["plugin", "sequential"]
    assert panel["legacy_dispatch_modes"] == ["inline_fallback"]
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
    assert "legacy alias" in panel["runtime_instruction"]

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
    assert advisory["contract_id"] == "interview_question_advisory_fanout.v1"
    assert (
        result.value.meta["question_advisory_contract_id"]
        == "interview_question_advisory_fanout.v1"
    )
    assert advisory["session_id"] == "sess-817"
    assert advisory["question"] == "What edge case remains?"
    assert advisory["phase"] == "answer"
    assert advisory["user_question_first"] is True
    assert advisory["allowed_capabilities"] == [
        "inspect_code",
        "web_research",
        "run_lateral_review",
        "read_data",
    ]
    assert {lane["lane_id"] for lane in advisory["lanes"]} == {
        "code_context",
        "web_context",
        "data_context",
        "ambiguity_contrarian",
        "answer_simplifier",
        "architecture_implications",
    }
    assert advisory["code_investigation_request"]["question"] == "What edge case remains?"
    advisory_subagents = result.value.meta["question_advisory_subagents"]
    assert [subagent["context"]["lane_id"] for subagent in advisory_subagents] == [
        "code_context",
        "web_context",
        "data_context",
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


async def test_advisory_fanout_is_host_driven_stamped_on_codex_runtime() -> None:
    """Codex (host-driven) advisory fanout must carry an explicit spawn directive."""
    state = InterviewState(
        interview_id="sess-codex",
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

    handler = InterviewHandler(
        interview_engine=mock_engine,
        agent_runtime_backend="codex",
        opencode_mode="plugin",
    )
    handler.llm_adapter = MagicMock()
    handler._score_interview_state = AsyncMock(return_value=_score(0.35))  # type: ignore[method-assign]
    handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]

    result = await handler.handle({"session_id": "sess-codex", "answer": "A3"})

    assert result.is_ok
    meta = result.value.meta
    # Advisory lanes are still attached...
    assert len(meta["question_advisory_subagents"]) == _ADVISORY_LANE_COUNT
    # ...but now stamped so the Codex host fans them out itself.
    assert meta["question_advisory_dispatch_mode"] == "host_driven"
    assert meta["question_advisory_contract_id"] == "interview_question_advisory_fanout.v1"
    assert meta["question_advisory_host_action"] == "spawn_subagents"
    # Advisory lanes correlate by lane_id (persona is None on some lanes).
    assert meta["question_advisory_result_correlation_key"] == "context.lane_id"
    assert "subagent_orchestration_instruction" in meta


def test_question_advisory_sequential_runtime_emits_processing_contract() -> None:
    meta: dict[str, object] = {}

    _attach_question_assist_requests(
        meta,
        session_id="sess-sequential",
        question="What constraint remains?",
        phase="answer",
        score=_score(0.35),
        dispatch_mode=SubagentDispatchMode.SEQUENTIAL,
        runtime_backend="gemini",
    )

    assert len(meta["question_advisory_subagents"]) == _ADVISORY_LANE_COUNT
    assert meta["question_advisory_dispatch_mode"] == "sequential"
    assert meta["question_advisory_contract_id"] == "interview_question_advisory_fanout.v1"
    assert meta["question_advisory_host_action"] == "process_payloads_sequentially"
    assert meta["question_advisory_result_correlation_key"] == "context.lane_id"
    assert (
        "process each structured subagent payload sequentially"
        in (meta["subagent_orchestration_instruction"])
    )


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


# ---------------------------------------------------------------------------
# Host-visible delivery of the advisory fan-out.
#
# The tests above assert the fan-out contract on ``result.value.meta``. That is
# where the contract is *built*, not where a host-driven runtime *reads* it:
# MCP ``_meta`` reaches a bridge process, and HOST_DRIVEN / SEQUENTIAL runtimes
# have none — the host model's only channel is response content. So these pin
# the rendered surface instead. A regression that put the payloads back into
# meta alone would keep every assertion above green.
# ---------------------------------------------------------------------------


def _advisory_meta(
    dispatch_mode: SubagentDispatchMode,
    *,
    runtime_backend: str,
    opencode_mode: str | None = None,
) -> dict[str, object]:
    meta: dict[str, object] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-render",
        question="What are the acceptance criteria?",
        phase="answer",
        score=_score(0.35),
        dispatch_mode=dispatch_mode,
        runtime_backend=runtime_backend,
        opencode_mode=opencode_mode,
    )
    return meta


def test_host_driven_advisory_payloads_reach_the_response_text() -> None:
    """A HOST_DRIVEN host must be able to fan out from content alone."""
    meta = _advisory_meta(SubagentDispatchMode.HOST_DRIVEN, runtime_backend="claude")

    text = append_question_advisory_dispatch("Session sess-render\n\nQ?", meta)

    # The spawn directive is stated, not merely stamped.
    assert "spawn_subagents" in text
    assert "context.lane_id" in text
    # Every declared lane is named, and the payloads are recoverable from the
    # content verbatim -- the host is told not to rewrite prompts from prose,
    # which only holds if it can read the prompts back out.
    payloads = meta["question_advisory_subagents"]
    assert isinstance(payloads, list)
    assert len(payloads) == _ADVISORY_LANE_COUNT
    for payload in payloads:
        assert payload["context"]["lane_id"] in text

    block = text.partition("```json\n")[2].partition("\n```")[0]
    assert json.loads(block) == payloads
    # Requiredness is visible, so a host knows which absences block completion.
    assert "data_context" in text
    assert "undispatched" in text


def test_advisory_dispatch_keeps_the_question_first() -> None:
    """The question must precede the directive, per preserve_content."""
    meta = _advisory_meta(SubagentDispatchMode.HOST_DRIVEN, runtime_backend="claude")

    text = append_question_advisory_dispatch(
        "Session sess-render\n\nWhat are the acceptance criteria?", meta
    )

    assert text.startswith("Session sess-render\n\nWhat are the acceptance criteria?")
    assert text.index("What are the acceptance criteria?") < text.index("Host action")


def test_sequential_runtime_gets_its_own_directive() -> None:
    """SEQUENTIAL hosts read content too, and are told to process in order."""
    meta = _advisory_meta(SubagentDispatchMode.SEQUENTIAL, runtime_backend="gemini")

    text = append_question_advisory_dispatch("Session sess-render\n\nQ?", meta)

    assert "process_payloads_sequentially" in text
    assert "spawn one subagent per payload" not in text


def test_plugin_passive_response_text_is_unchanged() -> None:
    """A bridge reads metadata out of band; duplicating it into content is noise."""
    meta = _advisory_meta(
        SubagentDispatchMode.PLUGIN_PASSIVE,
        runtime_backend="opencode",
        opencode_mode="plugin",
    )
    base = "Session sess-render\n\nQ?"

    # The lanes are still built and registered for the bridge...
    assert len(meta["question_advisory_subagents"]) == _ADVISORY_LANE_COUNT
    # ...and PLUGIN_PASSIVE stamps no host action, so content stays byte-identical.
    assert "question_advisory_host_action" not in meta
    assert append_question_advisory_dispatch(base, meta) == base


def test_advisory_dispatch_noop_without_payloads() -> None:
    """The length-guard turn attaches no advisory; content must not grow."""
    base = "Session sess-render\n\nQ?"

    assert append_question_advisory_dispatch(base, {}) == base
    assert (
        append_question_advisory_dispatch(
            base, {"question_advisory_host_action": "spawn_subagents"}
        )
        == base
    )


def test_auto_driver_reads_the_question_without_the_directive() -> None:
    """Two consumers read one response; only the host model wants the directive.

    ``auto`` is a programmatic driver, not a host model -- it spawns nothing and
    would otherwise answer a question with the fan-out directive appended to it.
    """
    from ouroboros.auto.adapters import _extract_interview_question

    meta = _advisory_meta(SubagentDispatchMode.HOST_DRIVEN, runtime_backend="claude")
    question = "What are the acceptance criteria?"
    text = append_question_advisory_dispatch(f"Session sess-render\n\n{question}", meta)

    assert _extract_interview_question(text, session_id="sess-render") == question


def test_dispatch_marker_separates_question_from_directive() -> None:
    """The boundary is explicit, so a parser never has to guess where to cut."""
    from ouroboros.mcp.tools.advisory_dispatch import QUESTION_ADVISORY_DISPATCH_MARKER

    meta = _advisory_meta(SubagentDispatchMode.HOST_DRIVEN, runtime_backend="claude")

    text = append_question_advisory_dispatch("Session sess-render\n\nQ?", meta)

    assert QUESTION_ADVISORY_DISPATCH_MARKER in text
    before, _, after = text.partition(QUESTION_ADVISORY_DISPATCH_MARKER)
    assert before.strip() == "Session sess-render\n\nQ?"
    assert "Host action" in after


async def test_a_question_quoting_the_whole_sentinel_reaches_the_transcript_intact() -> None:
    """At the handler, because that is where the truncation reached the Seed.

    Two rounds were spent recognising the directive by its shape: splitting at
    the marker truncated a question that mentioned it, and requiring the marker
    plus the directive's opening truncated a question that quoted both — which
    is what an interview *about this feature* contains. An in-band sentinel
    cannot tell output from quoted output, so the server compares against the
    question it issued instead.

    The two inputs below differ only in that: one is the issued question with
    our directive under it, the other is a different question that quotes the
    full sentinel. The first is recorded without the directive; the second is
    recorded whole.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    quoted = (
        "How should Ouroboros preserve this literal example?\n\n"
        f"{marker}\n\n> **Host action — quoted-example:** keep it"
    )
    echoed = f"Q2?\n\n{marker}\n\n> **Host action — spawn_subagents:** keep the question"

    for supplied, expected in ((echoed, "Q2?"), (quoted, quoted)):
        state = InterviewState(
            interview_id="sess-echo",
            rounds=[
                InterviewRound(round_number=1, question="Q1?", user_response="A1"),
                InterviewRound(round_number=2, question="Q2?", user_response=None),
            ],
        )

        async def record(
            current: InterviewState, answer: str, question: str
        ) -> Result[InterviewState, object]:
            current.rounds.append(
                InterviewRound(
                    round_number=current.current_round_number,
                    question=question,
                    user_response=answer,
                )
            )
            return Result.ok(current)

        engine = MagicMock()
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(side_effect=record)
        engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next?"))

        handler = InterviewHandler(interview_engine=engine, agent_runtime_backend="claude")
        handler.llm_adapter = MagicMock()
        handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]
        handler._score_interview_state = AsyncMock(return_value=_score(0.35))  # type: ignore[method-assign]

        result = await handler.handle(
            {"session_id": "sess-echo", "answer": "A", "last_question": supplied}
        )

        assert result.is_ok
        recorded = engine.record_response.await_args.args[2]
        assert recorded == expected, recorded


def test_every_question_bearing_response_path_appends_the_directive() -> None:
    """Three paths build a question response; a fourth would be a silent gap.

    A path that skipped the append would look identical to a turn with no
    advisory — the failure mode this whole fix exists to remove.
    """
    from pathlib import Path
    import re

    source = Path("src/ouroboros/mcp/tools/authoring_handlers.py").read_text(encoding="utf-8")

    built = set(re.findall(r"(\w+_response_text) = f?\"(?:Session|Interview started)", source))
    appended = set(re.findall(r"append_question_advisory_dispatch\(\s*(\w+_response_text)", source))

    assert built, "no question-bearing response paths found — the pattern moved"
    assert built <= appended, built - appended


def test_the_echo_comparison_states_what_it_does_not_cover() -> None:
    """The guarantee is about faithful echoes, and only those.

    A host that rewrites the question while keeping our directive attached
    matches nothing we issued, so the directive rides along. Pinned so the limit
    is a known shape rather than a surprise: closing it needs either shape
    recognition, which does not converge, or refusing the echo whenever a record
    exists, which strands plugin-mode transcripts on the placeholder
    `last_question` exists to repair.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )
    from ouroboros.mcp.tools.advisory_dispatch import resolve_echoed_question

    issued = "What are the acceptance\ncriteria for this feature?"
    directive = f"\n\n{marker}\n\n> **Host action — spawn_subagents:** keep it"

    # Faithful echo: the directive cannot reach the transcript.
    assert resolve_echoed_question(issued + directive, issued_question=issued) == issued
    # Nor when the echo arrived reflowed. Text picks that up crossing a host,
    # and judging it unequal would leak the directive through a host that did
    # exactly what was asked. Equality is `normalize_question_text` — the same
    # normalisation the fan-out digests to bind an answer to its question.
    for faithful in (
        issued + " " + directive,
        issued.replace("\n", " ") + directive,
        issued.replace("\n", "   ") + directive,
        issued.replace("?", "？") + directive,
    ):
        assert resolve_echoed_question(faithful, issued_question=issued) == issued, faithful
    # Rewritten echo: passes through as the host's own text, directive included.
    rewritten = "What acceptance criteria do you want?" + directive
    assert resolve_echoed_question(rewritten, issued_question=issued) == rewritten


def test_question_identity_and_the_echo_check_share_one_definition() -> None:
    """Two definitions of "same question" would drift, and drift invisibly.

    The fan-out digests this normalisation to bind an answer to its question;
    the echo check compares against it to tell a faithful echo from a rewritten
    one. If they disagreed, one side would bind what the other had let through.
    """
    from ouroboros.orchestrator.capabilities import (
        normalize_question_text,
        stable_code_investigation_question_identity,
    )

    reflowed = "What are the acceptance\n\ncriteria   for this feature?"
    canonical = "What are the acceptance criteria for this feature?"

    assert normalize_question_text(reflowed) == normalize_question_text(canonical)
    assert stable_code_investigation_question_identity(
        reflowed
    ) == stable_code_investigation_question_identity(canonical)


async def test_the_visible_echo_persists_only_the_question_when_a_score_is_shown() -> None:
    """A live interview scores nearly every question, so this is the normal path.

    A question-bearing response renders `(ambiguity: 0.35) ` in front of the
    question before the directive is appended, so the text a host is shown — and
    faithfully echoes back — carries a prefix the stored question never had.
    Comparing that against the stored form read a perfectly compliant echo as a
    rewrite and passed the whole thing through, marker, directive and payload
    JSON, into `record_response` and from there the Seed. The guarantee the
    previous round stated, that a faithful echo cannot reach durable state, was
    false as shipped for every scored round.

    So the comparison is against the *rendered* form, reconstructed from the
    score stored when the question was issued, and what is recorded is the
    stored form: the transcript keeps the question's identity, never its
    presentation. Recognising the prefix inside the echo instead would be shape
    matching again, and the collision would return for a question that opens
    with one.

    The unscored round runs beside it because it is the case every earlier
    sentinel regression covered, and covering only that is how this was missed.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )
    from ouroboros.orchestrator.capabilities.question_text import (
        format_question_with_ambiguity,
    )

    issued = "How do you define completion?"
    directive = f"\n\n{marker}\n\n> **Host action — spawn_subagents:** keep the question"
    scored = _score(0.35)

    for stored_score, shown in (
        (scored, format_question_with_ambiguity(issued, scored)),
        (None, issued),
    ):
        state = InterviewState(
            interview_id="sess-shown",
            rounds=[
                InterviewRound(round_number=1, question="Q1?", user_response="A1"),
                InterviewRound(round_number=2, question=issued, user_response=None),
            ],
        )
        if stored_score is not None:
            state.store_ambiguity(
                score=stored_score.overall_score,
                breakdown=stored_score.breakdown.model_dump(mode="json"),
            )

        async def record(
            current: InterviewState, answer: str, question: str
        ) -> Result[InterviewState, object]:
            current.rounds.append(
                InterviewRound(
                    round_number=current.current_round_number,
                    question=question,
                    user_response=answer,
                )
            )
            return Result.ok(current)

        engine = MagicMock()
        engine.load_state = AsyncMock(return_value=Result.ok(state))
        engine.record_response = AsyncMock(side_effect=record)
        engine.save_state = AsyncMock(return_value=MagicMock(is_err=False))
        engine.ask_next_question = AsyncMock(return_value=Result.ok("Next?"))

        handler = InterviewHandler(interview_engine=engine, agent_runtime_backend="claude")
        handler.llm_adapter = MagicMock()
        handler._emit_event_bg = MagicMock()  # type: ignore[method-assign]
        handler._score_interview_state = AsyncMock(return_value=_score(0.30))  # type: ignore[method-assign]

        result = await handler.handle(
            {"session_id": "sess-shown", "answer": "A", "last_question": shown + directive}
        )

        assert result.is_ok
        recorded = engine.record_response.await_args.args[2]
        # The stored form, not the presentation and not the directive.
        assert recorded == issued, (stored_score, recorded)
        assert marker not in recorded
        assert "ambiguity:" not in recorded
