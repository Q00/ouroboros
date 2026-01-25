"""MCP Server package.

This package provides MCP server functionality for exposing Ouroboros
capabilities to external MCP clients.

Public API:
    MCPServer: Protocol defining the server interface
    ToolHandler: Protocol for tool handlers
    ResourceHandler: Protocol for resource handlers
    MCPServerAdapter: Concrete implementation using FastMCP
"""

from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.server.protocol import MCPServer, ResourceHandler, ToolHandler

__all__ = [
    "MCPServer",
    "ToolHandler",
    "ResourceHandler",
    "MCPServerAdapter",
]
