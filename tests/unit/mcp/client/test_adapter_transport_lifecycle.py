"""Lifecycle and HTTP ownership tests for the SDK v2 client boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.mcp.client.adapter import MCPClientAdapter
from ouroboros.mcp.client.sdk_factory import SDKClientResources, build_sdk_client
from ouroboros.mcp.types import MCPServerConfig, TransportType


@pytest.fixture(autouse=True)
def _allow_local_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUROBOROS_ALLOW_LOCAL_TRANSPORT", "1")


def _config(transport: TransportType = TransportType.STDIO) -> MCPServerConfig:
    if transport == TransportType.STDIO:
        return MCPServerConfig(name="test", transport=transport, command="echo")
    return MCPServerConfig(
        name="test",
        transport=transport,
        url="http://localhost:3000",
        headers={"Authorization": "Bearer token"},
        timeout=15,
    )


def _entered_client(*, enter_error: Exception | None = None, exit_error: Exception | None = None):
    client = MagicMock()
    client.__aenter__ = (
        AsyncMock(side_effect=enter_error) if enter_error else AsyncMock(return_value=client)
    )
    client.__aexit__ = (
        AsyncMock(side_effect=exit_error) if exit_error else AsyncMock(return_value=None)
    )
    client.protocol_version = "2026-07-28"
    client.server_info = None
    client.server_capabilities = MagicMock(
        tools=None, resources=None, prompts=None, logging=None, extensions=None
    )
    client.instructions = None
    client.session = MagicMock(discover_result=None)
    return client


class TestLifecycle:
    async def test_failed_enter_closes_client_and_owned_http_client(self) -> None:
        client = _entered_client(enter_error=ConnectionError("discover failed"))
        http_client = AsyncMock()
        adapter = MCPClientAdapter(max_retries=1)
        with patch(
            "ouroboros.mcp.client.adapter.build_sdk_client",
            return_value=SDKClientResources(client, http_client),
        ):
            result = await adapter.connect(_config())

        assert result.is_err
        client.__aexit__.assert_awaited_once_with(None, None, None)
        http_client.aclose.assert_awaited_once()
        assert not adapter.is_connected

    async def test_disconnect_attempts_http_close_when_client_exit_fails(self) -> None:
        client = _entered_client(exit_error=RuntimeError("client exit boom"))
        http_client = AsyncMock()
        adapter = MCPClientAdapter()
        adapter._config = _config()
        adapter._client = client
        adapter._http_client = http_client

        result = await adapter.disconnect()

        assert result.is_err
        assert "client exit boom" in str(result.error)
        http_client.aclose.assert_awaited_once()
        assert adapter._client is None and adapter._http_client is None

    async def test_disconnect_reports_http_error_and_clears_state(self) -> None:
        adapter = MCPClientAdapter()
        adapter._http_client = AsyncMock()
        adapter._http_client.aclose.side_effect = OSError("socket close boom")
        result = await adapter.disconnect()
        assert result.is_err and "socket close boom" in str(result.error)
        assert adapter._http_client is None

    async def test_disconnect_is_idempotent(self) -> None:
        result = await MCPClientAdapter().disconnect()
        assert result.is_ok


class TestFactory:
    def test_http_factory_preserves_security_headers_timeouts_and_auto_mode(self) -> None:
        http_client = MagicMock()
        sdk_client = MagicMock()
        transport = MagicMock()
        with (
            patch("httpx2.AsyncClient", return_value=http_client) as async_client,
            patch("httpx2.Timeout", return_value="timeout") as timeout,
            patch(
                "mcp.client.streamable_http.streamable_http_client", return_value=transport
            ) as stream,
            patch("mcp.client.Client", return_value=sdk_client) as client_class,
        ):
            resources = build_sdk_client(_config(TransportType.HTTP))

        timeout.assert_called_once_with(15, read=300.0)
        kwargs = async_client.call_args.kwargs
        assert kwargs["follow_redirects"] is False
        assert kwargs["headers"]["Authorization"] == "Bearer token"
        assert kwargs["headers"]["User-Agent"].startswith("ouroboros-mcp-client/")
        stream.assert_called_once_with("http://localhost:3000", http_client=http_client)
        assert client_class.call_args.kwargs["mode"] == "auto"
        assert resources.client is sdk_client and resources.http_client is http_client

    def test_stdio_factory_delegates_transport_lifecycle_to_high_level_client(self) -> None:
        transport = MagicMock()
        sdk_client = MagicMock()
        with (
            patch("mcp.client.stdio.stdio_client", return_value=transport),
            patch("mcp.client.Client", return_value=sdk_client) as client_class,
        ):
            resources = build_sdk_client(_config())
        assert client_class.call_args.args[0] is transport
        assert client_class.call_args.kwargs["mode"] == "auto"
        assert resources.http_client is None
