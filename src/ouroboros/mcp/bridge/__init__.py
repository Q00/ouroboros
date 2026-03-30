"""MCP Bridge — Reusable server-to-server communication layer.

This module provides a lifecycle-managed bridge that allows an MCP server
to connect to and consume tools from other MCP servers during execution.
"""

from ouroboros.mcp.bridge.bridge import MCPBridge
from ouroboros.mcp.bridge.config import MCPBridgeConfig, load_bridge_config
from ouroboros.mcp.bridge.factory import (
    create_bridge,
    create_bridge_from_config_file,
    create_bridge_from_env,
)

__all__ = [
    "MCPBridge",
    "MCPBridgeConfig",
    "create_bridge",
    "create_bridge_from_config_file",
    "create_bridge_from_env",
    "load_bridge_config",
]
