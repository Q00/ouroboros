"""Milestone-transition lateral review advisory tests for #817."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from ouroboros.bigbang.ambiguity import AmbiguityScore, ComponentScore, ScoreBreakdown
from ouroboros.bigbang.interview import InterviewRound, InterviewState
from ouroboros.core.types import Result
from ouroboros.events.interview import interview_lateral_review_recommended
from ouroboros.mcp.tools.advisory_dispatch import (
    append_question_advisory_dispatch,
    directive_was_appended,
)
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
    assert panel["dispatch_modes"] == ["plugin", "host_driven", "host_decides", "sequential"]
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


def test_question_advisory_host_decides_contract_is_capability_neutral() -> None:
    meta = _advisory_meta(SubagentDispatchMode.HOST_DECIDES, runtime_backend="gemini")

    assert meta["question_advisory_dispatch_mode"] == "host_decides"
    assert meta["question_advisory_host_action"] == "dispatch_subagents_if_supported"
    assert meta["question_advisory_delivery_mode"] == "inline_host"
    assert meta["question_advisory_execution_preference"] == "parallel"
    assert meta["question_advisory_fallback_strategy"] == "sequential"
    assert "did not declare whether" in meta["subagent_orchestration_instruction"]

    text = append_question_advisory_dispatch("Session sess-render\n\nQ?", meta)
    assert "dispatch_subagents_if_supported" in text
    assert "when available" in text
    assert "otherwise process every payload below sequentially" in text


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

    assert (
        _extract_interview_question(
            text, session_id="sess-render", dispatch_appended=directive_was_appended(meta)
        )
        == question
    )


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

    Two rounds were spent recognising the directive by its shape so it could be
    cut: the marker truncated a question that mentioned it, the marker plus the
    directive's opening truncated a question that quoted both — which is what an
    interview *about this feature* contains. Nothing is cut now.

    On a pending round both inputs land on the question the server issued, which
    is the point: the echo is consulted to repair a damaged stored question, and
    one carrying our directive is not a repair. Neither is truncated, and no
    directive reaches the record.

    The quoting question survives *as itself* where it is genuinely the
    question — the reopen path, covered below — because there the server issued
    nothing and the host's probe is the only question there is.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    quoted = (
        "How should Ouroboros preserve this literal example?\n\n"
        f"{marker}\n\n> **Host action — quoted-example:** keep it"
    )
    echoed = f"Q2?\n\n{marker}\n\n> **Host action — spawn_subagents:** keep the question"

    for supplied, expected in ((echoed, "Q2?"), (quoted, "Q2?")):
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


def test_no_echo_of_any_shape_carries_a_directive_into_the_transcript() -> None:
    """The predicate is what closes this, and it closes it without a list.

    Three rounds went into recognising the directive by its shape so it could be
    cut: the marker, then the marker plus the directive's opening, each round
    truncating a question someone could legitimately ask. What made recognition
    unsafe was never the recognition — it was that it licensed *cutting*, so a
    false positive destroyed the user's text.

    Recognition licenses nothing here but a preference for the record. The
    stored question of an unanswered round is the question the server issued, so
    a false positive costs a repair that was not needed, and every echo shape
    fails closed: the question alone, the ambiguity-prefixed form, the whole
    response body, the body with the lateral notice, a paraphrase with the
    directive still attached. Enumerating those was what the comparison had to
    do, and each round found one more.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )
    from ouroboros.mcp.tools.advisory_dispatch import echo_carries_dispatch
    from ouroboros.orchestrator.capabilities.question_text import (
        format_question_with_ambiguity,
    )

    question = "How do you define completion?"
    rendered = format_question_with_ambiguity(question, _score(0.35))
    directive = f"\n\n{marker}\n\n> **Host action — spawn_subagents:** keep it"
    body = f"Session sess-x\n\n{rendered}"

    for shape in (
        question + directive,
        rendered + directive,
        body + directive,
        f"{body}\n\nLateral review queued: running researcher." + directive,
        "What counts as done?" + directive,
    ):
        assert echo_carries_dispatch(shape), shape[:60]

    # A repair must get through, and the marker alone does not make an echo.
    # This is the case the predicate must not swallow: `last_question` exists to
    # fix a round whose stored question was damaged by partial persistence, and
    # for one round the bare-marker test swallowed it — so a question that
    # merely quoted the sentinel could not repair anything, and where the stored
    # question was the damaged one, the damage is what reached the transcript.
    assert not echo_carries_dispatch("The real repaired question?")
    assert not echo_carries_dispatch(f"How should Ouroboros preserve {marker} here?")
    assert not echo_carries_dispatch(f"{marker}")
    assert not echo_carries_dispatch(None)


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

    That was first answered by comparing against the rendered form. The
    comparison is gone (#1484cd03) and the case it was built for is covered by a
    cheaper rule: an echo carrying our directive is not a repair, whatever
    prefix it wears, so the round keeps the question the server issued and the
    transcript keeps identity rather than presentation.

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


async def test_a_reopen_probe_quoting_the_sentinel_is_recorded_as_asked() -> None:
    """Where the host's question is the only question, it is kept verbatim.

    Reopening a completed interview — the Seed-ready challenge — has no pending
    round, so the server issued nothing and appended no directive. There is
    nothing to prefer the record over and nothing of ours to keep out, so the
    probe is recorded exactly as asked, sentinel quote and all.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    probe = f"How should Ouroboros preserve {marker} in a question?"
    state = InterviewState(
        interview_id="sess-reopen",
        rounds=[InterviewRound(round_number=1, question="Q1?", user_response="A1")],
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
        {"session_id": "sess-reopen", "answer": "A", "last_question": probe}
    )

    assert result.is_ok
    assert engine.record_response.await_args.args[2] == probe


def test_auto_cuts_a_directive_only_when_one_was_appended() -> None:
    """Shape is safe to match on only after the producer says there is one.

    `auto` parses the question out of the response body, and it used to cut at
    the last marker unconditionally. With no directive present that marker
    belongs to the question, so a question quoting the sentinel was truncated —
    and on `PLUGIN_PASSIVE`, where content is left unchanged by design, no
    directive is ever appended, so that was every turn. Auto would then answer,
    and persist through `last_question`, a question the server never asked.

    The predicate that guards the echo path does not catch it either: the
    truncation removes the marker, so what arrives looks like an ordinary
    question. The gate is what closes it — and once a directive *is* appended it
    is the last thing in the text, so the last marker is always ours.
    """
    from ouroboros.auto.adapters import _extract_interview_question
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    quoting = f"How should auto preserve this literal? {marker}\n\n> **Host action — quoted:** x"
    directive = f"\n\n{marker}\n\n> **Host action — spawn_subagents:** keep it"

    # No directive appended (the plugin-passive shape): nothing is cut.
    assert (
        _extract_interview_question(
            f"Session s\n\n{quoting}", session_id="s", dispatch_appended=False
        )
        == quoting
    )
    # A directive was appended: it is cut, and only it.
    assert (
        _extract_interview_question(
            f"Session s\n\nReal question?{directive}", session_id="s", dispatch_appended=True
        )
        == "Real question?"
    )
    # Both: the quote survives because our directive is the last thing there.
    assert (
        _extract_interview_question(
            f"Session s\n\n{quoting}{directive}", session_id="s", dispatch_appended=True
        )
        == quoting
    )
    # The default is not to cut, so a caller that forgets cannot destroy text.
    assert _extract_interview_question(f"Session s\n\n{quoting}", session_id="s") == quoting


def test_the_gate_and_the_renderer_share_one_condition() -> None:
    """A reader of the response must not disagree with its writer.

    `directive_was_appended` is the renderer's own condition, named once. Two
    copies would drift, and the drift would be silent in the worst direction:
    a gate that said yes where the renderer said no would cut text nobody added.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    for mode, backend, opencode_mode in (
        (SubagentDispatchMode.HOST_DRIVEN, "claude", None),
        (SubagentDispatchMode.SEQUENTIAL, "gemini", None),
        (SubagentDispatchMode.PLUGIN_PASSIVE, "opencode", "plugin"),
    ):
        meta = _advisory_meta(mode, runtime_backend=backend, opencode_mode=opencode_mode)
        rendered = append_question_advisory_dispatch("Q?", meta)
        assert directive_was_appended(meta) == (marker in rendered), mode

    # A turn that attached no advisory at all — the length-guard path.
    assert not directive_was_appended({})


async def test_a_repair_quoting_the_marker_replaces_a_damaged_question() -> None:
    """The harm the bare-marker test caused, at the handler.

    `last_question` exists to repair a pending round whose stored question was
    damaged by partial persistence. While the predicate tested for the marker
    alone, a repair that quoted the sentinel was read as an echo and dropped —
    and the damaged text it was sent to replace is what reached
    `record_response`, requirement extraction, and the Seed.

    The stored question below is that damage. If the repair cannot get through,
    the placeholder is what the interview remembers asking.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    repair = f"How should Ouroboros preserve {marker} in a question?"
    state = InterviewState(
        interview_id="sess-repair",
        rounds=[
            InterviewRound(round_number=1, question="Q1?", user_response="A1"),
            InterviewRound(
                round_number=2, question="[question text unavailable]", user_response=None
            ),
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
        {"session_id": "sess-repair", "answer": "A", "last_question": repair}
    )

    assert result.is_ok
    assert engine.record_response.await_args.args[2] == repair


def test_the_bridge_declares_itself_with_the_grammar_the_gatekeeper_knows() -> None:
    """Two producers append to the visible question; both must be recognisable.

    On `PLUGIN_PASSIVE` the server renders nothing — that is the transport's
    contract — and the OpenCode bridge then stamps its dispatch banner and
    response-shape JSON, `question_advisory_fanout_id` included, onto the text a
    host sees. A faithful echo of that carried the whole thing into
    `record_response` and the Seed, because the gatekeeper knew only the
    server's own grammar.

    The bridge declares itself rather than being detected: its prose is not
    stable — `[Ouroboros] ` leads only the dispatched banner, while failed-only
    and skipped-only banners start with their own words — so a gatekeeper
    matching on that would have been wrong on arrival.

    The constants live in two languages and cannot import each other. Their
    source-level parity is pinned by ``test_bridge_literal_contract.py``; this
    test exercises recognition of the bridge shape that parity protects.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        _BRIDGE_NOTICE_OPENING,
        echo_carries_dispatch,
    )
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    # The shape the bridge actually stamps, banner and identifiers and all.
    bridge_echo = (
        "How do you define completion?\n\n"
        f"{marker}\n\n{_BRIDGE_NOTICE_OPENING}\n"
        "[Ouroboros] Dispatched 6 subagents. Task widgets will update as they complete.\n"
        "  • Interview advisory: data_context → agent='general' [child=ses_abc]\n\n"
        '```json\n{\n  "question_advisory_fanout_id": "fanout_abc123"\n}\n```'
    )
    assert echo_carries_dispatch(bridge_echo)


