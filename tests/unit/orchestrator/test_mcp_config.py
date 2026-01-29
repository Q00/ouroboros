"""Unit tests for MCP configuration loading.

Tests cover:
- Configuration file loading
- Environment variable substitution
- File permission security checks
- Configuration validation
- Error handling
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

import pytest
import yaml

from ouroboros.orchestrator.mcp_config import (
    ConfigError,
    MCPClientConfig,
    MCPConnectionConfig,
    check_file_permissions,
    load_mcp_config,
    sanitize_server_name,
    substitute_env_vars,
    substitute_env_vars_in_dict,
)


class TestSubstituteEnvVars:
    """Tests for environment variable substitution."""

    def test_no_substitution(self) -> None:
        """Test string without variables."""
        result = substitute_env_vars("plain text")
        assert result == "plain text"

    def test_single_substitution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test single variable substitution."""
        monkeypatch.setenv("TEST_VAR", "test_value")
        result = substitute_env_vars("prefix_${TEST_VAR}_suffix")
        assert result == "prefix_test_value_suffix"

    def test_multiple_substitutions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test multiple variable substitutions."""
        monkeypatch.setenv("VAR1", "first")
        monkeypatch.setenv("VAR2", "second")
        result = substitute_env_vars("${VAR1} and ${VAR2}")
        assert result == "first and second"

    def test_missing_variable(self) -> None:
        """Test error on missing variable."""
        with pytest.raises(ValueError, match="Environment variable not set"):
            substitute_env_vars("${DEFINITELY_NOT_SET_VAR_12345}")

    def test_empty_variable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test empty variable value."""
        monkeypatch.setenv("EMPTY_VAR", "")
        result = substitute_env_vars("value=${EMPTY_VAR}")
        assert result == "value="


class TestSubstituteEnvVarsInDict:
    """Tests for recursive environment variable substitution."""

    def test_nested_dict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test substitution in nested dict."""
        monkeypatch.setenv("TOKEN", "secret123")
        data = {
            "outer": {
                "inner": "${TOKEN}",
            },
            "plain": "text",
        }
        result = substitute_env_vars_in_dict(data)

        assert result["outer"]["inner"] == "secret123"
        assert result["plain"] == "text"

    def test_list_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Test substitution in list values."""
        monkeypatch.setenv("ITEM", "value")
        data = {
            "items": ["${ITEM}", "plain", "${ITEM}"],
        }
        result = substitute_env_vars_in_dict(data)

        assert result["items"] == ["value", "plain", "value"]

    def test_non_string_values(self) -> None:
        """Test that non-string values are preserved."""
        data = {
            "number": 42,
            "boolean": True,
            "null": None,
        }
        result = substitute_env_vars_in_dict(data)

        assert result["number"] == 42
        assert result["boolean"] is True
        assert result["null"] is None


class TestSanitizeServerName:
    """Tests for server name sanitization."""

    def test_plain_name(self) -> None:
        """Test plain server name."""
        assert sanitize_server_name("filesystem") == "filesystem"

    def test_name_with_token(self) -> None:
        """Test name containing token-like pattern."""
        result = sanitize_server_name("server_token_abc123")
        assert "abc123" not in result
        assert "***" in result

    def test_long_name_truncation(self) -> None:
        """Test long name truncation."""
        long_name = "a" * 100
        result = sanitize_server_name(long_name)
        assert len(result) == 50
        assert result.endswith("...")


