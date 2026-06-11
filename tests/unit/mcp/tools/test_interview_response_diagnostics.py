"""Diagnostic-event regression for Q00/ouroboros#831 (follow-up to PR #834).

The ``InterviewHandler`` must emit an ``interview.response.emitted`` event
every time it returns an MCP response that carries an interview question.
The event payload captures response-shape characteristics (payload size,
transcript pressure, prefix presence, length-guard flag) so a later
investigation can correlate hang reports with response shape.  Pure
observability -- no behaviour change.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
import pytest

from ouroboros.bigbang.interview import (
    INITIAL_CONTEXT_SUMMARY_QUESTION,
    InterviewRound,
    InterviewState,
    InterviewStatus,
)
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.mcp.tools.subagent import synthesize_code_investigation_when_complete
from ouroboros.orchestrator.capabilities import (
    interview_code_investigation_answer_contract,
    stable_code_investigation_question_identity,
)


@dataclass(slots=True)
class _CapturingEventStore:
    """In-memory event sink that records every BaseEvent appended.

    Mirrors the surface of ``ouroboros.persistence.event_store.EventStore``
    that ``InterviewHandler._emit_event`` actually touches: an async
    ``initialize`` (idempotent) and an async ``append``.  Anything beyond
    that the production store offers is intentionally not stubbed.
    """

    events: list[BaseEvent] = field(default_factory=list)
    _initialized: bool = False

    async def initialize(self) -> None:
        self._initialized = True

    async def append(self, event: BaseEvent) -> None:
        self.events.append(event)


@dataclass(slots=True)
class _StubInterviewEngine:
    """Minimal engine: returns whatever question we configure for the next turn."""

    state_dir: Path
    next_question: str = "What is the primary user persona?"
    initial_state: InterviewState | None = None
    saved_states: list[InterviewState] = field(default_factory=list)
    record_calls: list[dict[str, str]] = field(default_factory=list)

    async def start_interview(
        self,
        initial_context: str,
        cwd: str | None = None,
        interview_id: str | None = None,
    ) -> Result[InterviewState, MCPServerError]:
        sid = interview_id or "interview_diagnostics00001"
        state = InterviewState(
            interview_id=sid,
            initial_context=initial_context,
            status=InterviewStatus.IN_PROGRESS,
        )
        await self.save_state(state)
        return Result.ok(state)

    async def ask_next_question(self, state: InterviewState) -> Result[str, MCPServerError]:
        return Result.ok(self.next_question)

    async def record_response(
        self,
        state: InterviewState,
        user_response: str,
        question: str,
    ) -> Result[InterviewState, MCPServerError]:
        self.record_calls.append({"question": question, "answer": user_response})
        state.rounds.append(
            InterviewRound(
                round_number=state.current_round_number,
                question=question,
                user_response=user_response,
            )
        )
        state.mark_updated()
        return Result.ok(state)

    async def save_state(self, state: InterviewState) -> Result[Path, MCPServerError]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        path = self.state_dir / f"interview_{state.interview_id}.json"
        path.write_text(
            json.dumps({"interview_id": state.interview_id}),
            encoding="utf-8",
        )
        self.saved_states.append(state)
        return Result.ok(path)

    async def load_state(self, session_id: str) -> Result[InterviewState, MCPServerError]:
        if self.initial_state is None:
            raise NotImplementedError
        # Always return the same canonical state object.
        return Result.ok(self.initial_state)


async def _drain_bg_tasks(handler: InterviewHandler) -> None:
    """Flush the handler's fire-and-forget event tasks deterministically."""
    if handler._bg_tasks:
        await asyncio.gather(*handler._bg_tasks, return_exceptions=True)


def _find_event(events: list[BaseEvent], *, event_type: str) -> BaseEvent | None:
    for event in events:
        if event.type == event_type:
            return event
    return None


def _assert_reasoning_meta(meta: dict[str, Any], *, phase: str, session_id: str) -> None:
    assert meta["interview_reasoning"]["phase"] == phase
    assert meta["interview_reasoning"]["session_id"] == session_id
    assert meta["interview_reasoning"]["next_action"]
    assert isinstance(meta["internal_reasoning"], list)
    assert f"phase: {phase}" in meta["internal_reasoning"]
    assert f"session: {session_id}" in meta["internal_reasoning"]


