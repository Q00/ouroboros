"""Textual settings app for Ouroboros configuration (#1413).

Standalone by design: this module imports Textual and the pure helpers in
this package, never :mod:`ouroboros.tui` — the import-isolation contract
that lets ourocode embed the settings screen without the monitor TUI.
"""

from __future__ import annotations

import os
from typing import Any

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    OptionList,
    Select,
    Static,
)
from textual.widgets.option_list import Option

from ouroboros.backends import (
    get_backend_capability,
    resolve_backend_alias,
    runtime_backend_choices,
)
from ouroboros.backends.model_catalog import (
    DEFAULT_MODEL_SENTINEL,
    configured_default_model,
    get_model_catalog,
    installed_backends,
    model_choices,
    refresh_models,
    uses_default_model_sentinel,
)
from ouroboros.config._model_defaults import DEFAULT_OPUS_MODEL, DEFAULT_SONNET_MODEL
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
from ouroboros.orchestrator_stage import Stage, resolve_runtime_for_stage

INHERIT_SENTINEL = "__inherit__"
CUSTOM_SENTINEL = "__custom__"
SEARCH_SENTINEL = "__search__"

INSTALL_REQUIRED_SUFFIX = "install required"

# Above this many fetched models, the select offers a search modal instead
# of inlining the whole listing (opencode reports ~400 ids).
SEARCH_THRESHOLD = 20

# One-click model presets: per-backend picks, falling back to the backend's
# catalog default where no differentiated tier exists (sentinel backends).
PRESET_MODELS: dict[str, dict[str, str]] = {
    "frugal": {"claude": "claude-haiku-4-5-20251001", "codex": "gpt-5-mini"},
    "balanced": {"claude": DEFAULT_SONNET_MODEL, "codex": "gpt-5"},
    "frontier": {"claude": DEFAULT_OPUS_MODEL, "codex": "gpt-5-codex"},
}

# One-click multi-LLM stage-routing presets: per-stage runtime backend picks
# for the four pipeline stages (interview → execute → evaluate → reflect). Each
# preset stages the four stage Agent selects; review and Save to persist into
# orchestrator.runtime_profile.stages. The spine of these recommendations is
# vendor diversity between the *generate* stage (execute) and the *verify*
# stage (evaluate) — a different vendor grading the work reduces correlated
# blind spots (the same principle ouroboros already encodes in
# consensus.diversity_required). A preset may reference a backend whose CLI is
# not installed; staging still works and the per-card install warning flags it.
PRESET_ROUTES: dict[str, dict[str, str]] = {
    # Single-vendor baseline — one subscription, predictable behavior.
    "all-claude": {
        "interview": "claude",
        "execute": "claude",
        "evaluate": "claude",
        "reflect": "claude",
    },
    # Cross-vendor verification: Claude generates, Antigravity (Gemini) verifies.
    # The highest-value diversity change — an independent vendor grades the work.
    "claude-verify": {
        "interview": "claude",
        "execute": "claude",
        "evaluate": "antigravity",
        "reflect": "claude",
    },
    # Maximum diversity across generate / verify / diverge (three vendors).
    "tri-vendor": {
        "interview": "claude",
        "execute": "claude",
        "evaluate": "antigravity",
        "reflect": "grok",
    },
    # OpenAI-centric execution with a cross-vendor verify gate.
    "codex-core": {
        "interview": "codex",
        "execute": "codex",
        "evaluate": "claude",
        "reflect": "grok",
    },
    # Cost/speed-leaning mix (Gemini Flash via agy, fast Grok for execution).
    "frugal-mix": {
        "interview": "antigravity",
        "execute": "grok",
        "evaluate": "antigravity",
        "reflect": "codex",
    },
}

# Saving these keys only takes effect for a running MCP server after a
# reconnect (#1376 friction log) — surface that instead of failing silently.
_RECONNECT_KEY_PREFIXES = ("orchestrator.runtime", "llm.backend")

# Cap the rows rendered in the search modal while filtering.
_SEARCH_MAX_ROWS = 200

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


