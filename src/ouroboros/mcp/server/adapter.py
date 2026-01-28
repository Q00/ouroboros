"""MCP Server adapter implementation.

This module provides the MCPServerAdapter class that implements the MCPServer
protocol using the MCP SDK (FastMCP). It handles tool registration, resource
handling, and server lifecycle.
"""

from collections.abc import Sequence
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import (
    MCPResourceNotFoundError,
    MCPServerError,
    MCPToolError,
)
from ouroboros.mcp.server.protocol import PromptHandler, ResourceHandler, ToolHandler
from ouroboros.mcp.server.security import AuthConfig, RateLimitConfig, SecurityLayer
from ouroboros.mcp.types import (
    MCPCapabilities,
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
)

log = structlog.get_logger(__name__)


class MCPServerAdapter:
    """Concrete implementation of MCPServer protocol.

    Uses the MCP SDK to expose Ouroboros functionality as an MCP server.
    Supports tool registration, resource handling, and optional security.

    Example:
        server = MCPServerAdapter(
            name="ouroboros-mcp",
            version="1.0.0",
        )

        # Register handlers
        server.register_tool(ExecuteSeedHandler())
        server.register_resource(SessionResourceHandler())

        # Start serving
        await server.serve()
    """

    def __init__(
        self,
        *,
        name: str = "ouroboros-mcp",
        version: str = "1.0.0",
        auth_config: AuthConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
    ) -> None:
        """Initialize the server adapter.

        Args:
            name: Server name for identification.
            version: Server version.
            auth_config: Optional authentication configuration.
            rate_limit_config: Optional rate limiting configuration.
        """
        self._name = name
        self._version = version
        self._tool_handlers: dict[str, ToolHandler] = {}
        self._resource_handlers: dict[str, ResourceHandler] = {}
        self._prompt_handlers: dict[str, PromptHandler] = {}
        self._mcp_server: Any = None

        # Initialize security layer
        self._security = SecurityLayer(
            auth_config=auth_config or AuthConfig(),
            rate_limit_config=rate_limit_config or RateLimitConfig(),
        )

    @property
    def info(self) -> MCPServerInfo:
        """Return server information."""
        return MCPServerInfo(
            name=self._name,
            version=self._version,
            capabilities=MCPCapabilities(
                tools=len(self._tool_handlers) > 0,
                resources=len(self._resource_handlers) > 0,
                prompts=len(self._prompt_handlers) > 0,
                logging=True,
            ),
            tools=tuple(h.definition for h in self._tool_handlers.values()),
            resources=tuple(
                defn
                for handler in self._resource_handlers.values()
                for defn in handler.definitions
            ),
            prompts=tuple(h.definition for h in self._prompt_handlers.values()),
        )

    def register_tool(self, handler: ToolHandler) -> None:
        """Register a tool handler.

        Args:
            handler: The tool handler to register.
        """
        name = handler.definition.name
        self._tool_handlers[name] = handler
        log.info("mcp.server.tool_registered", tool=name)

    def register_resource(self, handler: ResourceHandler) -> None:
        """Register a resource handler.

        Args:
            handler: The resource handler to register.
        """
        for defn in handler.definitions:
            self._resource_handlers[defn.uri] = handler
            log.info("mcp.server.resource_registered", uri=defn.uri)

    def register_prompt(self, handler: PromptHandler) -> None:
        """Register a prompt handler.

        Args:
            handler: The prompt handler to register.
        """
        name = handler.definition.name
        self._prompt_handlers[name] = handler
        log.info("mcp.server.prompt_registered", prompt=name)

    async def list_tools(self) -> Sequence[MCPToolDefinition]:
        """List all registered tools.

        Returns:
            Sequence of tool definitions.
        """
        return tuple(h.definition for h in self._tool_handlers.values())

    async def list_resources(self) -> Sequence[MCPResourceDefinition]:
        """List all registered resources.

        Returns:
            Sequence of resource definitions.
        """
        # Collect unique definitions from all handlers
        seen_uris: set[str] = set()
        definitions: list[MCPResourceDefinition] = []

        for handler in self._resource_handlers.values():
            for defn in handler.definitions:
                if defn.uri not in seen_uris:
                    seen_uris.add(defn.uri)
                    definitions.append(defn)

        return definitions

    async def list_prompts(self) -> Sequence[MCPPromptDefinition]:
        """List all registered prompts.

        Returns:
            Sequence of prompt definitions.
        """
        return tuple(h.definition for h in self._prompt_handlers.values())

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
        handler = self._tool_handlers.get(name)
        if not handler:
            return Result.err(
                MCPResourceNotFoundError(
                    f"Tool not found: {name}",
                    server_name=self._name,
                    resource_type="tool",
                    resource_id=name,
                )
            )

        # Security check
        security_result = await self._security.check_request(name, arguments, credentials)
        if security_result.is_err:
            return Result.err(security_result.error)

        try:
            result = await handler.handle(arguments)
            return result
        except Exception as e:
            log.error("mcp.server.tool_error", tool=name, error=str(e))
            return Result.err(
                MCPToolError(
                    f"Tool execution failed: {e}",
                    server_name=self._name,
                    tool_name=name,
                )
            )

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
        handler = self._resource_handlers.get(uri)
        if not handler:
            return Result.err(
                MCPResourceNotFoundError(
                    f"Resource not found: {uri}",
                    server_name=self._name,
                    resource_type="resource",
                    resource_id=uri,
                )
            )

        try:
            result = await handler.handle(uri)
            return result
        except Exception as e:
            log.error("mcp.server.resource_error", uri=uri, error=str(e))
            return Result.err(
                MCPServerError(
                    f"Resource read failed: {e}",
                    server_name=self._name,
                )
            )

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
        handler = self._prompt_handlers.get(name)
        if not handler:
            return Result.err(
                MCPResourceNotFoundError(
                    f"Prompt not found: {name}",
                    server_name=self._name,
                    resource_type="prompt",
                    resource_id=name,
                )
            )

        try:
            result = await handler.handle(arguments)
            return result
        except Exception as e:
            log.error("mcp.server.prompt_error", prompt=name, error=str(e))
            return Result.err(
                MCPServerError(
                    f"Prompt generation failed: {e}",
                    server_name=self._name,
                )
            )

    async def serve(self, transport: str = "stdio") -> None:
        """Start serving MCP requests.

        This method blocks until the server is stopped.
        Uses the MCP SDK's FastMCP server implementation.

        Args:
            transport: Transport type - "stdio" or "sse".
        """
        try:
            from mcp.server.fastmcp import FastMCP
        except ImportError as e:
            msg = "mcp package not installed. Install with: pip install mcp"
            raise ImportError(msg) from e

        # Create FastMCP server
        self._mcp_server = FastMCP(self._name)

        # Register tools with FastMCP
        for _name, handler in self._tool_handlers.items():
            defn = handler.definition

            # Create a closure to capture the handler
            async def create_tool_wrapper(
                h: ToolHandler,
            ) -> Any:
                async def tool_wrapper(**kwargs: Any) -> Any:
                    result = await h.handle(kwargs)
                    if result.is_ok:
                        # Convert MCPToolResult to FastMCP format
                        tool_result = result.value
                        return tool_result.text_content
                    else:
                        return f"Error: {result.error}"

                return tool_wrapper

            wrapper = await create_tool_wrapper(handler)
            self._mcp_server.tool(
                name=defn.name,
                description=defn.description,
            )(wrapper)

        # Register resources with FastMCP
        for uri, res_handler in self._resource_handlers.items():

            async def create_resource_wrapper(
                h: ResourceHandler,
                resource_uri: str,
            ) -> Any:
                async def resource_wrapper() -> str:
                    result = await h.handle(resource_uri)
                    if result.is_ok:
                        content = result.value
                        return content.text or ""
                    else:
                        return f"Error: {result.error}"

                return resource_wrapper

            wrapper = await create_resource_wrapper(res_handler, uri)
            self._mcp_server.resource(uri)(wrapper)

        log.info(
            "mcp.server.starting",
            name=self._name,
            tools=len(self._tool_handlers),
            resources=len(self._resource_handlers),
        )

        # Run the server with the appropriate transport
        if transport == "sse":
            await self._mcp_server.run_sse_async()
        else:
            await self._mcp_server.run_stdio_async()

    async def shutdown(self) -> None:
        """Shutdown the server gracefully."""
        log.info("mcp.server.shutdown", name=self._name)
        # FastMCP handles its own shutdown when run_async completes


def create_ouroboros_server(
    *,
    name: str = "ouroboros-mcp",
    version: str = "1.0.0",
    auth_config: AuthConfig | None = None,
    rate_limit_config: RateLimitConfig | None = None,
) -> MCPServerAdapter:
    """Create an Ouroboros MCP server with default handlers.

    This is a convenience function that creates a server with all
    standard Ouroboros tools and resources pre-registered.

    Args:
        name: Server name.
        version: Server version.
        auth_config: Optional authentication configuration.
        rate_limit_config: Optional rate limiting configuration.

    Returns:
        Configured MCPServerAdapter ready to serve.
    """
    server = MCPServerAdapter(
        name=name,
        version=version,
        auth_config=auth_config,
        rate_limit_config=rate_limit_config,
    )

    # Tools and resources will be registered separately
    # to avoid circular imports

    return server
