"""Bridge configuration for MCP server-to-server communication."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPClientError
from ouroboros.mcp.types import MCPServerConfig
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)

_ENV_VAR = "OUROBOROS_MCP_CONFIG"
_HOME_CONFIG = Path.home() / ".ouroboros" / "mcp_servers.yaml"


@dataclass(frozen=True, slots=True)
class MCPBridgeConfig:
    """Configuration for an MCP bridge."""

    servers: tuple[MCPServerConfig, ...] = field(default_factory=tuple)
    timeout_seconds: float = 30.0
    retry_attempts: int = 3
    health_check_interval: float = 60.0
    tool_prefix: str = ""


def discover_config(cwd: Path | None = None) -> Path | None:
    """Auto-discover bridge configuration file."""
    env_path = os.environ.get(_ENV_VAR)
    if env_path:
        p = Path(env_path)
        if p.is_file():
            log.info("bridge.config.discovered", source="env", path=str(p))
            return p

    if _HOME_CONFIG.is_file():
        log.info("bridge.config.discovered", source="home", path=str(_HOME_CONFIG))
        return _HOME_CONFIG

    if cwd is not None:
        cwd_config = cwd / ".ouroboros" / "mcp_servers.yaml"
        if cwd_config.is_file():
            log.info("bridge.config.discovered", source="cwd", path=str(cwd_config))
            return cwd_config

    return None


def load_bridge_config(config_path: Path) -> Result[MCPBridgeConfig, MCPClientError]:
    """Load bridge configuration from a YAML file."""
    from ouroboros.orchestrator.mcp_config import load_mcp_config

    result = load_mcp_config(config_path)
    if result.is_err:
        return Result.err(MCPClientError(f"Failed to load bridge config: {result.error}"))

    client_config = result.value
    return Result.ok(
        MCPBridgeConfig(
            servers=client_config.servers,
            timeout_seconds=client_config.connection.timeout_seconds,
            retry_attempts=client_config.connection.retry_attempts,
            health_check_interval=client_config.connection.health_check_interval,
            tool_prefix=client_config.tool_prefix,
        )
    )
