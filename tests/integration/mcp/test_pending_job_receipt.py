"""Follow acceptance-timeout receipts without inventing durable job state."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError, dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.mcp import detached_jobs
from ouroboros.mcp.errors import MCPToolError
from ouroboros.mcp.job_manager import JobLinks, JobManager, JobStatus
from ouroboros.mcp.tools import background
from ouroboros.mcp.tools.evaluation_handlers import StartEvaluateHandler
from ouroboros.mcp.tools.job_handlers import JobResultHandler, JobStatusHandler, JobWaitHandler
from ouroboros.mcp.tools.seed_handoff import SeedHandoffRegistry
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
        assert error.details["pending_receipt_persisted"] is True
        assert await store.count_events() == 1
        assert await store.query_events(aggregate_type="job") == []
        observations = await store.query_events(aggregate_type="job_receipt")
        assert observations[0].event_version == 1
        assert observations[0].data == {
            "worker_pid": process.pid,
            "startup_timeout_seconds": 0.1,
        }
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
    assert await launch.store.count_events() == 1
    assert await launch.store.query_events(aggregate_type="job") == []
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
    assert await launch.store.count_events() == 1


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
async def test_pending_receipt_is_discoverable_by_a_new_manager(
    pending_launch: _PendingLaunch, handler_type: Any
) -> None:
    launch = pending_launch
    fresh_manager = JobManager(launch.store, durable_jobs=True)
    launch.managers.append(fresh_manager)
    result = await handler_type(event_store=launch.store, job_manager=fresh_manager).handle(
        {"job_id": launch.job_id}
    )

    _assert_pending(result, launch.job_id)
    assert result.value.meta["pending_receipt_persisted"] is True
    assert await launch.store.count_events() == 1


async def test_pending_receipt_survives_store_close_and_a_new_process(
    pending_launch: _PendingLaunch,
) -> None:
    launch = pending_launch
    await launch.manager.drain(grace_seconds=0.1)
    await launch.store.close()
    script = """
import asyncio, json, sys
from pathlib import Path
import ouroboros
from ouroboros.mcp.job_manager import JobManager
from ouroboros.mcp.tools.job_handlers import JobStatusHandler, JobWaitHandler, JobResultHandler
from ouroboros.persistence.event_store import EventStore

async def read():
    store = EventStore(sys.argv[1])
    manager = JobManager(store, durable_jobs=True)
    try:
        states = []
        for handler in (JobStatusHandler, JobWaitHandler, JobResultHandler):
            result = await handler(event_store=store, job_manager=manager).handle(
                {"job_id": sys.argv[2]}
            )
            assert result.is_ok, result.error
            states.append(result.value.meta)
        print(json.dumps({"module": str(Path(ouroboros.__file__).resolve()), "states": states}))
    finally:
        await store.close()
asyncio.run(read())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        launch.store.database_url,
        launch.job_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    assert process.returncode == 0, stderr.decode()
    payload = json.loads(stdout.decode().splitlines()[-1])
    import ouroboros

    assert payload["module"] == str(Path(ouroboros.__file__).resolve())
    for state in payload["states"]:
        assert state["status"] == "acceptance_pending"
        assert state["pending_receipt_persisted"] is True
        assert state["acceptance_confirmed"] is False
        assert state["retry_start_allowed"] is False


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
async def test_other_database_does_not_inherit_pending_receipt(
    pending_launch: _PendingLaunch, tmp_path: Path, handler_type: Any
) -> None:
    store = EventStore(sqlite_database_url(tmp_path / "other.db"))
    manager = JobManager(store, durable_jobs=True)
    try:
        result = await handler_type(event_store=store, job_manager=manager).handle(
            {"job_id": pending_launch.job_id}
        )
        assert result.is_err
        assert "not found" in result.error.message.lower()
    finally:
        await store.close()


