"""Tests for sync-plugin-version script."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT_PATH = Path(__file__).parent.parent.parent.parent / "scripts" / "sync-plugin-version.py"
_spec = importlib.util.spec_from_file_location("sync_plugin_version", str(_SCRIPT_PATH))
assert _spec is not None
sync_plugin_version = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(sync_plugin_version)


def test_git_describe_fallback_uses_hatch_vcs_next_dev_version() -> None:
    assert sync_plugin_version.version_from_git_describe("v0.39.1-28-gc05024d6") == ("0.39.2.dev28")


def test_git_describe_fallback_preserves_exact_tag_version() -> None:
    assert sync_plugin_version.version_from_git_describe("v0.39.1") == "0.39.1"


def test_plugin_metadata_version_normalizes_dev_suffix_to_public_version() -> None:
    assert sync_plugin_version.normalize_version("0.39.2.dev28") == "0.39.2"


def test_main_write_updates_both_setup_skill_markers(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)

    source_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.4"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.4"}]}\n')

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "1.2.4"],
    )

    sync_plugin_version.main()

    captured = capsys.readouterr()
    assert "WRITE skills/setup/SKILL.md (0.39.1 -> 1.2.4)" in captured.out
    assert "WRITE .claude-plugin/skills/setup/SKILL.md (0.39.1 -> 1.2.4)" in captured.out
    assert source_skill.read_text() == "<!-- ooo:VERSION:1.2.4 -->\nsource\n"
    assert bundled_skill.read_text() == "<!-- ooo:VERSION:1.2.4 -->\nbundled\n"


def test_main_write_fails_when_required_setup_skill_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nsource\n")
    plugin_json.write_text('{"version": "1.2.4"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.4"}]}\n')

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "1.2.4"],
    )

    try:
        sync_plugin_version.main()
    except SystemExit as exc:
        assert "required setup skill not found" in str(exc)
    else:
        raise AssertionError("missing required setup skill must fail")


def test_main_write_fails_when_setup_marker_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("source without marker\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.4"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.4"}]}\n')
    original_plugin_json = plugin_json.read_text()
    original_marketplace_json = marketplace_json.read_text()

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "1.2.4"],
    )

    try:
        sync_plugin_version.main()
    except SystemExit as exc:
        assert "expected exactly one version marker" in str(exc)
        assert plugin_json.read_text() == original_plugin_json
        assert marketplace_json.read_text() == original_marketplace_json
    else:
        raise AssertionError("missing version marker must fail")


def test_main_write_preflights_json_targets_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [}\n')
    original_plugin_json = plugin_json.read_text()

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "1.2.4"],
    )

    try:
        sync_plugin_version.main()
    except SystemExit as exc:
        assert "could not validate" in str(exc)
        assert plugin_json.read_text() == original_plugin_json
    else:
        raise AssertionError("invalid marketplace JSON must fail before mutation")
