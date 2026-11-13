"""Settings page model: what the page shows and what a Save writes.

The terminal settings app keeps its save rules inside widget handlers. This
module holds the same rules as plain functions over the raw config dict and
a staged form, so the browser page saves exactly what the terminal app would.
Writes still go through :func:`ouroboros.config_tui.persistence.apply_config_values`
— the one validated, rolled-back, undoable write path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Any

from ouroboros.backends import (
    get_backend_capability,
    resolve_backend_alias,
    runtime_backend_choices,
)
from ouroboros.backends.model_catalog import (
    DEFAULT_MODEL_SENTINEL,
    configured_default_model,
    installed_backends,
    model_choices,
    recommended_cli_models,
    refresh_models,
    uses_default_model_sentinel,
)
from ouroboros.config.model_selection import AUTO_MODEL, TIERS
from ouroboros.config.models import OuroborosConfig, get_config_dir
from ouroboros.config_tui import persistence
from ouroboros.config_tui.fields import (
    GLOBAL_LLM_BACKEND_FIELD,
    GLOBAL_RUNTIME_FIELD,
    MODELS_DEFAULT_FIELD,
    MODELS_PIN_FIELD,
    STAGE_MODEL_FIELDS,
    SettingField,
    active_env_overrides,
    get_value,
    stage_runtime_field,
)
from ouroboros.orchestrator_stage import Stage, resolve_runtime_for_stage

INHERIT = "__inherit__"
PRESET_MODELS: tuple[str, ...] = (AUTO_MODEL, *TIERS)
RECONNECT_PREFIXES = ("orchestrator.runtime", "llm.backend")

STAGE_LABELS: dict[Stage, tuple[str, str]] = {
    Stage.INTERVIEW: ("Interview & Seed", "Asks the questions and writes the spec."),
    Stage.EXECUTE: ("Execute", "Does the work the spec describes."),
    Stage.EVALUATE: ("Evaluate", "Checks the result against the spec."),
    Stage.REFLECT: ("Reflect", "Finds new angles when a run gets stuck."),
}

# Same presets the terminal settings app offers (config_tui/app.py).
PRESET_ROUTES: dict[str, dict[str, str]] = {
    "all-claude": {
        "interview": "claude",
        "execute": "claude",
        "evaluate": "claude",
        "reflect": "claude",
    },
    "claude-verify": {
        "interview": "claude",
        "execute": "claude",
        "evaluate": "antigravity",
        "reflect": "claude",
    },
    "tri-vendor": {
        "interview": "claude",
        "execute": "claude",
        "evaluate": "antigravity",
        "reflect": "grok",
    },
    "codex-core": {
        "interview": "codex",
        "execute": "codex",
        "evaluate": "claude",
        "reflect": "grok",
    },
    "frugal-mix": {
        "interview": "antigravity",
        "execute": "grok",
        "evaluate": "antigravity",
        "reflect": "codex",
    },
}

PRESET_NOTES: dict[str, str] = {
    "all-claude": "One vendor for every stage. Predictable and simple.",
    "claude-verify": "Claude does the work; Gemini (agy) grades it.",
    "tri-vendor": "Three vendors across doing, checking and rethinking.",
    "codex-core": "Codex does the work; Claude grades it.",
    "frugal-mix": "Leans on fast, cheaper models.",
}


def canonical(value: Any) -> str:
    """Canonical backend name; unknown names pass through unchanged."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return resolve_backend_alias(text)
    except ValueError:
        return text.lower()


def _supports_llm(backend: str) -> bool:
    capability = get_backend_capability(backend) if backend else None
    return bool(capability and capability.supports_llm)


@dataclass(slots=True)
class StagedStage:
    """One stage card as the page submits it."""

    runtime: str = INHERIT
    model: str | None = None
    model_explicit: bool = False


