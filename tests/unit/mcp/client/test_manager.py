"""Tests for MCP client manager."""

import asyncio

import pytest

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
    MCPPeerIdentity,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerConfig,
    MCPServerInfo,
    MCPServerSnapshot,
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


class _DiscoveryAdapter:
    """Controllable adapter for connection/discovery race tests."""

    def __init__(self) -> None:
        self.discovery_started = asyncio.Event()
        self.release_discovery = asyncio.Event()
        self.is_connected = False
        self.disconnect_calls = 0
        self.server_snapshot = None

    async def connect(self, config):
        self.is_connected = True
        return Result.ok(
            MCPServerInfo(
                name=config.name,
                version="1.0.0",
                capabilities=MCPCapabilities(tools=True, resources=True),
            )
        )

    async def list_tools(self):
        self.discovery_started.set()
        await self.release_discovery.wait()
        return Result.ok((MCPToolDefinition(name="discovered", description=""),))

    async def list_resources(self):
        return Result.ok(())

    async def disconnect(self):
        self.disconnect_calls += 1
        self.is_connected = False
        return Result.ok(None)


class _ImmutableSnapshotAdapter:
    """Adapter returning nested mutable models for manager-freeze coverage."""

    is_connected = True

    def __init__(self) -> None:
        self.server_snapshot = MCPServerSnapshot(
            identity=MCPPeerIdentity(
                name="fixture",
                application_version="1",
                icons=({"src": "fixture.svg", "sizes": ["any"]},),
                details={"icons": [{"src": "fixture.svg", "sizes": ["any"]}]},
            ),
            protocol_version="2026-07-28",
            supported_protocol_versions=("2026-07-28",),
            capabilities=MCPCapabilities(tools=True, details={"tools": {"listChanged": True}}),
            extensions={"vendor": {"flag": True}},
            meta={"trace": {"id": "fixture"}},
            discovery_details={"capabilities": {"tools": {"listChanged": True}}},
        )

    async def connect(self, config):
        return Result.ok(MCPServerInfo(name=config.name))

    async def list_tools(self):
        return Result.ok(
            (
                MCPToolDefinition(
                    name="nested",
                    description="",
                    input_schema={
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                    meta={"vendor": {"stable": True}},
                ),
            )
        )

    async def list_resources(self):
        return Result.ok(
            (
                MCPResourceDefinition(
                    uri="fixture://resource",
                    name="resource",
                    meta={"cache": {"scope": "private"}},
                ),
            )
        )


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

    async def test_public_snapshot_deeply_freezes_manager_owned_state(self) -> None:
        """Snapshot mutation cannot alter reconnect config or discovery caches."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="immutable",
            transport=TransportType.STDIO,
            command="test-cmd",
            env={"TOKEN": "safe"},
            headers={"X-Test": "safe"},
        )
        assert (await manager.add_server(config)).is_ok

        # The caller-owned input is detached at registration time.
        config.env["TOKEN"] = "caller-mutated"
        registered = manager._connections[config.name]
        adapter = _ImmutableSnapshotAdapter()
        manager._connections[config.name] = ServerConnection(
            config=registered.config,
            adapter=adapter,  # type: ignore[arg-type]
        )
        assert (await manager.connect(config.name)).is_ok

        snapshot = manager.get_connection_snapshot(config.name)
        assert snapshot is not None
        assert not hasattr(snapshot, "adapter")
        assert snapshot.config.env["TOKEN"] == "safe"
        with pytest.raises(TypeError):
            snapshot.config.env["TOKEN"] = "snapshot-mutated"
        with pytest.raises(TypeError):
            snapshot.config.headers["X-Test"] = "snapshot-mutated"
        with pytest.raises(TypeError):
            snapshot.tools[0].input_schema["properties"]["path"]["type"] = "number"  # type: ignore[index]
        with pytest.raises(TypeError):
            snapshot.tools[0].meta["vendor"]["stable"] = False  # type: ignore[index]
        with pytest.raises(TypeError):
            snapshot.resources[0].meta["cache"]["scope"] = "public"  # type: ignore[index]
        assert snapshot.server_snapshot is not None
        with pytest.raises(TypeError):
            snapshot.server_snapshot.extensions["vendor"]["flag"] = False
        assert snapshot.server_snapshot.identity is not None
        with pytest.raises(TypeError):
            snapshot.server_snapshot.identity.icons[0]["sizes"] = ()
        with pytest.raises(TypeError):
            snapshot.server_snapshot.identity.details["icons"][0]["src"] = "mutated.svg"
        with pytest.raises(TypeError):
            snapshot.server_snapshot.meta["trace"]["id"] = "mutated"
        with pytest.raises(TypeError):
            snapshot.server_snapshot.discovery_details["capabilities"]["tools"] = {}

        internal = manager._connections[config.name]
        assert internal.config.env["TOKEN"] == "safe"
        assert internal.tools[0].input_schema["properties"]["path"]["type"] == "string"  # type: ignore[index]

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

    async def test_discovery_does_not_hold_global_manager_lock(self) -> None:
        """A slow server discovery does not block unrelated manager mutations."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="slow-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        adapter = _DiscoveryAdapter()
        manager._connections[config.name] = ServerConnection(config=config, adapter=adapter)  # type: ignore[arg-type]

        connect_task = asyncio.create_task(manager.connect(config.name))
        await adapter.discovery_started.wait()

        unrelated = MCPServerConfig(
            name="unrelated",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        add_result = await asyncio.wait_for(manager.add_server(unrelated), timeout=0.1)
        assert add_result.is_ok

        adapter.release_discovery.set()
        assert (await connect_task).is_ok

    async def test_disconnect_supersedes_in_flight_discovery_snapshot(self) -> None:
        """Late discovery cannot overwrite a newer disconnected generation."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="race-server",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        adapter = _DiscoveryAdapter()
        manager._connections[config.name] = ServerConnection(config=config, adapter=adapter)  # type: ignore[arg-type]

        connect_task = asyncio.create_task(manager.connect(config.name))
        await adapter.discovery_started.wait()
        connecting = manager.get_connection_snapshot(config.name)
        assert connecting is not None
        assert connecting.generation == 1

        disconnect_result = await manager.disconnect(config.name)
        assert disconnect_result.is_ok
        disconnected = manager.get_connection_snapshot(config.name)
        assert disconnected is not None
        assert disconnected.generation == 2
        assert disconnected.state == ConnectionState.DISCONNECTED

        adapter.release_discovery.set()
        assert (await connect_task).is_err
        current = manager.get_connection_snapshot(config.name)
        assert current is not None
        assert current.generation == disconnected.generation
        assert current.state == disconnected.state
        assert adapter.disconnect_calls == 2

    async def test_remove_and_readd_rejects_old_connection_snapshot(self) -> None:
        """Late discovery from a removed adapter cannot resurrect or replace a server."""
        manager = MCPClientManager()
        config = MCPServerConfig(
            name="reused-name",
            transport=TransportType.STDIO,
            command="test-cmd",
        )
        adapter = _DiscoveryAdapter()
        manager._connections[config.name] = ServerConnection(config=config, adapter=adapter)  # type: ignore[arg-type]

        connect_task = asyncio.create_task(manager.connect(config.name))
        await adapter.discovery_started.wait()
        assert (await manager.remove_server(config.name)).is_ok
        assert (await manager.add_server(config)).is_ok
        replacement = manager.get_connection_snapshot(config.name)

        adapter.release_discovery.set()
        assert (await connect_task).is_err
        assert manager.get_connection_snapshot(config.name) == replacement
        assert replacement is not None
        assert replacement.state == ConnectionState.DISCONNECTED
        assert manager._connections[config.name].adapter is not adapter
        assert adapter.disconnect_calls == 2

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
