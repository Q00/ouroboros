"""Tests for MCP client manager."""

import asyncio

from ouroboros.core.types import Result
from ouroboros.mcp.client.manager import (
    ConnectionState,
    MCPClientManager,
    ServerConnection,
)
from ouroboros.mcp.errors import MCPClientError, MCPConnectionError, MCPTimeoutError
from ouroboros.mcp.types import (
    ContentType,
    MCPCapabilities,
    MCPContentItem,
    MCPResourceContent,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolResult,
    TransportType,
)


class _ToolAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def call_tool(self, _tool_name, _arguments=None):
        self.calls += 1
        return self.result


class _SlowToolAdapter:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, _tool_name, _arguments=None):
        self.calls += 1
        await asyncio.sleep(10)


class _ResourceAdapter:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    async def read_resource(self, _uri):
        self.calls += 1
        return self.result


class _HealthAdapter:
    def __init__(self, result):
        self.result = result

    async def list_tools(self):
        return self.result


class TestConnectionState:
    """Test ConnectionState enum."""

    def test_connection_states(self) -> None:
        """ConnectionState has expected values."""
        assert ConnectionState.DISCONNECTED == "disconnected"
        assert ConnectionState.CONNECTING == "connecting"
        assert ConnectionState.CONNECTED == "connected"
        assert ConnectionState.UNHEALTHY == "unhealthy"
        assert ConnectionState.ERROR == "error"


class TestMCPClientManager:
    """Test MCPClientManager class."""

    def test_manager_initial_state(self) -> None:
        """Manager starts with no servers."""
        manager = MCPClientManager()
        assert len(manager.servers) == 0

    async def test_add_server(self) -> None:
        """add_server adds a server configuration."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )

        result = await manager.add_server(config)

        assert result.is_ok
        assert "test-server" in manager.servers

    async def test_add_duplicate_server_fails(self) -> None:
        """Adding duplicate server name fails."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )

        await manager.add_server(config)
        result = await manager.add_server(config)

        assert result.is_err
        assert "already exists" in str(result.error)

    async def test_remove_server(self) -> None:
        """remove_server removes a server."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )

        await manager.add_server(config)
        result = await manager.remove_server("test-server")

        assert result.is_ok
        assert "test-server" not in manager.servers

    async def test_remove_nonexistent_server_fails(self) -> None:
        """Removing nonexistent server fails."""
        manager = MCPClientManager()
        result = await manager.remove_server("nonexistent")

        assert result.is_err
        assert isinstance(result.error, MCPClientError)
        assert "Server not found" in str(result.error)

    def test_get_connection_state_nonexistent(self) -> None:
        """get_connection_state returns None for nonexistent server."""
        manager = MCPClientManager()
        state = manager.get_connection_state("nonexistent")
        assert state is None

    async def test_get_connection_state_after_add(self) -> None:
        """get_connection_state returns DISCONNECTED after add."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )

        await manager.add_server(config)
        state = manager.get_connection_state("test-server")

        assert state == ConnectionState.DISCONNECTED

    def test_find_tool_server_not_found(self) -> None:
        """find_tool_server returns None when tool not found."""
        manager = MCPClientManager()
        result = manager.find_tool_server("nonexistent_tool")
        assert result is None


