"""Tests for MCP error hierarchy."""

import pytest

from ouroboros.core.errors import OuroborosError
from ouroboros.mcp.errors import (
    MCPAuthError,
    MCPClientError,
    MCPConnectionError,
    MCPError,
    MCPProtocolError,
    MCPResourceNotFoundError,
    MCPServerError,
    MCPTimeoutError,
    MCPToolError,
)


class TestMCPErrorHierarchy:
    """Test MCP error class hierarchy."""

    def test_mcp_error_inherits_from_ouroboros_error(self) -> None:
        """MCPError inherits from OuroborosError."""
        error = MCPError("test error")
        assert isinstance(error, OuroborosError)
        assert isinstance(error, Exception)

    def test_mcp_client_error_inherits_from_mcp_error(self) -> None:
        """MCPClientError inherits from MCPError."""
        error = MCPClientError("client error")
        assert isinstance(error, MCPError)
        assert isinstance(error, OuroborosError)

    def test_mcp_server_error_inherits_from_mcp_error(self) -> None:
        """MCPServerError inherits from MCPError."""
        error = MCPServerError("server error")
        assert isinstance(error, MCPError)

    def test_specific_errors_inherit_correctly(self) -> None:
        """Specific error types have correct inheritance."""
        assert isinstance(MCPConnectionError("conn"), MCPClientError)
        assert isinstance(MCPTimeoutError("timeout"), MCPClientError)
        assert isinstance(MCPProtocolError("protocol"), MCPClientError)
        assert isinstance(MCPAuthError("auth"), MCPServerError)
        assert isinstance(MCPResourceNotFoundError("not found"), MCPServerError)
        assert isinstance(MCPToolError("tool error"), MCPError)


class TestMCPError:
    """Test MCPError base class."""

    def test_error_with_message_only(self) -> None:
        """MCPError can be created with just a message."""
        error = MCPError("Something went wrong")
        assert error.message == "Something went wrong"
        assert error.server_name is None
        assert error.is_retriable is False

    def test_error_with_all_attributes(self) -> None:
        """MCPError can be created with all attributes."""
        error = MCPError(
            "Something went wrong",
            server_name="my-server",
            is_retriable=True,
            details={"code": 500},
        )
        assert error.message == "Something went wrong"
        assert error.server_name == "my-server"
        assert error.is_retriable is True
        assert error.details == {"code": 500}

    def test_str_representation(self) -> None:
        """MCPError has informative string representation."""
        error = MCPError("test", server_name="srv", is_retriable=True)
        error_str = str(error)
        assert "test" in error_str
        assert "srv" in error_str
        assert "retriable=True" in error_str


class TestMCPClientError:
    """Test MCPClientError class."""

    def test_client_error_with_request_id(self) -> None:
        """MCPClientError can include request ID."""
        error = MCPClientError(
            "Request failed",
            server_name="server",
            request_id="req-123",
        )
        assert error.request_id == "req-123"

    def test_from_exception_preserves_cause(self) -> None:
        """from_exception preserves the original exception as __cause__."""
        original = ValueError("original error")
        error = MCPClientError.from_exception(
            original,
            server_name="my-server",
        )
        assert error.__cause__ is original
        assert error.server_name == "my-server"
        assert "original error" in str(error)

    def test_from_exception_captures_exception_type(self) -> None:
        """from_exception records the original exception type."""
        original = ConnectionError("connection lost")
        error = MCPClientError.from_exception(original)
        assert error.details["original_exception"] == "ConnectionError"


class TestMCPConnectionError:
    """Test MCPConnectionError class."""

    def test_connection_error_is_retriable(self) -> None:
        """MCPConnectionError is retriable by default."""
        error = MCPConnectionError("connection failed")
        assert error.is_retriable is True

    def test_connection_error_with_transport(self) -> None:
        """MCPConnectionError can include transport type."""
        error = MCPConnectionError(
            "connection failed",
            transport="stdio",
        )
        assert error.transport == "stdio"


class TestMCPTimeoutError:
    """Test MCPTimeoutError class."""

    def test_timeout_error_is_retriable(self) -> None:
        """MCPTimeoutError is retriable by default."""
        error = MCPTimeoutError("request timed out")
        assert error.is_retriable is True

    def test_timeout_error_with_details(self) -> None:
        """MCPTimeoutError can include timeout details."""
        error = MCPTimeoutError(
            "request timed out",
            timeout_seconds=30.0,
            operation="call_tool",
        )
        assert error.timeout_seconds == 30.0
        assert error.operation == "call_tool"


class TestMCPAuthError:
    """Test MCPAuthError class."""

    def test_auth_error_attributes(self) -> None:
        """MCPAuthError has correct attributes."""
        error = MCPAuthError(
            "authentication failed",
            auth_method="api_key",
            required_permission="execute",
        )
        assert error.auth_method == "api_key"
        assert error.required_permission == "execute"
        assert error.is_retriable is False


class TestMCPResourceNotFoundError:
    """Test MCPResourceNotFoundError class."""

    def test_resource_not_found_attributes(self) -> None:
        """MCPResourceNotFoundError has correct attributes."""
        error = MCPResourceNotFoundError(
            "tool not found",
            resource_type="tool",
            resource_id="my_tool",
        )
        assert error.resource_type == "tool"
        assert error.resource_id == "my_tool"


class TestMCPToolError:
    """Test MCPToolError class."""

    def test_tool_error_attributes(self) -> None:
        """MCPToolError has correct attributes."""
        error = MCPToolError(
            "tool execution failed",
            tool_name="my_tool",
            error_code="E001",
            is_retriable=True,
        )
        assert error.tool_name == "my_tool"
        assert error.error_code == "E001"
        assert error.is_retriable is True
