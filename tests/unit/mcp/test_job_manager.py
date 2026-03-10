"""Tests for async MCP job management."""

from __future__ import annotations

import asyncio

from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


def _build_store(tmp_path) -> EventStore:
    db_path = tmp_path / "jobs.db"
    return EventStore(f"sqlite+aiosqlite:///{db_path}")


class TestJobManager:
    """Test background job lifecycle behavior."""

    async def test_start_job_completes_and_persists_result(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

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

    async def test_wait_for_change_returns_new_cursor(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

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

    async def test_cancel_job_cancels_non_session_task(self, tmp_path) -> None:
        store = _build_store(tmp_path)
        manager = JobManager(store)

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
