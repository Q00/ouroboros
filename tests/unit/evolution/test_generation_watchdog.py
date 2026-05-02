"""Tests for progress-aware generation watchdog controls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.config.models import RuntimeControlsConfig
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import lineage_generation_failed
import ouroboros.evolution.watchdog as watchdog_module
from ouroboros.evolution.watchdog import (
    GenerationProgressWatchdog,
    GenerationWatchdogTimeout,
)
from ouroboros.orchestrator.execution_runtime_scope import build_ac_runtime_scope
from ouroboros.persistence.event_store import EventStore


class _FakeMonotonicClock:
    def __init__(self) -> None:
        self.current = 0.0

    def __call__(self) -> float:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += seconds


async def _store() -> EventStore:
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    await event_store.initialize()
    return event_store


def _workflow_progress(
    execution_id: str,
    *,
    completed_count: int,
    status: str = "executing",
    session_id: str = "session-1",
) -> BaseEvent:
    return BaseEvent(
        type="workflow.progress.updated",
        aggregate_type="execution",
        aggregate_id=execution_id,
        data={
            "session_id": session_id,
            "acceptance_criteria": [
                {
                    "index": 1,
                    "content": "AC 1",
                    "status": "completed" if completed_count else status,
                },
                {
                    "index": 2,
                    "content": "AC 2",
                    "status": status,
                },
            ],
            "completed_count": completed_count,
            "total_count": 2,
            "current_phase": "Deliver",
            "activity": "Monitoring",
        },
    )


def _session_started(session_id: str, execution_id: str) -> BaseEvent:
    return BaseEvent(
        type="orchestrator.session.started",
        aggregate_type="session",
        aggregate_id=session_id,
        data={
            "execution_id": execution_id,
            "seed_id": "seed-watch",
            "start_time": "2026-01-01T00:00:00+00:00",
        },
    )


def _session_tool_called(session_id: str) -> BaseEvent:
    return BaseEvent(
        type="orchestrator.tool.called",
        aggregate_type="session",
        aggregate_id=session_id,
        data={"tool_name": "Bash", "called_at": "2026-01-01T00:00:00+00:00"},
    )


def _ac_heartbeat(session_id: str, ac_id: str, message_count: int) -> BaseEvent:
    return BaseEvent(
        type="execution.ac.heartbeat",
        aggregate_type="execution",
        aggregate_id=ac_id,
        data={
            "session_id": session_id,
            "ac_index": 0,
            "elapsed_seconds": float(message_count),
            "message_count": message_count,
            "timestamp": "2026-01-01T00:00:00+00:00",
        },
    )


def _decomposition_level_event(session_id: str, event_type: str, level: int) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type="execution",
        aggregate_id=session_id,
        data={
            "level": level,
            "total_levels": 2,
            "child_indices": [0],
            "ac_count": 1,
            "successful": 1,
            "failed": 0,
            "blocked": 0,
            "total": 1,
        },
    )


def _watchdog(
    event_store: EventStore,
    *,
    lineage_id: str = "lin-watch",
    generation_number: int = 1,
    execution_id: str = "exec-watch",
    **control_overrides: Any,
) -> GenerationProgressWatchdog:
    control_values = {
        "generation_idle_timeout_seconds": 1.0,
        "generation_no_progress_timeout_seconds": 1.0,
        "generation_safety_timeout_seconds": 0,
        "watchdog_poll_seconds": 0.02,
        **control_overrides,
    }
    controls = RuntimeControlsConfig(**control_values)
    return GenerationProgressWatchdog(
        event_store=event_store,
        lineage_id=lineage_id,
        generation_number=generation_number,
        execution_id=execution_id,
        controls=controls,
    )


@pytest.mark.asyncio
async def test_productive_long_run_resets_material_progress_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Material progress keeps a generation alive past the no-progress window."""
    clock = _FakeMonotonicClock()
    monkeypatch.setattr(watchdog_module, "time", SimpleNamespace(monotonic=clock))
    event_store = await _store()
    execution_id = "exec-productive"
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_no_progress_timeout_seconds=0.09,
        watchdog_poll_seconds=0.005,
    )

    async def productive_work() -> str:
        for completed in (0, 1, 2):
            await asyncio.sleep(0.01)
            clock.advance(0.05)
            await event_store.append(_workflow_progress(execution_id, completed_count=completed))
        await asyncio.sleep(0.01)
        clock.advance(0.05)
        return "done"

    assert await watchdog.watch(productive_work()) == "done"


