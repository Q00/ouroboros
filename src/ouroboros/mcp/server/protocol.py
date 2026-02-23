"""MCP Server protocol definitions.

This module defines the protocols that MCP server implementations must follow.
It provides interfaces for the server, tool handlers, and resource handlers.
"""

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.types import (
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
)

# Type aliases for handler functions
ToolHandlerFunc = Callable[[dict[str, Any]], Awaitable[Result[MCPToolResult, MCPServerError]]]
ResourceHandlerFunc = Callable[[str], Awaitable[Result[MCPResourceContent, MCPServerError]]]
PromptHandlerFunc = Callable[[dict[str, str]], Awaitable[Result[str, MCPServerError]]]


class ToolHandler(Protocol):
    """Protocol for tool handler implementations.

    Tool handlers process incoming tool calls from MCP clients.
    Each handler corresponds to a specific tool and is responsible
    for validating arguments and executing the tool logic.

    Example:
        class MyToolHandler:
            def __init__(self, service: MyService):
                self._service = service

            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name="my_tool",
                    description="Does something useful",
                    parameters=(
                        MCPToolParameter(name="input", type=ToolInputType.STRING),
                    ),
                )

            async def handle(
                self, arguments: dict[str, Any]
            ) -> Result[MCPToolResult, MCPServerError]:
                result = await self._service.process(arguments["input"])
                return Result.ok(MCPToolResult(...))
    """

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        ...

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a tool call.

        Args:
            arguments: The arguments passed to the tool.

        Returns:
            Result containing the tool result or an error.
        """
        ...


class ResourceHandler(Protocol):
    """Protocol for resource handler implementations.

    Resource handlers provide read access to resources via URI.
    Each handler is responsible for one or more resource URIs.

    Example:
        class SessionResourceHandler:
            @property
            def definitions(self) -> Sequence[MCPResourceDefinition]:
                return [
                    MCPResourceDefinition(
                        uri="ouroboros://sessions/current",
                        name="Current Session",
                        description="The current active session",
                    ),
                ]

            async def handle(
                self, uri: str
            ) -> Result[MCPResourceContent, MCPServerError]:
                # Fetch and return the resource content
                ...
    """

    @property
    def definitions(self) -> Sequence[MCPResourceDefinition]:
        """Return the list of resource definitions this handler provides."""
        ...

    async def handle(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Handle a resource read request.

        Args:
            uri: The URI of the resource to read.

        Returns:
            Result containing the resource content or an error.
        """
        ...


class PromptHandler(Protocol):
    """Protocol for prompt handler implementations.

    Prompt handlers provide dynamic prompt templates that can be
    filled with arguments from the client.

    Example:
        class AnalysisPromptHandler:
            @property
            def definition(self) -> MCPPromptDefinition:
                return MCPPromptDefinition(
                    name="analyze",
                    description="Analyze code for issues",
                    arguments=(
                        MCPPromptArgument(name="code", required=True),
                    ),
                )

            async def handle(
                self, arguments: dict[str, str]
            ) -> Result[str, MCPServerError]:
                return Result.ok(f"Analyze this code:\n{arguments['code']}")
    """

    @property
    def definition(self) -> MCPPromptDefinition:
        """Return the prompt definition."""
        ...

    async def handle(
        self,
        arguments: dict[str, str],
    ) -> Result[str, MCPServerError]:
        """Handle a prompt request.

        Args:
            arguments: The arguments to fill in the prompt template.

        Returns:
            Result containing the filled prompt or an error.
        """
        ...


class MCPServer(Protocol):
    """Protocol for MCP server implementations.

    This protocol defines the interface that all MCP server adapters must
    implement. It supports registering handlers for tools, resources, and
    prompts, as well as starting and stopping the server.

    Example:
        server = MCPServerAdapter(name="ouroboros-mcp")

        # Register handlers
        server.register_tool(ExecuteSeedHandler())
        server.register_resource(SessionResourceHandler())

        # Start serving
        await server.serve()
    """

    @property
    def info(self) -> MCPServerInfo:
        """Return server information."""
        ...

    def register_tool(self, handler: ToolHandler) -> None:
        """Register a tool handler.

        Args:
            handler: The tool handler to register.
        """
        ...

    def register_resource(self, handler: ResourceHandler) -> None:
        """Register a resource handler.

        Args:
            handler: The resource handler to register.
        """
        ...

    def register_prompt(self, handler: PromptHandler) -> None:
        """Register a prompt handler.

        Args:
            handler: The prompt handler to register.
        """
        ...

    async def list_tools(self) -> Sequence[MCPToolDefinition]:
        """List all registered tools.

        Returns:
            Sequence of tool definitions.
        """
        ...

    async def list_resources(self) -> Sequence[MCPResourceDefinition]:
        """List all registered resources.

        Returns:
            Sequence of resource definitions.
        """
        ...

    async def list_prompts(self) -> Sequence[MCPPromptDefinition]:
        """List all registered prompts.

        Returns:
            Sequence of prompt definitions.
        """
        ...

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        credentials: dict[str, str] | None = None,
    ) -> Result[MCPToolResult, MCPServerError]:
        """Call a registered tool.

        Args:
            name: Name of the tool to call.
            arguments: Arguments for the tool.
            credentials: Optional credentials for authentication.

        Returns:
            Result containing the tool result or an error.
        """
        ...

    async def read_resource(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Read a registered resource.

        Args:
            uri: URI of the resource to read.

        Returns:
            Result containing the resource content or an error.
        """
        ...

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
    ) -> Result[str, MCPServerError]:
        """Get a filled prompt.

        Args:
            name: Name of the prompt.
            arguments: Arguments to fill in the template.

        Returns:
            Result containing the filled prompt or an error.
        """
        ...

    async def serve(self) -> None:
        """Start serving MCP requests.

        This method blocks until the server is stopped.
        """
        ...

    async def shutdown(self) -> None:
        """Shutdown the server gracefully."""
        ...
