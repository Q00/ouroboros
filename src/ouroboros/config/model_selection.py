"""Resolve the model each internal role runs on.

Every model setting defaults to ``auto``: the latest model of the role's tier
for the active backend. How a tier becomes a model is a property of the
backend, declared once as ``BackendModelCatalog.model_selection``:

- ``alias`` backends (the Claude CLI family) accept the tier aliases
  ``haiku`` / ``sonnet`` / ``opus``, which the CLI resolves to its newest model.
- ``sentinel`` backends choose their own model; they receive ``"default"``.
- ``explicit`` backends (``litellm``, ``copilot``) take provider-owned ids, so a
  configured value is used as-is and an unset value falls back to the behaviour
  that predates ``auto`` (the shipped pin for litellm, the sentinel for
  copilot). The literal ``auto`` is never forwarded to them.

Persisted concrete ids (config fields, legacy shipped defaults, and the
``OUROBOROS_*_MODEL`` variables) only take effect on alias and sentinel
backends when ``models.pin`` (or ``OUROBOROS_PIN_MODELS``) is on. A
per-invocation model and the new ``models.default`` / ``OUROBOROS_MODEL``
setting are explicit choices and always apply.

Resolution order (first match wins):

1. ``invocation_model``: a tier name resolves through the tier table, ``auto``
   selects the role's tier (explicit backends continue to step 3), and any
   other value is used as given.
2. ``OUROBOROS_MODEL`` / ``models.default`` when not ``auto``, same treatment.
3. Explicit backends: the role's env vars and config fields in order, then the
   backend fallback.
4. Pinned: the role's env vars and config fields in order; an untouched
   shipped default keeps today's backend mapping.
5. Auto: the tier alias on alias backends, ``"default"`` everywhere else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import functools
import logging
import os
from typing import TYPE_CHECKING, Final, Literal, cast, get_args

from ouroboros.config._model_defaults import (
    DEFAULT_CONSENSUS_OPUS_MODEL,
    DEFAULT_HAIKU_MODEL,
    DEFAULT_OPUS_MODEL,
    DEFAULT_SONNET_MODEL,
    recognized_shipped_defaults,
)
from ouroboros.orchestrator_stage import UnknownLLMRoleError, normalize_llm_role

if TYPE_CHECKING:
    from ouroboros.backends.model_catalog import ModelSelection
    from ouroboros.config.models import OuroborosConfig

# Standard logging, not structlog: unconfigured structlog prints to stdout,
# which would corrupt ``config show --json`` and the MCP stdio channel, while an
# unconfigured stdlib logger reports warnings on stderr.
log = logging.getLogger(__name__)

Tier = Literal["frugal", "standard", "frontier"]
ModelSource = Literal["auto", "pin", "invocation", "configured", "shipped_fallback"]

AUTO_MODEL: Final = "auto"
TIERS: Final[tuple[Tier, ...]] = get_args(Tier)
# Kept equal to ``backends.model_catalog.DEFAULT_MODEL_SENTINEL``; importing it
# here at module level would be circular (the catalog imports ``ouroboros.config``).
_SENTINEL: Final = "default"

CLAUDE_TIER_ALIASES: Final[Mapping[Tier, str]] = {
    "frugal": "haiku",
    "standard": "sonnet",
    "frontier": "opus",
}
# Concrete ids an explicit backend receives for a tier request. Only backends in
# ``_SHIPPED_PIN_FALLBACK_BACKENDS`` use them; they cannot receive an alias.
SHIPPED_TIER_MODELS: Final[Mapping[Tier, str]] = {
    "frugal": DEFAULT_HAIKU_MODEL,
    "standard": DEFAULT_SONNET_MODEL,
    "frontier": DEFAULT_OPUS_MODEL,
}
SHIPPED_CONSENSUS_ROSTER: Final[tuple[str, ...]] = (
    "openrouter/openai/gpt-4o",
    DEFAULT_CONSENSUS_OPUS_MODEL,
    "openrouter/google/gemini-2.5-pro",
)
_SHIPPED_PIN_FALLBACK_BACKENDS: Final = frozenset({"litellm"})
_UNSET_VALUES: Final = frozenset({"", AUTO_MODEL, _SENTINEL, "current"})
_TRUE_VALUES: Final = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES: Final = frozenset({"0", "false", "no", "off"})


@dataclass(frozen=True, slots=True)
class _Setting:
    """One persisted model setting: an env var and/or a ``section.field`` path."""

    env_var: str | None
    config_path: tuple[str, str] | None
    shipped: tuple[str, ...] = ()
    # The Execute variable has always treated a set-but-blank value as "let the
    # runtime choose", overriding the config file.
    blank_env_is_automatic: bool = False

    def recognized_shipped(self) -> tuple[str, ...]:
        """Current and historical shipped values, which are never a user's choice."""
        return tuple(value for pin in self.shipped for value in recognized_shipped_defaults(pin))