@pytest.mark.asyncio
async def test_busy_run_without_material_progress_times_out() -> None:
    """Activity alone does not count as material progress."""
    event_store = await _store()
    lineage_id = "lin-busy"
    execution_id = "exec-busy"
    watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        execution_id=execution_id,
        generation_no_progress_timeout_seconds=0.07,
    )

    async def busy_work() -> str:
        await event_store.append(_workflow_progress(execution_id, completed_count=0))
        try:
            while True:
                await asyncio.sleep(0.02)
                await event_store.append(_workflow_progress(execution_id, completed_count=0))
        except asyncio.CancelledError:
            await event_store.append(
                lineage_generation_failed(
                    lineage_id,
                    1,
                    "cancelled",
                    "Generation cancelled",
                )
            )
            raise

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(busy_work())

    assert exc_info.value.timeout_kind == "no_material_progress_timeout"
    events = await event_store.replay("lineage", lineage_id)
    assert any(event.type == "lineage.generation.watchdog_decision" for event in events)
    assert events[-1].type == "lineage.generation.watchdog_decision"


@pytest.mark.asyncio
async def test_session_activity_resets_idle_timeout() -> None:
    """Session aggregate tool/message events prove generation liveness."""
    event_store = await _store()
    session_id = "session-active"
    execution_id = "exec-session-active"
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=0.07,
        generation_no_progress_timeout_seconds=0,
    )

    async def session_work() -> str:
        await event_store.append(_session_started(session_id, execution_id))
        for _ in range(4):
            await asyncio.sleep(0.04)
            await event_store.append(_session_tool_called(session_id))
        return "done"

    assert await watchdog.watch(session_work()) == "done"


@pytest.mark.asyncio
async def test_ac_heartbeat_aggregate_resets_idle_timeout() -> None:
    """AC heartbeats are emitted under AC aggregate IDs, not the execution ID."""
    event_store = await _store()
    session_id = "session-heartbeat"
    execution_id = "evolve:lin-heartbeat:generation:1"
    ac_id = build_ac_runtime_scope(0, execution_context_id=execution_id).aggregate_id
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=0.07,
        generation_no_progress_timeout_seconds=0,
    )

    async def heartbeat_work() -> str:
        await event_store.append(_session_started(session_id, execution_id))
        for count in range(1, 5):
            await asyncio.sleep(0.04)
            await event_store.append(_ac_heartbeat(session_id, ac_id, count))
        return "done"

    assert await watchdog.watch(heartbeat_work()) == "done"


@pytest.mark.asyncio
async def test_session_scoped_decomposition_events_reset_material_progress_timeout() -> None:
    """Decomposition level progress is stored as execution events keyed by session ID."""
    event_store = await _store()
    session_id = "session-levels"
    execution_id = "exec-levels"
    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=1,
        generation_no_progress_timeout_seconds=0.07,
    )

    async def decomposition_work() -> str:
        await event_store.append(_session_started(session_id, execution_id))
        await asyncio.sleep(0.04)
        await event_store.append(
            _decomposition_level_event(
                session_id,
                "execution.decomposition.level_started",
                0,
            )
        )
        await asyncio.sleep(0.04)
        await event_store.append(
            _decomposition_level_event(
                session_id,
                "execution.decomposition.level_completed",
                0,
            )
        )
        await asyncio.sleep(0.04)
        return "done"

    assert await watchdog.watch(decomposition_work()) == "done"


