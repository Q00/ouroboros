"""Unit tests for :class:`AgentProcess` and :class:`AgentProcessHandle`.

Issue: #518 — slice 1 of M6. Pins the cooperative lifecycle, the
directive emission shape (target_type=agent_process), and the
durable replay behavior for empty, partial, in-flight, and completed journals.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from pathlib import Path
from typing import Any

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.agent_process import (
    AgentProcess,
    AgentProcessHandle,
    AgentProcessStatus,
    project_agent_process_snapshot,
    run_with_agent_process,
)
from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore
from ouroboros.persistence.event_store import EventStore


class _FakeEventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:  # noqa: ARG002
        return list(self.appended)


class _FailingAppendReplayStore:
    async def append(self, event: BaseEvent) -> None:  # noqa: ARG002
        raise RuntimeError("simulated append failure")

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:  # noqa: ARG002
        return []


class _DropAfterFirstAppendReplayStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        if self.appended:
            raise RuntimeError("simulated later append failure")
        self.appended.append(event)

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:  # noqa: ARG002
        return list(self.appended)


class _BlockingSecondAppendStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []
        self.second_append_started = asyncio.Event()
        self.release_second_append = asyncio.Event()

    async def append(self, event: BaseEvent) -> None:
        if self.appended:
            self.second_append_started.set()
            await self.release_second_append.wait()
        self.appended.append(event)

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:  # noqa: ARG002
        return list(self.appended)


class _DropSecondAppendReplayStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []
        self.append_attempts = 0

    async def append(self, event: BaseEvent) -> None:
        self.append_attempts += 1
        if self.append_attempts == 2:
            raise RuntimeError("simulated second append failure")
        self.appended.append(event)

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:  # noqa: ARG002
        return list(self.appended)


class _BlockingWaitEventStore(_FakeEventStore):
    """Event store that holds the WAIT append open to expose resume races."""

    def __init__(self) -> None:
        super().__init__()
        self.wait_append_started = asyncio.Event()
        self.release_wait_append = asyncio.Event()

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)
        if event.data.get("directive") == "wait":
            self.wait_append_started.set()
            await self.release_wait_append.wait()


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


async def _wait_for_projected_status(
    store: EventStore, process_id: str, status: AgentProcessStatus
) -> None:
    for _ in range(100):
        snapshot = project_agent_process_snapshot(
            await store.replay("agent_process", process_id), process_id=process_id
        )
        if snapshot is not None and snapshot.status is status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"persisted status did not become {status}")


async def _wait_for_no_pending_emit(handle) -> None:
    for _ in range(100):
        if not handle._pending_emit_statuses:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pending lifecycle emit did not finish")


@pytest.mark.asyncio
async def test_spawn_initializes_concrete_event_store_before_emitting() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    process = AgentProcess(event_store=store)

    async def work(handle):
        return None

    try:
        handle = await process.spawn(intent="ralph", work_fn=work)
        await handle.wait_until_complete(timeout=1.0)
        await _wait_for_no_pending_emit(handle)
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
    failure = handle.failure()
    assert isinstance(failure, _SimulatedFailure)
    assert str(failure) == "work blew up"
    assert _directives(store.appended)[-1] == "cancel"
    failed_event = store.appended[-1]
    assert "_SimulatedFailure" in failed_event.data["reason"]
    assert failed_event.data["extra"]["lifecycle_status"] == "failed"


@pytest.mark.asyncio
async def test_complete_on_return_after_cancel_marks_completed() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):
        await handle.cancel(reason="late cancel")
        handle.complete_on_return_after_cancel()

    handle = await process.spawn(intent="evolve_step", work_fn=work)
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.COMPLETED
    assert _directives(store.appended)[-1] == "converge"


@pytest.mark.asyncio
async def test_abort_cancels_underlying_work_task() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def work(handle):  # noqa: ARG001 — exercised through task cancellation
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    handle = await process.spawn(intent="evolve_step", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.abort(reason="caller cancelled")
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.CANCELLED
    assert cancelled.is_set()
    assert _directives(store.appended)[-1] == "cancel"


@pytest.mark.asyncio
async def test_terminal_emit_failure_still_completes_waiters() -> None:
    async def _raise_on_emit(*args, **kwargs) -> None:
        raise RuntimeError("emit failed")

    handle = AgentProcessHandle(process_id="proc-emit-fails", _emit_directive=_raise_on_emit)

    await handle._mark_completed()
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.COMPLETED


@pytest.mark.asyncio
async def test_abort_preserves_caller_reason_in_terminal_directive() -> None:
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()

    async def work(handle):  # noqa: ARG001 — exercised through task cancellation
        started.set()
        await asyncio.Event().wait()

    handle = await process.spawn(intent="evolve_step", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.abort(reason="caller cancelled")
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.CANCELLED
    assert "caller cancelled" in store.appended[-1].data["reason"]


@pytest.mark.asyncio
async def test_failed_work_unblocks_waiters_when_terminal_directive_emit_fails() -> None:
    """A failing work task must not hang if FAILED directive persistence also fails."""

    class _FailingEventStore(_FakeEventStore):
        async def append(self, event: BaseEvent) -> None:
            if (
                event.type == "control.directive.emitted"
                and event.data.get("extra", {}).get("lifecycle_status") == "failed"
            ):
                raise RuntimeError("failed directive write failed")
            await super().append(event)

    class _WorkFailure(RuntimeError):
        pass

    store = _FailingEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):  # noqa: ARG001 — failure path under test
        raise _WorkFailure("work failed first")

    handle = await process.spawn(intent="ralph", work_fn=work)
    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.FAILED
    assert isinstance(handle.failure(), _WorkFailure)


@pytest.mark.asyncio
async def test_replay_without_event_store_raises_runtime_error() -> None:
    process = AgentProcess(event_store=None)

    async def _trivial_work(handle) -> None:  # noqa: ARG001 — handle unused on trivial work
        return None

    handle = await process.spawn(intent="ralph", work_fn=_trivial_work)
    await handle.wait_until_complete(timeout=1.0)

    with pytest.raises(RuntimeError, match="requires an event store"):
        await handle.replay()


@pytest.mark.asyncio
async def test_replay_with_empty_persisted_lifecycle_raises_runtime_error() -> None:
    process = AgentProcess(event_store=_FailingAppendReplayStore())

    async def work(handle):  # noqa: ARG001 — handle unused on trivial work
        return None

    handle = await process.spawn(intent="ralph", work_fn=work)
    await handle.wait_until_complete(timeout=1.0)

    assert handle.status() is AgentProcessStatus.COMPLETED
    with pytest.raises(RuntimeError, match="no persisted lifecycle events"):
        await handle.replay()


@pytest.mark.asyncio
async def test_replay_during_in_flight_pause_append_uses_last_durable_status() -> None:
    store = _BlockingSecondAppendStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.pause()
    await asyncio.wait_for(store.second_append_started.wait(), timeout=1.0)

    assert handle.status() is AgentProcessStatus.PAUSED
    snapshot = await handle.replay()
    assert snapshot.status is AgentProcessStatus.RUNNING

    store.release_second_append.set()
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    await _wait_for_no_pending_emit(handle)
    snapshot = await handle.replay()
    assert snapshot.status is AgentProcessStatus.PAUSED

    await handle.cancel()
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_pause_resume_directives_serialize_when_pause_append_is_slow() -> None:
    store = _BlockingSecondAppendStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.pause()
    await asyncio.wait_for(store.second_append_started.wait(), timeout=1.0)

    resume_task = asyncio.create_task(handle.resume())
    await asyncio.sleep(0)
    snapshot = await handle.replay()
    assert snapshot.status is AgentProcessStatus.RUNNING

    store.release_second_append.set()
    await asyncio.wait_for(resume_task, timeout=1.0)
    await _wait_for_no_pending_emit(handle)

    snapshot = await handle.replay()
    assert snapshot.status is AgentProcessStatus.RUNNING
    assert _directives(store.appended)[:3] == ["continue", "wait", "continue"]

    await handle.cancel()
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_replay_detects_lost_intermediate_pause_when_final_status_matches() -> None:
    store = _DropSecondAppendReplayStore()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.pause()
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    await handle.resume()
    await _wait_for_status(handle, AgentProcessStatus.RUNNING)
    await _wait_for_no_pending_emit(handle)

    snapshot = project_agent_process_snapshot(store.appended, process_id=handle.process_id)
    assert snapshot is not None
    assert snapshot.status is AgentProcessStatus.RUNNING
    with pytest.raises(RuntimeError, match="partial lifecycle history"):
        await handle.replay()

    await handle.cancel()
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_terminal_status_does_not_wait_for_in_flight_append() -> None:
    store = _BlockingSecondAppendStore()
    process = AgentProcess(event_store=store)

    async def work(handle):  # noqa: ARG001 — handle unused on trivial work
        return None

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(store.second_append_started.wait(), timeout=1.0)

    final = await handle.wait_until_complete(timeout=1.0)
    assert final is AgentProcessStatus.COMPLETED
    snapshot = await handle.replay()
    assert snapshot.status is AgentProcessStatus.RUNNING

    store.release_second_append.set()


@pytest.mark.asyncio
async def test_wedged_terminal_append_finishes_work_task_and_replay_fails_closed() -> None:
    store = _BlockingSecondAppendStore()
    process = AgentProcess(event_store=store)

    async def work(handle):  # noqa: ARG001 — handle unused on trivial work
        return None

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(store.second_append_started.wait(), timeout=1.0)

    final = await handle.wait_until_complete(timeout=1.0)
    assert final is AgentProcessStatus.COMPLETED
    assert handle._work_task is not None
    await asyncio.wait_for(handle._work_task, timeout=2.0)

    with pytest.raises(RuntimeError, match="partial lifecycle history"):
        await handle.replay()


@pytest.mark.asyncio
async def test_replay_with_lost_terminal_directive_raises_runtime_error() -> None:
    process = AgentProcess(event_store=_DropAfterFirstAppendReplayStore())

    async def work(handle):  # noqa: ARG001 — handle unused on trivial work
        return None

    handle = await process.spawn(intent="ralph", work_fn=work)
    await handle.wait_until_complete(timeout=1.0)

    assert handle.status() is AgentProcessStatus.COMPLETED
    with pytest.raises(RuntimeError, match="(stale lifecycle state|partial lifecycle history)"):
        await handle.replay()


@pytest.mark.asyncio
async def test_replay_with_lost_pause_directive_raises_runtime_error() -> None:
    process = AgentProcess(event_store=_DropAfterFirstAppendReplayStore())
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    await handle.pause()
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)

    with pytest.raises(RuntimeError, match="(stale lifecycle state|partial lifecycle history)"):
        await handle.replay()

    await handle.cancel()
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_replay_after_completed_returns_completed_snapshot() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    process = AgentProcess(event_store=store)

    async def work(handle):  # noqa: ARG001 — handle unused on trivial work
        return None

    try:
        handle = await process.spawn(intent="evolve-step", work_fn=work)
        await handle.wait_until_complete(timeout=1.0)
        await _wait_for_no_pending_emit(handle)

        snapshot = await handle.replay()
    finally:
        await store.close()

    assert snapshot.status is AgentProcessStatus.COMPLETED
    assert snapshot.is_terminal is True
    assert snapshot.process_id == handle.process_id
    assert snapshot.directive_count >= 2


@pytest.mark.asyncio
async def test_replay_after_cancel_returns_cancelled_snapshot() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await asyncio.sleep(0.005)

    try:
        handle = await process.spawn(intent="ralph", work_fn=work)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await handle.cancel(reason="user requested")
        await handle.wait_until_complete(timeout=1.0)
        await _wait_for_no_pending_emit(handle)

        snapshot = await handle.replay()
    finally:
        await store.close()

    assert snapshot.status is AgentProcessStatus.CANCELLED
    assert snapshot.is_terminal is True


@pytest.mark.asyncio
async def test_replay_during_pause_returns_paused_snapshot() -> None:
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    process = AgentProcess(event_store=store)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    try:
        handle = await process.spawn(intent="ralph", work_fn=work)
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await handle.pause()
        await _wait_for_status(handle, AgentProcessStatus.PAUSED)
        await _wait_for_projected_status(store, handle.process_id, AgentProcessStatus.PAUSED)
        await _wait_for_no_pending_emit(handle)

        snapshot = await handle.replay()
        await handle.resume()
        await handle.cancel()
        await handle.wait_until_complete(timeout=1.0)
    finally:
        await store.close()

    assert snapshot.status is AgentProcessStatus.PAUSED
    assert snapshot.is_terminal is False


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
async def test_agent_process_snapshot_projects_lifecycle_status_from_events() -> None:
    """AgentProcess state should be reconstructable from directive events."""
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    release = asyncio.Event()

    async def work(handle):
        while not release.is_set():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await handle.pause()
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    await handle.resume()
    release.set()
    await handle.wait_until_complete(timeout=1.0)

    snapshot = project_agent_process_snapshot(store.appended, process_id=handle.process_id)

    assert snapshot is not None
    assert snapshot.process_id == handle.process_id
    assert snapshot.intent == "ralph"
    assert snapshot.status is AgentProcessStatus.COMPLETED
    assert snapshot.directive_count == 4
    assert snapshot.last_reason == "ralph: work returned"
    assert snapshot.is_terminal is True


@pytest.mark.asyncio
async def test_agent_process_snapshot_ignores_other_processes_and_malformed_rows() -> None:
    """Projection should skip malformed/foreign rows instead of corrupting state."""
    store = _FakeEventStore()
    process = AgentProcess(event_store=store)

    async def work(handle):
        return None

    wanted = await process.spawn(intent="evolve_step", work_fn=work, process_id="proc-wanted")
    other = await process.spawn(intent="ralph", work_fn=work, process_id="proc-other")
    await wanted.wait_until_complete(timeout=1.0)
    await other.wait_until_complete(timeout=1.0)
    malformed = BaseEvent(
        type="control.directive.emitted",
        aggregate_type="agent_process",
        aggregate_id="proc-wanted",
        data={"extra": {"lifecycle_status": "not-a-status", "intent": "bad"}},
    )

    snapshot = project_agent_process_snapshot(
        [malformed, *store.appended], process_id="proc-wanted"
    )

    assert snapshot is not None
    assert snapshot.process_id == "proc-wanted"
    assert snapshot.intent == "evolve_step"
    assert snapshot.status is AgentProcessStatus.COMPLETED
    assert snapshot.directive_count == 2


def test_agent_process_snapshot_accepts_minimal_lifecycle_rows() -> None:
    """Replay requires lifecycle status; descriptive metadata is optional."""
    same_time = datetime(2026, 1, 1, tzinfo=UTC)
    minimal_running = BaseEvent(
        id="00000000-0000-0000-0000-000000000001",
        type="control.directive.emitted",
        timestamp=same_time,
        aggregate_type="agent_process",
        aggregate_id="proc-wanted",
        data={
            "reason": "ralph: spawned",
            "extra": {"lifecycle_status": "running", "intent": "ralph"},
        },
    )
    minimal_cancelled = BaseEvent(
        id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        type="control.directive.emitted",
        timestamp=same_time,
        aggregate_type="agent_process",
        aggregate_id="proc-wanted",
        data={"extra": {"lifecycle_status": "cancelled"}},
    )

    snapshot = project_agent_process_snapshot(
        [minimal_running, minimal_cancelled],
        process_id="proc-wanted",
    )

    assert snapshot is not None
    assert snapshot.process_id == "proc-wanted"
    assert snapshot.intent == "ralph"
    assert snapshot.status is AgentProcessStatus.CANCELLED
    assert snapshot.directive_count == 2
    assert snapshot.last_reason == "ralph: spawned"
    assert snapshot.is_terminal is True


def test_agent_process_snapshot_matches_event_store_order_for_timestamp_ties() -> None:
    """Timestamp ties should follow EventStore's timestamp/id replay contract."""
    same_time = datetime(2026, 1, 1, tzinfo=UTC)
    completed = BaseEvent(
        id="00000000-0000-0000-0000-000000000001",
        type="control.directive.emitted",
        timestamp=same_time,
        aggregate_type="agent_process",
        aggregate_id="proc-wanted",
        data={
            "reason": "ralph: work returned",
            "extra": {"lifecycle_status": "completed", "intent": "ralph"},
        },
    )
    running = BaseEvent(
        id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        type="control.directive.emitted",
        timestamp=same_time,
        aggregate_type="agent_process",
        aggregate_id="proc-wanted",
        data={
            "reason": "ralph: spawned",
            "extra": {"lifecycle_status": "running", "intent": "ralph"},
        },
    )

    snapshot = project_agent_process_snapshot([completed, running], process_id="proc-wanted")

    assert snapshot is not None
    assert snapshot.status is AgentProcessStatus.RUNNING
    assert snapshot.intent == "ralph"
    assert snapshot.last_reason == "ralph: spawned"