@pytest.mark.parametrize(
    "arguments", ({"wait_for": "raw"}, {"wait_for": "ac_change"}, {"stream": "linked"})
)
async def test_pending_receipt_read_uses_remaining_positive_wait_budget(
    pending_launch: _PendingLaunch, monkeypatch: pytest.MonkeyPatch, arguments: dict[str, str]
) -> None:
    from ouroboros.mcp.tools import job_handlers

    launch = pending_launch
    fresh = JobManager(launch.store, durable_jobs=True)
    original_read = launch.store.get_events_after
    entered = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()
    first_job_read = True

    async def slow_read(aggregate_type: str, *args: Any, **kwargs: Any) -> Any:
        nonlocal first_job_read
        if aggregate_type == "job" and first_job_read:
            first_job_read = False
            await asyncio.sleep(1.4)
        if aggregate_type == "job_receipt":
            entered.set()
            try:
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await release.wait()
                return await original_read(aggregate_type, *args, **kwargs)
            finally:
                settled.set()
        return await original_read(aggregate_type, *args, **kwargs)

    monkeypatch.setattr(launch.store, "get_events_after", slow_read)
    monkeypatch.setattr(job_handlers, "_JOB_WAIT_RESPONSE_GRACE_SECONDS", 0.1)
    try:
        result = await asyncio.wait_for(
            JobWaitHandler(event_store=launch.store, job_manager=fresh).handle(
                {"job_id": launch.job_id, "timeout_seconds": 2, **arguments}
            ),
            timeout=3.0,
        )
        assert entered.is_set()
        assert result.is_ok
        assert result.value.meta["wait_timed_out"] is True
        assert result.value.meta["result_available"] is False
        assert not release.is_set()
        await asyncio.wait_for(cancelled.wait(), timeout=1)
    finally:
        release.set()
        if entered.is_set():
            await asyncio.wait_for(settled.wait(), timeout=2)


