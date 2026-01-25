"""Tests for MCP client manager."""

import pytest

from ouroboros.mcp.client.manager import (
    ConnectionState,
    MCPClientManager,
    ServerConnection,
)
from ouroboros.mcp.errors import MCPClientError
from ouroboros.mcp.types import MCPServerConfig, TransportType


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