def _bridge_echo(issued: str) -> str:
    """The visible text the bridge actually stamps, banner and identifiers and all."""
    from ouroboros.mcp.tools.advisory_dispatch import _BRIDGE_NOTICE_OPENING
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    return (
        f"{issued}\n\n{marker}\n\n{_BRIDGE_NOTICE_OPENING}\n"
        "[Ouroboros] Dispatched 6 subagents. Task widgets will update as they complete.\n"
        "  • Interview advisory: data_context → agent='general' [child=ses_abc]\n\n"
        '```json\n{\n  "question_advisory_fanout_id": "fanout_abc123",\n'
        '  "session_id": "sess-bridge"\n}\n```'
    )


async def test_a_bridge_mutated_echo_does_not_reach_the_plugin_transcript(tmp_path) -> None:
    """The plugin path, with the exact shape the bridge produces.

    **This is the path the bridge's append actually reaches.** `handle()` returns
    to `_handle_plugin_dispatch` before `_handle_answer` is entered, so the
    gatekeeper the subprocess path asks was never on the plugin round-trip — and
    plugin-passive is the only transport where the bridge stamps anything. A
    gatekeeper that recognised the bridge's grammar while sitting on the other
    branch would have read as a fix and persisted the banner anyway.

    So this drives the real handler against a real state directory rather than a
    mocked engine: the plugin path loads and saves state itself, and a mock of
    `InterviewEngine` is simply not consulted here. What the round remembers
    asking is read back off disk.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    issued = "How do you define completion?"
    state = InterviewState(
        interview_id="sess-bridge",
        rounds=[
            InterviewRound(round_number=1, question="Q1?", user_response="A1"),
            InterviewRound(round_number=2, question=issued, user_response=None),
        ],
    )
    (tmp_path / "interview_sess-bridge.json").write_text(state.model_dump_json(), encoding="utf-8")

    handler = InterviewHandler(
        data_dir=tmp_path, agent_runtime_backend="opencode", opencode_mode="plugin"
    )
    result = await handler.handle(
        {"session_id": "sess-bridge", "answer": "A", "last_question": _bridge_echo(issued)}
    )
    assert result.is_ok

    persisted = json.loads((tmp_path / "interview_sess-bridge.json").read_text(encoding="utf-8"))
    recorded = persisted["rounds"][-1]["question"]
    assert recorded == issued
    assert "fanout_abc123" not in recorded
    assert marker not in recorded


async def test_a_bridge_mutated_echo_does_not_reach_the_subprocess_transcript() -> None:
    """The same shape at the other consumer of the same gatekeeper.

    A bridge-shaped echo is not expected here — the bridge runs on plugin-passive
    and this branch serves the subprocess transports. It is pinned anyway because
    the two branches share one predicate, and the value of sharing it is that
    neither can come to answer differently about what a directive looks like.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    issued = "How do you define completion?"
    state = InterviewState(
        interview_id="sess-bridge",
        rounds=[
            InterviewRound(round_number=1, question="Q1?", user_response="A1"),
            InterviewRound(round_number=2, question=issued, user_response=None),
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
        {"session_id": "sess-bridge", "answer": "A", "last_question": _bridge_echo(issued)}
    )

    assert result.is_ok
    recorded = engine.record_response.await_args.args[2]
    assert recorded == issued
    assert "fanout_abc123" not in recorded
    assert marker not in recorded


def test_two_declared_appends_cut_at_the_server_s_own_directive() -> None:
    """When both producers append, the parser must still cut at the server's.

    Declaring the bridge with the shared marker gave the response two markers.
    The parser takes the last one, which used to be the server's because the
    bridge wrote none — so with the bridge declared, a naive last-marker read
    cuts at the *bridge's* notice and leaves the server's own host directive
    standing inside the text the auto driver answers as the question. That is
    the failure `_extract_interview_question` exists to prevent, reintroduced
    by the fix for a different one.

    So each reader names the producers it is asking about: the parser obeys a
    gate that speaks only for the server's directive, and asks only for that
    opening; the echo path accepts either, because it removes nothing.
    """
    from ouroboros.auto.adapters import _extract_interview_question
    from ouroboros.mcp.tools.advisory_dispatch import (
        _BRIDGE_NOTICE_OPENING,
        _HOST_DIRECTIVE_OPENING,
        echo_carries_dispatch,
        split_appended_dispatch,
    )
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    question = "How do you define completion?"
    # On the wire the server renders first and the bridge stamps after it.
    both = (
        f"{question}\n\n"
        f"{marker}\n\n{_HOST_DIRECTIVE_OPENING}spawn 6 subagents:**\n"
        '```json\n{"lanes": ["data_context"]}\n```\n\n'
        f"{marker}\n\n{_BRIDGE_NOTICE_OPENING}\n"
        "[Ouroboros] Dispatched 6 subagents.\n\n"
        '```json\n{"question_advisory_fanout_id": "fanout_abc123"}\n```'
    )

    assert split_appended_dispatch(both) == question
    assert (
        _extract_interview_question(
            f"Session sess-1\n\n{both}", session_id="sess-1", dispatch_appended=True
        )
        == question
    )
    # The echo path still recognises it — it accepts either producer.
    assert echo_carries_dispatch(both)


def test_the_correlation_key_is_stamped_on_every_transport() -> None:
    """It names how a submission is keyed, which no transport is exempt from.

    It used to be written only alongside the host-action cue, so
    ``PLUGIN_PASSIVE`` — the one transport with no cue to write — got neither.
    That did not degrade re-entry there, it removed it: the bridge lifts this
    key by name with no fallback, so the parent redeemed with nothing to key by
    and every submission came back ``correlation_mismatch``.
    """
    for mode in SubagentDispatchMode:
        meta = _advisory_meta(
            mode,
            runtime_backend="opencode" if mode is SubagentDispatchMode.PLUGIN_PASSIVE else "claude",
            opencode_mode="plugin" if mode is SubagentDispatchMode.PLUGIN_PASSIVE else None,
        )
        assert meta["question_advisory_result_correlation_key"] == "context.lane_id", mode
    # The cue itself stays host-only: it tells a host model to fan out, and on
    # the bridge transport nothing reads it.
    plugin_meta = _advisory_meta(
        SubagentDispatchMode.PLUGIN_PASSIVE, runtime_backend="opencode", opencode_mode="plugin"
    )
    assert "question_advisory_host_action" not in plugin_meta


def test_every_advisory_key_the_bridge_lifts_is_one_the_producer_stamps() -> None:
    """The producer's real output against the bridge's real lift list.

    This is the seam the last defect lived in, and the reason it survived a
    green suite: the bridge test builds its metadata by hand, so it proved the
    bridge lifts a key it was handed, never that anything hands it one. The
    producer stamped no correlation key on ``PLUGIN_PASSIVE`` and both sides
    passed.

    That is the third time a fixture stood in for the code it was meant to
    check — after the plugin regression that seeded a round shape plugin mode
    cannot produce, and the module-size gate that does not check its own table
    without ``--baseline-ref``. So neither side of this is written down here:
    the keys come from the bridge source as it stands, and the meta from the
    producer as it runs.

    Scoped to the ``question_advisory_`` keys because the rest of the lift list
    (``session_id``, ``ambiguity_score``, ``milestone``, ``seed_ready``) is
    stamped by the handler around this producer, not by it. It pins names
    rather than a byte-level round trip; the bridge's own suite covers the lift
    itself.
    """
    from pathlib import Path
    import re
    import tempfile

    from ouroboros.mcp.tools.fanout import FanoutRegistry

    # Resolved from this file, not the cwd: a test that reads the repo must not
    # depend on where pytest was invoked from.
    repo_root = Path(__file__).resolve().parents[4]
    source = (repo_root / "src/ouroboros/opencode/plugin/ouroboros-bridge.ts").read_text(
        encoding="utf-8"
    )
    # Anchored on the function, not on the `responseShape` declaration: that
    # line appears in `parse()` too, and splitting on it swallowed the whole of
    # `parse` and only landed right because of the prefix filter below. A
    # parser that reads more than it claims is the thing this test exists to
    # object to.
    body = source.split("export function parseMetadata", 1)
    assert len(body) == 2, "parseMetadata not found — parser is stale, not the code"
    block = body[1].split("]) {", 1)[0]
    lifted = re.findall(r'"([a-z_]+)"', block)
    advisory_lifted = [key for key in lifted if key.startswith("question_advisory_")]
    assert advisory_lifted, "bridge lift list not found — parser is stale, not the code"

    meta: dict[str, object] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-render",
        question="What are the acceptance criteria?",
        phase="answer",
        score=_score(0.35),
        dispatch_mode=SubagentDispatchMode.PLUGIN_PASSIVE,
        runtime_backend="opencode",
        opencode_mode="plugin",
        fanout_registry=FanoutRegistry(Path(tempfile.mkdtemp())),
    )

    missing = [key for key in advisory_lifted if key not in meta]
    assert not missing, f"bridge lifts keys the producer never stamps: {missing}"