def test_agent_process_snapshot_skips_rows_without_comparable_ordering() -> None:
    """Malformed event-like rows without timestamp/id should not crash sorting."""

    class _NoTimestampEvent:
        type = "control.directive.emitted"
        aggregate_type = "agent_process"
        aggregate_id = "proc-wanted"
        data = {
            "reason": "bad",
            "extra": {"lifecycle_status": "completed", "intent": "bad"},
        }

    valid = BaseEvent(
        id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        type="control.directive.emitted",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        aggregate_type="agent_process",
        aggregate_id="proc-wanted",
        data={
            "reason": "ralph: spawned",
            "extra": {"lifecycle_status": "running", "intent": "ralph"},
        },
    )

    snapshot = project_agent_process_snapshot(
        [_NoTimestampEvent(), valid], process_id="proc-wanted"
    )

    assert snapshot is not None
    assert snapshot.status is AgentProcessStatus.RUNNING
    assert snapshot.intent == "ralph"


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


# ---------------------------------------------------------------------------
# Slice 2 (#518): durable pause/resume via CheckpointStore
# ---------------------------------------------------------------------------


class _ErroringCheckpointStore(CheckpointStore):
    """A CheckpointStore whose save() always returns an error."""

    def save(self, checkpoint):  # type: ignore[override]
        return Result.err(PersistenceError("simulated save error", operation="write", details={}))


