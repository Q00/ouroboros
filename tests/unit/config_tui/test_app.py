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
from textual.widgets import Input, Select, Static

from ouroboros.config_tui import persistence
from ouroboros.config_tui.app import (
    CUSTOM_SENTINEL,
    INHERIT_SENTINEL,
    INSTALL_REQUIRED_SUFFIX,
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
async def test_inherit_label_tracks_global_default(app_env) -> None:
    """The stage inherit option shows the resolved default agent (UX: #1411)."""
    app = SettingsApp()
    async with app.run_test() as pilot:
        runtime_select = pilot.app.query_one(f"#stage-runtime-{Stage.INTERVIEW.value}", Select)
        labels = {str(label) for label, value in runtime_select._options if value}
        assert any("(inherit — claude)" in label for label in labels)

        pilot.app.query_one("#global-runtime", Select).value = "codex"
        await pilot.pause()
        labels = {str(label) for label, value in runtime_select._options if value}
        assert any("(inherit — codex)" in label for label in labels)
        # Rebuilding options must not lose the user's selection.
        assert runtime_select.value == INHERIT_SENTINEL
