"""Tests for MCP types."""

import pytest

from ouroboros.mcp.types import (
    ContentType,
    MCPCapabilities,
    MCPContentItem,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPRequest,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPResponse,
    MCPResponseError,
    MCPServerConfig,
    MCPServerInfo,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
    TransportType,
)


class TestTransportType:
    """Test TransportType enum."""

    def test_transport_type_values(self) -> None:
        """TransportType has expected string values."""
        assert TransportType.STDIO == "stdio"
        assert TransportType.SSE == "sse"
        assert TransportType.STREAMABLE_HTTP == "streamable-http"


class TestMCPServerConfig:
    """Test MCPServerConfig dataclass."""

    def test_stdio_config_requires_command(self) -> None:
        """STDIO transport requires command."""
        with pytest.raises(ValueError, match="command is required"):
            MCPServerConfig(
                name="test",
                transport=TransportType.STDIO,
            )

    def test_valid_stdio_config(self) -> None:
        """Valid STDIO config is created successfully."""
        config = MCPServerConfig(
            name="test",
            transport=TransportType.STDIO,
            command="my-server",
            args=("--mode", "test"),
        )
        assert config.name == "test"
        assert config.command == "my-server"
        assert config.args == ("--mode", "test")

    def test_sse_config_requires_url(self) -> None:
        """SSE transport requires URL."""
        with pytest.raises(ValueError, match="url is required"):
            MCPServerConfig(
                name="test",
                transport=TransportType.SSE,
            )

    def test_valid_sse_config(self) -> None:
        """Valid SSE config is created successfully."""
        config = MCPServerConfig(
            name="test",
            transport=TransportType.SSE,
            url="http://localhost:8080/sse",
        )
        assert config.url == "http://localhost:8080/sse"

    def test_config_is_frozen(self) -> None:
        """MCPServerConfig is immutable."""
        config = MCPServerConfig(
            name="test",
            transport=TransportType.STDIO,
            command="cmd",
        )
        with pytest.raises(AttributeError):
            config.name = "changed"  # type: ignore[misc]

    def test_default_values(self) -> None:
        """MCPServerConfig has correct default values."""
        config = MCPServerConfig(
            name="test",
            transport=TransportType.STDIO,
            command="cmd",
        )
        assert config.timeout == 30.0
        assert config.args == ()
        assert config.env == {}
        assert config.headers == {}


class TestMCPToolParameter:
    """Test MCPToolParameter dataclass."""

    def test_parameter_creation(self) -> None:
        """MCPToolParameter is created with correct values."""
        param = MCPToolParameter(
            name="input",
            type=ToolInputType.STRING,
            description="An input value",
            required=True,
        )
        assert param.name == "input"
        assert param.type == ToolInputType.STRING
        assert param.required is True

    def test_parameter_with_enum(self) -> None:
        """MCPToolParameter can have enum values."""
        param = MCPToolParameter(
            name="size",
            type=ToolInputType.STRING,
            enum=("small", "medium", "large"),
        )
        assert param.enum == ("small", "medium", "large")


class TestMCPToolDefinition:
    """Test MCPToolDefinition dataclass."""

    def test_tool_definition_creation(self) -> None:
        """MCPToolDefinition is created with correct values."""
        defn = MCPToolDefinition(
            name="my_tool",
            description="A useful tool",
            parameters=(
                MCPToolParameter(name="input", type=ToolInputType.STRING),
            ),
        )
        assert defn.name == "my_tool"
        assert defn.description == "A useful tool"
        assert len(defn.parameters) == 1

    def test_to_input_schema(self) -> None:
        """to_input_schema generates valid JSON schema."""
        defn = MCPToolDefinition(
            name="my_tool",
            description="A tool",
            parameters=(
                MCPToolParameter(
                    name="input",
                    type=ToolInputType.STRING,
                    description="Input value",
                    required=True,
                ),
                MCPToolParameter(
                    name="count",
                    type=ToolInputType.INTEGER,
                    description="Count",
                    required=False,
                    default=1,
                ),
            ),
        )
        schema = defn.to_input_schema()
        assert schema["type"] == "object"
        assert "input" in schema["properties"]
        assert "count" in schema["properties"]
        assert "input" in schema["required"]
        assert "count" not in schema["required"]
        assert schema["properties"]["count"]["default"] == 1


