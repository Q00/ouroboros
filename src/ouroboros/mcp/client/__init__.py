"""MCP Client package.

This package provides MCP client functionality for connecting to external
MCP servers and using their tools, resources, and prompts.

Public API:
    MCPClient: Protocol defining the client interface
    MCPClientAdapter: Concrete implementation using the MCP SDK
    MCPClientManager: Manager for multiple server connections
"""

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.client.manager import MCPClientManager
from ouroboros.mcp.client.protocol import MCPClient

__all__ = [
    "MCPClient",
    "MCPClientAdapter",
    "MCPClientManager",
]
