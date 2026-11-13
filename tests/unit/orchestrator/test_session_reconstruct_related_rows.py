"""``reconstruct_session`` reads each session row once.

The method replays the session aggregate, then asks the related-events query
for execution-scope rows. That query also matches the session's own rows, which
``_merge_event_streams`` then dropped as id duplicates. The related query now
skips the ids the replay already returned.

The only change inside ``reconstruct_session`` is that extra keyword, so the
origin/main behavior is reproduced exactly by running the same method against a
related query that ignores it. Every fixture compares the two.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.session import SessionRepository, SessionTracker
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import events_table

SESSION_ID = "orch_reconstruct_rows"
EXECUTION_ID = "exec_reconstruct_rows"
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _event(
    event_type: str,
    data: dict[str, Any],
    at: datetime,
    *,
    aggregate_type: str = "session",
    aggregate_id: str = SESSION_ID,
    event_id: str | None = None,
) -> BaseEvent:
    extra = {} if event_id is None else {"id": event_id}
    return BaseEvent(
        type=event_type,
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        data=data,
        timestamp=at,
        **extra,
    )


def _start(at: datetime = T0) -> BaseEvent:
    return _event(
        "orchestrator.session.started",
        {"execution_id": EXECUTION_ID, "seed_id": "seed-rows", "start_time": at.isoformat()},
        at,
    )


def _progress(step: int, at: datetime, **extra: Any) -> BaseEvent:
    return _event(
        "orchestrator.progress.updated",
        {
            "message_type": "assistant",
            "content_preview": f"step {step}",
            "step": step,
            "progress": {"messages_processed": step + 1, "last_step": step, **extra},
        },
        at,
    )


def _workflow(index: int, at: datetime, completed: int, total: int = 2) -> BaseEvent:
    return _event(
        "workflow.progress.updated",
        {
            "execution_id": EXECUTION_ID,
            "session_id": SESSION_ID,
            "completed_count": completed,
            "total_count": total,
            "current_ac_index": index,
            "acceptance_criteria": [
                {
                    "index": i,
                    "content": f"ac {i}",
                    "status": "completed" if i < completed else "pending",
                }
                for i in range(total)
            ],
        },
        at,
        aggregate_type="execution",
        aggregate_id=EXECUTION_ID,
    )


async def _raw_insert(store: EventStore, *events: BaseEvent) -> None:
    """Write rows the guarded append API refuses, as databases from older writers hold."""
    async with store._engine.begin() as conn:
        for event in events:
            await conn.execute(events_table.insert().values(**event.to_db_dict()))


Fixture = Callable[[EventStore], Awaitable[None]]


async def _session_rows_only(store: EventStore) -> None:
    """No related rows besides the session's own: the merge is skipped now."""
    await store.append(_start())
    await store.append_batch(
        [_progress(step, T0 + timedelta(seconds=step + 1)) for step in range(5)]
    )
    # Timestamp ties broken by id, inserted against id order.
    tied = T0 + timedelta(seconds=30)
    await store.append_batch(
        [
            _event(
                "orchestrator.progress.updated", {"progress": {"tie": "z"}}, tied, event_id="z-tie"
            ),
            _event(
                "orchestrator.progress.updated", {"progress": {"tie": "a"}}, tied, event_id="a-tie"
            ),
        ]
    )


async def _session_rows_before_start(store: EventStore) -> None:
    await store.append(_start())
    await _raw_insert(
        store,
        _progress(0, T0 - timedelta(minutes=5), runtime_status="early"),
        _event("orchestrator.session.paused", {"reason": "early pause"}, T0 - timedelta(minutes=4)),
    )
    await store.append_batch(
        [_progress(step, T0 + timedelta(seconds=step)) for step in range(1, 4)]
    )
    await store.append(_workflow(0, T0 + timedelta(seconds=10), completed=1))


async def _execution_rows_keyed_by_session_id(store: EventStore) -> None:
    """Execution-typed rows whose aggregate id is the session id must stay related."""
    await store.append(_start())
    await store.append(_progress(0, T0 + timedelta(seconds=1)))
    await store.append_batch(
        [
            _event(
                "execution.tool.started",
                {"tool_name": "Edit"},
                T0 + timedelta(seconds=2),
                aggregate_type="execution",
                aggregate_id=SESSION_ID,
            ),
            _event(
                "execution.session.completed",
                {"runtime_status": "completed"},
                T0 + timedelta(seconds=3),
                aggregate_type="execution",
                aggregate_id=SESSION_ID,
            ),
            _workflow(1, T0 + timedelta(seconds=4), completed=1),
        ]
    )