class _FailingSecondSaveCheckpointStore(CheckpointStore):
    """A CheckpointStore that succeeds once, then fails subsequent saves."""

    def __init__(self, *, base_path: Path) -> None:
        super().__init__(base_path=base_path)
        self.save_count = 0

    def save(self, checkpoint):  # type: ignore[override]
        self.save_count += 1
        if self.save_count >= 2:
            return Result.err(
                PersistenceError("simulated save error", operation="write", details={})
            )
        return super().save(checkpoint)


@pytest.mark.asyncio
async def test_pause_persists_state_via_checkpoint_store(tmp_path: Path) -> None:
    """Acknowledged pause must persist so load_persisted_pause returns True."""
    ck_store = CheckpointStore(base_path=tmp_path)
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=ck_store)
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)

    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is True

    await handle.cancel(reason="end test")
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_pause_request_does_not_persist_until_acknowledged(tmp_path: Path) -> None:
    """Restart recovery must not restore a merely requested, unacknowledged pause."""
    ck_store = CheckpointStore(base_path=tmp_path)
    process = AgentProcess(event_store=None)
    started = asyncio.Event()
    release = asyncio.Event()

    async def work(handle):  # noqa: ARG001 - intentionally ignores pause checkpoints until released
        started.set()
        await release.wait()

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=ck_store)

    assert handle.should_pause() is True
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is False

    await handle.cancel(reason="end test")
    release.set()
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_fast_resume_during_pause_ack_does_not_rewrite_stale_pause(
    tmp_path: Path,
) -> None:
    """A resume that wins while WAIT is being emitted must remain durable truth."""
    ck_store = CheckpointStore(base_path=tmp_path)
    event_store = _BlockingWaitEventStore()
    process = AgentProcess(event_store=event_store)
    started = asyncio.Event()
    checkpoint = asyncio.Event()
    wait_returned = asyncio.Event()

    async def work(handle):
        started.set()
        await checkpoint.wait()
        await handle.wait_unpaused()
        wait_returned.set()

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=ck_store)
    checkpoint.set()
    await asyncio.wait_for(event_store.wait_append_started.wait(), timeout=1.0)
    assert handle.status() is AgentProcessStatus.PAUSED

    await handle.resume(store=ck_store)
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is False

    event_store.release_wait_append.set()
    await asyncio.wait_for(wait_returned.wait(), timeout=1.0)
    await handle.wait_until_complete(timeout=1.0)

    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is False


