"""Footer-focused tests for the AC tree HUD markdown output."""

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


def test_render_ac_tree_hud_markdown_omits_zero_or_missing_footer_metrics() -> None:
    """Footer should only include best-effort metrics that are present and non-zero."""
    markdown = render_ac_tree_hud_markdown(
        session_id="sess_footer_render",
        execution_id="exec_footer_render",
        session_status="running",
        progress_data={
            "completed_count": 0,
            "total_count": 1,
            "acceptance_criteria": [
                {"index": 1, "content": "Criterion", "status": "pending"},
            ],
            "elapsed_display": "42s",
            "messages_count": 0,
            "tool_calls_count": None,
            "estimated_cost_usd": 0.0,
        },
    )

    assert "Metrics: elapsed 42s" in markdown
    assert "msgs" not in markdown
    assert "tools" not in markdown
    assert "$" not in markdown


@pytest.mark.asyncio
async def test_handle_renders_footer_metrics_from_progress_event_payload(
    memory_event_store: EventStore,
) -> None:
    """Handler should surface compact footer metrics from workflow.progress.updated payloads."""
    await memory_event_store.append(
        BaseEvent(
            type="orchestrator.session.started",
            aggregate_type="session",
            aggregate_id="sess_footer_event",
            data={
                "execution_id": "exec_footer_event",
                "seed_id": "seed_footer_event",
                "start_time": "2026-04-05T12:00:00+00:00",
            },
        )
    )
    await memory_event_store.append(
        BaseEvent(
            type="workflow.progress.updated",
            aggregate_type="execution",
            aggregate_id="exec_footer_event",
            data={
                "execution_id": "exec_footer_event",
                "completed_count": 1,
                "total_count": 2,
                "acceptance_criteria": [
                    {"index": 1, "content": "First criterion", "status": "completed"},
                    {"index": 2, "content": "Second criterion", "status": "in_progress"},
                ],
                "elapsed_display": "1m 05s",
                "messages_count": 8,
                "tool_calls_count": 3,
                "estimated_cost_usd": 0.04,
            },
        )
    )

    handler = ACTreeHUDHandler(event_store=memory_event_store)
    result = await handler.handle({"session_id": "sess_footer_event", "cursor": 0})

    assert result.is_ok
    assert "Metrics: elapsed 1m 05s · 8 msgs · 3 tools · $0.04" in result.value.text_content
