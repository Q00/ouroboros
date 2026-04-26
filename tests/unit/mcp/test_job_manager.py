"""Tests for async MCP job management."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
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
