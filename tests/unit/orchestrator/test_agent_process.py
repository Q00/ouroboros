"""Unit tests for :class:`AgentProcess` and :class:`AgentProcessHandle`.

Issue: #518 — slice 1 of M6. Pins the cooperative lifecycle, the
directive emission shape (target_type=agent_process), and the
deferred-implementation surface (replay raises NotImplementedError).
"""

from __future__ import annotations

import asyncio

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.agent_process import (
    AgentProcess,
    AgentProcessStatus,
)
from ouroboros.persistence.event_store import EventStore


class _FakeEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)


def _types(events: list[BaseEvent]) -> list[str]:
    return [e.type for e in events]


def _directives(events: list[BaseEvent]) -> list[str]:
    return [e.data["directive"] for e in events if e.type == "control.directive.emitted"]


async def _wait_for_status(handle, status: AgentProcessStatus) -> None:
    for _ in range(100):
        if handle.status() is status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"status did not become {status}")


@pytest.mark.asyncio
async def test_spawn_initializes_concrete_event_store_before_emitting() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    process = AgentProcess(event_store=store)

    async def work(handle):
        return None

    try:
        handle = await process.spawn(intent="ralph", work_fn=work)
        await handle.wait_until_complete(timeout=1.0)
        events = await store.replay("agent_process", handle.process_id)
    finally:
        await store.close()

    assert [event.data["directive"] for event in events] == ["continue", "converge"]


@pytest.mark.asyncio
async def test_spawn_emits_initial_running_directive() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):
        await asyncio.sleep(0)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await handle.wait_until_complete(timeout=1.0)

    types = _types(store.appended)
    assert types[0] == "control.directive.emitted"
    assert store.appended[0].data["directive"] == "continue"
    assert store.appended[0].aggregate_type == "agent_process"
    assert store.appended[0].aggregate_id == handle.process_id


@pytest.mark.asyncio
async def test_completed_emits_converge_terminal_directive() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):
        return None

    handle = await process.spawn(intent="ralph", work_fn=work)
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.COMPLETED
    assert _directives(store.appended)[-1] == "converge"


@pytest.mark.asyncio
async def test_cancel_transitions_to_cancelled_and_emits_cancel() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()
    cancelled_seen = asyncio.Event()

    async def work(handle):
        started.set()
        # Spin until cancel is requested at a cooperative checkpoint.
        while not handle.should_cancel():
            await asyncio.sleep(0.005)
        cancelled_seen.set()

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.cancel(reason="test cancel")
    await asyncio.wait_for(cancelled_seen.wait(), timeout=1.0)
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.CANCELLED
    # Last lifecycle directive emitted by the handle is CANCEL.
    last_directive = next(
        d for d in reversed(_directives(store.appended)) if d in {"cancel", "converge"}
    )
    assert last_directive == "cancel"


@pytest.mark.asyncio
async def test_cancel_status_and_directive_wait_for_work_exit() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()
    release = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await asyncio.sleep(0.005)
        await release.wait()

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.cancel(reason="stop requested")

    assert handle.status() is AgentProcessStatus.RUNNING
    assert "cancel" not in _directives(store.appended)

    release.set()
    final = await handle.wait_until_complete(timeout=1.0)
    assert final is AgentProcessStatus.CANCELLED
    assert _directives(store.appended)[-1] == "cancel"
    assert store.appended[-1].data["extra"]["lifecycle_status"] == "cancelled"


@pytest.mark.asyncio
async def test_pause_then_resume_transitions_emit_wait_continue() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        # Loop forever until cancel — gives the test deterministic
        # control over the pause/resume timing.
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause()
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    await handle.resume()
    await _wait_for_status(handle, AgentProcessStatus.RUNNING)
    await handle.cancel(reason="end test")

    final = await handle.wait_until_complete(timeout=1.0)
    # Test ends with cancel so the terminal directive is CANCEL.
    assert final is AgentProcessStatus.CANCELLED

    directives = _directives(store.appended)
    # Sequence: continue (spawn) → wait (pause) → continue (resume)
    # → cancel (cancel). Pins the external lifecycle the journal sees.
    assert directives[:4] == ["continue", "wait", "continue", "cancel"]


@pytest.mark.asyncio
async def test_failed_work_marks_status_and_emits_cancel() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    class _SimulatedFailure(RuntimeError):
        pass

    async def work(handle):
        raise _SimulatedFailure("work blew up")

    handle = await process.spawn(intent="ralph", work_fn=work)
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.FAILED
    assert _directives(store.appended)[-1] == "cancel"
    failed_event = store.appended[-1]
    assert "_SimulatedFailure" in failed_event.data["reason"]
    assert failed_event.data["extra"]["lifecycle_status"] == "failed"


