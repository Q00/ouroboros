"""Installation diagnostics for the official Codex marketplace layout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ouroboros.cli.commands import doctor
from ouroboros.cli.main import app


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_plugin(codex_home: Path, version: str, *, with_launcher: bool = True) -> Path:
    plugin = codex_home / "plugins" / "cache" / "ouroboros" / "ouroboros" / version
    _write_json(
        plugin / ".codex-plugin" / "plugin.json",
        {"name": "ouroboros", "version": version, "mcpServers": "./.mcp.codex.json"},
    )
    # The shared bundle's Claude launcher must not stand in for the Codex one.
    _write_json(
        plugin / ".mcp.json",
        {"mcpServers": {"ouroboros": {"command": "claude-decoy", "args": []}}},
    )
    if with_launcher:
        _write_json(
            plugin / ".mcp.codex.json",
            {
                "mcpServers": {
                    "ouroboros": {
                        "command": "ouroboros",
                        "args": ["mcp", "serve", "--runtime", "codex"],
                    }
                }
            },
        )
    return plugin


@pytest.mark.parametrize("custom_home", [False, True])
def test_marketplace_install_reports_manifest_and_declared_launcher(
    monkeypatch, tmp_path: Path, custom_home: bool
) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    codex_home = tmp_path / ("custom-codex" if custom_home else ".codex")
    if custom_home:
        monkeypatch.setenv("CODEX_HOME", str(codex_home))
    else:
        monkeypatch.delenv("CODEX_HOME", raising=False)
    plugin = _write_plugin(codex_home, "1.2.3")

    result = CliRunner().invoke(app, ["doctor", "install", "--json"])

    assert result.exit_code == 0
    surfaces = {s["name"]: s for s in json.loads(result.output)}
    manifest = surfaces["codex_personal_plugin"]
    launcher = surfaces["codex_personal_plugin_launcher"]
    assert manifest["status"] == "pass"
    assert manifest["version"] == "1.2.3"
    assert manifest["path"] == str(plugin / ".codex-plugin" / "plugin.json")
    assert launcher["status"] == "pass"
    assert launcher["path"] == str(plugin / ".mcp.codex.json")
    assert launcher["launcher"] == "ouroboros mcp serve --runtime codex"


def test_marketplace_cache_selects_version_order_and_reports_drift(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.11.0")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_plugin(tmp_path / ".codex", "1.9.0")
    newest = _write_plugin(tmp_path / ".codex", "1.10.0")

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin"].status == "warn"
    assert surfaces["codex_personal_plugin"].version == "1.10.0"
    launcher = surfaces["codex_personal_plugin_launcher"]
    assert launcher.status == "pass"
    assert launcher.path == str(newest / ".mcp.codex.json")


def test_marketplace_missing_launcher_does_not_fall_back_to_other_launchers(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_plugin(tmp_path / ".codex", "1.2.2")
    newest = _write_plugin(tmp_path / ".codex", "1.2.3", with_launcher=False)
    _write_json(
        tmp_path / ".codex/plugins/cache/personal/ouroboros/1.2.3/.mcp.json",
        {"mcpServers": {"ouroboros": {"command": "legacy-decoy", "args": []}}},
    )

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin"].status == "pass"
    launcher = surfaces["codex_personal_plugin_launcher"]
    assert launcher.status == "warn"
    assert launcher.path == str(newest / ".mcp.codex.json")
    assert launcher.launcher is None


def test_marketplace_malformed_manifest_is_reported_as_failure(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plugin = _write_plugin(tmp_path / ".codex", "1.2.3")
    (plugin / ".codex-plugin/plugin.json").write_text("{", encoding="utf-8")

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin"].status == "fail"
    assert surfaces["codex_personal_plugin_launcher"].status == "fail"


@pytest.mark.parametrize("reference", [None, 42, "", "../outside.json"])
def test_marketplace_invalid_launcher_reference_fails(
    monkeypatch, tmp_path: Path, reference: object
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plugin = _write_plugin(tmp_path / ".codex", "1.2.3")
    _write_json(
        plugin / ".codex-plugin/plugin.json",
        {"name": "ouroboros", "version": "1.2.3", "mcpServers": reference},
    )

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin_launcher"].status == "fail"
    assert surfaces["codex_personal_plugin_launcher"].launcher is None


def test_marketplace_absolute_launcher_reference_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plugin = _write_plugin(tmp_path / ".codex", "1.2.3")
    _write_json(
        plugin / ".codex-plugin/plugin.json",
        {"name": "ouroboros", "version": "1.2.3", "mcpServers": str(plugin / ".mcp.codex.json")},
    )

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin_launcher"].status == "fail"


def test_marketplace_manifest_without_reference_uses_default_launcher(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plugin = _write_plugin(tmp_path / ".codex", "1.2.3", with_launcher=False)
    _write_json(plugin / ".codex-plugin/plugin.json", {"name": "ouroboros", "version": "1.2.3"})

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    launcher = surfaces["codex_personal_plugin_launcher"]
    assert launcher.status == "pass"
    assert launcher.path == str(plugin / ".mcp.json")
    assert launcher.launcher == "claude-decoy"


def test_marketplace_invalid_declared_launcher_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    plugin = _write_plugin(tmp_path / ".codex", "1.2.3")
    _write_json(plugin / ".mcp.codex.json", {"mcpServers": {"ouroboros": {"command": ""}}})

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin_launcher"].status == "fail"


def test_marketplace_unreadable_cache_does_not_report_legacy_success(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_plugin(tmp_path / ".codex", "1.2.3")
    cache_root = tmp_path / ".codex/plugins/cache/ouroboros/ouroboros"
    original_iterdir = Path.iterdir

    def fail_iterdir(path: Path):
        if path == cache_root:
            raise PermissionError("permission denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin"].status == "fail"
    assert surfaces["codex_personal_plugin_launcher"].status == "fail"


def test_marketplace_diagnostics_do_not_write_files(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_plugin(tmp_path / ".codex", "1.2.3")

    def fail_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("installation diagnostics must not write files")

    monkeypatch.setattr(Path, "write_text", fail_write)

    surfaces = {s.name: s for s in doctor.collect_install_surfaces(home=tmp_path)}

    assert surfaces["codex_personal_plugin"].status == "pass"
    assert surfaces["codex_personal_plugin_launcher"].status == "pass"
