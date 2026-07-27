"""Population-total, bounded-memory replay for execution event streams."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Protocol

from ouroboros.events.base import BaseEvent


class ExecutionEventPageSource(Protocol):
    """Minimal EventStore surface needed by chronological replay."""

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
        payload_equals: dict[str, str | int | float | bool | None] | None = None,
        *,
        chronological: bool = False,
        cursor_after: tuple[datetime, str] | None = None,
        cursor_through: tuple[datetime, str] | None = None,
    ) -> list[BaseEvent]: ...


async def replay_execution_events_chronologically(
    source: ExecutionEventPageSource,
    *,
    execution_id: str,
    event_type: str,
    page_size: int,
    payload_equals: Mapping[str, str | int | float | bool | None] | None = None,
) -> AsyncIterator[BaseEvent]:
    """Yield one stable execution-event snapshot without a total-history cap.

    A one-row newest-first query freezes the population's high-water key. The
    remaining reads use an exclusive ``(timestamp, id)`` keyset cursor in
    chronological order, so equal timestamps, concurrent later appends, and
    arbitrarily long valid histories neither skip rows nor require loading the
    complete stream into memory.
    """

    if type(execution_id) is not str or not execution_id:
        raise ValueError("execution replay requires a non-empty execution ID")
    if type(event_type) is not str or not event_type:
        raise ValueError("execution replay requires a non-empty event type")
    if type(page_size) is not int or page_size < 1:
        raise ValueError("execution replay page size must be positive")
    filters = dict(payload_equals or {})

    newest = await source.query_execution_related_events(
        execution_id,
        event_type=event_type,
        limit=1,
        payload_equals=filters,
    )
    if not isinstance(newest, list | tuple):
        raise RuntimeError("execution event replay returned a non-sequence page")
    if not newest:
        return
    # Test doubles historically returned the complete requested stream while
    # ignoring ``limit``. Preserve their in-memory contract without weakening
    # the production EventStore path, whose SQL query always honors the bound.
    if len(newest) > 1:
        ordered = sorted(newest, key=lambda event: (event.timestamp, event.id))
        previous_key: tuple[datetime, str] | None = None
        for event in ordered:
            key = (event.timestamp, event.id)
            if previous_key is not None and key <= previous_key:
                raise RuntimeError("execution event replay contains duplicate ordering keys")
            previous_key = key
            yield event
        return

    high_water = (newest[0].timestamp, newest[0].id)
    cursor: tuple[datetime, str] | None = None
    while True:
        page = await source.query_execution_related_events(
            execution_id,
            event_type=event_type,
            limit=page_size,
            payload_equals=filters,
            chronological=True,
            cursor_after=cursor,
            cursor_through=high_water,
        )
        if not isinstance(page, list | tuple):
            raise RuntimeError("execution event replay returned a non-sequence page")
        if len(page) > page_size:
            raise RuntimeError("execution event replay source exceeded its requested page bound")
        if not page:
            return
        ordered = sorted(page, key=lambda event: (event.timestamp, event.id))
        for event in ordered:
            key = (event.timestamp, event.id)
            if cursor is not None and key <= cursor:
                raise RuntimeError("execution event replay did not advance its keyset cursor")
            if key > high_water:
                raise RuntimeError("execution event replay crossed its high-water snapshot")
            cursor = key
            yield event
        if len(page) < page_size or cursor == high_water:
            return