_SETTINGS: Final[Mapping[str, _Setting]] = {
    "clarification": _Setting(
        "OUROBOROS_CLARIFICATION_MODEL", ("clarification", "default_model"), (DEFAULT_OPUS_MODEL,)
    ),
    "qa": _Setting("OUROBOROS_QA_MODEL", ("llm", "qa_model"), (DEFAULT_SONNET_MODEL,)),
    "assertion_extraction": _Setting(
        "OUROBOROS_ASSERTION_EXTRACTION_MODEL",
        ("evaluation", "assertion_extraction_model"),
        (DEFAULT_SONNET_MODEL,),
    ),
    "detector": _Setting(
        "OUROBOROS_DETECTOR_MODEL",
        ("evaluation", "assertion_extraction_model"),
        (DEFAULT_SONNET_MODEL,),
    ),
    "dependency_analysis": _Setting(
        "OUROBOROS_DEPENDENCY_ANALYSIS_MODEL",
        ("llm", "dependency_analysis_model"),
        (DEFAULT_SONNET_MODEL, DEFAULT_OPUS_MODEL),
    ),
    "ontology_analysis": _Setting(
        "OUROBOROS_ONTOLOGY_ANALYSIS_MODEL",
        ("llm", "ontology_analysis_model"),
        (DEFAULT_SONNET_MODEL, DEFAULT_OPUS_MODEL),
    ),
    "context_compression": _Setting(
        "OUROBOROS_CONTEXT_COMPRESSION_MODEL", ("llm", "context_compression_model"), ("gpt-4",)
    ),
    "wonder": _Setting(
        "OUROBOROS_WONDER_MODEL", ("resilience", "wonder_model"), (DEFAULT_OPUS_MODEL,)
    ),
    "reflect": _Setting(
        "OUROBOROS_REFLECT_MODEL", ("resilience", "reflect_model"), (DEFAULT_OPUS_MODEL,)
    ),
    "semantic": _Setting(
        "OUROBOROS_SEMANTIC_MODEL", ("evaluation", "semantic_model"), (DEFAULT_OPUS_MODEL,)
    ),
    "execution": _Setting(
        "OUROBOROS_EXECUTION_MODEL", ("execution", "default_model"), blank_env_is_automatic=True
    ),
    "validation": _Setting("OUROBOROS_VALIDATION_MODEL", None),
    "consensus_advocate": _Setting(
        "OUROBOROS_CONSENSUS_ADVOCATE_MODEL",
        ("consensus", "advocate_model"),
        (DEFAULT_CONSENSUS_OPUS_MODEL,),
    ),
    "consensus_devil": _Setting(
        "OUROBOROS_CONSENSUS_DEVIL_MODEL",
        ("consensus", "devil_model"),
        (SHIPPED_CONSENSUS_ROSTER[0],),
    ),
    "consensus_judge": _Setting(
        "OUROBOROS_CONSENSUS_JUDGE_MODEL",
        ("consensus", "judge_model"),
        (SHIPPED_CONSENSUS_ROSTER[2],),
    ),
}
_CONSENSUS_ROSTER_ENV: Final = "OUROBOROS_CONSENSUS_MODELS"

# Every role-level model variable, for the untrusted project ``.env`` denylist.
MODEL_ENV_VARS: Final[frozenset[str]] = frozenset(
    {
        "OUROBOROS_MODEL",
        "OUROBOROS_PIN_MODELS",
        _CONSENSUS_ROSTER_ENV,
        *(setting.env_var for setting in _SETTINGS.values() if setting.env_var),
    }
)


