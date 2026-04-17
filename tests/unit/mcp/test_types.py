"""Tests for MCP types."""

import socket

import pytest

from ouroboros.mcp.types import (
    ContentType,
    MCPCapabilities,
    MCPContentItem,
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
        assert TransportType.HTTP == "http"


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

    def test_http_config_requires_url(self) -> None:
        """HTTP transport requires URL."""
        with pytest.raises(ValueError, match="url is required"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
            )

    def test_valid_http_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid HTTP config is created successfully."""
        monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "1")
        config = MCPServerConfig(
            name="test",
            transport=TransportType.HTTP,
            url="http://localhost:3000",
        )
        assert config.url == "http://localhost:3000"

    def test_valid_sse_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid SSE config is created successfully."""
        monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "1")
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

    @pytest.mark.parametrize("scheme", ["file", "gopher", "ftp"])
    def test_rejects_non_http_url_schemes(self, scheme: str) -> None:
        """MCPServerConfig rejects non-http(s) URL schemes to prevent SSRF."""
        url = f"{scheme}://example.com/path"
        with pytest.raises(ValueError, match="Only http:// and https:// URLs are supported"):
            MCPServerConfig(
                name="test",
                transport=TransportType.SSE,
                url=url,
            )

    @pytest.mark.parametrize("scheme", ["file", "gopher", "ftp"])
    def test_rejects_non_http_url_schemes_streamable_http(self, scheme: str) -> None:
        """MCPServerConfig rejects non-http(s) schemes for STREAMABLE_HTTP transport."""
        url = f"{scheme}://example.com/path"
        with pytest.raises(ValueError, match="Only http:// and https:// URLs are supported"):
            MCPServerConfig(
                name="test",
                transport=TransportType.STREAMABLE_HTTP,
                url=url,
            )

    def test_accepts_http_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MCPServerConfig accepts http:// URLs."""
        monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "1")
        config = MCPServerConfig(
            name="test",
            transport=TransportType.SSE,
            url="http://localhost:8080/sse",
        )
        assert config.url == "http://localhost:8080/sse"

    def test_accepts_https_url(self) -> None:
        """MCPServerConfig accepts https:// URLs."""
        config = MCPServerConfig(
            name="test",
            transport=TransportType.STREAMABLE_HTTP,
            url="https://api.example.com/mcp",
        )
        assert config.url == "https://api.example.com/mcp"


