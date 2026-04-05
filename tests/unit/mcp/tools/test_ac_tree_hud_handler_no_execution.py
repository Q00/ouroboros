"""Regression tests for graceful no-execution handling in the AC-tree HUD."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.ac_tree_hud_handler import ACTreeHUDHandler
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def memory_event_store() -> AsyncIterator[EventStore]:
    """Provide an initialized in-memory event store for HUD handler tests."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


async def test_handle_returns_graceful_warning_when_session_has_no_execution_link(
    memory_event_store: EventStore,
) -> None:
    """Sessions without an execution link should return compact markdown warnings."""
    await memory_event_store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_no_exec",
            data={
                "execution_id": "",
                "seed_id": "seed_no_exec",
                "start_time": "2026-04-05T12:00:00+00:00",
            },
        )
    )

    handler = ACTreeHUDHandler(event_store=memory_event_store)
    result = await handler.handle({"session_id": "sess_no_exec", "cursor": 23})

    assert result.is_ok
    tool_result = result.value
    assert tool_result.is_error is False
    assert tool_result.text_content == (
        "Session: sess_no_exec\nStatus: running\nWarning: no execution linked to this session yet."
    )
    assert tool_result.meta == {
        "session_id": "sess_no_exec",
        "execution_id": None,
        "status": "running",
        "cursor": 23,
        "changed": False,
        "warning": "no execution linked to this session yet.",
    }