class TestMCPClientManagerTools:
    """Test MCPClientManager tool operations."""

    async def test_call_tool_server_not_found(self) -> None:
        """call_tool fails with unknown server."""
        manager = MCPClientManager()
        result = await manager.call_tool("unknown", "tool", {})

        assert result.is_err
        assert isinstance(result.error, MCPClientError)
        assert "Server not found" in str(result.error)

    async def test_call_tool_auto_tool_not_found(self) -> None:
        """call_tool_auto fails when tool not found on any server."""
        manager = MCPClientManager()
        result = await manager.call_tool_auto("unknown_tool", {})

        assert result.is_err
        assert "not found on any server" in str(result.error)

    async def test_list_all_tools_empty(self) -> None:
        """list_all_tools returns empty when no servers connected."""
        manager = MCPClientManager()
        tools = await manager.list_all_tools()
        assert len(tools) == 0

    async def test_call_tool_reconnects_without_replaying_transport_failure(self) -> None:
        """call_tool restores the connection but does not replay side effects."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        stale_adapter = _ToolAdapter(
            Result.err(MCPConnectionError("transport closed", server_name="test-server"))
        )
        fresh_adapter = _ToolAdapter(
            Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                    is_error=False,
                )
            )
        )
        tool = MCPToolDefinition(name="tool", description="")
        manager._connections["test-server"] = ServerConnection(
            config=config,
            adapter=stale_adapter,
            state=ConnectionState.CONNECTED,
            tools=(tool,),
        )

        async def _connect(server_name: str):
            manager._connections[server_name] = ServerConnection(
                config=config,
                adapter=fresh_adapter,
                state=ConnectionState.CONNECTED,
                tools=(tool,),
            )
            return Result.ok(
                MCPServerInfo(
                    name=server_name,
                    version="1.0.0",
                    capabilities=MCPCapabilities(tools=True),
                )
            )

        manager.connect = _connect  # type: ignore[method-assign]

        result = await manager.call_tool("test-server", "tool", {})

        assert result.is_err
        assert isinstance(result.error, MCPConnectionError)
        assert stale_adapter.calls == 1
        assert fresh_adapter.calls == 0
        assert manager.get_connection_state("test-server") == ConnectionState.CONNECTED

    async def test_call_tool_does_not_reconnect_after_timeout_result(self) -> None:
        """Timeout failures are not retried because tools may have side effects."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        timeout_adapter = _ToolAdapter(
            Result.err(
                MCPTimeoutError(
                    "Tool call timed out: tool",
                    server_name="test-server",
                    timeout_seconds=0.01,
                    operation="call_tool",
                )
            )
        )
        manager._connections["test-server"] = ServerConnection(
            config=config,
            adapter=timeout_adapter,
            state=ConnectionState.CONNECTED,
        )
        reconnects: list[str] = []

        async def _connect(server_name: str):
            reconnects.append(server_name)
            return Result.err(MCPConnectionError("should not reconnect", server_name=server_name))

        manager.connect = _connect  # type: ignore[method-assign]

        result = await manager.call_tool("test-server", "tool", {})

        assert result.is_err
        assert isinstance(result.error, MCPTimeoutError)
        assert timeout_adapter.calls == 1
        assert reconnects == []

    async def test_call_tool_timeout_preserves_server_name_without_retry(self) -> None:
        """wait_for timeouts return MCPTimeoutError without reconnecting."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        slow_adapter = _SlowToolAdapter()
        manager._connections["test-server"] = ServerConnection(
            config=config,
            adapter=slow_adapter,
            state=ConnectionState.CONNECTED,
        )
        reconnects: list[str] = []

        async def _connect(server_name: str):
            reconnects.append(server_name)
            return Result.err(MCPConnectionError("should not reconnect", server_name=server_name))

        manager.connect = _connect  # type: ignore[method-assign]

        result = await manager.call_tool("test-server", "tool", {}, timeout=0.01)

        assert result.is_err
        assert isinstance(result.error, MCPTimeoutError)
        assert result.error.server_name == "test-server"
        assert slow_adapter.calls == 1
        assert reconnects == []


class TestMCPClientManagerResources:
    """Test MCPClientManager resource operations."""

    async def test_read_resource_server_not_found(self) -> None:
        """read_resource fails with unknown server."""
        manager = MCPClientManager()
        result = await manager.read_resource("unknown", "uri")

        assert result.is_err
        assert isinstance(result.error, MCPClientError)
        assert "Server not found" in str(result.error)

    async def test_list_all_resources_empty(self) -> None:
        """list_all_resources returns empty when no servers connected."""
        manager = MCPClientManager()
        resources = await manager.list_all_resources()
        assert len(resources) == 0

    async def test_read_resource_reconnects_once_after_transport_failure(self) -> None:
        """read_resource reconnects and retries once when the transport is closed."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        stale_adapter = _ResourceAdapter(
            Result.err(MCPConnectionError("transport closed", server_name="test-server"))
        )
        fresh_adapter = _ResourceAdapter(Result.ok(MCPResourceContent(uri="file://doc", text="ok")))
        manager._connections["test-server"] = ServerConnection(
            config=config,
            adapter=stale_adapter,
            state=ConnectionState.CONNECTED,
        )

        async def _connect(server_name: str):
            manager._connections[server_name] = ServerConnection(
                config=config,
                adapter=fresh_adapter,
                state=ConnectionState.CONNECTED,
            )
            return Result.ok(
                MCPServerInfo(
                    name=server_name,
                    version="1.0.0",
                    capabilities=MCPCapabilities(resources=True),
                )
            )

        manager.connect = _connect  # type: ignore[method-assign]

        result = await manager.read_resource("test-server", "file://doc")

        assert result.is_ok
        assert result.value.text == "ok"
        assert stale_adapter.calls == 1
        assert fresh_adapter.calls == 1


class TestMCPClientManagerHealthChecks:
    """Test MCPClientManager health-check recovery."""

    async def test_health_check_reconnects_immediately_after_failure(self) -> None:
        """A failed heartbeat check attempts reconnect in the same pass."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="test-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        manager._connections["test-server"] = ServerConnection(
            config=config,
            adapter=_HealthAdapter(
                Result.err(MCPConnectionError("transport closed", server_name="test-server"))
            ),
            state=ConnectionState.CONNECTED,
        )
        reconnects: list[str] = []

        async def _connect(server_name: str):
            reconnects.append(server_name)
            manager._connections[server_name] = ServerConnection(
                config=config,
                adapter=_HealthAdapter(Result.ok(())),
                state=ConnectionState.CONNECTED,
            )
            return Result.ok(
                MCPServerInfo(
                    name=server_name,
                    version="1.0.0",
                    capabilities=MCPCapabilities(tools=True),
                )
            )

        manager.connect = _connect  # type: ignore[method-assign]

        await manager._perform_health_checks()

        assert reconnects == ["test-server"]
        assert manager.get_connection_state("test-server") == ConnectionState.CONNECTED
