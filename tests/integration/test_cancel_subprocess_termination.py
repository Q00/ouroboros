"""Regression tests for background job cancellation terminating subprocesses."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys

import pytest

from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.orchestrator.runner import clear_cancellation, is_cancellation_requested
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore


def _build_store(tmp_path: Path) -> EventStore:
    return EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")


@pytest.mark.asyncio
async def test_cancel_job_terminates_linked_session_subprocess(tmp_path: Path) -> None:
    """Precreated linked sessions stop local subprocesses and persist cancellation."""
    session_id = "orch_cancel_123"
    execution_id = "exec_cancel_123"
    await clear_cancellation(session_id)
    store = _build_store(tmp_path)
    manager = JobManager(store)
    process_started = asyncio.Event()
    process_holder: dict[str, asyncio.subprocess.Process] = {}

    async def _runner() -> MCPToolResult:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            "import time; time.sleep(60)",
        )
        process_holder["process"] = process
        process_started.set()
        try:
            await process.wait()
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except TimeoutError:
                    process.kill()
                    await asyncio.wait_for(process.wait(), timeout=5)
            raise
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="finished"),),
            is_error=False,
        )

    try:
        await store.initialize()
        repo = SessionRepository(store)
        create_result = await repo.create_session(
            execution_id=execution_id,
            seed_id="seed_cancel_123",
            session_id=session_id,
        )
        assert create_result.is_ok

        started = await manager.start_job(
            job_type="linked-session-process",
            initial_message="queued",
            runner=_runner(),
            links=JobLinks(session_id=session_id, execution_id=execution_id),
        )
        await asyncio.wait_for(process_started.wait(), timeout=5)
        process = process_holder["process"]

        await manager.cancel_job(started.job_id)
        await asyncio.wait_for(process.wait(), timeout=5)
        await asyncio.sleep(0.05)
        snapshot = await manager.get_snapshot(started.job_id)
        cancellation_events = await store.query_events(
            aggregate_id=session_id,
            event_type="orchestrator.session.cancelled",
        )
        terminal_events = await store.query_events(
            aggregate_id=execution_id,
            event_type="execution.terminal",
        )

        assert process.returncode is not None
        assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
        assert cancellation_events
        assert cancellation_events[-1].data["cancelled_by"] == "mcp_job_manager"
        assert terminal_events
        assert terminal_events[-1].data["status"] == "cancelled"
        assert await is_cancellation_requested(session_id) is False
    finally:
        process = process_holder.get("process")
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await clear_cancellation(session_id)
        await store.close()
