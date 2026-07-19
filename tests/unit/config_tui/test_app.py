"""Textual pilot tests for the settings app (#1413).

UI behavior under test: stage cards render, runtime selection re-populates
the dependent model options, uninstalled backends are badged, env-override
warnings show, and Save routes every change through the validated
persistence layer.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
from textual.widgets import Input, OptionList, Select, Static

from ouroboros.backends import runtime_backend_choices
from ouroboros.backends.model_catalog import DEFAULT_MODEL_SENTINEL, uses_default_model_sentinel
from ouroboros.config_tui import persistence
from ouroboros.config_tui.app import (
    CUSTOM_SENTINEL,
    INHERIT_SENTINEL,
    INSTALL_REQUIRED_SUFFIX,
    SEARCH_SENTINEL,
    ModelSearchScreen,
    SettingsApp,
)
from ouroboros.config_tui.fields import STAGE_MODEL_FIELDS
from ouroboros.orchestrator_stage import Stage


@pytest.fixture
def app_env(monkeypatch):
    """Isolate the app from the real ~/.ouroboros and PATH."""
    raw = {
        "orchestrator": {
            "runtime_backend": "claude",
            "runtime_profile": {"stages": {"execute": "codex"}},
        },
        "llm": {"backend": "claude_code"},
    }
    monkeypatch.setattr(persistence, "load_raw_config", lambda: dict(raw))
    installed = {name: f"/bin/{name}" for name in ("claude", "codex")}
    monkeypatch.setattr(
        "ouroboros.config_tui.app.installed_backends",
        lambda: dict(installed),
    )
    # Never let unit tests shell out to real backend CLIs.
    monkeypatch.setattr("ouroboros.config_tui.app.refresh_models", lambda _backend: None)
    # ...or read the real ~/.hermes / ~/.codex configs.
    monkeypatch.setattr(
        "ouroboros.config_tui.app.configured_default_model",
        lambda backend: "gpt-9-test" if backend == "codex" else None,
    )
    return raw


async def _run_app() -> SettingsApp:
    return SettingsApp()


@pytest.mark.asyncio
async def test_stage_cards_render_for_all_stages(app_env) -> None:
    app = SettingsApp()
    async with app.run_test() as pilot:
        for stage in Stage:
            assert pilot.app.query_one(f"#stage-card-{stage.value}")
            assert pilot.app.query_one(f"#stage-runtime-{stage.value}", Select)
            if stage in STAGE_MODEL_FIELDS:
                assert pilot.app.query_one(f"#stage-model-{stage.value}", Select)
            else:
                assert not list(pilot.app.query(f"#stage-model-{stage.value}").results(Select))
        assert pilot.app.query_one("#global-runtime", Select)
        assert not list(pilot.app.query("#global-llm-backend").results(Select))


@pytest.mark.asyncio
async def test_uninstalled_backend_option_is_badged(app_env) -> None:
    app = SettingsApp()
    async with app.run_test() as pilot:
        select = pilot.app.query_one("#global-runtime", Select)
        labels = {str(label) for label, _ in select._options}
        assert any("hermes" in label and INSTALL_REQUIRED_SUFFIX in label for label in labels)
        assert "claude" in labels  # installed backends carry no badge


@pytest.mark.asyncio
async def test_runtime_change_repopulates_model_options(app_env) -> None:
    app = SettingsApp()
    async with app.run_test() as pilot:
        runtime_select = pilot.app.query_one(f"#stage-runtime-{Stage.INTERVIEW.value}", Select)
        runtime_select.value = "codex"
        await pilot.pause()
        model_select = pilot.app.query_one(f"#stage-model-{Stage.INTERVIEW.value}", Select)
        values = {value for _, value in model_select._options}
        assert "default" in values  # codex catalog sentinel
        assert CUSTOM_SENTINEL in values


@pytest.mark.asyncio
async def test_agent_change_resets_incompatible_stage_model(app_env) -> None:
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        assert model_select.value == "claude-opus-4-8"

        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()

        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        assert model_select.value == "default"
        values = {value for _, value in model_select._options}
        assert "claude-opus-4-8" not in values


@pytest.mark.asyncio
async def test_selecting_uninstalled_runtime_shows_install_warning(app_env) -> None:
    app = SettingsApp()
    async with app.run_test() as pilot:
        runtime_select = pilot.app.query_one(f"#stage-runtime-{Stage.REFLECT.value}", Select)
        runtime_select.value = "hermes"
        await pilot.pause()
        warning = pilot.app.query_one(f"#stage-install-warning-{Stage.REFLECT.value}", Static)
        assert not warning.has_class("hidden")
        runtime_select.value = "codex"
        await pilot.pause()
        assert warning.has_class("hidden")


@pytest.mark.asyncio
async def test_custom_model_choice_reveals_input(app_env) -> None:
    app = SettingsApp()
    async with app.run_test() as pilot:
        model_select = pilot.app.query_one(f"#stage-model-{Stage.EVALUATE.value}", Select)
        model_select.value = CUSTOM_SENTINEL
        await pilot.pause()
        custom = pilot.app.query_one(f"#stage-model-custom-{Stage.EVALUATE.value}", Input)
        assert not custom.has_class("hidden")
        assert custom.placeholder == "e.g. terra"


@pytest.mark.asyncio
async def test_env_override_badge_rendered(app_env, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_CLARIFICATION_MODEL", "gpt-test")
    app = SettingsApp()
    async with app.run_test() as pilot:
        warnings = [str(w.render()) for w in pilot.app.query(".env-warning").results(Static)]
        assert any("OUROBOROS_CLARIFICATION_MODEL" in text for text in warnings)


@pytest.mark.asyncio
async def test_env_override_badge_absent_when_unset(app_env, monkeypatch) -> None:
    for name in ("OUROBOROS_LLM_BACKEND", "OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME"):
        monkeypatch.delenv(name, raising=False)
    app = SettingsApp()
    async with app.run_test() as pilot:
        warnings = [str(w.render()) for w in pilot.app.query(".env-warning").results(Static)]
        assert not any("OUROBOROS_LLM_BACKEND" in text for text in warnings)


@pytest.mark.asyncio
async def test_runtime_env_override_drives_inherited_stage_cards(app_env, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_RUNTIME", "codex")

    app = SettingsApp()
    assert app._effective_stage_backend(Stage.INTERVIEW) == "codex"
    async with app.run_test() as pilot:
        caption = pilot.app.query_one(f"#stage-resolved-{Stage.INTERVIEW.value}", Static)
        assert "codex" in str(caption.render())

        model_select = pilot.app.query_one(f"#stage-model-{Stage.INTERVIEW.value}", Select)
        values = {value for _, value in model_select._options}
        assert "default" in values


@pytest.mark.asyncio
async def test_save_routes_changes_through_validated_persistence(app_env, monkeypatch) -> None:
    applied: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: applied.update(values))
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one("#global-runtime", Select).value = "codex"
        runtime_select = pilot.app.query_one(f"#stage-runtime-{Stage.EXECUTE.value}", Select)
        runtime_select.value = INHERIT_SENTINEL  # clears the existing codex override
        await pilot.pause()
        pilot.app.query_one("#save-button").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#save-button")
        await pilot.pause()
    assert applied["orchestrator.runtime_backend"] == "codex"
    assert applied["llm.backend"] == "codex"
    assert applied["orchestrator.runtime_profile.stages.execute"] is None


@pytest.mark.asyncio
async def test_hidden_llm_backend_syncs_to_latest_stage_agent(app_env, monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))

    app = SettingsApp()
    async with app.run_test() as pilot:
        assert not list(pilot.app.query("#global-llm-backend").results(Select))
        pilot.app.query_one(f"#stage-runtime-{Stage.REFLECT.value}", Select).value = "codex"
        await pilot.pause()
        pilot.app.action_save()
        await pilot.pause()

    assert captured["orchestrator.runtime_profile.stages.reflect"] == "codex"
    assert captured["llm.backend"] == "codex"


@pytest.mark.asyncio
async def test_hidden_llm_backend_preserved_without_agent_change(app_env) -> None:
    """An unrelated save must not clobber a user-managed llm.backend.

    Regression: the hidden llm.backend fallback was written on every save, so
    saving without touching any Agent selector silently overwrote an explicit
    user value with the default runtime. It must only sync after an intentional
    backend-routing change.
    """
    # Explicit llm.backend that differs (canonically) from the default runtime.
    app_env["llm"]["backend"] = "codex"  # default runtime stays "claude"
    app = SettingsApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        # No stage/default Agent selection happened this session.
        changes = pilot.app._collect_changes()
    assert "llm.backend" not in changes


@pytest.mark.asyncio
async def test_save_failure_is_surfaced_inline(app_env, monkeypatch) -> None:
    def _reject(values):
        raise persistence.ConfigWriteError("Unknown config key 'x'")

    monkeypatch.setattr(persistence, "apply_config_values", _reject)
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one("#global-runtime", Select).value = "codex"
        await pilot.pause()  # let the cascade settle before measuring layout
        pilot.app.query_one("#save-button").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#save-button")
        await pilot.pause()
        status = pilot.app.query_one("#status-bar", Static)
        assert "Save failed" in str(status.render())


def test_settings_app_imports_without_monitor_tui() -> None:
    """Import-isolation contract for ourocode reuse (#1413 AC)."""
    code = (
        "import sys; import ouroboros.config_tui.app; "
        "assert 'ouroboros.tui.app' not in sys.modules, 'monitor TUI leaked'; "
        "assert 'ouroboros.tui' not in sys.modules, 'monitor TUI package leaked'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.asyncio
async def test_global_change_cascades_to_inheriting_cards(app_env) -> None:
    """Changing the default agent re-resolves inheriting cards: the
    '→ runs on <agent>' caption updates and the model select repopulates to
    the new backend's catalog with its default selected (UX: #1411)."""
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        caption = pilot.app.query_one(f"#stage-resolved-{stage}", Static)
        assert "claude" in str(caption.render())

        pilot.app.query_one("#global-runtime", Select).value = "codex"
        await pilot.pause()

        assert "codex" in str(caption.render())
        runtime_select = pilot.app.query_one(f"#stage-runtime-{stage}", Select)
        assert runtime_select.value == INHERIT_SENTINEL  # selection preserved
        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        assert model_select.value == "default"  # codex catalog default
        values = {value for _, value in model_select._options}
        assert "claude-opus-4-8" not in values  # stale claude id dropped


@pytest.mark.asyncio
async def test_explicit_stage_agent_not_affected_by_global_change(app_env) -> None:
    """A card with an explicit agent keeps its model catalog when the
    default changes — only inheriting cards cascade."""
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.EXECUTE.value  # fixture pins execute to codex
        caption = pilot.app.query_one(f"#stage-resolved-{stage}", Static)
        runtime_select = pilot.app.query_one(f"#stage-runtime-{stage}", Select)
        assert runtime_select.value == "codex"

        pilot.app.query_one("#global-runtime", Select).value = "hermes"
        await pilot.pause()

        assert "codex" in str(caption.render())
        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        assert model_select.value == "default"


@pytest.mark.asyncio
async def test_dynamic_model_listing_merges_into_select(app_env, monkeypatch) -> None:
    """A verified CLI listing expands the model choices in the background,
    without displacing the static default or the current selection."""

    def _fake_listing(backend):
        if backend == "codex":
            return ("openai/gpt-5.2-codex", "openai/o5-mini")
        return None

    monkeypatch.setattr("ouroboros.config_tui.app.refresh_models", _fake_listing)
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        values = {value for _, value in model_select._options}
        assert "openai/gpt-5.2-codex" in values  # fetched entries merged
        assert "default" in values  # static catalog kept first
        assert model_select.value == "default"  # selection not displaced


@pytest.mark.asyncio
async def test_large_listing_collapses_into_search_option(app_env, monkeypatch) -> None:
    """Hundreds of fetched models stay behind a 'Search N models…' entry
    instead of flooding the dropdown."""
    big = tuple(f"provider/model-{i}" for i in range(300))
    monkeypatch.setattr(
        "ouroboros.config_tui.app.refresh_models",
        lambda backend: big if backend == "codex" else None,
    )
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        values = [value for _, value in model_select._options]
        assert SEARCH_SENTINEL in values
        assert len(values) < 30  # static catalog + sentinels only, not 300 rows
        labels = {str(label) for label, _ in model_select._options}
        assert any("Search 300 models" in label for label in labels)


@pytest.mark.asyncio
async def test_search_modal_filters_and_applies_choice(app_env, monkeypatch) -> None:
    big = tuple(f"provider/model-{i}" for i in range(300)) + ("anthropic/claude-opus-4-8",)
    monkeypatch.setattr(
        "ouroboros.config_tui.app.refresh_models",
        lambda backend: big if backend == "codex" else None,
    )
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        model_select.value = SEARCH_SENTINEL
        await pilot.pause()
        assert isinstance(pilot.app.screen, ModelSearchScreen)

        search_input = pilot.app.screen.query_one("#search-input", Input)
        search_input.value = "anthropic"
        await pilot.pause()
        results = pilot.app.screen.query_one("#search-results", OptionList)
        assert results.option_count == 1

        results.highlighted = 0
        results.action_select()
        await pilot.pause()
        assert model_select.value == "anthropic/claude-opus-4-8"


@pytest.mark.asyncio
async def test_search_modal_cancel_restores_previous_value(app_env, monkeypatch) -> None:
    big = tuple(f"provider/model-{i}" for i in range(300))
    monkeypatch.setattr(
        "ouroboros.config_tui.app.refresh_models",
        lambda backend: big if backend == "codex" else None,
    )
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        assert model_select.value == "default"
        model_select.value = SEARCH_SENTINEL
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert model_select.value == "default"


@pytest.mark.asyncio
async def test_default_sentinel_label_shows_configured_model(app_env) -> None:
    """For sentinel backends the 'default' entry names the model it resolves
    to (read from the CLI's own config), e.g. 'Use Codex default model — configured: gpt-9-test'."""
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        labels = {str(label) for label, _ in model_select._options}
        assert any("Use Codex default model — configured: gpt-9-test" in label for label in labels)
        assert model_select.value == "default"  # value stays the sentinel


@pytest.mark.parametrize(
    "backend",
    [backend for backend in runtime_backend_choices() if uses_default_model_sentinel(backend)],
)
def test_default_sentinel_describes_the_runtime_default_for_every_cli_backend(
    app_env, backend: str
) -> None:
    """Every CLI-owned default describes the actual model-selection behavior."""
    label = SettingsApp()._model_label(backend, DEFAULT_MODEL_SENTINEL)

    assert label.startswith("Use ")


@pytest.mark.asyncio
async def test_preset_button_stages_models_for_every_card(app_env) -> None:
    """One click sets a coherent model tier across all stages, respecting
    each card's effective backend when the stage has a model selector."""
    app = SettingsApp()
    async with app.run_test() as pilot:
        await pilot.click("#preset-frugal")
        await pilot.pause()
        interview_model = pilot.app.query_one(f"#stage-model-{Stage.INTERVIEW.value}", Select)
        assert interview_model.value == "claude-haiku-4-5-20251001"  # claude frugal
        execute_model = pilot.app.query_one(f"#stage-model-{Stage.EXECUTE.value}", Select)
        assert execute_model.value == "gpt-5-mini"  # codex frugal
        status = pilot.app.query_one("#status-bar", Static)
        assert "frugal" in str(status.render())
        assert "Save" in str(status.render())  # staged, not saved


@pytest.mark.asyncio
async def test_save_persists_execute_model_pin(app_env, monkeypatch) -> None:
    """The web/TUI Execute card persists a deliberate model pin."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))
    app = SettingsApp()
    async with app.run_test() as pilot:
        model_select = pilot.app.query_one(f"#stage-model-{Stage.EXECUTE.value}", Select)
        model_select.value = "gpt-5-codex"
        await pilot.pause()
        pilot.app.action_save()
        await pilot.pause()

    assert captured["execution.default_model"] == "gpt-5-codex"


@pytest.mark.asyncio
async def test_execute_automatic_survives_runtime_change_without_creating_a_pin(
    app_env, monkeypatch
) -> None:
    """Changing Execute's runtime never turns its default-model choice into a pin."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one(f"#stage-runtime-{Stage.EXECUTE.value}", Select).value = "claude"
        await pilot.pause()
        model_select = pilot.app.query_one(f"#stage-model-{Stage.EXECUTE.value}", Select)
        assert model_select.value == "default"
        pilot.app.action_save()
        await pilot.pause()

    assert "execution.default_model" not in captured


@pytest.mark.asyncio
async def test_routing_preset_stages_runtime_for_every_stage(app_env) -> None:
    """One click stages a coherent multi-LLM routing across all four stages."""
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one("#route-tri-vendor").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#route-tri-vendor")
        await pilot.pause()
        expected = {
            Stage.INTERVIEW: "claude",
            Stage.EXECUTE: "claude",
            Stage.EVALUATE: "antigravity",
            Stage.REFLECT: "grok",
        }
        for stage, backend in expected.items():
            runtime_select = pilot.app.query_one(f"#stage-runtime-{stage.value}", Select)
            assert runtime_select.value == backend
        status = pilot.app.query_one("#status-bar", Static)
        assert "tri-vendor" in str(status.render())
        assert "Save" in str(status.render())  # staged, not saved


@pytest.mark.asyncio
async def test_routing_preset_persists_stage_backends_on_save(app_env, monkeypatch) -> None:
    """Saving after a routing preset records orchestrator.runtime_profile.stages."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one("#route-claude-verify").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#route-claude-verify")
        await pilot.pause()
        pilot.app.query_one("#save-button").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#save-button")
        await pilot.pause()
    assert captured.get("orchestrator.runtime_profile.stages.evaluate") == "antigravity"


@pytest.mark.asyncio
async def test_routing_preset_with_runtime_only_final_stage_persists_safely(
    app_env, monkeypatch
) -> None:
    """A routing preset whose LAST staged card is a runtime-only backend (the
    tri-vendor preset ends with reflect=grok) must persist runtime_profile.stages
    WITHOUT mirroring the runtime-only agent into the completion-only llm.backend
    field — LLMConfig.backend rejects grok/antigravity and would roll the save
    back."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one("#route-tri-vendor").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#route-tri-vendor")
        await pilot.pause()
        pilot.app.query_one("#save-button").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#save-button")
        await pilot.pause()
    # The runtime-only reflect=grok / evaluate=antigravity stages are persisted...
    assert captured.get("orchestrator.runtime_profile.stages.reflect") == "grok"
    assert captured.get("orchestrator.runtime_profile.stages.evaluate") == "antigravity"
    # ...but the legacy llm.backend is NEVER set to a runtime-only backend.
    assert captured.get("llm.backend") not in ("grok", "antigravity")


@pytest.mark.asyncio
async def test_save_summary_shows_diff_and_reconnect_hint(app_env, monkeypatch) -> None:
    monkeypatch.setattr(persistence, "apply_config_values", lambda _values: None)
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one("#global-runtime", Select).value = "codex"
        await pilot.pause()
        pilot.app.query_one("#save-button").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#save-button")
        await pilot.pause()
        status = str(pilot.app.query_one("#status-bar", Static).render())
        assert "claude → codex" in status  # old → new diff
        assert "reconnect" in status  # backend change needs MCP reconnect


@pytest.mark.asyncio
async def test_runtime_only_agent_is_not_synced_to_llm_backend(app_env, monkeypatch) -> None:
    """Selecting a runtime-only backend (antigravity) must NOT write llm.backend.

    LLMConfig.backend rejects antigravity/grok, so syncing the hidden legacy
    llm.backend to a runtime-only agent would persist a config that fails
    validation on next load.
    """
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))
    monkeypatch.setattr(
        "ouroboros.config_tui.app.installed_backends",
        lambda: {"claude": "/bin/claude", "codex": "/bin/codex", "antigravity": "/bin/agy"},
    )
    app = SettingsApp()
    async with app.run_test() as pilot:
        pilot.app.query_one("#global-runtime", Select).value = "antigravity"
        await pilot.pause()
        pilot.app.query_one("#save-button").scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click("#save-button")
        await pilot.pause()
    # The runtime backend is persisted, but the runtime-only agent is never
    # synced into the completion-only llm.backend field.
    assert captured.get("orchestrator.runtime_backend") == "antigravity"
    assert "llm.backend" not in captured


