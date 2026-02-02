"""MCP Client protocol definition.

This module defines the MCPClient protocol that all MCP client implementations
must follow. It provides a unified interface for connecting to MCP servers
and using their tools, resources, and prompts.
"""

from collections.abc import Sequence
from typing import Any, Protocol

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPClientError
from ouroboros.mcp.types import (
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
)


class MCPClient(Protocol):
    """Protocol for MCP client implementations.

    This protocol defines the interface that all MCP client adapters must
    implement. It supports connecting to MCP servers and using their
    tools, resources, and prompts.

    All methods return Result[T, MCPClientError] to handle expected failures
    without exceptions. Exceptions are reserved for programming errors.

    Example:
        async with MCPClientAdapter() as client:
            result = await client.connect(config)
            if result.is_err:
                log.error("Failed to connect", error=result.error)
                return

            tools_result = await client.list_tools()
            if tools_result.is_ok:
                for tool in tools_result.value:
                    print(f"Tool: {tool.name}")

            call_result = await client.call_tool(
                "my_tool",
                {"arg1": "value1"}
            )
            if call_result.is_ok:
                print(call_result.value.text_content)
    """

    async def connect(
        self,
        config: MCPServerConfig,
    ) -> Result[MCPServerInfo, MCPClientError]:
        """Connect to an MCP server.

        Establishes a connection to the MCP server specified by the config.
        This method handles retries internally and returns server information
        on success.

        Args:
            config: Configuration for the server connection.

        Returns:
            Result containing server info on success or MCPClientError on failure.
        """
        ...

    async def disconnect(self) -> Result[None, MCPClientError]:
        """Disconnect from the current MCP server.

        Cleanly closes the connection to the server. Safe to call even if
        not connected.

        Returns:
            Result containing None on success or MCPClientError on failure.
        """
        ...

    @property
    def is_connected(self) -> bool:
        """Return True if currently connected to a server."""
        ...

    @property
    def server_info(self) -> MCPServerInfo | None:
        """Return information about the connected server, or None if not connected."""
        ...

    async def list_tools(self) -> Result[Sequence[MCPToolDefinition], MCPClientError]:
        """List available tools from the connected server.

        Returns:
            Result containing sequence of tool definitions or MCPClientError.
        """
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Result[MCPToolResult, MCPClientError]:
        """Call a tool on the connected server.

        Args:
            name: Name of the tool to call.
            arguments: Arguments to pass to the tool.

        Returns:
            Result containing tool result or MCPClientError.
        """
        ...

    async def list_resources(self) -> Result[Sequence[MCPResourceDefinition], MCPClientError]:
        """List available resources from the connected server.

        Returns:
            Result containing sequence of resource definitions or MCPClientError.
        """
        ...

    async def read_resource(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPClientError]:
        """Read a resource from the connected server.

        Args:
            uri: URI of the resource to read.

        Returns:
            Result containing resource content or MCPClientError.
        """
        ...

    async def list_prompts(self) -> Result[Sequence[MCPPromptDefinition], MCPClientError]:
        """List available prompts from the connected server.

        Returns:
            Result containing sequence of prompt definitions or MCPClientError.
        """
        ...

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> Result[str, MCPClientError]:
        """Get a prompt from the connected server.

        Args:
            name: Name of the prompt to get.
            arguments: Arguments to fill in the prompt template.

        Returns:
            Result containing the prompt text or MCPClientError.
        """
        ...
