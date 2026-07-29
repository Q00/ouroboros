"""Integration tests for MCPClientAdapter.

These tests verify that the MCPClientAdapter correctly integrates with
mock MCP servers, testing the full flow of connection, tool calling,
resource reading, and prompt handling.
"""

from unittest.mock import patch

import pytest

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.types import (
    MCPServerConfig,
    TransportType,
)

from .conftest import (
    MockMCPClient,
    MockMCPServerState,
    create_mock_sdk_client_resources,
)


@pytest.fixture
def mcp_mocks(configured_mock_server: MockMCPServerState):
    """Patch the adapter boundary with an in-memory SDK v2 client."""
    with patch(
        "ouroboros.mcp.client.adapter.build_sdk_client",
        side_effect=lambda _config: create_mock_sdk_client_resources(configured_mock_server),
    ) as build:
        yield build


@pytest.fixture
def empty_mcp_mocks(mock_server_state: MockMCPServerState):
    """Patch the adapter boundary with an empty SDK v2 server."""
    with patch(
        "ouroboros.mcp.client.adapter.build_sdk_client",
        side_effect=lambda _config: create_mock_sdk_client_resources(mock_server_state),
    ) as build:
        yield build


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

        server_state = MockMCPServerState(name="retry-test")

        class RetryingClient(MockMCPClient):
            async def __aenter__(self) -> MockMCPClient:
                nonlocal connection_attempts
                connection_attempts += 1
                if connection_attempts < 3:
                    raise ConnectionError("Transient failure")
                return await super().__aenter__()

        config = MCPServerConfig(
            name="retry-test",
            transport=TransportType.STDIO,
            command="test",
        )

        from ouroboros.mcp.client.sdk_factory import SDKClientResources

        with patch(
            "ouroboros.mcp.client.adapter.build_sdk_client",
            side_effect=lambda _config: SDKClientResources(client=RetryingClient(server_state)),
        ):
            async with adapter:
                result = await adapter.connect(config)

                assert result.is_ok
                assert connection_attempts == 3


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

    @pytest.mark.asyncio
    async def test_capability_snapshot_is_complete_and_deeply_immutable(
        self,
        mock_server_state: MockMCPServerState,
        stdio_server_config: MCPServerConfig,
    ) -> None:
        """Negotiated v2 capability metadata survives beyond convenience flags."""
        from mcp import types as sdk_types

        from ouroboros.mcp.client.sdk_factory import SDKClientResources

        client = MockMCPClient(mock_server_state)
        client.server_capabilities = sdk_types.ServerCapabilities(
            tools=sdk_types.ToolsCapability(list_changed=True),
            completions=sdk_types.CompletionsCapability(),
            tasks=sdk_types.ServerTasksCapability(cancel=sdk_types.TasksCancelCapability()),
            experimental={"com.example/jobs": {"version": 2, "modes": ["shared"]}},
        )

        with patch(
            "ouroboros.mcp.client.adapter.build_sdk_client",
            return_value=SDKClientResources(client=client),
        ):
            adapter = MCPClientAdapter()
            async with adapter:
                result = await adapter.connect(stdio_server_config)
                assert result.is_ok
                snapshot = adapter.server_snapshot

        assert snapshot is not None
        capabilities = snapshot.capabilities
        assert capabilities.tools is True
        assert capabilities.completions is True
        assert capabilities.tasks is True
        assert capabilities.experimental is True
        assert capabilities.details["tools"]["listChanged"] is True
        assert capabilities.details["experimental"]["com.example/jobs"]["version"] == 2
        assert capabilities.details["experimental"]["com.example/jobs"]["modes"] == ("shared",)
        with pytest.raises(TypeError):
            capabilities.details["tools"]["listChanged"] = False
