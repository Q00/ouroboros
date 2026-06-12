"""Regression tests for the `ouroboros config` bare-invocation dispatch (#1414).

The bare invocation must launch the settings GUI while every existing
subcommand keeps its scriptable behavior unchanged.
"""

from __future__ import annotations

from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.config import app

runner = CliRunner()


def test_bare_invocation_launches_settings_gui(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "ouroboros.config_tui.launcher.launch_settings", lambda: calls.append("launched")
    )
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert calls == ["launched"]


def test_subcommand_does_not_launch_settings_gui(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "ouroboros.config_tui.launcher.launch_settings", lambda: calls.append("launched")
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"orchestrator": {"runtime_backend": "claude"}})
    )
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert calls == []


def test_config_show_unchanged(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.dump({"orchestrator": {"runtime_backend": "codex"}}))
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "codex" in result.output


def test_config_set_unknown_key_still_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.dump({}))
    result = runner.invoke(app, ["set", "orchestrator.not_a_key_xyz", "v"])
    assert result.exit_code == 1


def test_config_help_lists_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for subcommand in ("show", "set", "backend", "init", "validate"):
        assert subcommand in result.output
