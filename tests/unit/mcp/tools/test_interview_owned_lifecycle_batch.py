"""Owned batch stores settle work and support reuse without closing shared stores."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ouroboros.bigbang.interview import InterviewEngine
from ouroboros.core.types import Result
from ouroboros.mcp.tools.authoring_handlers import InterviewHandler
from ouroboros.persistence.event_store import EventStore
from tests.unit.mcp.tools.test_interview_batch_prototype import _proposal


def make_handler(tmp_path, *, shared=False, model=None):
    adapter = model or SimpleNamespace(
        complete=AsyncMock(return_value=Result.ok(SimpleNamespace(content=_proposal())))
    )
    engine = InterviewEngine(llm_adapter=adapter, state_dir=tmp_path, model="synthetic")
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite'}")
    handler = InterviewHandler(
        interview_engine=engine,
        llm_adapter=adapter,
        data_dir=tmp_path,
        event_store=store if shared else None,
    )
    if not shared:
        handler._event_store = store
    handler._create_interview_engine = lambda: (engine, adapter)
    return handler, store, engine


@pytest.mark.asyncio
async def test_owned_delayed_event_drains_and_same_handler_reuse_records_answers(tmp_path):
    handler, store, engine = make_handler(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    append = store.append

    async def delayed(event):
        if event.type == "interview.started":
            started.set()
            await release.wait()
        return await append(event)

    store.append = delayed
    task = asyncio.create_task(
        handler.handle(
            {
                "initial_context": "Build a useful tool",
                "structured_first_batch": True,
                "cwd": str(tmp_path),
            }
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    assert not task.done()
    release.set()
    start = await asyncio.wait_for(task, 5)
    assert start.is_ok and not handler._bg_tasks and store._engine is None
    sid, digest = start.value.meta["session_id"], start.value.meta["batch_digest"]
    qid = start.value.meta["questions"][0]["question_id"]
    assert (await handler.handle({"session_id": sid})).is_ok
    assert (
        await handler.handle(
            {
                "session_id": sid,
                "batch_confirm_digest": digest,
                "batch_independence_confirmed": True,
                "batch_answers": [{"question_id": qid, "answer": "gardeners"}],
            }
        )
    ).is_ok
    assert store._engine is None and not handler._bg_tasks
    reader = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite'}")
    await reader.initialize()
    events = await reader.replay("interview", sid)
    assert sum(event.type == "interview.started" for event in events) == 1
    recorded = [event for event in events if event.type == "interview.response.recorded"]
    assert len(recorded) == 1 and recorded[0].data["question_id"] == qid
    await reader.close()
    assert (await engine.load_state(sid)).value.rounds[0].user_response == "gardeners"


@pytest.mark.asyncio
async def test_shared_store_stays_open_and_owned_elsewhere(tmp_path):
    handler, store, _ = make_handler(tmp_path, shared=True)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_ok
    await handler._drain_bg_tasks()
    assert not handler._closed and store._engine is not None
    events = await store.replay("interview", result.value.meta["session_id"])
    assert any(event.type == "interview.started" for event in events)
    await store.close()


@pytest.mark.asyncio
async def test_owned_error_path_closes_store(tmp_path):
    adapter = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError("synthetic failure")))
    handler, store, _ = make_handler(tmp_path, model=adapter)
    result = await handler.handle(
        {
            "initial_context": "Build a useful tool",
            "structured_first_batch": True,
            "cwd": str(tmp_path),
        }
    )
    assert result.is_ok and result.value.is_error
    assert handler._closed and store._engine is None and not handler._bg_tasks


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_delayed_owned_event(tmp_path):
    handler, store, _ = make_handler(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    append = store.append

    async def delayed(event):
        started.set()
        await release.wait()
        return await append(event)

    store.append = delayed
    task = asyncio.create_task(
        handler.handle(
            {
                "initial_context": "Build a useful tool",
                "structured_first_batch": True,
                "cwd": str(tmp_path),
            }
        )
    )
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert handler._closed and store._engine is None and not handler._bg_tasks
    reader = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite'}")
    await reader.initialize()
    events = await reader.get_recent_events(limit=20)
    assert any(event.type == "interview.started" for event in events)
    await reader.close()


@pytest.mark.asyncio
async def test_concurrent_owned_calls_close_only_after_last_call(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def complete(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        return Result.ok(SimpleNamespace(content=_proposal()))

    handler, store, _ = make_handler(tmp_path, model=SimpleNamespace(complete=complete))
    args = {
        "initial_context": "Build a useful tool",
        "structured_first_batch": True,
        "cwd": str(tmp_path),
    }
    first = asyncio.create_task(handler.handle(args))
    await asyncio.wait_for(entered.wait(), 5)
    second = await asyncio.wait_for(handler.handle(args), 5)
    assert second.is_ok and not handler._closed and handler._active_owned_calls == 1
    release.set()
    first_result = await asyncio.wait_for(first, 5)
    assert first_result.is_ok and handler._closed and store._engine is None
    reader = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.sqlite'}")
    await reader.initialize()
    for result in (first_result, second):
        events = await reader.replay("interview", result.value.meta["session_id"])
        assert sum(event.type == "interview.started" for event in events) == 1
    await reader.close()
