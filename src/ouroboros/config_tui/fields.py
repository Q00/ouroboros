"""Field schema for the settings GUI — pure logic, no Textual imports.

Defines which config keys the settings app surfaces, their friendly labels,
and which environment variables override each key's effective value (so the
UI can warn that a saved value will not take effect — the silent-failure
mode reported in discussion #1376 / RFC issue #1395).
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any

from ouroboros.orchestrator_stage import Stage

# Env vars that force the global runtime selection (loader precedence:
# env → config.yaml). Explicit runtime_profile.stages entries are NOT
# overridden by these (they only replace the fallback), so stage selects
# carry no env_vars.
_RUNTIME_ENV_VARS = ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME")


@dataclass(frozen=True, slots=True)
class SettingField:
    """One configurable key surfaced by the settings UI.

    Attributes:
        key: Dot-notation config key (the ``config set`` surface).
        label: Friendly UI label.
        env_vars: Environment variables whose presence overrides the
            effective value of this key at load time.
        stage: Stage name when the field belongs to a stage card.
    """

    key: str
    label: str
    env_vars: tuple[str, ...] = ()
    stage: str | None = None


GLOBAL_RUNTIME_FIELD = SettingField(
    key="orchestrator.runtime_backend",
    label="Default agent (runtime)",
    env_vars=_RUNTIME_ENV_VARS,
)

GLOBAL_LLM_BACKEND_FIELD = SettingField(
    key="llm.backend",
    label="LLM backend (internal calls)",
    env_vars=("OUROBOROS_LLM_BACKEND",),
)


def stage_runtime_field(stage: Stage) -> SettingField:
    """Per-stage runtime select (writes ``orchestrator.runtime_profile.stages.<stage>``)."""
    return SettingField(
        key=f"orchestrator.runtime_profile.stages.{stage.value}",
        label="Agent",
        stage=stage.value,
    )


# Friendly per-stage model bindings. "seed" is not a separate stage: it
# shares clarification config with interview (see Stage docstring).
STAGE_MODEL_FIELDS: dict[Stage, SettingField] = {
    Stage.INTERVIEW: SettingField(
        key="clarification.default_model",
        label="Interview & Seed model",
        env_vars=("OUROBOROS_CLARIFICATION_MODEL",),
        stage=Stage.INTERVIEW.value,
    ),
    Stage.EXECUTE: SettingField(
        key="execution.default_model",
        label="Execute model",
        env_vars=("OUROBOROS_EXECUTION_MODEL",),
        stage=Stage.EXECUTE.value,
    ),
    Stage.EVALUATE: SettingField(
        key="evaluation.semantic_model",
        label="Evaluation model",
        env_vars=("OUROBOROS_SEMANTIC_MODEL",),
        stage=Stage.EVALUATE.value,
    ),
    Stage.REFLECT: SettingField(
        key="resilience.reflect_model",
        label="Reflect model",
        env_vars=("OUROBOROS_REFLECT_MODEL",),
        stage=Stage.REFLECT.value,
    ),
}

ADVANCED_MODEL_FIELDS: tuple[SettingField, ...] = ()


def active_env_overrides(field: SettingField) -> tuple[str, ...]:
    """Names of this field's override env vars that are currently set (non-empty)."""
    return tuple(name for name in field.env_vars if os.environ.get(name, "").strip())


def get_value(data: dict[str, Any], key: str) -> Any:
    """Read a dot-notation key from a raw config dict (``None`` when absent)."""
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


__all__ = [
    "ADVANCED_MODEL_FIELDS",
    "GLOBAL_LLM_BACKEND_FIELD",
    "GLOBAL_RUNTIME_FIELD",
    "STAGE_MODEL_FIELDS",
    "SettingField",
    "active_env_overrides",
    "get_value",
    "stage_runtime_field",
]
