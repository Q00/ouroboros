"""Invalid argument coverage for async job MCP tools."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import JobStatus
from ouroboros.mcp.tools.job_handlers import (
    JobResultHandler,
    JobStatusHandler,
    JobWaitHandler,
)
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_job_wait_rejects_non_integer_cursor(event_store) -> None:
    handler = JobWaitHandler(event_store=event_store)

    result = await handler.handle({"job_id": "job_missing", "cursor": "abc"})

    assert result.is_err
    assert result.error.tool_name == "ouroboros_job_wait"
    assert result.error.message == "cursor must be a non-negative integer"


@pytest.mark.asyncio
async def test_job_wait_rejects_negative_timeout(event_store) -> None:
    handler = JobWaitHandler(event_store=event_store)

    result = await handler.handle({"job_id": "job_missing", "timeout_seconds": -1})

    assert result.is_err
    assert result.error.tool_name == "ouroboros_job_wait"
    assert result.error.message == "timeout_seconds must be a non-negative integer"


@pytest.mark.asyncio
async def test_detached_auto_status_invalid_handle_returns_stable_failure(event_store) -> None:
    job_id = "job_missing_detached_auto"
    handler = JobStatusHandler(event_store=event_store)

    first = await handler.handle({"job_id": job_id, "view": "full"})
    second = await handler.handle({"job_id": job_id, "view": "summary"})

    assert first.is_err
    assert second.is_err
    assert first.error.tool_name == "ouroboros_job_status"
    assert second.error.tool_name == "ouroboros_job_status"
    assert first.error.message == f"Job not found: {job_id}"
    assert second.error.message == f"Job not found: {job_id}"
    assert first.error.error_code is None
    assert second.error.error_code is None
    assert first.error.details == {}
    assert second.error.details == {}


@pytest.mark.asyncio
async def test_job_result_invalid_handle_returns_structured_error(event_store) -> None:
    job_id = "job_missing_detached_auto"
    handler = JobResultHandler(event_store=event_store)

    result = await handler.handle({"job_id": job_id})

    assert result.is_err
    assert result.error.tool_name == "ouroboros_job_result"
    assert result.error.error_code == "job_handle_not_found"
    assert result.error.message == f"Job handle not found: {job_id}. Result unavailable."
    assert result.error.details == {
        "job_id": job_id,
        "lifecycle_status": "invalid",
        "is_terminal": True,
        "result_available": False,
        "reason": "not_found",
        "source_error": f"Job not found: {job_id}",
    }


@pytest.mark.asyncio
async def test_job_result_returns_expired_terminal_handle_result(event_store) -> None:
    job_id = "job_expired_result"
    expired_at = datetime.now(UTC) - timedelta(hours=2)
    await event_store.append(
        BaseEvent(
            id="evt_expired_result_created",
            type="mcp.job.created",
            timestamp=expired_at,
            aggregate_type="job",
            aggregate_id=job_id,
            data={
                "job_type": "auto",
                "status": JobStatus.QUEUED.value,
                "message": "Queued detached auto",
            },
        )
    )
    await event_store.append(
        BaseEvent(
            id="evt_expired_result_completed",
            type="mcp.job.completed",
            timestamp=expired_at + timedelta(seconds=1),
            aggregate_type="job",
            aggregate_id=job_id,
            data={
                "status": JobStatus.COMPLETED.value,
                "message": "Job complete",
                "result_text": "stale result",
            },
        )
    )
    handler = JobResultHandler(event_store=event_store)

    result = await handler.handle({"job_id": job_id})

    assert result.is_ok
    assert result.value.content[0].text == "stale result"
    assert result.value.meta["job_id"] == job_id
    assert result.value.meta["lifecycle_status"] == "completed"
    assert result.value.meta["is_terminal"] is True
    assert result.value.meta["result_available"] is True
