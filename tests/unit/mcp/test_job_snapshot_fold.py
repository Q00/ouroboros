"""Resumable job-snapshot fold and network idle-shutdown lifecycle."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from ouroboros.cli.commands.mcp import _effective_idle_timeout, _idle_shutdown_loop
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import JobManager, JobStatus
from ouroboros.mcp.job_snapshot import fold_job_events
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


def _build_store(tmp_path) -> EventStore:
    db_path = tmp_path / "jobs.db"
    return EventStore(f"sqlite+aiosqlite:///{db_path}")


async def _blocked_runner(release: asyncio.Event) -> MCPToolResult:
    await release.wait()
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
        is_error=False,
    )


async def _drain_manager(manager: JobManager) -> None:
    tasks = [
        *manager._tasks.values(),
        *manager._runner_tasks.values(),
        *manager._monitors.values(),
    ]
    for task in tasks:
        if not task.done():
            task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def _job_event(event_type: str, job_id: str, data: dict[str, Any], at: datetime) -> BaseEvent:
    return BaseEvent(
        type=event_type,
        aggregate_type="job",
        aggregate_id=job_id,
        data=data,
        timestamp=at,
    )


class TestFoldJobEvents:
    def test_batched_fold_equals_single_fold(self) -> None:
        base = datetime.now(UTC)
        events = [
            _job_event(
                "mcp.job.started",
                "job-1",
                {
                    "job_type": "execute",
                    "status": "queued",
                    "message": "queued",
                    "links": {"session_id": "s-1"},
                },
                base,
            ),
            _job_event(
                "mcp.job.updated",
                "job-1",
                {
                    "status": "running",
                    "message": "working",
                    "links": {"execution_id": "e-1"},
                },
                base + timedelta(seconds=1),
            ),
            _job_event(
                "mcp.job.completed",
                "job-1",
                {
                    "status": "completed",
                    "message": "done",
                    "result_text": "all green",
                    "result_meta": {"final_approved": True},
                },
                base + timedelta(seconds=2),
            ),
        ]

        whole = fold_job_events(None, events, cursor=3).to_snapshot("job-1")
        partial = fold_job_events(None, events[:1], cursor=1)
        partial = fold_job_events(partial, events[1:2], cursor=2)
        resumed = fold_job_events(partial, events[2:], cursor=3).to_snapshot("job-1")

        assert resumed == whole
        assert resumed.status is JobStatus.COMPLETED
        assert resumed.links.session_id == "s-1"
        assert resumed.links.execution_id == "e-1"
        assert resumed.result_meta == {"final_approved": True}
        assert resumed.cursor == 3

    def test_empty_batch_preserves_state(self) -> None:
        base = datetime.now(UTC)
        events = [
            _job_event("mcp.job.started", "job-1", {"job_type": "qa", "status": "running"}, base),
        ]
        fold = fold_job_events(None, events, cursor=1)
        snapshot_before = fold.to_snapshot("job-1")
        fold = fold_job_events(fold, [], cursor=1)
        assert fold.to_snapshot("job-1") == snapshot_before


class TestGetSnapshotTail:
    async def test_tail_matches_full_replay_across_appends(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        fresh_reader = JobManager(store)
        release = asyncio.Event()
        try:
            started = await manager.start_job(
                job_type="execute",
                initial_message="queued",
                runner=_blocked_runner(release),
            )
            job_id = started.job_id
            # Let the startup RUNNING append land so the stream is quiescent
            # and both readers observe the same rows.
            deadline = asyncio.get_running_loop().time() + 2.0
            previous_cursor = -1
            while asyncio.get_running_loop().time() < deadline:
                current = await fresh_reader.get_snapshot(job_id)
                if current.cursor == previous_cursor:
                    break
                previous_cursor = current.cursor
                await asyncio.sleep(0.05)
            for step in range(3):
                await manager.update_status(job_id, JobStatus.RUNNING, f"step {step}")
                tail = await manager._get_snapshot_tail(job_id)
                # A manager with no fold cache performs the historical full
                # replay; the resumable fold must agree with it exactly.
                full = await fresh_reader.get_snapshot(job_id)
                assert tail == full
                assert tail.message == f"step {step}"
        finally:
            release.set()
            await _drain_manager(manager)
            await store.close()

    async def test_tail_reads_only_rows_after_cursor(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        release = asyncio.Event()
        seen_cursors: list[int] = []
        original = store.get_events_after

        async def spying_get_events_after(
            aggregate_type: str, aggregate_id: str, last_row_id: int = 0, **kwargs: Any
        ):
            if aggregate_type == "job":
                seen_cursors.append(last_row_id)
            return await original(aggregate_type, aggregate_id, last_row_id, **kwargs)

        try:
            started = await manager.start_job(
                job_type="execute",
                initial_message="queued",
                runner=_blocked_runner(release),
            )
            job_id = started.job_id
            await manager._get_snapshot_tail(job_id)  # prime the fold cache
            store.get_events_after = spying_get_events_after  # type: ignore[method-assign]
            await manager.update_status(job_id, JobStatus.RUNNING, "later")
            snapshot = await manager._get_snapshot_tail(job_id)
            assert snapshot.message == "later"
            assert seen_cursors, "tail read must hit the event store"
            assert all(cursor > 0 for cursor in seen_cursors)
        finally:
            store.get_events_after = original  # type: ignore[method-assign]
            release.set()
            await _drain_manager(manager)
            await store.close()

    async def test_tail_without_cache_raises_for_unknown_job(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        try:
            with pytest.raises(ValueError, match="Job not found"):
                await manager._get_snapshot_tail("job-missing")
        finally:
            await store.close()

    async def test_cleanup_drops_fold_cache_entry(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        try:
            started = await manager.start_job(
                job_type="execute",
                initial_message="queued",
                runner=_ok_result(),
            )
            job_id = started.job_id
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                snapshot = await manager._get_snapshot_tail(job_id)
                if snapshot.is_terminal:
                    break
                await asyncio.sleep(0.01)
            assert job_id in manager._fold_cache
            cleaned = await manager.cleanup_expired_jobs(ttl=timedelta(seconds=0))
            assert cleaned >= 1
            assert job_id not in manager._fold_cache
        finally:
            await _drain_manager(manager)
            await store.close()


async def _ok_result() -> MCPToolResult:
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
        is_error=False,
    )


class _FakeServer:
    def __init__(self, idle_for: float) -> None:
        self.seconds_since_last_tool_call = idle_for


class TestIdleShutdown:
    def test_effective_idle_timeout_defaults(self) -> None:
        assert _effective_idle_timeout("stdio", None) == 0.0
        assert _effective_idle_timeout("streamable-http", None) == 7200.0
        assert _effective_idle_timeout("sse", None) == 7200.0
        assert _effective_idle_timeout("streamable-http", 0.0) == 0.0
        assert _effective_idle_timeout("stdio", 30.0) == 30.0

    async def test_sets_stop_once_idle_threshold_is_crossed(self) -> None:
        stop = asyncio.Event()
        server = _FakeServer(idle_for=120.0)
        await asyncio.wait_for(
            _idle_shutdown_loop(stop, server, 60.0, poll_seconds=0.001),
            timeout=1.0,
        )
        assert stop.is_set()

    async def test_keeps_waiting_while_tool_calls_arrive(self) -> None:
        stop = asyncio.Event()
        server = _FakeServer(idle_for=1.0)
        task = asyncio.create_task(_idle_shutdown_loop(stop, server, 60.0, poll_seconds=0.001))
        await asyncio.sleep(0.05)
        assert not stop.is_set()
        assert not task.done()
        server.seconds_since_last_tool_call = 999.0
        await asyncio.wait_for(task, timeout=1.0)
        assert stop.is_set()

    async def test_exits_quietly_when_stop_already_set(self) -> None:
        stop = asyncio.Event()
        stop.set()
        await asyncio.wait_for(
            _idle_shutdown_loop(stop, _FakeServer(0.0), 60.0, poll_seconds=0.001),
            timeout=1.0,
        )

    async def test_bows_out_when_server_lacks_idle_gauge(self) -> None:
        stop = asyncio.Event()
        await asyncio.wait_for(
            _idle_shutdown_loop(stop, object(), 60.0, poll_seconds=0.001),
            timeout=1.0,
        )
        assert not stop.is_set()
