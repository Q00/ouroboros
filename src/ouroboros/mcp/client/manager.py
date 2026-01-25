"""MCP Client Manager for multi-server connection management.

This module provides the MCPClientManager class for managing connections to
multiple MCP servers with connection pooling, health checks, and per-request
timeouts.
"""

import asyncio
from collections.abc import Sequence
import contextlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.errors import (
    MCPClientError,
    MCPConnectionError,
)
from ouroboros.mcp.types import (
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
)

log = structlog.get_logger(__name__)


class ConnectionState(StrEnum):
    """State of a server connection."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    UNHEALTHY = "unhealthy"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ServerConnection:
    """Information about a server connection.

    Attributes:
        config: Server configuration.
        adapter: The client adapter for this connection.
        state: Current connection state.
        last_error: Last error message if any.
        tools: Cached list of tools from this server.
        resources: Cached list of resources from this server.
    """

    config: MCPServerConfig
    adapter: MCPClientAdapter
    state: ConnectionState = ConnectionState.DISCONNECTED
    last_error: str | None = None
    tools: tuple[MCPToolDefinition, ...] = field(default_factory=tuple)
    resources: tuple[MCPResourceDefinition, ...] = field(default_factory=tuple)


class MCPClientManager:
    """Manager for multiple MCP server connections.

    Provides connection pooling, health checks, and unified access to tools
    and resources across multiple MCP servers.

    Features:
        - Connection pooling: Reuses connections to servers
        - Health checks: Periodic checks for connection health
        - Per-request timeouts: Individual timeout per operation
        - Tool aggregation: Access all tools across servers
        - Auto-reconnection: Attempts to reconnect on failure

    Example:
        manager = MCPClientManager()

        # Add servers
        await manager.add_server(MCPServerConfig(...))
        await manager.add_server(MCPServerConfig(...))

        # Connect to all
        await manager.connect_all()

        # Use tools from any server
        tools = await manager.list_all_tools()
        result = await manager.call_tool("server1", "tool_name", {...})

        # Cleanup
        await manager.disconnect_all()
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        health_check_interval: float = 60.0,
        default_timeout: float = 30.0,
    ) -> None:
        """Initialize the manager.

        Args:
            max_retries: Maximum retry attempts for connections.
            health_check_interval: Seconds between health checks.
            default_timeout: Default timeout for operations.
        """
        self._max_retries = max_retries
        self._health_check_interval = health_check_interval
        self._default_timeout = default_timeout
        self._connections: dict[str, ServerConnection] = {}
        self._health_check_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    @property
    def servers(self) -> Sequence[str]:
        """Return list of server names."""
        return tuple(self._connections.keys())

    def get_connection_state(self, server_name: str) -> ConnectionState | None:
        """Get the connection state for a server.

        Args:
            server_name: Name of the server.

        Returns:
            ConnectionState or None if server not found.
        """
        conn = self._connections.get(server_name)
        return conn.state if conn else None

    async def add_server(
        self,
        config: MCPServerConfig,
        *,
        connect: bool = False,
    ) -> Result[MCPServerInfo | None, MCPClientError]:
        """Add a server configuration.

        Args:
            config: Server configuration.
            connect: Whether to immediately connect.

        Returns:
            Result containing server info if connect=True, None otherwise.
        """
        async with self._lock:
            if config.name in self._connections:
                return Result.err(
                    MCPClientError(
                        f"Server already exists: {config.name}",
                        server_name=config.name,
                    )
                )

            adapter = MCPClientAdapter(max_retries=self._max_retries)
            self._connections[config.name] = ServerConnection(
                config=config,
                adapter=adapter,
                state=ConnectionState.DISCONNECTED,
            )

            log.info("mcp.manager.server_added", server=config.name)

        if connect:
            connect_result = await self.connect(config.name)
            if connect_result.is_ok:
                return Result.ok(connect_result.value)
            return Result.err(connect_result.error)

        return Result.ok(None)

    async def remove_server(self, server_name: str) -> Result[None, MCPClientError]:
        """Remove a server and disconnect if connected.

        Args:
            server_name: Name of the server to remove.

        Returns:
            Result indicating success or failure.
        """
        async with self._lock:
            conn = self._connections.get(server_name)
            if not conn:
                return Result.err(
                    MCPClientError(
                        f"Server not found: {server_name}",
                        is_retriable=False,
                        details={"resource_type": "server", "resource_id": server_name},
                    )
                )

            # Disconnect if connected
            if conn.adapter.is_connected:
                await conn.adapter.disconnect()

            del self._connections[server_name]
            log.info("mcp.manager.server_removed", server=server_name)

        return Result.ok(None)

    async def connect(self, server_name: str) -> Result[MCPServerInfo, MCPClientError]:
        """Connect to a specific server.

        Args:
            server_name: Name of the server to connect to.

        Returns:
            Result containing server info or error.
        """
        async with self._lock:
            conn = self._connections.get(server_name)
            if not conn:
                return Result.err(
                    MCPClientError(
                        f"Server not found: {server_name}",
                        is_retriable=False,
                        details={"resource_type": "server", "resource_id": server_name},
                    )
                )

            # Update state to connecting
            self._connections[server_name] = ServerConnection(
                config=conn.config,
                adapter=conn.adapter,
                state=ConnectionState.CONNECTING,
            )

        # Connect outside the lock
        await conn.adapter.__aenter__()
        result = await conn.adapter.connect(conn.config)

        async with self._lock:
            if result.is_ok:
                # Cache tools and resources
                tools = await self._fetch_tools(conn.adapter, server_name)
                resources = await self._fetch_resources(conn.adapter, server_name)

                self._connections[server_name] = ServerConnection(
                    config=conn.config,
                    adapter=conn.adapter,
                    state=ConnectionState.CONNECTED,
                    tools=tools,
                    resources=resources,
                )
                log.info(
                    "mcp.manager.connected",
                    server=server_name,
                    tools=len(tools),
                    resources=len(resources),
                )
            else:
                self._connections[server_name] = ServerConnection(
                    config=conn.config,
                    adapter=conn.adapter,
                    state=ConnectionState.ERROR,
                    last_error=str(result.error),
                )
                log.error(
                    "mcp.manager.connect_failed",
                    server=server_name,
                    error=str(result.error),
                )

        return result

    async def _fetch_tools(
        self,
        adapter: MCPClientAdapter,
        server_name: str,
    ) -> tuple[MCPToolDefinition, ...]:
        """Fetch tools from an adapter, returning empty tuple on error."""
        result = await adapter.list_tools()
        if result.is_ok:
            return tuple(result.value)
        log.warning(
            "mcp.manager.fetch_tools_failed",
            server=server_name,
            error=str(result.error),
        )
        return ()

    async def _fetch_resources(
        self,
        adapter: MCPClientAdapter,
        server_name: str,
    ) -> tuple[MCPResourceDefinition, ...]:
        """Fetch resources from an adapter, returning empty tuple on error."""
        result = await adapter.list_resources()
        if result.is_ok:
            return tuple(result.value)
        log.warning(
            "mcp.manager.fetch_resources_failed",
            server=server_name,
            error=str(result.error),
        )
        return ()

    async def disconnect(self, server_name: str) -> Result[None, MCPClientError]:
        """Disconnect from a specific server.

        Args:
            server_name: Name of the server to disconnect from.

        Returns:
            Result indicating success or failure.
        """
        async with self._lock:
            conn = self._connections.get(server_name)
            if not conn:
                return Result.err(
                    MCPClientError(
                        f"Server not found: {server_name}",
                        is_retriable=False,
                        details={"resource_type": "server", "resource_id": server_name},
                    )
                )

        result = await conn.adapter.disconnect()
        await conn.adapter.__aexit__(None, None, None)

        async with self._lock:
            self._connections[server_name] = ServerConnection(
                config=conn.config,
                adapter=MCPClientAdapter(max_retries=self._max_retries),
                state=ConnectionState.DISCONNECTED,
                last_error=str(result.error) if result.is_err else None,
            )

        return result

    async def connect_all(self) -> dict[str, Result[MCPServerInfo, MCPClientError]]:
        """Connect to all registered servers.

        Returns:
            Dict mapping server names to their connection results.
        """
        results: dict[str, Result[MCPServerInfo, MCPClientError]] = {}

        # Get list of servers to connect
        servers = list(self._connections.keys())

        # Connect concurrently
        async def connect_server(name: str) -> tuple[str, Result[MCPServerInfo, MCPClientError]]:
            return (name, await self.connect(name))

        tasks = [connect_server(name) for name in servers]
        for name, result in await asyncio.gather(*[asyncio.create_task(t) for t in tasks]):
            results[name] = result

        return results

    async def disconnect_all(self) -> dict[str, Result[None, MCPClientError]]:
        """Disconnect from all servers.

        Returns:
            Dict mapping server names to their disconnect results.
        """
        results: dict[str, Result[None, MCPClientError]] = {}
        servers = list(self._connections.keys())

        for name in servers:
            results[name] = await self.disconnect(name)

        # Stop health check task if running
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._health_check_task
            self._health_check_task = None

        return results

    async def list_all_tools(self) -> Sequence[MCPToolDefinition]:
        """List all tools from all connected servers.

        Returns:
            Sequence of all tool definitions across servers.
        """
        tools: list[MCPToolDefinition] = []
        for conn in self._connections.values():
            if conn.state == ConnectionState.CONNECTED:
                tools.extend(conn.tools)
        return tools

    async def list_all_resources(self) -> Sequence[MCPResourceDefinition]:
        """List all resources from all connected servers.

        Returns:
            Sequence of all resource definitions across servers.
        """
        resources: list[MCPResourceDefinition] = []
        for conn in self._connections.values():
            if conn.state == ConnectionState.CONNECTED:
                resources.extend(conn.resources)
        return resources

    def find_tool_server(self, tool_name: str) -> str | None:
        """Find which server provides a given tool.

        Args:
            tool_name: Name of the tool to find.

        Returns:
            Server name or None if not found.
        """
        for name, conn in self._connections.items():
            if conn.state == ConnectionState.CONNECTED:
                for tool in conn.tools:
                    if tool.name == tool_name:
                        return name
        return None

    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Result[MCPToolResult, MCPClientError]:
        """Call a tool on a specific server.

        Args:
            server_name: Name of the server.
            tool_name: Name of the tool to call.
            arguments: Arguments for the tool.
            timeout: Optional timeout override.

        Returns:
            Result containing tool result or error.
        """
        conn = self._connections.get(server_name)
        if not conn:
            return Result.err(
                MCPClientError(
                    f"Server not found: {server_name}",
                    is_retriable=False,
                    details={"resource_type": "server", "resource_id": server_name},
                )
            )

        if conn.state != ConnectionState.CONNECTED:
            return Result.err(
                MCPConnectionError(
                    f"Server not connected: {server_name}",
                    server_name=server_name,
                )
            )

        effective_timeout = timeout or self._default_timeout

        try:
            return await asyncio.wait_for(
                conn.adapter.call_tool(tool_name, arguments),
                timeout=effective_timeout,
            )
        except TimeoutError:
            from ouroboros.mcp.errors import MCPTimeoutError

            return Result.err(
                MCPTimeoutError(
                    f"Tool call timed out: {tool_name}",
                    server_name=server_name,
                    timeout_seconds=effective_timeout,
                    operation="call_tool",
                )
            )

    async def call_tool_auto(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Result[MCPToolResult, MCPClientError]:
        """Call a tool, automatically finding the server that provides it.

        Args:
            tool_name: Name of the tool to call.
            arguments: Arguments for the tool.
            timeout: Optional timeout override.

        Returns:
            Result containing tool result or error.
        """
        server_name = self.find_tool_server(tool_name)
        if not server_name:
            return Result.err(
                MCPClientError(
                    f"Tool not found on any server: {tool_name}",
                    is_retriable=False,
                    details={"resource_type": "tool", "resource_id": tool_name},
                )
            )

        return await self.call_tool(server_name, tool_name, arguments, timeout=timeout)

    async def read_resource(
        self,
        server_name: str,
        uri: str,
        *,
        timeout: float | None = None,
    ) -> Result[MCPResourceContent, MCPClientError]:
        """Read a resource from a specific server.

        Args:
            server_name: Name of the server.
            uri: URI of the resource.
            timeout: Optional timeout override.

        Returns:
            Result containing resource content or error.
        """
        conn = self._connections.get(server_name)
        if not conn:
            return Result.err(
                MCPClientError(
                    f"Server not found: {server_name}",
                    is_retriable=False,
                    details={"resource_type": "server", "resource_id": server_name},
                )
            )

        if conn.state != ConnectionState.CONNECTED:
            return Result.err(
                MCPConnectionError(
                    f"Server not connected: {server_name}",
                    server_name=server_name,
                )
            )

        effective_timeout = timeout or self._default_timeout

        try:
            return await asyncio.wait_for(
                conn.adapter.read_resource(uri),
                timeout=effective_timeout,
            )
        except TimeoutError:
            from ouroboros.mcp.errors import MCPTimeoutError

            return Result.err(
                MCPTimeoutError(
                    f"Resource read timed out: {uri}",
                    server_name=server_name,
                    timeout_seconds=effective_timeout,
                    operation="read_resource",
                )
            )

    def start_health_checks(self) -> None:
        """Start periodic health checks for all connections."""
        if self._health_check_task and not self._health_check_task.done():
            return

        self._health_check_task = asyncio.create_task(self._health_check_loop())
        log.info("mcp.manager.health_checks_started")

    async def _health_check_loop(self) -> None:
        """Run periodic health checks."""
        while True:
            try:
                await asyncio.sleep(self._health_check_interval)
                await self._perform_health_checks()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("mcp.manager.health_check_error", error=str(e))

    async def _perform_health_checks(self) -> None:
        """Perform health checks on all connections."""
        for server_name, conn in list(self._connections.items()):
            if conn.state == ConnectionState.CONNECTED:
                # Simple health check: try to list tools
                result = await conn.adapter.list_tools()
                if result.is_err:
                    async with self._lock:
                        self._connections[server_name] = ServerConnection(
                            config=conn.config,
                            adapter=conn.adapter,
                            state=ConnectionState.UNHEALTHY,
                            last_error=str(result.error),
                            tools=conn.tools,
                            resources=conn.resources,
                        )
                    log.warning(
                        "mcp.manager.health_check_failed",
                        server=server_name,
                        error=str(result.error),
                    )
            elif conn.state == ConnectionState.UNHEALTHY:
                # Try to reconnect
                reconnect_result = await self.connect(server_name)
                if reconnect_result.is_ok:
                    log.info("mcp.manager.reconnected", server=server_name)
