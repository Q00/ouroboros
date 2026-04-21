"""Tests for async MCP job management."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from ouroboros.core.types import Result
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
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

    async def test_cancel_job_errors_when_linked_session_cancel_persist_fails(
        self, tmp_path
    ) -> None:
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
                job_type="persist-fail-test",
                initial_message="queued",
                runner=_runner(),
                links=JobLinks(session_id="orch_fail_123", execution_id="exec_fail_123"),
            )

            failed = Result.err(PersistenceError("write failed"))
            with patch(
                "ouroboros.mcp.job_manager.SessionRepository.mark_cancelled",
                new=AsyncMock(return_value=failed),
            ):
                try:
                    await manager.cancel_job(started.job_id)
                except ValueError as exc:
                    assert "Failed to mark linked session cancelled" in str(exc)
                else:
                    raise AssertionError("cancel_job should fail when session cancel does")

            terminal_events = await store.query_events(
                aggregate_id="exec_fail_123",
                event_type="execution.terminal",
            )
            assert not terminal_events
        finally:
            await _cancel_manager_tasks(manager)
            await store.close()
