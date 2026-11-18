"""Save planning matches the terminal settings app's reconciliation rules."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from ouroboros.backends import model_catalog
from ouroboros.gui.settings import INHERIT, SettingsModel, StagedForm, model_suggestions


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


# ── model suggestions ────────────────────────────────────────────────

_CODEX_CACHE = Path(__file__).resolve().parents[2] / "fixtures" / "codex_models_cache.json"
_CODEX_LISTED = [
    "gpt-6.2-sol",
    "gpt-6.2-astra",
    "gpt-6.1-terra",
    "gpt-6.1-sol",
    "gpt-6-astra",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-5.6-sol",
]


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    monkeypatch.setattr(model_catalog, "_codex_listed_memo", None)
    return home


@pytest.mark.parametrize("backend", ["codex", "codex_cli"])
def test_codex_suggestions_are_codex_own_list_after_default(codex_home: Path, backend: str) -> None:
    shutil.copy(_CODEX_CACHE, codex_home / "models_cache.json")

    models, labels = model_suggestions(backend)

    assert models == ["default", *_CODEX_LISTED]
    assert labels["gpt-6.2-astra"] == "GPT-6.2-Astra"
    assert labels["gpt-6.1-terra"] == "GPT-6.1 Terra"
    assert set(labels) == set(_CODEX_LISTED)


def test_codex_suggestions_fall_back_to_the_static_catalog(codex_home: Path) -> None:
    assert model_suggestions("codex") == (list(model_catalog.model_choices("codex")), {})
    (codex_home / "models_cache.json").write_text("{broken", encoding="utf-8")
    assert model_suggestions("codex") == (list(model_catalog.model_choices("codex")), {})


def test_other_backends_keep_their_static_catalog(codex_home: Path) -> None:
    shutil.copy(_CODEX_CACHE, codex_home / "models_cache.json")

    assert model_suggestions("claude") == (list(model_catalog.model_choices("claude")), {})
    assert model_suggestions("codex_mcp") == (list(model_catalog.model_choices("codex_mcp")), {})
    assert model_suggestions("nonsense") == ([], {})


def test_the_models_page_lists_codex_models_and_keeps_the_default_hint(codex_home: Path) -> None:
    shutil.copy(_CODEX_CACHE, codex_home / "models_cache.json")
    (codex_home / "config.toml").write_text('model = "gpt-6-sol"\n', encoding="utf-8")
    raw = {"orchestrator": {"runtime_backend": "codex"}, "llm": {"backend": "codex"}}

    stage = next(
        s for s in SettingsModel(raw).view(installed={})["stages"] if s["id"] == "interview"
    )

    assert stage["completion_backend"] == "codex"
    assert stage["models"] == ["default", *_CODEX_LISTED]
    assert stage["model_labels"]["gpt-6-sol"] == "GPT-6-Sol"
    # "default — currently <model>" still has what it needs.
    assert stage["default_hint"] == "gpt-6-sol"


def test_the_models_endpoint_sends_display_names(codex_home: Path) -> None:
    from ouroboros.gui import api

    shutil.copy(_CODEX_CACHE, codex_home / "models_cache.json")
    state = api.ConsoleState(stop=lambda *_args, **_kwargs: None)

    view = api.get_settings_models(state, query={"backend": "codex"}, body=None)

    assert view["backend"] == "codex"
    assert view["models"] == ["default", *_CODEX_LISTED]
    assert view["model_labels"]["gpt-6.2-sol"] == "GPT-6.2-Sol"


def test_the_new_chat_form_gets_codex_own_models_and_names(codex_home: Path) -> None:
    from ouroboros.gui import harness

    shutil.copy(_CODEX_CACHE, codex_home / "models_cache.json")

    defaults = harness.Harness().defaults()

    for pool in ("agents", "llm"):
        codex = next(item for item in defaults[pool] if item["id"] == "codex")
        assert codex["models"] == ["default", *_CODEX_LISTED]
        assert codex["model_labels"]["gpt-6.2-astra"] == "GPT-6.2-Astra"
    claude = next(item for item in defaults["llm"] if item["id"] == "claude")
    assert claude["model_labels"] == {}


def _ask_choice(backend: str) -> dict:
    from ouroboros.gui import harness

    return next(item for item in harness.Harness().defaults()["llm"] if item["id"] == backend)


def test_an_empty_ask_model_names_the_agent_default_it_runs_on(codex_home: Path) -> None:
    shutil.copy(_CODEX_CACHE, codex_home / "models_cache.json")
    assert _ask_choice("codex")["default_model"] is None  # Codex names no model of its own yet

    (codex_home / "config.toml").write_text('model = "gpt-6-sol"\n', encoding="utf-8")
    codex = _ask_choice("codex")

    assert codex["default_model"] == "gpt-6-sol"
    # The server's list keeps the sentinel first; the page leaves it out where
    # an empty field or "Follow the agent" already means it (core/modelChoices.ts).
    assert codex["models"][0] == "default"


def test_an_empty_ask_model_names_a_pinned_interview_model(
    codex_home: Path, isolated_home: Path
) -> None:
    """With a pinned Interview model an empty Ask field runs on that, not the CLI's own."""
    (codex_home / "config.toml").write_text('model = "gpt-6-sol"\n', encoding="utf-8")
    config = isolated_home / ".ouroboros" / "config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "models:\n  pin: true\nclarification:\n  default_model: gpt-6.1-sol\n", encoding="utf-8"
    )

    assert _ask_choice("codex")["default_model"] == "gpt-6.1-sol"


def test_an_unknown_backend_has_no_empty_model_default() -> None:
    from ouroboros.gui import harness

    assert harness._empty_model_default("nonsense") is None
