"""Tests for sync-plugin-version script."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

_SCRIPT_PATH = Path(__file__).parent.parent.parent.parent / "scripts" / "sync-plugin-version.py"
_spec = importlib.util.spec_from_file_location("sync_plugin_version", str(_SCRIPT_PATH))
sync_plugin_version = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sync_plugin_version)


def test_git_describe_fallback_uses_hatch_vcs_next_dev_version() -> None:
    assert sync_plugin_version.version_from_git_describe("v0.39.1-28-gc05024d6") == ("0.39.2.dev28")


def test_git_describe_fallback_preserves_exact_tag_version() -> None:
    assert sync_plugin_version.version_from_git_describe("v0.39.1") == "0.39.1"


def test_plugin_metadata_version_normalizes_dev_suffix_to_public_version() -> None:
    assert sync_plugin_version.normalize_version("0.39.2.dev28") == "0.39.2"


def test_write_syncs_codex_plugin_manifest_version(monkeypatch, tmp_path: Path) -> None:
    """The Codex marketplace manifest shares the release-version source of truth."""
    claude_plugin = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin = tmp_path / ".codex-plugin" / "plugin.json"
    for path, payload in (
        (claude_plugin, {"version": "0.50.4"}),
        (marketplace, {"plugins": [{"version": "0.50.4"}]}),
        (codex_plugin, {"version": "0.50.4"}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", claude_plugin)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", tmp_path / "missing-skill.md")
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "0.50.5"],
    )

    sync_plugin_version.main()

    assert json.loads(codex_plugin.read_text())["version"] == "0.50.5"
