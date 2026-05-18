"""Unit tests for the status command."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner
import yaml

from ouroboros.cli.commands.status import app

runner = CliRunner(env={"COLUMNS": "240"})


API_ENV_KEYS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY",
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_RUNTIME",
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_CLI_PATH",
    "OUROBOROS_CODEX_CLI_PATH",
    "OUROBOROS_COPILOT_CLI_PATH",
    "OUROBOROS_GEMINI_CLI_PATH",
    "OUROBOROS_GOOSE_CLI_PATH",
    "OUROBOROS_HERMES_CLI_PATH",
    "OUROBOROS_KIRO_CLI_PATH",
    "OUROBOROS_OPENCODE_CLI_PATH",
    "CODEX_HOME",
]


def _clear_auth_env(monkeypatch) -> None:
    for key in API_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make_cli(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def _write_config(
    config_dir: Path,
    *,
    cli_path: Path | str | None = None,
    backend: str = "claude",
    extra_orchestrator: dict[str, str] | None = None,
) -> Path:
    data = {
        "orchestrator": {
            "runtime_backend": backend,
            "cli_path": str(cli_path) if cli_path is not None else None,
        },
        "llm": {"backend": "claude_code"},
        "persistence": {"database_path": "data/ouroboros.db"},
    }
    if extra_orchestrator:
        data["orchestrator"].update(extra_orchestrator)
    config_path = config_dir / "config.yaml"
    config_path.write_text(yaml.dump(data))
    return config_path


def _write_credentials(config_dir: Path, api_key: str = "sk-present") -> None:
    (config_dir / "credentials.yaml").write_text(
        yaml.dump({"providers": {"anthropic": {"api_key": api_key}}})
    )


def test_health_reports_all_ok(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    data_dir = config_dir / "data"
    data_dir.mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    db = data_dir / "ouroboros.db"
    db.write_text("")
    _write_config(config_dir, cli_path=cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Configuration" in result.output
    assert "Database" in result.output
    assert "Runtime backend" in result.output
    assert "Credentials" in result.output
    assert "sk-present" not in result.output
    assert "key present" in result.output


def test_health_exits_nonzero_on_config_load_failure(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "config.yaml").write_text("orchestrator: []")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Configuration" in result.output
    assert "error" in result.output
    assert "Invalid config section" in result.output


def test_health_exits_nonzero_when_runtime_cli_missing(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    (config_dir / "data" / "ouroboros.db").write_text("")
    missing_cli = tmp_path / "missing" / "claude"
    _write_config(config_dir, cli_path=missing_cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Runtime backend" in result.output
    assert "CLI not found" in result.output
    assert str(missing_cli) in result.output


def test_health_exits_nonzero_when_credentials_file_missing(monkeypatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "missing" in result.output
    assert "credentials.yaml" in result.output
    assert "API_KEY" not in result.output


def test_health_warns_but_exits_zero_for_missing_database_and_empty_key(
    monkeypatch, tmp_path: Path
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    _clear_auth_env(monkeypatch)
    cli = _make_cli(tmp_path / "bin" / "claude")
    _write_config(config_dir, cli_path=cli)
    (config_dir / "credentials.yaml").write_text(
        yaml.dump({"providers": {"anthropic": {"api_key": ""}}})
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Database" in result.output
    assert "warning" in result.output
    assert "will be created on first run" in result.output
    assert "key is empty" in result.output


def test_health_accepts_provider_api_key_from_environment(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "ANTHROPIC_API_KEY present" in result.output
    assert "sk-from-env" not in result.output


def test_health_uses_llm_backend_environment_override_for_codex_oauth(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "codex OAuth file present" in result.output


def test_health_rejects_configured_runtime_cli_that_is_not_executable(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = tmp_path / "bin" / "claude"
    cli.parent.mkdir()
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o644)
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Runtime backend" in result.output
    assert "CLI not found" in result.output


def test_health_honors_agent_runtime_and_cli_path_environment_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    stale_claude = tmp_path / "missing" / "claude"
    codex_cli = _make_cli(tmp_path / "custom-bin" / "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=stale_claude, backend="claude")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_CODEX_CLI_PATH", str(codex_cli))
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert f"codex: {codex_cli}" in result.output
    assert str(stale_claude) not in result.output
    assert "codex OAuth file present" in result.output


def test_health_honors_effective_backend_configured_cli_when_runtime_env_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    stale_claude = tmp_path / "missing" / "claude"
    codex_cli = _make_cli(tmp_path / "configured-bin" / "codex")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text("{}")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(
        config_dir,
        cli_path=stale_claude,
        backend="claude",
        extra_orchestrator={"codex_cli_path": str(codex_cli)},
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert f"codex: {codex_cli}" in result.output
    assert str(stale_claude) not in result.output


def test_health_treats_copilot_as_cli_authenticated_not_openai_key_backend(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    copilot_cli = _make_cli(tmp_path / "bin" / "copilot")
    (config_dir / "data" / "ouroboros.db").write_text("")
    (config_dir / "config.yaml").write_text(
        yaml.dump(
            {
                "orchestrator": {
                    "runtime_backend": "copilot",
                    "copilot_cli_path": str(copilot_cli),
                },
                "llm": {"backend": "copilot"},
                "persistence": {"database_path": "data/ouroboros.db"},
            }
        )
    )
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "copilot uses local CLI authentication" in result.output
    assert "OPENAI_API_KEY" not in result.output


def test_health_accepts_claude_sdk_default_without_configured_cli(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=None, backend="claude")
    _write_credentials(config_dir)
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 0
    assert "Runtime backend" in result.output
    assert "claude: SDK default" in result.output


def test_health_reports_malformed_credentials_without_crashing(monkeypatch, tmp_path: Path) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    (config_dir / "credentials.yaml").write_text("[]")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "must contain a mapping" in result.output


def test_health_reports_malformed_credentials_providers_without_crashing(
    monkeypatch, tmp_path: Path
) -> None:
    _clear_auth_env(monkeypatch)
    config_dir = tmp_path / "config"
    (config_dir / "data").mkdir(parents=True)
    cli = _make_cli(tmp_path / "bin" / "claude")
    (config_dir / "data" / "ouroboros.db").write_text("")
    _write_config(config_dir, cli_path=cli)
    (config_dir / "credentials.yaml").write_text("providers: []")
    monkeypatch.setattr("ouroboros.config.models.get_config_dir", lambda: config_dir)

    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "Credentials" in result.output
    assert "credentials providers must be a mapping" in result.output