@dataclass(slots=True)
class StagedForm:
    """The whole form as the page submits it."""

    default_runtime: str | None = None
    models_default: str = ""
    models_pin: bool | None = None
    stages: dict[Stage, StagedStage] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> StagedForm:
        stages: dict[Stage, StagedStage] = {}
        raw_stages = payload.get("stages") or {}
        if isinstance(raw_stages, dict):
            for stage in Stage:
                item = raw_stages.get(stage.value)
                if not isinstance(item, dict):
                    continue
                runtime = item.get("runtime")
                model = item.get("model")
                stages[stage] = StagedStage(
                    runtime=str(runtime) if runtime else INHERIT,
                    model=str(model).strip() if isinstance(model, str) else None,
                    model_explicit=bool(item.get("model_explicit")),
                )
        default_runtime = payload.get("default_runtime")
        pin = payload.get("models_pin")
        return cls(
            default_runtime=str(default_runtime) if default_runtime else None,
            models_default=str(payload.get("models_default") or "").strip(),
            models_pin=bool(pin) if isinstance(pin, bool) else None,
            stages=stages,
        )


class SettingsModel:
    """Projections over saved config plus a staged form (no I/O after init)."""

    def __init__(self, raw: dict[str, Any] | None = None) -> None:
        self.raw: dict[str, Any] = raw if raw is not None else persistence.load_raw_config()
        self.defaults: dict[str, Any] = OuroborosConfig().model_dump(mode="json")

    # ── saved / effective values ────────────────────────────────────

    def current(self, key: str) -> Any:
        value = get_value(self.raw, key)
        return get_value(self.defaults, key) if value is None else value

    def runtime_env_override(self) -> str | None:
        for name in GLOBAL_RUNTIME_FIELD.env_vars:
            value = os.environ.get(name, "").strip()
            if value:
                return canonical(value)
        return None

    def effective_default_runtime(self) -> str:
        return self.runtime_env_override() or canonical(self.current(GLOBAL_RUNTIME_FIELD.key))

    def _profile_default(self) -> str | None:
        value = get_value(self.raw, "orchestrator.runtime_profile.default")
        return canonical(value) if value else None

    def effective_stage_backend(self, stage: Stage) -> str:
        stage_value = get_value(self.raw, stage_runtime_field(stage).key)
        stages = {stage: canonical(stage_value)} if stage_value else None
        return resolve_runtime_for_stage(
            stage,
            stages=stages,
            default=self._profile_default(),
            fallback=self.effective_default_runtime(),
        )

    def _effective_llm_fallback(self) -> str:
        env_llm = os.environ.get("OUROBOROS_LLM_BACKEND", "").strip()
        if env_llm:
            return canonical(env_llm)
        env_runtime = canonical(os.environ.get("OUROBOROS_RUNTIME", ""))
        if env_runtime and _supports_llm(env_runtime):
            return env_runtime
        raw_llm = get_value(self.raw, GLOBAL_LLM_BACKEND_FIELD.key)
        return canonical(raw_llm) if raw_llm else "claude"

    def _explicit_llm_override(self) -> str | None:
        env_llm = os.environ.get("OUROBOROS_LLM_BACKEND", "").strip()
        if env_llm:
            return canonical(env_llm)
        env_runtime = canonical(os.environ.get("OUROBOROS_RUNTIME", ""))
        if env_runtime and _supports_llm(env_runtime):
            return env_runtime
        raw_llm = get_value(self.raw, GLOBAL_LLM_BACKEND_FIELD.key)
        if raw_llm and str(raw_llm).strip().lower() != "claude_code":
            return canonical(raw_llm)
        return None

    def completion_capable(self, backend: str, *, fallback: str | None = None) -> str:
        runtime_backend = canonical(backend)
        if _supports_llm(runtime_backend):
            return runtime_backend
        if fallback is not None:
            return canonical(fallback)
        return canonical(self.current(GLOBAL_LLM_BACKEND_FIELD.key))

    def effective_completion_backend(self, stage: Stage) -> str:
        if stage is Stage.EXECUTE:
            return self.effective_stage_backend(stage)
        fallback = self._effective_llm_fallback()
        stage_value = get_value(self.raw, stage_runtime_field(stage).key)
        if stage_value:
            return self.completion_capable(str(stage_value), fallback=fallback)
        profile_default = get_value(self.raw, "orchestrator.runtime_profile.default")
        if profile_default:
            return self.completion_capable(str(profile_default), fallback=fallback)
        explicit = self._explicit_llm_override()
        if explicit:
            return explicit
        return self.completion_capable(self.effective_stage_backend(stage), fallback=fallback)

    def saved_stage_backend(self, stage: Stage) -> str:
        stage_value = get_value(self.raw, stage_runtime_field(stage).key)
        stages = {stage: canonical(stage_value)} if stage_value else None
        return resolve_runtime_for_stage(
            stage,
            stages=stages,
            default=self._profile_default(),
            fallback=canonical(self.current(GLOBAL_RUNTIME_FIELD.key)),
        )

    def saved_completion_backend(self, stage: Stage) -> str:
        if stage is Stage.EXECUTE:
            return self.saved_stage_backend(stage)
        stage_value = get_value(self.raw, stage_runtime_field(stage).key)
        if stage_value:
            return self.completion_capable(str(stage_value))
        profile_default = get_value(self.raw, "orchestrator.runtime_profile.default")
        if profile_default:
            return self.completion_capable(str(profile_default))
        raw_llm = get_value(self.raw, GLOBAL_LLM_BACKEND_FIELD.key)
        if raw_llm and str(raw_llm).strip().lower() != "claude_code":
            return canonical(raw_llm)
        return self.completion_capable(self.saved_stage_backend(stage))

    # ── projections of a staged form ────────────────────────────────

    def projected_default_runtime(self, form: StagedForm) -> str:
        if form.default_runtime:
            return canonical(form.default_runtime)
        return canonical(self.current(GLOBAL_RUNTIME_FIELD.key))

    def global_runtime_changed(self, form: StagedForm) -> bool:
        if not form.default_runtime:
            return False
        return canonical(self.current(GLOBAL_RUNTIME_FIELD.key)) != canonical(form.default_runtime)

    def projected_llm_backend(self, form: StagedForm) -> str:
        new_global = self.projected_default_runtime(form)
        if self.global_runtime_changed(form) and _supports_llm(new_global):
            return new_global
        return canonical(self.current(GLOBAL_LLM_BACKEND_FIELD.key))

    def _staged(self, form: StagedForm, stage: Stage) -> StagedStage:
        staged = form.stages.get(stage)
        if staged is not None:
            return staged
        saved = get_value(self.raw, stage_runtime_field(stage).key)
        return StagedStage(runtime=str(saved) if saved else INHERIT)

    def projected_stage_runtime(self, form: StagedForm, stage: Stage) -> str:
        staged = self._staged(form, stage)
        if staged.runtime == INHERIT:
            return resolve_runtime_for_stage(
                stage,
                stages=None,
                default=self._profile_default(),
                fallback=self.projected_default_runtime(form),
            )
        return canonical(staged.runtime)

    def projected_completion_backend(self, form: StagedForm, stage: Stage) -> str:
        if stage is Stage.EXECUTE:
            return self.projected_stage_runtime(form, stage)
        llm_fallback = self.projected_llm_backend(form)
        staged = self._staged(form, stage)
        if staged.runtime != INHERIT:
            return self.completion_capable(staged.runtime, fallback=llm_fallback)
        profile_default = get_value(self.raw, "orchestrator.runtime_profile.default")
        if profile_default:
            return self.completion_capable(str(profile_default), fallback=llm_fallback)
        new_global = self.projected_default_runtime(form)
        if self.global_runtime_changed(form) and _supports_llm(new_global):
            return new_global
        current_llm = get_value(self.raw, GLOBAL_LLM_BACKEND_FIELD.key)
        if current_llm and str(current_llm).strip().lower() != "claude_code":
            return canonical(current_llm)
        return self.completion_capable(new_global, fallback=llm_fallback)

    # ── save planning ────────────────────────────────────────────────

    def plan_changes(self, form: StagedForm) -> dict[str, Any]:
        """Return the dot-key changes a Save would apply (``None`` deletes)."""
        changes: dict[str, Any] = {}

        def record(key: str, new_value: Any) -> None:
            old = get_value(self.raw, key)
            if new_value != old and not (
                old is None and new_value == get_value(self.defaults, key)
            ):
                changes[key] = new_value

        def record_backend(key: str, new_value: str) -> None:
            old = get_value(self.raw, key) or get_value(self.defaults, key)
            if canonical(old) == canonical(new_value):
                return
            record(key, new_value)

        if form.default_runtime:
            record_backend(GLOBAL_RUNTIME_FIELD.key, form.default_runtime)
        if form.models_default:
            record(MODELS_DEFAULT_FIELD.key, form.models_default)
        if form.models_pin is not None:
            record(MODELS_PIN_FIELD.key, form.models_pin)

        for stage in Stage:
            runtime_key = stage_runtime_field(stage).key
            staged = form.stages.get(stage)
            if staged is None:
                continue
            if staged.runtime == INHERIT:
                if get_value(self.raw, runtime_key) is not None:
                    changes[runtime_key] = None
            else:
                record(runtime_key, staged.runtime)

        for stage, model_field in STAGE_MODEL_FIELDS.items():
            self._plan_stage_model(form, stage, model_field, changes, record)

        routing_changed = GLOBAL_RUNTIME_FIELD.key in changes or any(
            key.startswith("orchestrator.runtime_profile.stages.") for key in changes
        )
        if routing_changed:
            execute = self._staged(form, Stage.EXECUTE)
            old_execute = canonical(self.saved_stage_backend(Stage.EXECUTE))
            new_execute = canonical(self.projected_stage_runtime(form, Stage.EXECUTE))
            if (
                old_execute != new_execute
                and get_value(self.raw, "execution.default_model") is not None
                and "execution.default_model" not in changes
                and not execute.model_explicit
            ):
                changes["execution.default_model"] = None
            if GLOBAL_RUNTIME_FIELD.key in changes:
                new_backend = self.projected_default_runtime(form)
                if new_backend and _supports_llm(new_backend):
                    record_backend(GLOBAL_LLM_BACKEND_FIELD.key, new_backend)
        return changes

    def _plan_stage_model(
        self,
        form: StagedForm,
        stage: Stage,
        model_field: SettingField,
        changes: dict[str, Any],
        record: Any,
    ) -> None:
        staged = self._staged(form, stage)
        key = model_field.key
        saved_value = get_value(self.raw, key)
        projected = canonical(self.projected_completion_backend(form, stage))
        backend_changed = canonical(self.saved_completion_backend(stage)) != projected
        if not staged.model_explicit:
            # The model follows the agent: a pin saved for another backend is stale.
            if backend_changed and saved_value is not None:
                changes[key] = None
            return
        model = (staged.model or "").strip()
        if not model:
            if saved_value is not None:
                changes[key] = None
            return
        if model == DEFAULT_MODEL_SENTINEL:
            if stage is Stage.EXECUTE and uses_default_model_sentinel(projected):
                if saved_value is not None:
                    changes[key] = None
                return
            if not uses_default_model_sentinel(projected):
                if saved_value == DEFAULT_MODEL_SENTINEL:
                    changes[key] = None
                return
        record(key, model)

    # ── page payload ─────────────────────────────────────────────────

    def view(self, installed: dict[str, str | None] | None = None) -> dict[str, Any]:
        installed = installed if installed is not None else installed_backends()
        choices = []
        for name in runtime_backend_choices():
            capability = get_backend_capability(name)
            choices.append(
                {
                    "id": name,
                    "installed": bool(installed.get(name)),
                    "cli_path": installed.get(name),
                    "supports_llm": bool(capability and capability.supports_llm),
                }
            )
        stages = []
        for stage in Stage:
            label, hint = STAGE_LABELS[stage]
            model_field = STAGE_MODEL_FIELDS.get(stage)
            completion = canonical(self.effective_completion_backend(stage))
            saved_runtime = get_value(self.raw, stage_runtime_field(stage).key)
            stages.append(
                {
                    "id": stage.value,
                    "label": label,
                    "hint": hint,
                    "runtime_saved": canonical(saved_runtime) if saved_runtime else None,
                    "runtime_effective": canonical(self.effective_stage_backend(stage)),
                    "model_key": model_field.key if model_field else None,
                    "model_label": model_field.label if model_field else None,
                    "model_saved": get_value(self.raw, model_field.key) if model_field else None,
                    "model_env": list(active_env_overrides(model_field)) if model_field else [],
                    "completion_backend": completion,
                    "models": static_models(completion),
                    "default_hint": _default_hint(completion),
                }
            )
        backup = get_config_dir() / "config.yaml.bak"
        return {
            "config_path": str(get_config_dir() / "config.yaml"),
            "runtimes": choices,
            "default_runtime": {
                "saved": canonical(self.current(GLOBAL_RUNTIME_FIELD.key)),
                "effective": self.effective_default_runtime(),
                "env": list(active_env_overrides(GLOBAL_RUNTIME_FIELD)),
            },
            "llm_backend": {
                "saved": canonical(self.current(GLOBAL_LLM_BACKEND_FIELD.key)),
                "effective": self._effective_llm_fallback(),
                "env": list(active_env_overrides(GLOBAL_LLM_BACKEND_FIELD)),
            },
            "models_default": {
                "saved": self.current(MODELS_DEFAULT_FIELD.key),
                "presets": list(PRESET_MODELS),
                "env": list(active_env_overrides(MODELS_DEFAULT_FIELD)),
            },
            "models_pin": {
                "saved": bool(self.current(MODELS_PIN_FIELD.key)),
                "env": list(active_env_overrides(MODELS_PIN_FIELD)),
            },
            "stages": stages,
            "presets": [
                {"id": name, "note": PRESET_NOTES.get(name, ""), "routes": routes}
                for name, routes in PRESET_ROUTES.items()
            ],
            "undo_available": backup.exists(),
        }


