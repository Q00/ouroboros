"""Construct public MCP SDK v2 clients from Ouroboros transport config.

The official high-level :class:`mcp.client.Client` is the protocol boundary:
it owns ``server/discover``, legacy fallback, capability metadata, response
caching, and MRTR.  This module only translates application transport config.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from ouroboros.mcp.types import MCPServerConfig, TransportType


@dataclass(slots=True)
class TransportLifecycle:
    """Observed public transport entry, before SDK protocol negotiation."""

    entered: bool = False


@dataclass(frozen=True, slots=True)
class SDKClientResources:
    """A high-level SDK client and any HTTP client Ouroboros must close."""

    client: Any
    http_client: Any | None = None
    transport_lifecycle: TransportLifecycle = field(default_factory=TransportLifecycle)


def build_sdk_client(config: MCPServerConfig) -> SDKClientResources:
    """Build an auto-negotiating SDK client without opening the connection."""
    try:
        import httpx2
        from mcp import StdioServerParameters
        from mcp.client import Client
        from mcp.client.sse import sse_client
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamable_http_client
        from mcp.types import Implementation
    except ImportError as exc:  # pragma: no cover - exercised without the extra
        msg = "mcp package not installed. Install with: pip install 'ouroboros-ai[mcp]'"
        raise ImportError(msg) from exc

    from ouroboros import __version__ as ouroboros_version

    owned_http_client: Any | None = None
    if config.transport == TransportType.STDIO:
        if not config.command:
            raise ValueError("command is required for stdio transport")
        target = stdio_client(
            StdioServerParameters(
                command=config.command,
                args=list(config.args),
                env=config.env if config.env else None,
            )
        )
    elif config.transport == TransportType.SSE:
        if not config.url:
            raise ValueError("url is required for sse transport")
        target = sse_client(
            config.url,
            headers=dict(config.headers) if config.headers else None,
            timeout=config.timeout,
            sse_read_timeout=max(config.timeout, 300.0),
        )
    elif config.transport in (TransportType.STREAMABLE_HTTP, TransportType.HTTP):
        if not config.url:
            raise ValueError(f"url is required for {config.transport} transport")
        headers = {"User-Agent": f"ouroboros-mcp-client/{ouroboros_version}"}
        headers.update(config.headers)
        owned_http_client = httpx2.AsyncClient(
            headers=headers,
            timeout=httpx2.Timeout(config.timeout, read=max(config.timeout, 300.0)),
            # Static URL validation remains effective across the whole request:
            # an attacker-controlled endpoint cannot redirect to loopback or a
            # cloud metadata address.
            follow_redirects=False,
        )
        target = streamable_http_client(config.url, http_client=owned_http_client)
    else:  # pragma: no cover - enum construction prevents this in normal use
        raise ValueError(f"Unknown transport: {config.transport}")

    lifecycle = TransportLifecycle()

    @asynccontextmanager
    async def observed_transport() -> AsyncIterator[Any]:
        # Client accepts this public Transport context. Entering it establishes
        # streams; negotiation happens afterwards inside Client.__aenter__.
        async with target as streams:
            lifecycle.entered = True
            yield streams

    client = Client(
        observed_transport(),
        mode="auto",
        read_timeout_seconds=config.timeout,
        client_info=Implementation(name="ouroboros", version=ouroboros_version),
    )
    return SDKClientResources(
        client=client, http_client=owned_http_client, transport_lifecycle=lifecycle
    )


__all__ = ["SDKClientResources", "build_sdk_client"]