async def test_zero_time_pending_receipt_read_is_not_guarded(
    pending_launch: _PendingLaunch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.mcp.tools import job_wait_guard

    launch = pending_launch
    fresh = JobManager(launch.store, durable_jobs=True)
    original_read = launch.store.get_events_after
    reads: list[str] = []

    async def read(aggregate_type: str, *args: Any, **kwargs: Any) -> Any:
        reads.append(aggregate_type)
        await asyncio.sleep(0)
        return await original_read(aggregate_type, *args, **kwargs)

    guard = AsyncMock(side_effect=AssertionError("zero-time reads must not use a response guard"))
    monkeypatch.setattr(launch.store, "get_events_after", read)
    monkeypatch.setattr(job_wait_guard, "await_job_wait_branch", guard)
    result = await JobWaitHandler(event_store=launch.store, job_manager=fresh).handle(
        {"job_id": launch.job_id, "timeout_seconds": 0}
    )
    _assert_pending(result, launch.job_id)
    assert "job_receipt" in reads
    guard.assert_not_called()


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
async def test_durable_job_corruption_is_not_hidden_by_pending_receipt(
    pending_launch: _PendingLaunch, handler_type: Any
) -> None:
    launch = pending_launch
    await launch.store.append(
        BaseEvent(
            type="mcp.job.created",
            aggregate_type="job",
            aggregate_id=launch.job_id,
            data={"status": "malformed_state"},
        )
    )
    fresh = JobManager(launch.store, durable_jobs=True)
    result = await handler_type(event_store=launch.store, job_manager=fresh).handle(
        {"job_id": launch.job_id}
    )
    assert result.is_err
    assert "malformed_state" in (
        result.error.message + str(result.error.details.get("source_error", ""))
    )
    assert "acceptance_pending" not in result.error.message


@pytest.mark.parametrize(
    ("version", "data"),
    [
        (2, {"worker_pid": 123, "startup_timeout_seconds": 20}),
        (1, {"worker_pid": True, "startup_timeout_seconds": 20}),
        (1, {"worker_pid": 123, "startup_timeout_seconds": "20"}),
        (1, {"worker_pid": 123, "startup_timeout_seconds": -1}),
    ],
)
async def test_invalid_durable_receipt_is_an_error(
    pending_launch: _PendingLaunch, version: int, data: dict[str, Any]
) -> None:
    launch = pending_launch
    job_id = "job_invalid_receipt"
    await launch.store.append(
        BaseEvent(
            type="mcp.job.acceptance_pending_observed",
            aggregate_type="job_receipt",
            aggregate_id=job_id,
            event_version=version,
            data=data,
        )
    )
    with pytest.raises(PersistenceError, match="Invalid or unsupported"):
        await JobStatusHandler(event_store=launch.store, job_manager=launch.manager).handle(
            {"job_id": job_id}
        )


@pytest.mark.parametrize("failure", (PersistenceError("unwritable"), TimeoutError("locked")))
async def test_receipt_write_failure_preserves_pending_and_consumed_handoff(
    pending_launch: _PendingLaunch, monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    launch = pending_launch
    monkeypatch.setattr(launch.store, "append_durable", AsyncMock(side_effect=failure))
    registry = SeedHandoffRegistry()
    handoff_id = registry.register(session_id="receipt_write_failure", seed_content="goal: once\n")
    handler = StartEvaluateHandler(
        evaluate_handler=MagicMock(),
        event_store=launch.store,
        job_manager=launch.manager,
        seed_handoff_registry=registry,
    )
    arguments = {
        "session_id": "receipt_write_failure",
        "artifact": "synthetic artifact",
        "seed_handoff_id": handoff_id,
        "auto_evolve": True,
    }
    with pytest.raises(MCPToolError) as raised:
        await handler.handle(arguments)
    receipt = raised.value.details
    assert raised.value.error_code == "detached_job_acceptance_pending"
    assert receipt["pending_receipt_persisted"] is False
    assert receipt["retry_start_allowed"] is False
    assert registry.resolve(handoff_id, session_id="receipt_write_failure") is None
    assert receipt["job_id"] not in launch.manager._reserved_job_ids
    local = await JobStatusHandler(event_store=launch.store, job_manager=launch.manager).handle(
        {"job_id": receipt["job_id"]}
    )
    _assert_pending(local, receipt["job_id"])
    assert local.value.meta["pending_receipt_persisted"] is False
    fresh = JobManager(launch.store, durable_jobs=True)
    unknown = await JobStatusHandler(event_store=launch.store, job_manager=fresh).handle(
        {"job_id": receipt["job_id"]}
    )
    assert unknown.is_err
    retry = await handler.handle(arguments)
    assert retry.is_err
    assert "unknown or does not belong" in retry.error.message
    assert launch.spawn.call_count == 2  # original fixture start plus one evaluation
    assert await launch.store.query_events(aggregate_type="job") == []


async def test_cancellation_settles_receipt_write_without_restoring_handoff(
    pending_launch: _PendingLaunch, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ouroboros.persistence import event_store as store_module

    launch = pending_launch
    inserted = asyncio.Event()
    release = asyncio.Event()
    original_insert = store_module._insert_event

    async def insert_then_pause(*args: Any, **kwargs: Any) -> Any:
        result = await original_insert(*args, **kwargs)
        inserted.set()
        await release.wait()
        return result

    monkeypatch.setattr(store_module, "_insert_event", insert_then_pause)
    registry = SeedHandoffRegistry()
    handoff_id = registry.register(session_id="receipt_cancel", seed_content="goal: once\n")
    handler = StartEvaluateHandler(
        evaluate_handler=MagicMock(),
        event_store=launch.store,
        job_manager=launch.manager,
        seed_handoff_registry=registry,
    )
    arguments = {
        "session_id": "receipt_cancel",
        "artifact": "synthetic artifact",
        "seed_handoff_id": handoff_id,
        "auto_evolve": True,
    }
    dispatch = asyncio.create_task(handler.handle(arguments))
    try:
        await asyncio.wait_for(inserted.wait(), timeout=5)
        dispatch.cancel()
        await asyncio.sleep(0)
        assert not dispatch.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await dispatch
    finally:
        release.set()
        if not dispatch.done():
            dispatch.cancel()
            await asyncio.gather(dispatch, return_exceptions=True)
    assert registry.resolve(handoff_id, session_id="receipt_cancel") is None
    assert launch.manager._reserved_job_ids == set()
    observations = await launch.store.query_events(aggregate_type="job_receipt")
    assert len(observations) == 2
    cancelled_id = next(
        event.aggregate_id for event in observations if event.aggregate_id != launch.job_id
    )
    local = await JobStatusHandler(event_store=launch.store, job_manager=launch.manager).handle(
        {"job_id": cancelled_id}
    )
    _assert_pending(local, cancelled_id)
    assert local.value.meta["pending_receipt_persisted"] is True
    fresh = JobManager(launch.store, durable_jobs=True)
    result = await JobStatusHandler(event_store=launch.store, job_manager=fresh).handle(
        {"job_id": cancelled_id}
    )
    _assert_pending(result, cancelled_id)
    assert result.value.meta["pending_receipt_persisted"] is True
    retry = await handler.handle(arguments)
    assert retry.is_err
    assert launch.spawn.call_count == 2
    assert await launch.store.query_events(aggregate_type="job") == []


async def test_receipt_persistence_survives_concurrent_cache_discard(
    pending_launch: _PendingLaunch,
) -> None:
    launch = pending_launch

    async def accepted_during_failure_hook(error: BaseException) -> None:
        assert isinstance(error, detached_jobs.DetachedJobAcceptanceTimeout)
        await launch.store.append(
            BaseEvent(
                type="mcp.job.created",
                aggregate_type="job",
                aggregate_id=error.job_id,
                data={"status": "completed", "job_type": "auto"},
            )
        )
        assert (await launch.manager.get_snapshot(error.job_id)).is_terminal
        assert launch.manager.pending_receipts.get(error.job_id) is None

    with pytest.raises(MCPToolError) as raised:
        await background.start_background_tool_job(
            **_start_arguments(launch.manager, launch.store, launch.work),
            on_enqueue_failure=accepted_during_failure_hook,
        )
    receipt = raised.value.details
    assert receipt["pending_receipt_persisted"] is True
    fresh = JobManager(launch.store, durable_jobs=True)
    result = await JobStatusHandler(event_store=launch.store, job_manager=fresh).handle(
        {"job_id": receipt["job_id"]}
    )
    assert result.is_ok
    assert result.value.meta["status"] == "completed"


@pytest.mark.parametrize("handler_type", _READ_HANDLERS)
@pytest.mark.parametrize("terminal", ("completed", "failed", "cancelled"))
async def test_real_terminal_events_supersede_persisted_pending_receipt(
    pending_launch: _PendingLaunch, handler_type: Any, terminal: str
) -> None:
    launch = pending_launch
    await launch.store.append_batch(
        [
            BaseEvent(
                type="mcp.job.created",
                aggregate_type="job",
                aggregate_id=launch.job_id,
                data={"status": "queued", "job_type": "auto"},
            ),
            BaseEvent(
                type=f"mcp.job.{terminal}",
                aggregate_type="job",
                aggregate_id=launch.job_id,
                data={"status": terminal, "result_text": terminal},
            ),
        ]
    )
    fresh = JobManager(launch.store, durable_jobs=True)
    result = await handler_type(event_store=launch.store, job_manager=fresh).handle(
        {"job_id": launch.job_id}
    )
    assert result.is_ok
    assert result.value.meta["status"] == terminal
    assert result.value.meta["is_terminal"] is True


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
    assert await launch.store.count_events() == 1


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
    assert await launch.store.count_events() == 1


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
    _assert_pending(result, launch.job_id)
    assert result.value.meta["pending_receipt_persisted"] is True
    assert await launch.store.count_events() == 1


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
    assert await launch.store.count_events() == 1
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
