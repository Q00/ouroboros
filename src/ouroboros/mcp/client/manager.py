"""MCP Client Manager for multi-server connection management.

This module provides the MCPClientManager class for managing connections to
multiple MCP servers with connection pooling, health checks, and per-request
timeouts.
"""

import asyncio
from collections.abc import Mapping, Sequence
import contextlib
from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

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
    MCPServerSnapshot,
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
        generation: Monotonic revision used to reject stale async work.
        tools: Tool definitions captured for this connection generation.
        resources: Resource definitions captured for this connection generation.
        server_snapshot: Negotiated protocol snapshot for this generation.
    """

    config: MCPServerConfig
    adapter: MCPClientAdapter
    state: ConnectionState = ConnectionState.DISCONNECTED
    generation: int = 0
    last_error: str | None = None
    tools: tuple[MCPToolDefinition, ...] = field(default_factory=tuple)
    resources: tuple[MCPResourceDefinition, ...] = field(default_factory=tuple)
    server_snapshot: MCPServerSnapshot | None = None


@dataclass(frozen=True, slots=True)
class ServerConnectionSnapshot:
    """Deeply immutable public view of a managed connection.

    The live adapter is intentionally absent: exposing it would let a caller
    mutate transport state outside the manager's generation/lock protocol.
    Every structured value referenced here is recursively frozen before it is
    stored by the manager.
    """

    config: MCPServerConfig
    state: ConnectionState = ConnectionState.DISCONNECTED
    generation: int = 0
    last_error: str | None = None
    tools: tuple[MCPToolDefinition, ...] = field(default_factory=tuple)
    resources: tuple[MCPResourceDefinition, ...] = field(default_factory=tuple)
    server_snapshot: MCPServerSnapshot | None = None


def _deep_freeze(value: Any) -> Any:
    """Return a recursively immutable copy of a protocol/config value."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(item) for item in value)
    if is_dataclass(value) and not isinstance(value, type):
        cls = type(value)
        return cls(**{item.name: _deep_freeze(getattr(value, item.name)) for item in fields(value)})
    return value


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

    def get_connection_snapshot(self, server_name: str) -> ServerConnectionSnapshot | None:
        """Return a deeply immutable, adapter-free connection snapshot."""
        conn = self._connections.get(server_name)
        if conn is None:
            return None
        return ServerConnectionSnapshot(
            config=conn.config,
            state=conn.state,
            generation=conn.generation,
            last_error=conn.last_error,
            tools=conn.tools,
            resources=conn.resources,
            server_snapshot=conn.server_snapshot,
        )

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
            frozen_config = cast(MCPServerConfig, _deep_freeze(config))
            self._connections[config.name] = ServerConnection(
                config=frozen_config,
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

            del self._connections[server_name]
            log.info("mcp.manager.server_removed", server=server_name)

        # Never hold the manager lock across transport cleanup. Any in-flight
        # connect is prevented from reinstalling itself by the identity check.
        if conn.adapter.is_connected:
            await conn.adapter.disconnect()

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
            if conn.state == ConnectionState.CONNECTING:
                return Result.err(
                    MCPConnectionError(
                        f"Connection already in progress: {server_name}",
                        server_name=server_name,
                    )
                )

            generation = conn.generation + 1
            connecting = ServerConnection(
                config=conn.config,
                adapter=conn.adapter,
                state=ConnectionState.CONNECTING,
                generation=generation,
            )
            self._connections[server_name] = connecting

        # All transport I/O, including discovery, stays outside the global lock.
        result = await conn.adapter.connect(conn.config)

        tools: tuple[MCPToolDefinition, ...] = ()
        resources: tuple[MCPResourceDefinition, ...] = ()
        if result.is_ok:
            tools, resources = await asyncio.gather(
                self._fetch_tools(conn.adapter, server_name),
                self._fetch_resources(conn.adapter, server_name),
            )
            tools = tuple(cast(MCPToolDefinition, _deep_freeze(tool)) for tool in tools)
            resources = tuple(
                cast(MCPResourceDefinition, _deep_freeze(resource)) for resource in resources
            )

        superseded = False
        orphaned_adapter = False
        async with self._lock:
            current = self._connections.get(server_name)
            if (
                current is None
                or current.adapter is not conn.adapter
                or current.generation != generation
            ):
                superseded = True
                orphaned_adapter = current is None or current.adapter is not conn.adapter
                log.info(
                    "mcp.manager.connect_superseded",
                    server=server_name,
                    generation=generation,
                )
            elif result.is_ok:
                self._connections[server_name] = ServerConnection(
                    config=conn.config,
                    adapter=conn.adapter,
                    state=ConnectionState.CONNECTED,
                    generation=generation,
                    tools=tools,
                    resources=resources,
                    server_snapshot=cast(
                        MCPServerSnapshot | None,
                        _deep_freeze(getattr(conn.adapter, "server_snapshot", None)),
                    ),
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
                    generation=generation,
                    last_error=str(result.error),
                )
                log.error(
                    "mcp.manager.connect_failed",
                    server=server_name,
                    error=str(result.error),
                )

        if superseded:
            # A removed/replaced adapter is no longer manager-owned. Close it
            # outside the lock; a newer generation on the same adapter still
            # owns that transport and must not be interrupted.
            if orphaned_adapter:
                cleanup = await conn.adapter.disconnect()
                if cleanup.is_err:
                    log.warning(
                        "mcp.manager.superseded_cleanup_failed",
                        server=server_name,
                        generation=generation,
                        error=str(cleanup.error),
                    )
            return Result.err(
                MCPConnectionError(
                    f"Connection attempt was superseded: {server_name}",
                    server_name=server_name,
                )
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

        async with self._lock:
            current = self._connections.get(server_name)
            if current is not conn:
                return Result.ok(None)
            replacement = ServerConnection(
                config=conn.config,
                adapter=MCPClientAdapter(max_retries=self._max_retries),
                state=ConnectionState.DISCONNECTED,
                generation=conn.generation + 1,
            )
            self._connections[server_name] = replacement

        result = await conn.adapter.disconnect()
        if result.is_err:
            async with self._lock:
                current = self._connections.get(server_name)
                if current is replacement:
                    self._connections[server_name] = ServerConnection(
                        config=replacement.config,
                        adapter=replacement.adapter,
                        state=replacement.state,
                        generation=replacement.generation,
                        last_error=str(result.error),
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
        conn_result = await self._connected_or_reconnected(server_name)
        if conn_result.is_err:
            return Result.err(conn_result.error)
        conn = conn_result.value
        effective_timeout = timeout or self._default_timeout

        result = await self._call_tool_once(conn, tool_name, arguments, effective_timeout)
        if result.is_ok or not self._should_reconnect_after_error(result.error):
            return result

        await self._mark_unhealthy_and_reconnect(server_name, conn, result.error)
        return result

    async def _call_tool_once(
        self,
        conn: ServerConnection,
        tool_name: str,
        arguments: dict[str, Any] | None,
        effective_timeout: float,
    ) -> Result[MCPToolResult, MCPClientError]:
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
                    server_name=conn.config.name,
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
        conn_result = await self._connected_or_reconnected(server_name)
        if conn_result.is_err:
            return Result.err(conn_result.error)
        conn = conn_result.value
        effective_timeout = timeout or self._default_timeout

        result = await self._read_resource_once(conn, uri, effective_timeout)
        if result.is_ok or not self._should_reconnect_after_error(result.error):
            return result

        reconnect_result = await self._mark_unhealthy_and_reconnect(server_name, conn, result.error)
        if reconnect_result.is_err:
            return Result.err(reconnect_result.error)
        return await self._read_resource_once(reconnect_result.value, uri, effective_timeout)

    async def _read_resource_once(
        self,
        conn: ServerConnection,
        uri: str,
        effective_timeout: float,
    ) -> Result[MCPResourceContent, MCPClientError]:
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
                    server_name=conn.config.name,
                    timeout_seconds=effective_timeout,
                    operation="read_resource",
                )
            )

    async def _connected_or_reconnected(
        self,
        server_name: str,
    ) -> Result[ServerConnection, MCPClientError]:
        """Return a connected server, reconnecting unhealthy servers first."""
        conn = self._connections.get(server_name)
        if not conn:
            return Result.err(
                MCPClientError(
                    f"Server not found: {server_name}",
                    is_retriable=False,
                    details={"resource_type": "server", "resource_id": server_name},
                )
            )

        if conn.state == ConnectionState.CONNECTED:
            return Result.ok(conn)
        if conn.state == ConnectionState.UNHEALTHY:
            reconnect_result = await self.connect(server_name)
            if reconnect_result.is_err:
                return Result.err(reconnect_result.error)
            refreshed = self._connections.get(server_name)
            if refreshed and refreshed.state == ConnectionState.CONNECTED:
                return Result.ok(refreshed)

        return Result.err(
            MCPConnectionError(
                f"Server not connected: {server_name}",
                server_name=server_name,
            )
        )

    def _should_reconnect_after_error(self, error: MCPClientError) -> bool:
        """Return True when an MCP failure looks like a lost transport."""
        if isinstance(error, MCPConnectionError):
            return True
        error_text = str(error).lower()
        return any(
            token in error_text
            for token in (
                "broken pipe",
                "connection reset",
                "connection closed",
                "end of file",
                "eof",
                "not connected",
                "transport closed",
            )
        )

    async def _mark_unhealthy_and_reconnect(
        self,
        server_name: str,
        conn: ServerConnection,
        error: MCPClientError,
    ) -> Result[ServerConnection, MCPClientError]:
        """Mark a connection unhealthy, reconnect, and return the refreshed connection."""
        async with self._lock:
            current = self._connections.get(server_name)
            if (
                current is None
                or current.adapter is not conn.adapter
                or current.generation != conn.generation
            ):
                if current is not None and current.state == ConnectionState.CONNECTED:
                    return Result.ok(current)
                return Result.err(
                    MCPConnectionError(
                        f"Connection changed while handling failure: {server_name}",
                        server_name=server_name,
                    )
                )
            self._connections[server_name] = ServerConnection(
                config=current.config,
                adapter=current.adapter,
                state=ConnectionState.UNHEALTHY,
                generation=current.generation + 1,
                last_error=str(error),
                tools=current.tools,
                resources=current.resources,
                server_snapshot=current.server_snapshot,
            )

        reconnect_result = await self.connect(server_name)
        if reconnect_result.is_err:
            return Result.err(reconnect_result.error)

        refreshed = self._connections.get(server_name)
        if refreshed is None or refreshed.state != ConnectionState.CONNECTED:
            return Result.err(
                MCPConnectionError(
                    f"Server reconnect did not restore connection: {server_name}",
                    server_name=server_name,
                )
            )
        return Result.ok(refreshed)

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
                        current = self._connections.get(server_name)
                        if (
                            current is None
                            or current.adapter is not conn.adapter
                            or current.generation != conn.generation
                        ):
                            continue
                        self._connections[server_name] = ServerConnection(
                            config=conn.config,
                            adapter=conn.adapter,
                            state=ConnectionState.UNHEALTHY,
                            generation=conn.generation + 1,
                            last_error=str(result.error),
                            tools=conn.tools,
                            resources=conn.resources,
                            server_snapshot=conn.server_snapshot,
                        )
                    log.warning(
                        "mcp.manager.health_check_failed",
                        server=server_name,
                        error=str(result.error),
                    )
                    reconnect_result = await self.connect(server_name)
                    if reconnect_result.is_ok:
                        log.info("mcp.manager.reconnected", server=server_name)
            elif conn.state == ConnectionState.UNHEALTHY:
                # Try to reconnect
                reconnect_result = await self.connect(server_name)
                if reconnect_result.is_ok:
                    log.info("mcp.manager.reconnected", server=server_name)
