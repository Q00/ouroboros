from datetime import UTC, datetime
import json
from pathlib import Path

import pytest

from ouroboros.bigbang.seed_generator import save_seed_sync
from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.resources.handlers import (
    EventsResourceHandler,
    SeedsResourceHandler,
    SessionsResourceHandler,
)
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore


def _demo_seed(seed_id: str) -> Seed:
    return Seed(
        goal="Demo goal",
        ontology_schema=OntologySchema(name="demo", description="demo ontology"),
        metadata=SeedMetadata(seed_id=seed_id),
    )


@pytest.mark.asyncio
async def test_seeds_handler_lists_real_seed_files(tmp_path: Path) -> None:
    seeds_dir = tmp_path / "seeds"
    save_result = save_seed_sync(_demo_seed("seed_demo"), seeds_dir / "seed_demo.yaml")
    assert save_result.is_ok

    handler = SeedsResourceHandler(seed_dir=seeds_dir)
    result = await handler.handle("ouroboros://seeds")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["count"] == 1
    assert payload["seeds"][0]["id"] == "seed_demo"
    assert payload["seeds"][0]["goal"] == "Demo goal"
    assert "Example Seed" not in (result.value.text or "")


@pytest.mark.asyncio
async def test_seeds_handler_reads_specific_seed_by_id(tmp_path: Path) -> None:
    seeds_dir = tmp_path / "seeds"
    save_result = save_seed_sync(_demo_seed("seed_demo"), seeds_dir / "seed_demo.yaml")
    assert save_result.is_ok

    handler = SeedsResourceHandler(seed_dir=seeds_dir)
    result = await handler.handle("ouroboros://seeds/seed_demo")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["id"] == "seed_demo"
    assert payload["seed"]["goal"] == "Demo goal"


@pytest.mark.asyncio
async def test_sessions_handler_reads_event_store_sessions(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()

    repo = SessionRepository(store)
    created = await repo.create_session("exec_1", "seed_1", "orch_1")
    assert created.is_ok
    progress = await repo.track_progress(
        "orch_1",
        {"messages_processed": 3, "current_phase": "design"},
    )
    assert progress.is_ok

    handler = SessionsResourceHandler(event_store=store)

    sessions_result = await handler.handle("ouroboros://sessions")
    current_result = await handler.handle("ouroboros://sessions/current")

    assert sessions_result.is_ok
    assert current_result.is_ok

    sessions_payload = json.loads(sessions_result.value.text or "{}")
    current_payload = json.loads(current_result.value.text or "{}")

    assert sessions_payload["count"] == 1
    assert sessions_payload["sessions"][0]["session_id"] == "orch_1"
    assert sessions_payload["sessions"][0]["status"] == "running"
    assert sessions_payload["sessions"][0]["messages_processed"] == 3
    assert current_payload["session"]["session_id"] == "orch_1"

    await store.close()


@pytest.mark.asyncio
async def test_sessions_current_uses_latest_event_activity_when_reconstructed_times_are_empty(
    tmp_path: Path,
) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()

    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            timestamp=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            aggregate_type="session",
            aggregate_id="orch_old",
            data={
                "execution_id": "exec_old",
                "seed_id": "seed_old",
                "start_time": "2026-04-01T00:00:00+00:00",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            timestamp=datetime(2026, 4, 1, 0, 1, tzinfo=UTC),
            aggregate_type="session",
            aggregate_id="orch_new",
            data={
                "execution_id": "exec_new",
                "seed_id": "seed_new",
                "start_time": "2026-04-01T00:01:00+00:00",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="orchestrator.progress.updated",
            timestamp=datetime(2026, 4, 1, 0, 2, tzinfo=UTC),
            aggregate_type="session",
            aggregate_id="orch_new",
            data={"progress": {"messages_processed": 2}},
        )
    )

    handler = SessionsResourceHandler(event_store=store)
    result = await handler.handle("ouroboros://sessions/current")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["session"]["session_id"] == "orch_new"
    assert payload["session"]["last_message_time"] is None
    assert payload["session"]["last_activity"] == "2026-04-01T00:02:00+00:00"

    await store.close()


@pytest.mark.asyncio
async def test_sessions_current_uses_related_execution_activity(
    tmp_path: Path,
) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()

    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            timestamp=datetime(2026, 4, 1, 0, 0, tzinfo=UTC),
            aggregate_type="session",
            aggregate_id="orch_old",
            data={
                "execution_id": "exec_old",
                "seed_id": "seed_old",
                "start_time": "2026-04-01T00:00:00+00:00",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="orchestrator.session.started",
            timestamp=datetime(2026, 4, 1, 0, 5, tzinfo=UTC),
            aggregate_type="session",
            aggregate_id="orch_new",
            data={
                "execution_id": "exec_new",
                "seed_id": "seed_new",
                "start_time": "2026-04-01T00:05:00+00:00",
            },
        )
    )
    await store.append(
        BaseEvent(
            type="orchestrator.progress.updated",
            timestamp=datetime(2026, 4, 1, 0, 6, tzinfo=UTC),
            aggregate_type="session",
            aggregate_id="orch_new",
            data={"progress": {"messages_processed": 1}},
        )
    )
    await store.append(
        BaseEvent(
            type="workflow.progress.updated",
            timestamp=datetime(2026, 4, 1, 0, 10, tzinfo=UTC),
            aggregate_type="execution",
            aggregate_id="exec_old",
            data={
                "session_id": "orch_old",
                "messages_count": 4,
                "current_phase": "execute",
            },
        )
    )

    handler = SessionsResourceHandler(event_store=store)
    result = await handler.handle("ouroboros://sessions/current")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["session"]["session_id"] == "orch_old"
    assert payload["session"]["status"] == "running"
    assert payload["session"]["last_activity"] == "2026-04-01T00:10:00+00:00"

    await store.close()


