"""Tests for async MCP job management."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.events.lineage import lineage_generation_watchdog_decision
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobSnapshot, JobStatus
from ouroboros.mcp.tools.job_handlers import (
    JobStatusHandler,
    JobWaitHandler,
    _render_compact_job_snapshot,
    _render_job_snapshot,
    _render_job_snapshot_inner,
)
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.agent_process import AgentProcessHandle
from ouroboros.orchestrator.heartbeat import acquire as acquire_session_lock
from ouroboros.orchestrator.heartbeat import lock_path
from ouroboros.orchestrator.heartbeat import release as release_session_lock
from ouroboros.orchestrator.runner import clear_cancellation, is_cancellation_requested
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore, PersistenceError


def _build_store(tmp_path) -> EventStore:
    db_path = tmp_path / "jobs.db"
    return EventStore(f"sqlite+aiosqlite:///{db_path}")


async def _cancel_manager_tasks(manager: JobManager) -> None:
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


async def _wait_for_job_status(
    manager: JobManager,
    job_id: str,
    status: JobStatus,
    *,
    timeout: float = 1.0,
) -> JobSnapshot:
    deadline = asyncio.get_running_loop().time() + timeout
    last_snapshot: JobSnapshot | None = None
    while asyncio.get_running_loop().time() < deadline:
        last_snapshot = await manager.get_snapshot(job_id)
        if last_snapshot.status is status:
            return last_snapshot
        await asyncio.sleep(0.01)
    if last_snapshot is None:
        last_snapshot = await manager.get_snapshot(job_id)
    raise AssertionError(f"job {job_id} did not become {status}; last={last_snapshot.status}")


class TestJobManager:
    """Test background job lifecycle behavior."""

    async def test_status_message_reports_generation_watchdog_decision(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()

        try:
            await store.append(
                lineage_generation_watchdog_decision(
                    "lin_watchdog",
                    3,
                    "timeout",
                    "Generation had no material progress for 14400.0s",
                )
            )
            now = datetime.now(UTC)
            snapshot = JobSnapshot(
                job_id="job_watchdog",
                job_type="evolve_step",
                status=JobStatus.RUNNING,
                message="Running evolve_step",
                created_at=now,
                updated_at=now,
                links=JobLinks(lineage_id="lin_watchdog"),
            )

            message = await manager._derive_status_message(snapshot)

            expected = (
                "Generation 3 watchdog timeout | Generation had no material progress for 14400.0s"
            )
            assert message == expected
        finally:
            await store.close()

    async def test_render_job_snapshot_reports_generation_watchdog_decision(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                lineage_generation_watchdog_decision(
                    "lin_watchdog_render",
                    4,
                    "timeout",
                    "Generation idle for 7200.0s",
                )
            )
            snapshot = JobSnapshot(
                job_id="job_watchdog_render",
                job_type="evolve_step",
                status=JobStatus.RUNNING,
                message="Running evolve_step",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                links=JobLinks(lineage_id="lin_watchdog_render"),
            )

            text, _ = await _render_job_snapshot_inner(snapshot, store)

            assert "**Current Step**: Gen 4 watchdog timeout" in text
            assert "**Reason**: Generation idle for 7200.0s" in text
        finally:
            await store.close()

    async def test_monitor_completes_job_when_execution_terminal_is_complete(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_complete", execution_id="exec_complete"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_complete",
                    data={
                        "completed_count": 2,
                        "total_count": 2,
                        "current_phase": "Deliver",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_complete",
                    data={"session_id": "orch_complete", "status": "completed"},
                )
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert snapshot.result_text == "Execution complete: 2/2 ACs completed"
            assert snapshot.result_meta["completed_from_execution_terminal"] is True
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            terminal_events = [
                event
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert [event.type for event in terminal_events] == ["mcp.job.completed"]
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_requested_wins_over_complete_execution_terminal(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_cancel", execution_id="exec_cancel"),
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_cancel",
                    data={"session_id": "orch_cancel", "status": "completed"},
                )
            )

            snapshot = await manager.cancel_job(started.job_id)
            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}

            await asyncio.sleep(1.2)
            snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_execution_completion_persists_before_runner_cancellation_finishes(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            cancel_seen = asyncio.Event()
            release = asyncio.Event()

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancel_seen.set()
                    await release.wait()
                    raise
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_wait", execution_id="exec_wait"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_wait",
                    data={"completed_count": 1, "total_count": 1},
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_wait",
                    data={"session_id": "orch_wait", "status": "completed"},
                )
            )

            await asyncio.wait_for(cancel_seen.wait(), timeout=2)
            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            release.set()

            assert snapshot.result_text == "Execution complete: 1/1 ACs completed"
            recovered = JobManager(store)
            assert (await recovered.get_snapshot(started.job_id)).status is JobStatus.COMPLETED
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            assert [
                event.type
                for event in events
                if event.type.startswith("mcp.job.") and event.type != "mcp.job.updated"
            ].count("mcp.job.completed") == 1
        finally:
            release.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_execution_completion_does_not_depend_on_runner_cooperative_cancel(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            cancel_seen = asyncio.Event()
            release = asyncio.Event()

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancel_seen.set()
                    try:
                        await release.wait()
                    except asyncio.CancelledError:
                        raise
                    raise
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_stubborn", execution_id="exec_stubborn"),
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_stubborn",
                    data={"session_id": "orch_stubborn", "status": "completed"},
                )
            )

            await asyncio.wait_for(cancel_seen.wait(), timeout=2)
            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert snapshot.result_meta["completed_from_execution_terminal"] is True
            events, _ = await store.get_events_after("job", started.job_id, last_row_id=0)
            terminal_events = [
                event.type
                for event in events
                if event.type
                in {
                    "mcp.job.completed",
                    "mcp.job.failed",
                    "mcp.job.cancelled",
                    "mcp.job.interrupted",
                }
            ]
            assert terminal_events == ["mcp.job.completed"]
        finally:
            release.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_complete_workflow_progress_without_terminal_event_does_not_complete_job(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_progress_only", execution_id="exec_progress_only"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_progress_only",
                    data={"completed_count": 1, "total_count": 1},
                )
            )

            await asyncio.sleep(1.2)
            snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.is_terminal is False
            assert snapshot.result_meta.get("completed_from_execution_terminal") is not True
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_execution_terminal_ignores_incomplete_progress_for_result_text(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:
            stop = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await stop.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="execute_seed",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_partial_progress", execution_id="exec_partial"),
            )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_partial",
                    data={"completed_count": 1, "total_count": 2},
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.terminal",
                    aggregate_type="execution",
                    aggregate_id="exec_partial",
                    data={"session_id": "orch_partial_progress", "status": "completed"},
                )
            )

            snapshot = await _wait_for_job_status(
                manager, started.job_id, JobStatus.COMPLETED, timeout=2.0
            )

            assert snapshot.result_text == "Execution complete"
            assert snapshot.result_meta["completed_from_execution_terminal"] is True
        finally:
            stop.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_completes_and_persists_result(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(0.05)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="done"),),
                    is_error=False,
                    meta={"kind": "test"},
                )

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            snapshot = await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            assert snapshot.status == JobStatus.COMPLETED
            assert snapshot.result_text == "done"
            assert snapshot.result_meta["kind"] == "test"
        finally:
            await store.close()

    async def test_start_job_default_allocates_job_id(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult()

            started = await manager.start_job(
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            assert started.job_id.startswith("job_")
            assert len(started.job_id) == len("job_") + 12
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_accepts_preallocated_job_id_once(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult()

            job_id = await manager.allocate_job_id()
            started = await manager.start_job(
                job_id=job_id,
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            assert started.job_id == job_id
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_rejects_existing_job_id(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult()

            job_id = await manager.allocate_job_id()
            await manager.start_job(
                job_id=job_id,
                job_type="test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            try:
                runner = asyncio.get_running_loop().create_future()
                runner.set_result(MCPToolResult())
                await manager.start_job(
                    job_id=job_id,
                    job_type="test",
                    initial_message="queued again",
                    runner=runner,
                    links=JobLinks(),
                )
            except ValueError as exc:
                assert str(exc) == f"Job already exists: {job_id}"
            else:
                raise AssertionError("expected duplicate job_id to be rejected")
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_start_job_tracks_externally_created_task(self, tmp_path) -> None:
        """A pre-built Task is registered in ``_runner_tasks`` for cancellation routing."""
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(0.02)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ext"),),
                    is_error=False,
                )

            external_task = asyncio.create_task(_runner())
            started = await manager.start_job(
                job_type="external",
                initial_message="queued",
                runner=external_task,
                links=JobLinks(),
            )

            assert manager._runner_tasks.get(started.job_id) is external_task

            snapshot = await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)
            assert snapshot.status == JobStatus.COMPLETED
            assert started.job_id not in manager._runner_tasks
        finally:
            await store.close()

    async def test_start_job_wraps_bare_future_runner(self, tmp_path) -> None:
        """A bare Future is wrapped in a Task and still completes the job."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        future: asyncio.Future[MCPToolResult] = asyncio.get_running_loop().create_future()

        try:
            started = await manager.start_job(
                job_type="future",
                initial_message="queued",
                runner=future,
                links=JobLinks(),
            )
            runner_task = manager._runner_tasks.get(started.job_id)

            assert isinstance(runner_task, asyncio.Task)
            assert runner_task is not future

            future.set_result(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="future"),),
                    is_error=False,
                    meta={"kind": "future"},
                )
            )
            snapshot = await _wait_for_job_status(manager, started.job_id, JobStatus.COMPLETED)

            assert snapshot.status == JobStatus.COMPLETED
            assert snapshot.result_text == "future"
            assert snapshot.result_meta["kind"] == "future"
            assert started.job_id not in manager._runner_tasks
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_cancels_externally_created_task(self, tmp_path) -> None:
        """Cancellation reaches a pre-built Task registered as the runner."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        runner_cancelled = asyncio.Event()

        try:

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            external_task = asyncio.create_task(_runner())
            started = await manager.start_job(
                job_type="external-cancel",
                initial_message="queued",
                runner=external_task,
                links=JobLinks(),
            )

            await manager.cancel_job(started.job_id)
            await asyncio.wait_for(runner_cancelled.wait(), timeout=1)
            await asyncio.sleep(0)
            snapshot = await manager.get_snapshot(started.job_id)

            assert external_task.cancelled()
            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_wait_for_change_returns_new_cursor(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(0.05)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="waited"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="wait-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            snapshot, changed = await manager.wait_for_change(
                started.job_id,
                cursor=started.cursor,
                timeout_seconds=2,
            )

            assert changed is True
            assert snapshot.cursor >= started.cursor
        finally:
            await store.close()

    async def test_cancel_job_persists_job_scoped_agent_process_cancel(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        checkpoint_store = CheckpointStore(tmp_path / "checkpoints")
        manager = JobManager(store, checkpoint_store=checkpoint_store)

        try:
            never_done = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await never_done.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="durable_cancel",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            await manager.cancel_job(started.job_id)

            found, reason = AgentProcessHandle.load_persisted_cancel(
                f"mcp_job:{started.job_id}", store=checkpoint_store
            )
            assert found is True
            assert reason == "Background job cancelled"
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_persist_failure_does_not_block_cancellation(
        self, tmp_path, monkeypatch
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        def _raise_persist(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
            raise RuntimeError("checkpoint unavailable")

        monkeypatch.setattr(manager, "_persist_durable_cancel", _raise_persist)

        try:
            never_done = asyncio.Event()

            async def _runner() -> MCPToolResult:
                await never_done.wait()
                return MCPToolResult()

            started = await manager.start_job(
                job_type="durable_cancel_best_effort",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            snapshot = await manager.cancel_job(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_cancels_non_session_task(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="cancel-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(),
            )

            await manager.cancel_job(started.job_id)
            await asyncio.sleep(0.1)
            snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
        finally:
            await store.close()

    async def test_cancel_job_does_not_mark_linked_session_when_task_already_done(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

        try:

            async def _runner() -> MCPToolResult:
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="race-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_done_123", execution_id="exec_done_123"),
            )
            task = manager._tasks[started.job_id]
            await task

            snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id="orch_done_123",
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id="exec_done_123",
                event_type="execution.terminal",
            )

            assert snapshot.is_terminal
            assert not session_cancelled
            assert not any(event.data.get("status") == "cancelled" for event in execution_cancelled)
        finally:
            await store.close()

    async def test_cancel_job_stops_task_when_linked_session_already_terminal(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_terminal_123"
        execution_id = "exec_terminal_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="terminal-session-race",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            repo = SessionRepository(store)
            mark_result = await repo.mark_completed(session_id)
            assert mark_result.is_ok

            snapshot = await manager.cancel_job(started.job_id)
            await asyncio.wait_for(runner_cancelled.wait(), timeout=1)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is False
            assert not session_cancelled
            assert not any(event.data.get("status") == "cancelled" for event in execution_cancelled)
        finally:
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_requests_linked_session_cancellation_without_start_event(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_pending_123"
        execution_id = "exec_pending_123"
        await clear_cancellation(session_id)
        lock_path(session_id).unlink(missing_ok=True)

        try:

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="pending-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            terminal_events = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )
            await asyncio.sleep(0)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is False
            assert not session_cancelled
            assert not terminal_events
            assert runner_task.done() is True
        finally:
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_clears_precreated_unstarted_session_cancellation(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_precreated_123"
        execution_id = "exec_precreated_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_precreated_123",
                session_id=session_id,
            )
            assert create_result.is_ok

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="precreated-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            snapshot = await manager.cancel_job(started.job_id)
            await asyncio.sleep(0)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is False
            assert runner_task.done() is True
            assert session_cancelled
            assert execution_cancelled[-1].data["status"] == "cancelled"
        finally:
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_preserves_signal_when_runner_starts_during_cancel(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_start_race_123"
        execution_id = "exec_start_race_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_start_race_123",
                session_id=session_id,
            )
            assert create_result.is_ok

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    acquire_session_lock(session_id)
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="start-race-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            snapshot = await manager.cancel_job(started.job_id)

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is True
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_persists_cross_process_linked_cancellation(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_cross_process_123"
        execution_id = "exec_cross_process_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_cross_process_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="cross-process-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert session_cancelled
            assert session_cancelled[-1].data["cancelled_by"] == "mcp_job_manager"
            assert execution_cancelled
            assert execution_cancelled[-1].data["status"] == "cancelled"
        finally:
            release_session_lock(session_id)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_does_not_persist_cross_process_cancel_when_reconstruct_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_reconstruct_fail_123"
        execution_id = "exec_reconstruct_fail_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="reconstruct-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            with patch(
                "ouroboros.mcp.job_manager.SessionRepository.reconstruct_session",
                new=AsyncMock(return_value=Result.err(PersistenceError("replay failed"))),
            ):
                snapshot = await manager.cancel_job(started.job_id)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert runner_cancelled.is_set() is True
            assert not session_cancelled
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_errors_before_persist_when_latest_reconstruct_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_latest_reconstruct_fail_123"
        execution_id = "exec_latest_reconstruct_fail_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_latest_reconstruct_fail_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="latest-reconstruct-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            original_reconstruct = SessionRepository.reconstruct_session
            call_count = 0

            async def _reconstruct_once_then_fail(self, target_session_id):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return await original_reconstruct(self, target_session_id)
                return Result.err(PersistenceError("replay failed"))

            with patch(
                "ouroboros.mcp.job_manager.SessionRepository.reconstruct_session",
                new=_reconstruct_once_then_fail,
            ):
                try:
                    await manager.cancel_job(started.job_id)
                except ValueError as exc:
                    assert "Failed to inspect linked session before cancellation" in str(exc)
                else:
                    raise AssertionError("cancel_job should fail when latest inspect fails")

            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert runner_cancelled.is_set() is True
            assert not session_cancelled
            assert not execution_cancelled
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_stops_task_when_linked_session_inspection_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_inspection_fail_123"
        execution_id = "exec_inspection_fail_123"
        await clear_cancellation(session_id)

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_inspection_fail_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                await asyncio.sleep(10)
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="inspection-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            with patch.object(
                store,
                "query_events",
                new=AsyncMock(side_effect=PersistenceError("query failed")),
            ):
                snapshot = await manager.cancel_job(started.job_id)
            await asyncio.sleep(0)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            execution_cancelled = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert runner_task.done() is True
            assert await is_cancellation_requested(session_id) is True
            assert session_cancelled
            assert session_cancelled[-1].data["cancelled_by"] == "mcp_job_manager"
            assert execution_cancelled
            assert execution_cancelled[-1].data["status"] == "cancelled"
        finally:
            release_session_lock(session_id)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_requests_cancellation_for_started_linked_runner(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_started_123"
        execution_id = "exec_started_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_started_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            acquire_session_lock(session_id)

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    return MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="cancelled"),),
                        is_error=False,
                    )
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="started-session-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )
            runner_task = manager._runner_tasks[started.job_id]

            snapshot = await manager.cancel_job(started.job_id)
            await asyncio.wait_for(runner_cancelled.wait(), timeout=1)
            session_cancelled = await store.query_events(
                aggregate_id=session_id,
                event_type="orchestrator.session.cancelled",
            )
            terminal_events = await store.query_events(
                aggregate_id=execution_id,
                event_type="execution.terminal",
            )

            assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
            assert await is_cancellation_requested(session_id) is True
            assert runner_cancelled.is_set() is True
            assert runner_task.done() is True
            assert not session_cancelled
            assert not terminal_events
        finally:
            release_session_lock(session_id)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_cancel_job_stops_task_when_persisting_linked_cancel_fails(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)
        session_id = "orch_mark_fail_123"
        execution_id = "exec_mark_fail_123"
        await clear_cancellation(session_id)
        runner_cancelled = asyncio.Event()

        try:
            await store.initialize()
            repo = SessionRepository(store)
            create_result = await repo.create_session(
                execution_id=execution_id,
                seed_id="seed_mark_fail_123",
                session_id=session_id,
            )
            assert create_result.is_ok
            lock_path(session_id).write_text("1")

            async def _runner() -> MCPToolResult:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    runner_cancelled.set()
                    raise
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="late"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="mark-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id=session_id, execution_id=execution_id),
            )

            with patch(
                "ouroboros.mcp.job_manager.SessionRepository.mark_cancelled",
                new=AsyncMock(return_value=Result.err(PersistenceError("write failed"))),
            ):
                try:
                    await manager.cancel_job(started.job_id)
                except ValueError as exc:
                    assert "Failed to mark linked session cancelled" in str(exc)
                else:
                    raise AssertionError("cancel_job should fail when session cancel does")

            assert runner_cancelled.is_set() is True
        finally:
            lock_path(session_id).unlink(missing_ok=True)
            await clear_cancellation(session_id)
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_render_job_snapshot_includes_sub_ac_progress(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_sub_ac_progress",
                    data={
                        "execution_id": "exec_job_sub_ac_progress",
                        "completed_count": 0,
                        "total_count": 2,
                        "current_phase": "Deliver",
                        "activity": "Monitoring",
                        "activity_detail": "Level 1/1",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.subtask.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_sub_ac_progress",
                    data={
                        "ac_index": 1,
                        "sub_task_index": 1,
                        "sub_task_id": "ac_1_sub_1",
                        "content": "Child one",
                        "status": "completed",
                    },
                )
            )
            await store.append(
                BaseEvent(
                    type="execution.subtask.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_sub_ac_progress",
                    data={
                        "ac_index": 1,
                        "sub_task_index": 2,
                        "sub_task_id": "ac_1_sub_2",
                        "content": "Child two",
                        "status": "executing",
                    },
                )
            )

            snapshot = JobSnapshot(
                job_id="job_sub_ac_progress",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Deliver | 0/2 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=2,
                links=JobLinks(execution_id="exec_job_sub_ac_progress"),
            )

            text, progress = await _render_job_snapshot_inner(snapshot, store)

            assert "**AC Progress**: 0/2" in text
            assert "**Sub-AC Progress**: 1/2 complete · 1 working" in text
            assert progress["sub_ac_completed"] == 1
            assert progress["sub_ac_total"] == 2
            assert "- `ac_1_sub_2`: executing -- Child two" in text
        finally:
            await store.close()

    async def test_render_job_snapshot_counts_sub_ac_beyond_recent_event_window(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            for index in range(1, 301):
                await store.append(
                    BaseEvent(
                        type="execution.subtask.updated",
                        aggregate_type="execution",
                        aggregate_id="exec_job_many_sub_ac",
                        data={
                            "ac_index": 1,
                            "sub_task_index": index,
                            "sub_task_id": f"ac_1_sub_{index}",
                            "content": f"Child {index}",
                            "status": "completed",
                        },
                    )
                )
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_many_sub_ac",
                    data={
                        "execution_id": "exec_job_many_sub_ac",
                        "completed_count": 0,
                        "total_count": 1,
                        "current_phase": "Deliver",
                        "activity": "Monitoring",
                    },
                )
            )

            snapshot = JobSnapshot(
                job_id="job_many_sub_ac",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Deliver | 0/1 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=301,
                links=JobLinks(execution_id="exec_job_many_sub_ac"),
            )

            text, progress = await _render_job_snapshot_inner(snapshot, store)

            assert "**Sub-AC Progress**: 300/300 complete" in text
            assert progress["sub_ac_completed"] == 300
            assert progress["sub_ac_total"] == 300
        finally:
            await store.close()

    async def test_render_job_snapshot_keeps_workflow_after_subtask_burst(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_old_workflow",
                    data={
                        "execution_id": "exec_job_old_workflow",
                        "completed_count": 1,
                        "total_count": 4,
                        "current_phase": "Implement",
                        "activity": "Monitoring",
                    },
                )
            )
            for index in range(1, 301):
                await store.append(
                    BaseEvent(
                        type="execution.subtask.updated",
                        aggregate_type="execution",
                        aggregate_id="exec_job_old_workflow",
                        data={
                            "ac_index": 1,
                            "sub_task_index": index,
                            "sub_task_id": f"ac_1_sub_{index}",
                            "content": f"Child {index}",
                            "status": "completed",
                        },
                    )
                )

            snapshot = JobSnapshot(
                job_id="job_old_workflow",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Implement | 1/4 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=301,
                links=JobLinks(execution_id="exec_job_old_workflow"),
            )

            text, progress = await _render_job_snapshot_inner(snapshot, store)

            assert "**Phase**: Implement" in text
            assert "**AC Progress**: 1/4" in text
            assert "**Sub-AC Progress**: 300/300 complete" in text
            assert progress["current_phase"] == "Implement"
            assert progress["ac_completed"] == 1
            assert progress["ac_total"] == 4
            assert progress["sub_ac_completed"] == 300
            assert progress["sub_ac_total"] == 300
        finally:
            await store.close()

    async def test_render_job_snapshot_does_not_cache_execution_progress(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        await store.initialize()

        try:
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_live_progress",
                    data={
                        "execution_id": "exec_job_live_progress",
                        "completed_count": 0,
                        "total_count": 2,
                        "current_phase": "Plan",
                        "activity": "Starting",
                    },
                )
            )
            snapshot = JobSnapshot(
                job_id="job_live_progress",
                job_type="execute_seed",
                status=JobStatus.RUNNING,
                message="Plan | 0/2 ACs",
                created_at=datetime(2026, 4, 22, tzinfo=UTC),
                updated_at=datetime(2026, 4, 22, tzinfo=UTC),
                cursor=77,
                links=JobLinks(execution_id="exec_job_live_progress"),
            )

            first_text, first_progress = await _render_job_snapshot(snapshot, store)
            await store.append(
                BaseEvent(
                    type="workflow.progress.updated",
                    aggregate_type="execution",
                    aggregate_id="exec_job_live_progress",
                    data={
                        "execution_id": "exec_job_live_progress",
                        "completed_count": 1,
                        "total_count": 2,
                        "current_phase": "Implement",
                        "activity": "Running",
                    },
                )
            )

            second_text, second_progress = await _render_job_snapshot(snapshot, store)

            assert "**Phase**: Plan" in first_text
            assert first_progress["ac_completed"] == 0
            assert "**Phase**: Implement" in second_text
            assert second_progress["ac_completed"] == 1
        finally:
            await store.close()

    def test_render_compact_job_snapshot_omits_full_sections(self) -> None:
        snapshot = JobSnapshot(
            job_id="job_compact",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Deliver | Sub-AC work | 0/2 ACs",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=77,
            links=JobLinks(execution_id="exec_compact"),
        )

        text = _render_compact_job_snapshot(
            snapshot,
            {
                "ac_completed": 0,
                "ac_total": 2,
                "current_phase": "Deliver",
                "sub_ac_completed": 1,
                "sub_ac_total": 3,
            },
            include_message=False,
        )

        assert text == "job_compact | running | Deliver | AC 0/2 | Sub-AC 1/3 | cursor 77"

    async def test_job_status_omitted_view_preserves_full_snapshot(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_default_full",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=9,
            links=JobLinks(),
        )

        class StaticJobManager:
            async def get_snapshot(self, job_id: str) -> JobSnapshot:
                assert job_id == snapshot.job_id
                return snapshot

        handler = JobStatusHandler(event_store=store, job_manager=StaticJobManager())
        result = await handler.handle({"job_id": "job_default_full"})

        assert result.is_ok
        assert result.value.meta["view"] == "full"
        assert result.value.text_content.startswith("## Job: job_default_full")
        assert "**Status**: running" in result.value.text_content
        assert "job_default_full | running" not in result.value.text_content

    async def test_job_status_full_view_renders_raw_links_without_session_row(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_auto_links",
            job_type="auto",
            status=JobStatus.RUNNING,
            message="Running auto",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=4,
            links=JobLinks(
                session_id="auto_session_links",
                execution_id="exec_links",
                lineage_id="lin_links",
            ),
        )

        class StaticJobManager:
            async def get_snapshot(self, job_id: str) -> JobSnapshot:
                assert job_id == snapshot.job_id
                return snapshot

        handler = JobStatusHandler(event_store=store, job_manager=StaticJobManager())
        await store.initialize()
        try:
            result = await handler.handle({"job_id": "job_auto_links"})
        finally:
            await store.close()

        assert result.is_ok
        assert "### Links" in result.value.text_content
        assert "**Session ID**: auto_session_links" in result.value.text_content
        assert "**Execution ID**: exec_links" in result.value.text_content
        assert "**Lineage ID**: lin_links" in result.value.text_content
        assert result.value.meta["session_id"] == "auto_session_links"
        assert result.value.meta["execution_id"] == "exec_links"
        assert result.value.meta["lineage_id"] == "lin_links"

    async def test_job_wait_omitted_view_preserves_full_unchanged_snapshot(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_wait_full",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=12,
            links=JobLinks(),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 12
                assert timeout_seconds == 0
                return snapshot, False

        handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
        result = await handler.handle(
            {"job_id": "job_wait_full", "cursor": 12, "timeout_seconds": 0}
        )

        assert result.is_ok
        assert result.value.meta["view"] == "full"
        assert result.value.text_content.startswith("## Job: job_wait_full")
        assert "No new job-level events during this wait window." in result.value.text_content
        assert result.value.text_content != "unchanged cursor=12"

    async def test_job_wait_meta_includes_polling_links_for_clients(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_wait_links",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=21,
            links=JobLinks(
                session_id="orch_wait_links",
                execution_id="exec_wait_links",
                lineage_id="lin_wait_links",
            ),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                return snapshot, True

        handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
        await store.initialize()
        try:
            result = await handler.handle(
                {"job_id": "job_wait_links", "cursor": 0, "timeout_seconds": 0}
            )
        finally:
            await store.close()

        assert result.is_ok
        assert result.value.meta["job_id"] == "job_wait_links"
        assert result.value.meta["status"] == "running"
        assert result.value.meta["cursor"] == 21
        assert result.value.meta["session_id"] == "orch_wait_links"
        assert result.value.meta["execution_id"] == "exec_wait_links"
        assert result.value.meta["lineage_id"] == "lin_wait_links"

    async def test_job_wait_summary_view_returns_compact_unchanged_line(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        snapshot = JobSnapshot(
            job_id="job_wait_summary",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=15,
            links=JobLinks(),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                return snapshot, False

        handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
        result = await handler.handle(
            {
                "job_id": "job_wait_summary",
                "cursor": 15,
                "timeout_seconds": 0,
                "view": "summary",
            }
        )

        assert result.is_ok
        assert result.value.meta["view"] == "summary"
        assert result.value.text_content == "unchanged cursor=15"

    async def test_job_wait_compact_view_surfaces_execution_progress_without_job_change(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_wait_live_progress",
                data={
                    "execution_id": "exec_wait_live_progress",
                    "completed_count": 1,
                    "total_count": 3,
                    "current_phase": "Implement",
                    "activity": "Running",
                },
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_live_progress",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Implement | 1/3 ACs",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_live_progress"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_live_progress",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "view": "compact",
                }
            )

            assert result.is_ok
            assert result.value.meta["changed"] is True
            assert result.value.meta["view"] == "compact"
            assert result.value.meta["ac_completed"] == 1
            assert result.value.meta["cursor"] > 0
            assert result.value.text_content == (
                "job_wait_live_progress | running | Implement | AC 1/3 | "
                f"cursor {result.value.meta['cursor']}"
            )

            second_cursor = result.value.meta["cursor"]

            class UnchangedJobManager:
                async def wait_for_change(
                    self,
                    job_id: str,
                    *,
                    cursor: int,
                    timeout_seconds: int,
                ) -> tuple[JobSnapshot, bool]:
                    assert job_id == snapshot.job_id
                    assert cursor == second_cursor
                    assert timeout_seconds == 0
                    return snapshot, False

            unchanged_handler = JobWaitHandler(
                event_store=store,
                job_manager=UnchangedJobManager(),
            )
            unchanged = await unchanged_handler.handle(
                {
                    "job_id": "job_wait_live_progress",
                    "cursor": second_cursor,
                    "timeout_seconds": 0,
                    "view": "compact",
                }
            )

            assert unchanged.is_ok
            assert unchanged.value.meta["changed"] is False
            assert unchanged.value.text_content == f"unchanged cursor={second_cursor}"
        finally:
            await store.close()

    async def test_job_wait_full_view_labels_execution_progress_without_job_change(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_wait_full_progress",
                data={
                    "execution_id": "exec_wait_full_progress",
                    "completed_count": 1,
                    "total_count": 3,
                    "current_phase": "Implement",
                    "activity": "Running",
                },
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_full_progress",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Implement | 1/3 ACs",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_full_progress"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_full_progress",
                    "cursor": 0,
                    "timeout_seconds": 0,
                }
            )

            assert result.is_ok
            assert result.value.meta["changed"] is True
            assert "**AC Progress**: 1/3" in result.value.text_content
            assert "Execution progress updated during this wait window." in (
                result.value.text_content
            )
            assert "No new job-level events during this wait window." not in (
                result.value.text_content
            )
        finally:
            await store.close()

    async def test_job_wait_compact_view_surfaces_subtask_progress_before_workflow(
        self, tmp_path
    ) -> None:
        store = _build_store(tmp_path)
        await store.initialize()
        await store.append(
            BaseEvent(
                type="execution.subtask.updated",
                aggregate_type="execution",
                aggregate_id="exec_wait_subtask_only",
                data={
                    "ac_index": 1,
                    "sub_task_index": 1,
                    "sub_task_id": "ac_1_sub_1",
                    "content": "Child one",
                    "status": "executing",
                },
            )
        )
        snapshot = JobSnapshot(
            job_id="job_wait_subtask_only",
            job_type="execute_seed",
            status=JobStatus.RUNNING,
            message="Running execute_seed",
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            updated_at=datetime(2026, 4, 22, tzinfo=UTC),
            cursor=0,
            links=JobLinks(execution_id="exec_wait_subtask_only"),
        )

        class StaticJobManager:
            async def wait_for_change(
                self,
                job_id: str,
                *,
                cursor: int,
                timeout_seconds: int,
            ) -> tuple[JobSnapshot, bool]:
                assert job_id == snapshot.job_id
                assert cursor == 0
                assert timeout_seconds == 0
                return snapshot, False

        try:
            handler = JobWaitHandler(event_store=store, job_manager=StaticJobManager())
            result = await handler.handle(
                {
                    "job_id": "job_wait_subtask_only",
                    "cursor": 0,
                    "timeout_seconds": 0,
                    "view": "compact",
                }
            )

            assert result.is_ok
            assert result.value.meta["changed"] is True
            assert result.value.meta["view"] == "compact"
            assert result.value.meta["sub_ac_executing"] == 1
            assert result.value.meta["cursor"] > 0
            assert result.value.text_content == (
                "job_wait_subtask_only | running | Sub-AC work | Sub-AC 0/1 | "
                f"cursor {result.value.meta['cursor']}"
            )
        finally:
            await store.close()

    async def test_find_active_job_by_lineage_recovers_in_flight_job(self, tmp_path) -> None:
        """A non-terminal Ralph job is rediscoverable by lineage_id.

        Pins the auto-pipeline RALPH_HANDOFF resume contract: when
        ``ralph_lineage_id`` is persisted but ``ralph_job_id`` is not yet
        saved (gap window between ``start_job`` returning and the auto
        pipeline persisting the job_id), the resume path must re-attach
        to the in-flight job rather than dispatch a duplicate.
        """
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        gate: asyncio.Event = asyncio.Event()

        try:

            async def _slow_runner() -> MCPToolResult:
                await gate.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ralph done"),),
                    is_error=False,
                )

            assert (await manager.find_active_job_by_lineage("lin_recovery")) is None

            started = await manager.start_job(
                job_type="ralph",
                initial_message="queued",
                runner=_slow_runner(),
                links=JobLinks(lineage_id="lin_recovery"),
            )

            recovered = await manager.find_active_job_by_lineage("lin_recovery", job_type="ralph")
            assert recovered is not None
            assert recovered.job_id == started.job_id
            assert recovered.links.lineage_id == "lin_recovery"

            assert (
                await manager.find_active_job_by_lineage("lin_recovery", job_type="evolve")
            ) is None

            gate.set()
            await asyncio.sleep(0.05)

            assert (await manager.find_active_job_by_lineage("lin_recovery")) is None
            terminal_recovered = await manager.find_active_job_by_lineage(
                "lin_recovery", job_type="ralph", include_terminal=True
            )
            assert terminal_recovered is not None
            assert terminal_recovered.job_id == started.job_id
            assert terminal_recovered.status == JobStatus.COMPLETED
        finally:
            gate.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_find_active_job_by_lineage_recovers_persisted_job_after_restart(
        self, tmp_path
    ) -> None:
        """A fresh JobManager can rediscover a persisted non-terminal lineage job."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        gate: asyncio.Event = asyncio.Event()

        try:

            async def _slow_runner() -> MCPToolResult:
                await gate.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ralph done"),),
                    is_error=False,
                )

            started = await manager.start_job(
                job_type="ralph",
                initial_message="queued",
                runner=_slow_runner(),
                links=JobLinks(lineage_id="lin_after_restart"),
            )

            restarted_manager = JobManager(store)
            recovered = await restarted_manager.find_active_job_by_lineage(
                "lin_after_restart", job_type="ralph"
            )

            assert recovered is not None
            assert recovered.job_id == started.job_id
            assert recovered.links.lineage_id == "lin_after_restart"
            assert recovered.status in {JobStatus.QUEUED, JobStatus.RUNNING}
            assert started.job_id in restarted_manager._known_job_ids
        finally:
            gate.set()
            await _cancel_manager_tasks(manager)
            await store.close()

    async def test_find_active_job_by_session_recovers_in_flight_job(self, tmp_path) -> None:
        """A non-terminal auto job is rediscoverable by session_id."""
        store = _build_store(tmp_path)
        manager = JobManager(store)
        await store.initialize()
        gate: asyncio.Event = asyncio.Event()

        try:

            async def _slow_runner() -> MCPToolResult:
                await gate.wait()
                return MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="auto done"),),
                    is_error=False,
                )

            assert (await manager.find_active_job_by_session("auto_recovery")) is None

            started = await manager.start_job(
                job_type="auto",
                initial_message="queued",
                runner=_slow_runner(),
                links=JobLinks(session_id="auto_recovery"),
            )

            recovered = await manager.find_active_job_by_session("auto_recovery", job_type="auto")
            assert recovered is not None
            assert recovered.job_id == started.job_id
            assert recovered.links.session_id == "auto_recovery"

            assert (
                await manager.find_active_job_by_session("auto_recovery", job_type="ralph")
            ) is None

            gate.set()
            await asyncio.sleep(0.05)

            assert (await manager.find_active_job_by_session("auto_recovery")) is None
            terminal_recovered = await manager.find_active_job_by_session(
                "auto_recovery", job_type="auto", include_terminal=True
            )
            assert terminal_recovered is not None
            assert terminal_recovered.job_id == started.job_id
            assert terminal_recovered.status == JobStatus.COMPLETED
        finally:
            gate.set()
            await _cancel_manager_tasks(manager)
            await store.close()
