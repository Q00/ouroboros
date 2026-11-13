"""Pointing a host at the shared server is reversible and leaves other servers alone."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib

from ouroboros.gui import mcp_clients

URL = "http://127.0.0.1:8765/mcp"


def test_codex_round_trip_restores_the_previous_entry(isolated_home: Path) -> None:
    config = isolated_home / ".codex" / "config.toml"
    config.parent.mkdir()
    config.write_text(
        'model = "gpt-5.5"\n\n[mcp_servers.ouroboros]\ncommand = "uvx"\n'
        'args = ["ouroboros", "mcp", "serve"]\nenv = { A = "b" }\n\n'
        '[mcp_servers.other]\ncommand = "x"\n'
    )
    mcp_clients.set_shared("codex", True, port=8765)
    shared = tomllib.loads(config.read_text())
    assert shared["mcp_servers"]["ouroboros"] == {"url": URL}
    assert shared["mcp_servers"]["other"] == {"command": "x"}
    assert next(r for r in mcp_clients.status(8765) if r["id"] == "codex")["state"] == "shared"

    mcp_clients.set_shared("codex", False, port=8765)
    restored = tomllib.loads(config.read_text())
    assert restored["mcp_servers"]["ouroboros"]["command"] == "uvx"
    assert restored["mcp_servers"]["ouroboros"]["env"] == {"A": "b"}
    assert restored["mcp_servers"]["other"] == {"command": "x"}
    assert config.read_text().count("[mcp_servers.ouroboros]") == 1


def test_json_host_round_trip(isolated_home: Path) -> None:
    config = isolated_home / ".kiro" / "settings" / "mcp.json"
    config.parent.mkdir(parents=True)
    original = {
        "mcpServers": {"ouroboros": {"command": "uvx", "args": ["x"]}, "keep": {"command": "y"}}
    }
    config.write_text(json.dumps(original))
    mcp_clients.set_shared("kiro", True, port=8765)
    assert json.loads(config.read_text())["mcpServers"]["ouroboros"] == {"url": URL}
    mcp_clients.set_shared("kiro", False, port=8765)
    assert json.loads(config.read_text()) == original


def test_gemini_uses_http_url_key(isolated_home: Path) -> None:
    mcp_clients.set_shared("gemini", True, port=8765)
    data = json.loads((isolated_home / ".gemini" / "settings.json").read_text())
    assert data["mcpServers"]["ouroboros"] == {"httpUrl": URL}


def test_copilot_marks_http_transport(isolated_home: Path) -> None:
    mcp_clients.set_shared("copilot", True, port=8765)
    data = json.loads((isolated_home / ".copilot" / "mcp-config.json").read_text())
    assert data["mcpServers"]["ouroboros"] == {"type": "http", "url": URL}


def test_disabling_without_a_previous_entry_removes_it(isolated_home: Path) -> None:
    mcp_clients.set_shared("cursor", True, port=8765)
    mcp_clients.set_shared("cursor", False, port=8765)
    data = json.loads((isolated_home / ".cursor" / "mcp.json").read_text())
    assert "ouroboros" not in data["mcpServers"]