class TestMCPServerConfigSSRFHardening:
    """SSRF hardening beyond the scheme allowlist.

    These tests cover review finding #402: the prior check only validated
    URL schemes and left obvious SSRF vectors open (loopback, link-local
    metadata endpoints, RFC1918 ranges, credential smuggling, empty hosts).
    """

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/",
            "http://127.0.0.1:8080/",
            "http://[::1]/",
            "https://[::1]:443/",
        ],
    )
    def test_rejects_loopback(self, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loopback IPv4/IPv6 literals are rejected."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)
        with pytest.raises(ValueError, match="loopback/link-local/private"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url=url,
            )

    def test_rejects_aws_metadata(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The AWS / GCP / Azure metadata link-local IP is rejected."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)
        with pytest.raises(ValueError, match="loopback/link-local/private"):
            MCPServerConfig(
                name="test",
                transport=TransportType.STREAMABLE_HTTP,
                url="http://169.254.169.254/latest/meta-data/",
            )

    @pytest.mark.parametrize(
        "url",
        [
            "http://10.0.0.1/",
            "http://10.255.255.255/",
            "http://172.16.0.1/",
            "http://172.31.255.254/",
            "http://192.168.1.1/",
        ],
    )
    def test_rejects_private_ranges(self, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """RFC1918 private IPv4 ranges are rejected."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)
        with pytest.raises(ValueError, match="loopback/link-local/private"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url=url,
            )

    @pytest.mark.parametrize(
        "url",
        [
            "http://user:pass@example.com/",
            "http://user@example.com/",
            "https://admin:secret@api.example.com/mcp",
        ],
    )
    def test_rejects_userinfo(self, url: str) -> None:
        """URLs carrying userinfo (credential smuggling) are rejected."""
        with pytest.raises(ValueError, match="userinfo"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url=url,
            )

    def test_rejects_empty_hostname(self) -> None:
        """Bare scheme URLs without a hostname are rejected."""
        with pytest.raises(ValueError, match="hostname"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url="http://",
            )

    def test_rejects_javascript_scheme(self) -> None:
        """javascript: and other non-http schemes remain rejected."""
        with pytest.raises(ValueError, match="Only http:// and https://"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url="javascript:alert(1)",
            )

    def test_accepts_public_hostname(self) -> None:
        """Public DNS hostnames are still accepted."""
        config = MCPServerConfig(
            name="test",
            transport=TransportType.HTTP,
            url="http://example.com/",
        )
        assert config.url == "http://example.com/"

    def test_accepts_public_ip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Public IP literals (e.g. 8.8.8.8) are still accepted."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)
        config = MCPServerConfig(
            name="test",
            transport=TransportType.HTTP,
            url="https://8.8.8.8/mcp",
        )
        assert config.url == "https://8.8.8.8/mcp"

    def test_local_transport_escape_hatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OUROBOROS_ALLOW_LOCAL_TRANSPORT=1 permits loopback for local dev."""
        monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "1")
        config = MCPServerConfig(
            name="test",
            transport=TransportType.HTTP,
            url="http://127.0.0.1:3000/",
        )
        assert config.url == "http://127.0.0.1:3000/"

    def test_local_transport_escape_hatch_off_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without the env flag, the loopback guard still fires."""
        monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "0")
        with pytest.raises(ValueError, match="loopback/link-local/private"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url="http://127.0.0.1/",
            )

    @pytest.mark.parametrize(
        "url",
        [
            "http://localhost/",
            "http://localhost:3000/",
            "http://localhost:8080/sse",
            "https://localhost/",
            # Canonical FQDN form: a trailing dot marks an absolute DNS name
            # and must be treated as identical to "localhost".  Without
            # normalization these slipped past the well-known loopback check
            # and fell through to DNS resolution.
            "http://localhost./",
            "http://localhost.:3000/",
            "https://LOCALHOST./",
        ],
    )
    def test_rejects_localhost(self, url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        """Loopback hostnames (localhost) are rejected without escape hatch."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)
        with pytest.raises(ValueError, match="local hostname"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url=url,
            )

    def test_localhost_allowed_with_escape_hatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """OUROBOROS_ALLOW_LOCAL_TRANSPORT=1 permits localhost for local dev."""
        monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "1")
        config = MCPServerConfig(
            name="test",
            transport=TransportType.HTTP,
            url="http://localhost:3000/",
        )
        assert config.url == "http://localhost:3000/"

    def test_rejects_hostname_resolving_to_loopback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Public-looking hostnames that resolve to loopback are rejected."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)

        def _fake_getaddrinfo(host: str, *_args, **_kwargs):
            assert host == "127.0.0.1.nip.io"
            return [
                (
                    2,
                    1,
                    6,
                    "",
                    ("127.0.0.1", 0),
                )
            ]

        monkeypatch.setattr("ouroboros.mcp.types.socket.getaddrinfo", _fake_getaddrinfo)

        with pytest.raises(ValueError, match="hostname resolves to loopback/link-local/private"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url="http://127.0.0.1.nip.io/",
            )

    def test_rejects_hostname_resolving_to_metadata_ip(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Hostnames resolving to link-local metadata IPs are rejected."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)

        def _fake_getaddrinfo(host: str, *_args, **_kwargs):
            assert host == "metadata.example.test"
            return [
                (
                    2,
                    1,
                    6,
                    "",
                    ("169.254.169.254", 0),
                )
            ]

        monkeypatch.setattr("ouroboros.mcp.types.socket.getaddrinfo", _fake_getaddrinfo)

        with pytest.raises(ValueError, match="169.254.169.254"):
            MCPServerConfig(
                name="test",
                transport=TransportType.HTTP,
                url="http://metadata.example.test/latest/meta-data/",
            )

    def test_hostname_resolution_failure_is_inconclusive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolution failures stay non-fatal to preserve existing DNS behavior."""
        monkeypatch.delenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", raising=False)

        def _fake_getaddrinfo(_host: str, *_args, **_kwargs):
            raise socket.gaierror("unresolvable")

        monkeypatch.setattr("ouroboros.mcp.types.socket.getaddrinfo", _fake_getaddrinfo)

        config = MCPServerConfig(
            name="test",
            transport=TransportType.HTTP,
            url="http://future-host.example.test/",
        )
        assert config.url == "http://future-host.example.test/"

    def test_hostname_resolution_escape_hatch_allows_local_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The dev escape hatch still permits aliases that resolve locally."""
        monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "1")

        def _fake_getaddrinfo(_host: str, *_args, **_kwargs):
            return [(2, 1, 6, "", ("127.0.0.1", 0))]

        monkeypatch.setattr("ouroboros.mcp.types.socket.getaddrinfo", _fake_getaddrinfo)

        config = MCPServerConfig(
            name="test",
            transport=TransportType.HTTP,
            url="http://127.0.0.1.nip.io/",
        )
        assert config.url == "http://127.0.0.1.nip.io/"


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
            parameters=(MCPToolParameter(name="input", type=ToolInputType.STRING),),
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
