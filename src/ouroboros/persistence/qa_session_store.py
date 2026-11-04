"""Atomic QA completion publication in the shared session namespace."""

from __future__ import annotations

from sqlalchemy import select

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.picker_projection_updates import insert_event_with_picker_projection
from ouroboros.persistence.schema import events_table
from ouroboros.persistence.write_settlement import run_to_settlement

QA_EVALUATED = "qa.session.evaluated"
_IO_EVENTS = frozenset(
    {"tool.call.started", "tool.call.returned", "llm.call.requested", "llm.call.returned"}
)
_COMPATIBLE_EVENTS = _IO_EVENTS | {QA_EVALUATED}


class QASessionConflict(ValueError):
    """A QA ID is already used by a different kind of session."""


def qa_compatible_event(event: BaseEvent) -> bool:
    """I/O journal records do not claim session lifecycle ownership."""
    return event.type in _COMPATIBLE_EVENTS


async def append_qa_evaluation(store: EventStore, event: BaseEvent) -> None:
    """Check ownership and append in the same transaction as session starts.

    Session starts also use BEGIN IMMEDIATE and refuse any existing event in
    the session stream. An already-started session can therefore never be
    polluted by this QA event, even if the operations race.
    """
    engine = store._engine
    if engine is None:
        raise PersistenceError("EventStore not initialized", operation="append_qa_evaluation")
    if event.type != QA_EVALUATED or event.aggregate_type != "session":
        raise ValueError("invalid QA session event")

    async def insert() -> None:
        async with engine.connect() as conn:
            await conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                existing = await conn.execute(
                    select(events_table.c.id)
                    .where(
                        events_table.c.aggregate_type == "session",
                        events_table.c.aggregate_id == event.aggregate_id,
                        events_table.c.event_type.not_in(_COMPATIBLE_EVENTS),
                    )
                    .limit(1)
                )
                if existing.first() is not None:
                    raise QASessionConflict("session ID is already used by another session")
                await insert_event_with_picker_projection(
                    conn, event, store._picker_projection_ready
                )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    await run_to_settlement(
        insert(),
        registry=store._settling_writes,
        refuse_when=lambda: store._closing,
        operation="append_qa_evaluation",
    )