async def _parallel_scopes_and_foreign_rows(store: EventStore) -> None:
    await store.append(_start())
    await store.append_batch(
        [
            _progress(0, T0 + timedelta(seconds=1)),
            _workflow(0, T0 + timedelta(seconds=2), completed=0),
            _event(
                "execution.agent.thinking",
                {"parent_execution_id": EXECUTION_ID},
                T0 + timedelta(seconds=3),
                aggregate_type="execution",
                aggregate_id=f"{EXECUTION_ID}_ac_0",
            ),
            _event(
                "execution.coordinator.tool.started",
                {"session_id": SESSION_ID},
                T0 + timedelta(seconds=4),
                aggregate_type="execution",
                aggregate_id=f"{EXECUTION_ID}_level_1",
            ),
            # Another session aggregate that names this session in its payload.
            _event(
                "orchestrator.progress.updated",
                {"session_id": SESSION_ID, "progress": {"foreign": True}},
                T0 + timedelta(seconds=5),
                aggregate_id="orch_other_session",
            ),
            _event(
                "execution.tool.started",
                {"session_id": "orch_unrelated"},
                T0 + timedelta(seconds=6),
                aggregate_type="execution",
                aggregate_id="exec_unrelated",
            ),
            _workflow(1, T0 + timedelta(seconds=7), completed=2),
        ]
    )


async def _completed_then_late_progress(store: EventStore) -> None:
    repository = SessionRepository(store)
    created = await repository.create_session(
        execution_id=EXECUTION_ID, seed_id="seed-rows", session_id=SESSION_ID
    )
    assert created.is_ok
    now = datetime.now(UTC)
    await store.append_batch(
        [_progress(0, now + timedelta(seconds=1)), _workflow(0, now + timedelta(seconds=2), 1)]
    )
    assert (await repository.mark_completed(SESSION_ID, summary={"done": True})).is_ok
    await store.append_batch(
        [
            _progress(1, now + timedelta(minutes=1), runtime_status="running"),
            _workflow(1, now + timedelta(minutes=1, seconds=1), completed=1),
        ]
    )


async def _paused_then_resumed(store: EventStore) -> None:
    await store.append(_start())
    await store.append_batch(
        [
            _progress(0, T0 + timedelta(seconds=1)),
            _event(
                "orchestrator.session.paused",
                {"reason": "rate limit", "pause_kind": "rate_limit"},
                T0 + timedelta(seconds=2),
            ),
            _progress(1, T0 + timedelta(seconds=3), runtime_status="running"),
            _workflow(0, T0 + timedelta(seconds=4), completed=1),
        ]
    )


async def _no_start_event(store: EventStore) -> None:
    await store.append_batch([_progress(0, T0), _workflow(0, T0 + timedelta(seconds=1), 0)])


async def _no_rows(store: EventStore) -> None:
    return None


FIXTURES: dict[str, Fixture] = {
    "session_rows_only": _session_rows_only,
    "session_rows_before_start": _session_rows_before_start,
    "execution_rows_keyed_by_session_id": _execution_rows_keyed_by_session_id,
    "parallel_scopes_and_foreign_rows": _parallel_scopes_and_foreign_rows,
    "completed_then_late_progress": _completed_then_late_progress,
    "paused_then_resumed": _paused_then_resumed,
    "no_start_event": _no_start_event,
    "no_rows": _no_rows,
}


def _origin_main_related_query(store: EventStore):
    """The related query as origin/main called it: without ``exclude_event_ids``."""
    original = store.query_session_related_events

    async def call(**kwargs: Any) -> list[BaseEvent]:
        kwargs.pop("exclude_event_ids")
        return await original(**kwargs)

    return patch.object(store, "query_session_related_events", side_effect=call)


def _outcome(result: Any) -> tuple[str, object]:
    if result.is_ok:
        return "ok", result.value
    error = result.error
    assert isinstance(error, PersistenceError)
    return "err", (error.message, error.details)