@pytest.mark.asyncio
async def test_cancel_clears_persisted_pause_checkpoint(tmp_path: Path) -> None:
    """A paused-then-cancelled process must not restart as paused."""
    ck_store = CheckpointStore(base_path=tmp_path)
    process = AgentProcess(event_store=None)
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

    await handle.pause(store=ck_store)
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is True

    await handle.cancel(reason="cancel while paused")
    await asyncio.wait_for(saw_cancel.wait(), timeout=1.0)
    await handle.wait_until_complete(timeout=1.0)

    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is False


@pytest.mark.asyncio
async def test_resume_clears_persisted_pause(tmp_path: Path) -> None:
    """resume() must overwrite the checkpoint so load_persisted_pause returns False."""
    ck_store = CheckpointStore(base_path=tmp_path)
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=ck_store)
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    await handle.resume(store=ck_store)

    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is False

    await handle.cancel(reason="end test")
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_resume_clears_original_pause_store_when_called_with_different_store(
    tmp_path: Path,
) -> None:
    """Resume must clear the store that owns the acknowledged paused marker."""
    pause_store = CheckpointStore(base_path=tmp_path / "pause")
    resume_store = CheckpointStore(base_path=tmp_path / "resume")
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=pause_store)
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=pause_store) is True

    await handle.resume(store=resume_store)

    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=pause_store) is False
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=resume_store) is False

    await handle.cancel(reason="end test")
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_repeated_pause_preserves_original_checkpoint_store(tmp_path: Path) -> None:
    """A duplicate pause must not strand the acknowledged pause marker in its first store."""
    first_store = CheckpointStore(base_path=tmp_path / "first")
    second_store = CheckpointStore(base_path=tmp_path / "second")
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=first_store)
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=first_store) is True

    await handle.pause(store=second_store)
    await handle.resume()

    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=first_store) is False
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=second_store) is False

    await handle.cancel(reason="end test")
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_load_persisted_pause_does_not_rollback_to_stale_paused_checkpoint(
    tmp_path: Path,
) -> None:
    """Corrupt latest lifecycle truth must fail closed instead of resurrecting .1 paused."""
    ck_store = CheckpointStore(base_path=tmp_path)
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=ck_store)
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)
    await handle.resume(store=ck_store)

    checkpoint_seed = f"agent_process_{hashlib.sha256(handle.process_id.encode()).hexdigest()}"
    current_checkpoint = tmp_path / f"checkpoint_{checkpoint_seed}.json"
    current_checkpoint.write_text("{not valid json", encoding="utf-8")

    # The generic API rolls back to the older paused row, but pause recovery
    # must use stricter latest-row semantics and return False.
    assert ck_store.load(checkpoint_seed).value.phase == "agent_process_paused"
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is False

    await handle.cancel(reason="end test")
    await handle.wait_until_complete(timeout=1.0)


@pytest.mark.asyncio
async def test_pause_checkpoint_uses_agent_process_namespace(tmp_path: Path) -> None:
    """Agent pause persistence must not overwrite a workflow checkpoint with the same id."""
    ck_store = CheckpointStore(base_path=tmp_path)
    process_id = "shared-id"
    workflow_checkpoint = CheckpointData.create(
        seed_id=process_id,
        phase="workflow_running",
        state={"owner": "workflow"},
    )
    assert ck_store.save(workflow_checkpoint).is_ok

    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        started.set()
        while not handle.should_cancel():
            await handle.wait_unpaused()
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="ralph", work_fn=work, process_id=process_id)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    await handle.pause(store=ck_store)
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)

    assert AgentProcessHandle.load_persisted_pause(process_id, store=ck_store) is True
    loaded_workflow = ck_store.load(process_id)
    assert loaded_workflow.is_ok
    assert loaded_workflow.value.phase == "workflow_running"
    assert loaded_workflow.value.state == {"owner": "workflow"}

    await handle.cancel(reason="end test")
    await handle.wait_until_complete(timeout=1.0)


def test_pause_checkpoint_key_avoids_sanitizer_collisions(tmp_path: Path) -> None:
    """Distinct process ids that sanitize alike must not share pause recovery state."""
    ck_store = CheckpointStore(base_path=tmp_path)
    colliding_raw_id = "a/b"
    other_raw_id = "a_b"

    checkpoint = CheckpointData.create(
        seed_id=f"agent_process_{hashlib.sha256(colliding_raw_id.encode()).hexdigest()}",
        phase="agent_process_paused",
        state={"status": "paused"},
    )
    assert ck_store.save(checkpoint).is_ok

    assert AgentProcessHandle.load_persisted_pause(colliding_raw_id, store=ck_store) is True
    assert AgentProcessHandle.load_persisted_pause(other_raw_id, store=ck_store) is False