@dataclass(frozen=True, slots=True)
class _RoleSpec:
    """A role's tier, its settings (most specific first), and its litellm fallback.

    ``shipped_pin_settings`` are consulted after ``settings`` only on backends
    whose fallback is the shipped pin: an Execute role on litellm has always
    inherited the evaluation model, while every other backend kept its own default.
    """

    tier: Tier
    settings: tuple[str, ...]
    shipped: str
    shipped_pin_settings: tuple[str, ...] = ()


def _specs(roles: tuple[str, ...], spec: _RoleSpec) -> dict[str, _RoleSpec]:
    return dict.fromkeys(roles, spec)


_ROLE_SPECS: Final[Mapping[str, _RoleSpec]] = {
    **_specs(
        (
            "interview",
            "clarification",
            "seed_generation",
            "pm_interview",
            "pm_document",
            "brownfield",
            "brownfield_explore",
            "question_classification",
            "ambiguity",
            "double_diamond",
            "agent_runtime_interview",
        ),
        _RoleSpec("frontier", ("clarification",), DEFAULT_OPUS_MODEL),
    ),
    **_specs(
        (
            "semantic_evaluation",
            "consensus",
            "consensus_perspective",
            "consensus_vote",
            "agent_runtime_evaluation",
        ),
        _RoleSpec("frontier", ("semantic",), DEFAULT_OPUS_MODEL),
    ),
    **{
        role: _RoleSpec("frontier", (role,), _SETTINGS[role].shipped[0])
        for role in ("consensus_advocate", "consensus_devil", "consensus_judge")
    },
    **_specs(("reflect", "lateral"), _RoleSpec("frontier", ("reflect",), DEFAULT_OPUS_MODEL)),
    "wonder": _RoleSpec("frontier", ("wonder", "reflect"), DEFAULT_OPUS_MODEL),
    "context_compression": _RoleSpec(
        "standard", ("context_compression", "reflect"), DEFAULT_OPUS_MODEL
    ),
    "qa": _RoleSpec("standard", ("qa", "semantic"), DEFAULT_OPUS_MODEL),
    "assertion_extraction": _RoleSpec(
        "standard", ("assertion_extraction", "semantic"), DEFAULT_OPUS_MODEL
    ),
    "mechanical_detection": _RoleSpec("standard", ("detector", "semantic"), DEFAULT_OPUS_MODEL),
    "dependency_analysis": _RoleSpec(
        "standard", ("dependency_analysis", "semantic"), DEFAULT_OPUS_MODEL
    ),
    "ontology_analysis": _RoleSpec(
        "standard", ("ontology_analysis", "semantic"), DEFAULT_OPUS_MODEL
    ),
    **_specs(
        ("execute", "atomicity", "decomposition", "agent_runtime_implementation"),
        _RoleSpec("standard", ("execution",), DEFAULT_OPUS_MODEL, ("semantic",)),
    ),
    "validation": _RoleSpec("standard", ("validation", "execution"), DEFAULT_SONNET_MODEL),
    "brownfield_scan": _RoleSpec("frugal", (), DEFAULT_HAIKU_MODEL),
}
ROLE_TIERS: Final[Mapping[str, Tier]] = {role: spec.tier for role, spec in _ROLE_SPECS.items()}


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """The model a role runs on and where that choice came from."""

    role: str
    tier: Tier
    backend: str
    model: str
    source: ModelSource


def canonical_backend(backend: str | None) -> str:
    """Return the canonical backend name, or the normalized input when unknown."""
    from ouroboros.backends import get_backend_capability

    name = (backend or "").strip().lower()
    capability = get_backend_capability(name) if name else None
    return capability.name if capability is not None else name


def backend_model_selection(backend: str | None) -> ModelSelection:
    """How ``backend`` turns a tier into a model; unknown backends get the sentinel."""
    # Deferred: the catalog imports ``ouroboros.config`` at module level.
    from ouroboros.backends.model_catalog import get_model_catalog

    try:
        return get_model_catalog(canonical_backend(backend)).model_selection
    except ValueError:
        return "sentinel"


def tier_model(tier: Tier, backend: str | None) -> str:
    """The model that means "latest of ``tier``" on ``backend``."""
    canonical = canonical_backend(backend)
    selection = backend_model_selection(canonical)
    if selection == "alias":
        return CLAUDE_TIER_ALIASES[tier]
    if canonical in _SHIPPED_PIN_FALLBACK_BACKENDS:
        return SHIPPED_TIER_MODELS[tier]
    return _SENTINEL


