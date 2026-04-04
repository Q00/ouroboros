"""Integration tests for MCP server-to-server communication via bridge."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.bridge.bridge import MCPBridge
from ouroboros.mcp.bridge.config import MCPBridgeConfig
from ouroboros.mcp.client.manager import MCPClientManager
from ouroboros.mcp.types import MCPServerConfig, TransportType


class TestMCPBridgeReusability:
    def test_bridge_config_accepts_multiple_servers(self):
        config = MCPBridgeConfig(
            servers=(
                MCPServerConfig(
                    name="calc", transport=TransportType.STDIO, command="echo", args=("1",)
                ),
                MCPServerConfig(
                    name="greet", transport=TransportType.STDIO, command="echo", args=("2",)
                ),
            ),
            tool_prefix="upstream_",
        )
        assert len(config.servers) == 2

    def test_bridge_creates_manager(self):
        config = MCPBridgeConfig(
            servers=(
                MCPServerConfig(name="a", transport=TransportType.STDIO, command="echo", args=()),
            ),
        )
        bridge = MCPBridge.from_config(config)
        assert isinstance(bridge.manager, MCPClientManager)

    @pytest.mark.asyncio
    async def test_lifecycle_with_multiple_configs(self):
        config = MCPBridgeConfig(
            servers=(
                MCPServerConfig(
                    name="s1", transport=TransportType.STDIO, command="echo", args=("1",)
                ),
                MCPServerConfig(
                    name="s2", transport=TransportType.STDIO, command="echo", args=("2",)
                ),
            ),
        )
        bridge = MCPBridge.from_config(config)
        with (
            patch.object(bridge._manager, "add_server", new_callable=AsyncMock) as ma,
            patch.object(
                bridge._manager,
                "connect_all",
                new_callable=AsyncMock,
                return_value={"s1": Result.ok(None), "s2": Result.ok(None)},
            ),
        ):
            results = await bridge.connect()
            assert len(results) == 2
            assert ma.call_count == 2
        with patch.object(bridge._manager, "disconnect_all", new_callable=AsyncMock):
            await bridge.disconnect()


class TestBridgeToProviderIntegration:
    def test_manager_compatible_with_tool_provider(self):
        from ouroboros.orchestrator.mcp_tools import MCPToolProvider

        bridge = MCPBridge.from_config(MCPBridgeConfig())
        provider = MCPToolProvider(bridge.manager)
        assert provider is not None

    def test_manager_with_prefix(self):
        from ouroboros.orchestrator.mcp_tools import MCPToolProvider

        bridge = MCPBridge.from_config(MCPBridgeConfig(tool_prefix="ext_"))
        provider = MCPToolProvider(bridge.manager, tool_prefix=bridge.tool_prefix)
        assert provider._tool_prefix == "ext_"
