"""Integration tests for MCPClientManager.

These tests verify that the MCPClientManager correctly manages multiple
MCP server connections, including connection pooling, tool aggregation,
and health checks.
"""

import asyncio
import sys
from contextlib import asynccontextmanager, contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.client.manager import (
    ConnectionState,
    MCPClientManager,
    ServerConnection,
)
from ouroboros.mcp.errors import MCPClientError, MCPConnectionError, MCPTimeoutError
from ouroboros.mcp.types import (
    MCPServerConfig,
    MCPToolDefinition,
    MCPToolParameter,
    ToolInputType,
    TransportType,
)

from .conftest import (
    MockMCPServerState,
    create_mock_client_session_class,
    create_mock_session_factory,
)


@contextmanager
def install_mcp_mocks(server_state: MockMCPServerState):
    """Install MCP SDK module mocks and clean up after.

    Args:
        server_state: The mock server state to use.

    Yields:
        Tuple of (mock_mcp, mock_stdio) modules.
    """
    mock_session_class = create_mock_client_session_class(server_state)

    @asynccontextmanager
    async def mock_stdio_client(params: Any):
        read_stream = MagicMock()
        write_stream = MagicMock()
        yield (read_stream, write_stream)

    mock_mcp = MagicMock()
    mock_mcp.ClientSession = mock_session_class
    mock_mcp.StdioServerParameters = MagicMock()

    mock_mcp_client_stdio = MagicMock()
    mock_mcp_client_stdio.stdio_client = mock_stdio_client

    original_mcp = sys.modules.get("mcp")
    original_mcp_client = sys.modules.get("mcp.client")
    original_mcp_client_stdio = sys.modules.get("mcp.client.stdio")

    try:
        sys.modules["mcp"] = mock_mcp
        sys.modules["mcp.client"] = MagicMock()
        sys.modules["mcp.client.stdio"] = mock_mcp_client_stdio
        yield mock_mcp, mock_mcp_client_stdio
    finally:
        if original_mcp is not None:
            sys.modules["mcp"] = original_mcp
        else:
            sys.modules.pop("mcp", None)
        if original_mcp_client is not None:
            sys.modules["mcp.client"] = original_mcp_client
        else:
            sys.modules.pop("mcp.client", None)
        if original_mcp_client_stdio is not None:
            sys.modules["mcp.client.stdio"] = original_mcp_client_stdio
        else:
            sys.modules.pop("mcp.client.stdio", None)


# ---------------------------------------------------------------------------
# Helper Functions
# ---------------------------------------------------------------------------


def create_server_config(name: str) -> MCPServerConfig:
    """Create a test server configuration.

    Args:
        name: Server name.

    Returns:
        MCPServerConfig for testing.
    """
    return MCPServerConfig(
        name=name,
        transport=TransportType.STDIO,
        command="python",
        args=("-m", f"mock_server_{name}"),
        timeout=30.0,
    )


def create_mock_server_with_tools(name: str, tools: list[tuple[str, str]]) -> MockMCPServerState:
    """Create a mock server state with specified tools.

    Args:
        name: Server name.
        tools: List of (tool_name, description) tuples.

    Returns:
        Configured MockMCPServerState.
    """
    state = MockMCPServerState(name=name)

    for tool_name, description in tools:
        state.register_tool(
            name=tool_name,
            description=description,
            parameters=(
                MCPToolParameter(
                    name="input",
                    type=ToolInputType.STRING,
                    description="Input value",
                    required=False,
                ),
            ),
            handler=lambda args, tn=tool_name: f"{tn}: {args.get('input', '')}",
        )

    return state


# ---------------------------------------------------------------------------
# Test Classes
# ---------------------------------------------------------------------------


