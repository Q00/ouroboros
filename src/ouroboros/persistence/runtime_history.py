"""Cursor-ordered read history shared by runtime projection consumers."""

from ouroboros.events.base import BaseEvent
from ouroboros.persistence.event_store import EventStore


async def read_runtime_history(
    store: EventStore,
    *,
    execution_id: str | None,
    session_id: str | None = None,
    after_cursor: int = 0,
    max_row_id: int | None = None,
) -> tuple[list[BaseEvent], int]:
    """Read finite cursor pages without changing the store's legacy ordering."""
    history: list[BaseEvent] = []
    cursor = after_cursor
    if max_row_id is None:
        max_row_id = await store.get_current_rowid()
    while True:
        if session_id is not None:
            page, next_cursor = await store.query_session_related_events_after(
                session_id=session_id,
                execution_id=execution_id,
                last_row_id=cursor,
                limit=1000,
                max_row_id=max_row_id,
                session_start_by_rowid=True,
            )
        elif execution_id:
            page, next_cursor = await store.get_events_after(
                "execution", execution_id, cursor, limit=1000, max_row_id=max_row_id
            )
        else:
            return history, cursor
        history.extend(
            event
            for event in page
            if not session_id
            or event.data.get("session_id") is None
            or event.data.get("session_id") == session_id
        )
        if next_cursor <= cursor or len(page) < 1000:
            return history, max(cursor, next_cursor)
        cursor = next_cursor