async def test_the_advertised_correlation_key_is_the_one_that_redeems(tmp_path) -> None:
    """Three producers of one protocol, compared against each other.

    The capability advertises a correlation key, the response meta stamps one,
    and the registry registers one. They are written in three places and were
    checked in three places, so the capability could say ``lane_id`` while the
    other two said ``context.lane_id`` and every suite stayed green — a host
    that followed the published contract was refused with
    ``correlation_mismatch``.

    So this compares them rather than pinning them: the value comes from the
    real capability blob, the meta from the real producer, the record from the
    real registry, and the submission is keyed with what the capability
    advertises. Nothing here is written down, which is the point — a fourth
    producer that disagrees fails here instead of at a host.
    """
    from ouroboros.mcp.tools.fanout import FanoutRegistry, submit_fanout_results
    from ouroboros.orchestrator.capabilities import ouroboros_tool_capability_metadata

    capability = ouroboros_tool_capability_metadata("ouroboros_interview")
    advertised = capability["orchestration"]["question_advisory_fanout"]["response_payload_refs"][
        "result_correlation_key"
    ]

    registry = FanoutRegistry(tmp_path)
    meta: dict[str, object] = {}
    _attach_question_assist_requests(
        meta,
        session_id="sess-agree",
        question="What are the acceptance criteria?",
        phase="answer",
        score=_score(0.35),
        dispatch_mode=SubagentDispatchMode.PLUGIN_PASSIVE,
        runtime_backend="opencode",
        opencode_mode="plugin",
        fanout_registry=registry,
    )

    stamped = meta["question_advisory_result_correlation_key"]
    fanout_id = str(meta["question_advisory_fanout_id"])
    record = registry.load(fanout_id)
    assert record is not None

    assert advertised == stamped, "the capability advertises a key the response does not stamp"
    assert advertised == record.correlation_key, (
        "the capability advertises a key the registry did not register"
    )

    # And the whole point of agreeing: a host that follows the advertisement
    # redeems. Keyed with `advertised`, never with a literal.
    result = submit_fanout_results(
        registry,
        fanout_id=fanout_id,
        session_id="sess-agree",
        correlation_key=str(advertised),
        results=[{"key": lane, "undispatched": True} for lane in record.expected_keys],
    )
    assert result["status"] == "complete", result


