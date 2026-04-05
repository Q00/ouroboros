"""Regression tests for graceful invalid-session handling in the AC-tree HUD."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

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


async def test_handle_returns_graceful_warning_for_invalid_session_id(
    memory_event_store: EventStore,
) -> None:
    """Unknown session IDs should return compact markdown instead of an MCP error."""
    handler = ACTreeHUDHandler(event_store=memory_event_store)

    result = await handler.handle({"session_id": "sess_missing", "cursor": 17})

    assert result.is_ok
    tool_result = result.value
    assert tool_result.is_error is False
    assert tool_result.text_content == "Session: sess_missing\nWarning: session not found."
    assert tool_result.meta == {
        "session_id": "sess_missing",
        "execution_id": None,
        "status": None,
        "cursor": 17,
        "changed": False,
        "warning": "session not found.",
    }