def _load_config() -> OuroborosConfig | None:
    from ouroboros.config.loader import load_config
    from ouroboros.core.errors import ConfigError

    try:
        return load_config()
    except ConfigError:
        return None


def pin_models_enabled(config: OuroborosConfig | None = None) -> bool:
    """Whether persisted concrete model ids apply on alias and sentinel backends."""
    env_value = os.environ.get("OUROBOROS_PIN_MODELS", "").strip().lower()
    if env_value in _TRUE_VALUES:
        return True
    if env_value in _FALSE_VALUES:
        return False
    config = config if config is not None else _load_config()
    return config is not None and config.models.pin


def _global_default(config: OuroborosConfig | None) -> str:
    env_value = os.environ.get("OUROBOROS_MODEL", "").strip()
    if env_value:
        return env_value
    return config.models.default.strip() if config is not None else AUTO_MODEL


def _config_value(config: OuroborosConfig | None, path: tuple[str, str] | None) -> object:
    if config is None or path is None:
        return None
    section, field = path
    return getattr(getattr(config, section), field)


@dataclass(frozen=True, slots=True)
class _Persisted:
    """What the persisted settings hold for a role.

    ``shipped_pin`` is the current shipped id of the first setting that still
    holds a (current or historical) shipped default, which is what that
    untouched setting has always resolved to (#1324, #2069).
    """

    value: str | None = None
    setting: str | None = None
    shipped_pin: str | None = None
    automatic: bool = False


def _persisted(setting_keys: tuple[str, ...], config: OuroborosConfig | None) -> _Persisted:
    """Walk ``setting_keys``: the first user-chosen value wins.

    An env var is always a user choice, and one set to an automatic value
    (``auto``/``default``/``current``) stops the walk. A config value that
    equals a current or historical shipped default is not a choice; the walk
    records it and continues.
    """
    shipped_pin: str | None = None
    for key in setting_keys:
        setting = _SETTINGS[key]
        raw_env = os.environ.get(setting.env_var) if setting.env_var is not None else None
        if raw_env is not None:
            env_value = raw_env.strip()
            if env_value not in _UNSET_VALUES:
                return _Persisted(env_value, setting.env_var, shipped_pin)
            if env_value or setting.blank_env_is_automatic:
                return _Persisted(shipped_pin=shipped_pin, automatic=True)
        raw = _config_value(config, setting.config_path)
        value = raw.strip() if isinstance(raw, str) else ""
        if value in _UNSET_VALUES:
            continue
        if value in setting.recognized_shipped():
            shipped_pin = shipped_pin or setting.shipped[0]
            continue
        return _Persisted(value, ".".join(setting.config_path or ()), shipped_pin)
    return _Persisted(shipped_pin=shipped_pin)


@functools.cache
def _warn_ignored_once() -> Callable[[str, str], None]:
    """Return a warner that logs only its first call per process."""
    emitted = False

    def warn(setting: str, value: str) -> None:
        nonlocal emitted
        if emitted:
            return
        emitted = True
        log.warning(
            "Ignoring model %r from %s: models resolve automatically to the latest "
            "model of each tier. Set models.pin: true (or OUROBOROS_PIN_MODELS=1) to "
            "run persisted model ids.",
            value,
            setting,
        )

    return warn


def reset_ignored_model_warning() -> None:
    """Allow the ignored-setting warning to fire again (tests)."""
    _warn_ignored_once.cache_clear()


def _explicit_choice(value: str, spec: _RoleSpec, backend: str) -> str:
    if value in TIERS:
        return tier_model(cast(Tier, value), backend)
    return tier_model(spec.tier, backend) if value == AUTO_MODEL else value


