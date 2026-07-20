"""Tests for the settings field schema and env-override detection (#1413)."""

from __future__ import annotations

from ouroboros.config_tui import fields
from ouroboros.orchestrator_stage import VALID_STAGE_KEYS, Stage


def test_stage_model_fields_cover_all_configurable_stage_models() -> None:
    assert {stage.value for stage in fields.STAGE_MODEL_FIELDS} <= VALID_STAGE_KEYS
    execute_field = fields.STAGE_MODEL_FIELDS[Stage.EXECUTE]
    assert execute_field.key == "execution.default_model"
    assert execute_field.env_vars == ("OUROBOROS_EXECUTION_MODEL",)


def test_stage_runtime_field_targets_runtime_profile() -> None:
    field = fields.stage_runtime_field(Stage.EXECUTE)
    assert field.key == "orchestrator.runtime_profile.stages.execute"
    assert field.stage == "execute"


def test_active_env_overrides_set(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    assert fields.active_env_overrides(fields.GLOBAL_LLM_BACKEND_FIELD) == (
        "OUROBOROS_LLM_BACKEND",
    )


def test_active_env_overrides_unset(monkeypatch) -> None:
    monkeypatch.delenv("OUROBOROS_LLM_BACKEND", raising=False)
    assert fields.active_env_overrides(fields.GLOBAL_LLM_BACKEND_FIELD) == ()


def test_active_env_overrides_blank_value_does_not_count(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_CLARIFICATION_MODEL", "   ")
    field = fields.STAGE_MODEL_FIELDS[Stage.INTERVIEW]
    assert fields.active_env_overrides(field) == ()


def test_blank_execution_model_env_is_an_active_clear_override(monkeypatch) -> None:
    """Unlike other model vars, an empty Execute override intentionally clears a pin."""
    monkeypatch.setenv("OUROBOROS_EXECUTION_MODEL", "   ")
    field = fields.STAGE_MODEL_FIELDS[Stage.EXECUTE]
    assert fields.active_env_overrides(field) == ("OUROBOROS_EXECUTION_MODEL",)


def test_runtime_field_tracks_both_runtime_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_AGENT_RUNTIME", "codex")
    monkeypatch.setenv("OUROBOROS_RUNTIME", "hermes")
    assert fields.active_env_overrides(fields.GLOBAL_RUNTIME_FIELD) == (
        "OUROBOROS_AGENT_RUNTIME",
        "OUROBOROS_RUNTIME",
    )


def test_stage_runtime_selects_have_no_env_override() -> None:
    # Env runtime vars replace only the fallback, never an explicit
    # runtime_profile.stages entry — so stage selects carry no badge.
    for stage in Stage:
        assert fields.stage_runtime_field(stage).env_vars == ()


def test_get_value_dot_navigation() -> None:
    data = {"orchestrator": {"runtime_profile": {"stages": {"execute": "codex"}}}}
    assert fields.get_value(data, "orchestrator.runtime_profile.stages.execute") == "codex"
    assert fields.get_value(data, "orchestrator.runtime_profile.stages.reflect") is None
    assert fields.get_value(data, "missing.key") is None
    assert fields.get_value({"a": "leaf"}, "a.b") is None
