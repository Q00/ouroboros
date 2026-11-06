"""In-process QA results have a durable, content-minimal status."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.tools import qa_session
from ouroboros.mcp.tools.qa import QAHandler
from ouroboros.mcp.tools.qa_session import QA_EVALUATED
from ouroboros.mcp.tools.query_handlers import SessionStatusHandler
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence import qa_session_store
from ouroboros.persistence.event_store import EventStore, sqlite_database_url
from ouroboros.persistence.qa_session_store import QASessionConflict
from ouroboros.providers.base import CompletionResponse, UsageInfo


@pytest.fixture
async def store(tmp_path):
    value = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await value.initialize()
    yield value
    await value.close()


def qa(store, score=0.9):
    adapter = AsyncMock()
    adapter.complete.return_value = Result.ok(
        CompletionResponse(content=f'{{"score": {score}}}', model="fake", usage=UsageInfo(0, 0, 0))
    )
    return QAHandler(llm_adapter=adapter, llm_backend="claude_code", event_store=store)


async def test_default_handlers_share_persistent_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = await qa(None).handle({"artifact": "x", "quality_bar": "works"})
    assert result.is_ok
    session_id = result.value.meta["qa_session_id"]
    status = await SessionStatusHandler().handle({"session_id": session_id})
    assert status.is_ok
    assert status.value.meta["session_kind"] == "qa"


async def test_mcp_tool_dispatch_returns_queryable_qa_session(store):
    server = MCPServerAdapter()
    server.register_tool(qa(store))
    server.register_tool(SessionStatusHandler(event_store=store))
    result = await server.call_tool("ouroboros_qa", {"artifact": "x", "quality_bar": "works"})
    assert result.is_ok
    status = await server.call_tool(
        "ouroboros_session_status", {"session_id": result.value.meta["qa_session_id"]}
    )
    assert status.is_ok
    assert status.value.meta["session_kind"] == "qa"


async def test_success_is_reconstructible_without_persisting_artifact(store, tmp_path):
    result = await qa(store).handle({"artifact": "private artifact", "quality_bar": "private bar"})
    assert result.is_ok
    session_id = result.value.meta["qa_session_id"]
    reopened = EventStore(sqlite_database_url(tmp_path / "events.db"))
    try:
        status = await SessionStatusHandler(event_store=reopened).handle({"session_id": session_id})
    finally:
        await reopened.close()
    assert status.is_ok
    assert status.value.meta["session_kind"] == "qa"
    assert status.value.meta["status"] == "completed"
    assert status.value.meta["score"] == 0.9
    events, _ = await store.get_events_after("session", session_id, 0)
    assert len(events) == 1 and events[0].type == QA_EVALUATED
    assert "private" not in str(events[0].data)


async def test_multiple_evaluations_use_last_committed_record_and_allow_io_journal(store):
    handler = qa(store, 0.7)
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-shared"}
    first = await handler.handle(args)
    assert first.is_ok
    await store.append(
        BaseEvent(type="llm.call.requested", aggregate_type="session", aggregate_id="qa-shared")
    )
    second = await qa(store, 0.9).handle({**args, "iteration_history": [{}]})
    assert second.is_ok
    status = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-shared"})
    assert status.is_ok
    assert status.value.meta["iteration"] == 2
    assert status.value.meta["score"] == 0.9


async def test_last_committed_evaluation_wins_even_with_older_timestamp(store):
    handler = qa(store)
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-clock"}
    assert (await handler.handle(args)).is_ok
    await store.append(
        BaseEvent(
            type=QA_EVALUATED,
            aggregate_type="session",
            aggregate_id="qa-clock",
            timestamp=datetime(2000, 1, 1, tzinfo=UTC),
            data={
                "iteration": 2,
                "score": 0.1,
                "pass_threshold": 0.8,
                "passed": False,
                "verdict": "fail",
            },
        )
    )
    status = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-clock"})
    assert status.is_ok
    assert status.value.meta["iteration"] == 2
    assert status.value.meta["score"] == 0.1


async def test_existing_or_concurrent_other_session_cannot_be_reported_as_qa(store):
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-collision"}
    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="qa-collision",
            data={"execution_id": "exec-existing", "seed_id": "seed"},
        )
    )
    assert (await qa(store).handle(args)).is_err
    await store.append(
        BaseEvent(
            type=QA_EVALUATED,
            aggregate_type="session",
            aggregate_id="qa-collision",
            data={
                "iteration": 1,
                "score": 1.0,
                "pass_threshold": 0.8,
                "passed": True,
                "verdict": "pass",
            },
        )
    )
    status = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-collision"})
    assert status.is_err
    assert "shared" in str(status.error)


async def test_session_claim_between_check_and_append_fails_closed(store, monkeypatch):
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-racing"}
    append = qa_session.append_qa_evaluation

    async def competing_append(event_store, event):
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="qa-racing",
                data={"execution_id": "exec-race", "seed_id": "seed"},
            )
        )
        await append(event_store, event)

    monkeypatch.setattr(qa_session, "append_qa_evaluation", competing_append)
    result = await qa(store).handle(args)
    assert result.is_err
    status = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-racing"})
    assert status.is_ok
    assert status.value.meta["execution_id"] == "exec-race"
    events, _ = await store.get_events_after("session", "qa-racing", 0)
    assert all(event.type != QA_EVALUATED for event in events)


async def test_session_start_after_qa_commit_cannot_take_its_id(store):
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-claimed"}
    assert (await qa(store).handle(args)).is_ok
    with pytest.raises(PersistenceError):
        await store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="qa-claimed",
                data={"execution_id": "exec-late", "seed_id": "seed"},
            )
        )
    status = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-claimed"})
    assert status.is_ok and status.value.meta["session_kind"] == "qa"


async def test_concurrent_qa_and_execution_claim_have_exactly_one_owner(store, tmp_path):
    peer = EventStore(sqlite_database_url(tmp_path / "events.db"))
    await peer.initialize()
    try:
        meta = {
            "iteration": 1,
            "score": 0.9,
            "pass_threshold": 0.8,
            "passed": True,
            "verdict": "pass",
        }
        qa_result, execution_result = await asyncio.gather(
            qa_session.record_qa_evaluation(store, "qa-concurrent", meta),
            SessionRepository(peer).create_session("exec-race", "seed", "qa-concurrent"),
            return_exceptions=True,
        )
        qa_won = qa_result is None
        assert not isinstance(execution_result, BaseException)
        assert qa_won != execution_result.is_ok
        if not qa_won:
            assert isinstance(qa_result, QASessionConflict)
        status = await SessionStatusHandler(event_store=store).handle(
            {"session_id": "qa-concurrent"}
        )
        assert status.is_ok
        assert (status.value.meta.get("session_kind") == "qa") == qa_won
    finally:
        await peer.close()


async def test_rejected_or_failed_qa_does_not_write_success(store):
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-failure"}
    handler = qa(store)
    handler.llm_adapter.complete.return_value = Result.err(ValueError("failure"))
    assert (await handler.handle(args)).is_err
    events, _ = await store.get_events_after("session", "qa-failure", 0)
    assert events == []


async def test_cancelled_evaluation_does_not_publish_success(store):
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-cancelled"}
    handler = qa(store)
    started = asyncio.Event()

    async def waiting(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    handler.llm_adapter.complete.side_effect = waiting
    task = asyncio.create_task(handler.handle(args))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    events, _ = await store.get_events_after("session", "qa-cancelled", 0)
    assert events == []


async def test_cancel_during_write_waits_for_settlement_and_preserves_completed_record(
    store, monkeypatch
):
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-settlement"}
    entered, release = asyncio.Event(), asyncio.Event()
    insert = qa_session_store.insert_event_with_picker_projection

    async def held_insert(conn, event, projection_ready):
        entered.set()
        await release.wait()
        await insert(conn, event, projection_ready)

    monkeypatch.setattr(qa_session_store, "insert_event_with_picker_projection", held_insert)
    task = asyncio.create_task(qa(store).handle(args))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    status = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-settlement"})
    assert status.is_ok and status.value.meta["session_kind"] == "qa"


async def test_write_failure_does_not_return_success_or_publish_record(store, monkeypatch):
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": "qa-write-error"}
    monkeypatch.setattr(
        qa_session, "append_qa_evaluation", AsyncMock(side_effect=OSError("write failed"))
    )
    result = await qa(store).handle(args)
    assert result.is_err
    events, _ = await store.get_events_after("session", "qa-write-error", 0)
    assert events == []


async def test_bad_record_and_auto_namespace_fail_closed(store):
    assert (
        await qa(store).handle({"artifact": "x", "quality_bar": "q", "qa_session_id": "auto_1"})
    ).is_err
    await store.append(
        BaseEvent(type=QA_EVALUATED, aggregate_type="session", aggregate_id="qa-bad", data={})
    )
    result = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-bad"})
    assert result.is_err


@pytest.mark.parametrize(
    ("score", "threshold", "passed", "verdict"),
    [(0.9, 0.8, True, "fail"), (0.5, 0.8, False, "fail"), (0.2, 0.8, False, "revise")],
)
async def test_score_verdict_disagreement_is_not_reported_as_valid(
    store, score, threshold, passed, verdict
):
    await store.append(
        BaseEvent(
            type=QA_EVALUATED,
            aggregate_type="session",
            aggregate_id="qa-mismatch",
            data={
                "iteration": 1,
                "score": score,
                "pass_threshold": threshold,
                "passed": passed,
                "verdict": verdict,
            },
        )
    )
    result = await SessionStatusHandler(event_store=store).handle({"session_id": "qa-mismatch"})
    assert result.is_err


async def test_large_compatible_history_remains_queryable_and_detects_conflict(store):
    from ouroboros.persistence.schema import events_table

    session_id = "qa-long-history"
    args = {"artifact": "x", "quality_bar": "works", "qa_session_id": session_id}
    assert (await qa(store, 0.7).handle(args)).is_ok
    assert store._engine is not None
    journal = [
        BaseEvent(
            type="llm.call.returned", aggregate_type="session", aggregate_id=session_id
        ).to_db_dict()
        for _ in range(10_001)
    ]
    async with store._engine.begin() as conn:
        await conn.execute(events_table.insert(), journal)
    assert (await qa(store, 0.9).handle({**args, "iteration_history": [{}]})).is_ok
    status = await SessionStatusHandler(event_store=store).handle({"session_id": session_id})
    assert status.is_ok
    assert status.value.meta["iteration"] == 2
    assert status.value.meta["score"] == 0.9
    await store.append(
        BaseEvent(
            type="orchestrator.session.progressed",
            aggregate_type="session",
            aggregate_id=session_id,
        )
    )
    conflict = await SessionStatusHandler(event_store=store).handle({"session_id": session_id})
    assert conflict.is_err
    assert "shared by QA" in str(conflict.error)
