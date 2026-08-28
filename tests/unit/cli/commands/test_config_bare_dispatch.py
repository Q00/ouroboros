"""Regression tests for the `ouroboros config` bare-invocation dispatch (#1414).

The bare invocation must launch the settings GUI while every existing
subcommand keeps its scriptable behavior unchanged.
"""

from __future__ import annotations

from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.config import app
from ouroboros.config._model_defaults import DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL

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


def test_config_show_text_resolves_explicit_stage_agents(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: tmp_path)
    (tmp_path / "config.yaml").write_text(
        yaml.dump(
            {
                "orchestrator": {
                    "runtime_backend": "opencode",
                    "runtime_profile": {
                        "default": "opencode",
                        "stages": {
                            "interview": "codex",
                            "execute": "claude",
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"opencode": "/bin/opencode", "codex": "/bin/codex", "claude": "/bin/claude"},
    )

    result = runner.invoke(app, ["show"])

    assert result.exit_code == 0, result.output
    assert "interview" in result.output
    assert "codex" in result.output
    assert "execute" in result.output
    assert "claude" in result.output


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
        "OUROBOROS_EXECUTION_MODEL",
        "OUROBOROS_SEMANTIC_MODEL",
        "OUROBOROS_REFLECT_MODEL",
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
    assert "backend" in out and "default" in out  # runtime-normalized stage model
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
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)
    result = runner.invoke(app, ["show", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["defaults"]["default_agent"]["value"] == "opencode"
    assert payload["stages"]["execute"] == {
        "agent": "codex",
        "inherited": False,
        "agent_installed": True,
        "model": "Codex current selected model (concrete model not reported by Codex)",
        "model_source": "automatic Codex selection",
        "model_key": "execution.default_model",
    }
    assert payload["stages"]["interview"]["inherited"] is True
    assert payload["stages"]["interview"]["agent"] == "opencode"


def test_show_json_normalizes_runtime_env_backend(monkeypatch, tmp_path) -> None:
    import json

    _show_env(monkeypatch, tmp_path, {"orchestrator": {"runtime_backend": "claude"}})
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "CODEX")
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex"},
    )
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["defaults"]["default_agent"]["value"] == "codex"
    assert payload["stages"]["interview"]["agent"] == "codex"
    assert payload["stages"]["interview"]["agent_installed"] is True


def test_show_json_renders_configured_claude_code_llm_backend(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "codex"},
            "llm": {"backend": "claude_code"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex", "claude_code": "/bin/claude"},
    )
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["defaults"]["default_agent"]["value"] == "codex"
    assert payload["defaults"]["llm_backend"]["value"] == "claude_code"
    assert payload["defaults"]["llm_backend"]["source"] == "config"
    assert payload["stages"]["interview"]["agent"] == "codex"


def test_show_json_preserves_env_stage_model_pin_for_codex(monkeypatch, tmp_path) -> None:
    import json

    _show_env(monkeypatch, tmp_path, {"orchestrator": {"runtime_backend": "codex"}})
    monkeypatch.setenv("OUROBOROS_CLARIFICATION_MODEL", "claude-opus-4-8")
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex"},
    )
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["stages"]["interview"]["model"] == "claude-opus-4-8"
    assert payload["stages"]["interview"]["model_source"] == "env OUROBOROS_CLARIFICATION_MODEL ⚠"


def test_show_json_uses_stage_llm_backend_for_inherited_internal_models(
    monkeypatch, tmp_path
) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "codex"},
            "llm": {"backend": "litellm"},
            "clarification": {"default_model": "claude-opus-4-8"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex", "litellm": "/bin/litellm"},
    )
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stages"]["interview"]["agent"] == "codex"
    assert payload["stages"]["interview"]["model"] == DEFAULT_OPUS_MODEL
    assert payload["stages"]["interview"]["model_source"] == "config → backend default"


def test_show_json_stage_override_beats_llm_backend_env(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {
                "runtime_backend": "codex",
                "runtime_profile": {"stages": {"interview": "opencode"}},
            },
            "llm": {"backend": "codex"},
        },
    )
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "claude_code")
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex", "opencode": "/bin/opencode", "claude": "/bin/claude"},
    )

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["defaults"]["llm_backend"] == {
        "value": "claude_code",
        "source": "env OUROBOROS_LLM_BACKEND ⚠",
    }
    assert payload["stages"]["interview"]["agent"] == "opencode"
    assert payload["stages"]["interview"]["model"] == "default"
    assert payload["stages"]["interview"]["model_source"] == "default → backend default"