class TestMCPToolResult:
    """Test MCPToolResult dataclass."""

    def test_result_text_content(self) -> None:
        """text_content concatenates text items."""
        result = MCPToolResult(
            content=(
                MCPContentItem(type=ContentType.TEXT, text="Line 1"),
                MCPContentItem(type=ContentType.TEXT, text="Line 2"),
                MCPContentItem(type=ContentType.IMAGE, data="base64..."),
            ),
        )
        assert result.text_content == "Line 1\nLine 2"

    def test_empty_result(self) -> None:
        """Empty result has no text content."""
        result = MCPToolResult()
        assert result.text_content == ""
        assert result.is_error is False


class TestMCPContentItem:
    """Test MCPContentItem dataclass."""

    def test_text_content_item(self) -> None:
        """Text content item has correct type."""
        item = MCPContentItem(type=ContentType.TEXT, text="Hello")
        assert item.type == ContentType.TEXT
        assert item.text == "Hello"

    def test_image_content_item(self) -> None:
        """Image content item has correct type."""
        item = MCPContentItem(
            type=ContentType.IMAGE,
            data="base64data",
            mime_type="image/png",
        )
        assert item.type == ContentType.IMAGE
        assert item.data == "base64data"


class TestMCPResourceDefinition:
    """Test MCPResourceDefinition dataclass."""

    def test_resource_definition(self) -> None:
        """MCPResourceDefinition is created correctly."""
        defn = MCPResourceDefinition(
            uri="ouroboros://sessions",
            name="Sessions",
            description="List of sessions",
        )
        assert defn.uri == "ouroboros://sessions"
        assert defn.mime_type == "text/plain"  # default


class TestMCPResourceContent:
    """Test MCPResourceContent dataclass."""

    def test_text_resource(self) -> None:
        """Text resource content."""
        content = MCPResourceContent(
            uri="ouroboros://test",
            text="Hello, world!",
            mime_type="text/plain",
        )
        assert content.text == "Hello, world!"
        assert content.blob is None

    def test_binary_resource(self) -> None:
        """Binary resource content."""
        content = MCPResourceContent(
            uri="ouroboros://test",
            blob="base64data",
            mime_type="application/octet-stream",
        )
        assert content.blob == "base64data"


class TestMCPCapabilities:
    """Test MCPCapabilities dataclass."""

    def test_default_capabilities(self) -> None:
        """Default capabilities are all False."""
        caps = MCPCapabilities()
        assert caps.tools is False
        assert caps.resources is False
        assert caps.prompts is False
        assert caps.logging is False

    def test_custom_capabilities(self) -> None:
        """Custom capabilities are set correctly."""
        caps = MCPCapabilities(tools=True, resources=True)
        assert caps.tools is True
        assert caps.resources is True


class TestMCPServerInfo:
    """Test MCPServerInfo dataclass."""

    def test_server_info_creation(self) -> None:
        """MCPServerInfo is created correctly."""
        info = MCPServerInfo(
            name="test-server",
            version="1.0.0",
            capabilities=MCPCapabilities(tools=True),
        )
        assert info.name == "test-server"
        assert info.version == "1.0.0"
        assert info.capabilities.tools is True


class TestMCPRequest:
    """Test MCPRequest dataclass."""

    def test_request_creation(self) -> None:
        """MCPRequest is created correctly."""
        request = MCPRequest(
            method="tools/call",
            params={"name": "my_tool"},
            request_id="req-123",
        )
        assert request.method == "tools/call"
        assert request.params == {"name": "my_tool"}
        assert request.request_id == "req-123"


class TestMCPResponse:
    """Test MCPResponse dataclass."""

    def test_successful_response(self) -> None:
        """Successful response is detected."""
        response = MCPResponse(result={"data": "value"})
        assert response.is_success is True

    def test_error_response(self) -> None:
        """Error response is detected."""
        response = MCPResponse(
            error=MCPResponseError(code=-32600, message="Invalid request"),
        )
        assert response.is_success is False