class TestMCPClientManagerServerManagement:
    """Test MCPClientManager server management operations."""

    @pytest.mark.asyncio
    async def test_add_single_server(self) -> None:
        """Single server can be added to manager."""
        manager = MCPClientManager()
        config = create_server_config("server1")

        result = await manager.add_server(config)

        assert result.is_ok
        assert "server1" in manager.servers
        assert manager.get_connection_state("server1") == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_add_multiple_servers(self) -> None:
        """Multiple servers can be added to manager."""
        manager = MCPClientManager()

        for i in range(3):
            config = create_server_config(f"server{i}")
            result = await manager.add_server(config)
            assert result.is_ok

        assert len(manager.servers) == 3
        assert "server0" in manager.servers
        assert "server1" in manager.servers
        assert "server2" in manager.servers

    @pytest.mark.asyncio
    async def test_add_duplicate_server_fails(self) -> None:
        """Adding server with duplicate name fails."""
        manager = MCPClientManager()
        config = create_server_config("server1")

        await manager.add_server(config)
        result = await manager.add_server(config)

        assert result.is_err
        assert "already exists" in str(result.error)

    @pytest.mark.asyncio
    async def test_remove_server(self) -> None:
        """Server can be removed from manager."""
        manager = MCPClientManager()
        config = create_server_config("server1")

        await manager.add_server(config)
        assert "server1" in manager.servers

        result = await manager.remove_server("server1")

        assert result.is_ok
        assert "server1" not in manager.servers

    @pytest.mark.asyncio
    async def test_remove_nonexistent_server_fails(self) -> None:
        """Removing nonexistent server fails."""
        manager = MCPClientManager()

        result = await manager.remove_server("nonexistent")

        assert result.is_err
        assert "Server not found" in str(result.error)


class TestMCPClientManagerConnection:
    """Test MCPClientManager connection operations."""

    @pytest.mark.asyncio
    async def test_connect_to_server(self) -> None:
        """Manager can connect to a registered server."""
        manager = MCPClientManager()
        config = create_server_config("server1")
        server_state = create_mock_server_with_tools("server1", [("tool1", "Test tool")])

        await manager.add_server(config)

        with install_mcp_mocks(server_state):
            result = await manager.connect("server1")

            assert result.is_ok
            assert manager.get_connection_state("server1") == ConnectionState.CONNECTED

    @pytest.mark.asyncio
    async def test_connect_caches_tools(self) -> None:
        """Connection caches available tools from server."""
        manager = MCPClientManager()
        config = create_server_config("server1")
        server_state = create_mock_server_with_tools(
            "server1",
            [("tool1", "First tool"), ("tool2", "Second tool")],
        )

        await manager.add_server(config)

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            tools = await manager.list_all_tools()
            assert len(tools) == 2
            tool_names = {t.name for t in tools}
            assert "tool1" in tool_names
            assert "tool2" in tool_names

    @pytest.mark.asyncio
    async def test_connect_nonexistent_server_fails(self) -> None:
        """Connecting to unregistered server fails."""
        manager = MCPClientManager()

        result = await manager.connect("nonexistent")

        assert result.is_err
        assert "Server not found" in str(result.error)

    @pytest.mark.asyncio
    async def test_disconnect_from_server(self) -> None:
        """Manager can disconnect from a connected server."""
        manager = MCPClientManager()
        config = create_server_config("server1")
        server_state = create_mock_server_with_tools("server1", [("tool1", "Test")])

        await manager.add_server(config)

        with install_mcp_mocks(server_state):
            await manager.connect("server1")
            assert manager.get_connection_state("server1") == ConnectionState.CONNECTED

            result = await manager.disconnect("server1")

            assert result.is_ok
            assert manager.get_connection_state("server1") == ConnectionState.DISCONNECTED


