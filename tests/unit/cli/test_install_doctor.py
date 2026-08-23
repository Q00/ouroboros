"""Tests for the installation-surface doctor."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from ouroboros.cli.commands import doctor
from ouroboros.cli.main import app

runner = CliRunner()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_current_install(home: Path, *, version: str = "1.2.3") -> None:
    _write_json(
        home / "plugins" / "ouroboros" / ".codex-plugin" / "plugin.json",
        {"name": "ouroboros", "version": version},
    )
    _write_json(
        home / ".codex" / "plugins" / "cache" / "personal" / "ouroboros" / version / ".mcp.json",
        {"mcpServers": {"ouroboros": {"command": "ouroboros", "args": ["mcp", "serve"]}}},
    )
    _write_json(
        home
        / ".claude"
        / "plugins"
        / "marketplaces"
        / "ouroboros"
        / ".claude-plugin"
        / "plugin.json",
        {"name": "ouroboros", "version": version},
    )
    _write_json(
        home / ".claude" / "plugins" / "cache" / "ouroboros" / "ouroboros" / version / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": [
                        "--isolated",
                        "--python",
                        ">=3.12",
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                        "--runtime",
                        "claude-cli",
                        "--llm-backend",
                        "claude_code",
                    ],
                }
            }
        },
    )
    _write_json(home / ".claude" / "mcp.json", {"mcpServers": {"telegram": {"command": "x"}}})
    _write_json(
        home
        / ".zcode"
        / "cli"
        / "plugins"
        / "cache"
        / "zcode-plugins-official"
        / "ouroboros"
        / "0.1.0"
        / ".zcode-plugin"
        / "plugin.json",
        {"name": "ouroboros", "version": "0.1.0"},
    )
    _write_json(
        home
        / ".zcode"
        / "cli"
        / "plugins"
        / "cache"
        / "zcode-plugins-official"
        / "ouroboros"
        / "0.1.0"
        / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "/tmp/ouroboros/.venv/bin/ouroboros",
                    "args": ["mcp", "serve", "--runtime", "zcode", "--llm-backend", "zcode"],
                }
            }
        },
    )


def test_install_doctor_reports_current_surfaces(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")

    surfaces = doctor.collect_install_surfaces(home=tmp_path)

    statuses = {surface.name: surface.status for surface in surfaces}
    assert statuses["python_package"] == "pass"
    assert statuses["codex_personal_plugin"] == "pass"
    assert statuses["claude_plugin_marketplace"] == "pass"
    assert statuses["claude_plugin_launcher"] == "pass"
    assert statuses["claude_user_mcp_duplicate"] == "pass"
    assert statuses["zcode_bridge_plugin"] == "pass"


def test_collect_install_surfaces_is_read_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")

    def fail_write(*args: object, **kwargs: object) -> None:
        raise AssertionError("install doctor must not write files")

    monkeypatch.setattr(Path, "write_text", fail_write)

    surfaces = doctor.collect_install_surfaces(home=tmp_path)

    assert surfaces


def test_install_doctor_flags_stale_codex_and_claude_plugins(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.2")

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    by_name = {surface.name: surface for surface in surfaces}

    assert by_name["codex_personal_plugin"].status == "warn"
    assert "differs from package 1.2.3" in by_name["codex_personal_plugin"].message
    assert by_name["claude_plugin_marketplace"].status == "warn"


def test_install_doctor_warns_for_non_isolated_claude_launcher(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": ["--from", "ouroboros-ai[mcp]", "ouroboros", "mcp", "serve"],
                }
            }
        },
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "warn"
    assert "isolated uvx" in launcher.message


def test_claude_launcher_rejects_unsupported_python_constraint(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    launcher_path = (
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json"
    )
    payload = json.loads(launcher_path.read_text(encoding="utf-8"))
    payload["mcpServers"]["ouroboros"]["args"][2] = "2.7"
    _write_json(launcher_path, payload)

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "warn"
    assert "isolated uvx" in launcher.message


def test_claude_launcher_rejects_flattened_argv_boundary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": [
                        "--isolated --python >=3.12 --from ouroboros-ai[mcp] ouroboros mcp serve"
                    ],
                }
            }
        },
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "warn"
    assert "isolated uvx" in launcher.message


def test_claude_launcher_requires_from_value_adjacency(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": [
                        "--isolated",
                        "--python",
                        ">=3.12",
                        "--from",
                        "different-package",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                    ],
                }
            }
        },
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "warn"
    assert "isolated uvx" in launcher.message


def test_claude_launcher_requires_python_value(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": [
                        "--isolated",
                        "--python",
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                    ],
                }
            }
        },
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "warn"
    assert "isolated uvx" in launcher.message


def test_claude_launcher_rejects_flags_after_command_tail(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": [
                        "--python",
                        ">=3.12",
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                        "--isolated",
                    ],
                }
            }
        },
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "warn"
    assert "isolated uvx" in launcher.message


def test_claude_launcher_rejects_extra_argv_tokens(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": [
                        "--isolated",
                        "--python",
                        ">=3.12",
                        "--from",
                        "ouroboros-ai[mcp]",
                        "ouroboros",
                        "mcp",
                        "serve",
                        "--extra",
                    ],
                }
            }
        },
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "warn"
    assert "isolated uvx" in launcher.message


def test_install_doctor_flags_duplicate_claude_user_mcp(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path / ".claude" / "mcp.json",
        {
            "mcpServers": {
                "ouroboros": {
                    "command": "uvx",
                    "args": ["--from", "ouroboros-ai", "ouroboros", "mcp", "serve"],
                }
            }
        },
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    duplicate = next(surface for surface in surfaces if surface.name == "claude_user_mcp_duplicate")

    assert duplicate.status == "warn"
    assert "user-level Claude MCP" in duplicate.message
    assert duplicate.launcher == "uvx --from ouroboros-ai ouroboros mcp serve"


def test_install_doctor_warns_for_missing_configs(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)

    surfaces = doctor.collect_install_surfaces(home=tmp_path)

    assert any(surface.status == "warn" for surface in surfaces)
    assert (
        next(surface for surface in surfaces if surface.name == "python_package").status == "pass"
    )
    assert (
        next(surface for surface in surfaces if surface.name == "claude_user_mcp_duplicate").status
        == "pass"
    )


def test_install_doctor_fails_on_malformed_present_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    malformed = tmp_path / ".claude" / "mcp.json"
    malformed.parent.mkdir(parents=True, exist_ok=True)
    malformed.write_text("{", encoding="utf-8")

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    claude_user = next(
        surface for surface in surfaces if surface.name == "claude_user_mcp_duplicate"
    )

    assert claude_user.status == "fail"
    assert "invalid JSON" in claude_user.message


def test_install_doctor_fails_on_malformed_present_plugin_manifest(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    manifest = tmp_path / "plugins" / "ouroboros" / ".codex-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("{", encoding="utf-8")

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    codex_plugin = next(surface for surface in surfaces if surface.name == "codex_personal_plugin")

    assert codex_plugin.status == "fail"
    assert "invalid JSON" in codex_plugin.message


def test_zcode_bridge_version_is_not_compared_to_core(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    zcode = next(surface for surface in surfaces if surface.name == "zcode_bridge_plugin")

    assert zcode.status == "pass"
    assert zcode.version == "0.1.0"
    assert zcode.expected_version is None
    assert "bridge plugin version 0.1.0" in zcode.message


def test_zcode_bridge_discovers_newer_version_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".zcode"
        / "cli"
        / "plugins"
        / "cache"
        / "zcode-plugins-official"
        / "ouroboros"
        / "0.2.0"
        / ".zcode-plugin"
        / "plugin.json",
        {"name": "ouroboros", "version": "0.2.0"},
    )
    _write_json(
        tmp_path
        / ".zcode"
        / "cli"
        / "plugins"
        / "cache"
        / "zcode-plugins-official"
        / "ouroboros"
        / "0.2.0"
        / ".mcp.json",
        {"mcpServers": {"ouroboros": {"command": "new-zcode", "args": ["mcp", "serve"]}}},
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    zcode = next(surface for surface in surfaces if surface.name == "zcode_bridge_plugin")
    launcher = next(surface for surface in surfaces if surface.name == "zcode_bridge_launcher")

    assert zcode.status == "pass"
    assert zcode.version == "0.2.0"
    assert "/0.2.0/" in zcode.path
    assert launcher.launcher == "new-zcode mcp serve"


def test_codex_cache_uses_effective_codex_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    normal_home = tmp_path / "home"
    codex_home = tmp_path / "custom-codex"
    _write_current_install(normal_home, version="1.2.3")
    _write_json(
        codex_home / "plugins" / "cache" / "personal" / "ouroboros" / "1.2.3" / ".mcp.json",
        {"mcpServers": {"ouroboros": {"command": "custom-codex", "args": ["mcp", "serve"]}}},
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    surfaces = doctor.collect_install_surfaces(home=normal_home)
    launcher = next(
        surface for surface in surfaces if surface.name == "codex_personal_plugin_launcher"
    )

    assert launcher.status == "pass"
    assert launcher.path == str(
        codex_home / "plugins" / "cache" / "personal" / "ouroboros" / "1.2.3" / ".mcp.json"
    )
    assert launcher.launcher == "custom-codex mcp serve"


def test_cache_selection_uses_version_order(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.10.0")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.9.0")
    _write_json(
        tmp_path
        / ".codex"
        / "plugins"
        / "cache"
        / "personal"
        / "ouroboros"
        / "1.10.0"
        / ".mcp.json",
        {"mcpServers": {"ouroboros": {"command": "newer", "args": ["mcp", "serve"]}}},
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(
        surface for surface in surfaces if surface.name == "codex_personal_plugin_launcher"
    )

    assert launcher.path == str(
        tmp_path
        / ".codex"
        / "plugins"
        / "cache"
        / "personal"
        / "ouroboros"
        / "1.10.0"
        / ".mcp.json"
    )
    assert launcher.launcher == "newer mcp serve"


def test_empty_claude_launcher_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {"mcpServers": {"ouroboros": {"command": "", "args": ["--isolated"]}}},
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "fail"
    assert "command is missing or empty" in launcher.message


def test_malformed_launcher_args_fail(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {"mcpServers": {"ouroboros": {"command": "uvx", "args": "--isolated"}}},
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "fail"
    assert "args must be a list" in launcher.message


def test_wrong_type_launcher_entry_fails(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    _write_json(
        tmp_path
        / ".claude"
        / "plugins"
        / "cache"
        / "ouroboros"
        / "ouroboros"
        / "1.2.3"
        / ".mcp.json",
        {"mcpServers": {"ouroboros": ["uvx", "--isolated"]}},
    )

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(surface for surface in surfaces if surface.name == "claude_plugin_launcher")

    assert launcher.status == "fail"
    assert "launcher entry is not an object" in launcher.message


def test_unreadable_cache_directory_becomes_failed_surface(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")
    cache_root = tmp_path / ".codex" / "plugins" / "cache" / "personal" / "ouroboros"
    original_iterdir = Path.iterdir

    def fail_iterdir(path: Path):
        if path == cache_root:
            raise PermissionError("permission denied")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_iterdir)

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    launcher = next(
        surface for surface in surfaces if surface.name == "codex_personal_plugin_launcher"
    )

    assert launcher.status == "fail"
    assert "permission denied" in launcher.message


def test_editable_zero_version_does_not_mark_plugins_stale(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "0.0.0")
    monkeypatch.delenv("CODEX_HOME", raising=False)
    _write_current_install(tmp_path, version="1.2.3")

    surfaces = doctor.collect_install_surfaces(home=tmp_path)
    by_name = {surface.name: surface for surface in surfaces}

    assert by_name["codex_personal_plugin"].status == "pass"
    assert "stale comparison skipped" in by_name["codex_personal_plugin"].message
    assert by_name["claude_plugin_marketplace"].status == "pass"


def test_cli_install_doctor_json_uses_home(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_current_install(tmp_path, version="1.2.3")

    result = runner.invoke(app, ["doctor", "install", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert any(surface["name"] == "claude_plugin_launcher" for surface in payload)


def test_cli_install_doctor_json_reports_non_utf8_manifest(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_current_install(tmp_path, version="1.2.3")
    manifest = tmp_path / "plugins" / "ouroboros" / ".codex-plugin" / "plugin.json"
    manifest.write_bytes(b"\xff")

    result = runner.invoke(app, ["doctor", "install", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    by_name = {surface["name"]: surface for surface in payload}
    assert by_name["codex_personal_plugin"]["status"] == "fail"
    assert "invalid UTF-8" in by_name["codex_personal_plugin"]["message"]
    assert by_name["claude_plugin_launcher"]["status"] == "pass"


def test_cli_doctor_help_registered() -> None:
    result = runner.invoke(app, ["doctor", "--help"])

    assert result.exit_code == 0
    assert "install" in result.output


def test_cli_install_doctor_human_output_preserves_launcher_brackets(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _write_current_install(tmp_path, version="1.2.3")

    result = runner.invoke(app, ["doctor", "install"])

    assert result.exit_code == 0
    assert "ouroboros-ai[mcp]" in result.output