@pytest.mark.asyncio
async def test_idle_generation_times_out_without_activity() -> None:
    """Silent generations are still bounded by idle timeout."""
    event_store = await _store()
    watchdog = _watchdog(
        event_store,
        generation_idle_timeout_seconds=0.05,
        generation_no_progress_timeout_seconds=0,
    )

    async def silent_work() -> str:
        await asyncio.sleep(0.2)
        return "late"

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(silent_work())

    assert exc_info.value.timeout_kind == "idle_timeout"


@pytest.mark.asyncio
async def test_retried_generation_does_not_count_stale_events_as_activity() -> None:
    """Baseline cursors skip events from prior attempts with the same execution ID."""
    event_store = await _store()
    lineage_id = "lin-retry"
    execution_id = "evolve:lin-retry:generation:1"
    session_id = "session-retry-old"
    ac_id = build_ac_runtime_scope(0, execution_context_id=execution_id).aggregate_id
    await event_store.append(
        BaseEvent(
            type="lineage.generation.started",
            aggregate_type="lineage",
            aggregate_id=lineage_id,
            data={"generation_number": 1},
        )
    )
    await event_store.append(_workflow_progress(execution_id, completed_count=1))
    await event_store.append(_session_started(session_id, execution_id))
    await event_store.append(_ac_heartbeat(session_id, ac_id, 1))

    watchdog = _watchdog(
        event_store,
        lineage_id=lineage_id,
        execution_id=execution_id,
        generation_idle_timeout_seconds=0.05,
        generation_no_progress_timeout_seconds=0,
    )

    async def silent_retry() -> str:
        await asyncio.sleep(0.2)
        return "late"

    with pytest.raises(GenerationWatchdogTimeout) as exc_info:
        await watchdog.watch(silent_retry())

    assert exc_info.value.timeout_kind == "idle_timeout"
    assert exc_info.value.details["activity_event_count"] == 0
    assert exc_info.value.details["material_event_count"] == 0
    assert exc_info.value.details["last_event_type"] is None


@pytest.mark.asyncio
async def test_late_discovered_session_starts_from_attempt_baseline() -> None:
    """A newly discovered session must not backfill rows from before the attempt."""
    event_store = await _store()
    execution_id = "evolve:lin-late-session:generation:1"
    session_id = "session-late-discovery"
    await event_store.append(_session_tool_called(session_id))

    watchdog = _watchdog(
        event_store,
        execution_id=execution_id,
        generation_idle_timeout_seconds=1,
        generation_no_progress_timeout_seconds=0,
    )
    await watchdog.initialize_baseline()
    await event_store.append(
        _workflow_progress(
            execution_id,
            completed_count=0,
            session_id=session_id,
        )
    )

    await watchdog.poll()

    assert watchdog._activity_event_count == 1
    assert watchdog._last_event_type == "workflow.progress.updated"
    assert session_id in watchdog._session_cursors
    assert watchdog._session_cursors[session_id] >= watchdog._attempt_start_cursor


@pytest.mark.asyncio
async def test_parent_cancellation_cancels_watched_generation() -> None:
    """Cancelling the watchdog wrapper cancels the child generation task."""
    event_store = await _store()
    watchdog = _watchdog(event_store, generation_idle_timeout_seconds=10)
    child_cancelled = asyncio.Event()

    async def long_work() -> str:
        try:
            await asyncio.sleep(10)
            return "late"
        except asyncio.CancelledError:
            child_cancelled.set()
            raise

    parent = asyncio.create_task(watchdog.watch(long_work()))
    await asyncio.sleep(0.05)
    parent.cancel()

    with pytest.raises(asyncio.CancelledError):
        await parent
    await asyncio.wait_for(child_cancelled.wait(), timeout=1)
