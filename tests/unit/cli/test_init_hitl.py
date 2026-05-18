from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.bigbang.interview import InterviewState
from ouroboros.cli.commands.init import (
    _append_hitl_events,
    _interview_hitl_request,
    _interview_hitl_response,
)
from ouroboros.events.hitl import create_hitl_answered_event, create_hitl_requested_event


class FakeEventStore:
    def __init__(self) -> None:
        self.batches: list[list[object]] = []

    async def append_batch(self, events):
        self.batches.append(list(events))


def test_interview_hitl_request_response_contract() -> None:
    state = InterviewState(interview_id="interview_123", initial_context="Build a CLI")
    created_at = datetime(2026, 5, 18, tzinfo=UTC)
    request = _interview_hitl_request(
        state,
        round_number=2,
        question="What should it do?",
        created_at=created_at,
    )

    assert request.request_id == "hitl_interview_interview_123_2"
    assert request.session_id == "interview_123"
    assert request.run_id == "interview_123"
    assert request.invocation_id == "interview-round-2"
    assert request.source.value == "interview"
    assert request.kind.value == "free_text"
    assert request.resume_target == "init:interview:interview_123:round:2"

    response = _interview_hitl_response(request, "It should lint files.", received_at=created_at)
    event = create_hitl_answered_event(request, response)
    assert event.type == "hitl.answered"
    assert event.data["text"] == "It should lint files."


@pytest.mark.asyncio
async def test_append_hitl_events_is_noop_without_event_store() -> None:
    await _append_hitl_events(None, [])


@pytest.mark.asyncio
async def test_append_hitl_events_persists_requested_event() -> None:
    state = InterviewState(interview_id="interview_123")
    request = _interview_hitl_request(state, round_number=1, question="Q?")
    event = create_hitl_requested_event(request)
    store = FakeEventStore()

    await _append_hitl_events(store, [event])

    assert len(store.batches) == 1
    assert store.batches[0][0].type == "hitl.requested"
