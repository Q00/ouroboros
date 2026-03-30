"""Tests for MCPBridge lifecycle management."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.mcp.bridge.bridge import MCPBridge
from ouroboros.mcp.bridge.config import MCPBridgeConfig
from ouroboros.mcp.types import MCPServerConfig, TransportType


@pytest.fixture
def sample_config() -> MCPBridgeConfig:
    return MCPBridgeConfig(
        servers=(
            MCPServerConfig(
                name="test-server", transport=TransportType.STDIO, command="echo", args=("hello",)
            ),
        ),
        timeout_seconds=5.0,
        retry_attempts=1,
        tool_prefix="test_",
    )


class TestMCPBridge:
    def test_from_config(self, sample_config):
        bridge = MCPBridge.from_config(sample_config)
        assert bridge.config is sample_config
        assert not bridge.is_connected
        assert bridge.tool_prefix == "test_"

    @pytest.mark.asyncio
    async def test_connect_disconnect(self, sample_config):
        bridge = MCPBridge.from_config(sample_config)
        with (
            patch.object(bridge._manager, "add_server", new_callable=AsyncMock),
            patch.object(bridge._manager, "connect_all", new_callable=AsyncMock, return_value={}),
        ):
            await bridge.connect()
            assert bridge.is_connected
        with patch.object(bridge._manager, "disconnect_all", new_callable=AsyncMock):
            await bridge.disconnect()
            assert not bridge.is_connected

    @pytest.mark.asyncio
    async def test_double_connect_warns(self, sample_config):
        bridge = MCPBridge.from_config(sample_config)
        bridge._connected = True
        assert await bridge.connect() == {}

    @pytest.mark.asyncio
    async def test_context_manager(self, sample_config):
        bridge = MCPBridge.from_config(sample_config)
        with (
            patch.object(bridge, "connect", new_callable=AsyncMock) as mc,
            patch.object(bridge, "disconnect", new_callable=AsyncMock) as md,
        ):
            async with bridge as b:
                assert b is bridge
            mc.assert_called_once()
            md.assert_called_once()

    def test_from_config_file_valid(self, tmp_path):
        f = tmp_path / "mcp.yaml"
        f.write_text(
            "mcp_servers:\n  - name: fs\n    transport: stdio\n    command: echo\n    args: ['test']\n"
        )
        bridge = MCPBridge.from_config_file(f)
        assert len(bridge.config.servers) == 1
