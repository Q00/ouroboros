"""Installation inventory must take precedence over leftover cache files."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.cli import codex_plugin
from ouroboros.cli.commands import doctor


def _snapshot(codex_home: Path, version: str) -> Path:
    root = codex_home / "plugins/cache/ouroboros/ouroboros" / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "ouroboros", "version": version, "mcpServers": "./.mcp.codex.json"}),
        encoding="utf-8",
    )
    (root / ".mcp.codex.json").write_text(
        json.dumps({"mcpServers": {"ouroboros": {"command": f"codex-{version}", "args": []}}}),
        encoding="utf-8",
    )
    return root


@pytest.fixture
def inventory(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(doctor, "__version__", "1.2.3")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))

    def set_result(status="installed", *, version="1.2.3", enabled=True):
        if status == "installed":
            result = codex_plugin._parse_inventory(
                json.dumps(
                    {
                        "installed": [
                            {
                                "pluginId": "ouroboros@ouroboros",
                                "installed": True,
                                "version": version,
                                "enabled": enabled,
                            }
                        ]
                    }
                )
            )
        else:
            result = codex_plugin.CodexPluginVersion(status, reason="Codex plugin query timed out")
        monkeypatch.setattr(codex_plugin, "inspect_codex_plugin", lambda *_args, **_kwargs: result)

    return set_result


def _checks(home: Path):
    return {s.name: s for s in doctor.collect_install_surfaces(home=home)}


def test_installed_version_wins_over_newer_cached_version(inventory, tmp_path: Path) -> None:
    inventory(version="1.2.2")
    installed = _snapshot(tmp_path / ".codex", "1.2.2")
    _snapshot(tmp_path / ".codex", "1.2.3")

    checks = _checks(tmp_path)

    assert checks["codex_personal_plugin"].version == "1.2.2"
    assert checks["codex_personal_plugin_launcher"].path == str(installed / ".mcp.codex.json")


def test_missing_installed_snapshot_does_not_use_latest_cache(inventory, tmp_path: Path) -> None:
    inventory(version="1.2.2")
    _snapshot(tmp_path / ".codex", "1.2.3")

    checks = _checks(tmp_path)

    assert checks["codex_personal_plugin"].status == "warn"
    assert checks["codex_personal_plugin_launcher"].status == "warn"
    assert checks["codex_personal_plugin_launcher"].launcher is None


@pytest.mark.parametrize("status", ["not_installed", "unknown"])
def test_unconfirmed_cache_files_do_not_report_installation_success(
    inventory, tmp_path: Path, status: str
) -> None:
    inventory(status)
    _snapshot(tmp_path / ".codex", "1.2.3")

    checks = _checks(tmp_path)

    for name in ("codex_personal_plugin", "codex_personal_plugin_launcher"):
        assert checks[name].status == "warn"
        assert checks[name].remediation
    assert checks["codex_personal_plugin_launcher"].launcher == "codex-1.2.3"


def test_disabled_installed_plugin_remains_a_warning(inventory, tmp_path: Path) -> None:
    inventory(enabled=False)
    _snapshot(tmp_path / ".codex", "1.2.3")

    checks = _checks(tmp_path)

    assert checks["codex_personal_plugin"].status == "warn"
    assert "disabled" in checks["codex_personal_plugin"].message
    assert checks["codex_personal_plugin_launcher"].status == "warn"


def test_installed_snapshot_manifest_cannot_claim_another_version(
    inventory, tmp_path: Path
) -> None:
    inventory()
    root = _snapshot(tmp_path / ".codex", "1.2.3")
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps(
            {
                "version": "1.2.2",
                "mcpServers": "./.mcp.codex.json",
            }
        ),
        encoding="utf-8",
    )

    checks = _checks(tmp_path)

    assert "Codex reports" in checks["codex_personal_plugin"].message
    assert checks["codex_personal_plugin"].status == "warn"


def test_equivalent_release_version_finds_the_matching_snapshot(inventory, tmp_path: Path) -> None:
    inventory(version="v1.2.3")
    root = _snapshot(tmp_path / ".codex", "1.2.3")
    _snapshot(tmp_path / ".codex", "1.2.4")
    checks = _checks(tmp_path)
    assert checks["codex_personal_plugin"].status == "pass"
    assert checks["codex_personal_plugin_launcher"].path == str(root / ".mcp.codex.json")


def test_unknown_inventory_does_not_hide_a_broken_launcher(inventory, tmp_path: Path) -> None:
    inventory("unknown")
    root = _snapshot(tmp_path / ".codex", "1.2.3")
    (root / ".mcp.codex.json").write_text("{", encoding="utf-8")
    checks = _checks(tmp_path)
    assert checks["codex_personal_plugin_launcher"].status == "fail"
    assert "unconfirmed" in checks["codex_personal_plugin_launcher"].message


def test_doctor_queries_selected_executable_and_effective_codex_home(
    monkeypatch, tmp_path: Path
) -> None:
    from subprocess import CompletedProcess
    from unittest.mock import patch

    codex_home = tmp_path / "separate-codex"
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setattr(doctor, "detect_backend_cli", lambda _: "/selected/codex")
    output = json.dumps({"installed": []}).encode()
    with patch(
        "ouroboros.cli.codex_plugin.subprocess.run",
        return_value=CompletedProcess([], 0, output, b""),
    ) as run:
        _checks(tmp_path)
    assert run.call_args.args[0][0] == "/selected/codex"
    assert run.call_args.kwargs["env"]["CODEX_HOME"] == str(codex_home)


def test_query_resolution_failure_reports_unknown_without_echoing_details(
    monkeypatch, tmp_path: Path
) -> None:
    def fail_resolution(_):
        raise ValueError("private configuration value")

    monkeypatch.setattr(doctor, "detect_backend_cli", fail_resolution)
    _snapshot(tmp_path / ".codex", "1.2.3")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
    checks = _checks(tmp_path)
    assert checks["codex_personal_plugin"].status == "warn"
    assert "unconfirmed" in checks["codex_personal_plugin"].message
    assert "private" not in str(checks)


def test_ambiguous_equivalent_caches_do_not_pick_one(inventory, tmp_path: Path) -> None:
    inventory(version="1.2.3")
    _snapshot(tmp_path / ".codex", "v1.2.3")
    _snapshot(tmp_path / ".codex", "1.2.3.0")
    checks = _checks(tmp_path)
    assert checks["codex_personal_plugin"].status == "fail"
    assert checks["codex_personal_plugin_launcher"].status == "fail"
