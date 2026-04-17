"""MCP types for Ouroboros.

This module defines frozen dataclasses for MCP data structures including
server configuration, tool definitions, and results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse

# Schemes permitted for SSE / HTTP / STREAMABLE_HTTP transports.
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

# Environment flag that intentionally re-enables loopback / private / link-local
# IP literals for local development. Leave unset in production.
_ALLOW_LOCAL_TRANSPORT_ENV = "OUROBOROS_ALLOW_LOCAL_TRANSPORT"

# Well-known hostnames that resolve to loopback addresses.  These bypass the
# ``ipaddress.ip_address()`` check (they raise ``ValueError`` because they are
# not IP literals), so we must block them explicitly.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


def _is_blocked_transport_ip(ip: ipaddress._BaseAddress) -> bool:
    """Return True when an IP literal should be rejected for MCP transport use."""
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _resolved_blocked_transport_ips(hostname: str) -> tuple[str, ...]:
    """Resolve *hostname* and return any blocked IPs it maps to.

    Static hostname validation is not enough because public DNS aliases can
    legally resolve to private or loopback literals (for example ``nip.io``).
    This helper resolves the hostname once at validation time and records any
    IPs that fall inside the blocked transport ranges.

    Resolution failures are treated as inconclusive rather than fatal so the
    validator does not reject legitimate-but-currently-unresolvable hostnames.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        return ()

    blocked: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addr = sockaddr[0]
        try:
            ip = ipaddress.ip_address(addr.strip("[]"))
        except ValueError:
            continue
        if _is_blocked_transport_ip(ip):
            blocked.append(str(ip))

    return tuple(dict.fromkeys(blocked))


def _validate_transport_url(url: str, transport: str) -> None:
    """Validate a transport URL against common SSRF vectors.

    The MCP client will dial whatever URL is configured for SSE, HTTP, or
    STREAMABLE_HTTP transports. Without hardening, that turns any untrusted
    caller that can influence the ``url`` field into an SSRF primitive able to
    reach cloud metadata services (169.254.169.254), loopback services, or
    private RFC1918 ranges. This helper rejects the common vectors before a
    connection is attempted.

    Blocks:
        * Non-http(s) schemes (``file://``, ``gopher://``, ``ftp://`` ...).
        * URLs carrying userinfo (``user:pass@host``) used for credential
          smuggling / host confusion.
        * URLs with an empty hostname (e.g. bare ``http://``).
        * Well-known canonical loopback hostnames (``localhost`` and its
          trailing-dot / mixed-case variants).
        * Literal IPs inside loopback, link-local, private, multicast,
          reserved, or unspecified ranges.
        * DNS hostnames whose ``getaddrinfo()`` result resolves to any
          address in those same blocked ranges (e.g. ``*.nip.io`` aliases,
          DNS rebinding targets, metadata-IP aliases).

    The ``OUROBOROS_ALLOW_LOCAL_TRANSPORT=1`` dev escape re-enables every
    range above for local development only.

    Boundary note: two distinct normalizations are used here. Canonical
    matching against ``_LOOPBACK_HOSTNAMES`` uses a ``rstrip('.').lower()``
    form so that ``LOCALHOST.``/``localhost.``/``localhost`` are recognized
    as the same name. The DNS / IP-literal checks, however, resolve the
    exact host the MCP client will connect to (the unmodified value
    returned by ``urllib.parse``, minus IPv6 brackets), because a stripped
    trailing dot can change how some resolvers answer. Canonicalization is
    for identity matching, not for picking the lookup target.

    Args:
        url: The transport URL to validate.
        transport: Name of the transport (used only in error messages).

    Raises:
        ValueError: If any SSRF guard is triggered.
    """

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme not in _ALLOWED_URL_SCHEMES:
        msg = (
            f"Only http:// and https:// URLs are supported for {transport} "
            f"transport, got: {parsed.scheme}://"
        )
        raise ValueError(msg)

    if parsed.username or parsed.password:
        msg = "Transport URL must not contain userinfo (credentials)"
        raise ValueError(msg)

    hostname = parsed.hostname
    if not hostname:
        msg = "Transport URL must include a hostname"
        raise ValueError(msg)

    allow_local = os.environ.get(_ALLOW_LOCAL_TRANSPORT_ENV, "0") == "1"

    # ``lookup_host`` is the exact host the HTTP client will dial. ``urlparse``
    # has already stripped IPv6 brackets and lowercased the hostname, but we
    # defensively re-strip brackets in case a future parser change preserves
    # them. This value is passed verbatim to ``ipaddress.ip_address()`` and to
    # ``getaddrinfo()`` so DNS resolution sees exactly what the client will.
    lookup_host = hostname.strip("[]")

    # ``canonical_host`` collapses DNS-equivalent spellings (trailing dot,
    # case) into a single form used *only* for well-known-name matching. DNS
    # is case-insensitive, and a single trailing dot marks an absolute FQDN,
    # so ``LOCALHOST.``/``localhost.``/``localhost`` must all hit the
    # loopback guard. ``urlparse`` already lowercases, but it does not strip
    # the trailing dot, which let variants like ``http://localhost./`` slip
    # past the well-known check in earlier versions.
    canonical_host = (lookup_host.rstrip(".") or lookup_host).lower()

    # Check for well-known loopback hostnames before attempting IP parsing.
    # These bypass the IP-literal checks because they are DNS names, but
    # they resolve to loopback addresses and must be blocked unless the
    # dev escape hatch is enabled.
    if canonical_host in _LOOPBACK_HOSTNAMES:
        if allow_local:
            return
        msg = (
            f"Transport URL points to a local hostname: "
            f"{hostname}. Set {_ALLOW_LOCAL_TRANSPORT_ENV}=1 for local dev."
        )
        raise ValueError(msg)

    # Use the un-normalized ``lookup_host`` for IP parsing and DNS resolution
    # so the check matches exactly what the HTTP client will connect to. A
    # resolver may answer differently for ``localhost.`` (absolute) vs
    # ``localhost`` (search-list), so stripping the trailing dot here would
    # break the boundary with the runtime connect path.
    try:
        ip = ipaddress.ip_address(lookup_host)
    except ValueError:
        blocked_ips = _resolved_blocked_transport_ips(lookup_host)
        if not blocked_ips or allow_local:
            return
        msg = (
            "Transport URL hostname resolves to loopback/link-local/private IP(s): "
            f"{', '.join(blocked_ips)} for {hostname}. "
            f"Set {_ALLOW_LOCAL_TRANSPORT_ENV}=1 for local dev."
        )
        raise ValueError(msg)

    if allow_local:
        return

    if _is_blocked_transport_ip(ip):
        msg = (
            f"Transport URL points to loopback/link-local/private IP: "
            f"{hostname}. Set {_ALLOW_LOCAL_TRANSPORT_ENV}=1 for local dev."
        )
        raise ValueError(msg)


class TransportType(StrEnum):
    """MCP transport type for server connections."""

    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"
    HTTP = "http"


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
        if (
            self.transport
            in (
                TransportType.SSE,
                TransportType.STREAMABLE_HTTP,
                TransportType.HTTP,
            )
            and not self.url
        ):
            msg = f"url is required for {self.transport} transport"
            raise ValueError(msg)
        if self.url:
            _validate_transport_url(self.url, str(self.transport))


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
        items: JSON Schema for array items (e.g. ``{"type": "string"}``).
    """

    name: str
    type: ToolInputType
    description: str = ""
    required: bool = True
    default: Any = None
    enum: tuple[str, ...] | None = None
    items: dict[str, str] | None = None


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
            if param.items is not None:
                prop["items"] = dict(param.items)
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
