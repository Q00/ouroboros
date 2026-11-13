"""Connect-page detection: integration state and sign-in, read-only."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess

from ouroboros.gui import clients


def _cards(monkeypatch, installed: dict[str, str | None]) -> dict[str, dict]:
    monkeypatch.setattr(clients, "_detect_installed", lambda: installed)
    monkeypatch.setattr(clients, "_sign_in", lambda *_args: None)
    return {card["id"]: card for card in clients.list_clients(fresh=True)}


def test_codex_skills_without_mcp_is_partial(isolated_home: Path, monkeypatch) -> None:
    skills = isolated_home / ".codex" / "skills" / "ouroboros-run"
    skills.mkdir(parents=True)
    (isolated_home / ".codex" / "config.toml").write_text('model = "gpt-5.5"\n')
    card = _cards(monkeypatch, {"codex": "/usr/local/bin/codex"})["codex"]
    assert card["installed"] is True
    assert card["integration"]["state"] == "partial"


def test_codex_mcp_and_skills_is_connected(isolated_home: Path, monkeypatch) -> None:
    (isolated_home / ".codex" / "skills" / "ouroboros-run").mkdir(parents=True)
    (isolated_home / ".codex" / "config.toml").write_text(
        '[mcp_servers.ouroboros]\ncommand = "uvx"\nargs = []\n'
    )
    card = _cards(monkeypatch, {"codex": "/usr/local/bin/codex"})["codex"]
    assert card["integration"]["state"] == "connected"


def test_kiro_json_registration(isolated_home: Path, monkeypatch) -> None:
    path = isolated_home / ".kiro" / "settings" / "mcp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"mcpServers": {"ouroboros": {"command": "uvx"}}}))
    assert _cards(monkeypatch, {})["kiro"]["integration"]["state"] == "connected"


def test_runtime_only_tools(isolated_home: Path, monkeypatch) -> None:
    cards = _cards(monkeypatch, {})
    assert cards["goose"]["integration"]["state"] == "runtime"
    assert cards["goose"]["installed"] is False


def test_default_runtime_is_flagged(isolated_home: Path, monkeypatch) -> None:
    config = isolated_home / ".ouroboros" / "config.yaml"
    config.parent.mkdir()
    config.write_text("orchestrator:\n  runtime_backend: codex\n")
    cards = _cards(monkeypatch, {"codex": "/x/codex"})
    assert cards["codex"]["is_default"] is True
    assert cards["claude"]["is_default"] is False


def test_sign_in_parsing(monkeypatch) -> None:
    def fake(argv):
        if argv[1:] == ["auth", "status"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"loggedIn": False}), "")
        return subprocess.CompletedProcess(argv, 1, "Not logged in", "")

    monkeypatch.setattr(clients, "_run_quiet", fake)
    claude = clients._sign_in("claude", "/x/claude")
    codex = clients._sign_in("codex", "/x/codex")
    assert claude is not None and claude.state == "signed_out" and claude.fix == "claude auth login"
    assert codex is not None and codex.state == "signed_out" and codex.fix == "codex login"
    assert clients._sign_in("goose", "/x/goose") is None
    assert clients._sign_in("claude", None) is None
