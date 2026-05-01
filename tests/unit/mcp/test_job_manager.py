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
from ouroboros.orchestrator.heartbeat import acquire as acquire_session_lock
from ouroboros.orchestrator.heartbeat import lock_path
from ouroboros.orchestrator.heartbeat import release as release_session_lock
from ouroboros.orchestrator.runner import clear_cancellation, is_cancellation_requested
from ouroboros.orchestrator.session import SessionRepository
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

            await asyncio.sleep(0.15)
            snapshot = await manager.get_snapshot(started.job_id)

            assert snapshot.status == JobStatus.COMPLETED
            assert snapshot.result_text == "done"
            assert snapshot.result_meta["kind"] == "test"
        finally:
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