@pytest.mark.asyncio
async def test_pause_acknowledgement_surfaces_checkpoint_save_error() -> None:
    """Acknowledged durable pause must not silently hide CheckpointStore.save errors."""
    erroring_store = _ErroringCheckpointStore()
    handle = AgentProcessHandle(process_id="erroring-pause")

    await handle.pause(store=erroring_store)

    with pytest.raises(PersistenceError):
        await handle.wait_unpaused()

    assert handle.status() is AgentProcessStatus.PAUSED
    assert handle.should_pause() is True


@pytest.mark.asyncio
async def test_spawned_process_fails_closed_when_pause_checkpoint_save_fails() -> None:
    """A work-loop checkpoint failure must complete the handle as FAILED, not hang."""
    process = AgentProcess(event_store=None)
    erroring_store = _ErroringCheckpointStore()

    async def work(handle):
        await handle.pause(store=erroring_store)
        await handle.wait_unpaused()

    handle = await process.spawn(intent="ralph", work_fn=work)

    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.FAILED


@pytest.mark.asyncio
async def test_resume_surfaces_checkpoint_save_error(tmp_path: Path) -> None:
    """A failed running overwrite must be visible because stale paused recovery remains."""
    ck_store = _FailingSecondSaveCheckpointStore(base_path=tmp_path)
    handle = AgentProcessHandle(process_id="erroring-resume")

    await handle.pause(store=ck_store)
    waiter = asyncio.create_task(handle.wait_unpaused())
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)

    with pytest.raises(PersistenceError):
        await handle.resume()

    assert handle.status() is AgentProcessStatus.PAUSED
    assert handle.should_pause() is True

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_failed_finalization_deletes_stale_pause_when_failed_checkpoint_save_fails(
    tmp_path: Path,
) -> None:
    """If failed cleanup cannot write a tombstone, stale paused truth is deleted."""
    ck_store = _FailingSecondSaveCheckpointStore(base_path=tmp_path)
    handle = AgentProcessHandle(process_id="failed-delete")

    await handle.pause(store=ck_store)
    waiter = asyncio.create_task(handle.wait_unpaused())
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)

    await handle._mark_failed(reason="simulated failure")

    assert handle.status() is AgentProcessStatus.FAILED
    assert AgentProcessHandle.load_persisted_pause(handle.process_id, store=ck_store) is False

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_spawned_cancel_finalization_failure_completes_failed(tmp_path: Path) -> None:
    """Event-loop cancellation cleanup must not strand completion waiters on save failure."""
    ck_store = _FailingSecondSaveCheckpointStore(base_path=tmp_path)
    process = AgentProcess(event_store=None)
    started = asyncio.Event()

    async def work(handle):
        await handle.pause(store=ck_store)
        started.set()
        await handle.wait_unpaused()

    handle = await process.spawn(intent="ralph", work_fn=work)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    work_task = handle._work_task
    assert work_task is not None

    work_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await work_task

    assert handle.status() is AgentProcessStatus.FAILED
    assert handle._completed_event.is_set()


@pytest.mark.asyncio
async def test_spawned_process_fails_closed_when_terminal_checkpoint_save_fails() -> None:
    """A terminal durability failure in the runner must not leave waiters hanging."""
    process = AgentProcess(event_store=None)
    erroring_store = _ErroringCheckpointStore()

    async def work(handle):
        await handle.pause(store=erroring_store)
        return None

    handle = await process.spawn(intent="ralph", work_fn=work)

    final = await handle.wait_until_complete(timeout=1.0)

    assert final is AgentProcessStatus.FAILED


@pytest.mark.asyncio
async def test_terminal_transition_surfaces_checkpoint_save_error(tmp_path: Path) -> None:
    """Terminal cleanup must not silently leave durable pause truth stale."""
    ck_store = _FailingSecondSaveCheckpointStore(base_path=tmp_path)
    handle = AgentProcessHandle(process_id="erroring-terminal")

    await handle.pause(store=ck_store)
    waiter = asyncio.create_task(handle.wait_unpaused())
    await _wait_for_status(handle, AgentProcessStatus.PAUSED)

    with pytest.raises(PersistenceError):
        await handle._mark_cancelled()

    assert handle.status() is AgentProcessStatus.PAUSED
    assert handle.should_pause() is True

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter


@pytest.mark.asyncio
async def test_load_persisted_pause_returns_false_when_no_checkpoint(tmp_path: Path) -> None:
    """load_persisted_pause must return False for a process_id with no prior checkpoint."""
    ck_store = CheckpointStore(base_path=tmp_path)
    fresh_process_id = "deadbeefdeadbeefdeadbeefdeadbeef"

    assert AgentProcessHandle.load_persisted_pause(fresh_process_id, store=ck_store) is False


# ---------------------------------------------------------------------------
# Slice 4 of #518 — durable cancel signal via CheckpointStore
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_persists_state_via_checkpoint_store(tmp_path) -> None:
    """``cancel()`` must write an ``agent_process_cancelled`` checkpoint
    so a restarted process can detect that a previous run was cancelled
    via :meth:`AgentProcessHandle.load_persisted_cancel`."""
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()

    event_store = _FakeEventStore()
    process = AgentProcess(event_store=event_store)

    async def work(handle):
        await asyncio.sleep(0.05)

    handle = await process.spawn(intent="canary", work_fn=work)
    handle._checkpoint_store = store  # noqa: SLF001 — exercise the injection point
    await handle.cancel(reason="quota exceeded")
    await handle.wait_until_complete(timeout=1.0)

    found, reason = AgentProcessHandle.load_persisted_cancel(handle.process_id, store=store)
    assert found is True
    assert reason == "quota exceeded"


