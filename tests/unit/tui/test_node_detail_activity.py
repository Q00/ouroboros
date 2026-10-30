"""Selected node details reflect that node's existing and live activity."""

import pytest

from ouroboros.tui.app import OuroborosTUI
from ouroboros.tui.events import AgentThinkingUpdated, ToolCallCompleted
from ouroboros.tui.screens.dashboard_v3 import DashboardScreenV3, NodeDetailPanel, NodeSelected


@pytest.mark.asyncio
async def test_selecting_nodes_loads_and_clears_their_own_activity():
    app = OuroborosTUI()
    app.state.thinking["a"] = "Thinking about A"
    app.state.thinking["b"] = "Thinking about B"
    app.state.tool_history["a"] = [{"tool_name": "Read", "tool_detail": "Read A"}]
    app.state.tool_history["b"] = [{"tool_name": "Edit", "tool_detail": "Edit B"}]
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreenV3)
        panel = screen.query_one(NodeDetailPanel)
        screen.on_node_selected(NodeSelected({"id": "a", "content": "A"}))
        await pilot.pause()
        assert panel.thinking_text == "Thinking about A"
        assert panel.tool_history == app.state.tool_history["a"]
        screen.on_node_selected(NodeSelected({"id": "b", "content": "B"}))
        await pilot.pause()
        assert panel.thinking_text == "Thinking about B"
        assert panel.tool_history == app.state.tool_history["b"]
        screen.on_node_selected(NodeSelected({"id": "empty", "content": "No activity"}))
        await pilot.pause()
        assert panel.thinking_text == ""
        assert panel.tool_history == []


@pytest.mark.asyncio
async def test_selected_details_update_only_from_the_selected_nodes_live_activity():
    app = OuroborosTUI()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreenV3)
        panel = screen.query_one(NodeDetailPanel)
        screen.on_node_selected(NodeSelected({"id": "a", "content": "A"}))
        app.on_agent_thinking_updated(AgentThinkingUpdated("e1", "a", "New thought"))
        app.on_tool_call_completed(ToolCallCompleted("e1", "a", "Read", "Read A", 1, 0.2, True))
        await pilot.pause()
        assert panel.thinking_text == "New thought"
        assert panel.tool_history == app.state.tool_history["a"]
        assert len(panel.tool_history) == 1
        app.on_agent_thinking_updated(AgentThinkingUpdated("e1", "b", "Other thought"))
        app.on_tool_call_completed(ToolCallCompleted("e1", "b", "Edit", "Edit B", 1, 0.1, True))
        await pilot.pause()
        assert panel.thinking_text == "New thought"
        assert panel.tool_history == app.state.tool_history["a"]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 8, 12])
@pytest.mark.parametrize("size", [(180, 60), (80, 24)])
async def test_latest_tool_completion_is_visible_in_selected_details(count, size):
    app = OuroborosTUI()
    async with app.run_test(size=size) as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, DashboardScreenV3)
        screen.on_node_selected(NodeSelected({"id": "a", "content": "A"}))
        for index in range(1, count + 1):
            app.post_message(
                ToolCallCompleted("e1", "a", "Read", f"VISIBLE_TOOL_{index:02}", 1, 0.2, True)
            )
            await pilot.pause()
        await pilot.click("NodeDetailPanel")
        await pilot.press("end")
        await pilot.pause()
        screenshot = app.export_screenshot()
        assert f"VISIBLE_TOOL_{count:02}" in screenshot
        if count > 8:
            assert "VISIBLE_TOOL_01" not in screenshot