@pytest.mark.asyncio
async def test_sessions_current_sorts_mixed_offset_activity_as_datetimes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = SessionsResourceHandler()

    async def list_sessions() -> list[dict[str, object]]:
        return [
            {
                "session_id": "utc_equivalent",
                "status": "running",
                "last_activity": "2026-04-01T01:00:00+00:00",
            },
            {
                "session_id": "offset_equivalent",
                "status": "running",
                "last_activity": "2026-04-01T10:00:00+09:00",
            },
            {
                "session_id": "actual_latest",
                "status": "running",
                "last_activity": "2026-04-01T01:01:00+00:00",
            },
        ]

    monkeypatch.setattr(handler, "_list_sessions", list_sessions)

    result = await handler.handle("ouroboros://sessions/current")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["session"]["session_id"] == "actual_latest"


@pytest.mark.asyncio
async def test_sessions_handler_reads_specific_session(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()

    repo = SessionRepository(store)
    created = await repo.create_session("exec_1", "seed_1", "orch_1")
    assert created.is_ok

    handler = SessionsResourceHandler(event_store=store)
    result = await handler.handle("ouroboros://sessions/orch_1")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["session"]["session_id"] == "orch_1"
    assert payload["session"]["execution_id"] == "exec_1"

    await store.close()


@pytest.mark.asyncio
async def test_events_handler_reads_recent_events(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()

    repo = SessionRepository(store)
    created = await repo.create_session("exec_1", "seed_1", "orch_1")
    assert created.is_ok

    handler = EventsResourceHandler(event_store=store)
    result = await handler.handle("ouroboros://events")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["count"] >= 1
    assert payload["events"][0]["aggregate_id"] == "orch_1"
    assert payload["events"][0]["type"] == "orchestrator.session.started"

    await store.close()


@pytest.mark.asyncio
async def test_events_handler_reads_session_related_events(tmp_path: Path) -> None:
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'events.db'}")
    await store.initialize()

    repo = SessionRepository(store)
    created = await repo.create_session("exec_1", "seed_1", "orch_1")
    assert created.is_ok
    progress = await repo.track_progress("orch_1", {"messages_processed": 1})
    assert progress.is_ok

    handler = EventsResourceHandler(event_store=store)
    result = await handler.handle("ouroboros://events/orch_1")

    assert result.is_ok
    payload = json.loads(result.value.text or "{}")
    assert payload["session_id"] == "orch_1"
    assert payload["count"] >= 2
    assert {event["type"] for event in payload["events"]} >= {
        "orchestrator.session.started",
        "orchestrator.progress.updated",
    }

    await store.close()
