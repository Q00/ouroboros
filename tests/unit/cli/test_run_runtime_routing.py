from __future__ import annotations

import pytest

from ouroboros.cli.commands import run
from ouroboros.config.models import OrchestratorConfig, OuroborosConfig, RuntimeProfileConfig
from ouroboros.core.errors import ConfigError


def test_run_execute_runtime_honors_runtime_profile(monkeypatch) -> None:
    config = OuroborosConfig(
        orchestrator=OrchestratorConfig(
            runtime_backend="claude",
            runtime_profile=RuntimeProfileConfig(stages={"execute": "opencode"}),
        )
    )
    monkeypatch.setattr(run, "load_config", lambda: config)

    resolved = run._resolve_run_execute_runtime_backend(
        None,
        resolve_backend=lambda value=None: value or "claude",
    )

    assert resolved == "opencode"


def test_run_explicit_runtime_overrides_runtime_profile(monkeypatch) -> None:
    config = OuroborosConfig(
        orchestrator=OrchestratorConfig(
            runtime_backend="claude",
            runtime_profile=RuntimeProfileConfig(stages={"execute": "opencode"}),
        )
    )
    monkeypatch.setattr(run, "load_config", lambda: config)

    resolved = run._resolve_run_execute_runtime_backend(
        "codex",
        resolve_backend=lambda value=None: value or "claude",
    )

    assert resolved == "codex"


def test_run_runtime_profile_config_error_fails_fast_when_config_exists(
    monkeypatch, tmp_path
) -> None:
    def _raise() -> OuroborosConfig:
        raise ConfigError("bad runtime_profile")

    (tmp_path / "config.yaml").write_text("orchestrator: {}\n")
    monkeypatch.setattr(run, "load_config", _raise)
    monkeypatch.setattr(run, "get_config_dir", lambda: tmp_path)

    with pytest.raises(ConfigError):
        run._resolve_run_execute_runtime_backend(
            None,
            resolve_backend=lambda value=None: value or "claude",
        )


def test_run_execute_runtime_honors_runtime_profile_default(monkeypatch) -> None:
    config = OuroborosConfig(
        orchestrator=OrchestratorConfig(
            runtime_backend="claude",
            runtime_profile=RuntimeProfileConfig(default="opencode"),
        )
    )
    monkeypatch.setattr(run, "load_config", lambda: config)

    resolved = run._resolve_run_execute_runtime_backend(
        None,
        resolve_backend=lambda value=None: value or "claude",
    )

    assert resolved == "opencode"


def test_run_runtime_profile_absent_config_falls_back(monkeypatch, tmp_path) -> None:
    def _raise() -> OuroborosConfig:
        raise ConfigError("Configuration file not found")

    monkeypatch.setattr(run, "load_config", _raise)
    monkeypatch.setattr(run, "get_config_dir", lambda: tmp_path)

    resolved = run._resolve_run_execute_runtime_backend(
        None,
        resolve_backend=lambda value=None: value or "claude",
    )

    assert resolved == "claude"