@pytest.mark.asyncio
async def test_cancel_swallows_checkpoint_save_error() -> None:
    """A failing CheckpointStore must NOT raise out of ``cancel()`` —
    the in-memory flag is authoritative and the durable hint is best
    effort."""
    from ouroboros.core.errors import PersistenceError
    from ouroboros.core.types import Result
    from ouroboros.persistence.checkpoint import CheckpointStore

    class _ExplodingStore(CheckpointStore):
        def __init__(self) -> None:  # noqa: D401 — minimal stub
            self.calls: list[Any] = []

        def save(self, checkpoint: Any) -> Result[None, PersistenceError]:
            self.calls.append(checkpoint)
            return Result.err(PersistenceError("simulated disk full"))

    event_store = _FakeEventStore()
    process = AgentProcess(event_store=event_store)

    async def work(handle):
        await asyncio.sleep(0.05)

    handle = await process.spawn(intent="explode", work_fn=work)
    bad_store = _ExplodingStore()
    handle._checkpoint_store = bad_store  # noqa: SLF001
    await handle.cancel(reason="boom")
    await handle.wait_until_complete(timeout=1.0)

    # cancel() did not raise; the in-memory transition still happened
    assert handle.status() is AgentProcessStatus.CANCELLED
    # And the store attempt was made (so we know the code path executed)
    assert len(bad_store.calls) == 1
    assert bad_store.calls[0].phase == "agent_process_cancelled"


def test_load_persisted_cancel_returns_false_when_no_checkpoint(tmp_path) -> None:
    """A fresh ``process_id`` with no prior writes returns ``(False, None)``."""
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    found, reason = AgentProcessHandle.load_persisted_cancel("never-seen-process", store=store)
    assert found is False
    assert reason is None


@pytest.mark.asyncio
async def test_cancel_uses_process_checkpoint_store_public_spawn_path(tmp_path) -> None:
    """``AgentProcess(checkpoint_store=...)`` wires durable cancel into spawned handles."""
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process = AgentProcess(event_store=_FakeEventStore(), checkpoint_store=store)

    async def work(handle):
        while not handle.should_cancel():
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="public-store", work_fn=work)
    await handle.cancel(reason="operator stop")
    await handle.wait_until_complete(timeout=1.0)

    found, reason = AgentProcessHandle.load_persisted_cancel(handle.process_id, store=store)
    assert found is True
    assert reason == "operator stop"


@pytest.mark.asyncio
async def test_spawn_enters_work_fn_cancelled_when_persisted_cancel_exists(tmp_path) -> None:
    """Restarting the same process_id must let workflow teardown observe cancel."""
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process = AgentProcess(event_store=_FakeEventStore(), checkpoint_store=store)
    process_id = "restart-cancelled-process"

    async def first_work(handle):
        while not handle.should_cancel():
            await asyncio.sleep(0.005)

    first = await process.spawn(intent="first", process_id=process_id, work_fn=first_work)
    await first.cancel(reason="operator stop")
    assert await first.wait_until_complete(timeout=1.0) is AgentProcessStatus.CANCELLED
    restart_event_offset = len(process.event_store.appended)

    teardown_called = False
    normal_work_called = False

    async def restarted_work(handle):
        nonlocal normal_work_called, teardown_called
        if handle.should_cancel():
            teardown_called = True
            return
        normal_work_called = True

    restarted = await process.spawn(intent="restart", process_id=process_id, work_fn=restarted_work)

    assert await restarted.wait_until_complete(timeout=1.0) is AgentProcessStatus.CANCELLED
    assert restarted.should_cancel() is True
    assert teardown_called is True
    assert normal_work_called is False
    restart_directives = _directives(process.event_store.appended[restart_event_offset:])
    assert restart_directives == ["continue", "cancel"]

    third_called = False

    async def third_work(handle):
        nonlocal third_called
        third_called = True

    third = await process.spawn(intent="third", process_id=process_id, work_fn=third_work)

    assert await third.wait_until_complete(timeout=1.0) is AgentProcessStatus.COMPLETED
    assert third_called is True


@pytest.mark.asyncio
async def test_spawn_rejects_process_id_in_cancel_control_namespace(tmp_path) -> None:
    """Caller-supplied process IDs must not collide with cancel-control keys."""
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process = AgentProcess(event_store=_FakeEventStore(), checkpoint_store=store)

    async def work(handle):
        return None

    with pytest.raises(ValueError):
        await process.spawn(
            intent="reserved",
            process_id="__ouroboros_agent_process_cancel__:external",
            work_fn=work,
        )


@pytest.mark.asyncio
async def test_spawn_returns_cancelled_when_cancel_consumption_fails(tmp_path) -> None:
    """Consume cleanup failure must not escape after CANCELLED is recorded."""
    from ouroboros.core.errors import PersistenceError
    from ouroboros.core.types import Result
    from ouroboros.persistence.checkpoint import CheckpointStore

    class _ConsumeFailingStore(CheckpointStore):
        def save(self, checkpoint: Any) -> Result[None, PersistenceError]:
            if checkpoint.phase == "agent_process_cancel_consumed":
                return Result.err(PersistenceError("simulated consume failure"))
            return super().save(checkpoint)

    store = _ConsumeFailingStore(base_path=tmp_path)
    store.initialize()
    process = AgentProcess(event_store=_FakeEventStore(), checkpoint_store=store)
    process_id = "consume-fails-process"

    async def first_work(handle):
        while not handle.should_cancel():
            await asyncio.sleep(0.005)

    first = await process.spawn(intent="first", process_id=process_id, work_fn=first_work)
    await first.cancel(reason="operator stop")
    assert await first.wait_until_complete(timeout=1.0) is AgentProcessStatus.CANCELLED

    teardown_called = False
    normal_work_called = False

    async def restarted_work(handle):
        nonlocal normal_work_called, teardown_called
        if handle.should_cancel():
            teardown_called = True
            return
        normal_work_called = True

    restarted = await process.spawn(intent="restart", process_id=process_id, work_fn=restarted_work)

    assert await restarted.wait_until_complete(timeout=1.0) is AgentProcessStatus.CANCELLED
    assert teardown_called is True
    assert normal_work_called is False


@pytest.mark.asyncio
async def test_cancel_survives_ordinary_checkpoint_rotation_after_cancel(tmp_path) -> None:
    """Durable cancel must not share the ordinary process checkpoint ring."""
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process = AgentProcess(event_store=_FakeEventStore(), checkpoint_store=store)

    async def work(handle):
        while not handle.should_cancel():
            await asyncio.sleep(0.005)

    handle = await process.spawn(intent="rotation-proof", work_fn=work)
    await handle.cancel(reason="operator stop")
    await handle.wait_until_complete(timeout=1.0)

    for index in range(store.MAX_ROLLBACK_DEPTH + 2):
        ordinary_checkpoint = CheckpointData.create(
            seed_id=handle.process_id,
            phase="execution",
            state={"post_cancel_checkpoint": index},
        )
        assert store.save(ordinary_checkpoint).is_ok

    found, reason = AgentProcessHandle.load_persisted_cancel(handle.process_id, store=store)

    assert found is True
    assert reason == "operator stop"


