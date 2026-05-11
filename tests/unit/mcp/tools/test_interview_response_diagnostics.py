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
        len("What should this tool do?")
        + len("It creates reports.")
        + len("Who uses it first?")
    )
    assert diagnostic.data["ambiguity_prefix_present"] is False
    assert diagnostic.data["is_length_guard"] is False
