"""Unit tests for the AC tree HUD MCP handler."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from ouroboros.events.base import BaseEvent
from ouroboros.mcp.tools.ac_tree_hud_handler import (
    ACTreeHUDHandler,
    _extract_tree_snapshot,
    render_ac_tree_hud_markdown,
)
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


class TestRenderACTreeHUDMarkdown:
    """Render-only tests for the compact markdown HUD."""

    def test_renders_depth_1_tree_with_expected_icons_and_prefixes(self) -> None:
        """Depth-1 trees render as a compact list under the AC root."""
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_depth1",
            execution_id="exec_depth1",
            session_status="running",
            progress_data={
                "current_phase": "deliver",
                "completed_count": 1,
                "total_count": 3,
                "current_ac_index": 2,
                "activity": "executing",
                "activity_detail": "Read source files",
                "elapsed_display": "1m 05s",
                "messages_count": 8,
                "tool_calls_count": 3,
                "estimated_cost_usd": 0.04,
                "acceptance_criteria": [
                    {"index": 1, "content": "First criterion", "status": "completed"},
                    {"index": 2, "content": "Second criterion", "status": "in_progress"},
                    {"index": 3, "content": "Third criterion", "status": "pending"},
                ],
                "last_update": {
                    "tool_name": "Read",
                    "tool_input": {
                        "file_path": "src/ouroboros/mcp/tools/ac_tree_hud_handler.py",
                    },
                },
            },
        )

        assert "Session: sess_depth1" in markdown
        assert "Execution: exec_depth1" in markdown
        assert "Status: running" in markdown
        assert "Phase: deliver" in markdown
        assert "Progress: 1/3 AC complete" in markdown
        assert "Activity: executing | Read source files" in markdown
        assert "Metrics: elapsed 1m 05s · 8 msgs · 3 tools · $0.04" in markdown
        assert "◇ Acceptance Criteria" in markdown
        assert "├─ ● AC 1: First criterion" in markdown
        assert (
            "├─ ◐ AC 2: Second criterion  [Read src/ouroboros/mcp/tools/ac_tree_hud_handler.py]"
        ) in markdown
        assert "└─ ○ AC 3: Third criterion" in markdown

    def test_renders_all_top_level_acs_when_tree_has_12_or_fewer_nodes(self) -> None:
        """Small trees should render every top-level AC without ellipsis."""
        acceptance_criteria = [
            {
                "index": index,
                "content": f"Criterion {index}",
                "status": "in_progress" if index == 6 else "pending",
            }
            for index in range(1, 13)
        ]

        markdown = render_ac_tree_hud_markdown(
            session_id="sess_small_tree",
            execution_id="exec_small_tree",
            session_status="running",
            progress_data={
                "current_phase": "deliver",
                "completed_count": 0,
                "total_count": 12,
                "current_ac_index": 6,
                "acceptance_criteria": acceptance_criteria,
            },
        )

        assert "◇ Acceptance Criteria" in markdown
        assert "... (+" not in markdown
        for index in range(1, 12):
            assert f"├─ {'◐' if index == 6 else '○'} AC {index}: Criterion {index}" in markdown
        assert "└─ ○ AC 12: Criterion 12" in markdown

    def test_renders_normalized_current_tool_activity_from_node_payload(self) -> None:
        """Node-level current-tool-activity payloads should render as a compact summary."""
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_tool_activity_node",
            execution_id="exec_tool_activity_node",
            session_status="running",
            progress_data={
                "completed_count": 0,
                "total_count": 1,
                "current_ac_index": 1,
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Normalize tool activity",
                        "status": "executing",
                        "current_tool_activity": {
                            "tool_name": "Edit",
                            "tool_input": {
                                "file_path": "src/ouroboros/mcp/tools/ac_tree_hud_handler.py",
                            },
                        },
                    }
                ],
            },
        )

        assert (
            "└─ ◐ AC 1: Normalize tool activity  "
            "[Edit src/ouroboros/mcp/tools/ac_tree_hud_handler.py]"
        ) in markdown

    def test_renders_compact_tool_activity_when_last_update_payload_is_malformed(self) -> None:
        """Malformed last_update fields should degrade to a compact tool-only summary."""
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_tool_activity_last_update",
            execution_id="exec_tool_activity_last_update",
            session_status="running",
            progress_data={
                "completed_count": 0,
                "total_count": 1,
                "current_ac_index": 1,
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Fallback malformed tool payload",
                        "status": "executing",
                    }
                ],
                "last_update": {
                    "tool_name": "Read",
                    "tool_input": ["unexpected", "shape"],
                },
            },
        )

        assert "└─ ◐ AC 1: Fallback malformed tool payload  [Read]" in markdown

    def test_extract_tree_snapshot_attaches_tool_activity_only_to_executing_ac_nodes(self) -> None:
        """Synthesized AC trees should keep tool activity off non-executing nodes."""
        snapshot = _extract_tree_snapshot(
            {
                "current_ac_index": 2,
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Completed criterion",
                        "status": "completed",
                        "current_tool_activity": {
                            "tool_name": "Read",
                            "tool_input": {"file_path": "src/completed.py"},
                        },
                    },
                    {
                        "index": 2,
                        "content": "Executing criterion",
                        "status": "executing",
                        "current_tool_activity": {
                            "tool_name": "Edit",
                            "tool_input": {"file_path": "src/executing.py"},
                        },
                    },
                ],
            }
        )

        completed_node = snapshot["nodes"]["ac_1"]
        executing_node = snapshot["nodes"]["ac_2"]

        assert "tool_activity" not in completed_node
        assert "tool_activity_summary" not in completed_node
        assert executing_node["tool_activity_summary"] == "Edit src/executing.py"

    def test_extract_tree_snapshot_preserves_non_executing_explicit_nodes_without_tool_summary(
        self,
    ) -> None:
        """Explicit tree payloads should only gain normalized tool activity on executing nodes."""
        snapshot = _extract_tree_snapshot(
            {
                "ac_tree": {
                    "root_id": "root",
                    "nodes": {
                        "root": {
                            "id": "root",
                            "content": "Acceptance Criteria",
                            "status": "executing",
                            "children_ids": ["ac_1", "ac_2"],
                        },
                        "ac_1": {
                            "id": "ac_1",
                            "content": "Pending criterion",
                            "status": "pending",
                            "children_ids": [],
                            "current_tool_activity": {
                                "tool_name": "Read",
                                "tool_input": {"file_path": "src/pending.py"},
                            },
                        },
                        "ac_2": {
                            "id": "ac_2",
                            "content": "Executing criterion",
                            "status": "executing",
                            "children_ids": [],
                            "current_tool_activity": {
                                "tool_name": "Edit",
                                "tool_input": {"file_path": "src/executing.py"},
                            },
                        },
                    },
                }
            }
        )

        pending_node = snapshot["nodes"]["ac_1"]
        executing_node = snapshot["nodes"]["ac_2"]

        assert "tool_activity" not in pending_node
        assert "tool_activity_summary" not in pending_node
        assert executing_node["tool_activity_summary"] == "Edit src/executing.py"

    def test_extract_tree_snapshot_preserves_first_class_fallback_activity_state_on_nested_ac(
        self,
    ) -> None:
        """Synthesized nested AC nodes should keep precomputed fallback activity states."""
        snapshot = _extract_tree_snapshot(
            {
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Parent criterion",
                        "status": "executing",
                        "children_ids": ["sub_ac_1"],
                    },
                    {
                        "id": "sub_ac_1",
                        "parent_id": "ac_1",
                        "depth": 2,
                        "content": "Nested executing child",
                        "status": "executing",
                        "tool_activity_state": "unavailable",
                    },
                ],
            }
        )

        nested_node = snapshot["nodes"]["sub_ac_1"]

        assert nested_node["tool_activity_state"] == "unavailable"
        assert nested_node["tool_activity"]["state"] == "unavailable"
        assert nested_node["tool_activity_summary"] is None

    def test_extract_tree_snapshot_omits_tool_activity_when_executing_node_has_no_payload(
        self,
    ) -> None:
        """Executing nodes without tool activity should get a stable missing-state payload."""
        snapshot = _extract_tree_snapshot(
            {
                "current_ac_index": 1,
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Executing criterion without activity",
                        "status": "executing",
                    }
                ],
            }
        )

        executing_node = snapshot["nodes"]["ac_1"]

        assert executing_node["status"] == "executing"
        assert executing_node["tool_activity_state"] == "missing"
        assert executing_node["tool_activity"]["state"] == "missing"
        assert executing_node["tool_activity_summary"] is None

    def test_extract_tree_snapshot_gracefully_handles_invalid_tool_input_on_executing_node(
        self,
    ) -> None:
        """Malformed tool_input payloads should degrade to compact tool-only summaries."""
        snapshot = _extract_tree_snapshot(
            {
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
                            "content": "Executing criterion with invalid tool input",
                            "status": "executing",
                            "children_ids": [],
                            "current_tool_activity": {
                                "tool_name": "Read",
                                "tool_input": ["unexpected", "shape"],
                            },
                        },
                    },
                }
            }
        )

        executing_node = snapshot["nodes"]["ac_1"]

        assert executing_node["tool_activity_summary"] == "Read"
        assert executing_node["tool_activity"]["path_hint"] == ""

    def test_extract_tree_snapshot_marks_empty_activity_payload_unavailable(self) -> None:
        """Empty current-tool-activity payloads should normalize to unavailable, not throw."""
        snapshot = _extract_tree_snapshot(
            {
                "current_ac_index": 1,
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Executing criterion with unavailable activity",
                        "status": "executing",
                        "current_tool_activity": {},
                    }
                ],
            }
        )

        executing_node = snapshot["nodes"]["ac_1"]

        assert executing_node["tool_activity_state"] == "unavailable"
        assert executing_node["tool_activity"]["state"] == "unavailable"
        assert executing_node["tool_activity_summary"] is None

    def test_render_suppresses_stale_last_update_tool_activity(self) -> None:
        """Tool-result last_update payloads should not render as active inline activity."""
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_stale_tool_activity",
            execution_id="exec_stale_tool_activity",
            session_status="running",
            progress_data={
                "completed_count": 0,
                "total_count": 1,
                "current_ac_index": 1,
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Executing criterion with stale last update",
                        "status": "executing",
                    }
                ],
                "last_update": {
                    "message_type": "tool_result",
                    "tool_name": "Read",
                    "tool_input": {"file_path": "src/stale.py"},
                    "tool_result": {"content": "done"},
                },
            },
        )

        assert "└─ ◐ AC 1: Executing criterion with stale last update" in markdown
        assert "[Read src/stale.py]" not in markdown

    def test_render_uses_compact_working_fallback_when_executing_activity_is_missing(self) -> None:
        """Executing rows should still show compact inline text without live tool detail."""
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_missing_tool_activity",
            execution_id="exec_missing_tool_activity",
            session_status="running",
            progress_data={
                "completed_count": 0,
                "total_count": 2,
                "current_ac_index": 1,
                "acceptance_criteria": [
                    {
                        "index": 1,
                        "content": "Executing criterion without activity payload",
                        "status": "executing",
                    },
                    {
                        "index": 2,
                        "content": "Executing criterion with unavailable activity payload",
                        "status": "executing",
                        "current_tool_activity": {},
                    },
                ],
            },
        )

        assert ("├─ ◐ AC 1: Executing criterion without activity payload  [working]") in markdown
        assert (
            "└─ ◐ AC 2: Executing criterion with unavailable activity payload  [working]"
        ) in markdown

    def test_render_preserves_first_class_fallback_activity_state_through_nested_tree_pipeline(
        self,
    ) -> None:
        """Nested explicit-tree nodes should render fallback activity states without live payloads."""
        markdown = render_ac_tree_hud_markdown(
            session_id="sess_nested_fallback",
            execution_id="exec_nested_fallback",
            session_status="running",
            progress_data={
                "completed_count": 0,
                "total_count": 1,
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
                            "children_ids": ["sub_ac_1"],
                        },
                        "sub_ac_1": {
                            "id": "sub_ac_1",
                            "content": "Nested child waiting on tool payload",
                            "status": "executing",
                            "tool_activity_state": "unavailable",
                            "children_ids": [],
                        },
                    },
                },
            },
        )

        assert "└─ ◐ Parent criterion  [working]" in markdown
        assert "   └─ ◐ Nested child waiting on tool payload  [working]" in markdown


class TestACTreeHUDHandler:
    """Integration tests for ACTreeHUDHandler against EventStore data."""

    async def test_handle_renders_depth_1_tree_from_workflow_progress_event(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """The handler renders a depth-1 AC tree from the latest progress event."""
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_depth1",
                data={
                    "execution_id": "exec_depth1",
                    "seed_id": "seed_depth1",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_depth1",
                data={
                    "execution_id": "exec_depth1",
                    "current_phase": "deliver",
                    "completed_count": 1,
                    "total_count": 3,
                    "current_ac_index": 2,
                    "activity": "executing",
                    "activity_detail": "Read source files",
                    "elapsed_display": "1m 05s",
                    "messages_count": 8,
                    "tool_calls_count": 3,
                    "estimated_cost_usd": 0.04,
                    "acceptance_criteria": [
                        {"index": 1, "content": "First criterion", "status": "completed"},
                        {"index": 2, "content": "Second criterion", "status": "in_progress"},
                        {"index": 3, "content": "Third criterion", "status": "pending"},
                    ],
                    "last_update": {
                        "tool_name": "Read",
                        "tool_input": {
                            "file_path": "src/ouroboros/mcp/tools/ac_tree_hud_handler.py",
                        },
                    },
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)
        result = await handler.handle({"session_id": "sess_depth1", "cursor": 0})

        assert result.is_ok
        tool_result = result.value
        assert tool_result.meta["session_id"] == "sess_depth1"
        assert tool_result.meta["execution_id"] == "exec_depth1"
        assert tool_result.meta["changed"] is True
        assert tool_result.meta["cursor"] > 0
        assert "◇ Acceptance Criteria" in tool_result.text_content
        assert "├─ ● AC 1: First criterion" in tool_result.text_content
        assert "└─ ○ AC 3: Third criterion" in tool_result.text_content

    async def test_handle_renders_explicit_tree_with_fallback_activity_state(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Explicit-tree fallback activity states should still produce a compact snapshot."""
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_explicit_fallback",
                data={
                    "execution_id": "exec_explicit_fallback",
                    "seed_id": "seed_explicit_fallback",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_explicit_fallback",
                data={
                    "execution_id": "exec_explicit_fallback",
                    "current_phase": "deliver",
                    "completed_count": 0,
                    "total_count": 1,
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
                                "index": 1,
                                "content": "Fallback-only criterion",
                                "status": "executing",
                                "tool_activity_state": "unavailable",
                                "children_ids": [],
                            },
                        },
                    },
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)
        result = await handler.handle({"session_id": "sess_explicit_fallback", "cursor": 0})

        assert result.is_ok
        tool_result = result.value
        assert tool_result.meta["session_id"] == "sess_explicit_fallback"
        assert tool_result.meta["execution_id"] == "exec_explicit_fallback"
        assert tool_result.meta["changed"] is True
        assert tool_result.meta["cursor"] > 0
        assert "◇ Acceptance Criteria" in tool_result.text_content
        assert "Progress: 0/1 AC complete" in tool_result.text_content
        assert "└─ ◐ AC 1: Fallback-only criterion  [working]" in tool_result.text_content

    async def test_handle_returns_delta_one_liner_when_only_non_progress_events_are_new(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """A newer cursor with no new progress event returns the compact unchanged line."""
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_delta",
                data={
                    "execution_id": "exec_delta",
                    "seed_id": "seed_delta",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_delta",
                data={
                    "execution_id": "exec_delta",
                    "current_phase": "deliver",
                    "completed_count": 0,
                    "total_count": 1,
                    "current_ac_index": 1,
                    "acceptance_criteria": [
                        {"index": 1, "content": "Track delta mode", "status": "in_progress"}
                    ],
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)
        initial_result = await handler.handle({"session_id": "sess_delta", "cursor": 0})

        assert initial_result.is_ok
        initial_cursor = initial_result.value.meta["cursor"]

        await memory_event_store.append(
            BaseEvent(
                type="execution.note.recorded",
                aggregate_type="execution",
                aggregate_id="exec_delta",
                data={"message": "non-progress update after initial cursor"},
            )
        )

        delta_result = await handler.handle({"session_id": "sess_delta", "cursor": initial_cursor})

        assert delta_result.is_ok
        tool_result = delta_result.value
        assert tool_result.text_content == f"No AC tree change since cursor {initial_cursor}."
        assert tool_result.meta["session_id"] == "sess_delta"
        assert tool_result.meta["execution_id"] == "exec_delta"
        assert tool_result.meta["changed"] is False
        assert tool_result.meta["cursor"] > initial_cursor

    async def test_handle_returns_delta_one_liner_when_no_events_arrive_after_cursor(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """An unchanged cursor returns the same compact delta line without a full rerender."""
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_idle",
                data={
                    "execution_id": "exec_idle",
                    "seed_id": "seed_idle",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_idle",
                data={
                    "execution_id": "exec_idle",
                    "current_phase": "deliver",
                    "completed_count": 1,
                    "total_count": 1,
                    "current_ac_index": 1,
                    "acceptance_criteria": [
                        {"index": 1, "content": "Stay unchanged", "status": "completed"}
                    ],
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)
        initial_result = await handler.handle({"session_id": "sess_idle", "cursor": 0})

        assert initial_result.is_ok
        initial_cursor = initial_result.value.meta["cursor"]

        delta_result = await handler.handle({"session_id": "sess_idle", "cursor": initial_cursor})

        assert delta_result.is_ok
        tool_result = delta_result.value
        assert tool_result.text_content == f"No AC tree change since cursor {initial_cursor}."
        assert tool_result.meta["session_id"] == "sess_idle"
        assert tool_result.meta["execution_id"] == "exec_idle"
        assert tool_result.meta["changed"] is False
        assert tool_result.meta["cursor"] == initial_cursor

    async def test_handle_returns_delta_one_liner_when_new_progress_repeats_fallback_snapshot(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Repeated fallback-only progress snapshots should collapse to the unchanged delta line."""
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_fallback_delta",
                data={
                    "execution_id": "exec_fallback_delta",
                    "seed_id": "seed_fallback_delta",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_fallback_delta",
                data={
                    "execution_id": "exec_fallback_delta",
                    "current_phase": "deliver",
                    "completed_count": 0,
                    "total_count": 1,
                    "current_ac_index": 1,
                    "acceptance_criteria": [
                        {
                            "index": 1,
                            "content": "Fallback activity criterion",
                            "status": "executing",
                        }
                    ],
                },
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)
        initial_result = await handler.handle({"session_id": "sess_fallback_delta", "cursor": 0})

        assert initial_result.is_ok
        initial_cursor = initial_result.value.meta["cursor"]

        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_fallback_delta",
                data={
                    "execution_id": "exec_fallback_delta",
                    "current_phase": "deliver",
                    "completed_count": 0,
                    "total_count": 1,
                    "current_ac_index": 1,
                    "acceptance_criteria": [
                        {
                            "index": 1,
                            "content": "Fallback activity criterion",
                            "status": "executing",
                        }
                    ],
                },
            )
        )

        delta_result = await handler.handle(
            {"session_id": "sess_fallback_delta", "cursor": initial_cursor}
        )

        assert delta_result.is_ok
        tool_result = delta_result.value
        assert tool_result.text_content == f"No AC tree change since cursor {initial_cursor}."
        assert tool_result.meta["session_id"] == "sess_fallback_delta"
        assert tool_result.meta["execution_id"] == "exec_fallback_delta"
        assert tool_result.meta["changed"] is False
        assert tool_result.meta["cursor"] > initial_cursor

    async def test_handle_returns_delta_one_liner_when_explicit_fallback_tree_repeats(
        self,
        memory_event_store: EventStore,
    ) -> None:
        """Repeated explicit-tree fallback states should not force a full rerender."""
        await memory_event_store.append(
            BaseEvent(
                type="orchestrator.session.started",
                aggregate_type="session",
                aggregate_id="sess_explicit_fallback_delta",
                data={
                    "execution_id": "exec_explicit_fallback_delta",
                    "seed_id": "seed_explicit_fallback_delta",
                    "start_time": "2026-04-05T12:00:00+00:00",
                },
            )
        )
        initial_progress = {
            "execution_id": "exec_explicit_fallback_delta",
            "current_phase": "deliver",
            "completed_count": 0,
            "total_count": 1,
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
                        "index": 1,
                        "content": "Fallback-only criterion",
                        "status": "executing",
                        "tool_activity_state": "unavailable",
                        "children_ids": [],
                    },
                },
            },
        }
        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_explicit_fallback_delta",
                data=initial_progress,
            )
        )

        handler = ACTreeHUDHandler(event_store=memory_event_store)
        initial_result = await handler.handle(
            {"session_id": "sess_explicit_fallback_delta", "cursor": 0}
        )

        assert initial_result.is_ok
        initial_cursor = initial_result.value.meta["cursor"]

        await memory_event_store.append(
            BaseEvent(
                type="workflow.progress.updated",
                aggregate_type="execution",
                aggregate_id="exec_explicit_fallback_delta",
                data=initial_progress,
            )
        )

        delta_result = await handler.handle(
            {"session_id": "sess_explicit_fallback_delta", "cursor": initial_cursor}
        )

        assert delta_result.is_ok
        tool_result = delta_result.value
        assert tool_result.text_content == f"No AC tree change since cursor {initial_cursor}."
        assert tool_result.meta["session_id"] == "sess_explicit_fallback_delta"
        assert tool_result.meta["execution_id"] == "exec_explicit_fallback_delta"
        assert tool_result.meta["changed"] is False
        assert tool_result.meta["cursor"] > initial_cursor
