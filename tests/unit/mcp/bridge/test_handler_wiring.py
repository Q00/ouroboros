"""Tests for mcp_manager wiring through ExecuteSeedHandler."""

from __future__ import annotations

from unittest.mock import MagicMock

from ouroboros.mcp.tools.definitions import (
    execute_seed_handler,
    get_ouroboros_tools,
    start_execute_seed_handler,
)
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler


class TestExecuteSeedHandlerWiring:
    def test_handler_accepts_mcp_manager(self):
        mock_mgr = MagicMock()
        handler = ExecuteSeedHandler(mcp_manager=mock_mgr, mcp_tool_prefix="pfx_")
        assert handler.mcp_manager is mock_mgr
        assert handler.mcp_tool_prefix == "pfx_"

    def test_handler_defaults_none(self):
        handler = ExecuteSeedHandler()
        assert handler.mcp_manager is None
        assert handler.mcp_tool_prefix == ""

    def test_factory_passes_mcp_manager(self):
        mock_mgr = MagicMock()
        handler = execute_seed_handler(mcp_manager=mock_mgr, mcp_tool_prefix="t_")
        assert handler.mcp_manager is mock_mgr

    def test_start_handler_factory(self):
        mock_mgr = MagicMock()
        handler = start_execute_seed_handler(mcp_manager=mock_mgr, mcp_tool_prefix="s_")
        assert handler.execute_handler.mcp_manager is mock_mgr

    def test_get_ouroboros_tools_passes(self):
        mock_mgr = MagicMock()
        tools = get_ouroboros_tools(mcp_manager=mock_mgr, mcp_tool_prefix="x_")
        assert isinstance(tools[0], ExecuteSeedHandler)
        assert tools[0].mcp_manager is mock_mgr

    def test_get_ouroboros_tools_default(self):
        tools = get_ouroboros_tools()
        assert tools[0].mcp_manager is None


class TestCompositionRootWiring:
    def test_server_creation_with_bridge(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        mock_bridge = MagicMock()
        mock_bridge.manager = MagicMock()
        mock_bridge.tool_prefix = "bridge_"
        server = create_ouroboros_server(mcp_bridge=mock_bridge)
        assert mock_bridge in server._owned_resources

    def test_server_creation_without_bridge(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        server = create_ouroboros_server()
        bridge_resources = [r for r in server._owned_resources if hasattr(r, "tool_prefix")]
        assert len(bridge_resources) == 0