def _assert_code_investigation_request(
    meta: dict[str, Any],
    *,
    session_id: str,
    question: str,
) -> None:
    request = meta["code_investigation_request"]
    assert request["session_id"] == session_id
    assert request["question"] == question
    assert request["question_identity"] == stable_code_investigation_question_identity(question)
    assert request["investigation_goal"] == "describe_current_state_from_code"
    assert request["investigation_targets"] == [{"target_type": "workspace", "scope": "active"}]
    assert "configuration" in request["fact_categories"]
    assert request["allowed_capabilities"] == ["inspect_code"]
    repo_tool_capabilities = request["repo_inspection_tool_capabilities"]
    repo_tool_by_name = {tool["tool_name"]: tool for tool in repo_tool_capabilities}
    assert set(repo_tool_by_name) == {"Read", "Glob", "Grep"}
    for tool_name, tool_capability in repo_tool_by_name.items():
        assert tool_capability["stable_id"] == f"builtin:{tool_name}"
        assert tool_capability["source_kind"] == "builtin"
        assert tool_capability["execution_mode"] == "repo_inspection"
        assert tool_capability["logical_capability"] == "inspect_code"
        assert tool_capability["mutation_class"] == "read_only"
        assert tool_capability["side_effects"] == ["side_effect_free"]
        assert tool_capability["fallback_used"] is False
        Draft202012Validator.check_schema(tool_capability["input_schema"])
    assert repo_tool_by_name["Read"]["input_schema"]["required"] == ["file_path"]
    assert repo_tool_by_name["Glob"]["input_schema"]["required"] == ["pattern"]
    assert repo_tool_by_name["Grep"]["input_schema"]["required"] == ["pattern"]
    assert request["answer_prefixes"] == ["[from-code]", "[from-code][auto-confirmed]"]
    assert request["answer_contract"] == interview_code_investigation_answer_contract()
    capability = request["mcp_tool_capability"]
    assert capability["tool_name"] == "ouroboros_interview"
    assert capability["source_kind"] == "attached_mcp"
    assert capability["source_name"] == "ouroboros"
    assert capability["fallback_used"] is False
    assert capability["execution_mode"] == "subagent_orchestration"
    assert set(capability) >= {
        "input_schema",
        "execution_mode",
        "companions",
        "required_context_keys",
        "side_effects",
        "retry",
        "interrupt",
        "cancel",
    }
    request_schema = capability["orchestration"]["code_investigation"]["request_model_schema"]
    Draft202012Validator(request_schema).validate(request)


@pytest.mark.asyncio
async def test_code_investigation_results_are_collected_before_synthesis(
    tmp_path: Path,
) -> None:
    """Code-fact subagent output is collected and passed to synthesis."""
    engine = _StubInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=_CapturingEventStore(),
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {"initial_context": "Build a CLI", "cwd": str(tmp_path)},
    )
    assert outcome.is_ok
    request = outcome.value.meta["code_investigation_request"]
    response_schema = request["answer_contract"]["response_model_schema"]
    code_fact_output = {
        "session_id": request["session_id"],
        "question_identity": request["question_identity"],
        "answer_prefix": "[from-code][auto-confirmed]",
        "answer_text": "The current project is a Python package managed by pyproject.toml.",
        "confidence": "high_exact_match",
        "evidence": [
            {
                "source": "pyproject.toml",
                "locator": "project.name",
                "claim": "The manifest declares the Python project metadata.",
            }
        ],
        "requires_user_confirmation": False,
    }
    Draft202012Validator(response_schema).validate(code_fact_output)

    synthesis_calls: list[list[dict[str, Any]]] = []

    def synthesize(aggregated_outputs: list[dict[str, Any]]) -> dict[str, Any]:
        synthesis_calls.append(aggregated_outputs)
        collected = aggregated_outputs[0]["output"]
        return {
            "answer": f"{collected['answer_prefix']} {collected['answer_text']}",
            "evidence_sources": [evidence["source"] for evidence in collected["evidence"]],
        }

    partial = synthesize_code_investigation_when_complete(
        request,
        {
            "code_facts": {
                **code_fact_output,
                "question_identity": stable_code_investigation_question_identity(
                    "a different question"
                ),
            }
        },
        synthesize,
    )
    assert partial["ready_for_synthesis"] is False
    assert partial["ready_for_forward"] is False
    assert partial["requires_user_confirmation"] is False
    assert partial["confirmation_required_result_ids"] == []
    assert partial["user_confirmation_prompts"] == []
    assert partial["missing_result_ids"] == ["code_facts"]
    assert partial["aggregated_outputs"] == []
    assert partial["synthesis"] is None
    assert synthesis_calls == []

    complete = synthesize_code_investigation_when_complete(
        request,
        {"code_facts": code_fact_output},
        synthesize,
    )
    assert complete["ready_for_synthesis"] is True
    assert complete["ready_for_forward"] is True
    assert complete["requires_user_confirmation"] is False
    assert complete["confirmation_required_result_ids"] == []
    assert complete["user_confirmation_prompts"] == []
    assert complete["missing_result_ids"] == []
    assert complete["aggregated_outputs"] == [
        {"result_id": "code_facts", "output": code_fact_output}
    ]
    assert complete["synthesis"] == {
        "answer": (
            "[from-code][auto-confirmed] The current project is a Python "
            "package managed by pyproject.toml."
        ),
        "evidence_sources": ["pyproject.toml"],
    }
    assert synthesis_calls == [[{"result_id": "code_facts", "output": code_fact_output}]]


