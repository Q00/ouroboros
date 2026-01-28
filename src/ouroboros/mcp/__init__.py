"""MCP (Model Context Protocol) integration for Ouroboros.

This module provides both MCP client and server functionality:
- MCP Client: Connect to external MCP servers to use their tools and resources
- MCP Server: Expose Ouroboros functionality as an MCP server

Public API:
    Errors:
        MCPError, MCPClientError, MCPServerError, MCPAuthError,
        MCPTimeoutError, MCPConnectionError, MCPProtocolError,
        MCPResourceNotFoundError, MCPToolError

    Types:
        TransportType, MCPServerConfig, MCPToolDefinition, MCPToolResult,
        MCPToolParameter, MCPContentItem, ContentType,
        MCPResourceDefinition, MCPResourceContent,
        MCPPromptDefinition, MCPPromptArgument,
        MCPCapabilities, MCPServerInfo, MCPRequest, MCPResponse

    Client:
        MCPClient (Protocol), MCPClientAdapter, MCPClientManager

    Server:
        MCPServer (Protocol), MCPServerAdapter
"""

from ouroboros.mcp.errors import (
    MCPAuthError,
    MCPClientError,
    MCPConnectionError,
    MCPError,
    MCPProtocolError,
    MCPResourceNotFoundError,
    MCPServerError,
    MCPTimeoutError,
    MCPToolError,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPCapabilities,
    MCPContentItem,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPRequest,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPResponse,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    TransportType,
)

__all__ = [
    # Errors
    "MCPError",
    "MCPClientError",
    "MCPServerError",
    "MCPAuthError",
    "MCPTimeoutError",
    "MCPConnectionError",
    "MCPProtocolError",
    "MCPResourceNotFoundError",
    "MCPToolError",
    # Types
    "TransportType",
    "ContentType",
    "MCPServerConfig",
    "MCPToolDefinition",
    "MCPToolParameter",
    "MCPToolResult",
    "MCPContentItem",
    "MCPResourceDefinition",
    "MCPResourceContent",
    "MCPPromptDefinition",
    "MCPPromptArgument",
    "MCPCapabilities",
    "MCPServerInfo",
    "MCPRequest",
    "MCPResponse",
]