class TestCheckFilePermissions:
    """Tests for file permission checks."""

    def test_world_readable_warning(self, tmp_path: Path) -> None:
        """Test warning for world-readable file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("test: value")
        config_file.chmod(0o644)  # World-readable

        warnings = check_file_permissions(config_file)

        assert len(warnings) > 0
        assert any("world-readable" in w.lower() for w in warnings)

    def test_restricted_permissions_no_warning(self, tmp_path: Path) -> None:
        """Test no warning for restricted file."""
        config_file = tmp_path / "config.yaml"
        config_file.write_text("test: value")
        config_file.chmod(0o600)  # Owner only

        warnings = check_file_permissions(config_file)

        # Should have no world-readable warning
        assert not any("world-readable" in w.lower() for w in warnings)


class TestLoadMCPConfig:
    """Tests for load_mcp_config function."""

    def test_load_valid_config(self, tmp_path: Path) -> None:
        """Test loading valid configuration."""
        config_data = {
            "mcp_servers": [
                {
                    "name": "filesystem",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@anthropic/mcp-server-filesystem", "/workspace"],
                },
            ],
            "connection": {
                "timeout_seconds": 60,
                "retry_attempts": 5,
            },
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_ok
        config = result.value
        assert len(config.servers) == 1
        assert config.servers[0].name == "filesystem"
        assert config.servers[0].command == "npx"
        assert config.connection.timeout_seconds == 60
        assert config.connection.retry_attempts == 5

    def test_load_with_env_vars(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test loading config with environment variable substitution."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret123")

        config_data = {
            "mcp_servers": [
                {
                    "name": "github",
                    "transport": "stdio",
                    "command": "npx",
                    "args": ["-y", "@anthropic/mcp-server-github"],
                    "env": {
                        "GITHUB_TOKEN": "${GITHUB_TOKEN}",
                    },
                },
            ],
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_ok
        config = result.value
        assert config.servers[0].env["GITHUB_TOKEN"] == "ghp_secret123"

    def test_load_missing_env_var(self, tmp_path: Path) -> None:
        """Test error on missing environment variable."""
        config_data = {
            "mcp_servers": [
                {
                    "name": "test",
                    "transport": "stdio",
                    "command": "test",
                    "env": {
                        "SECRET": "${DEFINITELY_NOT_SET_VAR_ABC}",
                    },
                },
            ],
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_err
        assert "Environment variable" in str(result.error)

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        """Test error on nonexistent file."""
        result = load_mcp_config(tmp_path / "nonexistent.yaml")

        assert result.is_err
        assert "not found" in str(result.error).lower()

    def test_load_invalid_yaml(self, tmp_path: Path) -> None:
        """Test error on invalid YAML."""
        config_file = tmp_path / "invalid.yaml"
        config_file.write_text("{ invalid yaml: [")
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_err
        assert "yaml" in str(result.error).lower()

    def test_load_empty_file(self, tmp_path: Path) -> None:
        """Test error on empty file."""
        config_file = tmp_path / "empty.yaml"
        config_file.write_text("")
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_err
        assert "empty" in str(result.error).lower()

    def test_load_invalid_transport(self, tmp_path: Path) -> None:
        """Test error on invalid transport type."""
        config_data = {
            "mcp_servers": [
                {
                    "name": "test",
                    "transport": "invalid_transport",
                    "command": "test",
                },
            ],
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_err
        assert "transport" in str(result.error).lower()

    def test_load_missing_server_name(self, tmp_path: Path) -> None:
        """Test error on missing server name."""
        config_data = {
            "mcp_servers": [
                {
                    "transport": "stdio",
                    "command": "test",
                },
            ],
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_err
        assert "name" in str(result.error).lower()

    def test_load_with_tool_prefix(self, tmp_path: Path) -> None:
        """Test loading config with tool prefix."""
        config_data = {
            "mcp_servers": [
                {
                    "name": "test",
                    "transport": "stdio",
                    "command": "test",
                },
            ],
            "tool_prefix": "mcp_",
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_ok
        assert result.value.tool_prefix == "mcp_"

    def test_load_multiple_servers(self, tmp_path: Path) -> None:
        """Test loading config with multiple servers."""
        config_data = {
            "mcp_servers": [
                {
                    "name": "server1",
                    "transport": "stdio",
                    "command": "cmd1",
                },
                {
                    "name": "server2",
                    "transport": "stdio",
                    "command": "cmd2",
                },
            ],
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_ok
        assert len(result.value.servers) == 2

    def test_default_connection_settings(self, tmp_path: Path) -> None:
        """Test default connection settings."""
        config_data = {
            "mcp_servers": [
                {
                    "name": "test",
                    "transport": "stdio",
                    "command": "test",
                },
            ],
        }

        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(yaml.dump(config_data))
        config_file.chmod(0o600)

        result = load_mcp_config(config_file)

        assert result.is_ok
        assert result.value.connection.timeout_seconds == 30.0
        assert result.value.connection.retry_attempts == 3


class TestMCPClientConfig:
    """Tests for MCPClientConfig dataclass."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = MCPClientConfig()

        assert len(config.servers) == 0
        assert config.tool_prefix == ""
        assert config.connection.timeout_seconds == 30.0

    def test_frozen(self) -> None:
        """Test that config is immutable."""
        config = MCPClientConfig()

        with pytest.raises(AttributeError):
            config.tool_prefix = "changed"  # type: ignore


class TestMCPConnectionConfig:
    """Tests for MCPConnectionConfig dataclass."""

    def test_defaults(self) -> None:
        """Test default values."""
        config = MCPConnectionConfig()

        assert config.timeout_seconds == 30.0
        assert config.retry_attempts == 3
        assert config.health_check_interval == 60.0

    def test_custom_values(self) -> None:
        """Test custom values."""
        config = MCPConnectionConfig(
            timeout_seconds=60.0,
            retry_attempts=5,
            health_check_interval=120.0,
        )

        assert config.timeout_seconds == 60.0
        assert config.retry_attempts == 5
        assert config.health_check_interval == 120.0


class TestConfigError:
    """Tests for ConfigError exception."""

    def test_create_error(self) -> None:
        """Test creating config error."""
        error = ConfigError(
            "Test error message",
            config_path="/path/to/config.yaml",
        )

        assert "Test error message" in str(error)
        assert error.config_path == "/path/to/config.yaml"

    def test_error_with_details(self) -> None:
        """Test error with additional details."""
        error = ConfigError(
            "Test error",
            config_path="/path",
            details={"key": "value"},
        )

        assert error.details is not None
        assert error.details.get("config_path") == "/path"
        assert error.details.get("key") == "value"
