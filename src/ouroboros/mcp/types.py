"""MCP types for Ouroboros.

This module defines frozen dataclasses for MCP data structures including
server configuration, tool definitions, and results.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class TransportType(StrEnum):
    """MCP transport type for server connections."""

    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"


class ToolInputType(StrEnum):
    """JSON Schema types for tool input parameters."""

    STRING = "string"
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """Configuration for connecting to an MCP server.

    Attributes:
        name: Unique name for the server connection.
        transport: Transport type (stdio, sse, etc.).
        command: Command to run for stdio transport.
        args: Arguments for the command.
        url: URL for SSE/HTTP transport.
        env: Environment variables to set.
        timeout: Connection timeout in seconds.
        headers: HTTP headers for SSE/HTTP transport.
    """

    name: str
    transport: TransportType
    command: str | None = None
    args: tuple[str, ...] = field(default_factory=tuple)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate configuration after initialization."""
        if self.transport == TransportType.STDIO and not self.command:
            msg = "command is required for stdio transport"
            raise ValueError(msg)
        if self.transport in (TransportType.SSE, TransportType.STREAMABLE_HTTP) and not self.url:
            msg = f"url is required for {self.transport} transport"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class MCPToolParameter:
    """A single parameter for an MCP tool.

    Attributes:
        name: Parameter name.
        type: JSON Schema type of the parameter.
        description: Human-readable description.
        required: Whether the parameter is required.
        default: Default value if not provided.
        enum: Allowed values if restricted.
    """

    name: str
    type: ToolInputType
    description: str = ""
    required: bool = True
    default: Any = None
    enum: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class MCPToolDefinition:
    """Definition of an MCP tool.

    Attributes:
        name: Unique tool name.
        description: Human-readable description.
        parameters: List of tool parameters.
        server_name: Name of the server providing this tool.
    """

    name: str
    description: str
    parameters: tuple[MCPToolParameter, ...] = field(default_factory=tuple)
    server_name: str | None = None

    def to_input_schema(self) -> dict[str, Any]:
        """Convert to JSON Schema for tool input.

        Returns:
            A JSON Schema dict describing the tool's input parameters.
        """
        properties: dict[str, Any] = {}
        required: list[str] = []

        for param in self.parameters:
            prop: dict[str, Any] = {
                "type": param.type.value,
                "description": param.description,
            }
            if param.default is not None:
                prop["default"] = param.default
            if param.enum is not None:
                prop["enum"] = list(param.enum)
            properties[param.name] = prop
            if param.required:
                required.append(param.name)

        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }


@dataclass(frozen=True, slots=True)
class MCPToolResult:
    """Result from an MCP tool invocation.

    Attributes:
        content: List of content items from the tool.
        is_error: Whether the tool execution resulted in an error.
        meta: Optional metadata from the tool.
    """

    content: tuple[MCPContentItem, ...] = field(default_factory=tuple)
    is_error: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def text_content(self) -> str:
        """Return concatenated text content from all text items.

        Returns:
            All text content joined with newlines.
        """
        return "\n".join(
            item.text for item in self.content if item.type == ContentType.TEXT and item.text
        )


class ContentType(StrEnum):
    """Type of content in an MCP response."""

    TEXT = "text"
    IMAGE = "image"
    RESOURCE = "resource"


@dataclass(frozen=True, slots=True)
class MCPContentItem:
    """A single content item in an MCP response.

    Attributes:
        type: Type of content (text, image, resource).
        text: Text content if type is TEXT.
        data: Binary data (base64) if type is IMAGE.
        mime_type: MIME type for binary data.
        uri: Resource URI if type is RESOURCE.
    """

    type: ContentType
    text: str | None = None
    data: str | None = None
    mime_type: str | None = None
    uri: str | None = None


@dataclass(frozen=True, slots=True)
class MCPResourceDefinition:
    """Definition of an MCP resource.

    Attributes:
        uri: Resource URI (unique identifier).
        name: Human-readable name.
        description: Description of the resource.
        mime_type: MIME type of the resource content.
    """

    uri: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"


@dataclass(frozen=True, slots=True)
class MCPResourceContent:
    """Content of an MCP resource.

    Attributes:
        uri: Resource URI.
        text: Text content (for text resources).
        blob: Binary content as base64 (for binary resources).
        mime_type: MIME type of the content.
    """

    uri: str
    text: str | None = None
    blob: str | None = None
    mime_type: str = "text/plain"


@dataclass(frozen=True, slots=True)
class MCPPromptDefinition:
    """Definition of an MCP prompt.

    Attributes:
        name: Unique prompt name.
        description: Description of what the prompt does.
        arguments: List of argument definitions.
    """

    name: str
    description: str = ""
    arguments: tuple[MCPPromptArgument, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MCPPromptArgument:
    """Argument definition for an MCP prompt.

    Attributes:
        name: Argument name.
        description: Description of the argument.
        required: Whether the argument is required.
    """

    name: str
    description: str = ""
    required: bool = True


@dataclass(frozen=True, slots=True)
class MCPCapabilities:
    """Capabilities of an MCP server.

    Attributes:
        tools: Whether the server supports tools.
        resources: Whether the server supports resources.
        prompts: Whether the server supports prompts.
        logging: Whether the server supports logging.
    """

    tools: bool = False
    resources: bool = False
    prompts: bool = False
    logging: bool = False


@dataclass(frozen=True, slots=True)
class MCPServerInfo:
    """Information about an MCP server.

    Attributes:
        name: Server name.
        version: Server version.
        capabilities: Server capabilities.
        tools: Available tools.
        resources: Available resources.
        prompts: Available prompts.
    """

    name: str
    version: str = "1.0.0"
    capabilities: MCPCapabilities = field(default_factory=MCPCapabilities)
    tools: tuple[MCPToolDefinition, ...] = field(default_factory=tuple)
    resources: tuple[MCPResourceDefinition, ...] = field(default_factory=tuple)
    prompts: tuple[MCPPromptDefinition, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class MCPRequest:
    """An MCP request message.

    Attributes:
        method: The MCP method being called.
        params: Parameters for the method.
        request_id: Unique request identifier.
    """

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class MCPResponse:
    """An MCP response message.

    Attributes:
        result: The result data if successful.
        error: Error information if failed.
        request_id: The request ID this is responding to.
    """

    result: dict[str, Any] | None = None
    error: MCPResponseError | None = None
    request_id: str | None = None

    @property
    def is_success(self) -> bool:
        """Return True if this is a successful response."""
        return self.error is None


@dataclass(frozen=True, slots=True)
class MCPResponseError:
    """Error information in an MCP response.

    Attributes:
        code: Error code.
        message: Error message.
        data: Additional error data.
    """

    code: int
    message: str
    data: dict[str, Any] | None = None