async def test_a_driven_plugin_interview_records_only_the_question(tmp_path) -> None:
    """Driven ``start → answer``, never by hand-built state.

    The sibling regression above seeds a question-only pending round, and that
    is the one shape plugin mode does not produce: the child asks the questions
    and the server only records answers, so ``record_answer`` appends a whole
    round and ``has_pending`` is false on every turn. The gate written for the
    pending branch was therefore never reached here, and the test meant to
    prove otherwise passed because its fixture stood in for a state the
    transport cannot reach.

    So this one asks the handler for a session and answers it three times,
    reading each round back off disk.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    issued = "How do you define completion?"
    handler = InterviewHandler(
        data_dir=tmp_path, agent_runtime_backend="opencode", opencode_mode="plugin"
    )
    started = await handler.handle(
        {
            "action": "start",
            "initial_context": "Build a data-aware interview lane for Ouroboros.",
            "cwd": str(tmp_path),
        }
    )
    assert started.is_ok
    session_id = started.value.meta["session_id"]
    state_file = tmp_path / f"interview_{session_id}.json"

    for turn in range(3):
        before = json.loads(state_file.read_text(encoding="utf-8"))["rounds"]
        assert not (before and before[-1]["user_response"] is None), (
            "plugin mode is not expected to persist a question-only round"
        )
        result = await handler.handle(
            {
                "session_id": session_id,
                "answer": f"A{turn}",
                "last_question": _bridge_echo(issued),
            }
        )
        assert result.is_ok

    for round_data in json.loads(state_file.read_text(encoding="utf-8"))["rounds"]:
        assert round_data["question"] == issued
        assert marker not in round_data["question"]
        assert "fanout_abc123" not in round_data["question"]


def test_a_question_quoting_a_host_directive_survives_the_plugin_strip() -> None:
    """The cut is scoped to the producer that actually appends here.

    Asking for both openings would cut at a quoted host directive — and an
    interview about this feature quotes one. This module appends nothing on
    ``PLUGIN_PASSIVE``, so the bridge is the only producer that can have
    written there, and a question carrying someone else's grammar is left
    whole.
    """
    from ouroboros.mcp.tools.advisory_dispatch import (
        _BRIDGE_NOTICE_OPENING,
        _HOST_DIRECTIVE_OPENING,
        strip_bridge_notice,
    )
    from ouroboros.mcp.tools.advisory_dispatch import (
        QUESTION_ADVISORY_DISPATCH_MARKER as marker,
    )

    quoting = (
        "Two things. First, is this directive too verbose?\n\n"
        f"{marker}\n\n{_HOST_DIRECTIVE_OPENING}spawn 6 subagents:**\nkeep it visible.\n\n"
        "Second, should the bare sentinel appear in user-visible text?"
    )
    bridge = (
        f"{marker}\n\n{_BRIDGE_NOTICE_OPENING}\n[Ouroboros] Dispatched 6 subagents.\n\n"
        '```json\n{"question_advisory_fanout_id": "fanout_quoted"}\n```'
    )

    assert strip_bridge_notice(f"{quoting}\n\n{bridge}") == quoting
    assert strip_bridge_notice(quoting) == quoting