def test_code_investigation_synthesis_fails_closed_for_stale_from_code_output() -> None:
    """A stale inferred [from-code] answer cannot become forwardable by flag drift."""
    question_identity = stable_code_investigation_question_identity("Which framework is used?")
    request = {
        "session_id": "sess-123",
        "question_identity": question_identity,
        "answer_contract": interview_code_investigation_answer_contract(),
    }
    stale_output = {
        "session_id": "sess-123",
        "question_identity": question_identity,
        "answer_prefix": "[from-code]",
        "answer_text": "The project appears to use FastAPI.",
        "confidence": "medium_inferred",
        "evidence": [
            {
                "source": "src/app.py",
                "locator": "imports",
                "claim": "The inspected imports resemble a FastAPI application.",
            }
        ],
        "requires_user_confirmation": False,
    }
    synthesis_calls: list[list[dict[str, Any]]] = []

    result = synthesize_code_investigation_when_complete(
        request,
        {"code_facts": stale_output},
        lambda outputs: synthesis_calls.append(outputs) or {"answer": "unsafe"},
    )

    assert result["ready_for_synthesis"] is True
    assert result["ready_for_forward"] is False
    assert result["requires_user_confirmation"] is True
    assert result["confirmation_required_result_ids"] == ["code_facts"]
    assert result["synthesis"] is None
    assert result["contract_violations"][0]["result_id"] == "code_facts"
    assert any("True was expected" in error for error in result["contract_violations"][0]["errors"])
    assert result["user_confirmation_prompts"] == [
        "Confirm before forwarding this code-derived answer: The project appears to use FastAPI."
    ]
    assert synthesis_calls == []


def test_code_investigation_synthesis_requires_confirmation_for_from_code_prefix() -> None:
    question_identity = stable_code_investigation_question_identity("Is there a router?")
    request = {
        "session_id": "sess-123",
        "question_identity": question_identity,
        "answer_contract": interview_code_investigation_answer_contract(),
    }
    output = {
        "session_id": "sess-123",
        "question_identity": question_identity,
        "answer_prefix": "[from-code]",
        "answer_text": "No router was found in the inspected files.",
        "confidence": "medium_inferred",
        "evidence": [
            {
                "source": "rg --files",
                "locator": "workspace root",
                "claim": "No router file was found during repository inspection.",
            }
        ],
        "requires_user_confirmation": True,
        "user_confirmation_prompt": "Confirm whether this repository has no router.",
    }

    result = synthesize_code_investigation_when_complete(
        request,
        {"code_facts": output},
        lambda outputs: {"answer": outputs[0]["output"]["answer_text"]},
    )

    assert result["ready_for_synthesis"] is True
    assert result["ready_for_forward"] is False
    assert result["requires_user_confirmation"] is True
    assert result["confirmation_required_result_ids"] == ["code_facts"]
    assert result["user_confirmation_prompts"] == ["Confirm whether this repository has no router."]
    assert result["contract_violations"] == []
    assert result["synthesis"] == {"answer": "No router was found in the inspected files."}


