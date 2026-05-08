"""Server-side idempotency contract for ``StartExecuteSeedHandler``.

Regression suite for Q00/ouroboros#774. The handler maintains an
in-memory ``dict[str, ExecutionMeta]`` keyed by ``idempotency_key``. A
second call with the same key short-circuits and returns the original
metadata; different keys produce independent executions; a fresh
handler instance has no memory of prior keys (process-restart bound).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from ouroboros.mcp.job_manager import JobLinks, JobManager, JobSnapshot, JobStatus
from ouroboros.mcp.tools.execution_handlers import StartExecuteSeedHandler
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def event_store():
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    yield store
    await store.close()


def _make_job_manager_stub(start_calls: list[dict]) -> JobManager:
    """Return a JobManager-shaped mock that records ``start_job`` invocations.

    Each call appends ``{"job_type", "links", "runner"}`` to ``start_calls``
    and resolves the runner so the synchronous ExecuteSeedHandler awaitable
    is consumed exactly the way the real JobManager would (preventing
    "coroutine was never awaited" warnings while keeping test isolation).
    """

    job_manager = MagicMock(spec=JobManager)

    counter = {"n": 0}

    async def _start_job(*, job_type, initial_message, runner, links=None):  # noqa: ARG001
        counter["n"] += 1
        job_id = f"job_{counter['n']:012d}"
        start_calls.append(
            {
                "job_type": job_type,
                "links": links,
                "job_id": job_id,
            }
        )
        # Drain the runner coroutine so it doesn't leak as never-awaited.
        try:
            runner.close()
        except AttributeError:
            pass
        now = datetime.now(UTC)
        snapshot = JobSnapshot(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.QUEUED,
            cursor=0,
            message=initial_message,
            links=links or JobLinks(),
            created_at=now,
            updated_at=now,
        )
        return snapshot

    job_manager.start_job = _start_job
    return job_manager


def _make_handler(event_store: EventStore, start_calls: list[dict]) -> StartExecuteSeedHandler:
    """Build a handler whose JobManager is the stub recording start_job calls.

    ``execute_handler`` is mocked because the server-side idempotency
    contract is tested at the boundary of ``handle()``; the inner
    ExecuteSeedHandler is only invoked through the runner coroutine,
    which the JobManager stub closes without awaiting.
    """

    return StartExecuteSeedHandler(
        execute_handler=MagicMock(),
        event_store=event_store,
        job_manager=_make_job_manager_stub(start_calls),
    )


@pytest.mark.asyncio
async def test_same_idempotency_key_replays_metadata_and_skips_enqueue(
    event_store, tmp_path
) -> None:
    """A second call with the same key returns the same meta and does not re-enqueue."""
    start_calls: list[dict] = []
    handler = _make_handler(event_store, start_calls)

    arguments = {
        "seed_content": "goal: idempotent\n",
        "cwd": str(tmp_path),
        "idempotency_key": "auto_session_abc",
    }
    first = await handler.handle(arguments)
    second = await handler.handle(arguments)

    assert first.is_ok
    assert second.is_ok
    first_meta = first.value.meta
    second_meta = second.value.meta
    assert first_meta["execution_id"] == second_meta["execution_id"]
    assert first_meta["session_id"] == second_meta["session_id"]
    assert first_meta["job_id"] == second_meta["job_id"]
    # Exactly one execution was enqueued: the JobManager stub recorded
    # one start_job call, even though handle() ran twice.
    assert len(start_calls) == 1


@pytest.mark.asyncio
async def test_different_idempotency_keys_produce_independent_executions(
    event_store, tmp_path
) -> None:
    """Distinct keys must lead to distinct enqueue events."""
    start_calls: list[dict] = []
    handler = _make_handler(event_store, start_calls)

    first = await handler.handle(
        {
            "seed_content": "goal: a\n",
            "cwd": str(tmp_path),
            "idempotency_key": "auto_session_a",
        }
    )
    second = await handler.handle(
        {
            "seed_content": "goal: b\n",
            "cwd": str(tmp_path),
            "idempotency_key": "auto_session_b",
        }
    )

    assert first.is_ok and second.is_ok
    assert first.value.meta["execution_id"] != second.value.meta["execution_id"]
    assert len(start_calls) == 2


@pytest.mark.asyncio
async def test_process_restart_resets_idempotency_map(event_store, tmp_path) -> None:
    """A fresh handler instance simulates a process restart — keys are forgotten."""
    start_calls_first: list[dict] = []
    handler_first = _make_handler(event_store, start_calls_first)

    arguments = {
        "seed_content": "goal: restart\n",
        "cwd": str(tmp_path),
        "idempotency_key": "auto_session_restart",
    }
    await handler_first.handle(arguments)
    assert len(start_calls_first) == 1

    # Simulate process restart: discard the handler instance and rebuild.
    start_calls_second: list[dict] = []
    handler_second = _make_handler(event_store, start_calls_second)

    await handler_second.handle(arguments)
    # Documented non-goal: persistence across restarts. The new handler
    # has no memory of the prior idempotency key, so the request enqueues
    # a fresh execution.
    assert len(start_calls_second) == 1


@pytest.mark.asyncio
async def test_call_without_idempotency_key_always_enqueues(event_store, tmp_path) -> None:
    """Omitting the key keeps the legacy behavior — every call enqueues."""
    start_calls: list[dict] = []
    handler = _make_handler(event_store, start_calls)

    arguments = {"seed_content": "goal: legacy\n", "cwd": str(tmp_path)}
    await handler.handle(arguments)
    await handler.handle(arguments)

    assert len(start_calls) == 2


@pytest.mark.asyncio
async def test_plugin_dispatch_replays_response_shape_and_skips_dispatch(
    event_store, tmp_path
) -> None:
    """Plugin-dispatch path: same idempotency_key returns identical response_shape
    and emits no second subagent_dispatched event (Q00/ouroboros#787 review-1).
    """
    handler = StartExecuteSeedHandler(
        execute_handler=MagicMock(),
        event_store=event_store,
        job_manager=_make_job_manager_stub([]),
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    arguments = {
        "seed_content": "goal: plugin-idempotent\n",
        "cwd": str(tmp_path),
        "idempotency_key": "auto_plugin_session",
    }

    first = await handler.handle(arguments)
    second = await handler.handle(arguments)

    assert first.is_ok
    assert second.is_ok

    first_meta = first.value.meta
    second_meta = second.value.meta
    # Identical response_shape — same session_id, status, dispatch_mode.
    assert first_meta["session_id"] == second_meta["session_id"]
    assert first_meta["status"] == "delegated_to_plugin"
    assert second_meta["status"] == "delegated_to_plugin"
    assert first_meta["dispatch_mode"] == second_meta["dispatch_mode"] == "plugin"
    assert first_meta["runtime_backend"] == second_meta["runtime_backend"]
    # Identical _subagent envelope (replay re-emits the cached payload).
    assert first_meta["_subagent"] == second_meta["_subagent"]

    # No second subagent.dispatched event was emitted on retry.
    aggregate_id = first_meta["session_id"]
    events, _ = await event_store.get_events_after("subagent", aggregate_id)
    dispatched_events = [e for e in events if e.type == "subagent.dispatched"]
    assert len(dispatched_events) == 1, (
        f"expected exactly 1 subagent.dispatched event on the first call, "
        f"got {len(dispatched_events)} (replay must not re-dispatch)"
    )


@pytest.mark.asyncio
async def test_concurrent_calls_with_same_key_dedupe(event_store, tmp_path) -> None:
    """Two concurrent handle() calls with the same idempotency_key must not
    both enqueue (Q00/ouroboros#787 review-2 BLOCKING-2). Without the
    per-key serialization lock, each in-flight call would miss the cache
    (the entry is written *after* dispatch) and call ``start_job`` again.
    """
    start_calls: list[dict] = []

    # The job manager stub awaits a per-test event so the first handle()
    # is held mid-dispatch while the second handle() arrives. This is the
    # exact race the per-key lock has to defeat.
    release = asyncio.Event()

    job_manager = MagicMock()
    counter = {"n": 0}

    async def _slow_start_job(*, job_type, initial_message, runner, links=None):  # noqa: ARG001
        counter["n"] += 1
        job_id = f"job_{counter['n']:012d}"
        start_calls.append({"job_id": job_id})
        try:
            runner.close()
        except AttributeError:
            pass
        # Hold the first dispatch open so the second handle() races us.
        await release.wait()
        now = datetime.now(UTC)
        return JobSnapshot(
            job_id=job_id,
            job_type=job_type,
            status=JobStatus.QUEUED,
            cursor=0,
            message=initial_message,
            links=links or JobLinks(),
            created_at=now,
            updated_at=now,
        )

    job_manager.start_job = _slow_start_job

    handler = StartExecuteSeedHandler(
        execute_handler=MagicMock(),
        event_store=event_store,
        job_manager=job_manager,
    )

    arguments = {
        "seed_content": "goal: concurrent\n",
        "cwd": str(tmp_path),
        "idempotency_key": "auto_session_concurrent",
    }

    first_task = asyncio.create_task(handler.handle(arguments))
    second_task = asyncio.create_task(handler.handle(arguments))
    # Yield so both tasks are admitted; first acquires the lock, second
    # parks on lock acquisition. Without the lock the second would race
    # past the cache check and trigger a duplicate start_job call.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    release.set()

    first, second = await asyncio.gather(first_task, second_task)

    assert first.is_ok and second.is_ok
    assert len(start_calls) == 1, (
        f"expected exactly 1 start_job call across two concurrent handle() "
        f"calls with the same idempotency_key; got {len(start_calls)}"
    )
    assert first.value.meta["execution_id"] == second.value.meta["execution_id"]
    assert first.value.meta["job_id"] == second.value.meta["job_id"]


@pytest.mark.asyncio
async def test_concurrent_plugin_calls_with_same_key_dedupe(event_store, tmp_path) -> None:
    """Concurrent plugin-dispatch calls with the same key must emit exactly
    one ``subagent.dispatched`` event (Q00/ouroboros#787 review-2 BLOCKING-2).
    """
    handler = StartExecuteSeedHandler(
        execute_handler=MagicMock(),
        event_store=event_store,
        job_manager=_make_job_manager_stub([]),
        agent_runtime_backend="opencode",
        opencode_mode="plugin",
    )

    arguments = {
        "seed_content": "goal: concurrent-plugin\n",
        "cwd": str(tmp_path),
        "idempotency_key": "auto_plugin_concurrent",
    }

    first_task = asyncio.create_task(handler.handle(arguments))
    second_task = asyncio.create_task(handler.handle(arguments))
    first, second = await asyncio.gather(first_task, second_task)

    assert first.is_ok and second.is_ok
    assert first.value.meta["session_id"] == second.value.meta["session_id"]
    assert first.value.meta["_subagent"] == second.value.meta["_subagent"]

    aggregate_id = first.value.meta["session_id"]
    events, _ = await event_store.get_events_after("subagent", aggregate_id)
    dispatched = [e for e in events if e.type == "subagent.dispatched"]
    assert len(dispatched) == 1, (
        f"expected exactly 1 subagent.dispatched event under concurrent "
        f"plugin dispatch with the same key; got {len(dispatched)}"
    )