class TestMCPClientManagerMultiServer:
    """Test MCPClientManager with multiple servers."""

    @pytest.mark.asyncio
    async def test_connect_all_servers(self) -> None:
        """Manager can connect to all registered servers."""
        manager = MCPClientManager()

        # Create mock states for multiple servers
        server_states = {
            "server1": create_mock_server_with_tools("server1", [("s1_tool", "Server 1 tool")]),
            "server2": create_mock_server_with_tools("server2", [("s2_tool", "Server 2 tool")]),
        }

        for name in server_states:
            await manager.add_server(create_server_config(name))

        # Use a shared server state that can work for both
        combined_state = create_mock_server_with_tools(
            "combined",
            [("s1_tool", "Server 1 tool"), ("s2_tool", "Server 2 tool")],
        )

        with install_mcp_mocks(combined_state):
            results = await manager.connect_all()

            # Both servers should be connected
            assert len(results) == 2
            assert all(r.is_ok for r in results.values())

    @pytest.mark.asyncio
    async def test_disconnect_all_servers(self) -> None:
        """Manager can disconnect from all servers."""
        manager = MCPClientManager()
        server_state = create_mock_server_with_tools("server1", [("tool1", "Test")])

        await manager.add_server(create_server_config("server1"))
        await manager.add_server(create_server_config("server2"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")
            await manager.connect("server2")

            results = await manager.disconnect_all()

            assert len(results) == 2
            assert manager.get_connection_state("server1") == ConnectionState.DISCONNECTED
            assert manager.get_connection_state("server2") == ConnectionState.DISCONNECTED

    @pytest.mark.asyncio
    async def test_list_tools_from_multiple_servers(self) -> None:
        """Manager aggregates tools from all connected servers."""
        manager = MCPClientManager()

        # Server 1 with tools A and B
        state1 = create_mock_server_with_tools("server1", [("toolA", "Tool A"), ("toolB", "Tool B")])
        # Server 2 with tools C and D
        state2 = create_mock_server_with_tools("server2", [("toolC", "Tool C"), ("toolD", "Tool D")])

        await manager.add_server(create_server_config("server1"))
        await manager.add_server(create_server_config("server2"))

        # Connect to server1
        with install_mcp_mocks(state1):
            await manager.connect("server1")

        # Connect to server2
        with install_mcp_mocks(state2):
            await manager.connect("server2")

        # List all tools
        tools = await manager.list_all_tools()

        assert len(tools) == 4
        tool_names = {t.name for t in tools}
        assert "toolA" in tool_names
        assert "toolB" in tool_names
        assert "toolC" in tool_names
        assert "toolD" in tool_names


class TestMCPClientManagerToolOperations:
    """Test MCPClientManager tool operations."""

    @pytest.mark.asyncio
    async def test_call_tool_on_specific_server(self) -> None:
        """Manager can call tool on a specific server."""
        manager = MCPClientManager()
        server_state = create_mock_server_with_tools("server1", [("echo", "Echo tool")])

        await manager.add_server(create_server_config("server1"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            result = await manager.call_tool("server1", "echo", {"input": "hello"})

            assert result.is_ok
            assert "echo" in result.value.text_content.lower()

    @pytest.mark.asyncio
    async def test_call_tool_on_unknown_server_fails(self) -> None:
        """Calling tool on unknown server fails."""
        manager = MCPClientManager()

        result = await manager.call_tool("nonexistent", "tool", {})

        assert result.is_err
        assert "Server not found" in str(result.error)

    @pytest.mark.asyncio
    async def test_call_tool_on_disconnected_server_fails(self) -> None:
        """Calling tool on disconnected server fails."""
        manager = MCPClientManager()
        await manager.add_server(create_server_config("server1"))

        result = await manager.call_tool("server1", "tool", {})

        assert result.is_err
        assert isinstance(result.error, MCPConnectionError)

    @pytest.mark.asyncio
    async def test_find_tool_server(self) -> None:
        """Manager can find which server provides a tool."""
        manager = MCPClientManager()
        server_state = create_mock_server_with_tools(
            "server1",
            [("unique_tool", "A unique tool")],
        )

        await manager.add_server(create_server_config("server1"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            server = manager.find_tool_server("unique_tool")

            assert server == "server1"

    @pytest.mark.asyncio
    async def test_find_tool_server_not_found(self) -> None:
        """find_tool_server returns None for unknown tool."""
        manager = MCPClientManager()

        server = manager.find_tool_server("nonexistent_tool")

        assert server is None

    @pytest.mark.asyncio
    async def test_call_tool_auto(self) -> None:
        """Manager can auto-route tool calls to correct server."""
        manager = MCPClientManager()
        server_state = create_mock_server_with_tools(
            "server1",
            [("auto_tool", "Auto-routed tool")],
        )

        await manager.add_server(create_server_config("server1"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            result = await manager.call_tool_auto("auto_tool", {"input": "auto"})

            assert result.is_ok

    @pytest.mark.asyncio
    async def test_call_tool_auto_not_found(self) -> None:
        """call_tool_auto returns error for unknown tool."""
        manager = MCPClientManager()

        result = await manager.call_tool_auto("nonexistent", {})

        assert result.is_err
        assert "not found on any server" in str(result.error)


class TestMCPClientManagerResourceOperations:
    """Test MCPClientManager resource operations."""

    @pytest.mark.asyncio
    async def test_list_all_resources(self) -> None:
        """Manager aggregates resources from all servers."""
        manager = MCPClientManager()
        server_state = MockMCPServerState(name="server1")
        server_state.register_resource(
            uri="test://resource1",
            name="Resource 1",
            description="First resource",
            content="Content 1",
        )

        await manager.add_server(create_server_config("server1"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            resources = await manager.list_all_resources()

            assert len(resources) == 1
            assert resources[0].uri == "test://resource1"

    @pytest.mark.asyncio
    async def test_read_resource_from_server(self) -> None:
        """Manager can read resource from specific server."""
        manager = MCPClientManager()
        server_state = MockMCPServerState(name="server1")
        server_state.register_resource(
            uri="test://data",
            name="Data",
            description="Test data",
            content="Hello, World!",
        )

        await manager.add_server(create_server_config("server1"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            result = await manager.read_resource("server1", "test://data")

            assert result.is_ok
            assert result.value.text == "Hello, World!"


class TestMCPClientManagerTimeout:
    """Test MCPClientManager timeout handling."""

    @pytest.mark.asyncio
    async def test_tool_call_timeout(self) -> None:
        """Tool call respects timeout setting."""
        manager = MCPClientManager(default_timeout=0.1)  # Very short timeout
        server_state = MockMCPServerState(name="server1")

        # Add a tool with a slow handler
        async def slow_handler(args: dict[str, Any]) -> str:
            await asyncio.sleep(1.0)  # Longer than timeout
            return "slow result"

        server_state.register_tool(
            name="slow_tool",
            description="A slow tool",
            parameters=(),
            handler=slow_handler,
        )

        await manager.add_server(create_server_config("server1"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            result = await manager.call_tool("server1", "slow_tool", {})

            assert result.is_err
            assert isinstance(result.error, MCPTimeoutError)

    @pytest.mark.asyncio
    async def test_custom_timeout_override(self) -> None:
        """Per-call timeout can override default."""
        manager = MCPClientManager(default_timeout=10.0)  # Long default
        server_state = create_mock_server_with_tools("server1", [("tool", "Test")])

        await manager.add_server(create_server_config("server1"))

        with install_mcp_mocks(server_state):
            await manager.connect("server1")

            # Call with short timeout - should work for fast tool
            result = await manager.call_tool(
                "server1",
                "tool",
                {"input": "test"},
                timeout=5.0,
            )

            assert result.is_ok


class TestMCPClientManagerHealthChecks:
    """Test MCPClientManager health check functionality."""

    @pytest.mark.asyncio
    async def test_start_health_checks(self) -> None:
        """Health check task can be started."""
        manager = MCPClientManager(health_check_interval=0.1)

        manager.start_health_checks()

        # Give it time to start
        await asyncio.sleep(0.05)

        assert manager._health_check_task is not None
        assert not manager._health_check_task.done()

        # Cleanup
        await manager.disconnect_all()

    @pytest.mark.asyncio
    async def test_disconnect_all_stops_health_checks(self) -> None:
        """Health checks are stopped on disconnect_all."""
        manager = MCPClientManager(health_check_interval=0.1)

        manager.start_health_checks()
        await asyncio.sleep(0.05)

        task = manager._health_check_task

        await manager.disconnect_all()

        assert manager._health_check_task is None
        # Task is done (either cancelled or finished normally after catching CancelledError)
        assert task is not None and task.done()


class TestMCPClientManagerIntegration:
    """Integration tests for complete manager workflows."""

    @pytest.mark.asyncio
    async def test_full_workflow_single_server(self) -> None:
        """Complete workflow with a single server."""
        manager = MCPClientManager()
        server_state = MockMCPServerState(name="workflow-server")

        server_state.register_tool(
            name="greet",
            description="Greet someone",
            parameters=(
                MCPToolParameter(
                    name="name",
                    type=ToolInputType.STRING,
                    description="Name to greet",
                    required=True,
                ),
            ),
            handler=lambda args: f"Hello, {args.get('name', 'World')}!",
        )

        server_state.register_resource(
            uri="workflow://status",
            name="Status",
            description="Current status",
            content="Active",
        )

        config = create_server_config("workflow-server")
        await manager.add_server(config)

        with install_mcp_mocks(server_state):
            # Connect
            connect_result = await manager.connect("workflow-server")
            assert connect_result.is_ok

            # List tools
            tools = await manager.list_all_tools()
            assert len(tools) == 1
            assert tools[0].name == "greet"

            # Call tool
            tool_result = await manager.call_tool(
                "workflow-server",
                "greet",
                {"name": "Integration"},
            )
            assert tool_result.is_ok
            assert "Hello, Integration!" in tool_result.value.text_content

            # List resources
            resources = await manager.list_all_resources()
            assert len(resources) == 1

            # Read resource
            resource_result = await manager.read_resource(
                "workflow-server",
                "workflow://status",
            )
            assert resource_result.is_ok
            assert resource_result.value.text == "Active"

            # Disconnect
            disconnect_result = await manager.disconnect("workflow-server")
            assert disconnect_result.is_ok
            assert manager.get_connection_state("workflow-server") == ConnectionState.DISCONNECTED
