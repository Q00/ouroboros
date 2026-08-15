"""Tests for sync-plugin-version script."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

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


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.39.2.dev28", "0.39.2"),
        ("1.2.3a1", "1.2.3a1"),
        ("1.2.3b4", "1.2.3b4"),
        ("1.2.3rc2", "1.2.3rc2"),
        ("1.2.3b4.dev1", "1.2.3b4"),
        ("1.2.3alpha1", "1.2.3a1"),
        ("1.2.3beta02", "1.2.3b2"),
        ("01.02.003", "1.2.3"),
    ],
)
def test_plugin_metadata_version_normalizes_supported_versions(
    version: str,
    expected: str,
) -> None:
    assert sync_plugin_version.normalize_version(version) == expected


@pytest.mark.parametrize(
    "version",
    ["not-a-version", "1.2", "1.2.3garbage", "١.٢.٣", "1.2.3\n"],
)
def test_plugin_metadata_version_rejects_unsupported_versions(version: str) -> None:
    with pytest.raises(ValueError, match="unsupported version"):
        sync_plugin_version.normalize_version(version)


@pytest.mark.parametrize("version", ["01.2.3", "1.2.3alpha1", "1.2.3b01", "1.2.3.dev1"])
def test_release_version_rejects_noncanonical_aliases(version: str) -> None:
    with pytest.raises(ValueError, match="release version must be canonical"):
        sync_plugin_version.require_canonical_version(version)


@pytest.mark.parametrize("version", ["1.2.3", "1.2.3a1", "1.2.3b2", "1.2.3rc3"])
def test_release_version_accepts_canonical_versions(version: str) -> None:
    assert sync_plugin_version.require_canonical_version(version) == version


def test_main_write_updates_both_setup_skill_markers(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin_json = tmp_path / ".codex-plugin" / "plugin.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    codex_plugin_json.parent.mkdir(parents=True, exist_ok=True)

    source_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.4"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.4"}]}\n')
    codex_plugin_json.write_text('{"version": "1.2.4"}\n')

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin_json)
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
    assert "OK    .codex-plugin/plugin.json (1.2.4)" in captured.out
    assert source_skill.read_text() == "<!-- ooo:VERSION:1.2.4 -->\nsource\n"
    assert bundled_skill.read_text() == "<!-- ooo:VERSION:1.2.4 -->\nbundled\n"


def test_write_syncs_codex_plugin_manifest_version(monkeypatch, tmp_path: Path) -> None:
    """The Codex marketplace manifest shares the release-version source of truth."""
    claude_plugin = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin = tmp_path / ".codex-plugin" / "plugin.json"
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    for path, payload in (
        (claude_plugin, {"version": "0.50.4"}),
        (marketplace, {"plugins": [{"version": "0.50.4"}]}),
        (codex_plugin, {"version": "0.50.4"}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:0.50.4 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.50.4 -->\nbundled\n")

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", claude_plugin)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "0.50.5"],
    )

    sync_plugin_version.main()

    assert json.loads(codex_plugin.read_text())["version"] == "0.50.5"


def _seed_sync_fixture(tmp_path: Path, monkeypatch, version: str = "0.50.4") -> None:
    """Seed the required sync targets at *version* under a relocated ROOT."""
    claude_plugin = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace = tmp_path / ".claude-plugin" / "marketplace.json"
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    for path, payload in (
        (claude_plugin, {"version": version}),
        (marketplace, {"plugins": [{"version": version}]}),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))
    for path, body in ((source_skill, "source"), (bundled_skill, "bundled")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"<!-- ooo:VERSION:{version} -->\n{body}\n")
    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", claude_plugin)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)


def _mcp_descriptor(from_spec: str) -> dict:
    return {
        "mcpServers": {
            "ouroboros": {
                "command": "uvx",
                "args": [
                    "--isolated",
                    "--python",
                    ">=3.12",
                    "--from",
                    from_spec,
                    "ouroboros",
                    "mcp",
                    "serve",
                ],
            }
        }
    }


def test_write_pins_both_plugin_mcp_descriptors(monkeypatch, tmp_path: Path, capsys) -> None:
    """Both shipped plugin MCP descriptors — Claude and Codex — pin the
    served package to the plugin version (#2066), so a plugin update
    changes the uvx cache key on every plugin surface."""
    _seed_sync_fixture(tmp_path, monkeypatch)
    mcp_json = tmp_path / ".claude-plugin" / ".mcp.json"
    codex_mcp_json = tmp_path / ".mcp.codex.json"
    mcp_json.write_text(json.dumps(_mcp_descriptor("ouroboros-ai[mcp]")))
    codex_mcp_json.write_text(json.dumps(_mcp_descriptor("ouroboros-ai[mcp]==0.50.4")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "0.50.5"],
    )

    sync_plugin_version.main()

    for descriptor in (mcp_json, codex_mcp_json):
        args = json.loads(descriptor.read_text())["mcpServers"]["ouroboros"]["args"]
        assert args[args.index("--from") + 1] == "ouroboros-ai[mcp]==0.50.5"
    captured = capsys.readouterr()
    assert "WRITE .claude-plugin/.mcp.json (unpinned -> 0.50.5)" in captured.out
    assert "WRITE .mcp.codex.json (0.50.4 -> 0.50.5)" in captured.out


def test_mcp_pin_already_current_reports_ok(monkeypatch, tmp_path: Path, capsys) -> None:
    _seed_sync_fixture(tmp_path, monkeypatch, version="0.50.5")
    mcp_json = tmp_path / ".claude-plugin" / ".mcp.json"
    original = json.dumps(_mcp_descriptor("ouroboros-ai[mcp]==0.50.5"))
    mcp_json.write_text(original)
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "0.50.5"],
    )

    sync_plugin_version.main()

    captured = capsys.readouterr()
    assert "OK    .claude-plugin/.mcp.json (0.50.5)" in captured.out
    assert mcp_json.read_text() == original


def test_mcp_pin_drift_fails_without_write(monkeypatch, tmp_path: Path, capsys) -> None:
    _seed_sync_fixture(tmp_path, monkeypatch, version="0.50.5")
    mcp_json = tmp_path / ".claude-plugin" / ".mcp.json"
    mcp_json.write_text(json.dumps(_mcp_descriptor("ouroboros-ai[mcp]==0.50.4")))
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--version", "0.50.5"],
    )

    with pytest.raises(SystemExit):
        sync_plugin_version.main()

    captured = capsys.readouterr()
    assert "DRIFT .claude-plugin/.mcp.json (0.50.4 != 0.50.5)" in captured.out


def test_write_fails_before_mutation_on_ambiguous_mcp_requirement(
    monkeypatch, tmp_path: Path
) -> None:
    """An ambiguous descriptor fails preflight before any target mutates."""
    _seed_sync_fixture(tmp_path, monkeypatch)
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    descriptor = _mcp_descriptor("ouroboros-ai[mcp]")
    descriptor["mcpServers"]["ouroboros"]["args"] += ["--from", "ouroboros-ai[mcp]"]
    mcp_json = tmp_path / ".claude-plugin" / ".mcp.json"
    mcp_json.write_text(json.dumps(descriptor))
    plugin_before = plugin_json.read_text()
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "0.50.5"],
    )

    with pytest.raises(SystemExit, match="could not validate"):
        sync_plugin_version.main()

    assert plugin_json.read_text() == plugin_before
    assert json.loads(mcp_json.read_text()) == descriptor


def test_write_fails_before_mutation_on_unrelated_second_from(monkeypatch, tmp_path: Path) -> None:
    """Every additional --from makes the shipped launcher ambiguous."""
    _seed_sync_fixture(tmp_path, monkeypatch)
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    descriptor = _mcp_descriptor("ouroboros-ai[mcp]")
    descriptor["mcpServers"]["ouroboros"]["args"] = [
        "--from",
        "other-package",
        *descriptor["mcpServers"]["ouroboros"]["args"],
    ]
    mcp_json = tmp_path / ".claude-plugin" / ".mcp.json"
    mcp_json.write_text(json.dumps(descriptor))
    plugin_before = plugin_json.read_text()
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "0.50.5"],
    )

    with pytest.raises(SystemExit, match="could not validate"):
        sync_plugin_version.main()

    assert plugin_json.read_text() == plugin_before
    assert json.loads(mcp_json.read_text()) == descriptor


def test_write_fails_before_mutation_on_equals_form_second_from(
    monkeypatch, tmp_path: Path
) -> None:
    _seed_sync_fixture(tmp_path, monkeypatch)
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    descriptor = _mcp_descriptor("ouroboros-ai[mcp]")
    descriptor["mcpServers"]["ouroboros"]["args"].append("--from=other-package")
    mcp_json = tmp_path / ".claude-plugin" / ".mcp.json"
    mcp_json.write_text(json.dumps(descriptor))
    plugin_before = plugin_json.read_text()
    monkeypatch.setattr(
        sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "0.50.5"],
    )

    with pytest.raises(SystemExit, match="could not validate"):
        sync_plugin_version.main()

    assert plugin_json.read_text() == plugin_before
    assert json.loads(mcp_json.read_text()) == descriptor


def test_update_version_marker_preserves_unrelated_bytes_with_bom_and_crlf(
    tmp_path: Path,
    monkeypatch,
) -> None:
    marker = b"<!-- ooo:VERSION:1.2.3 -->"
    target = tmp_path / "SKILL.md"
    original = b"\xef\xbb\xbfheading\r\n" + marker + "\r\nbody café\r\n".encode()
    target.write_bytes(original)
    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)

    assert sync_plugin_version.update_version_marker(target, "1.2.4") is not None

    assert target.read_bytes() == original.replace(marker, b"<!-- ooo:VERSION:1.2.4 -->")


def test_main_write_fails_when_required_setup_skill_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin_json = tmp_path / ".codex-plugin" / "plugin.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    codex_plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nsource\n")
    plugin_json.write_text('{"version": "1.2.4"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.4"}]}\n')
    codex_plugin_json.write_text('{"version": "1.2.4"}\n')

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin_json)
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
    codex_plugin_json = tmp_path / ".codex-plugin" / "plugin.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    codex_plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("source without marker\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.4"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.4"}]}\n')
    codex_plugin_json.write_text('{"version": "1.2.4"}\n')
    original_plugin_json = plugin_json.read_text()
    original_marketplace_json = marketplace_json.read_text()
    original_codex_plugin_json = codex_plugin_json.read_text()

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin_json)
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
        assert codex_plugin_json.read_text() == original_codex_plugin_json
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
    codex_plugin_json = tmp_path / ".codex-plugin" / "plugin.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    codex_plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [}\n')
    codex_plugin_json.write_text('{"version": "1.2.3"}\n')
    original_plugin_json = plugin_json.read_text()
    original_codex_plugin_json = codex_plugin_json.read_text()

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin_json)
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
        assert codex_plugin_json.read_text() == original_codex_plugin_json
    else:
        raise AssertionError("invalid marketplace JSON must fail before mutation")


def test_update_json_rejects_duplicate_object_keys(tmp_path: Path) -> None:
    target = tmp_path / "plugin.json"
    original = b'{"version": "1.2.3", "version": "9.9.9"}\n'
    target.write_bytes(original)

    with pytest.raises(ValueError, match="duplicate JSON object key: version"):
        sync_plugin_version.update_json(target, "1.2.4")

    assert target.read_bytes() == original


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_load_json_rejects_non_rfc_constants(tmp_path: Path, constant: str) -> None:
    path = tmp_path / "plugin.json"
    path.write_text(f'{{"version":"1.2.3","invalid":{constant}}}\n')

    with pytest.raises(ValueError, match="non-RFC JSON constant"):
        sync_plugin_version._load_json(path)


def test_main_write_preflight_rejects_duplicate_json_keys_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_bytes(b"<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_bytes(b"<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_bytes(b'{"version":"1.2.3"}\n')
    marketplace_json.write_bytes(b'{"plugins":[{"version":"1.2.3","version":"9.9.9"}]}\n')
    originals = {path: path.read_bytes() for path in (source_skill, bundled_skill, plugin_json)}
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

    with pytest.raises(SystemExit, match="duplicate JSON object key: version"):
        sync_plugin_version.main()

    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("missing_target", ["plugin", "marketplace"])
def test_main_write_fails_before_mutation_when_required_json_is_missing(
    tmp_path: Path,
    monkeypatch,
    missing_target: str,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin_json = tmp_path / ".codex-plugin" / "plugin.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    codex_plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:0.39.1 -->\nbundled\n")
    if missing_target != "plugin":
        plugin_json.write_text('{"version": "1.2.3"}\n')
    if missing_target != "marketplace":
        marketplace_json.write_text('{"plugins": [{"version": "1.2.3"}]}\n')
    codex_plugin_json.write_text('{"version": "1.2.3"}\n')

    original_source = source_skill.read_text()
    original_bundled = bundled_skill.read_text()
    existing_json = marketplace_json if missing_target == "plugin" else plugin_json
    original_json = existing_json.read_text()
    original_codex_plugin_json = codex_plugin_json.read_text()

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "1.2.4"],
    )

    with pytest.raises(SystemExit, match="required plugin metadata not found"):
        sync_plugin_version.main()

    assert source_skill.read_text() == original_source
    assert bundled_skill.read_text() == original_bundled
    assert existing_json.read_text() == original_json
    assert codex_plugin_json.read_text() == original_codex_plugin_json


@pytest.mark.parametrize(
    ("plugin_version", "marketplace_version"),
    [
        (None, "1.2.3"),
        (123, "1.2.3"),
        ("1.2.3", None),
        ("1.2.3", 123),
    ],
)
def test_main_write_rejects_missing_or_non_string_json_version_before_mutation(
    tmp_path: Path,
    monkeypatch,
    plugin_version: object,
    marketplace_version: object,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin_json = tmp_path / ".codex-plugin" / "plugin.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    codex_plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")

    plugin_payload = {} if plugin_version is None else {"version": plugin_version}
    marketplace_plugin = {} if marketplace_version is None else {"version": marketplace_version}
    plugin_json.write_text(__import__("json").dumps(plugin_payload) + "\n")
    marketplace_json.write_text(__import__("json").dumps({"plugins": [marketplace_plugin]}) + "\n")
    codex_plugin_json.write_text('{"version": "1.2.3"}\n')
    original_plugin = plugin_json.read_text()
    original_marketplace = marketplace_json.read_text()
    original_codex_plugin = codex_plugin_json.read_text()
    original_source = source_skill.read_text()
    original_bundled = bundled_skill.read_text()

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "1.2.4"],
    )

    with pytest.raises(SystemExit, match="version must be a string"):
        sync_plugin_version.main()

    assert plugin_json.read_text() == original_plugin
    assert marketplace_json.read_text() == original_marketplace
    assert codex_plugin_json.read_text() == original_codex_plugin
    assert source_skill.read_text() == original_source
    assert bundled_skill.read_text() == original_bundled


def test_main_write_rejects_unsupported_source_version_before_mutation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"
    codex_plugin_json = tmp_path / ".codex-plugin" / "plugin.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    codex_plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.3"}]}\n')
    codex_plugin_json.write_text('{"version": "1.2.3"}\n')
    originals = {
        path: path.read_text()
        for path in (source_skill, bundled_skill, plugin_json, marketplace_json, codex_plugin_json)
    }

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "CODEX_PLUGIN_JSON", codex_plugin_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--write", "--version", "1.2.3garbage"],
    )

    with pytest.raises(SystemExit, match="unsupported version"):
        sync_plugin_version.main()

    for path, original in originals.items():
        assert path.read_text() == original


@pytest.mark.parametrize("marker_version", ["stale.version", "abc", "1..2", "1.2.3.4"])
def test_main_write_rejects_malformed_single_marker_value_before_mutation(
    tmp_path: Path,
    monkeypatch,
    marker_version: str,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text(f"<!-- ooo:VERSION:{marker_version} -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.3"}]}\n')
    originals = {
        path: path.read_bytes()
        for path in (source_skill, bundled_skill, plugin_json, marketplace_json)
    }

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

    with pytest.raises(SystemExit, match="expected exactly one version marker"):
        sync_plugin_version.main()

    for path, original in originals.items():
        assert path.read_bytes() == original


@pytest.mark.parametrize(
    "source_text",
    [
        "<!-- ooo:VERSION:stale-version -->\n<!-- ooo:VERSION:1.2.3 -->\nsource\n",
        "<!-- ooo:VERSION:stale\nversion -->\n<!-- ooo:VERSION:1.2.3 -->\nsource\n",
        "<!-- ooo:VERSION:1.2.3 -->\n<!-- ooo:VERSION:stale\nversion-->\nsource\n",
        "<!-- ooo:VERSION:1.2.3 -->\n<!-- ooo:VERSION:stale\nversion\nsource\n",
    ],
)
def test_main_write_rejects_malformed_duplicate_marker_before_mutation(
    tmp_path: Path,
    monkeypatch,
    source_text: str,
) -> None:
    source_skill = tmp_path / "skills" / "setup" / "SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin" / "skills" / "setup" / "SKILL.md"
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    marketplace_json = tmp_path / ".claude-plugin" / "marketplace.json"

    source_skill.parent.mkdir(parents=True, exist_ok=True)
    bundled_skill.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text(source_text)
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.3"}]}\n')
    originals = {
        path: path.read_text()
        for path in (source_skill, bundled_skill, plugin_json, marketplace_json)
    }

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

    with pytest.raises(SystemExit, match="expected exactly one version marker"):
        sync_plugin_version.main()

    for path, original in originals.items():
        assert path.read_text() == original


def test_atomic_write_keeps_target_intact_when_temp_fsync_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version": "1.2.3"}\n'
    target.write_bytes(original)

    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(sync_plugin_version.os, "fsync", fail_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        sync_plugin_version._atomic_write_bytes(target, b'{"version": "1.2.4"}\n')

    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_applies_mode_to_open_temp_before_file_fsync(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    target.write_bytes(b"old")
    target.chmod(0o640)
    calls: list[tuple[str, int]] = []
    real_fchmod = sync_plugin_version.os.fchmod
    real_fsync = sync_plugin_version.os.fsync

    def record_fchmod(fd: int, mode: int) -> None:
        calls.append(("fchmod", mode))
        real_fchmod(fd, mode)

    def record_fsync(fd: int) -> None:
        calls.append(("fsync", fd))
        real_fsync(fd)

    monkeypatch.setattr(sync_plugin_version.os, "fchmod", record_fchmod)
    monkeypatch.setattr(sync_plugin_version.os, "fsync", record_fsync)

    sync_plugin_version._atomic_write_bytes(target, b"new")

    assert calls[0] == ("fchmod", 0o640)
    assert calls[1][0] == "fsync"


def test_atomic_write_rejects_target_changed_since_preflight(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    external = b'{"version":"1.2.3","note":"external"}\n'
    target.write_bytes(external)

    with pytest.raises(RuntimeError, match="write conflict"):
        sync_plugin_version._atomic_write_bytes(
            target,
            b'{"version":"1.2.4"}\n',
            expected_current=original,
        )

    assert target.read_bytes() == external


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_exchange_preserves_same_bytes_new_inode_replacement_at_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    content = b'{"version":"1.2.4"}\n'
    target.write_bytes(original)
    expected_generation = sync_plugin_version._path_generation(target)
    real_exchange = sync_plugin_version._exchange_paths
    injected = False
    replacement_generation = None

    def inject_same_bytes_replacement(source: Path, destination: Path) -> bool:
        nonlocal injected, replacement_generation
        if not injected:
            injected = True
            replacement = tmp_path / "same-bytes-replacement"
            replacement.write_bytes(original)
            os.replace(replacement, destination)
            replacement_generation = sync_plugin_version._path_generation(destination)
        return real_exchange(source, destination)

    monkeypatch.setattr(sync_plugin_version, "_exchange_paths", inject_same_bytes_replacement)

    with pytest.raises(RuntimeError, match="write conflict") as raised:
        sync_plugin_version._atomic_write_bytes(
            target,
            content,
            expected_current=original,
            expected_generation=expected_generation,
        )

    assert "preserved exchanged content at" in str(raised.value)
    assert replacement_generation is not None
    assert target.read_bytes() == original
    assert sync_plugin_version._path_generation(target) == replacement_generation
    reachable_bytes = [path.read_bytes() for path in tmp_path.iterdir()]
    assert content in reachable_bytes


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_exchange_restores_edit_arriving_at_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    external = b'{"version":"1.2.3","note":"external"}\n'
    target.write_bytes(original)
    real_exchange = sync_plugin_version._exchange_paths
    injected = False

    def inject_before_exchange(source: Path, destination: Path) -> bool:
        nonlocal injected
        if not injected:
            injected = True
            destination.write_bytes(external)
        return real_exchange(source, destination)

    monkeypatch.setattr(sync_plugin_version, "_exchange_paths", inject_before_exchange)

    with pytest.raises(RuntimeError, match="write conflict"):
        sync_plugin_version._atomic_write_bytes(
            target,
            b'{"version":"1.2.4"}\n',
            expected_current=original,
        )

    assert target.read_bytes() == external


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_exchange_preserves_newer_edit_arriving_during_restore(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    first_external = b'{"version":"1.2.3","note":"first"}\n'
    newer_external = b'{"version":"1.2.3","note":"newer"}\n'
    target.write_bytes(original)
    real_exchange = sync_plugin_version._exchange_paths
    exchange_count = 0

    def inject_edits(source: Path, destination: Path) -> bool:
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            destination.write_bytes(first_external)
        elif exchange_count == 2:
            destination.write_bytes(newer_external)
        return real_exchange(source, destination)

    monkeypatch.setattr(sync_plugin_version, "_exchange_paths", inject_edits)

    with pytest.raises(RuntimeError, match="write conflict"):
        sync_plugin_version._atomic_write_bytes(
            target,
            b'{"version":"1.2.4"}\n',
            expected_current=original,
        )

    preserved = [path.read_bytes() for path in tmp_path.iterdir()]
    assert first_external in preserved
    assert newer_external in preserved


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_exchange_preserves_timestamp_restored_same_bytes_edit_during_restore(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    content = b'{"version":"1.2.4"}\n'
    first_external = b'{"version":"1.2.5"}\n'
    newer_external = content
    target.write_bytes(original)
    real_exchange = sync_plugin_version._exchange_paths
    exchange_count = 0

    def inject_edits(source: Path, destination: Path) -> bool:
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            destination.write_bytes(first_external)
        elif exchange_count == 2:
            before = destination.stat()
            destination.write_bytes(newer_external)
            os.utime(
                destination,
                ns=(before.st_atime_ns, before.st_mtime_ns),
                follow_symlinks=False,
            )
        return real_exchange(source, destination)

    monkeypatch.setattr(sync_plugin_version, "_exchange_paths", inject_edits)

    with pytest.raises(RuntimeError, match="write conflict") as raised:
        sync_plugin_version._atomic_write_bytes(
            target,
            content,
            expected_current=original,
        )

    assert "preserved exchanged content at" in str(raised.value)
    preserved = [path.read_bytes() for path in tmp_path.iterdir()]
    assert first_external in preserved
    assert newer_external in preserved


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_exchange_preserves_same_bytes_edit_arriving_during_restore(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    content = b'{"version":"1.2.4"}\n'
    first_external = b'{"version":"1.2.3","note":"first"}\n'
    target.write_bytes(original)
    real_exchange = sync_plugin_version._exchange_paths
    exchange_count = 0

    def inject_edits(source: Path, destination: Path) -> bool:
        nonlocal exchange_count
        exchange_count += 1
        if exchange_count == 1:
            destination.write_bytes(first_external)
        elif exchange_count == 2:
            destination.write_bytes(content)
        return real_exchange(source, destination)

    monkeypatch.setattr(sync_plugin_version, "_exchange_paths", inject_edits)

    with pytest.raises(RuntimeError, match="write conflict"):
        sync_plugin_version._atomic_write_bytes(
            target,
            content,
            expected_current=original,
        )

    preserved = [path.read_bytes() for path in tmp_path.iterdir()]
    assert first_external in preserved
    assert content in preserved


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_exchange_preserves_open_descriptor_write_displaced_to_temp(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    content = b'{"version":"1.2.4"}\n'
    first_external = b'{"version":"1.2.3","note":"first"}\n'
    late_external = b'{"version":"1.2.4","note":"late fd"}\n'
    target.write_bytes(original)
    real_exchange = sync_plugin_version._exchange_paths
    exchange_count = 0
    displaced_fd: int | None = None

    def inject_open_descriptor_writer(source: Path, destination: Path) -> bool:
        nonlocal exchange_count, displaced_fd
        exchange_count += 1
        if exchange_count == 1:
            destination.write_bytes(first_external)
        result = real_exchange(source, destination)
        if exchange_count == 1:
            displaced_fd = os.open(destination, os.O_WRONLY)
        elif exchange_count == 2:
            assert displaced_fd is not None
            os.write(displaced_fd, late_external)
            os.ftruncate(displaced_fd, len(late_external))
            os.fsync(displaced_fd)
            os.close(displaced_fd)
            displaced_fd = None
        return result

    monkeypatch.setattr(sync_plugin_version, "_exchange_paths", inject_open_descriptor_writer)

    with pytest.raises(RuntimeError, match="preserved exchanged content"):
        sync_plugin_version._atomic_write_bytes(
            target,
            content,
            expected_current=original,
        )

    preserved = [path for path in tmp_path.iterdir() if path != target]
    reachable_bytes = [target.read_bytes(), *(path.read_bytes() for path in preserved)]
    assert preserved
    assert late_external in reachable_bytes


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_exchange_quarantines_late_open_descriptor_write_on_success(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    content = b'{"version":"1.2.4"}\n'
    late_external = b'{"version":"1.2.3","note":"late fd during cleanup"}\n'
    target.write_bytes(original)
    expected_generation = sync_plugin_version._path_generation(target)
    displaced_fd = os.open(target, os.O_WRONLY)
    real_quarantine = sync_plugin_version._quarantine_displaced_path
    quarantined_path: Path | None = None

    def write_before_quarantine(temp_path: Path, path: Path) -> Path:
        nonlocal quarantined_path
        os.lseek(displaced_fd, 0, os.SEEK_SET)
        os.write(displaced_fd, late_external)
        os.ftruncate(displaced_fd, len(late_external))
        os.fsync(displaced_fd)
        quarantined_path = real_quarantine(temp_path, path)
        return quarantined_path

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(
        sync_plugin_version,
        "_quarantine_displaced_path",
        write_before_quarantine,
    )

    try:
        owned_generation = sync_plugin_version._atomic_write_bytes(
            target,
            content,
            expected_current=original,
            expected_generation=expected_generation,
        )
    finally:
        os.close(displaced_fd)

    assert target.read_bytes() == content
    assert sync_plugin_version._path_generation(target) == owned_generation
    assert quarantined_path is not None
    assert quarantined_path.read_bytes() == late_external


def test_atomic_write_fails_closed_when_exchange_is_unavailable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    target.write_bytes(original)

    monkeypatch.setattr(sync_plugin_version, "_exchange_paths", lambda *_args: False)

    with pytest.raises(RuntimeError, match="atomic path exchange is unavailable"):
        sync_plugin_version._atomic_write_bytes(
            target,
            b'{"version":"1.2.4"}\n',
            expected_current=original,
        )

    assert target.read_bytes() == original
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_atomic_write_quarantines_displaced_inode_before_directory_sync_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    target = tmp_path / "target.json"
    original = b'{"version":"1.2.3"}\n'
    content = b'{"version":"1.2.4"}\n'
    target.write_bytes(original)
    real_open = sync_plugin_version.os.open
    real_quarantine = sync_plugin_version._quarantine_displaced_path
    quarantined_path: Path | None = None

    def fail_directory_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            raise OSError("injected directory open failure")
        return real_open(path, flags, *args, **kwargs)

    def capture_quarantine(temp_path: Path, path: Path) -> Path:
        nonlocal quarantined_path
        quarantined_path = real_quarantine(temp_path, path)
        return quarantined_path

    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version.os, "open", fail_directory_open)
    monkeypatch.setattr(sync_plugin_version, "_quarantine_displaced_path", capture_quarantine)

    with pytest.raises(sync_plugin_version._OwnedWriteError) as raised:
        sync_plugin_version._atomic_write_bytes(
            target,
            content,
            expected_current=original,
        )

    assert "directory open failure" in str(raised.value)
    assert isinstance(raised.value.__cause__, OSError)
    assert "directory open failure" in str(raised.value.__cause__)
    assert target.read_bytes() == content
    assert quarantined_path is not None
    assert quarantined_path.read_bytes() == original


def test_main_write_rolls_back_when_later_write_fails(
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
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.3"}]}\n')
    originals = {
        path: path.read_bytes()
        for path in (source_skill, bundled_skill, plugin_json, marketplace_json)
    }

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

    original_update_json = sync_plugin_version.update_json
    calls = 0

    def fail_second_json_write(
        path: Path,
        version: str,
        *,
        nested_key=None,
        expected_current=None,
        expected_generation=None,
    ) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected later write failure")
        return original_update_json(
            path,
            version,
            nested_key=nested_key,
            expected_current=expected_current,
            expected_generation=expected_generation,
        )

    monkeypatch.setattr(sync_plugin_version, "update_json", fail_second_json_write)

    with pytest.raises(OSError, match="injected later write failure"):
        sync_plugin_version.main()

    for path, original in originals.items():
        assert path.read_bytes() == original


def test_main_write_does_not_clobber_external_edit_during_rollback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.3"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
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
    original_update_json = sync_plugin_version.update_json
    calls = 0
    external = b'{"version":"external"}\n'

    def fail_after_external_edit(
        path: Path,
        version: str,
        *,
        nested_key=None,
        expected_current=None,
        expected_generation=None,
    ) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            plugin_json.write_bytes(external)
            raise OSError("injected later write failure")
        return original_update_json(
            path,
            version,
            nested_key=nested_key,
            expected_current=expected_current,
            expected_generation=expected_generation,
        )

    monkeypatch.setattr(sync_plugin_version, "update_json", fail_after_external_edit)

    with pytest.raises(BaseExceptionGroup, match="rollback was incomplete") as raised:
        sync_plugin_version.main()

    assert plugin_json.read_bytes() == external
    assert any("rollback conflict" in str(exc) for exc in raised.value.exceptions)


def test_main_write_does_not_roll_back_same_bytes_new_inode_aba(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.3"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
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
    original_update_json = sync_plugin_version.update_json
    calls = 0

    def replace_first_write_with_same_bytes_new_inode_before_bookkeeping_then_fail(
        path: Path,
        version: str,
        *,
        nested_key=None,
        expected_current=None,
        expected_generation=None,
    ):
        nonlocal calls
        calls += 1
        result = original_update_json(
            path,
            version,
            nested_key=nested_key,
            expected_current=expected_current,
            expected_generation=expected_generation,
        )
        if calls == 1:
            same_bytes = plugin_json.read_bytes()
            replacement = tmp_path / "same-bytes-replacement"
            replacement.write_bytes(same_bytes)
            os.replace(replacement, plugin_json)
        if calls == 2:
            raise OSError("injected later write failure")
        return result

    monkeypatch.setattr(
        sync_plugin_version,
        "update_json",
        replace_first_write_with_same_bytes_new_inode_before_bookkeeping_then_fail,
    )

    with pytest.raises(BaseExceptionGroup, match="rollback was incomplete") as raised:
        sync_plugin_version.main()

    assert __import__("json").loads(plugin_json.read_text())["version"] == "1.2.4"
    assert any("file generation changed" in str(exc) for exc in raised.value.exceptions)


def test_main_write_rolls_back_owned_exchange_when_parent_dir_open_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.3"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
    originals = {
        path: path.read_bytes()
        for path in (source_skill, bundled_skill, plugin_json, marketplace_json)
    }
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
    real_open = sync_plugin_version.os.open
    directory_opens = 0
    failure_injected = False
    foreign_generation = None

    def fail_second_plugin_metadata_directory_open(path, flags, *args, **kwargs):
        nonlocal directory_opens, failure_injected, foreign_generation
        if Path(path) == plugin_json.parent:
            directory_opens += 1
            if directory_opens == 2:
                replacement = tmp_path / "source-skill-same-bytes-foreign-generation"
                replacement.write_bytes(originals[source_skill])
                os.replace(replacement, source_skill)
                foreign_generation = sync_plugin_version._path_generation(source_skill)
                failure_injected = True
                raise OSError("injected parent directory open failure")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(sync_plugin_version.os, "open", fail_second_plugin_metadata_directory_open)

    with pytest.raises(sync_plugin_version._OwnedWriteError, match="parent directory open failure"):
        sync_plugin_version.main()

    assert failure_injected
    assert foreign_generation is not None
    assert source_skill.read_bytes() == originals[source_skill]
    assert sync_plugin_version._path_generation(source_skill) == foreign_generation
    assert bundled_skill.read_bytes() == originals[bundled_skill]
    assert plugin_json.read_bytes() == originals[plugin_json]
    assert marketplace_json.read_bytes() == originals[marketplace_json]


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") or sys.platform == "darwin"),
    reason="atomic pathname exchange is unavailable on this platform",
)
def test_main_write_rolls_back_owned_exchange_when_displaced_quarantine_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.3"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
    originals = {
        path: path.read_bytes()
        for path in (source_skill, bundled_skill, plugin_json, marketplace_json)
    }
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
    real_quarantine = sync_plugin_version._quarantine_displaced_path
    quarantine_failure_injected = False
    foreign_generation = None

    def fail_marketplace_displaced_quarantine(temp_path: Path, path: Path) -> Path:
        nonlocal quarantine_failure_injected, foreign_generation
        if (
            not quarantine_failure_injected
            and path == marketplace_json
            and temp_path.read_bytes() == originals[marketplace_json]
        ):
            replacement = tmp_path / "source-skill-same-bytes-foreign-generation"
            replacement.write_bytes(originals[source_skill])
            os.replace(replacement, source_skill)
            foreign_generation = sync_plugin_version._path_generation(source_skill)
            quarantine_failure_injected = True
            raise OSError("injected displaced quarantine failure")
        return real_quarantine(temp_path, path)

    monkeypatch.setattr(
        sync_plugin_version,
        "_quarantine_displaced_path",
        fail_marketplace_displaced_quarantine,
    )

    with pytest.raises(sync_plugin_version._OwnedWriteError, match="displaced quarantine failure"):
        sync_plugin_version.main()

    assert quarantine_failure_injected
    assert foreign_generation is not None
    assert source_skill.read_bytes() == originals[source_skill]
    assert sync_plugin_version._path_generation(source_skill) == foreign_generation
    assert bundled_skill.read_bytes() == originals[bundled_skill]
    assert plugin_json.read_bytes() == originals[plugin_json]
    assert marketplace_json.read_bytes() == originals[marketplace_json]


def test_main_write_rejects_external_edit_after_preflight(tmp_path: Path, monkeypatch) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.3"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
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
    original_update_json = sync_plugin_version.update_json
    external = b'{"version":{"external":"must-survive"}}\n'
    edited = False

    def edit_after_preflight(
        path: Path,
        version: str,
        *,
        nested_key=None,
        expected_current=None,
        expected_generation=None,
    ) -> bool:
        nonlocal edited
        if not edited:
            edited = True
            plugin_json.write_bytes(external)
        return original_update_json(
            path,
            version,
            nested_key=nested_key,
            expected_current=expected_current,
            expected_generation=expected_generation,
        )

    monkeypatch.setattr(sync_plugin_version, "update_json", edit_after_preflight)

    with pytest.raises(BaseExceptionGroup, match="rollback was incomplete") as raised:
        sync_plugin_version.main()

    assert plugin_json.read_bytes() == external
    assert any("write conflict" in str(exc) for exc in raised.value.exceptions)


def test_main_write_revalidates_targets_that_were_already_current(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.4 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.4 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.4"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
    original_marketplace = marketplace_json.read_bytes()
    external_plugin = b'{"version":"external"}\n'
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
    original_update_json = sync_plugin_version.update_json

    def mutate_current_target_after_write(
        path: Path,
        version: str,
        *,
        nested_key=None,
        expected_current=None,
        expected_generation=None,
    ) -> bool:
        updated = original_update_json(
            path,
            version,
            nested_key=nested_key,
            expected_current=expected_current,
            expected_generation=expected_generation,
        )
        plugin_json.write_bytes(external_plugin)
        return updated

    monkeypatch.setattr(
        sync_plugin_version,
        "update_json",
        mutate_current_target_after_write,
    )

    with pytest.raises(RuntimeError, match="post-write conflict"):
        sync_plugin_version.main()

    assert plugin_json.read_bytes() == external_plugin
    assert marketplace_json.read_bytes() == original_marketplace


def test_main_read_only_revalidates_targets_after_preflight(tmp_path: Path, monkeypatch) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.4 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.4 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.4"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.4"}]}\n')
    external_plugin = b'{"version":"external"}\n'
    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)
    monkeypatch.setattr(sync_plugin_version, "PLUGIN_JSON", plugin_json)
    monkeypatch.setattr(sync_plugin_version, "MARKETPLACE_JSON", marketplace_json)
    monkeypatch.setattr(sync_plugin_version, "SETUP_SKILL_MD", source_skill)
    monkeypatch.setattr(sync_plugin_version, "BUNDLED_SETUP_SKILL_MD", bundled_skill)
    monkeypatch.setattr(
        sync_plugin_version.sys,
        "argv",
        ["sync-plugin-version.py", "--require-canonical", "--version", "1.2.4"],
    )
    original_print = print
    mutated = False

    def mutate_after_preflight(*args, **kwargs) -> None:
        nonlocal mutated
        original_print(*args, **kwargs)
        if not mutated and args and str(args[0]).startswith("  OK"):
            mutated = True
            plugin_json.write_bytes(external_plugin)

    monkeypatch.setattr(sync_plugin_version, "print", mutate_after_preflight, raising=False)

    with pytest.raises(RuntimeError, match="validation conflict"):
        sync_plugin_version.main()

    assert plugin_json.read_bytes() == external_plugin


def test_main_write_rolls_back_only_successfully_replaced_targets(
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
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.3"}]}\n')

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

    original_atomic_write = sync_plugin_version._atomic_write_bytes
    rollback_attempts: list[Path] = []

    def fail_primary_and_two_rollbacks(
        path: Path,
        content: bytes,
        *,
        expected_current: bytes | None = None,
        expected_generation=None,
    ):
        if b"1.2.4" in content and path == marketplace_json:
            raise OSError("primary marketplace failure")
        if b"1.2.3" in content:
            rollback_attempts.append(path)
            if path == plugin_json:
                raise OSError("plugin rollback failure")
            if path == source_skill:
                raise OSError("source rollback failure")
        return original_atomic_write(
            path,
            content,
            expected_current=expected_current,
            expected_generation=expected_generation,
        )

    monkeypatch.setattr(
        sync_plugin_version,
        "_atomic_write_bytes",
        fail_primary_and_two_rollbacks,
    )

    with pytest.raises(BaseExceptionGroup) as raised:
        sync_plugin_version.main()

    assert [str(exc) for exc in raised.value.exceptions] == [
        "primary marketplace failure",
        f"failed to roll back {plugin_json}: plugin rollback failure",
    ]
    assert rollback_attempts == [plugin_json]


def test_main_write_ignores_untrusted_legacy_transaction_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.3"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
    forged = tmp_path / ".sync-plugin-version.transaction.json"
    forged.write_text('{"originals":{".claude-plugin/plugin.json":"Zm9yZ2Vk"}}\n')
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

    assert forged.read_text() == '{"originals":{".claude-plugin/plugin.json":"Zm9yZ2Vk"}}\n'
    assert __import__("json").loads(plugin_json.read_text())["version"] == "1.2.4"
    assert __import__("json").loads(marketplace_json.read_text())["plugins"][0]["version"] == (
        "1.2.4"
    )


def test_concurrent_invocations_are_serialized_by_os_lock(tmp_path: Path) -> None:
    source_skill = tmp_path / "skills/setup/SKILL.md"
    bundled_skill = tmp_path / ".claude-plugin/skills/setup/SKILL.md"
    plugin_json = tmp_path / ".claude-plugin/plugin.json"
    marketplace_json = tmp_path / ".claude-plugin/marketplace.json"
    source_skill.parent.mkdir(parents=True)
    bundled_skill.parent.mkdir(parents=True)
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version":"1.2.3"}\n')
    marketplace_json.write_text('{"plugins":[{"version":"1.2.3"}]}\n')
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    runner = """
import importlib.util
from pathlib import Path
import sys
import time
script, root, version, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], sys.argv[4]
spec = importlib.util.spec_from_file_location("sync_plugin_version_process", script)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROOT = root
module.PLUGIN_JSON = root / ".claude-plugin/plugin.json"
module.MARKETPLACE_JSON = root / ".claude-plugin/marketplace.json"
module.SETUP_SKILL_MD = root / "skills/setup/SKILL.md"
module.BUNDLED_SETUP_SKILL_MD = root / ".claude-plugin/skills/setup/SKILL.md"
if mode == "hold":
    original = module.update_json
    def hold_first_write(
        path,
        value,
        *,
        nested_key=None,
        expected_current=None,
        expected_generation=None,
    ):
        if not (root / "ready").exists():
            (root / "ready").touch()
            while not (root / "release").exists():
                time.sleep(0.01)
        return original(
            path,
            value,
            nested_key=nested_key,
            expected_current=expected_current,
            expected_generation=expected_generation,
        )
    module.update_json = hold_first_write
