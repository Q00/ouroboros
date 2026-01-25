"""MCP error hierarchy for Ouroboros.

This module defines MCP-specific exceptions extending the base Ouroboros
error hierarchy. These are used for both unexpected errors (bugs) and
as error types in Result for expected MCP failures.

Exception Hierarchy:
    OuroborosError (base from core.errors)
    └── MCPError (MCP base)
        ├── MCPClientError  - Client-side failures (connection, protocol)
        │   ├── MCPConnectionError    - Failed to connect to server
        │   ├── MCPTimeoutError       - Request timeout
        │   └── MCPProtocolError      - Protocol-level errors
        └── MCPServerError  - Server-side failures
            ├── MCPAuthError              - Authentication/authorization failures
            ├── MCPResourceNotFoundError  - Resource not found
            └── MCPToolError              - Tool execution failures
"""

from typing import Any

from ouroboros.core.errors import OuroborosError


class MCPError(OuroborosError):
    """Base exception for all MCP-related errors.

    All MCP-specific exceptions inherit from this class and OuroborosError.
    This allows catching all MCP errors with a single except clause.

    Attributes:
        message: Human-readable error description.
        server_name: Name of the MCP server involved (if applicable).
        is_retriable: Whether the operation can be retried.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        is_retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the MCP error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server involved.
            is_retriable: Whether the operation can be retried.
            details: Optional dict with additional context.
        """
        super().__init__(message, details)
        self.server_name = server_name
        self.is_retriable = is_retriable

    def __str__(self) -> str:
        """Return string representation of the error."""
        parts = [self.message]
        if self.server_name:
            parts.append(f"server={self.server_name}")
        if self.is_retriable:
            parts.append("retriable=True")
        if self.details:
            parts.append(f"details={self.details}")
        return " ".join(parts)


class MCPClientError(MCPError):
    """Error from MCP client operations.

    Raised when client-side MCP operations fail (connection, protocol errors).
    This is the base class for more specific client errors.

    Attributes:
        request_id: ID of the request that failed (if applicable).
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        request_id: str | None = None,
        is_retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize client error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            request_id: ID of the request that failed.
            is_retriable: Whether the operation can be retried.
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=is_retriable,
            details=details,
        )
        self.request_id = request_id

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        *,
        server_name: str | None = None,
        request_id: str | None = None,
        is_retriable: bool = False,
    ) -> MCPClientError:
        """Create MCPClientError from another exception.

        Args:
            exc: The original exception.
            server_name: Name of the MCP server.
            request_id: ID of the request that failed.
            is_retriable: Whether the operation can be retried.

        Returns:
            An MCPClientError wrapping the original exception.
        """
        error = cls(
            str(exc),
            server_name=server_name,
            request_id=request_id,
            is_retriable=is_retriable,
            details={"original_exception": type(exc).__name__},
        )
        error.__cause__ = exc
        return error


class MCPConnectionError(MCPClientError):
    """Failed to connect to an MCP server.

    Raised when the client cannot establish a connection to the server.
    This is typically retriable after a delay.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        transport: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize connection error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            transport: Transport type (stdio, sse, etc.).
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=True,
            details=details,
        )
        self.transport = transport


class MCPTimeoutError(MCPClientError):
    """MCP request timed out.

    Raised when an MCP request exceeds the configured timeout.
    This is typically retriable with exponential backoff.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        timeout_seconds: float | None = None,
        operation: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize timeout error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            timeout_seconds: The timeout value that was exceeded.
            operation: The operation that timed out.
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=True,
            details=details,
        )
        self.timeout_seconds = timeout_seconds
        self.operation = operation


class MCPProtocolError(MCPClientError):
    """MCP protocol-level error.

    Raised when there's a protocol violation or unexpected response format.
    This is generally not retriable as it indicates a compatibility issue.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        error_code: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize protocol error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            error_code: Protocol error code if available.
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=False,
            details=details,
        )
        self.error_code = error_code


class MCPServerError(MCPError):
    """Error from MCP server operations.

    Raised when server-side MCP operations fail. This is the base class
    for more specific server errors.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        is_retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize server error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            is_retriable: Whether the operation can be retried.
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=is_retriable,
            details=details,
        )


class MCPAuthError(MCPServerError):
    """Authentication or authorization failure.

    Raised when authentication fails or the client lacks permission
    for the requested operation.

    Attributes:
        auth_method: The authentication method that failed.
        required_permission: The permission that was required.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        auth_method: str | None = None,
        required_permission: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize auth error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            auth_method: The authentication method that failed.
            required_permission: The permission that was required.
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=False,
            details=details,
        )
        self.auth_method = auth_method
        self.required_permission = required_permission


class MCPResourceNotFoundError(MCPServerError):
    """Requested resource not found.

    Raised when a requested resource (tool, prompt, resource URI) does not exist.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize resource not found error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            resource_type: Type of resource (tool, prompt, resource).
            resource_id: ID or name of the resource.
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=False,
            details=details,
        )
        self.resource_type = resource_type
        self.resource_id = resource_id


class MCPToolError(MCPServerError):
    """Error during tool execution.

    Raised when a tool invocation fails during execution.
    Extends MCPServerError since tools are server-side operations.

    Attributes:
        tool_name: Name of the tool that failed.
        error_code: Tool-specific error code if available.
    """

    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        tool_name: str | None = None,
        error_code: str | None = None,
        is_retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize tool error.

        Args:
            message: Human-readable error description.
            server_name: Name of the MCP server.
            tool_name: Name of the tool that failed.
            error_code: Tool-specific error code.
            is_retriable: Whether the operation can be retried.
            details: Optional dict with additional context.
        """
        super().__init__(
            message,
            server_name=server_name,
            is_retriable=is_retriable,
            details=details,
        )
        self.tool_name = tool_name
        self.error_code = error_code
