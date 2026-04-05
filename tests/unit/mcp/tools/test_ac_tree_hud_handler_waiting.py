"""Regression tests for the AC-tree HUD waiting-state behavior."""

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


async def test_handle_returns_waiting_message_when_no_ac_tree_progress_exists(
    memory_event_store: EventStore,
) -> None:
    """Sessions without workflow progress should return a graceful waiting message."""
    await memory_event_store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_waiting",
            data={
                "execution_id": "exec_waiting",
                "seed_id": "seed_waiting",
                "start_time": "2026-04-05T12:00:00+00:00",
            },
        )
    )

    handler = ACTreeHUDHandler(event_store=memory_event_store)
    result = await handler.handle({"session_id": "sess_waiting", "cursor": 0})

    assert result.is_ok
    tool_result = result.value
    assert tool_result.is_error is False
    assert tool_result.text_content == (
        "Session: sess_waiting\n"
        "Execution: exec_waiting\n"
        "Status: running\n"
        "Warning: waiting for the first AC tree update."
    )
    assert tool_result.meta == {
        "session_id": "sess_waiting",
        "execution_id": "exec_waiting",
        "status": "running",
        "cursor": 0,
        "changed": False,
        "warning": "waiting for the first AC tree update.",
    }
