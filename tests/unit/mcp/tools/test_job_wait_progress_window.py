"""job_wait reuses the progress window while no progress row has landed (#2928 E2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import random
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import JobLinks, JobSnapshot, JobStatus
from ouroboros.mcp.tools import job_handlers as job_handlers_module
from ouroboros.mcp.tools.ac_tree_hud_handler import (
    format_subtask_progress_summary,
    summarize_subtask_events,
)
from ouroboros.mcp.tools.job_handlers import JobWaitHandler
from ouroboros.persistence.event_store import EventStore

_BASE = datetime(2026, 4, 22, tzinfo=UTC)
_EXECUTION = "exec_progress_window"
_WINDOW = job_handlers_module._JOB_SUBTASK_EVENT_PAGE_SIZE


async def _reference_progress_at_cursor(
    event_store: Any,
    execution_id: str,
    *,
    cursor: int | None = None,
    previous: tuple[dict[str, Any], int] | None = None,
) -> tuple[dict[str, Any], int]:
    """``_query_execution_progress_at_cursor`` as on origin/main: always re-reads."""
    del previous
    events, event_cursor = await job_handlers_module._query_recent_execution_progress_events(
        event_store, execution_id, cursor=cursor
    )
    progress = dict(job_handlers_module._EMPTY_PROGRESS)
    workflow_event = next(
        (event for event in reversed(events) if event.type == "workflow.progress.updated"),
        None,
    )
    subtask_events = [
        event
        for event in events
        if event.type
        in {"execution.node.created", "execution.node.updated", "execution.subtask.updated"}
    ]
    subtask_summary = summarize_subtask_events(subtask_events)
    subtask_progress = format_subtask_progress_summary(subtask_summary)
    if subtask_summary:
        progress.update(
            {
                "current_phase": "Sub-AC work",
                "activity": subtask_progress or "running",
                "sub_ac_completed": subtask_summary.get("completed_count"),
                "sub_ac_total": subtask_summary.get("total_count"),
                "sub_ac_executing": subtask_summary.get("executing_count"),
                "sub_ac_pending": subtask_summary.get("pending_count"),
                "sub_ac_failed": subtask_summary.get("failed_count"),
            }
        )
    if workflow_event is not None:
        data = workflow_event.data
        progress.update(
            {
                "ac_completed": data.get("completed_count"),
                "ac_total": data.get("total_count"),
                "current_phase": data.get("current_phase") or "Working",
                "activity": data.get("activity_detail") or data.get("activity") or "running",
            }
        )
    return progress, event_cursor


def _event(
    index: int, event_type: str, *, aggregate_id: str = _EXECUTION, **data: Any
) -> BaseEvent:
    return BaseEvent(
        id=f"evt_progress_window_{index}",
        type=event_type,
        aggregate_type="execution",
        aggregate_id=aggregate_id,
        timestamp=_BASE + timedelta(microseconds=index),
        data={"execution_id": aggregate_id, **data},
    )


def _workflow(index: int, completed: int, **extra: Any) -> BaseEvent:
    return _event(
        index,
        "workflow.progress.updated",
        completed_count=completed,
        total_count=4,
        current_phase=f"phase-{completed}",
        activity=f"step-{completed}",
        **extra,
    )


def _subtask(index: int, sub_task: int, status: str) -> BaseEvent:
    return _event(
        index,
        "execution.subtask.updated",
        sub_task_id=f"st_{sub_task}",
        content=f"subtask {sub_task}",
        status=status,
    )


async def _store(tmp_path, name: str) -> EventStore:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / name}")
    await store.initialize()
    return store


class _ReadLog:
    """Classify ``get_recent_aggregate_events`` calls made for the execution."""

    def __init__(self, store: EventStore) -> None:
        self.calls: list[dict[str, Any]] = []
        self._original = store.get_recent_aggregate_events

    async def __call__(self, *args: Any, **kwargs: Any) -> tuple[list[BaseEvent], int]:
        self.calls.append(kwargs)
        return await self._original(*args, **kwargs)

    @property
    def probes(self) -> int:
        return sum(call.get("limit") == 1 for call in self.calls)

    @property
    def latest_windows(self) -> int:
        return sum(
            call.get("limit") == _WINDOW and call.get("max_row_id") is None for call in self.calls
        )

    @property
    def baseline_windows(self) -> int:
        return sum(
            call.get("limit") == _WINDOW and call.get("max_row_id") is not None
            for call in self.calls
        )


async def test_head_probe_result_matches_full_window_read(tmp_path) -> None:
    """Threading ``previous`` returns what a full re-read returns after every append."""
    store = await _store(tmp_path, "equivalence.db")
    rng = random.Random(2928)
    try:
        previous: tuple[dict[str, Any], int] | None = None
        index = progress_rows = reused = 0
        for tick in range(120):
            # Between ticks: nothing, a few rows, or a burst; progress and other rows mixed.
            batch: list[BaseEvent] = []
            for _ in range(rng.choice((0, 0, 1, 3, 60))):
                index += 1
                kind = rng.randrange(6)
                if kind == 0:
                    event = _workflow(index, rng.randrange(5))
                elif kind == 1:
                    event = _subtask(
                        index, rng.randrange(30), rng.choice(("executing", "completed"))
                    )
                elif kind == 2:
                    event = _event(index, "execution.node.updated", node_id=f"n{index % 7}")
                elif kind == 3:
                    event = _event(index, "execution.tool.called", tool="Read")
                elif kind == 4:
                    event = _event(
                        index,
                        "workflow.progress.updated",
                        aggregate_id="exec_other",
                        completed_count=3,
                    )
                else:
                    event = _event(index, "execution.output", message="raw")
                progress_rows += kind <= 2
                batch.append(event)
            if batch:
                await store.append_batch(batch)
            with patch.object(
                store, "get_recent_aggregate_events", wraps=store.get_recent_aggregate_events
            ) as recent:
                fresh = await job_handlers_module._query_execution_progress_at_cursor(
                    store, _EXECUTION, previous=previous
                )
            assert fresh == await _reference_progress_at_cursor(store, _EXECUTION), tick
            reused += previous is not None and recent.call_count == 1
            previous = fresh
        assert progress_rows > _WINDOW  # the 500-row window slid during the run
        assert reused > 10  # the reuse path was exercised, not only full reads
    finally:
        await store.close()


async def test_head_probe_is_skipped_for_cursored_reads_and_first_reads(tmp_path) -> None:
    store = await _store(tmp_path, "first.db")
    try:
        await store.append(_workflow(1, 1))
        log = _ReadLog(store)
        with patch.object(store, "get_recent_aggregate_events", log):
            first = await job_handlers_module._query_execution_progress_at_cursor(store, _EXECUTION)
            at_cursor = await job_handlers_module._query_execution_progress_at_cursor(
                store, _EXECUTION, cursor=1, previous=first
            )
        assert first == at_cursor == await _reference_progress_at_cursor(store, _EXECUTION)
        assert (log.probes, log.latest_windows, log.baseline_windows) == (0, 1, 1)
    finally:
        await store.close()


@pytest.mark.parametrize("other", ["exec_other", "exec_empty"])
async def test_previous_from_another_execution_matches_full_read(tmp_path, other: str) -> None:
    """A head rowid names one row of one aggregate; only two empty windows compare equal."""
    store = await _store(tmp_path, "other.db")
    try:
        await store.append(_workflow(1, 2))
        await store.append(_event(2, "workflow.progress.updated", aggregate_id="exec_other"))
        for previous_execution in (_EXECUTION, "exec_none"):
            previous = await job_handlers_module._query_execution_progress_at_cursor(
                store, previous_execution
            )
            assert await job_handlers_module._query_execution_progress_at_cursor(
                store, other, previous=previous
            ) == await _reference_progress_at_cursor(store, other)
    finally:
        await store.close()


async def test_store_without_recent_reader_keeps_fallback_path(tmp_path) -> None:
    """Stores lacking ``get_recent_aggregate_events`` never probe and always re-read."""
    store = await _store(tmp_path, "fallback.db")
    try:
        await store.append(_workflow(1, 2))

        class ForwardOnlyStore:
            def __init__(self) -> None:
                self.reads = 0

            async def get_events_after(self, *args: Any, **kwargs: Any) -> Any:
                self.reads += 1
                return await store.get_events_after(*args, **kwargs)

        legacy = ForwardOnlyStore()
        previous = None
        for _ in range(3):
            previous = await job_handlers_module._query_execution_progress_at_cursor(
                legacy, _EXECUTION, previous=previous
            )
            assert previous == await _reference_progress_at_cursor(legacy, _EXECUTION)
        assert legacy.reads == 6  # 3 handler reads + 3 reference reads
    finally:
        await store.close()


class _TickJobManager:
    """Returns a fixed running snapshot; optionally appends one row on a given tick."""

    def __init__(self, snapshot: JobSnapshot, store: EventStore, append_on_tick: int | None):
        self.snapshot = snapshot
        self.store = store
        self.append_on_tick = append_on_tick
        self.ticks = 0

    async def wait_for_change(
        self, job_id: str, *, cursor: int, timeout_seconds: int
    ) -> tuple[JobSnapshot, bool]:
        self.ticks += 1
        if self.ticks == self.append_on_tick:
            await self.store.append(_workflow(900, 3, activity_detail="landed mid-wait"))
        return self.snapshot, False


async def _seeded_store(tmp_path, name: str) -> tuple[EventStore, int]:
    store = await _store(tmp_path, name)
    rows = [_workflow(0, 1)] + [
        _subtask(index, index % 40, "executing" if index % 3 else "completed")
        for index in range(1, 700)
    ]
    await store.append_batch(rows)
    # Non-progress noise after the cursor must not count as a progress row.
    cursor = await store.get_current_rowid()
    await store.append(_event(800, "execution.tool.called", tool="Read"))
    return store, cursor


@pytest.mark.parametrize("stream", ["progress", "linked"])
@pytest.mark.parametrize("append_on_tick", [None, 3])
async def test_job_wait_reuses_window_on_idle_ticks_with_identical_result(
    tmp_path, stream: str, append_on_tick: int | None
) -> None:
    """Idle ticks probe one row; a progress row landing mid-wait still wakes the call.

    Each case runs twice on identically seeded stores: once as shipped and once
    with the origin/main implementation swapped in. Text and meta must match.
    """

    def wait_args(cursor: int) -> dict[str, Any]:
        return {
            "job_id": "job_progress_window",
            "cursor": cursor,
            "timeout_seconds": 2,
            "stream": stream,
            "wait_for": "ac_change",
            "view": "summary",
        }

    async def run(name: str, *, reference: bool) -> tuple[Any, _ReadLog, int]:
        store, cursor = await _seeded_store(tmp_path, name)
        snapshot = JobSnapshot(
            job_id="job_progress_window",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=_BASE,
            updated_at=_BASE,
            cursor=cursor,
            links=JobLinks(execution_id=_EXECUTION),
        )
        manager = _TickJobManager(snapshot, store, append_on_tick)
        handler = JobWaitHandler(event_store=store, job_manager=manager)
        log = _ReadLog(store)
        implementation = (
            _reference_progress_at_cursor
            if reference
            else job_handlers_module._query_execution_progress_at_cursor
        )
        try:
            with (
                patch.object(store, "get_recent_aggregate_events", log),
                patch.object(
                    job_handlers_module, "_query_execution_progress_at_cursor", implementation
                ),
            ):
                result = await handler.handle(wait_args(cursor))
        finally:
            await store.close()
        assert result.is_ok
        return result.value, log, manager.ticks

    shipped, log, ticks = await run("shipped.db", reference=False)
    reference, reference_log, reference_ticks = await run("reference.db", reference=True)

    landed = 0 if append_on_tick is None else 1
    if landed:
        assert ticks == reference_ticks == append_on_tick
        assert shipped.meta["changed"] is True
        assert shipped.meta["ac_completed"] == 3
    else:
        assert ticks >= 3  # several idle 0.5 s ticks before the 2 s deadline
        assert shipped.meta["ac_completed"] == 1
    # One baseline and one latest full window, then only 1-row probes; the tick
    # that sees the appended row re-reads the full window once more.
    assert log.baseline_windows == 1
    assert log.latest_windows == 1 + landed
    assert log.probes == ticks - 1
    assert (reference_log.probes, reference_log.latest_windows) == (0, reference_ticks)
    # The tick count of an idle run is timing-dependent, but no field depends on it.
    assert shipped.meta == reference.meta
    assert shipped.text_content == reference.text_content