def resolve_role_model(
    role: str,
    *,
    backend: str | None,
    invocation_model: str | None = None,
    pinned: bool | None = None,
) -> ResolvedModel:
    """Resolve the model ``role`` runs on for ``backend`` (see the module docstring).

    Raises:
        UnknownLLMRoleError: If ``role`` has no tier.
    """
    key = normalize_llm_role(role)
    spec = _ROLE_SPECS.get(key)
    if spec is None:
        msg = f"Unknown model role: {role!r}. Valid roles are: {', '.join(sorted(_ROLE_SPECS))}."
        raise UnknownLLMRoleError(msg)
    canonical = canonical_backend(backend)
    selection = backend_model_selection(canonical)

    def resolved(model: str, source: ModelSource) -> ResolvedModel:
        return ResolvedModel(key, spec.tier, canonical, model, source)

    requested = (invocation_model or "").strip()
    if requested and (requested != AUTO_MODEL or selection != "explicit"):
        source: ModelSource = "auto" if requested == AUTO_MODEL else "invocation"
        return resolved(_explicit_choice(requested, spec, canonical), source)

    config = _load_config()
    global_default = _global_default(config)
    if global_default and global_default != AUTO_MODEL:
        return resolved(_explicit_choice(global_default, spec, canonical), "configured")

    if selection == "explicit":
        uses_shipped_pin = canonical in _SHIPPED_PIN_FALLBACK_BACKENDS
        chain = spec.settings + (spec.shipped_pin_settings if uses_shipped_pin else ())
        persisted = _persisted(chain, config)
        if persisted.value is not None:
            return resolved(persisted.value, "configured")
        shipped_pin = None if persisted.automatic else persisted.shipped_pin
        fallback = (shipped_pin or spec.shipped) if uses_shipped_pin else _SENTINEL
        return resolved(fallback, "shipped_fallback")

    persisted = _persisted(spec.settings, config)
    if pinned if pinned is not None else pin_models_enabled(config):
        if persisted.value is not None:
            return resolved(persisted.value, "pin")
        if persisted.shipped_pin is not None and not persisted.automatic:
            return resolved(persisted.shipped_pin if selection == "alias" else _SENTINEL, "pin")
    elif persisted.value is not None and persisted.setting is not None:
        _warn_ignored_once()(persisted.setting, persisted.value)
    return resolved(tier_model(spec.tier, canonical), "auto")


def _roster(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, tuple | list):
        return ()
    roster = tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return () if all(item in _UNSET_VALUES for item in roster) else roster


def resolve_consensus_roster(*, backend: str | None, pinned: bool | None = None) -> tuple[str, ...]:
    """Resolve the stage-3 voting roster with the same precedence as a single role.

    Auto repeats the consensus role's model to the shipped roster length, the
    shape the sentinel collapse has always produced.
    """
    canonical = canonical_backend(backend)
    selection = backend_model_selection(canonical)
    config = _load_config()
    env_roster = _roster(os.environ.get(_CONSENSUS_ROSTER_ENV, ""))
    config_roster = _roster(_config_value(config, ("consensus", "models")))
    shipped = len(config_roster) == len(SHIPPED_CONSENSUS_ROSTER) and all(
        item in recognized_shipped_defaults(pin)
        for item, pin in zip(config_roster, SHIPPED_CONSENSUS_ROSTER, strict=True)
    )
    chosen = env_roster or (() if shipped else config_roster)
    size = len(SHIPPED_CONSENSUS_ROSTER)
    global_default = _global_default(config)
    if global_default and global_default != AUTO_MODEL:
        return (resolve_role_model("consensus", backend=canonical).model,) * size
    if selection == "explicit":
        if chosen:
            return chosen
        if canonical in _SHIPPED_PIN_FALLBACK_BACKENDS:
            return SHIPPED_CONSENSUS_ROSTER
        return (_SENTINEL,) * size
    if pinned if pinned is not None else pin_models_enabled(config):
        if chosen:
            return chosen
        if shipped:
            return SHIPPED_CONSENSUS_ROSTER if selection == "alias" else (_SENTINEL,) * size
    elif chosen:
        _warn_ignored_once()(
            _CONSENSUS_ROSTER_ENV if env_roster else "consensus.models", ",".join(chosen)
        )
    return (tier_model(ROLE_TIERS["consensus"], canonical),) * size


__all__ = [
    "AUTO_MODEL",
    "CLAUDE_TIER_ALIASES",
    "MODEL_ENV_VARS",
    "ROLE_TIERS",
    "SHIPPED_CONSENSUS_ROSTER",
    "SHIPPED_TIER_MODELS",
    "TIERS",
    "ModelSource",
    "ResolvedModel",
    "Tier",
    "backend_model_selection",
    "canonical_backend",
    "pin_models_enabled",
    "reset_ignored_model_warning",
    "resolve_consensus_roster",
    "resolve_role_model",
    "tier_model",
]
