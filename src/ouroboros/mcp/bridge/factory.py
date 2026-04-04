"""Convenience factories for creating MCP bridges."""

from __future__ import annotations

from pathlib import Path

from ouroboros.mcp.bridge.bridge import MCPBridge
from ouroboros.mcp.bridge.config import MCPBridgeConfig, discover_config, load_bridge_config
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


async def create_bridge(config: MCPBridgeConfig) -> MCPBridge:
    bridge = MCPBridge.from_config(config)
    await bridge.connect()
    return bridge


async def create_bridge_from_config_file(path: Path) -> MCPBridge:
    bridge = MCPBridge.from_config_file(path)
    await bridge.connect()
    return bridge


def create_bridge_from_env(cwd: Path | None = None) -> MCPBridge | None:
    config_path = discover_config(cwd)
    if config_path is None:
        return None
    result = load_bridge_config(config_path)
    if result.is_err:
        log.warning(
            "bridge.factory.config_load_failed", path=str(config_path), error=str(result.error)
        )
        return None
    log.info("bridge.factory.created_from_env", config_path=str(config_path))
    return MCPBridge.from_config(result.value)
