"""BridgeAwareMixin for MCP tool handlers that need external MCP access.

Handlers that inherit from this mixin will automatically receive
an MCPClientManager reference when an MCPBridge is configured,
via loop-based injection in the composition root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class BridgeAwareMixin:
    """Mixin for handlers that need access to external MCP servers.

    Provides ``mcp_manager`` and ``mcp_tool_prefix`` fields that are
    injected by the composition root when an MCPBridge is configured.
    Both fields default to None/"" so handlers work without a bridge.

    Usage::

        @dataclass
        class MyHandler(BridgeAwareMixin):
            other_field: str = ""

            async def handle(self, arguments):
                if self.mcp_manager:
                    tools = await self.mcp_manager.list_all_tools()
    """

    mcp_manager: Any | None = field(default=None, repr=False)
    mcp_tool_prefix: str = ""


def inject_bridge(handler: object, bridge: object | None) -> bool:
    """Inject bridge manager into a BridgeAwareMixin handler.

    Args:
        handler: A tool handler, possibly BridgeAwareMixin.
        bridge: An MCPBridge instance (must have .manager and .tool_prefix).

    Returns:
        True if injection was performed, False otherwise.
    """
    if bridge is None or not isinstance(handler, BridgeAwareMixin):
        return False

    handler.mcp_manager = getattr(bridge, "manager", None)
    handler.mcp_tool_prefix = getattr(bridge, "tool_prefix", "")
    return True
