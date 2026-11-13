"""Lifecycle-only replay behind the job monitor's session-acceptance check.

``JobManager._read_authoritative_session_acceptance`` runs on every
execute-seed monitor tick. It needs only the session's first start row and its
last terminal row, so it asks ``EventStore.replay`` for those event types
instead of deserializing every progress row. These tests pin that the result
(return value or raised ``PersistenceError``) is identical to the previous
full-replay implementation, kept verbatim below as the reference.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.job_manager import (
    JobLinks,
    JobManager,
    JobSnapshot,
    JobStatus,
    _AuthoritativeSessionAcceptance,
    _canonical_acceptance_payload,
)
from ouroboros.orchestrator.session import (
    ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY,
    SessionRepository,
)
from ouroboros.persistence.event_store import (
    EventStore,
    acceptance_generation_id_for_session,
    validate_acceptance_finalization_payload,
)
from ouroboros.persistence.schema import events_table

SESSION_ID = "orch_lifecycle_replay"
EXECUTION_ID = "exec_lifecycle_replay"
LIFECYCLE_TYPES = frozenset(
    {
        "orchestrator.session.started",
        "orchestrator.session.completed",
        "orchestrator.session.failed",
        "orchestrator.session.cancelled",
    }
)
T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


# Verbatim copy of the origin/main (592c667f) implementation, which replayed the
# whole session aggregate. Only the ``def`` line changed (method -> function).
async def _reference_read_authoritative_session_acceptance(
    self: JobManager,
    snapshot: JobSnapshot,
) -> _AuthoritativeSessionAcceptance | None:
    """Validate the complete Foundation B terminal plan for one linked session.

    ``None`` means that no Foundation B terminal plan is present. A present
    but malformed, incomplete, mis-scoped, or partially persisted plan raises
    ``PersistenceError`` so recovery fails closed instead of falling back to
    provisional attempt telemetry.
    """
    session_id = snapshot.links.session_id
    execution_id = snapshot.links.execution_id
    if not session_id or not execution_id:
        return None

    session_events = await self._event_store.replay("session", session_id)
    start_event = next(
        (event for event in session_events if event.type == "orchestrator.session.started"),
        None,
    )
    if start_event is None:
        return None
    if start_event.data.get("execution_id") != execution_id:
        raise PersistenceError(
            "Linked session start belongs to a different execution.",
            operation="mcp_recover_authoritative_acceptance",
            details={"session_id": session_id, "execution_id": execution_id},
        )
    current_format = ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY in start_event.data

    terminal_event = next(
        (
            event
            for event in reversed(session_events)
            if event.type
            in {
                "orchestrator.session.completed",
                "orchestrator.session.failed",
                "orchestrator.session.cancelled",
            }
        ),
        None,
    )
    if terminal_event is None:
        if current_format:
            raise PersistenceError(
                "Current-format linked session has no terminal acceptance plan.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            )
        return None
    if "acceptance_finalizations" not in terminal_event.data:
        if current_format:
            raise PersistenceError(
                "Current-format linked terminal session is missing its acceptance plan.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            )
        return None
    terminal_status = {
        "orchestrator.session.completed": "completed",
        "orchestrator.session.failed": "failed",
        "orchestrator.session.cancelled": "cancelled",
    }[terminal_event.type]
    raw_plan = terminal_event.data.get("acceptance_finalizations")
    if not isinstance(raw_plan, list):
        raise PersistenceError(
            "Linked terminal session has a malformed acceptance plan.",
            operation="mcp_recover_authoritative_acceptance",
            details={"session_id": session_id, "execution_id": execution_id},
        )

    expected_roots: set[int] | None = None
    if ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY in start_event.data:
        raw_roots = start_event.data[ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY]
        if not isinstance(raw_roots, list):
            raise PersistenceError(
                "Linked session has a malformed durable acceptance root set.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            )
        expected_roots = set()
        for raw_root in raw_roots:
            if (
                isinstance(raw_root, bool)
                or not isinstance(raw_root, int)
                or raw_root < 0
                or raw_root in expected_roots
            ):
                raise PersistenceError(
                    "Linked session has an invalid durable acceptance root set.",
                    operation="mcp_recover_authoritative_acceptance",
                    details={"session_id": session_id, "execution_id": execution_id},
                )
            expected_roots.add(raw_root)

    planned_by_root: dict[int, dict[str, Any]] = {}
    for raw_payload in raw_plan:
        if not isinstance(raw_payload, Mapping):
            raise PersistenceError(
                "Linked terminal acceptance plan contains a malformed entry.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            )
        payload = dict(raw_payload)
        try:
            _generation, root = validate_acceptance_finalization_payload(
                payload,
                aggregate_id=execution_id,
            )
        except PersistenceError as exc:
            raise PersistenceError(
                "Linked terminal acceptance plan contains invalid final evidence.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            ) from exc
        if (
            payload.get("session_id") != session_id
            or payload.get("execution_id") != execution_id
            or payload.get("terminal_status") != terminal_status
        ):
            raise PersistenceError(
                "Linked terminal acceptance plan is mis-scoped.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            )
        existing = planned_by_root.get(root)
        if existing is not None:
            if _canonical_acceptance_payload(existing) != _canonical_acceptance_payload(payload):
                raise PersistenceError(
                    "Linked terminal acceptance plan contains conflicting root decisions.",
                    operation="mcp_recover_authoritative_acceptance",
                    details={"session_id": session_id, "execution_id": execution_id},
                )
            # EventStore treats byte-equivalent duplicate entries in the
            # same terminal plan as idempotent guard hits. Canonicalize
            # them here so recovery mirrors the durable writer exactly.
            continue
        planned_by_root[root] = payload

    if expected_roots is not None and set(planned_by_root) != expected_roots:
        raise PersistenceError(
            "Linked terminal acceptance plan is incomplete for its durable root set.",
            operation="mcp_recover_authoritative_acceptance",
            details={
                "session_id": session_id,
                "execution_id": execution_id,
                "expected_root_indices": sorted(expected_roots),
                "planned_root_indices": sorted(planned_by_root),
            },
        )

    all_final_events = await self._event_store.query_events(
        aggregate_id=execution_id,
        event_type="execution.ac.acceptance_finalized",
        limit=None,
    )
    scoped_events = tuple(
        event for event in all_final_events if event.data.get("session_id") == session_id
    )
    persisted_by_root: dict[int, dict[str, Any]] = {}
    for acceptance_event in scoped_events:
        try:
            _generation, root = validate_acceptance_finalization_payload(
                acceptance_event.data,
                aggregate_id=acceptance_event.aggregate_id,
            )
        except PersistenceError as exc:
            raise PersistenceError(
                "Linked session contains malformed persisted final acceptance.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            ) from exc
        payload = dict(acceptance_event.data)
        existing = persisted_by_root.get(root)
        if existing is not None:
            raise PersistenceError(
                "Linked session contains duplicate persisted final acceptance.",
                operation="mcp_recover_authoritative_acceptance",
                details={"session_id": session_id, "execution_id": execution_id},
            )
        persisted_by_root[root] = payload

    if persisted_by_root != planned_by_root:
        raise PersistenceError(
            "Linked terminal plan and persisted final acceptance set disagree.",
            operation="mcp_recover_authoritative_acceptance",
            details={
                "session_id": session_id,
                "execution_id": execution_id,
                "planned_root_indices": sorted(planned_by_root),
                "persisted_root_indices": sorted(persisted_by_root),
            },
        )
    return _AuthoritativeSessionAcceptance(
        terminal_status=terminal_status,
        terminal_event=terminal_event,
        acceptance_events=scoped_events,
    )


def _event(
    event_type: str,
    data: dict[str, Any],
    *,
    at: datetime,
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


def _start(at: datetime, *, execution_id: str = EXECUTION_ID, roots: list[int] | None = None):
    data: dict[str, Any] = {"execution_id": execution_id, "seed_id": "seed-lifecycle"}
    if roots is not None:
        data[ACCEPTANCE_ROOT_INDICES_PROGRESS_KEY] = roots
    return _event("orchestrator.session.started", data, at=at)


def _acceptance(root: int, terminal_status: str) -> dict[str, Any]:
    accepted = terminal_status == "completed"
    verdict = {"completed": "succeeded", "failed": "failed", "cancelled": "cancelled"}
    return {
        "execution_id": EXECUTION_ID,
        "session_id": SESSION_ID,
        "acceptance_generation_id": acceptance_generation_id_for_session(SESSION_ID, EXECUTION_ID),
        "root_ac_index": root,
        "final_retry_attempt": 0,
        "accepted": accepted,
        "disposition": "accepted" if accepted else verdict[terminal_status],
        "outcome": verdict[terminal_status],
        "terminal_status": terminal_status,
    }


def _terminal(event_type: str, at: datetime, plan: object = None, *, event_id: str | None = None):
    data: dict[str, Any] = {"summary": {"final_message": event_type}}
    if plan is not None:
        data["acceptance_finalizations"] = plan
    return _event(event_type, data, at=at, event_id=event_id)


def _final(payload: dict[str, Any], at: datetime) -> BaseEvent:
    return _event(
        "execution.ac.acceptance_finalized",
        payload,
        at=at,
        aggregate_type="execution",
        aggregate_id=EXECUTION_ID,
    )


def _progress(count: int, start: datetime) -> list[BaseEvent]:
    return [
        _event(
            "orchestrator.progress.updated",
            {"message_type": "assistant", "content": "x" * 64, "step": step},
            at=start + timedelta(milliseconds=step),
        )
        for step in range(count)
    ]


async def _raw_insert(store: EventStore, *events: BaseEvent) -> None:
    """Write rows the guarded append API refuses, as databases from older writers hold."""
    async with store._engine.begin() as conn:
        for event in events:
            await conn.execute(events_table.insert().values(**event.to_db_dict()))


def _snapshot(execution_id: str = EXECUTION_ID) -> JobSnapshot:
    now = datetime.now(UTC)
    return JobSnapshot(
        job_id="job_lifecycle_replay",
        job_type="execute_seed",
        status=JobStatus.RUNNING,
        message="running",
        created_at=now,
        updated_at=now,
        links=JobLinks(session_id=SESSION_ID, execution_id=execution_id),
    )


Fixture = Callable[[EventStore], Awaitable[None]]


async def _no_start(store: EventStore) -> None:
    await store.append_batch(_progress(20, T0))


async def _legacy_running(store: EventStore) -> None:
    await store.append(_start(T0))
    await store.append_batch(_progress(20, T0 + timedelta(seconds=1)))


async def _current_running(store: EventStore) -> None:
    await store.append(_start(T0, roots=[0]))
    await store.append_batch(_progress(20, T0 + timedelta(seconds=1)))


async def _legacy_terminal_without_plan(store: EventStore) -> None:
    await _legacy_running(store)
    await _raw_insert(store, _terminal("orchestrator.session.completed", T0 + timedelta(minutes=1)))


async def _current_terminal_without_plan(store: EventStore) -> None:
    await _current_running(store)
    await _raw_insert(store, _terminal("orchestrator.session.failed", T0 + timedelta(minutes=1)))


async def _complete_plan_via_repository(store: EventStore) -> None:
    repository = SessionRepository(store)
    created = await repository.create_session(
        execution_id=EXECUTION_ID,
        seed_id="seed-lifecycle",
        session_id=SESSION_ID,
        acceptance_root_indices=[0, 1],
    )
    assert created.is_ok
    await store.append_batch(_progress(20, datetime.now(UTC)))
    acceptance = [_acceptance(0, "completed"), _acceptance(1, "completed")]
    completed = await repository.mark_completed(
        SESSION_ID,
        summary={"final_message": "done"},
        acceptance_finalizations=[*acceptance, dict(acceptance[0])],
    )
    assert completed.is_ok and completed.value is True


async def _complete_failed_plan(store: EventStore) -> None:
    payload = _acceptance(0, "failed")
    await _current_running(store)
    await _raw_insert(
        store,
        _terminal("orchestrator.session.failed", T0 + timedelta(minutes=1), [payload]),
        _final(payload, T0 + timedelta(minutes=1)),
    )


async def _wrong_execution(store: EventStore) -> None:
    await store.append(_start(T0, execution_id="exec_someone_else", roots=[0]))
    await store.append_batch(_progress(20, T0 + timedelta(seconds=1)))


async def _malformed_plan(store: EventStore) -> None:
    await _current_running(store)
    await _raw_insert(
        store, _terminal("orchestrator.session.completed", T0 + timedelta(minutes=1), {"0": 1})
    )


async def _incomplete_plan(store: EventStore) -> None:
    payload = _acceptance(0, "completed")
    await store.append(_start(T0, roots=[0, 1]))
    await store.append_batch(_progress(20, T0 + timedelta(seconds=1)))
    await _raw_insert(
        store,
        _terminal("orchestrator.session.completed", T0 + timedelta(minutes=1), [payload]),
        _final(payload, T0 + timedelta(minutes=1)),
    )


async def _two_terminals_completed_last(store: EventStore) -> None:
    """The later-timestamped terminal is inserted first (lower rowid) and must win."""
    completed = _acceptance(0, "completed")
    failed = _acceptance(0, "failed")
    await _current_running(store)
    await _raw_insert(
        store,
        _terminal("orchestrator.session.completed", T0 + timedelta(minutes=2), [completed]),
        _final(completed, T0 + timedelta(minutes=2)),
        _terminal("orchestrator.session.failed", T0 + timedelta(minutes=1), [failed]),
    )


async def _two_terminals_failed_last(store: EventStore) -> None:
    completed = _acceptance(0, "completed")
    failed = _acceptance(0, "failed")
    await _current_running(store)
    await _raw_insert(
        store,
        _terminal("orchestrator.session.completed", T0 + timedelta(minutes=1), [completed]),
        _final(completed, T0 + timedelta(minutes=1)),
        _terminal("orchestrator.session.failed", T0 + timedelta(minutes=2), [failed]),
    )


async def _two_terminals_tied_timestamp(store: EventStore) -> None:
    """Equal timestamps: replay breaks the tie by event id, so the larger id wins."""
    cancelled = _acceptance(0, "cancelled")
    completed = _acceptance(0, "completed")
    at = T0 + timedelta(minutes=1)
    await _current_running(store)
    await _raw_insert(
        store,
        _terminal("orchestrator.session.cancelled", at, [cancelled], event_id="ffff-terminal"),
        _terminal("orchestrator.session.completed", at, [completed], event_id="0000-terminal"),
        _final(cancelled, at),
    )


async def _two_starts_first_inserted_last(store: EventStore) -> None:
    """The earliest start (wrong execution) is inserted after the matching one."""
    await _raw_insert(
        store,
        _start(T0 + timedelta(seconds=5), roots=[0]),
        _start(T0, execution_id="exec_someone_else", roots=[0]),
    )
    await store.append_batch(_progress(20, T0 + timedelta(seconds=10)))


async def _progress_before_start_and_after_terminal(store: EventStore) -> None:
    payload = _acceptance(0, "completed")
    await _current_running(store)
    await _raw_insert(
        store,
        *_progress(10, T0 - timedelta(minutes=5)),
        _terminal("orchestrator.session.completed", T0 + timedelta(minutes=1), [payload]),
        _final(payload, T0 + timedelta(minutes=1)),
    )
    await store.append_batch(_progress(10, T0 + timedelta(minutes=2)))


FIXTURES: dict[str, Fixture] = {
    "no_start_event": _no_start,
    "legacy_no_terminal_yet": _legacy_running,
    "current_no_terminal_yet": _current_running,
    "legacy_terminal_without_plan": _legacy_terminal_without_plan,
    "current_terminal_without_plan": _current_terminal_without_plan,
    "complete_plan_via_repository": _complete_plan_via_repository,
    "complete_failed_plan": _complete_failed_plan,
    "wrong_execution_id": _wrong_execution,
    "malformed_plan": _malformed_plan,
    "incomplete_plan": _incomplete_plan,
    "two_terminals_completed_last": _two_terminals_completed_last,
    "two_terminals_failed_last": _two_terminals_failed_last,
    "two_terminals_tied_timestamp": _two_terminals_tied_timestamp,
    "two_starts_first_inserted_last": _two_starts_first_inserted_last,
    "progress_before_start_and_after_terminal": _progress_before_start_and_after_terminal,
}


async def _outcome(
    read: Callable[[], Awaitable[_AuthoritativeSessionAcceptance | None]],
) -> tuple[str, object]:
    try:
        return "ok", await read()
    except PersistenceError as exc:
        return "err", (exc.message, exc.details, exc.operation)


@pytest.fixture
async def store(tmp_path):
    event_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'acceptance.db'}")
    await event_store.initialize()
    yield event_store
    await event_store.close()


@pytest.mark.parametrize("fixture_name", sorted(FIXTURES))
@pytest.mark.parametrize("execution_id", [EXECUTION_ID, "exec_not_linked"])
async def test_lifecycle_replay_matches_full_replay_reference(
    store: EventStore, fixture_name: str, execution_id: str
) -> None:
    await FIXTURES[fixture_name](store)
    manager = JobManager(store)
    snapshot = _snapshot(execution_id)

    current = await _outcome(lambda: manager._read_authoritative_session_acceptance(snapshot))
    reference = await _outcome(
        lambda: _reference_read_authoritative_session_acceptance(manager, snapshot)
    )

    assert current == reference


async def test_fixture_table_reaches_every_outcome_class(tmp_path) -> None:
    """Guard the table itself: it must keep covering None, success, and errors."""
    outcomes: dict[str, tuple[str, object]] = {}
    for name, populate in FIXTURES.items():
        fixture_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / f'{name}.db'}")
        await fixture_store.initialize()
        try:
            await populate(fixture_store)
            manager = JobManager(fixture_store)
            outcomes[name] = await _outcome(
                lambda: manager._read_authoritative_session_acceptance(_snapshot())
            )
        finally:
            await fixture_store.close()

    assert outcomes["no_start_event"] == ("ok", None)
    assert outcomes["legacy_no_terminal_yet"] == ("ok", None)
    assert outcomes["legacy_terminal_without_plan"] == ("ok", None)
    assert outcomes["current_no_terminal_yet"][0] == "err"
    assert outcomes["wrong_execution_id"][0] == "err"
    assert outcomes["two_starts_first_inserted_last"][0] == "err"
    for name, status in (
        ("complete_plan_via_repository", "completed"),
        ("complete_failed_plan", "failed"),
        ("two_terminals_completed_last", "completed"),
        ("two_terminals_tied_timestamp", "cancelled"),
        ("progress_before_start_and_after_terminal", "completed"),
    ):
        kind, result = outcomes[name]
        assert kind == "ok" and isinstance(result, _AuthoritativeSessionAcceptance), name
        assert result.terminal_status == status, name
    assert outcomes["two_terminals_failed_last"][0] == "err"


async def test_monitor_check_deserializes_only_lifecycle_rows(store: EventStore) -> None:
    payload = _acceptance(0, "completed")
    await store.append(_start(T0, roots=[0]))
    progress = _progress(1_000, T0 + timedelta(seconds=1))
    for offset in range(0, len(progress), 250):
        await store.append_batch(progress[offset : offset + 250])
    await _raw_insert(
        store,
        _terminal("orchestrator.session.completed", T0 + timedelta(minutes=1), [payload]),
        _final(payload, T0 + timedelta(minutes=1)),
    )
    manager = JobManager(store)
    original_from_db_row = BaseEvent.from_db_row
    replay_kwargs: list[dict[str, Any]] = []
    original_replay = store.replay

    async def spy_replay(*args: Any, **kwargs: Any) -> list[BaseEvent]:
        replay_kwargs.append(kwargs)
        return await original_replay(*args, **kwargs)

    async def rows_deserialized(read: Callable[[], Awaitable[object]]) -> tuple[int, object]:
        with patch.object(
            BaseEvent, "from_db_row", side_effect=original_from_db_row
        ) as from_db_row:
            result = await read()
        return from_db_row.call_count, result

    with patch.object(store, "replay", side_effect=spy_replay):
        current_rows, current = await rows_deserialized(
            lambda: manager._read_authoritative_session_acceptance(_snapshot())
        )
    reference_rows, reference = await rows_deserialized(
        lambda: _reference_read_authoritative_session_acceptance(manager, _snapshot())
    )

    assert current == reference
    assert isinstance(current, _AuthoritativeSessionAcceptance)
    assert replay_kwargs == [{"event_types": LIFECYCLE_TYPES}]
    # start + terminal from the session replay, plus the one persisted final row.
    assert current_rows == 3
    assert reference_rows == 1_003
