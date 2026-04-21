"""Regression tests for background job cancellation terminating subprocesses."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore


def _build_store(tmp_path: Path) -> EventStore:
    return EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")


@pytest.mark.asyncio
async def test_cancel_job_terminates_linked_session_subprocess(tmp_path: Path) -> None:
    """Cancelling a linked session job must cancel its runner and child process."""
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
        started = await manager.start_job(
            job_type="linked-session-process",
            initial_message="queued",
            runner=_runner(),
            links=JobLinks(session_id="orch_cancel_123", execution_id="exec_cancel_123"),
        )
        await asyncio.wait_for(process_started.wait(), timeout=5)
        process = process_holder["process"]

        await manager.cancel_job(started.job_id)
        await asyncio.wait_for(process.wait(), timeout=5)
        snapshot = await manager.get_snapshot(started.job_id)

        assert process.returncode is not None
        assert snapshot.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
    finally:
        process = process_holder.get("process")
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await store.close()
