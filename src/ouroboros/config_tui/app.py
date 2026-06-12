"""Textual settings app for Ouroboros configuration (#1413).

Standalone by design: this module imports Textual and the pure helpers in
this package, never :mod:`ouroboros.tui` — the import-isolation contract
that lets ourocode embed the settings screen without the monitor TUI.
"""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Select,
    Static,
)

from ouroboros.backends import resolve_backend_alias, runtime_backend_choices
from ouroboros.backends.capabilities import llm_backend_choices
from ouroboros.backends.model_catalog import installed_backends, model_choices
from ouroboros.config.models import OuroborosConfig, get_config_dir
from ouroboros.config_tui import persistence
from ouroboros.config_tui.fields import (
    ADVANCED_MODEL_FIELDS,
    GLOBAL_LLM_BACKEND_FIELD,
    GLOBAL_RUNTIME_FIELD,
    STAGE_MODEL_FIELDS,
    SettingField,
    active_env_overrides,
    get_value,
    stage_runtime_field,
)
from ouroboros.orchestrator_stage import Stage

INHERIT_SENTINEL = "__inherit__"
CUSTOM_SENTINEL = "__custom__"

INSTALL_REQUIRED_SUFFIX = "install required"

# Textual's no-selection sentinel compares by identity only; isinstance is
# the robust blank check across widget interactions.
_NO_SELECTION = type(Select.NULL)


def _is_blank(value: Any) -> bool:
    return isinstance(value, _NO_SELECTION)


def _slug(key: str) -> str:
    return key.replace(".", "-").replace("_", "-")


def _canonical_backend(value: Any) -> str:
    """Resolve backend aliases (e.g. ``claude_code`` → ``claude``) for display."""
    candidate = str(value or "")
    try:
        return resolve_backend_alias(candidate)
    except ValueError:
        return candidate


def _env_warning_text(field: SettingField) -> str | None:
    overrides = active_env_overrides(field)
    if not overrides:
        return None
    names = ", ".join(overrides)
    return f"⚠ overridden by {names} — saved value takes effect only after unsetting it"