@pytest.fixture
async def store(tmp_path):
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'sessions.db'}")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("fixture_name", sorted(FIXTURES))
async def test_reconstruction_matches_origin_main(
    store: EventStore, fixture_name: str, strict: bool
) -> None:
    await FIXTURES[fixture_name](store)
    repository = SessionRepository(store)

    current = await repository.reconstruct_session(SESSION_ID, strict_related_events=strict)
    with _origin_main_related_query(store):
        reference = await repository.reconstruct_session(SESSION_ID, strict_related_events=strict)

    assert _outcome(current) == _outcome(reference)


async def test_fixture_table_reaches_the_paths_it_names(tmp_path) -> None:
    """Guard the table itself so the comparison above keeps meaning something."""
    trackers: dict[str, SessionTracker] = {}
    for name, populate in FIXTURES.items():
        fixture_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
        await fixture_store.initialize()
        try:
            await populate(fixture_store)
            result = await SessionRepository(fixture_store).reconstruct_session(SESSION_ID)
            if result.is_ok:
                trackers[name] = result.value
        finally:
            await fixture_store.close()

    assert set(FIXTURES) - set(trackers) == {"no_start_event", "no_rows"}
    assert trackers["session_rows_only"].progress["tie"] == "z"
    # Only the pre-start rows carry a runtime status and a pause: both came through.
    assert trackers["session_rows_before_start"].progress["runtime_status"] == "paused"
    assert trackers["execution_rows_keyed_by_session_id"].messages_processed == 3
    assert trackers["parallel_scopes_and_foreign_rows"].progress.get("foreign") is True
    assert trackers["completed_then_late_progress"].status.value == "completed"
    assert trackers["paused_then_resumed"].status.value == "running"


async def test_row_appended_between_the_two_reads_is_still_returned(tmp_path) -> None:
    """Exclusion is by the ids the replay returned, not by aggregate.

    A session row committed after the replay but before the related query was
    picked up by the related query on origin/main; it still is.
    """
    outcomes = []
    for name in ("current", "reference"):
        store = EventStore(f"sqlite+aiosqlite:///{tmp_path / f'race-{name}.db'}")
        await store.initialize()
        try:
            await _parallel_scopes_and_foreign_rows(store)
            original = store.query_session_related_events
            late = _progress(99, T0 + timedelta(minutes=5), runtime_status="late")

            async def append_then_query(
                original=original, store=store, late=late, name=name, **kwargs: Any
            ):
                await store.append(late)
                if name == "reference":
                    kwargs.pop("exclude_event_ids")
                return await original(**kwargs)

            with patch.object(store, "query_session_related_events", side_effect=append_then_query):
                result = await SessionRepository(store).reconstruct_session(SESSION_ID)
            assert result.is_ok
            assert result.value.progress["runtime_status"] == "late"
            outcomes.append(result.value)
        finally:
            await store.close()

    current, reference = outcomes
    assert current == reference


async def test_session_rows_are_deserialized_once(store: EventStore) -> None:
    await store.append(_start())
    session_rows = [_progress(step, T0 + timedelta(seconds=step + 1)) for step in range(1_000)]
    execution_rows = [
        _workflow(0, T0 + timedelta(seconds=step, milliseconds=500), 0) for step in range(100)
    ]
    for offset in range(0, len(session_rows), 250):
        await store.append_batch(session_rows[offset : offset + 250])
    await store.append_batch(execution_rows)
    repository = SessionRepository(store)
    original_from_db_row = BaseEvent.from_db_row

    async def rows_read(run: Callable[[], Awaitable[Any]]) -> tuple[int, Any]:
        with patch.object(BaseEvent, "from_db_row", side_effect=original_from_db_row) as spy:
            result = await run()
        return spy.call_count, result

    current_rows, current = await rows_read(lambda: repository.reconstruct_session(SESSION_ID))
    with _origin_main_related_query(store):
        reference_rows, reference = await rows_read(
            lambda: repository.reconstruct_session(SESSION_ID)
        )

    assert current.value == reference.value
    # 1,001 session rows once + 100 execution rows; origin/main read the session rows twice.
    assert current_rows == 1_001 + 100
    assert reference_rows == 2 * 1_001 + 100