class ModelSearchScreen(ModalScreen[str | None]):
    """Type-to-filter picker for large model listings."""

    BINDINGS = [("escape", "cancel", "Close")]

    CSS = """
    ModelSearchScreen { align: center middle; }
    #search-dialog {
        width: 70%;
        max-width: 90;
        height: 70%;
        border: round $accent;
        background: $surface;
        padding: 1 2;
    }
    #search-title { text-style: bold; color: $accent; }
    #search-input { margin: 1 0; }
    #search-results { height: 1fr; }
    """

    def __init__(self, models: tuple[str, ...], *, title: str) -> None:
        super().__init__()
        self._models = models
        self._title = title

    def compose(self) -> ComposeResult:
        with Container(id="search-dialog"):
            yield Static(self._title, id="search-title")
            yield Input(placeholder="Type to filter…", id="search-input")
            yield OptionList(id="search-results")

    def on_mount(self) -> None:
        self._apply_filter("")
        self.query_one("#search-input", Input).focus()

    def _apply_filter(self, needle: str) -> None:
        needle = needle.strip().lower()
        matches = [model for model in self._models if needle in model.lower()]
        results = self.query_one("#search-results", OptionList)
        results.clear_options()
        results.add_options(Option(model, id=None) for model in matches[:_SEARCH_MAX_ROWS])
        overflow = len(matches) - _SEARCH_MAX_ROWS
        if overflow > 0:
            results.add_options(
                [Option(f"… {overflow} more — keep typing to narrow down", disabled=True)]
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        if (event.input.id or "") == "search-input":
            self._apply_filter(event.value)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option.prompt))

    def action_cancel(self) -> None:
        self.dismiss(None)


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

    #preset-row { layout: horizontal; height: auto; margin: 1 0 0 0; }
    #preset-row Button { margin: 0 1 0 0; min-width: 14; }
    #preset-help { margin: 1 0 0 1; width: 1fr; }
    #routing-preset-row { layout: horizontal; height: auto; margin: 1 0 0 0; }
    #routing-preset-row Button { margin: 0 1 0 0; min-width: 14; }
    #routing-preset-help { margin: 1 0 0 1; width: 1fr; }

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
        # Dynamic model listings fetched from backend CLIs (None = attempted,
        # nothing usable). Fetches run in background threads so the UI never
        # blocks on a slow CLI.
        self._fetched_models: dict[str, tuple[str, ...] | None] = {}
        self._fetch_pending: set[str] = set()
        # Last concrete model per stage, restored when a search is cancelled.
        self._last_model_value: dict[str, str | None] = {}
        # Backend whose catalog populated each stage model Select.  A refresh
        # for the *same* backend must retain an arbitrary persisted pin even
        # when it is absent from the shipped/discovered catalog; changing the
        # backend is the point at which that pin becomes incompatible.
        self._stage_model_backends: dict[Stage, str] = {}
        # Effective model values rendered from active environment overrides.
        # They are display state, not user edits: an unrelated Save must never
        # copy a temporary override into config.yaml.
        self._env_display_models: dict[Stage, str] = {}
        # What the "default" sentinel resolves to per backend (config-file
        # hint, e.g. hermes → gpt-5.5). Cached: file reads once per backend.
        self._default_hints: dict[str, str | None] = {}
        # Hidden legacy fallback sync for llm.backend after its visible field
        # was removed from the TUI. The most recent Agent selection wins.
        self._last_agent_backend_selection: str | None = None

    def on_mount(self) -> None:
        # Config-derived (not widget-derived): widgets may still be mounting.
        for backend in {self._effective_stage_backend(stage) for stage in Stage}:
            self._request_model_listing(backend)

    # ── value helpers ────────────────────────────────────────────────

    def _current(self, key: str) -> Any:
        value = get_value(self._raw, key)
        if value is None:
            value = get_value(self._defaults, key)
        return value

    @staticmethod
    def _env_model_override(field: SettingField) -> str | None:
        """Return an active model override, preserving Execute's empty clear.

        ``None`` means no environment value is in effect.  The empty string is
        meaningful only for Execute, where it deliberately masks a saved pin.
        """
        for name in field.env_vars:
            if name not in os.environ:
                continue
            value = os.environ[name].strip()
            if value or field.empty_env_overrides:
                return value
        return None

    def _runtime_options(self, *, include_inherit: bool) -> list[tuple[str, str]]:
        # Option labels must stay static: Textual's Select does not re-render
        # the selected label after set_options when the value is unchanged, so
        # dynamic text (the resolved default) lives in the per-card caption
        # (#stage-resolved-*) instead.
        options: list[tuple[str, str]] = []
        if include_inherit:
            options.append(("(inherit default)", INHERIT_SENTINEL))
        for name in runtime_backend_choices():
            if self._installed.get(name):
                options.append((name, name))
            else:
                options.append((f"{name} — ⚠ {INSTALL_REQUIRED_SUFFIX}", name))
        return options

    def _static_models(self, backend: str) -> list[str]:
        try:
            return list(model_choices(backend))
        except ValueError:
            return []

    def _all_models(self, backend: str) -> list[str]:
        """Static catalog merged with the full CLI-fetched listing."""
        known = self._static_models(backend)
        fetched = self._fetched_models.get(backend) or ()
        known.extend(model for model in fetched if model not in known)
        return known

    def _model_options(
        self,
        backend: str,
        current: str | None,
        *,
        include_automatic: bool = False,
    ) -> list[tuple[str, str]]:
        """Select options for a backend.

        Small fetched listings merge inline; large ones (e.g. opencode's
        ~400 ids) stay behind a "Search…" entry that opens a filter modal,
        keeping the dropdown scannable.
        """
        fetched = self._fetched_models.get(backend) or ()
        if len(fetched) <= SEARCH_THRESHOLD:
            known = self._all_models(backend)
        else:
            known = self._static_models(backend)
        if include_automatic and DEFAULT_MODEL_SENTINEL not in known:
            known.insert(0, DEFAULT_MODEL_SENTINEL)
        if current and current not in known:
            known.insert(0, current)
        options = [(self._model_label(backend, model), model) for model in known]
        if len(fetched) > SEARCH_THRESHOLD:
            options.append((f"Search {len(fetched)} models…", SEARCH_SENTINEL))
        options.append(("Enter another model ID…", CUSTOM_SENTINEL))
        return options

    def _model_label(self, backend: str, model: str) -> str:
        """Describe the runtime-owned default in terms of the model it selects."""
        if model != DEFAULT_MODEL_SENTINEL:
            return model
        if _canonical_backend(backend) == "codex":
            # The Codex App/CLI may select a model independently of the
            # config file. A config.toml value is only a hint, not evidence
            # that this invocation will use it.
            if backend not in self._default_hints:
                self._default_hints[backend] = configured_default_model(backend)
            hint = self._default_hints[backend]
            if hint:
                return (
                    "Follow Codex's currently selected model "
                    f"— config.toml: {hint} (not confirmed at runtime)"
                )
            return "Follow Codex's currently selected model — concrete model not reported by Codex"
        if backend not in self._default_hints:
            self._default_hints[backend] = configured_default_model(backend)
        hint = self._default_hints[backend]
        prefix = "Use runtime default model"
        return f"{prefix} — configured: {hint}" if hint else prefix

    def _runtime_env_override(self) -> str | None:
        for name in GLOBAL_RUNTIME_FIELD.env_vars:
            value = os.environ.get(name, "").strip()
            if value:
                return _canonical_backend(value)
        return None

    def _effective_default_runtime(self) -> str:
        return self._runtime_env_override() or _canonical_backend(
            self._current(GLOBAL_RUNTIME_FIELD.key)
        )

    def _selected_default_runtime(self) -> str:
        override = self._runtime_env_override()
        if override:
            return override
        global_select = self.query_one("#global-runtime", Select)
        global_value = global_select.value
        if _is_blank(global_value):
            return self._effective_default_runtime()
        return _canonical_backend(global_value)

    # ── dynamic model listings ───────────────────────────────────────

    def _request_model_listing(self, backend: str) -> None:
        """Fetch the backend CLI's model listing once, in the background."""
        if backend in self._fetched_models or backend in self._fetch_pending:
            return
        self._fetch_pending.add(backend)
        self._fetch_models_worker(backend)

    @work(thread=True, exclusive=False)
    def _fetch_models_worker(self, backend: str) -> None:
        models = refresh_models(backend)
        self.call_from_thread(self._on_models_fetched, backend, models)

    def _on_models_fetched(self, backend: str, models: tuple[str, ...] | None) -> None:
        self._fetch_pending.discard(backend)
        self._fetched_models[backend] = models
        if not models:
            return
        for stage in Stage:
            try:
                if self._selected_runtime(stage) == backend:
                    self._merge_fetched_into_stage(stage, backend)
            except NoMatches:
                continue

    def _merge_fetched_into_stage(self, stage: Stage, backend: str) -> None:
        model_select = self.query_one(f"#stage-model-{stage.value}", Select)
        current = model_select.value
        current_str = None if _is_blank(current) else str(current)
        model_select.set_options(self._model_options(backend, current_str))
        if current_str:
            model_select.value = current_str

    def _effective_stage_backend(self, stage: Stage) -> str:
        stage_value = get_value(self._raw, f"orchestrator.runtime_profile.stages.{stage.value}")
        profile_default = get_value(self._raw, "orchestrator.runtime_profile.default")
        stages = {stage: _canonical_backend(stage_value)} if stage_value else None
        default = _canonical_backend(profile_default) if profile_default else None
        return resolve_runtime_for_stage(
            stage,
            stages=stages,
            default=default,
            fallback=self._effective_default_runtime(),
        )

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
            with Container(id="preset-row"):
                yield Button("⚡ Frugal", id="preset-frugal")
                yield Button("⚖ Balanced", id="preset-balanced")
                yield Button("🚀 Frontier", id="preset-frontier")
                yield Static(
                    "one-click models for every stage — review, then Save",
                    classes="field-help",
                    id="preset-help",
                )
            with Container(id="routing-preset-row"):
                yield Button("All Claude", id="route-all-claude")
                yield Button("Claude+Verify", id="route-claude-verify")
                yield Button("Tri-Vendor", id="route-tri-vendor")
                yield Button("Codex Core", id="route-codex-core")
                yield Button("Frugal Mix", id="route-frugal-mix")
                yield Static(
                    "one-click multi-LLM routing (which agent runs each stage) — review, then Save",
                    classes="field-help",
                    id="routing-preset-help",
                )
            with Container(id="stage-row"):
                for stage in Stage:
                    yield from self._compose_stage_card(stage)

            if ADVANCED_MODEL_FIELDS:
                with Collapsible(title="Advanced", collapsed=True):
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
        model_field = STAGE_MODEL_FIELDS.get(stage)
        stage_value = get_value(self._raw, runtime_field.key)
        effective_backend = self._effective_stage_backend(stage)
        current_model = str(self._current(model_field.key) or "") if model_field else ""
        # ``OUROBOROS_EXECUTION_MODEL=`` deliberately clears a saved pin.
        # Render the effective runtime-owned choice instead of displaying the
        # dormant persisted value as though it will be used in this session.
        env_override = self._env_model_override(model_field) if model_field is not None else None
        if env_override is not None:
            current_model = env_override
        # Execute has no shipped model pin: display the implicit behavior as an
        # explicit default-model choice without persisting anything merely because
        # the user saves an unrelated setting.
        if stage is Stage.EXECUTE and not current_model:
            current_model = DEFAULT_MODEL_SENTINEL

        if model_field is not None:
            self._stage_model_backends[stage] = effective_backend
            if env_override is not None:
                self._env_display_models[stage] = current_model

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
                f"→ runs on {effective_backend}",
                classes="field-help",
                id=f"stage-resolved-{stage.value}",
            )
            yield Static(
                "",
                classes="install-warning hidden",
                id=f"stage-install-warning-{stage.value}",
            )
            if model_field is not None:
                yield Static(model_field.label, classes="field-label")
                warning = _env_warning_text(model_field)
                if warning:
                    yield Static(warning, classes="env-warning")
                yield Select(
                    self._model_options(
                        effective_backend,
                        current_model,
                        # Every Execute runtime can inherit its own default;
                        # the other stages retain backend-specific catalogs.
                        include_automatic=stage is Stage.EXECUTE,
                    ),
                    value=current_model if current_model else Select.NULL,
                    allow_blank=True,
                    id=f"stage-model-{stage.value}",
                )
                yield Input(
                    placeholder="e.g. terra",
                    classes="hidden",
                    id=f"stage-model-custom-{stage.value}",
                )
                yield Static(
                    "Choose ‘Enter another model ID…’ to pin a model not listed here.",
                    classes="field-help",
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
            self._update_resolved_caption(stage)
            self._remember_agent_selection(self._selected_runtime(stage))
            if stage in STAGE_MODEL_FIELDS:
                self._refresh_stage_model_options(stage)
            self._refresh_install_warning(stage, event.value)
        elif select_id == "global-runtime":
            self._remember_agent_selection(self._selected_default_runtime())
            # Cascade: every inheriting card re-resolves its agent and pulls
            # the matching model catalog. Guard per card so one failure
            # cannot skip the rest.
            for stage in Stage:
                try:
                    self._sync_stage_card(stage)
                except NoMatches:
                    continue
        elif select_id.startswith("stage-model-") and not select_id.startswith(
            "stage-model-custom-"
        ):
            stage = Stage(select_id.removeprefix("stage-model-"))
            custom_input = self.query_one(f"#stage-model-custom-{stage.value}", Input)
            custom_input.set_class(event.value != CUSTOM_SENTINEL, "hidden")
            if event.value == SEARCH_SENTINEL:
                self._open_model_search(stage)
            elif not _is_blank(event.value) and event.value != CUSTOM_SENTINEL:
                self._last_model_value[stage.value] = str(event.value)

    def _open_model_search(self, stage: Stage) -> None:
        backend = self._selected_runtime(stage)
        models = tuple(self._all_models(backend))

        def _picked(model: str | None) -> None:
            previous = self._last_model_value.get(stage.value)
            self._set_stage_model(stage, model or previous)

        self.push_screen(
            ModelSearchScreen(models, title=f"{stage.value.title()} model — {backend}"),
            _picked,
        )

    def _set_stage_model(self, stage: Stage, model: str | None) -> None:
        if stage not in STAGE_MODEL_FIELDS:
            return
        backend = self._selected_runtime(stage)
        model_select = self.query_one(f"#stage-model-{stage.value}", Select)
        model_select.set_options(
            self._model_options(backend, model, include_automatic=stage is Stage.EXECUTE)
        )
        if model:
            model_select.value = model

    def _sync_stage_card(self, stage: Stage) -> None:
        runtime_select = self.query_one(f"#stage-runtime-{stage.value}", Select)
        self._update_resolved_caption(stage)
        if stage in STAGE_MODEL_FIELDS and runtime_select.value == INHERIT_SENTINEL:
            self._refresh_stage_model_options(stage)

    def _update_resolved_caption(self, stage: Stage) -> None:
        caption = self.query_one(f"#stage-resolved-{stage.value}", Static)
        caption.update(f"→ runs on {self._selected_runtime(stage)}")

    def _remember_agent_selection(self, backend: str) -> None:
        self._last_agent_backend_selection = _canonical_backend(backend)

    def _selected_runtime(self, stage: Stage) -> str:
        runtime_select = self.query_one(f"#stage-runtime-{stage.value}", Select)
        value = runtime_select.value
        if value == INHERIT_SENTINEL or _is_blank(value):
            profile_default = get_value(self._raw, "orchestrator.runtime_profile.default")
            default = _canonical_backend(profile_default) if profile_default else None
            return resolve_runtime_for_stage(
                stage,
                stages=None,
                default=default,
                fallback=self._selected_default_runtime(),
            )
        return _canonical_backend(value)

    def _refresh_stage_model_options(self, stage: Stage) -> None:
        """Repopulate the model select with the effective backend's catalog.

        The value *follows* the backend: a model the new backend cannot run
        is replaced by that backend's catalog default rather than carried
        over (the user changed the agent; the old model id is stale).
        """
        if stage not in STAGE_MODEL_FIELDS:
            return
        backend = self._selected_runtime(stage)
        self._request_model_listing(backend)
        model_select = self.query_one(f"#stage-model-{stage.value}", Select)
        current = model_select.value
        current_str = None if _is_blank(current) else str(current)
        previous_backend = self._stage_model_backends.get(stage)
        # Keep a non-catalog value while refreshing the same backend.  This is
        # how a saved pin such as ``terra`` survives reopening the settings UI
        # before a provider happens to advertise it.  A real agent change still
        # resets an incompatible pin to the new backend's default.
        keep = (
            current_str
            if current_str
            and (current_str in self._all_models(backend) or previous_backend == backend)
            else None
        )
        options = self._model_options(backend, keep, include_automatic=stage is Stage.EXECUTE)
        model_select.set_options(options)
        self._stage_model_backends[stage] = backend
        concrete = [v for _, v in options if v not in (SEARCH_SENTINEL, CUSTOM_SENTINEL)]
        if keep:
            model_select.value = keep
        elif concrete:
            model_select.value = concrete[0]
        # Custom-only backends (no known models) stay blank for free text.

    def _refresh_install_warning(self, stage: Stage, value: Any) -> None:
        warning = self.query_one(f"#stage-install-warning-{stage.value}", Static)
        backend = None if value == INHERIT_SENTINEL or _is_blank(value) else str(value)
        if backend and not self._installed.get(backend):
            warning.update(f"⚠ {backend} CLI not installed — {INSTALL_REQUIRED_SUFFIX}")
            warning.set_class(False, "hidden")
        else:
            warning.set_class(True, "hidden")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "save-button":
            self.action_save()
        elif button_id.startswith("route-"):
            self._apply_routing_preset(button_id.removeprefix("route-"))
        elif button_id.startswith("preset-"):
            self._apply_preset(button_id.removeprefix("preset-"))

    def _apply_preset(self, level: str) -> None:
        """Stage a one-click model preset across all stage cards (not saved yet)."""
        picks = PRESET_MODELS.get(level)
        if picks is None:
            return
        for stage in Stage:
            if stage not in STAGE_MODEL_FIELDS:
                continue
            try:
                backend = self._selected_runtime(stage)
                model = picks.get(backend)
                if model is None:
                    model = get_model_catalog(backend).default_model
                self._set_stage_model(stage, model)
            except (NoMatches, ValueError):
                continue
        status = self.query_one("#status-bar", Static)
        status.update(f"Preset [bold]{level}[/bold] staged — review the cards, then Save.")

    def _set_stage_runtime(self, stage: Stage, backend: str) -> None:
        """Stage a per-stage runtime backend selection and cascade its card.

        Mirrors the `stage-runtime-*` Select.Changed handling so the resolved
        caption, model options, and install warning stay consistent without
        relying on event ordering during a preset apply.
        """
        runtime_select = self.query_one(f"#stage-runtime-{stage.value}", Select)
        runtime_select.value = backend
        self._update_resolved_caption(stage)
        self._remember_agent_selection(self._selected_runtime(stage))
        self._refresh_install_warning(stage, backend)
        if stage in STAGE_MODEL_FIELDS:
            self._refresh_stage_model_options(stage)

    def _apply_routing_preset(self, name: str) -> None:
        """Stage a one-click multi-LLM routing preset across all stage cards."""
        routes = PRESET_ROUTES.get(name)
        if routes is None:
            return
        for stage in Stage:
            backend = routes.get(stage.value)
            if backend is None:
                continue
            try:
                self._set_stage_runtime(stage, backend)
            except (NoMatches, ValueError):
                continue
        status = self.query_one("#status-bar", Static)
        status.update(f"Routing preset [bold]{name}[/bold] staged — review the cards, then Save.")

    # ── save ─────────────────────────────────────────────────────────

    def _collect_changes(self) -> dict[str, Any]:
        changes: dict[str, Any] = {}

        def record(key: str, new_value: Any) -> None:
            if new_value != get_value(self._raw, key) and not (
                get_value(self._raw, key) is None and new_value == get_value(self._defaults, key)
            ):
                changes[key] = new_value

        def record_backend(key: str, new_value: str) -> None:
            old = get_value(self._raw, key) or get_value(self._defaults, key)
            # claude_code → claude is the same backend; alias-only diffs are noise.
            if _canonical_backend(old) == _canonical_backend(new_value):
                return
            record(key, new_value)

        global_runtime = self.query_one("#global-runtime", Select).value
        if not _is_blank(global_runtime):
            record_backend(GLOBAL_RUNTIME_FIELD.key, str(global_runtime))

        for stage in Stage:
            runtime_field = stage_runtime_field(stage)
            runtime_value = self.query_one(f"#stage-runtime-{stage.value}", Select).value
            if runtime_value == INHERIT_SENTINEL:
                if get_value(self._raw, runtime_field.key) is not None:
                    changes[runtime_field.key] = None
            elif not _is_blank(runtime_value):
                record(runtime_field.key, str(runtime_value))

            model_field = STAGE_MODEL_FIELDS.get(stage)
            if model_field is not None:
                model_value = self.query_one(f"#stage-model-{stage.value}", Select).value
                if model_value == CUSTOM_SENTINEL:
                    custom = self.query_one(
                        f"#stage-model-custom-{stage.value}", Input
                    ).value.strip()
                    if custom:
                        record(model_field.key, custom)
                elif not _is_blank(model_value):
                    model_text = str(model_value)
                    # A non-empty environment pin is rendered into the Select
                    # so people can see what this process will use.  It is not
                    # a config edit, though: preserve the saved pin whenever
                    # the user saves an unrelated setting.  (The same guard
                    # also covers Execute's intentional empty override, whose
                    # displayed value is the automatic sentinel.)
                    if self._env_display_models.get(stage) == model_text:
                        continue
                    if stage is Stage.EXECUTE and model_text == DEFAULT_MODEL_SENTINEL:
                        # A blank ``OUROBOROS_EXECUTION_MODEL`` masks the saved
                        # pin.  Saving an unrelated UI change must not turn
                        # that temporary effective state into a destructive
                        # persisted clear.
                        if model_field.empty_env_overrides and any(
                            name in os.environ for name in model_field.env_vars
                        ):
                            continue
                        if get_value(self._raw, model_field.key) is not None:
                            changes[model_field.key] = None
                        continue
                    if model_text == DEFAULT_MODEL_SENTINEL and not uses_default_model_sentinel(
                        self._selected_runtime(stage)
                    ):
                        if get_value(self._raw, model_field.key) == DEFAULT_MODEL_SENTINEL:
                            changes[model_field.key] = None
                        continue
                    record(model_field.key, model_text)

        for field in ADVANCED_MODEL_FIELDS:
            raw_value = self.query_one(f"#adv-{_slug(field.key)}", Input).value.strip()
            if raw_value:
                record(field.key, raw_value)

        # Sync the hidden legacy llm.backend fallback ONLY when this save already
        # changes backend routing (the default Agent or any stage Agent). On
        # unrelated saves (e.g. editing only a model field) leave it untouched so
        # an existing user-managed llm.backend is preserved, not clobbered.
        routing_changed = GLOBAL_RUNTIME_FIELD.key in changes or any(
            key.startswith("orchestrator.runtime_profile.stages.") for key in changes
        )
        if routing_changed:
            new_backend = self._last_agent_backend_selection or self._selected_default_runtime()
            # Only sync the legacy llm.backend (a completion backend) when the
            # selected agent is itself completion-capable. Runtime-only backends
            # (antigravity / grok, supports_llm=False) are not valid llm.backend
            # values, so leave the existing completion backend untouched rather
            # than persisting a config that fails validation on next load.
            capability = get_backend_capability(new_backend) if new_backend else None
            if new_backend and capability is not None and capability.supports_llm:
                record_backend(GLOBAL_LLM_BACKEND_FIELD.key, new_backend)

        return changes

    def action_save(self) -> None:
        status = self.query_one("#status-bar", Static)
        changes = self._collect_changes()
        if not changes:
            status.update("No changes to save.")
            return
        old_values = {key: get_value(self._raw, key) for key in changes}
        try:
            persistence.apply_config_values(changes)
        except persistence.ConfigWriteError as exc:
            status.update(f"[red]Save failed:[/red] {exc}")
            return
        self._raw = persistence.load_raw_config()
        status.update(self._save_summary(changes, old_values))

    @staticmethod
    def _save_summary(changes: dict[str, Any], old_values: dict[str, Any]) -> str:
        """Render what changed (old → new) and flag keys needing an MCP reconnect."""
        diffs = []
        ordered = sorted(changes, key=lambda k: (not k.startswith(_RECONNECT_KEY_PREFIXES), k))
        for key in ordered:
            old = old_values.get(key)
            old_text = "unset" if old is None else str(old)
            new_text = "unset" if changes[key] is None else str(changes[key])
            short_key = key.rsplit(".", 1)[-1] if key.count(".") > 1 else key
            diffs.append(f"{short_key}: {old_text} → {new_text}")
        shown = "; ".join(diffs[:4])
        if len(diffs) > 4:
            shown += f"; … +{len(diffs) - 4} more"
        summary = f"[green]Saved.[/green] {shown}"
        if any(key.startswith(_RECONNECT_KEY_PREFIXES) for key in changes):
            summary += (
                "\n[yellow]⚠ backend changes apply to new sessions — "
                "reconnect the MCP server to pick them up now.[/yellow]"
            )
        return summary


__all__ = ["CUSTOM_SENTINEL", "INHERIT_SENTINEL", "INSTALL_REQUIRED_SUFFIX", "SettingsApp"]
