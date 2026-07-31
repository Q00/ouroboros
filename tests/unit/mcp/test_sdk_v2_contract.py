"""Contract gate for the public MCP Python SDK v2 surface Ouroboros uses."""

from inspect import signature

from mcp.client import Client
from mcp.server import CacheHint, MCPServer
from mcp.types import (
    LATEST_PROTOCOL_VERSION,
    CallToolResult,
    DiscoverResult,
    ServerCapabilities,
)


def test_sdk_exposes_modern_client_and_server_boundaries() -> None:
    """The pinned SDK owns negotiation and both sides of the wire boundary."""
    assert LATEST_PROTOCOL_VERSION == "2026-07-28"
    assert "mode" in signature(Client).parameters
    assert signature(Client).parameters["mode"].default == "auto"
    assert "version" in signature(MCPServer).parameters


def test_sdk_result_and_cache_contract_uses_v2_fields() -> None:
    """Modern results are typed and cache metadata uses the 2026 aliases."""
    tool_result = CallToolResult(content=[])
    assert tool_result.result_type == "complete"
    assert tool_result.model_dump(by_alias=True)["resultType"] == "complete"

    discover = DiscoverResult(
        supportedVersions=[LATEST_PROTOCOL_VERSION],
        capabilities=ServerCapabilities(),
    )
    payload = discover.model_dump(by_alias=True)
    assert payload["resultType"] == "complete"
    assert payload["ttlMs"] == 0
    assert payload["cacheScope"] == "private"

    hint = CacheHint(ttl_ms=1_000, scope="public")
    assert hint.ttl_ms == 1_000
    assert hint.scope == "public"
