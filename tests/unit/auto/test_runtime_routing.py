from __future__ import annotations

from ouroboros.auto import runtime_routing
from ouroboros.config.models import OrchestratorConfig, OuroborosConfig, RuntimeProfileConfig


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
