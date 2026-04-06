from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.openclaw.adapter import OpenClawAdapterResponse
from ouroboros.openclaw.contracts import OpenClawChannelEvent
from ouroboros.openclaw.orchestrator import OpenClawWorkflowOrchestrator


class FakeAdapter:
    def __init__(self, responses: list[OpenClawAdapterResponse]) -> None:
        self.responses = responses
        self.handle_calls: list[OpenClawChannelEvent] = []
        self.dispatch_calls: list[dict[str, Any]] = []

    async def handle_event(self, event: OpenClawChannelEvent):
        self.handle_calls.append(event)
        return Result.ok(self.responses[0])

    async def dispatch(self, command):
        self.dispatch_calls.append(command.to_tool_arguments())
        if len(self.responses) == 1:
            return Result.ok(self.responses[0])
        next_response = self.responses.pop(1)
        return Result.ok(next_response)


class FakeSink:
    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    async def send_reply(
        self,
        *,
        channel_id: str,
        guild_id: str | None,
        text: str,
        meta: dict[str, Any],
    ) -> None:
        self.messages.append((text, meta))


@pytest.mark.asyncio
async def test_orchestrator_sends_initial_reply_without_poll_for_interviewing() -> None:
    adapter = FakeAdapter(
        [
            OpenClawAdapterResponse(
                reply_text="Interview in progress",
                meta={"stage": "interviewing"},
            )
        ]
    )
    sink = FakeSink()
    orchestrator = OpenClawWorkflowOrchestrator(
        adapter=adapter,
    )

    result = await orchestrator.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="work on feature x",
        ),
        sink,
    )

    assert result.is_ok
    assert len(sink.messages) == 1
    assert adapter.dispatch_calls == []


@pytest.mark.asyncio
async def test_orchestrator_waits_until_completed() -> None:
    adapter = FakeAdapter(
        [
            OpenClawAdapterResponse(
                reply_text="Execution started",
                meta={"stage": "executing"},
            ),
            OpenClawAdapterResponse(
                reply_text="Still running",
                meta={"stage": "executing", "job_status": "running"},
            ),
            OpenClawAdapterResponse(
                reply_text="Done with draft PR",
                meta={"stage": "completed"},
            ),
        ]
    )
    sink = FakeSink()
    orchestrator = OpenClawWorkflowOrchestrator(
        adapter=adapter,
        wait_timeout_seconds=0,
        max_waits=5,
    )

    result = await orchestrator.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="goal: demo",
        ),
        sink,
    )

    assert result.is_ok
    assert result.value.reply_text == "Done with draft PR"
    assert result.value.meta["stage"] == "completed"
    assert len(sink.messages) == 3
    assert adapter.dispatch_calls[0]["action"] == "wait"


@pytest.mark.asyncio
async def test_orchestrator_deduplicates_identical_wait_messages() -> None:
    adapter = FakeAdapter(
        [
            OpenClawAdapterResponse(
                reply_text="Execution started",
                meta={"stage": "executing"},
            ),
            OpenClawAdapterResponse(
                reply_text="Execution started",
                meta={"stage": "executing", "job_status": "running"},
            ),
            OpenClawAdapterResponse(
                reply_text="Done",
                meta={"stage": "completed"},
            ),
        ]
    )
    sink = FakeSink()
    orchestrator = OpenClawWorkflowOrchestrator(
        adapter=adapter,
        wait_timeout_seconds=0,
        max_waits=5,
    )

    result = await orchestrator.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="goal: demo",
        ),
        sink,
    )

    assert result.is_ok
    assert [text for text, _ in sink.messages] == ["Execution started", "Done"]


@pytest.mark.asyncio
async def test_orchestrator_keeps_waiting_after_auto_started_execution() -> None:
    adapter = FakeAdapter(
        [
            OpenClawAdapterResponse(
                reply_text="Execution started",
                meta={"stage": "executing"},
            ),
            OpenClawAdapterResponse(
                reply_text="First workflow done\n\nStarted next queued workflow:\n\nExecution started for next",
                meta={
                    "stage": "executing",
                    "workflow_id": "next-workflow",
                    "job_status": "running",
                    "next_workflow_started": True,
                },
            ),
            OpenClawAdapterResponse(
                reply_text="Next workflow complete",
                meta={"stage": "completed"},
            ),
        ]
    )
    sink = FakeSink()
    orchestrator = OpenClawWorkflowOrchestrator(
        adapter=adapter,
        wait_timeout_seconds=0,
        max_waits=5,
    )

    result = await orchestrator.handle_event(
        OpenClawChannelEvent(
            channel_id="c1",
            guild_id="g1",
            user_id="u1",
            message="goal: demo",
        ),
        sink,
    )

    assert result.is_ok
    assert result.value.reply_text == "Next workflow complete"
    assert len(adapter.dispatch_calls) == 2
