"""Follow acceptance-timeout receipts without inventing durable job state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from ouroboros.mcp import detached_jobs
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools import background
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolResult
from ouroboros.persistence.event_store import EventStore, sqlite_database_url

_READ_HANDLERS = (JobStatusHandler, JobWaitHandler, JobResultHandler)


@dataclass
class _WorkerProcess:
    pid: int = 424242
    returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode


@dataclass
class _PendingLaunch:
    store: EventStore
    manager: JobManager
    receipt: dict[str, Any]
    process: _WorkerProcess
    spawn: MagicMock
    work: AsyncMock
    managers: list[JobManager]

    @property
    def job_id(self) -> str:
        return str(self.receipt["job_id"])


def _start_arguments(manager: JobManager, store: EventStore, work: Any) -> dict[str, Any]:
    return {
        "job_manager": manager,
        "event_store": store,
        "job_type": "auto",
        "intent": "auto",
        "process_scope": "pending-receipt-test",
        "initial_message": "Queued",
        "links": JobLinks(),
        "work_fn": work,
        "cancelled_text": "Cancelled",
        "detached_tool_name": "ouroboros_start_auto",
        "detached_arguments": {"goal": "Synthetic acceptance receipt test"},
    }


@pytest_asyncio.fixture
async def pending_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_PendingLaunch]:
    """Exercise real allocation/launch/timeout; only process creation is faked."""
    task_home = tmp_path / "home"
    task_home.mkdir()
    monkeypatch.setenv("HOME", str(task_home))
    monkeypatch.setenv("USERPROFILE", str(task_home))
    store = EventStore(sqlite_database_url(tmp_path / "events.db"))
    manager = JobManager(store, durable_jobs=True)
    managers = [manager]
    process = _WorkerProcess()
    spawn = MagicMock(return_value=process)
    monkeypatch.setattr(detached_jobs, "_spawn_worker", spawn)

    async def short_launch(**kwargs: Any) -> Any:
        return await detached_jobs.launch_detached_job(**kwargs, startup_timeout_seconds=0.1)

    # Shorten the real boundary's deadline instead of fabricating its exception.
    monkeypatch.setattr(background, "launch_detached_job", short_launch)
    work = AsyncMock()
    try:
        with pytest.raises(MCPToolError) as raised:
            await background.start_background_tool_job(**_start_arguments(manager, store, work))
        error = raised.value
        assert error.error_code == "detached_job_acceptance_pending"
        assert error.details["status"] == "acceptance_pending"
        assert error.details["retry_start_allowed"] is False
        spawn.assert_called_once()
        work.assert_not_called()
        assert await store.count_events() == 0
        yield _PendingLaunch(store, manager, error.details, process, spawn, work, managers)
    finally:
        for item in reversed(managers):
            await item.drain(grace_seconds=0.1)
        await store.close()


def _assert_pending(result: Any, job_id: str) -> None:
    assert result.is_ok, result.error if result.is_err else None
    assert result.value.is_error is False
    meta = result.value.meta
    assert meta["job_id"] == job_id
    assert meta["status"] == "acceptance_pending"
    assert meta["lifecycle_status"] == "acceptance_pending"
    assert meta["status_category"] == "non_terminal"
    assert meta["is_terminal"] is False
    assert meta["result_available"] is False
    assert meta["acceptance_confirmed"] is False
    assert meta["worker_state"] == "unknown"
    assert meta["retry_start_allowed"] is False
    assert meta.get("worker_continues") is not True
    assert meta["status_check"] == {
        "tool": "ouroboros_job_status",
        "arguments": {"job_id": job_id},
    }


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
async def test_timeout_receipt_can_be_followed_without_creating_a_job(
    pending_launch: _PendingLaunch, handler_type: Any
) -> None:
    launch = pending_launch
    handler = handler_type(event_store=launch.store, job_manager=launch.manager)
    arguments = launch.receipt["status_check"]["arguments"]

    result = await handler.handle(arguments)

    _assert_pending(result, launch.job_id)
    with pytest.raises(ValueError, match="Job not found"):
        await launch.manager.get_snapshot(launch.job_id)
    assert await launch.store.count_events() == 0
    assert launch.manager.get_cached_snapshot(launch.job_id) is None
    assert launch.job_id not in launch.manager._reserved_job_ids
    launch.spawn.assert_called_once()
    launch.work.assert_not_called()


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
async def test_pending_read_does_not_reassert_worker_liveness(
    pending_launch: _PendingLaunch, handler_type: Any
) -> None:
    launch = pending_launch
    # The worker can exit after the receipt. Read APIs hold no fresh process proof.
    launch.process.returncode = 1
    result = await handler_type(event_store=launch.store, job_manager=launch.manager).handle(
        {"job_id": launch.job_id}
    )

    _assert_pending(result, launch.job_id)
    assert await launch.store.count_events() == 0


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
async def test_pending_receipt_is_scoped_to_the_accepting_manager(
    pending_launch: _PendingLaunch, handler_type: Any
) -> None:
    launch = pending_launch
    fresh_manager = JobManager(launch.store, durable_jobs=True)
    launch.managers.append(fresh_manager)
    result = await handler_type(event_store=launch.store, job_manager=fresh_manager).handle(
        {"job_id": launch.job_id}
    )

    assert result.is_err
    assert "not found" in result.error.message.lower()
    assert await launch.store.count_events() == 0


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
@pytest.mark.parametrize("kind", ("missing", "empty", "unknown", "reserved"))
async def test_unissued_or_invalid_handles_do_not_become_pending(
    pending_launch: _PendingLaunch, handler_type: Any, kind: str
) -> None:
    launch = pending_launch
    if kind == "reserved":
        arguments = {"job_id": await launch.manager.allocate_job_id()}
    elif kind == "unknown":
        arguments = {"job_id": "job_never_issued"}
    elif kind == "empty":
        arguments = {"job_id": ""}
    else:
        arguments = {}

    result = await handler_type(event_store=launch.store, job_manager=launch.manager).handle(
        arguments
    )

    assert result.is_err
    assert "required" in result.error.message or "not found" in result.error.message.lower()
    assert await launch.store.count_events() == 0


@pytest.mark.parametrize("argument", ("cursor", "timeout_seconds"))
@pytest.mark.parametrize("value", (-1, "invalid"))
async def test_pending_wait_preserves_argument_validation(
    pending_launch: _PendingLaunch, argument: str, value: Any
) -> None:
    launch = pending_launch
    result = await JobWaitHandler(event_store=launch.store, job_manager=launch.manager).handle(
        {"job_id": launch.job_id, argument: value}
    )

    assert result.is_err
    assert argument in result.error.message
    assert await launch.store.count_events() == 0


async def test_pending_receipt_is_immutable_and_can_be_discarded(
    pending_launch: _PendingLaunch,
) -> None:
    launch = pending_launch
    receipt = launch.manager.pending_receipts.get(launch.job_id)
    assert receipt is not None
    assert receipt.job_id == launch.job_id
    assert receipt.worker_pid == launch.process.pid
    with pytest.raises(FrozenInstanceError):
        receipt.worker_pid = 7  # type: ignore[misc]

    launch.manager.pending_receipts.discard(launch.job_id)

    assert launch.manager.pending_receipts.get(launch.job_id) is None
    result = await JobStatusHandler(event_store=launch.store, job_manager=launch.manager).handle(
        {"job_id": launch.job_id}
    )
    assert result.is_err
    assert await launch.store.count_events() == 0


async def test_definitive_worker_exit_does_not_create_a_pending_receipt(
    pending_launch: _PendingLaunch,
) -> None:
    launch = pending_launch
    launch.process.returncode = 1

    with pytest.raises(RuntimeError, match="exited before persisting job acceptance"):
        await background.start_background_tool_job(
            **_start_arguments(launch.manager, launch.store, launch.work)
        )

    request_path = launch.spawn.call_args.args[0]
    rejected_id = request_path.stem
    assert rejected_id != launch.job_id
    assert launch.manager.pending_receipts.get(rejected_id) is None
    assert rejected_id not in launch.manager._reserved_job_ids
    for handler_type in _READ_HANDLERS:
        result = await handler_type(event_store=launch.store, job_manager=launch.manager).handle(
            {"job_id": rejected_id}
        )
        assert result.is_err
    assert await launch.store.count_events() == 0
    launch.work.assert_not_called()


async def _wait_status(manager: JobManager, job_id: str, expected: JobStatus) -> None:
    async with asyncio.timeout(5):
        while (await manager.get_snapshot(job_id)).status is not expected:
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
async def test_late_worker_acceptance_supersedes_pending_receipt_without_duplicate_job(
    pending_launch: _PendingLaunch, handler_type: Any
) -> None:
    launch = pending_launch
    worker = JobManager(launch.store, durable_jobs=True, forced_inline_job_id=launch.job_id)
    launch.managers.append(worker)
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_work(_handle: Any) -> MCPToolResult:
        started.set()
        await release.wait()
        return MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text="Finished once"),),
            meta={"pending_receipt_test": "complete"},
        )

    try:
        snapshot = await background.start_background_tool_job(
            **_start_arguments(worker, launch.store, delayed_work)
        )
        assert snapshot.job_id == launch.job_id
        await asyncio.wait_for(started.wait(), timeout=5)
        await _wait_status(worker, launch.job_id, JobStatus.RUNNING)

        handler = handler_type(event_store=launch.store, job_manager=launch.manager)
        running = await handler.handle({"job_id": launch.job_id})

        assert running.is_ok
        assert running.value.meta["status"] == "running"
        assert running.value.meta["is_terminal"] is False
        assert launch.manager.pending_receipts.get(launch.job_id) is None
        assert len(await launch.store.query_events(event_type="mcp.job.created")) == 1
        launch.spawn.assert_called_once()
        launch.work.assert_not_called()

        # Even a stale receipt must never hide authoritative terminal events.
        launch.manager.pending_receipts.remember(
            job_id=launch.job_id, worker_pid=launch.process.pid, timeout_seconds=0.1
        )
        release.set()
        await _wait_status(worker, launch.job_id, JobStatus.COMPLETED)
        completed = await handler.handle({"job_id": launch.job_id})

        assert completed.is_ok
        assert completed.value.meta["status"] == "completed"
        assert completed.value.meta["is_terminal"] is True
        assert launch.manager.pending_receipts.get(launch.job_id) is None
        assert len(await launch.store.query_events(event_type="mcp.job.created")) == 1
        if handler_type is JobResultHandler:
            assert completed.value.meta["pending_receipt_test"] == "complete"
            assert completed.value.content[0].text == "Finished once"
    finally:
        release.set()
