"""Tests for bridge factory functions."""

from __future__ import annotations

import ouroboros.mcp.bridge.config as bridge_config
from ouroboros.mcp.bridge.factory import (
    create_bridge_from_env,
)


class TestCreateBridgeFromEnv:
    def test_returns_none_when_no_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OUROBOROS_MCP_CONFIG", raising=False)
        monkeypatch.setattr(
            bridge_config,
            "_HOME_CONFIG",
            tmp_path / "home" / ".ouroboros" / "mcp_servers.yaml",
        )
        result = create_bridge_from_env(cwd=tmp_path)
        assert result is None

    def test_returns_bridge_when_config_exists(self, tmp_path, monkeypatch):
        monkeypatch.delenv("OUROBOROS_MCP_CONFIG", raising=False)
        monkeypatch.setattr(
            bridge_config,
            "_HOME_CONFIG",
            tmp_path / "home" / ".ouroboros" / "mcp_servers.yaml",
        )
        d = tmp_path / ".ouroboros"
        d.mkdir()
        (d / "mcp_servers.yaml").write_text(
            "mcp_servers:\n  - name: local\n    transport: stdio\n    command: echo\n    args: ['hello']\n"
        )
        bridge = create_bridge_from_env(cwd=tmp_path)
        assert bridge is not None
        assert not bridge.is_connected