@pytest.mark.asyncio
async def test_replay_is_not_yet_implemented() -> None:
    process = AgentProcess(event_store=None)

    async def _trivial_work(handle) -> None:  # noqa: ARG001 — handle unused on trivial work
        return None

    handle = await process.spawn(intent="ralph", work_fn=_trivial_work)
    await handle.wait_until_complete(timeout=1.0)

    with pytest.raises(NotImplementedError):
        await handle.replay()


@pytest.mark.asyncio
async def test_no_event_store_means_no_emission() -> None:
    """The handle must still operate without a journal store attached."""
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.pause()
    await handle.resume()
    await handle.cancel(reason="end test")
    final = await handle.wait_until_complete(timeout=1.0)
    assert final is AgentProcessStatus.CANCELLED


@pytest.mark.asyncio
async def test_double_cancel_is_idempotent() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):
        while not handle.should_cancel():
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await handle.cancel(reason="first")
    await handle.cancel(reason="second-no-op")
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.CANCELLED
    # Cancel emitted exactly once even though we called cancel twice.
    cancel_count = sum(1 for d in _directives(store.appended) if d == "cancel")
    assert cancel_count == 1


@pytest.mark.asyncio
async def test_cancel_releases_paused_loop() -> None:
    """A paused loop must observe the cancel flag at the next checkpoint."""
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()
    saw_cancel = asyncio.Event()

    async def work(handle):
        started.set()
        # Loop until cancel. Each iteration parks on wait_unpaused so
        # the test can deterministically pause the work mid-run.
        while True:
            await handle.wait_unpaused()
            if handle.should_cancel():
                saw_cancel.set()
                return
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.pause()
    # Brief delay lets the loop reach its next wait_unpaused checkpoint
    # while paused. cancel() then sets paused_event + cancel_event;
    # the loop wakes, sees the cancel flag, and exits cleanly.
    await asyncio.sleep(0.02)
    await handle.cancel(reason="cancel while paused")
    await asyncio.wait_for(saw_cancel.wait(), timeout=1.0)
    final = await handle.wait_until_complete(timeout=1.0)
    assert final is AgentProcessStatus.CANCELLED


@pytest.mark.asyncio
async def test_pause_after_cancel_cannot_reblock_work_loop() -> None:
    """Once cancel is requested, a later pause must not reintroduce blocking."""
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()
    saw_cancel = asyncio.Event()

    async def work(handle):
        started.set()
        while True:
            await handle.wait_unpaused()
            if handle.should_cancel():
                saw_cancel.set()
                return
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.cancel(reason="cancel before pause")
    await handle.pause()

    await asyncio.wait_for(saw_cancel.wait(), timeout=1.0)
    final = await handle.wait_until_complete(timeout=1.0)
    assert final is AgentProcessStatus.CANCELLED


@pytest.mark.asyncio
async def test_lifecycle_directive_carries_target_type_agent_process() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):
        await asyncio.sleep(0)

    handle = await process.spawn(intent="evolve_step", work_fn=work)
    await handle.wait_until_complete(timeout=1.0)

    for event in store.appended:
        assert event.aggregate_type == "agent_process"
        assert event.aggregate_id == handle.process_id
        assert event.data["target_type"] == "agent_process"
        assert event.data["emitted_by"] == "agent_process"
        assert event.data["extra"]["intent"] == "evolve_step"
        assert "lifecycle_status" in event.data["extra"]


@pytest.mark.asyncio
async def test_status_is_running_immediately_after_spawn() -> None:
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        await asyncio.sleep(0.05)

    handle = await process.spawn(intent="ralph", work_fn=work)
    # Wait for the work to actually start so we can observe RUNNING.
    await asyncio.wait_for(started.wait(), timeout=1.0)
    assert handle.status() is AgentProcessStatus.RUNNING
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_wait_until_complete_waits_for_cancelled_work_to_exit() -> None:
    process = AgentProcess(event_store=None)
    release = asyncio.Event()
    exited = asyncio.Event()

    async def work(handle):
        while not handle.should_cancel():
            await asyncio.sleep(0)
        await release.wait()
        exited.set()

    handle = await process.spawn(intent="ralph", work_fn=work)
    await handle.cancel(reason="stop requested")

    waiter = asyncio.create_task(handle.wait_until_complete(timeout=1.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    assert not exited.is_set()

    release.set()
    final = await waiter
    assert final is AgentProcessStatus.CANCELLED
    assert exited.is_set()


@pytest.mark.asyncio
async def test_lifecycle_reasons_are_prefixed_once() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):
        return None

    handle = await process.spawn(intent="ralph", work_fn=work)
    await handle.wait_until_complete(timeout=1.0)

    reasons = [event.data["reason"] for event in store.appended]
    assert "ralph: spawned" in reasons
    assert "ralph: work returned" in reasons
    assert all("ralph: ralph:" not in reason for reason in reasons)