class SettingsApp(App[None]):
    """Mouse-friendly editor for ``~/.ouroboros/config.yaml``."""

    TITLE = "Ouroboros Settings"

    CSS = """
    #settings-body { padding: 1 2; }
    #config-path { color: $text-muted; text-style: italic; margin: 0 0 1 0; }

    .section-title { text-style: bold; color: $accent; margin: 1 0 0 0; }

    /* Left-to-right stage row: one card per pipeline stage (#1411 mockup). */
    #stage-row {
        layout: grid;
        grid-size: 4;
        grid-gutter: 0 1;
        height: auto;
        margin: 1 0 0 0;
    }
    .stage-card { border: round $primary; padding: 0 1; height: auto; }
    .stage-card:focus-within { border: round $accent; }
    .stage-title { text-style: bold; color: $accent; }
    .field-label { color: $text-muted; margin: 1 0 0 0; }

    /* Defaults band above the stage row. */
    #global-row {
        layout: grid;
        grid-size: 1;
        grid-gutter: 0 1;
        height: auto;
        margin: 1 0 0 0;
    }
    .global-cell { border: round $secondary; padding: 0 1; height: auto; }
    .field-help { color: $text-muted; text-style: italic; margin: 0 0 1 0; }

    #action-bar { layout: horizontal; height: auto; margin: 1 0 0 0; }
    #status-bar { width: 1fr; margin: 0 0 0 2; content-align: left middle; }

    .env-warning { color: $warning; }
    .install-warning { color: $error; }
    .hidden { display: none; }
    """

    # Terminal-safe stage glyphs for the card headers.
    _STAGE_GLYPHS = {
        Stage.INTERVIEW: "✎",
        Stage.EXECUTE: "⚙",
        Stage.EVALUATE: "✓",
        Stage.REFLECT: "↻",
    }

    BINDINGS = [
        ("ctrl+s", "save", "Save"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._raw = persistence.load_raw_config()
        self._defaults: dict[str, Any] = OuroborosConfig().model_dump(mode="json")
        self._installed: dict[str, str | None] = installed_backends()

    # ── value helpers ────────────────────────────────────────────────

    def _current(self, key: str) -> Any:
        value = get_value(self._raw, key)
        if value is None:
            value = get_value(self._defaults, key)
        return value

    def _global_runtime_value(self) -> str:
        """The default agent as currently selected (falling back to config)."""
        try:
            global_value = self.query_one("#global-runtime", Select).value
        except NoMatches:
            global_value = Select.NULL
        if _is_blank(global_value):
            return _canonical_backend(self._current(GLOBAL_RUNTIME_FIELD.key))
        return str(global_value)

    def _runtime_options(self, *, include_inherit: bool) -> list[tuple[str, str]]:
        options: list[tuple[str, str]] = []
        if include_inherit:
            # Show the resolved default inline so "(inherit)" is concrete.
            options.append((f"(inherit — {self._global_runtime_value()})", INHERIT_SENTINEL))
        for name in runtime_backend_choices():
            if self._installed.get(name):
                options.append((name, name))
            else:
                options.append((f"{name} — ⚠ {INSTALL_REQUIRED_SUFFIX}", name))
        return options

    def _model_options(self, backend: str, current: str | None) -> list[tuple[str, str]]:
        try:
            known = list(model_choices(backend))
        except ValueError:
            known = []
        if current and current not in known:
            known.insert(0, current)
        options = [(model, model) for model in known]
        options.append(("Custom…", CUSTOM_SENTINEL))
        return options

    def _effective_stage_backend(self, stage: Stage) -> str:
        stage_value = get_value(self._raw, f"orchestrator.runtime_profile.stages.{stage.value}")
        profile_default = get_value(self._raw, "orchestrator.runtime_profile.default")
        fallback = self._current("orchestrator.runtime_backend")
        return str(stage_value or profile_default or fallback)

    # ── compose ──────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="settings-body"):
            yield Static(
                f"∞  {get_config_dir() / 'config.yaml'} — Ctrl+S to save",
                id="config-path",
            )

            yield Static("Defaults", classes="section-title")
            with Container(id="global-row"):
                with Container(classes="global-cell"):
                    yield from self._compose_select_field(
                        GLOBAL_RUNTIME_FIELD,
                        options=self._runtime_options(include_inherit=False),
                        value=_canonical_backend(self._current(GLOBAL_RUNTIME_FIELD.key)),
                        select_id="global-runtime",
                    )
                    yield Static(
                        "The coding agent that runs your work. Every stage below "
                        "inherits this unless you override it.",
                        classes="field-help",
                    )

            yield Static(
                "Per-stage overrides — interview → execute → evaluate → reflect",
                classes="section-title",
            )
            with Container(id="stage-row"):
                for stage in Stage:
                    yield from self._compose_stage_card(stage)

            with Collapsible(title="Advanced", collapsed=True):
                yield from self._compose_select_field(
                    GLOBAL_LLM_BACKEND_FIELD,
                    options=[(name, name) for name in llm_backend_choices()],
                    value=_canonical_backend(self._current(GLOBAL_LLM_BACKEND_FIELD.key)),
                    select_id="global-llm-backend",
                )
                yield Static(
                    "Engine for Ouroboros' own internal LLM calls (QA verdicts, "
                    "semantic evaluation) — usually the same as the default agent.",
                    classes="field-help",
                )
                for field in ADVANCED_MODEL_FIELDS:
                    yield Static(field.label, classes="field-label")
                    warning = _env_warning_text(field)
                    if warning:
                        yield Static(warning, classes="env-warning")
                    yield Input(
                        value=str(self._current(field.key) or ""),
                        id=f"adv-{_slug(field.key)}",
                    )

            with Container(id="action-bar"):
                yield Button("Save", variant="primary", id="save-button")
                yield Static("", id="status-bar")
        yield Footer()

    def _compose_select_field(
        self,
        field: SettingField,
        *,
        options: list[tuple[str, str]],
        value: str,
        select_id: str,
    ) -> ComposeResult:
        yield Static(field.label, classes="field-label")
        warning = _env_warning_text(field)
        if warning:
            yield Static(warning, classes="env-warning")
        values = {option_value for _, option_value in options}
        yield Select(
            options,
            value=value if value in values else Select.NULL,
            allow_blank=True,
            id=select_id,
        )

    def _compose_stage_card(self, stage: Stage) -> ComposeResult:
        runtime_field = stage_runtime_field(stage)
        model_field = STAGE_MODEL_FIELDS[stage]
        stage_value = get_value(self._raw, runtime_field.key)
        effective_backend = self._effective_stage_backend(stage)
        current_model = str(self._current(model_field.key) or "")

        with Container(classes="stage-card", id=f"stage-card-{stage.value}"):
            yield Static(
                f"{self._STAGE_GLYPHS.get(stage, '·')} {stage.value.title()}",
                classes="stage-title",
            )
            yield Static(runtime_field.label, classes="field-label")
            yield Select(
                self._runtime_options(include_inherit=True),
                value=str(stage_value) if stage_value else INHERIT_SENTINEL,
                allow_blank=False,
                id=f"stage-runtime-{stage.value}",
            )
            yield Static(
                "",
                classes="install-warning hidden",
                id=f"stage-install-warning-{stage.value}",
            )
            yield Static(model_field.label, classes="field-label")
            warning = _env_warning_text(model_field)
            if warning:
                yield Static(warning, classes="env-warning")
            yield Select(
                self._model_options(effective_backend, current_model),
                value=current_model if current_model else Select.NULL,
                allow_blank=True,
                id=f"stage-model-{stage.value}",
            )
            yield Input(
                placeholder="custom model id",
                classes="hidden",
                id=f"stage-model-custom-{stage.value}",
            )

    # ── events ───────────────────────────────────────────────────────

    def on_select_changed(self, event: Select.Changed) -> None:
        # Selects post an initial Changed while the screen is still composing,
        # before later-composed sibling widgets exist. Those events carry no
        # user intent; the NoMatches guard skips them.
        try:
            self._handle_select_changed(event)
        except NoMatches:
            return

    def _handle_select_changed(self, event: Select.Changed) -> None:
        select_id = event.select.id or ""
        if select_id.startswith("stage-runtime-"):
            stage = Stage(select_id.removeprefix("stage-runtime-"))
            self._refresh_stage_model_options(stage)
            self._refresh_install_warning(stage, event.value)
        elif select_id == "global-runtime":
            for stage in Stage:
                runtime_select = self.query_one(f"#stage-runtime-{stage.value}", Select)
                current = runtime_select.value
                # Rebuild so the "(inherit — <agent>)" label tracks the new default.
                runtime_select.set_options(self._runtime_options(include_inherit=True))
                if not _is_blank(current):
                    runtime_select.value = current
                if runtime_select.value == INHERIT_SENTINEL:
                    self._refresh_stage_model_options(stage)
        elif select_id.startswith("stage-model-"):
            stage_name = select_id.removeprefix("stage-model-")
            custom_input = self.query_one(f"#stage-model-custom-{stage_name}", Input)
            custom_input.set_class(event.value != CUSTOM_SENTINEL, "hidden")

    def _selected_runtime(self, stage: Stage) -> str:
        runtime_select = self.query_one(f"#stage-runtime-{stage.value}", Select)
        value = runtime_select.value
        if value == INHERIT_SENTINEL or _is_blank(value):
            global_select = self.query_one("#global-runtime", Select)
            global_value = global_select.value
            if _is_blank(global_value):
                return str(self._current("orchestrator.runtime_backend"))
            return str(global_value)
        return str(value)

    def _refresh_stage_model_options(self, stage: Stage) -> None:
        backend = self._selected_runtime(stage)
        model_select = self.query_one(f"#stage-model-{stage.value}", Select)
        current = model_select.value
        current_str = None if _is_blank(current) else str(current)
        options = self._model_options(backend, current_str)
        model_select.set_options(options)
        if current_str and any(value == current_str for _, value in options):
            model_select.value = current_str

    def _refresh_install_warning(self, stage: Stage, value: Any) -> None:
        warning = self.query_one(f"#stage-install-warning-{stage.value}", Static)
        backend = None if value == INHERIT_SENTINEL or _is_blank(value) else str(value)
        if backend and not self._installed.get(backend):
            warning.update(f"⚠ {backend} CLI not installed — {INSTALL_REQUIRED_SUFFIX}")
            warning.set_class(False, "hidden")
        else:
            warning.set_class(True, "hidden")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-button":
            self.action_save()

    # ── save ─────────────────────────────────────────────────────────

    def _collect_changes(self) -> dict[str, Any]:
        changes: dict[str, Any] = {}

        def record(key: str, new_value: Any) -> None:
            if new_value != get_value(self._raw, key) and not (
                get_value(self._raw, key) is None and new_value == get_value(self._defaults, key)
            ):
                changes[key] = new_value

        global_runtime = self.query_one("#global-runtime", Select).value
        if not _is_blank(global_runtime):
            record(GLOBAL_RUNTIME_FIELD.key, str(global_runtime))
        llm_backend = self.query_one("#global-llm-backend", Select).value
        if not _is_blank(llm_backend):
            record(GLOBAL_LLM_BACKEND_FIELD.key, str(llm_backend))

        for stage in Stage:
            runtime_field = stage_runtime_field(stage)
            runtime_value = self.query_one(f"#stage-runtime-{stage.value}", Select).value
            if runtime_value == INHERIT_SENTINEL:
                if get_value(self._raw, runtime_field.key) is not None:
                    changes[runtime_field.key] = None
            elif not _is_blank(runtime_value):
                record(runtime_field.key, str(runtime_value))

            model_field = STAGE_MODEL_FIELDS[stage]
            model_value = self.query_one(f"#stage-model-{stage.value}", Select).value
            if model_value == CUSTOM_SENTINEL:
                custom = self.query_one(f"#stage-model-custom-{stage.value}", Input).value.strip()
                if custom:
                    record(model_field.key, custom)
            elif not _is_blank(model_value):
                record(model_field.key, str(model_value))

        for field in ADVANCED_MODEL_FIELDS:
            raw_value = self.query_one(f"#adv-{_slug(field.key)}", Input).value.strip()
            if raw_value:
                record(field.key, raw_value)

        return changes

    def action_save(self) -> None:
        status = self.query_one("#status-bar", Static)
        changes = self._collect_changes()
        if not changes:
            status.update("No changes to save.")
            return
        try:
            persistence.apply_config_values(changes)
        except persistence.ConfigWriteError as exc:
            status.update(f"[red]Save failed:[/red] {exc}")
            return
        self._raw = persistence.load_raw_config()
        summary = ", ".join(sorted(changes))
        status.update(f"[green]Saved:[/green] {summary}")


__all__ = ["CUSTOM_SENTINEL", "INHERIT_SENTINEL", "INSTALL_REQUIRED_SUFFIX", "SettingsApp"]
