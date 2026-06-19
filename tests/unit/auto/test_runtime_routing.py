from __future__ import annotations

import pytest

from ouroboros.auto import runtime_routing
from ouroboros.config.models import OrchestratorConfig, OuroborosConfig, RuntimeProfileConfig
from ouroboros.core.errors import ConfigError


def test_auto_stage_runtime_plan_honors_runtime_profile(monkeypatch) -> None:
    config = OuroborosConfig(
        orchestrator=OrchestratorConfig(
            runtime_backend="opencode",
            opencode_mode="plugin",
            runtime_profile=RuntimeProfileConfig(
                stages={
                    "interview": "gjc",
                    "execute": "pi",
                }
            ),
        )
    )
    monkeypatch.setattr(runtime_routing, "load_config", lambda: config)

    plan = runtime_routing.resolve_auto_stage_runtime_plan(
        runtime_override=None,
        fallback_runtime_backend="opencode",
        fallback_opencode_mode="plugin",
    )

    assert plan.default.runtime_backend == "opencode"
    assert plan.default.opencode_mode == "plugin"
    assert plan.interview.runtime_backend == "gjc"
    assert plan.interview.opencode_mode is None
    assert plan.execute.runtime_backend == "pi"
    assert plan.execute.opencode_mode is None
    assert plan.evaluate.runtime_backend == "opencode"
    assert plan.evaluate.opencode_mode == "plugin"


def test_auto_stage_runtime_plan_explicit_runtime_overrides_profile(monkeypatch) -> None:
    config = OuroborosConfig(
        orchestrator=OrchestratorConfig(
            runtime_backend="opencode",
            runtime_profile=RuntimeProfileConfig(
                stages={
                    "interview": "gjc",
                    "execute": "pi",
                }
            ),
        )
    )
    monkeypatch.setattr(runtime_routing, "load_config", lambda: config)

    plan = runtime_routing.resolve_auto_stage_runtime_plan(
        runtime_override="codex",
        fallback_runtime_backend="opencode",
        fallback_opencode_mode="plugin",
    )

    assert plan.default.runtime_backend == "codex"
    assert plan.interview.runtime_backend == "codex"
    assert plan.execute.runtime_backend == "codex"
    assert plan.evaluate.runtime_backend == "codex"


def test_invalid_profile_config_fails_fast_when_config_file_exists(monkeypatch, tmp_path) -> None:
    """A present-but-invalid config must FAIL FAST, not silently fall back.

    If ``load_config()`` raises (malformed YAML or failed validation — e.g. an
    unknown runtime_profile stage key/backend) AND the config file exists, the
    resolver must propagate the error so an operator routing mistake surfaces
    instead of silently rerouting auto to the fallback runtime.
    """

    def _raise() -> OuroborosConfig:
        raise ConfigError("Configuration validation failed: bad runtime_profile stage")

    (tmp_path / "config.yaml").write_text("orchestrator: {}\n")
    monkeypatch.setattr(runtime_routing, "load_config", _raise)
    monkeypatch.setattr(runtime_routing, "get_config_dir", lambda: tmp_path)

    with pytest.raises(ConfigError):
        runtime_routing.resolve_auto_stage_runtime_plan(
            runtime_override=None,
            fallback_runtime_backend="opencode",
            fallback_opencode_mode="plugin",
        )


def test_absent_config_file_falls_back_without_error(monkeypatch, tmp_path) -> None:
    """A genuinely absent config file is a legitimate "no profile" → fall back.

    ``load_config()`` raises ConfigError when no config file exists; with no
    ``config.yaml`` in the config dir the resolver must swallow it and use the
    fallback runtime rather than failing.
    """

    def _raise() -> OuroborosConfig:
        raise ConfigError("Configuration file not found")

    # tmp_path has no config.yaml → genuinely absent.
    monkeypatch.setattr(runtime_routing, "load_config", _raise)
    monkeypatch.setattr(runtime_routing, "get_config_dir", lambda: tmp_path)

    plan = runtime_routing.resolve_auto_stage_runtime_plan(
        runtime_override=None,
        fallback_runtime_backend="opencode",
        fallback_opencode_mode="plugin",
    )

    assert plan.default.runtime_backend == "opencode"
    assert plan.interview.runtime_backend == "opencode"
    assert plan.execute.runtime_backend == "opencode"