def test_code_investigation_synthesis_forwards_auto_confirmed_output() -> None:
    question_identity = stable_code_investigation_question_identity("Which manifest exists?")
    request = {
        "session_id": "sess-123",
        "question_identity": question_identity,
        "answer_contract": interview_code_investigation_answer_contract(),
    }
    output = {
        "session_id": "sess-123",
        "question_identity": question_identity,
        "answer_prefix": "[from-code][auto-confirmed]",
        "answer_text": "pyproject.toml declares the package metadata.",
        "confidence": "high_exact_match",
        "evidence": [
            {
                "source": "pyproject.toml",
                "locator": "project.name",
                "claim": "The package name is declared in pyproject.toml.",
            }
        ],
        "requires_user_confirmation": False,
    }

    result = synthesize_code_investigation_when_complete(
        request,
        {"code_facts": output},
        lambda outputs: {"answer": outputs[0]["output"]["answer_text"]},
    )

    assert result["ready_for_synthesis"] is True
    assert result["ready_for_forward"] is True
    assert result["requires_user_confirmation"] is False
    assert result["confirmation_required_result_ids"] == []
    assert result["user_confirmation_prompts"] == []
    assert result["contract_violations"] == []
    assert result["synthesis"] == {"answer": "pyproject.toml declares the package metadata."}


@pytest.mark.asyncio
async def test_interview_answer_records_only_after_code_investigation_synthesis(
    tmp_path: Path,
) -> None:
    """The parent runtime must submit only auto-confirmed synthesized answers.

    ``InterviewHandler`` exposes code-investigation metadata and records whatever
    answer the parent runtime later submits. This test pins the orchestration
    boundary: incomplete or confirmation-required investigation output does not
    call the answer path, and only the auto-confirmed synthesis is recorded.
    """
    engine = _StubInterviewEngine(state_dir=tmp_path, next_question="Which framework is used?")
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=_CapturingEventStore(),
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    start = await handler.handle({"initial_context": "Audit the current app"})
    assert start.is_ok
    request = start.value.meta["code_investigation_request"]
    request["required_result_ids"] = ["manifest", "router"]

    contract = request["answer_contract"]
    response_schema = contract["response_model_schema"]
    manifest_output = {
        "session_id": request["session_id"],
        "question_identity": request["question_identity"],
        "answer_prefix": "[from-code][auto-confirmed]",
        "answer_text": "The manifest declares a Python package.",
        "confidence": "high_exact_match",
        "evidence": [
            {
                "source": "pyproject.toml",
                "locator": "project.name",
                "claim": "The project metadata is declared in pyproject.toml.",
            }
        ],
        "requires_user_confirmation": False,
    }
    router_output = {
        "session_id": request["session_id"],
        "question_identity": request["question_identity"],
        "answer_prefix": "[from-code]",
        "answer_text": "No web router is present in the inspected files.",
        "confidence": "medium_inferred",
        "evidence": [
            {
                "source": "rg --files",
                "locator": "workspace root",
                "claim": "No frontend router file was found in the fixture workspace.",
            }
        ],
        "requires_user_confirmation": True,
        "user_confirmation_prompt": (
            "Confirm whether the missing web router means this is not a web app."
        ),
    }
    Draft202012Validator(response_schema).validate(manifest_output)
    Draft202012Validator(response_schema).validate(router_output)

    ordering: list[str] = []

    def synthesize(aggregated_outputs: list[dict[str, Any]]) -> dict[str, Any]:
        ordering.append("synthesized")
        answer_lines = [item["output"]["answer_text"] for item in aggregated_outputs]
        return {
            "answer": "[from-code] " + " ".join(answer_lines),
            "evidence_sources": [
                evidence["source"]
                for item in aggregated_outputs
                for evidence in item["output"]["evidence"]
            ],
        }

    partial = synthesize_code_investigation_when_complete(
        request,
        {"manifest": manifest_output},
        synthesize,
    )
    assert partial["ready_for_synthesis"] is False
    assert partial["ready_for_forward"] is False
    assert partial["synthesis"] is None
    assert ordering == []
    assert engine.record_calls == []

    complete = synthesize_code_investigation_when_complete(
        request,
        {"manifest": manifest_output, "router": router_output},
        synthesize,
    )
    assert complete["ready_for_synthesis"] is True
    assert complete["ready_for_forward"] is False
    assert complete["requires_user_confirmation"] is True
    assert complete["confirmation_required_result_ids"] == ["router"]
    assert complete["user_confirmation_prompts"] == [
        "Confirm whether the missing web router means this is not a web app."
    ]
    assert complete["synthesis"]["answer"] == (
        "[from-code] The manifest declares a Python package. "
        "No web router is present in the inspected files."
    )
    assert ordering == ["synthesized"]
    assert engine.record_calls == []

    auto_confirmed = synthesize_code_investigation_when_complete(
        request,
        {"manifest": manifest_output},
        synthesize,
    )
    assert auto_confirmed["ready_for_synthesis"] is False

    request["required_result_ids"] = ["manifest"]
    auto_confirmed = synthesize_code_investigation_when_complete(
        request,
        {"manifest": manifest_output},
        synthesize,
    )
    assert auto_confirmed["ready_for_synthesis"] is True
    assert auto_confirmed["ready_for_forward"] is True
    synthesized_answer = auto_confirmed["synthesis"]["answer"]

    state = engine.saved_states[-1]
    engine.initial_state = state
    engine.next_question = "What should acceptance cover?"
    answer = await handler.handle(
        {
            "session_id": request["session_id"],
            "answer": synthesized_answer,
            "last_question": request["question"],
        }
    )

    assert answer.is_ok
    assert ordering == ["synthesized", "synthesized"]
    assert engine.record_calls == [
        {
            "question": "Which framework is used?",
            "answer": synthesized_answer,
        }
    ]
    recorded_round = state.rounds[0]
    assert recorded_round.question == "Which framework is used?"
    assert recorded_round.user_response == synthesized_answer
    assert "pyproject.toml" in auto_confirmed["synthesis"]["evidence_sources"]