@pytest.mark.asyncio
async def test_save_uses_stage_agent_for_default_sentinel_validation(monkeypatch) -> None:
    raw = {
        "orchestrator": {"runtime_backend": "claude"},
        "llm": {"backend": "claude_code"},
    }
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "load_raw_config", lambda: dict(raw))
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))
    monkeypatch.setattr(
        "ouroboros.config_tui.app.installed_backends",
        lambda: {"claude": "/bin/claude", "codex": "/bin/codex"},
    )
    monkeypatch.setattr("ouroboros.config_tui.app.refresh_models", lambda _backend: None)
    monkeypatch.setattr("ouroboros.config_tui.app.configured_default_model", lambda _backend: None)

    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        assert pilot.app.query_one(f"#stage-model-{stage}", Select).value == "default"

        pilot.app.action_save()
        await pilot.pause()

    assert captured["orchestrator.runtime_profile.stages.interview"] == "codex"
    assert captured["llm.backend"] == "codex"
    assert captured["clarification.default_model"] == "default"


@pytest.mark.asyncio
async def test_save_keeps_default_sentinel_when_llm_backend_supports_it(
    app_env, monkeypatch
) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(persistence, "apply_config_values", lambda values: captured.update(values))

    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        pilot.app.query_one(f"#stage-model-{stage}", Select).value = "default"
        await pilot.pause()

        pilot.app.action_save()
        await pilot.pause()

    assert captured["llm.backend"] == "codex"
    assert captured["orchestrator.runtime_profile.stages.interview"] == "codex"
    assert captured["clarification.default_model"] == "default"


def test_save_summary_without_backend_change_has_no_reconnect_hint() -> None:
    summary = SettingsApp._save_summary(
        {"clarification.default_model": "m2"}, {"clarification.default_model": "m1"}
    )
    assert "m1 → m2" in summary
    assert "reconnect" not in summary
