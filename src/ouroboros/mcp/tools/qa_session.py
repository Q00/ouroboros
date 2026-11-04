"""Durable, content-minimal records of completed in-process QA evaluations."""

from __future__ import annotations

from typing import Any

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.qa_session_store import (
    QA_EVALUATED,
    QASessionConflict,
    append_qa_evaluation,
)


async def read_session_events(store: EventStore, session_id: str) -> list[BaseEvent]:
    """Read one bounded, rowid-ordered snapshot, failing closed on overflow."""
    boundary = await store.get_current_rowid()
    cursor = 0
    events: list[BaseEvent] = []
    while cursor < boundary:
        page, next_cursor = await store.get_events_after(
            "session", session_id, cursor, limit=512, max_row_id=boundary
        )
        if not page:
            break
        events.extend(page)
        if len(events) > 10_000 or next_cursor <= cursor:
            raise QASessionConflict("session history cannot be safely reconstructed")
        cursor = next_cursor
    return events


async def record_qa_evaluation(
    event_store: EventStore | None, session_id: str, meta: dict[str, Any]
) -> None:
    """Append one completed evaluation before the tool reports success.

    Only the public score summary is retained, not artifacts, prompts or model
    text.  Session status checks the same stream for collisions, including a
    concurrent claim by another session type.
    """
    owns_store = event_store is None
    store = event_store or EventStore()
    try:
        await store.initialize()
        await append_qa_evaluation(
            store,
            BaseEvent(
                type=QA_EVALUATED,
                aggregate_type="session",
                aggregate_id=session_id,
                data={
                    "iteration": meta["iteration"],
                    "score": meta["score"],
                    "verdict": meta["verdict"],
                    "passed": meta["passed"],
                    "pass_threshold": meta["pass_threshold"],
                },
            ),
        )
    finally:
        if owns_store:
            await store.close()
