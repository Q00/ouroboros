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
        "ouroboros.config_tui.launcher.launch_settings", lambda **_kwargs: calls.append("launched")
    )
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert calls == ["launched"]


def test_subcommand_does_not_launch_settings_gui(monkeypatch, tmp_path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        "ouroboros.config_tui.launcher.launch_settings", lambda **_kwargs: calls.append("launched")
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


def test_bare_invocation_forwards_web_flags(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def _fake_launch(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("ouroboros.config_tui.launcher.launch_settings", _fake_launch)
    result = runner.invoke(app, ["--web", "--host", "0.0.0.0", "--port", "8765", "--no-browser"])
    assert result.exit_code == 0
    assert captured == {
        "force_web": True,
        "host": "0.0.0.0",
        "port": 8765,
        "open_browser": False,
    }


def _show_env(monkeypatch, tmp_path, config: dict) -> None:
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    for name in (
        "OUROBOROS_AGENT_RUNTIME",
        "OUROBOROS_RUNTIME",
        "OUROBOROS_LLM_BACKEND",
        "OUROBOROS_CLARIFICATION_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_show_effective_view_renders_stages_and_inheritance(monkeypatch, tmp_path) -> None:
    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {
                "runtime_backend": "opencode",
                "runtime_profile": {"stages": {"execute": "codex"}},
            },
            "clarification": {"default_model": "my-model"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"opencode": "/bin/opencode", "codex": "/bin/codex"},
    )
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    out = result.output
    assert "Per-stage overrides" in out
    assert "(inherit)" in out and "opencode" in out  # inheriting stages resolved
    assert "codex" in out  # explicit execute override
    assert "my-model" in out  # configured stage model
    assert "interview" in out and "reflect" in out


def test_show_effective_view_marks_env_override(monkeypatch, tmp_path) -> None:
    _show_env(monkeypatch, tmp_path, {"orchestrator": {"runtime_backend": "opencode"}})
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"hermes": "/bin/hermes", "opencode": "/bin/opencode"},
    )
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "hermes")
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "hermes" in result.output  # env wins over config
    assert "OUROBOROS_AGENT_RUNTIME" in result.output  # and the source says so


def test_show_effective_view_marks_uninstalled_agent(monkeypatch, tmp_path) -> None:
    _show_env(monkeypatch, tmp_path, {"orchestrator": {"runtime_backend": "kiro"}})
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"kiro": None},
    )
    result = runner.invoke(app, ["show"])
    assert result.exit_code == 0
    assert "not installed" in result.output


def test_show_section_still_returns_raw_contents(monkeypatch, tmp_path) -> None:
    _show_env(monkeypatch, tmp_path, {"logging": {"level": "debug"}})
    result = runner.invoke(app, ["show", "logging"])
    assert result.exit_code == 0
    assert "debug" in result.output


def test_show_json_emits_machine_readable_effective_view(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {
                "runtime_backend": "opencode",
                "runtime_profile": {"stages": {"execute": "codex"}},
            }
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"opencode": "/bin/opencode", "codex": "/bin/codex"},
    )
    result = runner.invoke(app, ["show", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["defaults"]["default_agent"]["value"] == "opencode"
    assert payload["stages"]["execute"] == {
        "agent": "codex",
        "inherited": False,
        "agent_installed": True,
        "model": None,
        "model_source": "not configurable",
        "model_key": None,
    }
    assert payload["stages"]["interview"]["inherited"] is True
    assert payload["stages"]["interview"]["agent"] == "opencode"


def test_show_json_uses_runtime_env_as_llm_backend_fallback(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "claude"},
            "llm": {"backend": "claude_code"},
        },
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME", "codex")
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex"},
    )

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["defaults"]["llm_backend"] == {
        "value": "codex",
        "source": "env OUROBOROS_RUNTIME ⚠",
    }


def test_show_text_uses_runtime_env_as_llm_backend_fallback(monkeypatch, tmp_path) -> None:
    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "claude"},
            "llm": {"backend": "claude_code"},
        },
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME", "codex")
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex"},
    )

    result = runner.invoke(app, ["show"])

    assert result.exit_code == 0
    assert "LLM backend" in result.output
    assert "codex" in result.output
    assert "OUROBOROS_RUNTIME" in result.output


def test_undo_swaps_in_backup_and_supports_redo(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    from ouroboros.config import loader as config_loader

    monkeypatch.setattr(config_loader, "get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"orchestrator": {"runtime_backend": "hermes"}})
    )
    (tmp_path / "config.yaml.bak").write_text(
        yaml.dump({"orchestrator": {"runtime_backend": "codex"}})
    )

    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 0
    assert (
        yaml.safe_load((tmp_path / "config.yaml").read_text())["orchestrator"]["runtime_backend"]
        == "codex"
    )

    # undo again = redo
    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 0
    assert (
        yaml.safe_load((tmp_path / "config.yaml").read_text())["orchestrator"]["runtime_backend"]
        == "hermes"
    )


def test_undo_without_backup_errors(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(yaml.dump({}))
    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 1


def test_undo_invalid_backup_aborts_safely(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    from ouroboros.config import loader as config_loader

    monkeypatch.setattr(config_loader, "get_config_dir", lambda: tmp_path)
    good = yaml.dump({"orchestrator": {"runtime_backend": "hermes"}})
    (tmp_path / "config.yaml").write_text(good)
    (tmp_path / "config.yaml.bak").write_text(
        yaml.dump({"orchestrator": {"runtime_backend": "not-a-backend"}})
    )
    result = runner.invoke(app, ["undo"])
    assert result.exit_code == 1
    assert (tmp_path / "config.yaml").read_text() == good  # untouched
