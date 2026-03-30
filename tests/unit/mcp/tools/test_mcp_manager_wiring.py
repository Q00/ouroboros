"""Tests for mcp_manager wiring through the dependency injection chain.

Covers PR #264 review findings #4 and #5:
- create_ouroboros_server with mcp_bridge injects manager into ExecuteSeedHandler
- Factory functions propagate mcp_manager and mcp_tool_prefix
- get_ouroboros_tools passes params to ExecuteSeedHandler
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ouroboros.mcp.tools.definitions import (
    execute_seed_handler,
    get_ouroboros_tools,
    start_execute_seed_handler,
)
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler


class TestExecuteSeedHandlerFields:
    """Verify mcp_manager/mcp_tool_prefix fields on ExecuteSeedHandler."""

    def test_defaults_to_none(self):
        handler = ExecuteSeedHandler()
        assert handler.mcp_manager is None
        assert handler.mcp_tool_prefix == ""

    def test_accepts_mcp_manager(self):
        mock = MagicMock()
        handler = ExecuteSeedHandler(mcp_manager=mock, mcp_tool_prefix="pfx_")
        assert handler.mcp_manager is mock
        assert handler.mcp_tool_prefix == "pfx_"


class TestFactoryFunctions:
    """Verify factory functions propagate mcp_manager."""

    def test_execute_seed_handler_factory(self):
        mock = MagicMock()
        h = execute_seed_handler(mcp_manager=mock, mcp_tool_prefix="a_")
        assert h.mcp_manager is mock
        assert h.mcp_tool_prefix == "a_"

    def test_execute_seed_handler_factory_defaults(self):
        h = execute_seed_handler()
        assert h.mcp_manager is None
        assert h.mcp_tool_prefix == ""

    def test_start_execute_seed_handler_factory(self):
        mock = MagicMock()
        h = start_execute_seed_handler(mcp_manager=mock, mcp_tool_prefix="b_")
        assert h.execute_handler.mcp_manager is mock
        assert h.execute_handler.mcp_tool_prefix == "b_"


class TestGetOuroborosTools:
    """Verify get_ouroboros_tools propagates mcp_manager to handlers."""

    def test_with_mcp_manager(self):
        mock = MagicMock()
        tools = get_ouroboros_tools(mcp_manager=mock, mcp_tool_prefix="x_")
        exec_handler = tools[0]
        assert isinstance(exec_handler, ExecuteSeedHandler)
        assert exec_handler.mcp_manager is mock
        assert exec_handler.mcp_tool_prefix == "x_"

    def test_without_mcp_manager(self):
        tools = get_ouroboros_tools()
        exec_handler = tools[0]
        assert isinstance(exec_handler, ExecuteSeedHandler)
        assert exec_handler.mcp_manager is None
        assert exec_handler.mcp_tool_prefix == ""

    def test_tool_count_unchanged(self):
        """Adding mcp_manager params does not change the number of tools."""
        default_tools = get_ouroboros_tools()
        with_mgr = get_ouroboros_tools(mcp_manager=MagicMock())
        assert len(default_tools) == len(with_mgr)


class TestCompositionRoot:
    """Verify create_ouroboros_server wires bridge into handlers."""

    def test_bridge_manager_injected_into_execute_handler(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        mock_bridge = MagicMock()
        mock_bridge.manager = MagicMock(name="FakeMCPClientManager")
        mock_bridge.tool_prefix = "ext_"

        server = create_ouroboros_server(mcp_bridge=mock_bridge)

        # Find the ExecuteSeedHandler in registered tools
        exec_handler = None
        for handler in server._tool_handlers.values():
            if isinstance(handler, ExecuteSeedHandler):
                exec_handler = handler
                break

        assert exec_handler is not None, "ExecuteSeedHandler not found in server"
        assert exec_handler.mcp_manager is mock_bridge.manager
        assert exec_handler.mcp_tool_prefix == "ext_"

    def test_bridge_registered_as_owned_resource(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        mock_bridge = MagicMock()
        mock_bridge.manager = MagicMock()
        mock_bridge.tool_prefix = ""

        server = create_ouroboros_server(mcp_bridge=mock_bridge)
        assert mock_bridge in server._owned_resources

    def test_no_bridge_leaves_handler_with_none(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        server = create_ouroboros_server()

        exec_handler = None
        for handler in server._tool_handlers.values():
            if isinstance(handler, ExecuteSeedHandler):
                exec_handler = handler
                break

        assert exec_handler is not None
        assert exec_handler.mcp_manager is None
        assert exec_handler.mcp_tool_prefix == ""

    def test_no_bridge_not_in_owned_resources(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        server = create_ouroboros_server()
        bridge_resources = [
            r
            for r in server._owned_resources
            if hasattr(r, "tool_prefix") and hasattr(r, "manager")
        ]
        assert len(bridge_resources) == 0
