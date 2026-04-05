"""Completed-session regression tests for the AC-tree HUD handler."""

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


@pytest.mark.asyncio
async def test_handle_renders_final_completed_snapshot_after_session_completion(
    memory_event_store: EventStore,
) -> None:
    """A terminal session event after the cursor should rerender the final tree."""
    await memory_event_store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_completed_hud",
            data={
                "execution_id": "exec_completed_hud",
                "seed_id": "seed_completed_hud",
                "start_time": "2026-04-05T12:00:00+00:00",
            },
        )
    )
    await memory_event_store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="exec_completed_hud",
            data={
                "execution_id": "exec_completed_hud",
                "current_phase": "deliver",
                "completed_count": 2,
                "total_count": 2,
                "acceptance_criteria": [
                    {"index": 1, "content": "First criterion", "status": "completed"},
                    {"index": 2, "content": "Second criterion", "status": "completed"},
                ],
                "elapsed_display": "2m 10s",
                "messages_count": 11,
                "tool_calls_count": 4,
                "estimated_cost_usd": 0.05,
            },
        )
    )

    handler = ACTreeHUDHandler(event_store=memory_event_store)
    initial_result = await handler.handle({"session_id": "sess_completed_hud", "cursor": 0})

    assert initial_result.is_ok
    initial_cursor = initial_result.value.meta["cursor"]

    await memory_event_store.append(
        BaseEvent(
            type="orchestrator.session.completed",
            aggregate_type="session",
            aggregate_id="sess_completed_hud",
            data={"summary": "all acceptance criteria completed"},
        )
    )

    completed_result = await handler.handle(
        {"session_id": "sess_completed_hud", "cursor": initial_cursor}
    )

    assert completed_result.is_ok
    tool_result = completed_result.value
    assert tool_result.meta["session_id"] == "sess_completed_hud"
    assert tool_result.meta["execution_id"] == "exec_completed_hud"
    assert tool_result.meta["status"] == "completed"
    assert tool_result.meta["changed"] is True
    assert tool_result.meta["cursor"] > initial_cursor
    assert tool_result.text_content != f"No AC tree change since cursor {initial_cursor}."
    assert "Status: completed" in tool_result.text_content
    assert "Progress: 2/2 AC complete" in tool_result.text_content
    assert "Metrics: elapsed 2m 10s · 11 msgs · 4 tools · $0.05" in tool_result.text_content
    assert "├─ ● AC 1: First criterion" in tool_result.text_content
    assert "└─ ● AC 2: Second criterion" in tool_result.text_content