module.sys.argv = ["sync-plugin-version.py", "--write", "--version", version]
module.main()
"""
    first = subprocess.Popen(
        [sys.executable, "-c", runner, str(_SCRIPT_PATH), str(tmp_path), "1.2.4", "hold"]
    )
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    second = subprocess.Popen(
        [sys.executable, "-c", runner, str(_SCRIPT_PATH), str(tmp_path), "1.2.5", "normal"]
    )
    time.sleep(0.25)
    try:
        assert second.poll() is None
    finally:
        release.touch()
        first.wait(timeout=5)
        second.wait(timeout=5)

    assert first.returncode == second.returncode == 0
    assert __import__("json").loads(plugin_json.read_text())["version"] == "1.2.5"
    assert __import__("json").loads(marketplace_json.read_text())["plugins"][0]["version"] == (
        "1.2.5"
    )
    assert b"VERSION:1.2.5" in source_skill.read_bytes()
    assert b"VERSION:1.2.5" in bundled_skill.read_bytes()


def test_lock_file_is_outside_checkout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sync_plugin_version, "ROOT", tmp_path)

    lock_path = sync_plugin_version._lock_path()

    assert lock_path.parent != tmp_path


def test_release_workflow_checks_tag_metadata_before_build() -> None:
    workflow = (_SCRIPT_PATH.parents[1] / ".github/workflows/release.yml").read_text()

    validation = workflow.index("python scripts/sync-plugin-version.py")
    build = workflow.index("uv build")
    package_identity = workflow.index("PACKAGE_VERSION=", build)
    tui_job = workflow[workflow.index("  build-tui:") : workflow.index("  attach-tui-binaries:")]

    assert "${REF_NAME#v}" in workflow
    assert '[[ "$VERSION" == *.dev* ]]' in workflow
    assert "--require-canonical" in workflow
    assert "zipfile.ZipFile" in workflow
    assert '[[ "$PACKAGE_VERSION" != "$VERSION" ]]' in workflow
    assert validation < build
    assert build < package_identity
    assert workflow.count("zipfile.ZipFile") == 2
    assert "needs: release" in tui_job


def test_main_write_rolls_back_when_marker_update_reports_no_change(
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
    source_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nsource\n")
    bundled_skill.write_text("<!-- ooo:VERSION:1.2.3 -->\nbundled\n")
    plugin_json.write_text('{"version": "1.2.3"}\n')
    marketplace_json.write_text('{"plugins": [{"version": "1.2.3"}]}\n')
    originals = {
        path: path.read_bytes()
        for path in (source_skill, bundled_skill, plugin_json, marketplace_json)
    }

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
    monkeypatch.setattr(
        sync_plugin_version,
        "update_version_marker",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(SystemExit, match="failed to update"):
        sync_plugin_version.main()

    for path, original in originals.items():
        assert path.read_bytes() == original
