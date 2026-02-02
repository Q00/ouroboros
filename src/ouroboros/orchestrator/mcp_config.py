"""MCP Client Configuration Loading for OrchestratorRunner.

This module provides configuration loading for external MCP servers that
the orchestrator can connect to for additional tools.

Configuration Schema (YAML):
    mcp_servers:
      - name: "filesystem"
        transport: "stdio"
        command: "npx"
        args: ["-y", "@anthropic/mcp-server-filesystem", "/workspace"]
      - name: "github"
        transport: "stdio"
        command: "npx"
        args: ["-y", "@anthropic/mcp-server-github"]
        env:
          GITHUB_TOKEN: "${GITHUB_TOKEN}"  # Environment variable reference
    connection:
      timeout_seconds: 30
      retry_attempts: 3

Security Features:
    - Credentials via environment variables only (no plaintext)
    - Config file permission warnings (world-readable files)
    - Server name sanitization for logging
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPClientError
from ouroboros.mcp.types import MCPServerConfig, TransportType
from ouroboros.observability.logging import get_logger

log = get_logger(__name__)


# Pattern for environment variable substitution: ${VAR_NAME}
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class MCPConnectionConfig:
    """Connection configuration for MCP servers.

    Attributes:
        timeout_seconds: Default timeout for MCP operations.
        retry_attempts: Number of retry attempts for failed connections.
        health_check_interval: Seconds between health checks.
    """

    timeout_seconds: float = 30.0
    retry_attempts: int = 3
    health_check_interval: float = 60.0


@dataclass(frozen=True, slots=True)
class MCPClientConfig:
    """Complete MCP client configuration.

    Attributes:
        servers: List of MCP server configurations.
        connection: Connection settings.
        tool_prefix: Optional prefix for all MCP tool names.
    """

    servers: tuple[MCPServerConfig, ...] = field(default_factory=tuple)
    connection: MCPConnectionConfig = field(default_factory=MCPConnectionConfig)
    tool_prefix: str = ""


class ConfigError(MCPClientError):
    """Error during configuration loading."""

    def __init__(
        self,
        message: str,
        *,
        config_path: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Initialize configuration error.

        Args:
            message: Error description.
            config_path: Path to the config file.
            details: Additional error details.
        """
        full_details = details or {}
        if config_path:
            full_details["config_path"] = config_path
        super().__init__(message, is_retriable=False, details=full_details)
        self.config_path = config_path


def substitute_env_vars(value: str) -> str:
    """Substitute environment variable references in a string.

    Replaces ${VAR_NAME} with the value of the environment variable.
    Raises ValueError if a referenced variable is not set.

    Args:
        value: String potentially containing ${VAR_NAME} references.

    Returns:
        String with environment variables substituted.

    Raises:
        ValueError: If an environment variable is not set.
    """

    def replace_var(match: re.Match[str]) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f"Environment variable not set: {var_name}")
        return env_value

    return ENV_VAR_PATTERN.sub(replace_var, value)


def substitute_env_vars_in_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively substitute environment variables in a dict.

    Args:
        data: Dictionary potentially containing ${VAR_NAME} references.

    Returns:
        Dictionary with environment variables substituted.

    Raises:
        ValueError: If an environment variable is not set.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = substitute_env_vars(value)
        elif isinstance(value, dict):
            result[key] = substitute_env_vars_in_dict(value)
        elif isinstance(value, list):
            result[key] = [
                substitute_env_vars(v) if isinstance(v, str) else v for v in value
            ]
        else:
            result[key] = value
    return result


def check_file_permissions(path: Path) -> list[str]:
    """Check file permissions and return security warnings.

    Args:
        path: Path to the config file.

    Returns:
        List of security warning messages.
    """
    warnings: list[str] = []

    try:
        mode = path.stat().st_mode
        # Check if world-readable
        if mode & stat.S_IROTH:
            warnings.append(
                f"Config file is world-readable: {path}. "
                "Consider restricting permissions with: chmod 600"
            )
        # Check if group-readable (less severe)
        if mode & stat.S_IRGRP:
            warnings.append(
                f"Config file is group-readable: {path}. "
                "Consider restricting permissions with: chmod 600"
            )
    except OSError as e:
        log.warning(
            "orchestrator.mcp_config.permission_check_failed",
            path=str(path),
            error=str(e),
        )

    return warnings


def sanitize_server_name(name: str) -> str:
    """Sanitize server name for safe logging.

    Removes potentially sensitive information from server names
    while keeping them identifiable.

    Args:
        name: Server name to sanitize.

    Returns:
        Sanitized server name.
    """
    # Remove anything that looks like a credential or token
    sanitized = re.sub(r"(token|key|secret|password|auth)[^a-z]*[a-z0-9]+", r"\1=***", name, flags=re.IGNORECASE)
    # Truncate if too long
    if len(sanitized) > 50:
        sanitized = sanitized[:47] + "..."
    return sanitized


