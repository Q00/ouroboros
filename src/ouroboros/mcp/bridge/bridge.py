"""MCPBridge — lifecycle-managed server-to-server MCP connections."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ouroboros.core.types import Result
from ouroboros.mcp.bridge.config import MCPBridgeConfig, load_bridge_config
from ouroboros.mcp.client.manager import MCPClientManager
from ouroboros.mcp.types import MCPServerInfo
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


@dataclass
class MCPBridge:
    """Manages the lifecycle of server-to-server MCP connections."""

    config: MCPBridgeConfig
    _manager: MCPClientManager = field(init=False, repr=False)
    _connected: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        self._manager = MCPClientManager(
            max_retries=self.config.retry_attempts,
            health_check_interval=self.config.health_check_interval,
            default_timeout=self.config.timeout_seconds,
        )

    @classmethod
    def from_config(cls, config: MCPBridgeConfig) -> MCPBridge:
        return cls(config=config)

    @classmethod
    def from_config_file(cls, path: Path) -> MCPBridge:
        result = load_bridge_config(path)
        if result.is_err:
            raise ValueError(f"Failed to load bridge config from {path}: {result.error}")
        return cls(config=result.value)

    @property
    def manager(self) -> MCPClientManager:
        return self._manager

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def tool_prefix(self) -> str:
        return self.config.tool_prefix

    async def connect(self) -> dict[str, Result[MCPServerInfo, Any]]:
        if self._connected:
            log.warning("bridge.already_connected")
            return {}
        for server_config in self.config.servers:
            await self._manager.add_server(server_config)
        results = await self._manager.connect_all()
        connected_count = sum(1 for r in results.values() if r.is_ok)
        log.info(
            "bridge.connected",
            connected=connected_count,
            total=len(results),
            servers=list(results.keys()),
        )
        self._connected = True
        return results

    async def disconnect(self) -> None:
        if not self._connected:
            return
        await self._manager.disconnect_all()
        self._connected = False
        log.info("bridge.disconnected")

    async def close(self) -> None:
        await self.disconnect()

    async def __aenter__(self) -> MCPBridge:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.disconnect()
