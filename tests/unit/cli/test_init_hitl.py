from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.cli.commands.init import (
    _append_hitl_events,
    _interview_hitl_request,
    _interview_hitl_response,
    _run_interview_loop,
)
from ouroboros.core.types import Result
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


@pytest.mark.asyncio
async def test_empty_interview_response_retries_same_hitl_request_without_cancelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeEngine:
        def __init__(self) -> None:
            self.recorded: list[tuple[int, str, str]] = []
            self.saved: list[InterviewState] = []

        async def ask_next_question(self, state: InterviewState):
            return Result.ok("What should it do?")

        async def record_response(
            self,
            state: InterviewState,
            user_response: str,
            question: str,
        ):
            self.recorded.append((state.current_round_number, user_response, question))
            state.rounds.append(
                InterviewRound(
                    round_number=state.current_round_number,
                    question=question,
                    user_response=user_response,
                )
            )
            state.status = InterviewStatus.COMPLETED
            return Result.ok(state)

        async def save_state(self, state: InterviewState):
            self.saved.append(state)
            return Result.ok(None)

    responses = iter(["   ", "Useful answer"])

    async def fake_prompt(_prompt: str) -> str:
        return next(responses)

    monkeypatch.setattr("ouroboros.cli.commands.init.multiline_prompt_async", fake_prompt)

    state = InterviewState(interview_id="interview_123")
    store = FakeEventStore()
    engine = FakeEngine()

    final_state = await _run_interview_loop(engine, state, event_store=store)

    events = [event for batch in store.batches for event in batch]
    assert [event.type for event in events] == [
        "hitl.requested",
        "hitl.requested",
        "hitl.answered",
    ]
    assert {event.data["request_id"] for event in events} == {"hitl_interview_interview_123_1"}
    assert all(event.type != "hitl.cancelled" for event in events)
    assert engine.recorded == [(1, "Useful answer", "What should it do?")]
    assert final_state.is_complete