def parse_server_config(server_data: dict[str, Any]) -> Result[MCPServerConfig, ConfigError]:
    """Parse a single server configuration.

    Args:
        server_data: Server configuration dictionary.

    Returns:
        Result containing MCPServerConfig or ConfigError.
    """
    try:
        name = server_data.get("name")
        if not name:
            return Result.err(ConfigError("Server configuration missing 'name' field"))

        transport_str = server_data.get("transport", "stdio")
        try:
            transport = TransportType(transport_str)
        except ValueError:
            return Result.err(
                ConfigError(
                    f"Invalid transport type: {transport_str}",
                    details={"valid_types": [t.value for t in TransportType]},
                )
            )

        # Handle environment variable substitution in env dict
        env_raw = server_data.get("env", {})
        try:
            env = substitute_env_vars_in_dict(env_raw)
        except ValueError as e:
            return Result.err(
                ConfigError(
                    f"Environment variable substitution failed for server '{name}': {e}",
                    details={"server": sanitize_server_name(name)},
                )
            )

        # Build config
        config = MCPServerConfig(
            name=name,
            transport=transport,
            command=server_data.get("command"),
            args=tuple(server_data.get("args", [])),
            url=server_data.get("url"),
            env=env,
            timeout=server_data.get("timeout", 30.0),
            headers=server_data.get("headers", {}),
        )

        return Result.ok(config)

    except Exception as e:
        return Result.err(
            ConfigError(
                f"Failed to parse server configuration: {e}",
                details={"error_type": type(e).__name__},
            )
        )


def load_mcp_config(config_path: Path) -> Result[MCPClientConfig, ConfigError]:
    """Load MCP client configuration from a YAML file.

    Performs:
    - YAML parsing
    - Environment variable substitution for credentials
    - File permission security checks
    - Server configuration validation

    Args:
        config_path: Path to the YAML configuration file.

    Returns:
        Result containing MCPClientConfig or ConfigError.
    """
    log.info(
        "orchestrator.mcp_config.loading",
        path=str(config_path),
    )

    # Check file exists
    if not config_path.exists():
        return Result.err(
            ConfigError(
                f"Configuration file not found: {config_path}",
                config_path=str(config_path),
            )
        )

    if not config_path.is_file():
        return Result.err(
            ConfigError(
                f"Configuration path is not a file: {config_path}",
                config_path=str(config_path),
            )
        )

    # Check file permissions
    permission_warnings = check_file_permissions(config_path)
    for warning in permission_warnings:
        log.warning(
            "orchestrator.mcp_config.security_warning",
            warning=warning,
        )

    # Load YAML
    try:
        with open(config_path) as f:
            raw_config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return Result.err(
            ConfigError(
                f"Invalid YAML in configuration file: {e}",
                config_path=str(config_path),
            )
        )
    except OSError as e:
        return Result.err(
            ConfigError(
                f"Failed to read configuration file: {e}",
                config_path=str(config_path),
            )
        )

    if not raw_config:
        return Result.err(
            ConfigError(
                "Configuration file is empty",
                config_path=str(config_path),
            )
        )

    if not isinstance(raw_config, dict):
        return Result.err(
            ConfigError(
                "Configuration must be a YAML mapping",
                config_path=str(config_path),
            )
        )

    # Parse servers
    servers: list[MCPServerConfig] = []
    servers_data = raw_config.get("mcp_servers", [])

    if not isinstance(servers_data, list):
        return Result.err(
            ConfigError(
                "'mcp_servers' must be a list",
                config_path=str(config_path),
            )
        )

    for i, server_data in enumerate(servers_data):
        if not isinstance(server_data, dict):
            return Result.err(
                ConfigError(
                    f"Server configuration at index {i} must be a mapping",
                    config_path=str(config_path),
                )
            )

        result = parse_server_config(server_data)
        if result.is_err:
            return Result.err(
                ConfigError(
                    f"Server configuration error at index {i}: {result.error}",
                    config_path=str(config_path),
                )
            )
        servers.append(result.value)

    # Parse connection config
    connection_data = raw_config.get("connection", {})
    connection = MCPConnectionConfig(
        timeout_seconds=connection_data.get("timeout_seconds", 30.0),
        retry_attempts=connection_data.get("retry_attempts", 3),
        health_check_interval=connection_data.get("health_check_interval", 60.0),
    )

    # Get tool prefix
    tool_prefix = raw_config.get("tool_prefix", "")

    config = MCPClientConfig(
        servers=tuple(servers),
        connection=connection,
        tool_prefix=tool_prefix,
    )

    log.info(
        "orchestrator.mcp_config.loaded",
        server_count=len(servers),
        servers=[sanitize_server_name(s.name) for s in servers],
        tool_prefix=tool_prefix or "(none)",
    )

    return Result.ok(config)


__all__ = [
    "ConfigError",
    "MCPClientConfig",
    "MCPConnectionConfig",
    "check_file_permissions",
    "load_mcp_config",
    "sanitize_server_name",
    "substitute_env_vars",
]
