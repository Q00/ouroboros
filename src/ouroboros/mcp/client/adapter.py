"""MCP Client adapter implementation.

This module provides the MCPClientAdapter class that implements the MCPClient
protocol using the MCP SDK. It handles connection management, retries, and
error handling.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, Self

import stamina
import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import (
    MCPClientError,
    MCPConnectionError,
    MCPTimeoutError,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPCapabilities,
    MCPContentItem,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
    TransportType,
)

log = structlog.get_logger(__name__)

# Exceptions that are safe to retry
RETRIABLE_EXCEPTIONS = (
    TimeoutError,
    ConnectionError,
    OSError,
)


class MCPClientAdapter:
    """Concrete implementation of MCPClient protocol.

    Uses the MCP SDK to connect to MCP servers and provides automatic retry
    logic using stamina for transient failures.

    Example:
        config = MCPServerConfig(
            name="my-server",
            transport=TransportType.STDIO,
            command="my-mcp-server",
        )

        async with MCPClientAdapter() as client:
            result = await client.connect(config)
            if result.is_ok:
                tools = await client.list_tools()

        # Or use as regular async context manager
        adapter = MCPClientAdapter()
        await adapter.__aenter__()
        try:
            await adapter.connect(config)
        finally:
            await adapter.__aexit__(None, None, None)
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        retry_wait_initial: float = 1.0,
        retry_wait_max: float = 10.0,
    ) -> None:
        """Initialize the adapter.

        Args:
            max_retries: Maximum number of retry attempts for transient failures.
            retry_wait_initial: Initial wait time between retries in seconds.
            retry_wait_max: Maximum wait time between retries in seconds.
        """
        self._max_retries = max_retries
        self._retry_wait_initial = retry_wait_initial
        self._retry_wait_max = retry_wait_max
        self._session: Any = None
        self._read_stream: Any = None
        self._write_stream: Any = None
        self._server_info: MCPServerInfo | None = None
        self._config: MCPServerConfig | None = None

    async def __aenter__(self) -> Self:
        """Enter async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit async context manager, ensuring disconnect."""
        await self.disconnect()

    @property
    def is_connected(self) -> bool:
        """Return True if currently connected to a server."""
        return self._session is not None

    @property
    def server_info(self) -> MCPServerInfo | None:
        """Return information about the connected server."""
        return self._server_info

    async def connect(
        self,
        config: MCPServerConfig,
    ) -> Result[MCPServerInfo, MCPClientError]:
        """Connect to an MCP server.

        Establishes a connection using the appropriate transport (stdio, SSE, etc.)
        and initializes the session. Uses stamina for automatic retries.

        Args:
            config: Configuration for the server connection.

        Returns:
            Result containing server info on success or MCPClientError on failure.
        """
        if self._session is not None:
            disconnect_result = await self.disconnect()
            if disconnect_result.is_err:
                log.warning(
                    "mcp.disconnect_before_connect_failed",
                    error=disconnect_result.error,
                )

        self._config = config

        @stamina.retry(
            on=RETRIABLE_EXCEPTIONS,
            attempts=self._max_retries,
            wait_initial=self._retry_wait_initial,
            wait_max=self._retry_wait_max,
            wait_jitter=1.0,
        )
        async def _connect_with_retry() -> None:
            await self._raw_connect(config)

        try:
            await _connect_with_retry()
            log.info(
                "mcp.connected",
                server=config.name,
                transport=config.transport.value,
            )
            return Result.ok(self._server_info)  # type: ignore[arg-type]
        except TimeoutError as e:
            timeout_error = MCPTimeoutError(
                f"Connection timeout: {e}",
                server_name=config.name,
                timeout_seconds=config.timeout,
                operation="connect",
            )
            timeout_error.__cause__ = e
            return Result.err(timeout_error)
        except ConnectionError as e:
            conn_error = MCPConnectionError(
                f"Connection failed: {e}",
                server_name=config.name,
                transport=config.transport.value,
            )
            conn_error.__cause__ = e
            return Result.err(conn_error)
        except Exception as e:
            client_error = MCPClientError.from_exception(
                e,
                server_name=config.name,
                is_retriable=False,
            )
            return Result.err(client_error)

    async def _raw_connect(self, config: MCPServerConfig) -> None:
        """Perform the actual connection without retry logic.

        Args:
            config: Server configuration.

        Raises:
            Various exceptions depending on connection issues.
        """
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as e:
            msg = "mcp package not installed. Install with: pip install mcp"
            raise ImportError(msg) from e

        if config.transport == TransportType.STDIO:
            if not config.command:
                msg = "command is required for stdio transport"
                raise ValueError(msg)

            server_params = StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env=config.env if config.env else None,
            )

            self._read_stream, self._write_stream = await stdio_client(server_params).__aenter__()
            self._session = ClientSession(self._read_stream, self._write_stream)
            await self._session.__aenter__()

            # Initialize the session
            result = await self._session.initialize()
            self._server_info = self._parse_server_info(result, config.name)

        elif config.transport in (TransportType.SSE, TransportType.STREAMABLE_HTTP):
            # SSE/HTTP transport would be implemented here
            msg = f"Transport {config.transport} not yet implemented"
            raise NotImplementedError(msg)
        else:
            msg = f"Unknown transport: {config.transport}"
            raise ValueError(msg)

    def _parse_server_info(self, init_result: Any, server_name: str) -> MCPServerInfo:
        """Parse server info from initialization result.

        Args:
            init_result: Result from session.initialize().
            server_name: Name of the server.

        Returns:
            Parsed MCPServerInfo.
        """
        capabilities = MCPCapabilities(
            tools=getattr(init_result.capabilities, "tools", None) is not None,
            resources=getattr(init_result.capabilities, "resources", None) is not None,
            prompts=getattr(init_result.capabilities, "prompts", None) is not None,
            logging=getattr(init_result.capabilities, "logging", None) is not None,
        )

        return MCPServerInfo(
            name=server_name,
            version=getattr(init_result, "protocolVersion", "1.0.0"),
            capabilities=capabilities,
        )

    async def disconnect(self) -> Result[None, MCPClientError]:
        """Disconnect from the current MCP server.

        Returns:
            Result containing None on success or MCPClientError on failure.
        """
        if self._session is None:
            return Result.ok(None)

        try:
            await self._session.__aexit__(None, None, None)
            self._session = None
            self._server_info = None
            log.info("mcp.disconnected", server=self._config.name if self._config else "unknown")
            return Result.ok(None)
        except Exception as e:
            return Result.err(
                MCPClientError.from_exception(
                    e,
                    server_name=self._config.name if self._config else None,
                )
            )

    def _ensure_connected(self) -> Result[None, MCPClientError]:
        """Ensure we're connected to a server.

        Returns:
            Result.ok(None) if connected, Result.err otherwise.
        """
        if self._session is None:
            return Result.err(
                MCPConnectionError(
                    "Not connected to any server",
                    server_name=self._config.name if self._config else None,
                )
            )
        return Result.ok(None)

    async def list_tools(self) -> Result[Sequence[MCPToolDefinition], MCPClientError]:
        """List available tools from the connected server.

        Returns:
            Result containing sequence of tool definitions or MCPClientError.
        """
        connected = self._ensure_connected()
        if connected.is_err:
            return Result.err(connected.error)

        try:
            result = await self._session.list_tools()
            tools = tuple(
                self._parse_tool_definition(tool, self._config.name if self._config else None)
                for tool in result.tools
            )
            return Result.ok(tools)
        except Exception as e:
            return Result.err(
                MCPClientError.from_exception(
                    e,
                    server_name=self._config.name if self._config else None,
                )
            )

    def _parse_tool_definition(self, tool: Any, server_name: str | None) -> MCPToolDefinition:
        """Parse a tool definition from the MCP SDK format.

        Args:
            tool: Tool object from MCP SDK.
            server_name: Name of the server providing this tool.

        Returns:
            Parsed MCPToolDefinition.
        """
        parameters: list[MCPToolParameter] = []

        if hasattr(tool, "inputSchema") and tool.inputSchema:
            schema = tool.inputSchema
            properties = schema.get("properties", {})
            required = set(schema.get("required", []))

            for name, prop in properties.items():
                param_type = ToolInputType(prop.get("type", "string"))
                parameters.append(
                    MCPToolParameter(
                        name=name,
                        type=param_type,
                        description=prop.get("description", ""),
                        required=name in required,
                        default=prop.get("default"),
                        enum=tuple(prop["enum"]) if "enum" in prop else None,
                    )
                )

        return MCPToolDefinition(
            name=tool.name,
            description=getattr(tool, "description", "") or "",
            parameters=tuple(parameters),
            server_name=server_name,
        )

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
        connected = self._ensure_connected()
        if connected.is_err:
            return Result.err(connected.error)

        try:
            result = await self._session.call_tool(name, arguments or {})
            return Result.ok(self._parse_tool_result(result, name))
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg or "unknown tool" in error_msg:
                return Result.err(
                    MCPClientError(
                        f"Tool not found: {name}",
                        server_name=self._config.name if self._config else None,
                        is_retriable=False,
                        details={"resource_type": "tool", "resource_id": name},
                    )
                )
            return Result.err(
                MCPClientError(
                    f"Tool execution failed: {e}",
                    server_name=self._config.name if self._config else None,
                    is_retriable=False,
                    details={"tool_name": name},
                )
            )

    def _parse_tool_result(self, result: Any, _tool_name: str) -> MCPToolResult:
        """Parse a tool result from the MCP SDK format.

        Args:
            result: Result object from MCP SDK.
            tool_name: Name of the tool that was called.

        Returns:
            Parsed MCPToolResult.
        """
        content_items: list[MCPContentItem] = []

        for item in getattr(result, "content", []):
            if hasattr(item, "text"):
                content_items.append(MCPContentItem(type=ContentType.TEXT, text=item.text))
            elif hasattr(item, "data"):
                content_items.append(
                    MCPContentItem(
                        type=ContentType.IMAGE,
                        data=item.data,
                        mime_type=getattr(item, "mimeType", "image/png"),
                    )
                )
            elif hasattr(item, "uri"):
                content_items.append(MCPContentItem(type=ContentType.RESOURCE, uri=item.uri))

        return MCPToolResult(
            content=tuple(content_items),
            is_error=getattr(result, "isError", False),
        )

    async def list_resources(self) -> Result[Sequence[MCPResourceDefinition], MCPClientError]:
        """List available resources from the connected server.

        Returns:
            Result containing sequence of resource definitions or MCPClientError.
        """
        connected = self._ensure_connected()
        if connected.is_err:
            return Result.err(connected.error)

        try:
            result = await self._session.list_resources()
            resources = tuple(
                MCPResourceDefinition(
                    uri=res.uri,
                    name=getattr(res, "name", res.uri),
                    description=getattr(res, "description", "") or "",
                    mime_type=getattr(res, "mimeType", "text/plain"),
                )
                for res in result.resources
            )
            return Result.ok(resources)
        except Exception as e:
            return Result.err(
                MCPClientError.from_exception(
                    e,
                    server_name=self._config.name if self._config else None,
                )
            )

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
        connected = self._ensure_connected()
        if connected.is_err:
            return Result.err(connected.error)

        try:
            result = await self._session.read_resource(uri)
            contents = result.contents

            if not contents:
                return Result.err(
                    MCPClientError(
                        f"Resource not found: {uri}",
                        server_name=self._config.name if self._config else None,
                        is_retriable=False,
                        details={"resource_type": "resource", "resource_id": uri},
                    )
                )

            first_content = contents[0]
            return Result.ok(
                MCPResourceContent(
                    uri=uri,
                    text=getattr(first_content, "text", None),
                    blob=getattr(first_content, "blob", None),
                    mime_type=getattr(first_content, "mimeType", "text/plain"),
                )
            )
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg:
                return Result.err(
                    MCPClientError(
                        f"Resource not found: {uri}",
                        server_name=self._config.name if self._config else None,
                        is_retriable=False,
                        details={"resource_type": "resource", "resource_id": uri},
                    )
                )
            return Result.err(
                MCPClientError.from_exception(
                    e,
                    server_name=self._config.name if self._config else None,
                )
            )

    async def list_prompts(self) -> Result[Sequence[MCPPromptDefinition], MCPClientError]:
        """List available prompts from the connected server.

        Returns:
            Result containing sequence of prompt definitions or MCPClientError.
        """
        connected = self._ensure_connected()
        if connected.is_err:
            return Result.err(connected.error)

        try:
            result = await self._session.list_prompts()
            prompts = tuple(
                MCPPromptDefinition(
                    name=prompt.name,
                    description=getattr(prompt, "description", "") or "",
                    arguments=tuple(
                        MCPPromptArgument(
                            name=arg.name,
                            description=getattr(arg, "description", "") or "",
                            required=getattr(arg, "required", True),
                        )
                        for arg in getattr(prompt, "arguments", [])
                    ),
                )
                for prompt in result.prompts
            )
            return Result.ok(prompts)
        except Exception as e:
            return Result.err(
                MCPClientError.from_exception(
                    e,
                    server_name=self._config.name if self._config else None,
                )
            )

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
        connected = self._ensure_connected()
        if connected.is_err:
            return Result.err(connected.error)

        try:
            result = await self._session.get_prompt(name, arguments or {})
            # Combine all text messages into a single prompt
            texts = [msg.content.text for msg in result.messages if hasattr(msg.content, "text")]
            return Result.ok("\n".join(texts))
        except Exception as e:
            error_msg = str(e).lower()
            if "not found" in error_msg:
                return Result.err(
                    MCPClientError(
                        f"Prompt not found: {name}",
                        server_name=self._config.name if self._config else None,
                        is_retriable=False,
                        details={"resource_type": "prompt", "resource_id": name},
                    )
                )
            return Result.err(
                MCPClientError.from_exception(
                    e,
                    server_name=self._config.name if self._config else None,
                )
            )


@asynccontextmanager
async def create_mcp_client(
    config: MCPServerConfig,
    *,
    max_retries: int = 3,
) -> AsyncIterator[MCPClientAdapter]:
    """Create and connect an MCP client as an async context manager.

    Convenience function that creates an MCPClientAdapter, connects to
    the specified server, and yields the connected client.

    Args:
        config: Configuration for the server connection.
        max_retries: Maximum number of retry attempts.

    Yields:
        Connected MCPClientAdapter.

    Raises:
        MCPConnectionError: If connection fails after all retries.

    Example:
        async with create_mcp_client(config) as client:
            result = await client.list_tools()
    """
    adapter = MCPClientAdapter(max_retries=max_retries)
    async with adapter:
        result = await adapter.connect(config)
        if result.is_err:
            raise result.error
        yield adapter
