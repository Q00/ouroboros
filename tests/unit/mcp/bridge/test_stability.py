"""Stability tests for MCP bridge — timeout, hang prevention, resource cleanup."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.bridge.bridge import MCPBridge
from ouroboros.mcp.bridge.config import MCPBridgeConfig
from ouroboros.mcp.types import MCPServerConfig, TransportType


class TestTimeoutBehavior:
    @pytest.mark.asyncio
    async def test_connect_to_nonexistent_command_does_not_hang(self):
        config = MCPBridgeConfig(
            servers=(
                MCPServerConfig(
                    name="ghost",
                    transport=TransportType.STDIO,
                    command="nonexistent_command_xyz_12345",
                    args=(),
                ),
            ),
            timeout_seconds=3.0,
            retry_attempts=1,
        )
        bridge = MCPBridge.from_config(config)
        results = await bridge.connect()
        assert "ghost" in results
        assert results["ghost"].is_err
        await bridge.disconnect()

    @pytest.mark.asyncio
    async def test_partial_failure_does_not_hang(self):
        config = MCPBridgeConfig(
            servers=(
                MCPServerConfig(
                    name="ok", transport=TransportType.STDIO, command="echo", args=("1",)
                ),
                MCPServerConfig(
                    name="bad", transport=TransportType.STDIO, command="false", args=()
                ),
            ),
            timeout_seconds=3.0,
            retry_attempts=1,
        )
        bridge = MCPBridge.from_config(config)
        mock_results = {"ok": Result.ok(None), "bad": Result.err(Exception("fail"))}
        with (
            patch.object(bridge._manager, "add_server", new_callable=AsyncMock),
            patch.object(
                bridge._manager, "connect_all", new_callable=AsyncMock, return_value=mock_results
            ),
        ):
            results = await bridge.connect()
            assert results["ok"].is_ok
            assert results["bad"].is_err
            assert bridge.is_connected
        await bridge.close()


class TestConnectionLifecycle:
    @pytest.mark.asyncio
    async def test_double_disconnect_is_safe(self):
        bridge = MCPBridge.from_config(MCPBridgeConfig())
        bridge._connected = True
        with patch.object(bridge._manager, "disconnect_all", new_callable=AsyncMock):
            await bridge.disconnect()
        await bridge.disconnect()  # no-op
        assert not bridge.is_connected

    @pytest.mark.asyncio
    async def test_close_after_failed_connect(self):
        config = MCPBridgeConfig(
            servers=(
                MCPServerConfig(
                    name="fail", transport=TransportType.STDIO, command="false", args=()
                ),
            )
        )
        bridge = MCPBridge.from_config(config)
        with (
            patch.object(bridge._manager, "add_server", new_callable=AsyncMock),
            patch.object(
                bridge._manager,
                "connect_all",
                new_callable=AsyncMock,
                side_effect=Exception("boom"),
            ),
        ):
            with pytest.raises(Exception):
                await bridge.connect()
        await bridge.close()


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_tool_calls(self):
        bridge = MCPBridge.from_config(MCPBridgeConfig())
        call_count = 0

        async def mock_call(name, args):
            nonlocal call_count
            await asyncio.sleep(0.01)
            call_count += 1
            return Result.ok(MagicMock())

        with patch.object(bridge._manager, "call_tool_auto", side_effect=mock_call):
            tasks = [bridge.manager.call_tool_auto(f"tool_{i}", {}) for i in range(10)]
            await asyncio.gather(*tasks)
            assert call_count == 10


class TestResourceCleanup:
    @pytest.mark.asyncio
    async def test_context_manager_cleans_up_on_exception(self):
        bridge = MCPBridge.from_config(MCPBridgeConfig())
        mock_disconnect = AsyncMock()
        with (
            patch.object(bridge, "connect", new_callable=AsyncMock),
            patch.object(bridge, "disconnect", mock_disconnect),
        ):
            with pytest.raises(ValueError):
                async with bridge:
                    raise ValueError("test")
            mock_disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_server_shutdown_calls_bridge_close(self):
        from ouroboros.mcp.server.adapter import MCPServerAdapter

        server = MCPServerAdapter(name="test", version="1.0")
        mock_bridge = MagicMock()
        mock_bridge.close = AsyncMock()
        server.register_owned_resource(mock_bridge)
        await server.shutdown()
        mock_bridge.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_cleans_transport_cm(self):
        from ouroboros.mcp.client.adapter import MCPClientAdapter

        adapter = MCPClientAdapter()
        adapter._session = MagicMock()
        adapter._session.__aexit__ = AsyncMock()
        adapter._config = MagicMock(name="test")
        mock_cm = MagicMock()
        mock_cm.__aexit__ = AsyncMock()
        adapter._transport_cm = mock_cm
        result = await adapter.disconnect()
        assert result.is_ok
        mock_cm.__aexit__.assert_called_once()
        assert adapter._transport_cm is None
