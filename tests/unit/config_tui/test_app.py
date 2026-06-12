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

from ouroboros.config_tui import persistence
from ouroboros.config_tui.app import (
    CUSTOM_SENTINEL,
    INHERIT_SENTINEL,
    INSTALL_REQUIRED_SUFFIX,
    SEARCH_SENTINEL,
    ModelSearchScreen,
    SettingsApp,
)
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
            assert pilot.app.query_one(f"#stage-model-{stage.value}", Select)
        assert pilot.app.query_one("#global-runtime", Select)
        assert pilot.app.query_one("#global-llm-backend", Select)


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


@pytest.mark.asyncio
async def test_env_override_badge_rendered(app_env, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "codex")
    app = SettingsApp()
    async with app.run_test() as pilot:
        warnings = [str(w.render()) for w in pilot.app.query(".env-warning").results(Static)]
        assert any("OUROBOROS_LLM_BACKEND" in text for text in warnings)


@pytest.mark.asyncio
async def test_env_override_badge_absent_when_unset(app_env, monkeypatch) -> None:
    for name in ("OUROBOROS_LLM_BACKEND", "OUROBOROS_AGENT_RUNTIME", "OUROBOROS_RUNTIME"):
        monkeypatch.delenv(name, raising=False)
    app = SettingsApp()
    async with app.run_test() as pilot:
        warnings = [str(w.render()) for w in pilot.app.query(".env-warning").results(Static)]
        assert not any("OUROBOROS_LLM_BACKEND" in text for text in warnings)


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
    assert applied["orchestrator.runtime_profile.stages.execute"] is None


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
        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        runtime_select = pilot.app.query_one(f"#stage-runtime-{stage}", Select)
        assert runtime_select.value == "codex"
        before_value = model_select.value

        pilot.app.query_one("#global-runtime", Select).value = "hermes"
        await pilot.pause()

        assert "codex" in str(caption.render())
        assert model_select.value == before_value


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
    to (read from the CLI's own config), e.g. 'default — currently gpt-9-test'."""
    app = SettingsApp()
    async with app.run_test() as pilot:
        stage = Stage.INTERVIEW.value
        pilot.app.query_one(f"#stage-runtime-{stage}", Select).value = "codex"
        await pilot.pause()
        model_select = pilot.app.query_one(f"#stage-model-{stage}", Select)
        labels = {str(label) for label, _ in model_select._options}
        assert any("default — currently gpt-9-test" in label for label in labels)
        assert model_select.value == "default"  # value stays the sentinel