def test_show_json_uses_completion_backend_for_runtime_only_stage_agent(
    monkeypatch, tmp_path
) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {
                "runtime_backend": "claude",
                "runtime_profile": {"stages": {"interview": "antigravity"}},
            },
            "llm": {"backend": "claude_code"},
            "clarification": {"default_model": "claude-opus-4-8"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"claude": "/bin/claude", "antigravity": "/bin/agy"},
    )

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stages"]["interview"]["agent"] == "antigravity"
    assert payload["stages"]["interview"]["model"] == DEFAULT_OPUS_MODEL
    assert payload["stages"]["interview"]["model_source"] == "config → backend default"


def test_show_json_uses_llm_fallback_for_inherited_runtime_only_agent(
    monkeypatch, tmp_path
) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "antigravity"},
            "llm": {"backend": "claude_code"},
            "clarification": {"default_model": "claude-opus-4-8"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"claude": "/bin/claude", "antigravity": "/bin/agy"},
    )

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["stages"]["interview"]["agent"] == "antigravity"
    assert payload["stages"]["interview"]["model"] == DEFAULT_OPUS_MODEL
    assert payload["stages"]["interview"]["model_source"] == "config → backend default"


def test_show_json_normalizes_execute_current_sentinel_through_loader(
    monkeypatch, tmp_path
) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {
                "runtime_backend": "claude",
                "runtime_profile": {"stages": {"execute": "claude"}},
            },
            "execution": {"default_model": "current"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"claude": "/bin/claude"},
    )

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    execute = json.loads(result.output)["stages"]["execute"]
    assert execute["model"] == DEFAULT_SONNET_MODEL
    assert execute["model_source"] == "config → backend default"


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


def test_show_json_normalizes_shipped_stage_defaults_for_codex(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "codex"},
            "clarification": {"default_model": "claude-opus-4-8"},
            "evaluation": {"semantic_model": "claude-opus-4-8"},
            "resilience": {"reflect_model": "claude-opus-4-8"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex"},
    )
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    for stage in ("interview", "evaluate", "reflect"):
        assert payload["stages"][stage]["agent"] == "codex"
        assert (
            payload["stages"][stage]["model"]
            == "Codex current selected model (concrete model not reported by Codex)"
        )
        assert payload["stages"][stage]["model_source"] == "automatic Codex selection"


def test_show_json_treats_serialized_claude_llm_backend_as_config(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "codex"},
            "llm": {"backend": "claude_code"},
            "clarification": {"default_model": "claude-opus-4-8"},
        },
    )
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex", "claude_code": "/bin/claude"},
    )
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["defaults"]["llm_backend"] == {"value": "claude_code", "source": "config"}
    assert payload["stages"]["interview"]["agent"] == "codex"
    assert payload["stages"]["interview"]["model"] == (
        "Codex current selected model (concrete model not reported by Codex)"
    )
    assert payload["stages"]["interview"]["model_source"] == "automatic Codex selection"


def test_show_json_honors_explicit_claude_llm_env_under_codex_agent(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {"runtime_backend": "codex"},
            "llm": {"backend": "codex"},
            "clarification": {"default_model": "claude-opus-4-8"},
        },
    )
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "claude_code")
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex", "claude": "/bin/claude"},
    )

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["defaults"]["llm_backend"] == {
        "value": "claude_code",
        "source": "env OUROBOROS_LLM_BACKEND ⚠",
    }
    assert payload["stages"]["interview"]["agent"] == "codex"
    assert payload["stages"]["interview"]["model"] == DEFAULT_OPUS_MODEL
    assert payload["stages"]["interview"]["model_source"] == "config → backend default"


def test_show_json_cli_path_follows_effective_runtime_env(monkeypatch, tmp_path) -> None:
    import json

    _show_env(
        monkeypatch,
        tmp_path,
        {
            "orchestrator": {
                "runtime_backend": "claude",
                "cli_path": "/bin/claude",
                "codex_cli_path": "/bin/codex-config",
            }
        },
    )
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_CODEX_CLI_PATH", "/bin/codex-env")
    monkeypatch.setattr(
        "ouroboros.backends.model_catalog.installed_backends",
        lambda: {"codex": "/bin/codex-env"},
    )
    monkeypatch.setattr("ouroboros.backends.model_catalog.configured_default_model", lambda _: None)

    result = runner.invoke(app, ["show", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["defaults"]["default_agent"]["value"] == "codex"
    assert payload["environment"]["cli_path"] == "/bin/codex-env"


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
