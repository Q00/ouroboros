"""Regression tests for changed AC-tree HUD renders after a cursor."""

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


async def test_handle_returns_full_render_when_progress_event_arrives_after_cursor(
    memory_event_store: EventStore,
) -> None:
    """A new workflow progress update after the cursor should trigger a full rerender."""
    await memory_event_store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_changed",
            data={
                "execution_id": "exec_changed",
                "seed_id": "seed_changed",
                "start_time": "2026-04-05T12:00:00+00:00",
            },
        )
    )
    await memory_event_store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="exec_changed",
            data={
                "execution_id": "exec_changed",
                "current_phase": "deliver",
                "completed_count": 0,
                "total_count": 2,
                "current_ac_index": 1,
                "acceptance_criteria": [
                    {"index": 1, "content": "First criterion", "status": "in_progress"},
                    {"index": 2, "content": "Second criterion", "status": "pending"},
                ],
            },
        )
    )

    handler = ACTreeHUDHandler(event_store=memory_event_store)
    initial_result = await handler.handle({"session_id": "sess_changed", "cursor": 0})

    assert initial_result.is_ok
    initial_cursor = initial_result.value.meta["cursor"]

    await memory_event_store.append(
        BaseEvent(
            type="execution.note.recorded",
            aggregate_type="execution",
            aggregate_id="exec_changed",
            data={"message": "intermediate note"},
        )
    )
    await memory_event_store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="exec_changed",
            data={
                "execution_id": "exec_changed",
                "current_phase": "deliver",
                "completed_count": 1,
                "total_count": 2,
                "current_ac_index": 2,
                "activity": "executing",
                "activity_detail": "Update second criterion",
                "acceptance_criteria": [
                    {"index": 1, "content": "First criterion", "status": "completed"},
                    {"index": 2, "content": "Second criterion", "status": "in_progress"},
                ],
                "last_update": {
                    "tool_name": "Edit",
                    "tool_input": {"file_path": "src/ouroboros/mcp/tools/ac_tree_hud_handler.py"},
                },
            },
        )
    )

    changed_result = await handler.handle({"session_id": "sess_changed", "cursor": initial_cursor})

    assert changed_result.is_ok
    tool_result = changed_result.value
    assert tool_result.meta["session_id"] == "sess_changed"
    assert tool_result.meta["execution_id"] == "exec_changed"
    assert tool_result.meta["changed"] is True
    assert tool_result.meta["cursor"] > initial_cursor
    assert tool_result.text_content != f"No AC tree change since cursor {initial_cursor}."
    assert "Progress: 1/2 AC complete" in tool_result.text_content
    assert "Activity: executing | Update second criterion" in tool_result.text_content
    assert "├─ ● AC 1: First criterion" in tool_result.text_content
    assert (
        "└─ ◐ AC 2: Second criterion  [Edit src/ouroboros/mcp/tools/ac_tree_hud_handler.py]"
    ) in tool_result.text_content
