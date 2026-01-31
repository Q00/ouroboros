"""Tests for MCP client adapter."""

from unittest.mock import MagicMock

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.errors import MCPConnectionError


class TestMCPClientAdapter:
    """Test MCPClientAdapter class."""

    def test_adapter_initial_state(self) -> None:
        """Adapter starts disconnected."""
        adapter = MCPClientAdapter()
        assert adapter.is_connected is False
        assert adapter.server_info is None

    async def test_adapter_context_manager(self) -> None:
        """Adapter works as async context manager."""
        async with MCPClientAdapter() as adapter:
            assert adapter.is_connected is False

    async def test_ensure_connected_when_disconnected(self) -> None:
        """ensure_connected returns error when disconnected."""
        adapter = MCPClientAdapter()
        result = adapter._ensure_connected()
        assert result.is_err
        assert isinstance(result.error, MCPConnectionError)

    async def test_list_tools_requires_connection(self) -> None:
        """list_tools fails when not connected."""
        adapter = MCPClientAdapter()
        result = await adapter.list_tools()
        assert result.is_err
        assert "Not connected" in str(result.error)

    async def test_call_tool_requires_connection(self) -> None:
        """call_tool fails when not connected."""
        adapter = MCPClientAdapter()
        result = await adapter.call_tool("test_tool", {})
        assert result.is_err
        assert "Not connected" in str(result.error)

    async def test_read_resource_requires_connection(self) -> None:
        """read_resource fails when not connected."""
        adapter = MCPClientAdapter()
        result = await adapter.read_resource("ouroboros://test")
        assert result.is_err
        assert "Not connected" in str(result.error)


class TestMCPClientAdapterParsing:
    """Test MCPClientAdapter parsing methods."""

    def test_parse_tool_definition(self) -> None:
        """_parse_tool_definition converts SDK format."""
        adapter = MCPClientAdapter()

        # Mock tool object from SDK
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"
        mock_tool.description = "A test tool"
        mock_tool.inputSchema = {
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Input value"},
            },
            "required": ["input"],
        }

        defn = adapter._parse_tool_definition(mock_tool, "test-server")

        assert defn.name == "test_tool"
        assert defn.description == "A test tool"
        assert defn.server_name == "test-server"
        assert len(defn.parameters) == 1
        assert defn.parameters[0].name == "input"
        assert defn.parameters[0].required is True

    def test_parse_tool_result_text(self) -> None:
        """_parse_tool_result handles text content."""
        adapter = MCPClientAdapter()

        # Mock result from SDK
        mock_result = MagicMock()
        mock_content = MagicMock()
        mock_content.text = "Hello, world!"
        mock_result.content = [mock_content]
        mock_result.isError = False

        result = adapter._parse_tool_result(mock_result, "test_tool")

        assert len(result.content) == 1
        assert result.content[0].text == "Hello, world!"
        assert result.is_error is False


class TestMCPClientAdapterRetry:
    """Test MCPClientAdapter retry behavior."""

    def test_adapter_retry_configuration(self) -> None:
        """Adapter accepts retry configuration."""
        adapter = MCPClientAdapter(
            max_retries=5,
            retry_wait_initial=2.0,
            retry_wait_max=20.0,
        )
        assert adapter._max_retries == 5
        assert adapter._retry_wait_initial == 2.0
        assert adapter._retry_wait_max == 20.0