@pytest.mark.asyncio
async def test_start_emits_response_diagnostic_event(tmp_path: Path) -> None:
    """Start path: a normal first question emits the response.emitted event."""
    event_store = _CapturingEventStore()
    engine = _StubInterviewEngine(state_dir=tmp_path)
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=event_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {"initial_context": "Build a CLI", "cwd": str(tmp_path)},
    )
    assert outcome.is_ok
    _assert_reasoning_meta(
        outcome.value.meta, phase="start", session_id="interview_diagnostics00001"
    )
    _assert_code_investigation_request(
        outcome.value.meta,
        session_id="interview_diagnostics00001",
        question="What is the primary user persona?",
    )
    assert outcome.value.meta["interview_reasoning"]["pending_question"] is True
    assert outcome.value.meta["interview_reasoning"]["question_chars"] == len(
        "What is the primary user persona?"
    )
    await _drain_bg_tasks(handler)

    diagnostic = _find_event(event_store.events, event_type="interview.response.emitted")
    assert diagnostic is not None, "start path must emit interview.response.emitted"

    data: dict[str, Any] = diagnostic.data
    assert data["response_kind"] == "start"
    assert data["round_number"] == 1, "start path should fire after the pending round is appended"
    assert data["payload_chars"] > 0
    assert data["transcript_chars"] >= 0
    assert isinstance(data["ambiguity_prefix_present"], bool)
    assert data["is_length_guard"] is False
    assert diagnostic.aggregate_id == "interview_diagnostics00001"


@pytest.mark.asyncio
async def test_start_with_length_guard_question_marks_event(tmp_path: Path) -> None:
    """Start path: when the engine returns the length-guard meta-directive, the
    event must carry ``is_length_guard=True``.  This is what a future analysis
    will use to distinguish the two response shapes without re-parsing text.
    """
    event_store = _CapturingEventStore()
    engine = _StubInterviewEngine(
        state_dir=tmp_path,
        next_question=INITIAL_CONTEXT_SUMMARY_QUESTION,
    )
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=event_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {"initial_context": "Build a CLI", "cwd": str(tmp_path)},
    )
    assert outcome.is_ok
    assert "code_investigation_request" not in outcome.value.meta
    await _drain_bg_tasks(handler)

    diagnostic = _find_event(event_store.events, event_type="interview.response.emitted")
    assert diagnostic is not None
    assert diagnostic.data["is_length_guard"] is True
    assert diagnostic.data["response_kind"] == "start"


