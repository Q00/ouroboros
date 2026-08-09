"""Transactional ownership fence for durable Synapse SessionSignals."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ouroboros.core.session_signal import SessionSignal, SessionSignalMode, SessionSignalState
from ouroboros.core.session_signal_projection import project_session_signal
from ouroboros.events.base import BaseEvent
from ouroboros.events.session_signal import (
    create_session_signal_delivery_uncertain_event,
    create_session_signal_rejected_event,
)
from ouroboros.persistence.schema import events_table, session_signal_target_guards_table

_ACTIVE_EVENTS = frozenset(
    {"execution.session.started", "execution.session.resumed", "execution.session.recovered"}
)
_TERMINAL_EVENTS = frozenset(
    {
        "execution.session.completed",
        "execution.session.failed",
        "execution.session.cancelled",
    }
)


def _engine(event_store: object) -> AsyncEngine | None:
    engine = getattr(event_store, "_engine", None)
    return engine if isinstance(engine, AsyncEngine) else None


def _identity(event: BaseEvent) -> tuple[str, str, str] | None:
    values = (
        event.data.get("execution_id"),
        event.data.get("session_scope_id"),
        event.data.get("session_attempt_id"),
    )
    if not all(isinstance(value, str) and value.strip() for value in values):
        return None
    return tuple(str(value).strip() for value in values)  # type: ignore[return-value]


async def _begin(engine: AsyncEngine):
    conn = await engine.connect()
    await conn.exec_driver_sql("BEGIN IMMEDIATE")
    return conn


async def append_runtime_lifecycle(event_store: object, event: BaseEvent) -> bool:
    """Atomically publish lifecycle ownership and drain pending target signals.

    Returns False for lightweight test doubles so callers can retain their
    ordinary append contract.
    """
    identity = _identity(event)
    settle = getattr(event_store, "settle_transactional_write", None)
    declared_settle = getattr(type(event_store), "settle_transactional_write", None)
    if (
        not callable(declared_settle)
        or not callable(settle)
        or identity is None
        or event.type not in _ACTIVE_EVENTS | _TERMINAL_EVENTS
    ):
        return False
    await settle(
        lambda engine: _append_runtime_lifecycle(engine, event, identity),
        operation="append_runtime_lifecycle",
    )
    return True


async def runtime_attempt_is_active(
    event_store: object,
    *,
    identity: tuple[str, str, str],
) -> bool:
    """Project whether one exact durable runtime attempt still owns delivery."""
    return await runtime_attempt_guard_state(event_store, identity=identity) is True


async def runtime_attempt_guard_state(
    event_store: object,
    *,
    identity: tuple[str, str, str],
) -> bool | None:
    """Read an exact attempt guard without inventing authority when absent."""
    engine = _engine(event_store)
    if engine is None:
        return None
    async with engine.connect() as connection:
        active = await connection.scalar(
            select(session_signal_target_guards_table.c.active).where(
                session_signal_target_guards_table.c.execution_id == identity[0],
                session_signal_target_guards_table.c.session_scope_id == identity[1],
                session_signal_target_guards_table.c.session_attempt_id == identity[2],
            )
        )
    return active if isinstance(active, bool) else None


async def _append_runtime_lifecycle(
    engine: AsyncEngine,
    event: BaseEvent,
    identity: tuple[str, str, str],
) -> None:
    execution_id, scope_id, attempt_id = identity
    conn = await _begin(engine)
    try:
        guard_values = {
            "execution_id": execution_id,
            "session_scope_id": scope_id,
            "session_attempt_id": attempt_id,
            "active": event.type in _ACTIVE_EVENTS,
            "lifecycle_event_id": event.id,
            "updated_at": datetime.now(UTC),
        }
        statement = sqlite_insert(session_signal_target_guards_table).values(**guard_values)
        # A terminal exact-attempt guard is absorbing.  A delayed provider
        # callback may still append its immutable lifecycle telemetry, but it
        # must never reopen delivery authority after shutdown.
        update_where = (
            session_signal_target_guards_table.c.active.is_(True)
            if event.type in _ACTIVE_EVENTS
            else None
        )
        upsert = statement.on_conflict_do_update(
            index_elements=["execution_id", "session_scope_id", "session_attempt_id"],
            set_={
                "active": guard_values["active"],
                "lifecycle_event_id": event.id,
                "updated_at": guard_values["updated_at"],
            },
            where=update_where,
        )
        await conn.execute(upsert)
        await conn.execute(events_table.insert().values(**event.to_db_dict()))
        if event.type in _TERMINAL_EVENTS:
            for terminal_event in await _pending_terminal_events(conn, identity, event):
                await conn.execute(events_table.insert().values(**terminal_event.to_db_dict()))
        await conn.commit()
    except BaseException:
        if conn.in_transaction():
            await conn.rollback()
        raise
    finally:
        await conn.close()


async def _pending_terminal_events(conn, identity, lifecycle_event: BaseEvent) -> list[BaseEvent]:
    execution_id, scope_id, attempt_id = identity
    result = await conn.execute(
        select(events_table)
        .where(events_table.c.aggregate_type == "session_signal")
        .where(events_table.c.payload["expected_execution_id"].as_string() == execution_id)
        .where(events_table.c.payload["target_session_scope_id"].as_string() == scope_id)
        .where(events_table.c.payload["target_session_attempt_id"].as_string() == attempt_id)
        .order_by(events_table.c.timestamp, events_table.c.id)
    )
    grouped: dict[str, list[BaseEvent]] = defaultdict(list)
    for row in result.mappings().all():
        persisted = BaseEvent.from_db_row(dict(row))
        grouped[persisted.aggregate_id].append(persisted)

    terminal_events: list[BaseEvent] = []
    for signal_events in grouped.values():
        projection = project_session_signal(signal_events)
        if projection.state not in {SessionSignalState.QUEUED, SessionSignalState.DELIVERING}:
            continue
        requested = next(event for event in signal_events if event.type.endswith(".requested"))
        signal = SessionSignal.from_event_data(requested.data)
        effective_mode = projection.effective_mode or SessionSignalMode.AFTER_TURN
        if projection.state is SessionSignalState.DELIVERING:
            terminal_events.append(
                create_session_signal_delivery_uncertain_event(
                    signal,
                    effective_mode=effective_mode,
                    detail="The runtime ended after claiming delivery without a final acknowledgement.",
                    runtime_backend=lifecycle_event.data.get("runtime_backend"),
                    orchestrator_session_id=lifecycle_event.data.get("orchestrator_session_id"),
                )
            )
        else:
            terminal_events.append(
                create_session_signal_rejected_event(
                    signal,
                    rejection_code="target_ended_before_boundary",
                    detail="The runtime attempt ended before the queued signal reached its boundary.",
                    effective_mode=effective_mode,
                    runtime_backend=lifecycle_event.data.get("runtime_backend"),
                    orchestrator_session_id=lifecycle_event.data.get("orchestrator_session_id"),
                )
            )
    return terminal_events


async def admit_if_target_active(
    event_store: object,
    *,
    identity: tuple[str, str, str],
    accepted: BaseEvent,
    queued: BaseEvent,
    rejected: BaseEvent,
    locally_owned: bool = False,
) -> bool | None:
    """Queue under the ownership fence, or reject after target shutdown.

    A resolver/queue pair backed by the same live ``SessionSignalHub`` can own
    a target before a durable lifecycle guard exists.  That process-local
    ownership is admitted only while the guard row is absent; an explicit
    inactive guard always wins and prevents a stale hub from reopening it.
    """
    settle = getattr(event_store, "settle_transactional_write", None)
    declared_settle = getattr(type(event_store), "settle_transactional_write", None)
    if not callable(declared_settle) or not callable(settle):
        return None
    return await settle(
        lambda engine: _admit_if_target_active(
            engine, identity, accepted, queued, rejected, locally_owned=locally_owned
        ),
        operation="admit_session_signal_if_target_active",
    )


async def _admit_if_target_active(
    engine: AsyncEngine,
    identity: tuple[str, str, str],
    accepted: BaseEvent,
    queued: BaseEvent,
    rejected: BaseEvent,
    *,
    locally_owned: bool,
) -> bool:
    conn = await _begin(engine)
    try:
        active = await conn.scalar(
            select(session_signal_target_guards_table.c.active).where(
                session_signal_target_guards_table.c.execution_id == identity[0],
                session_signal_target_guards_table.c.session_scope_id == identity[1],
                session_signal_target_guards_table.c.session_attempt_id == identity[2],
            )
        )
        admitted = active is True or (active is None and locally_owned)
        events = (accepted, queued) if admitted else (rejected,)
        for event in events:
            await conn.execute(events_table.insert().values(**event.to_db_dict()))
        await conn.commit()
        return admitted
    except BaseException:
        if conn.in_transaction():
            await conn.rollback()
        raise
    finally:
        await conn.close()