def static_models(backend: str) -> list[str]:
    try:
        return list(model_choices(backend))
    except ValueError:
        return []


def _default_hint(backend: str) -> str | None:
    try:
        return configured_default_model(backend) if uses_default_model_sentinel(backend) else None
    except Exception:
        return None


def fetch_models(backend: str) -> list[str]:
    """Static catalog merged with what the backend's CLI lists (may be slow)."""
    models = static_models(backend)
    try:
        fetched = refresh_models(backend)
    except Exception:
        fetched = None
    if fetched:
        for model in recommended_cli_models(backend, fetched):
            if model not in models:
                models.append(model)
    return models


def save(payload: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any]:
    """Plan and (unless ``dry_run``) apply a staged form."""
    model = SettingsModel()
    form = StagedForm.from_json(payload)
    changes = model.plan_changes(form)
    diff = [
        {"key": key, "old": get_value(model.raw, key), "new": value}
        for key, value in sorted(changes.items())
    ]
    result: dict[str, Any] = {
        "changes": diff,
        "reconnect": any(key.startswith(RECONNECT_PREFIXES) for key in changes),
        "saved": False,
    }
    if dry_run or not changes:
        return result
    persistence.apply_config_values(changes)
    result["saved"] = True
    return result


def undo() -> None:
    """Swap config.yaml with its backup (the same contract as ``config undo``)."""
    from ouroboros.config.loader import load_config

    config_path = get_config_dir() / "config.yaml"
    backup_path = get_config_dir() / "config.yaml.bak"
    if not backup_path.exists():
        raise persistence.ConfigWriteError("Nothing to undo: no previous version saved.")
    if not config_path.exists():
        raise persistence.ConfigWriteError(f"Config not found: {config_path}")
    current_bytes = config_path.read_bytes()
    backup_bytes = backup_path.read_bytes()
    try:
        backup_bytes.decode("utf-8")
    except UnicodeError:
        raise persistence.ConfigWriteError("The saved copy is not UTF-8; undo aborted.") from None
    config_path.write_bytes(backup_bytes)
    try:
        load_config()
    except Exception as exc:
        config_path.write_bytes(current_bytes)
        raise persistence.ConfigWriteError(f"The saved copy is not a valid config: {exc}") from exc
    backup_path.write_bytes(current_bytes)


__all__ = [
    "INHERIT",
    "PRESET_ROUTES",
    "SettingsModel",
    "StagedForm",
    "canonical",
    "fetch_models",
    "save",
    "undo",
]