@pytest.mark.asyncio
async def test_resume_pending_emits_response_diagnostic_event(tmp_path: Path) -> None:
    """Resume path (session_id only, no answer, pending round)."""
    pending_state = InterviewState(
        interview_id="interview_resume00000001",
        initial_context="ctx",
        status=InterviewStatus.IN_PROGRESS,
    )
    pending_state.rounds.append(
        InterviewRound(
            round_number=1,
            question="What is the main goal?",
            user_response=None,
        )
    )

    event_store = _CapturingEventStore()
    engine = _StubInterviewEngine(state_dir=tmp_path, initial_state=pending_state)
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=event_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle({"session_id": pending_state.interview_id})
    assert outcome.is_ok
    _assert_reasoning_meta(
        outcome.value.meta,
        phase="resume_pending",
        session_id=pending_state.interview_id,
    )
    _assert_code_investigation_request(
        outcome.value.meta,
        session_id=pending_state.interview_id,
        question="What is the main goal?",
    )
    assert outcome.value.meta["interview_reasoning"]["pending_question"] is True
    await _drain_bg_tasks(handler)

    diagnostic = _find_event(event_store.events, event_type="interview.response.emitted")
    assert diagnostic is not None
    assert diagnostic.data["response_kind"] == "resume_pending"
    assert diagnostic.data["round_number"] == 1
    assert diagnostic.data["is_length_guard"] is False
    # Transcript chars must include the pending question text length.
    assert diagnostic.data["transcript_chars"] >= len("What is the main goal?")


@pytest.mark.asyncio
async def test_resume_pending_ambiguity_prefix_reflects_full_response_text(
    tmp_path: Path,
) -> None:
    """The diagnostic flag is about the emitted body, not the embedded question."""
    pending_state = InterviewState(
        interview_id="interview_resume00000002",
        initial_context="ctx",
        status=InterviewStatus.IN_PROGRESS,
        ambiguity_score=0.42,
    )
    pending_state.rounds.append(
        InterviewRound(
            round_number=1,
            question="What is the main goal?",
            user_response=None,
        )
    )

    event_store = _CapturingEventStore()
    engine = _StubInterviewEngine(state_dir=tmp_path, initial_state=pending_state)
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=event_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle({"session_id": pending_state.interview_id})
    assert outcome.is_ok
    response_text = outcome.value.content[0].text
    assert response_text.startswith("Session ")
    assert "(ambiguity: 0.42)" in response_text
    await _drain_bg_tasks(handler)

    diagnostic = _find_event(event_store.events, event_type="interview.response.emitted")
    assert diagnostic is not None
    assert diagnostic.data["ambiguity_prefix_present"] is False


@pytest.mark.asyncio
async def test_answer_emits_response_diagnostic_event(tmp_path: Path) -> None:
    """Answer path: recording an answer and returning the next question emits diagnostics."""
    pending_state = InterviewState(
        interview_id="interview_answer00000001",
        initial_context="ctx",
        status=InterviewStatus.IN_PROGRESS,
    )
    pending_state.rounds.append(
        InterviewRound(
            round_number=1,
            question="What should this tool do?",
            user_response=None,
        )
    )

    event_store = _CapturingEventStore()
    engine = _StubInterviewEngine(
        state_dir=tmp_path,
        initial_state=pending_state,
        next_question="Who uses it first?",
    )
    handler = InterviewHandler(
        interview_engine=engine,
        event_store=event_store,
        agent_runtime_backend=None,
        opencode_mode=None,
        data_dir=tmp_path,
    )

    outcome = await handler.handle(
        {"session_id": pending_state.interview_id, "answer": "It creates reports."}
    )
    assert outcome.is_ok
    _assert_reasoning_meta(
        outcome.value.meta,
        phase="answer",
        session_id=pending_state.interview_id,
    )
    _assert_code_investigation_request(
        outcome.value.meta,
        session_id=pending_state.interview_id,
        question="Who uses it first?",
    )
    assert outcome.value.meta["interview_reasoning"]["answered_rounds"] == 1
    assert outcome.value.content[0].text == (
        f"Session {pending_state.interview_id}\n\nWho uses it first?"
    )
    await _drain_bg_tasks(handler)

    diagnostic = _find_event(event_store.events, event_type="interview.response.emitted")
    assert diagnostic is not None
    assert diagnostic.data["response_kind"] == "answer"
    assert diagnostic.data["round_number"] == 2
    assert diagnostic.data["payload_chars"] == len(outcome.value.content[0].text)
    assert diagnostic.data["transcript_chars"] == (
        len("What should this tool do?") + len("It creates reports.") + len("Who uses it first?")
    )
    assert diagnostic.data["ambiguity_prefix_present"] is False
    assert diagnostic.data["is_length_guard"] is False
