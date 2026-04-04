"""Tests for mcp_manager wiring via BridgeAwareMixin and inject_bridge.

Covers:
- BridgeAwareMixin fields default to None/""
- ExecuteSeedHandler inherits BridgeAwareMixin
- inject_bridge auto-discovers and injects into mixin handlers
- create_ouroboros_server loop-based bridge injection
"""

from __future__ import annotations

from unittest.mock import MagicMock

from ouroboros.mcp.tools.bridge_mixin import BridgeAwareMixin, inject_bridge
from ouroboros.mcp.tools.definitions import get_ouroboros_tools
from ouroboros.mcp.tools.execution_handlers import ExecuteSeedHandler


class TestBridgeAwareMixin:
    """Verify the mixin provides correct defaults."""

    def test_handler_is_bridge_aware(self):
        assert issubclass(ExecuteSeedHandler, BridgeAwareMixin)

    def test_defaults_to_none(self):
        handler = ExecuteSeedHandler()
        assert handler.mcp_manager is None
        assert handler.mcp_tool_prefix == ""

    def test_accepts_mcp_manager_directly(self):
        mock = MagicMock()
        handler = ExecuteSeedHandler(mcp_manager=mock, mcp_tool_prefix="pfx_")
        assert handler.mcp_manager is mock
        assert handler.mcp_tool_prefix == "pfx_"


class TestInjectBridge:
    """Verify inject_bridge auto-discovery and injection."""

    def test_injects_into_bridge_aware_handler(self):
        handler = ExecuteSeedHandler()
        bridge = MagicMock()
        bridge.manager = MagicMock(name="FakeManager")
        bridge.tool_prefix = "ext_"

        result = inject_bridge(handler, bridge)

        assert result is True
        assert handler.mcp_manager is bridge.manager
        assert handler.mcp_tool_prefix == "ext_"

    def test_skips_non_bridge_aware_handler(self):
        handler = MagicMock(spec=[])  # No BridgeAwareMixin
        bridge = MagicMock()

        result = inject_bridge(handler, bridge)
        assert result is False

    def test_skips_when_bridge_is_none(self):
        handler = ExecuteSeedHandler()
        result = inject_bridge(handler, None)
        assert result is False
        assert handler.mcp_manager is None

    def test_handles_bridge_without_tool_prefix(self):
        handler = ExecuteSeedHandler()
        bridge = MagicMock(spec=["manager"])  # No tool_prefix attr
        bridge.manager = MagicMock()

        result = inject_bridge(handler, bridge)
        assert result is True
        assert handler.mcp_manager is bridge.manager
        assert handler.mcp_tool_prefix == ""  # getattr fallback


class TestGetOuroborosTools:
    """Verify tools can be created and later injected."""

    def test_default_tools_have_no_manager(self):
        tools = get_ouroboros_tools()
        exec_handler = tools[0]
        assert isinstance(exec_handler, ExecuteSeedHandler)
        assert exec_handler.mcp_manager is None

    def test_tool_count_unchanged(self):
        tools = get_ouroboros_tools()
        assert len(tools) >= 20  # Sanity check


class TestCompositionRoot:
    """Verify create_ouroboros_server loop-based bridge injection."""

    def test_bridge_injected_into_execute_handler(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        mock_bridge = MagicMock()
        mock_bridge.manager = MagicMock(name="FakeManager")
        mock_bridge.tool_prefix = "ext_"

        server = create_ouroboros_server(mcp_bridge=mock_bridge)

        exec_handler = None
        for handler in server._tool_handlers.values():
            if isinstance(handler, ExecuteSeedHandler):
                exec_handler = handler
                break

        assert exec_handler is not None
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

    def test_no_bridge_not_in_owned_resources(self):
        from ouroboros.mcp.server.adapter import create_ouroboros_server

        server = create_ouroboros_server()
        bridge_resources = [
            r
            for r in server._owned_resources
            if hasattr(r, "tool_prefix") and hasattr(r, "manager")
        ]
        assert len(bridge_resources) == 0