def test_load_persisted_cancel_finds_rotated_marker_after_partial_consume(tmp_path) -> None:
    """If consume write fails after rotation, rollback cancel marker remains active."""
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process_id = "partial-consume-process"
    seed_id = AgentProcessHandle._cancel_checkpoint_seed(process_id)  # noqa: SLF001
    checkpoint = CheckpointData.create(
        seed_id=seed_id,
        phase="agent_process_cancelled",
        state={"status": "cancelled", "reason": "operator stop"},
    )
    assert store.save(checkpoint).is_ok

    store._rotate_checkpoints(seed_id)  # noqa: SLF001 — simulate post-rotate write failure

    found, reason = AgentProcessHandle.load_persisted_cancel(process_id, store=store)

    assert found is True
    assert reason == "operator stop"


def test_load_persisted_cancel_raises_when_checkpoint_artifact_is_corrupt(tmp_path) -> None:
    """Restart gate must fail closed when durable state exists but is unreadable."""
    from ouroboros.core.errors import PersistenceError
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process_id = "corrupt-cancel-process"
    checkpoint = CheckpointData.create(
        seed_id=AgentProcessHandle._cancel_checkpoint_seed(process_id),  # noqa: SLF001
        phase="agent_process_cancelled",
        state={"status": "cancelled", "reason": "operator stop"},
    )
    save_result = store.save(checkpoint)
    assert save_result.is_ok

    path = store._get_checkpoint_path(  # noqa: SLF001 — corrupt public artifact
        AgentProcessHandle._cancel_checkpoint_seed(process_id)  # noqa: SLF001
    )
    path.write_text("not valid json")

    with pytest.raises(PersistenceError):
        AgentProcessHandle.load_persisted_cancel(process_id, store=store)


def test_load_persisted_cancel_returns_false_without_store() -> None:
    """``store=None`` means no durable state — return ``(False, None)``
    rather than raising."""
    from ouroboros.orchestrator.agent_process import AgentProcessHandle

    found, reason = AgentProcessHandle.load_persisted_cancel("any-process", store=None)
    assert found is False
    assert reason is None


def test_load_persisted_cancel_consumed_marker_beats_older_cancel(tmp_path) -> None:
    """A current consumed marker must not resurrect an older cancel marker."""
    from ouroboros.orchestrator.agent_process import AgentProcessHandle
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process_id = "consumed-beats-stale-cancel"
    seed_id = AgentProcessHandle._cancel_checkpoint_seed(process_id)  # noqa: SLF001
    cancel = CheckpointData.create(
        seed_id=seed_id,
        phase="agent_process_cancelled",
        state={"status": "cancelled", "reason": "old stop"},
    )
    consumed = CheckpointData.create(
        seed_id=seed_id,
        phase="agent_process_cancel_consumed",
        state={"status": "cancel_consumed", "reason": "old stop"},
    )

    assert store.save(cancel).is_ok
    assert store.save(consumed).is_ok

    found, reason = AgentProcessHandle.load_persisted_cancel(process_id, store=store)

    assert found is False
    assert reason is None


@pytest.mark.asyncio
async def test_spawn_rejects_sanitized_process_id_cancel_control_collision(tmp_path) -> None:
    """IDs that sanitize into the reserved cancel namespace must be rejected."""
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process = AgentProcess(event_store=_FakeEventStore(), checkpoint_store=store)

    async def work(handle):
        return None

    with pytest.raises(ValueError):
        await process.spawn(
            intent="reserved-sanitized",
            process_id="__ouroboros/agent_process_cancel__:external",
            work_fn=work,
        )


@pytest.mark.asyncio
async def test_run_with_agent_process_reuses_durable_cancel_process_id(tmp_path) -> None:
    """The production helper must expose crash-left durable cancel by stable process ID."""
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process_id = "helper:durable-cancel"
    checkpoint = CheckpointData.create(
        seed_id=AgentProcessHandle._cancel_checkpoint_seed(process_id),  # noqa: SLF001
        phase="agent_process_cancelled",
        state={"status": "cancelled", "reason": "operator stop"},
    )
    assert store.save(checkpoint).is_ok

    teardown_called = False
    normal_work_called = False

    async def restarted_work(handle):
        nonlocal normal_work_called, teardown_called
        if handle.should_cancel():
            teardown_called = True
            return "teardown"
        normal_work_called = True
        return "normal"

    restart_result = await run_with_agent_process(
        event_store=_FakeEventStore(),
        intent="helper",
        work_fn=restarted_work,
        checkpoint_store=store,
        process_id=process_id,
    )

    assert restart_result == "teardown"
    assert teardown_called is True
    assert normal_work_called is False

    fresh_called = False

    async def fresh_work(handle):
        nonlocal fresh_called
        fresh_called = True
        return "fresh"

    fresh_result = await run_with_agent_process(
        event_store=_FakeEventStore(),
        intent="helper",
        work_fn=fresh_work,
        checkpoint_store=store,
        process_id=process_id,
    )

    assert fresh_result == "fresh"
    assert fresh_called is True


@pytest.mark.asyncio
async def test_run_with_agent_process_keeps_live_cancel_marker_until_restart_consumes(
    tmp_path,
) -> None:
    """Live cancellation must not consume the durable marker too early.

    ``JobManager.cancel_job`` writes the job-scoped marker before the runner sees
    cancellation. If the same process crashes after live teardown but before job
    state is durably terminal, the restart must still observe that marker.
    """
    from ouroboros.persistence.checkpoint import CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process_id = "helper:live-cancel"

    async def cancel_work(handle):
        await handle.cancel(reason="operator stop")
        return "cancelled"

    cancel_result = await run_with_agent_process(
        event_store=_FakeEventStore(),
        intent="helper",
        work_fn=cancel_work,
        checkpoint_store=store,
        process_id=process_id,
    )

    assert cancel_result == "cancelled"
    assert AgentProcessHandle.load_persisted_cancel(process_id, store=store) == (
        True,
        "operator stop",
    )

    restart_teardown_called = False

    async def restart_work(handle):
        nonlocal restart_teardown_called
        if handle.should_cancel():
            restart_teardown_called = True
            return "teardown"
        return "normal"

    restart_result = await run_with_agent_process(
        event_store=_FakeEventStore(),
        intent="helper",
        work_fn=restart_work,
        checkpoint_store=store,
        process_id=process_id,
    )

    assert restart_result == "teardown"
    assert restart_teardown_called is True
    assert AgentProcessHandle.load_persisted_cancel(process_id, store=store) == (False, None)


