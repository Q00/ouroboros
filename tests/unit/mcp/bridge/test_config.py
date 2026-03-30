"""Tests for MCPBridgeConfig and config discovery."""

from __future__ import annotations

from pathlib import Path

import pytest

from ouroboros.mcp.bridge.config import MCPBridgeConfig, discover_config, load_bridge_config


class TestMCPBridgeConfig:
    def test_default_values(self):
        config = MCPBridgeConfig()
        assert config.servers == ()
        assert config.timeout_seconds == 30.0
        assert config.retry_attempts == 3
        assert config.tool_prefix == ""

    def test_frozen(self):
        config = MCPBridgeConfig()
        with pytest.raises(AttributeError):
            config.timeout_seconds = 10.0

    def test_custom_values(self):
        config = MCPBridgeConfig(timeout_seconds=10.0, retry_attempts=5, tool_prefix="upstream_")
        assert config.timeout_seconds == 10.0
        assert config.retry_attempts == 5
        assert config.tool_prefix == "upstream_"


class TestDiscoverConfig:
    def test_env_var_takes_precedence(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        config_file = tmp_path / "custom.yaml"
        config_file.write_text("mcp_servers: []")
        monkeypatch.setenv("OUROBOROS_MCP_CONFIG", str(config_file))
        assert discover_config() == config_file

    def test_cwd_config(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("OUROBOROS_MCP_CONFIG", raising=False)
        cwd_config = tmp_path / ".ouroboros" / "mcp_servers.yaml"
        cwd_config.parent.mkdir(parents=True)
        cwd_config.write_text("mcp_servers: []")
        assert discover_config(cwd=tmp_path) == cwd_config


class TestLoadBridgeConfig:
    def test_load_valid_config(self, tmp_path: Path):
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(
            "mcp_servers:\n  - name: test\n    transport: stdio\n    command: echo\n    args: ['hi']\nconnection:\n  timeout_seconds: 15\n  retry_attempts: 2\n"
        )
        result = load_bridge_config(config_file)
        assert result.is_ok
        assert len(result.value.servers) == 1
        assert result.value.timeout_seconds == 15.0

    def test_load_preserves_tool_prefix_and_health_check(self, tmp_path: Path):
        config_file = tmp_path / "mcp.yaml"
        config_file.write_text(
            "mcp_servers:\n"
            "  - name: test\n"
            "    transport: stdio\n"
            "    command: echo\n"
            "    args: ['hi']\n"
            "tool_prefix: upstream_\n"
            "connection:\n"
            "  timeout_seconds: 10\n"
            "  retry_attempts: 1\n"
            "  health_check_interval: 45.0\n"
        )
        result = load_bridge_config(config_file)
        assert result.is_ok
        assert result.value.tool_prefix == "upstream_"
        assert result.value.health_check_interval == 45.0

    def test_load_nonexistent_file(self, tmp_path: Path):
        result = load_bridge_config(tmp_path / "nonexistent.yaml")
        assert result.is_err
