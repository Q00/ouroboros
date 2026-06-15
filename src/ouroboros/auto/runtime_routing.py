"""Stage-aware runtime routing for ``ooo auto`` entry points."""

from __future__ import annotations

from dataclasses import dataclass

from ouroboros.config import get_opencode_mode, load_config
from ouroboros.core.errors import ConfigError
from ouroboros.orchestrator import resolve_agent_runtime_backend
from ouroboros.orchestrator_stage import Stage, parse_stage, resolve_runtime_for_stage


@dataclass(frozen=True, slots=True)
class StageRuntime:
    """Resolved runtime backend plus the OpenCode mode relevant to it."""

    runtime_backend: str
    opencode_mode: str | None


@dataclass(frozen=True, slots=True)
class AutoStageRuntimePlan:
    """Resolved runtime bindings used by auto's coarse pipeline phases."""

    default: StageRuntime
    interview: StageRuntime
    execute: StageRuntime
    evaluate: StageRuntime
    reflect: StageRuntime


def resolve_auto_stage_runtime_plan(
    *,
    runtime_override: str | None,
    fallback_runtime_backend: str | None,
    fallback_opencode_mode: str | None,
) -> AutoStageRuntimePlan:
    """Resolve auto runtime bindings from ``--runtime`` or runtime_profile.

    ``--runtime`` preserves the historical auto contract: one explicit runtime
    drives both authoring and execution. Without that override, auto now honors
    ``orchestrator.runtime_profile.stages`` using the shared stage resolver.
    """

    default_runtime = resolve_agent_runtime_backend(
        runtime_override or fallback_runtime_backend
    )
    default = StageRuntime(
        runtime_backend=default_runtime,
        opencode_mode=_opencode_mode_for_runtime(default_runtime, fallback_opencode_mode),
    )

    profile_stages: dict[Stage, str] | None = None
    profile_default: str | None = None
    if runtime_override is None:
        try:
            profile = load_config().orchestrator.runtime_profile
        except ConfigError:
            profile = None
        if profile is not None:
            profile_stages = {parse_stage(key): value for key, value in profile.stages.items()}
            profile_default = profile.default

    def resolve(stage: Stage) -> StageRuntime:
        if runtime_override is not None:
            runtime = default.runtime_backend
        else:
            runtime = resolve_agent_runtime_backend(
                resolve_runtime_for_stage(
                    stage,
                    stages=profile_stages,
                    default=profile_default,
                    fallback=default.runtime_backend,
                )
            )
        return StageRuntime(
            runtime_backend=runtime,
            opencode_mode=_opencode_mode_for_runtime(runtime, fallback_opencode_mode),
        )

    return AutoStageRuntimePlan(
        default=default,
        interview=resolve(Stage.INTERVIEW),
        execute=resolve(Stage.EXECUTE),
        evaluate=resolve(Stage.EVALUATE),
        reflect=resolve(Stage.REFLECT),
    )


def demote_plugin_opencode_mode(opencode_mode: str | None) -> str | None:
    """Return the in-process-safe OpenCode mode for non-plugin consumers."""

    return "subprocess" if opencode_mode == "plugin" else opencode_mode


def _opencode_mode_for_runtime(
    runtime_backend: str,
    fallback_opencode_mode: str | None,
) -> str | None:
    if runtime_backend != "opencode":
        return None
    return fallback_opencode_mode or get_opencode_mode()


__all__ = [
    "AutoStageRuntimePlan",
    "StageRuntime",
    "demote_plugin_opencode_mode",
    "resolve_auto_stage_runtime_plan",
]
