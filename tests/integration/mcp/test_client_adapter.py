"""Integration tests for MCPClientAdapter.

These tests verify that the MCPClientAdapter correctly integrates with
mock MCP servers, testing the full flow of connection, tool calling,
resource reading, and prompt handling.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.mcp.client.adapter import MCPClientAdapter, create_mcp_client
from ouroboros.mcp.errors import MCPClientError, MCPConnectionError
from ouroboros.mcp.types import (
    ContentType,
    MCPServerConfig,
    TransportType,
)

from .conftest import (
    MockMCPServerState,
    MockMCPSession,
    create_mock_client_session_class,
    create_mock_session_factory,
)


def create_mcp_module_mocks(server_state: MockMCPServerState):
    """Create mock MCP SDK modules that can be injected into sys.modules.

    This is necessary because the adapter imports MCP SDK inside _raw_connect,
    so we need to provide mock modules before that import happens.

    Args:
        server_state: The mock server state to use.

    Returns:
        Tuple of (mock_mcp_module, mock_stdio_module, cleanup_func)
    """
    mock_session_class = create_mock_client_session_class(server_state)

    @asynccontextmanager
    async def mock_stdio_client(params: Any):
        """Mock stdio_client context manager."""
        read_stream = MagicMock()
        write_stream = MagicMock()
        yield (read_stream, write_stream)

    # Create mock modules
    mock_mcp = MagicMock()
    mock_mcp.ClientSession = mock_session_class
    mock_mcp.StdioServerParameters = MagicMock()

    mock_mcp_client = MagicMock()
    mock_mcp_client_stdio = MagicMock()
    mock_mcp_client_stdio.stdio_client = mock_stdio_client

    # Store original modules if they exist
    original_mcp = sys.modules.get("mcp")
    original_mcp_client = sys.modules.get("mcp.client")
    original_mcp_client_stdio = sys.modules.get("mcp.client.stdio")

    # Install mock modules
    sys.modules["mcp"] = mock_mcp
    sys.modules["mcp.client"] = mock_mcp_client
    sys.modules["mcp.client.stdio"] = mock_mcp_client_stdio

    def cleanup():
        """Restore original modules."""
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

    return mock_mcp, mock_mcp_client_stdio, cleanup


@pytest.fixture
def mcp_mocks(configured_mock_server: MockMCPServerState):
    """Fixture that sets up MCP module mocks and cleans up after test."""
    mock_mcp, mock_stdio, cleanup = create_mcp_module_mocks(configured_mock_server)
    yield mock_mcp, mock_stdio
    cleanup()


@pytest.fixture
def empty_mcp_mocks(mock_server_state: MockMCPServerState):
    """Fixture for empty server mocks."""
    mock_mcp, mock_stdio, cleanup = create_mcp_module_mocks(mock_server_state)
    yield mock_mcp, mock_stdio
    cleanup()


class TestMCPClientAdapterConnection:
    """Test MCPClientAdapter connection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_to_mock_server(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can connect to a mock MCP server."""
        adapter = MCPClientAdapter()

        async with adapter:
            result = await adapter.connect(stdio_server_config)

            assert result.is_ok
            assert adapter.is_connected
            assert adapter.server_info is not None
            assert adapter.server_info.name == "test-server"

    @pytest.mark.asyncio
    async def test_connect_initializes_session(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Connection initializes the MCP session properly."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            # Verify server was initialized
            assert configured_mock_server.initialized is True

    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Disconnect properly cleans up the connection state."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)
            assert adapter.is_connected

            result = await adapter.disconnect()
            assert result.is_ok
            assert not adapter.is_connected
            assert adapter.server_info is None

    @pytest.mark.asyncio
    async def test_context_manager_auto_disconnects(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Context manager automatically disconnects on exit."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)
            assert adapter.is_connected

        # After context exit
        assert not adapter.is_connected


class TestMCPClientAdapterTools:
    """Test MCPClientAdapter tool operations."""

    @pytest.mark.asyncio
    async def test_list_tools(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can list tools from connected server."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.list_tools()

            assert result.is_ok
            tools = result.value
            assert len(tools) == 2

            tool_names = {t.name for t in tools}
            assert "echo" in tool_names
            assert "add" in tool_names

    @pytest.mark.asyncio
    async def test_call_tool_echo(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can call echo tool and receive result."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.call_tool(
                "echo",
                {"message": "Hello, MCP!"},
            )

            assert result.is_ok
            tool_result = result.value
            assert tool_result.text_content == "Echo: Hello, MCP!"
            assert tool_result.is_error is False

    @pytest.mark.asyncio
    async def test_call_tool_add(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can call add tool with numeric arguments."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.call_tool("add", {"a": 5, "b": 3})

            assert result.is_ok
            tool_result = result.value
            assert tool_result.text_content == "8"

    @pytest.mark.asyncio
    async def test_call_unknown_tool_returns_error(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Calling unknown tool returns appropriate error."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.call_tool("nonexistent_tool", {})

            assert result.is_err
            assert "not found" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_tool_call_logging(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Tool calls are logged in server state."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            # Clear any initialization logs
            configured_mock_server.call_log.clear()

            await adapter.call_tool("echo", {"message": "test"})

            assert len(configured_mock_server.call_log) == 1
            log_entry = configured_mock_server.call_log[0]
            assert log_entry["type"] == "call_tool"
            assert log_entry["name"] == "echo"
            assert log_entry["arguments"] == {"message": "test"}


class TestMCPClientAdapterResources:
    """Test MCPClientAdapter resource operations."""

    @pytest.mark.asyncio
    async def test_list_resources(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can list resources from connected server."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.list_resources()

            assert result.is_ok
            resources = result.value
            assert len(resources) == 2

            uris = {r.uri for r in resources}
            assert "test://config" in uris
            assert "test://status" in uris

    @pytest.mark.asyncio
    async def test_read_resource(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can read resource content."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.read_resource("test://config")

            assert result.is_ok
            content = result.value
            assert content.uri == "test://config"
            assert content.text == '{"version": "1.0.0", "debug": false}'

    @pytest.mark.asyncio
    async def test_read_unknown_resource_returns_error(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Reading unknown resource returns appropriate error."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.read_resource("test://nonexistent")

            assert result.is_err
            assert "not found" in str(result.error).lower()


class TestMCPClientAdapterPrompts:
    """Test MCPClientAdapter prompt operations."""

    @pytest.mark.asyncio
    async def test_list_prompts(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can list prompts from connected server."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.list_prompts()

            assert result.is_ok
            prompts = result.value
            assert len(prompts) == 1
            assert prompts[0].name == "greeting"

    @pytest.mark.asyncio
    async def test_get_prompt(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client can get a filled prompt."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.get_prompt(
                "greeting",
                {"name": "Alice"},
            )

            assert result.is_ok
            prompt_text = result.value
            assert "Hello, Alice!" in prompt_text
            assert "Welcome to the system" in prompt_text

    @pytest.mark.asyncio
    async def test_get_unknown_prompt_returns_error(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Getting unknown prompt returns appropriate error."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            result = await adapter.get_prompt("nonexistent", {})

            assert result.is_err
            assert "not found" in str(result.error).lower()


class TestMCPClientAdapterRetry:
    """Test MCPClientAdapter retry behavior."""

    @pytest.mark.asyncio
    async def test_retry_on_transient_failure(self) -> None:
        """Client retries on transient connection failures."""
        adapter = MCPClientAdapter(max_retries=3, retry_wait_initial=0.1)

        connection_attempts = 0

        @asynccontextmanager
        async def failing_then_success_cm(*args: Any, **kwargs: Any):
            nonlocal connection_attempts
            connection_attempts += 1
            if connection_attempts < 3:
                raise ConnectionError("Transient failure")
            # Return mock streams on success
            yield (MagicMock(), MagicMock())

        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock()
        mock_session.initialize = AsyncMock(return_value=MagicMock(
            protocolVersion="1.0.0",
            capabilities=MagicMock(tools=False, resources=False, prompts=False, logging=True),
        ))

        config = MCPServerConfig(
            name="retry-test",
            transport=TransportType.STDIO,
            command="test",
        )

        # Create mock modules
        mock_mcp = MagicMock()
        mock_mcp.ClientSession = MagicMock(return_value=mock_session)
        mock_mcp.StdioServerParameters = MagicMock()

        mock_mcp_client_stdio = MagicMock()
        mock_mcp_client_stdio.stdio_client = failing_then_success_cm

        # Store original modules
        original_mcp = sys.modules.get("mcp")
        original_mcp_client = sys.modules.get("mcp.client")
        original_mcp_client_stdio = sys.modules.get("mcp.client.stdio")

        try:
            # Install mock modules
            sys.modules["mcp"] = mock_mcp
            sys.modules["mcp.client"] = MagicMock()
            sys.modules["mcp.client.stdio"] = mock_mcp_client_stdio

            async with adapter:
                result = await adapter.connect(config)

                assert result.is_ok
                assert connection_attempts == 3
        finally:
            # Restore original modules
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


class TestMCPClientAdapterCapabilities:
    """Test MCPClientAdapter capability detection."""

    @pytest.mark.asyncio
    async def test_server_capabilities_detected(
        self,
        configured_mock_server: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        mcp_mocks: tuple,
    ) -> None:
        """Client correctly detects server capabilities."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            info = adapter.server_info
            assert info is not None
            assert info.capabilities.tools is True
            assert info.capabilities.resources is True
            assert info.capabilities.prompts is True
            assert info.capabilities.logging is True

    @pytest.mark.asyncio
    async def test_empty_server_capabilities(
        self,
        mock_server_state: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
        empty_mcp_mocks: tuple,
    ) -> None:
        """Client handles server with no capabilities."""
        adapter = MCPClientAdapter()

        async with adapter:
            await adapter.connect(stdio_server_config)

            info = adapter.server_info
            assert info is not None
            assert info.capabilities.tools is False
            assert info.capabilities.resources is False
            assert info.capabilities.prompts is False