@pytest.mark.asyncio
async def test_spawn_consumes_persisted_cancel_even_without_reason(tmp_path) -> None:
    """Missing optional reason must not make a durable cancel permanent."""
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process = AgentProcess(event_store=_FakeEventStore(), checkpoint_store=store)
    process_id = "missing-reason-cancel"
    checkpoint = CheckpointData.create(
        seed_id=AgentProcessHandle._cancel_checkpoint_seed(process_id),  # noqa: SLF001
        phase="agent_process_cancelled",
        state={"status": "cancelled"},
    )
    assert store.save(checkpoint).is_ok

    teardown_called = False

    async def restart_work(handle):
        nonlocal teardown_called
        if handle.should_cancel():
            teardown_called = True

    restarted = await process.spawn(intent="restart", process_id=process_id, work_fn=restart_work)

    assert await restarted.wait_until_complete(timeout=1.0) is AgentProcessStatus.CANCELLED
    assert teardown_called is True

    second_called = False

    async def second_work(handle):
        nonlocal second_called
        second_called = True

    second = await process.spawn(intent="second", process_id=process_id, work_fn=second_work)

    assert await second.wait_until_complete(timeout=1.0) is AgentProcessStatus.COMPLETED
    assert second_called is True


@pytest.mark.asyncio
async def test_run_with_agent_process_checkpoint_init_failure_is_best_effort(monkeypatch) -> None:
    """Implicit helper durability must not make background jobs require writable home."""
    from ouroboros.persistence.checkpoint import CheckpointStore

    def fail_initialize(self):
        raise OSError("read-only checkpoint directory")

    monkeypatch.setattr(CheckpointStore, "initialize", fail_initialize)

    async def work(handle):
        return "completed"

    result = await run_with_agent_process(
        event_store=_FakeEventStore(),
        intent="helper",
        work_fn=work,
        process_id="helper:init-fails",
    )

    assert result == "completed"


def test_load_persisted_cancel_raises_when_newer_artifact_corrupt_despite_backup(
    tmp_path,
) -> None:
    """A corrupt newer cancel artifact must fail closed instead of using stale rollback."""
    from ouroboros.core.errors import PersistenceError
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process_id = "corrupt-current-with-backup"
    seed_id = AgentProcessHandle._cancel_checkpoint_seed(process_id)  # noqa: SLF001
    consumed = CheckpointData.create(
        seed_id=seed_id,
        phase="agent_process_cancel_consumed",
        state={"status": "cancel_consumed"},
    )
    current = CheckpointData.create(
        seed_id=seed_id,
        phase="agent_process_cancelled",
        state={"status": "cancelled", "reason": "new stop"},
    )
    assert store.save(consumed).is_ok
    assert store.save(current).is_ok
    store._get_checkpoint_path(seed_id).write_text("not valid json")  # noqa: SLF001

    with pytest.raises(PersistenceError):
        AgentProcessHandle.load_persisted_cancel(process_id, store=store)


def test_load_persisted_cancel_raises_on_rotated_consumed_without_current_level(
    tmp_path,
) -> None:
    """A rotated consumed marker with no current file is an ambiguous failed write.

    ``CheckpointStore.save()`` rotates before writing level 0. If a fresh cancel
    write fails after rotating an older consumed marker, level 0 is absent and
    level 1 contains stale consumed state. Restart must fail closed instead of
    treating cancellation as absent.
    """
    from ouroboros.core.errors import PersistenceError
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    process_id = "rotated-consumed-without-current"
    seed_id = AgentProcessHandle._cancel_checkpoint_seed(process_id)  # noqa: SLF001
    consumed = CheckpointData.create(
        seed_id=seed_id,
        phase="agent_process_cancel_consumed",
        state={"status": "cancel_consumed"},
    )
    assert store.save(consumed).is_ok
    store._rotate_checkpoints(seed_id)  # noqa: SLF001 — simulate failed fresh cancel write

    with pytest.raises(PersistenceError):
        AgentProcessHandle.load_persisted_cancel(process_id, store=store)


@pytest.mark.asyncio
async def test_run_with_agent_process_separates_lifecycle_id_from_cancel_key(tmp_path) -> None:
    """Lifecycle process_id can be attempt-unique while cancel_key is restart-stable."""
    from ouroboros.persistence.checkpoint import CheckpointData, CheckpointStore

    store = CheckpointStore(base_path=tmp_path)
    store.initialize()
    cancel_key = "helper:stable-cancel-key"
    checkpoint = CheckpointData.create(
        seed_id=AgentProcessHandle._cancel_checkpoint_seed(cancel_key),  # noqa: SLF001
        phase="agent_process_cancelled",
        state={"status": "cancelled", "reason": "operator stop"},
    )
    assert store.save(checkpoint).is_ok
    event_store = _FakeEventStore()

    async def restarted_work(handle):
        assert handle.process_id == "helper:attempt-1"
        assert handle.should_cancel() is True
        return "teardown"

    restart_result = await run_with_agent_process(
        event_store=event_store,
        intent="helper",
        work_fn=restarted_work,
        checkpoint_store=store,
        process_id="helper:attempt-1",
        cancel_key=cancel_key,
    )

    assert restart_result == "teardown"
    assert {event.aggregate_id for event in event_store.appended} == {"helper:attempt-1"}

    fresh_called = False

    async def fresh_work(handle):
        nonlocal fresh_called
        assert handle.process_id == "helper:attempt-2"
        fresh_called = True
        return "fresh"

    fresh_result = await run_with_agent_process(
        event_store=_FakeEventStore(),
        intent="helper",
        work_fn=fresh_work,
        checkpoint_store=store,
        process_id="helper:attempt-2",
        cancel_key=cancel_key,
    )

    assert fresh_result == "fresh"
    assert fresh_called is True
