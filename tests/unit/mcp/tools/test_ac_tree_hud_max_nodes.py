"""Tests for AC tree HUD max-node capping and override behavior."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.ac_tree_hud_handler import ACTreeHUDHandler, render_ac_tree_hud_markdown
from ouroboros.persistence.event_store import EventStore


@pytest.fixture
async def memory_event_store() -> AsyncIterator[EventStore]:
    """Provide an initialized in-memory event store and dispose it after each test."""
    store = EventStore("sqlite+aiosqlite:///:memory:")
    await store.initialize()
    try:
        yield store
    finally:
        await store.close()


def test_render_ac_tree_hud_markdown_caps_nested_output_by_max_nodes() -> None:
    """Large nested trees should stop rendering visible nodes at the requested limit."""
    child_ids = [f"sub_{index}" for index in range(1, 8)]
    progress_data = {
        "completed_count": 0,
        "total_count": 8,
        "ac_tree": {
            "root_id": "root",
            "nodes": {
                "root": {
                    "id": "root",
                    "content": "Acceptance Criteria",
                    "status": "executing",
                    "children_ids": ["ac_1"],
                },
                "ac_1": {
                    "id": "ac_1",
                    "content": "Parent criterion",
                    "status": "executing",
                    "children_ids": child_ids,
                },
                **{
                    child_id: {
                        "id": child_id,
                        "content": f"Nested task {index}",
                        "status": "pending",
                        "children_ids": [],
                    }
                    for index, child_id in enumerate(child_ids, start=1)
                },
            },
        },
    }

    markdown = render_ac_tree_hud_markdown(
        session_id="sess_cap",
        execution_id="exec_cap",
        session_status="running",
        progress_data=progress_data,
        max_nodes=4,
    )

    visible_node_lines = [
        line
        for line in markdown.splitlines()
        if line.startswith(("├─ ", "└─ ", "│  ├─ ", "│  └─ ", "   ├─ ", "   └─ "))
        and "... (+" not in line
    ]

    assert len(visible_node_lines) == 4
    assert "└─ ◐ Parent criterion" in markdown
    assert "Nested task 3" in markdown
    assert "Nested task 4" not in markdown


@pytest.mark.asyncio
async def test_handle_respects_max_nodes_override_for_large_top_level_tree(
    memory_event_store: EventStore,
) -> None:
    """Caller-provided max_nodes should keep the active AC visible in compact mode."""
    await memory_event_store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_max_nodes",
            data={
                "execution_id": "exec_max_nodes",
                "seed_id": "seed_max_nodes",
                "start_time": "2026-04-05T12:00:00+00:00",
            },
        )
    )
    await memory_event_store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="exec_max_nodes",
            data={
                "execution_id": "exec_max_nodes",
                "current_phase": "deliver",
                "completed_count": 0,
                "total_count": 20,
                "current_ac_index": 10,
                "acceptance_criteria": [
                    {
                        "index": index,
                        "content": f"Criterion {index}",
                        "status": "in_progress" if index == 10 else "pending",
                    }
                    for index in range(1, 21)
                ],
            },
        )
    )

    handler = ACTreeHUDHandler(event_store=memory_event_store)
    result = await handler.handle({"session_id": "sess_max_nodes", "cursor": 0, "max_nodes": 3})

    assert result.is_ok
    markdown = result.value.text_content
    assert "├─ ○ AC 1: Criterion 1" in markdown
    assert "├─ ◐ AC 10: Criterion 10" in markdown
    assert "└─ ○ AC 20: Criterion 20" in markdown
    assert "AC 8: Criterion 8" not in markdown
    assert "AC 9: Criterion 9" not in markdown
    assert "AC 11: Criterion 11" not in markdown
