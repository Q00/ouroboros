"""Save planning matches the terminal settings app's reconciliation rules."""

from __future__ import annotations

from ouroboros.gui.settings import INHERIT, SettingsModel, StagedForm


def _plan(raw: dict, payload: dict) -> dict:
    return SettingsModel(raw).plan_changes(StagedForm.from_json(payload))


def test_no_changes_when_form_matches_config() -> None:
    raw = {"orchestrator": {"runtime_backend": "claude"}}
    assert _plan(raw, {"default_runtime": "claude"}) == {}


def test_changing_default_agent_syncs_llm_backend() -> None:
    changes = _plan({"orchestrator": {"runtime_backend": "claude"}}, {"default_runtime": "codex"})
    assert changes["orchestrator.runtime_backend"] == "codex"
    assert changes["llm.backend"] == "codex"


def test_alias_only_change_is_not_a_change() -> None:
    raw = {"orchestrator": {"runtime_backend": "claude"}, "llm": {"backend": "claude"}}
    assert _plan(raw, {"default_runtime": "claude_code"}) == {}


def test_inherit_clears_a_stage_runtime() -> None:
    raw = {"orchestrator": {"runtime_profile": {"stages": {"evaluate": "codex"}}}}
    changes = _plan(raw, {"stages": {"evaluate": {"runtime": INHERIT}}})
    assert changes == {"orchestrator.runtime_profile.stages.evaluate": None}


def test_stale_model_pin_follows_a_backend_change() -> None:
    raw = {
        "orchestrator": {"runtime_backend": "claude"},
        "evaluation": {"semantic_model": "claude-opus-4-8"},
    }
    changes = _plan(raw, {"stages": {"evaluate": {"runtime": "codex"}}})
    assert changes["orchestrator.runtime_profile.stages.evaluate"] == "codex"
    assert changes["evaluation.semantic_model"] is None


def test_explicit_model_is_recorded() -> None:
    changes = _plan(
        {"orchestrator": {"runtime_backend": "claude"}},
        {"stages": {"interview": {"model": "claude-sonnet-5", "model_explicit": True}}},
    )
    assert changes == {"clarification.default_model": "claude-sonnet-5"}


def test_execute_model_is_cleared_when_execute_agent_moves() -> None:
    raw = {
        "orchestrator": {"runtime_backend": "claude"},
        "execution": {"default_model": "claude-opus-4-8"},
    }
    changes = _plan(raw, {"default_runtime": "codex"})
    assert changes["execution.default_model"] is None


def test_view_lists_stages_and_presets() -> None:
    view = SettingsModel({}).view(installed={})
    assert [stage["id"] for stage in view["stages"]] == [
        "interview",
        "execute",
        "evaluate",
        "reflect",
    ]
    assert {preset["id"] for preset in view["presets"]} >= {"all-claude", "codex-core"}
