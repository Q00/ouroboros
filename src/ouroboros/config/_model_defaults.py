"""Single source of truth for default Claude model pins.

Every config default (Pydantic field defaults in :mod:`ouroboros.config.models`,
the ``get_*_model`` fallbacks in :mod:`ouroboros.config.loader`, and the setup
wizard tables in :mod:`ouroboros.cli.commands.setup`) references the constants
below. Bumping a pinned model is therefore a one-line edit here instead of
shotgun surgery across three layers. See Q00/ouroboros#1322.

From the Claude 4.6 generation onward, model IDs are dateless but still pinned
snapshots (not evergreen pointers), so pinning remains fully reproducible:
https://platform.claude.com/docs/en/about-claude/models/overview

These pins intentionally do NOT use the ``"default"`` sentinel: the evaluation
and consensus phases depend on a stable model tier for reproducible grading.

Scope: these constants cover the Anthropic-direct API ids (used by the
``claude``/``litellm`` backends) and the OpenRouter consensus roster. The
Copilot setup path selects models from GitHub Copilot's own discovery catalog
(distinct dotted ids surfaced by ``copilot.model_discovery``) and is therefore
out of scope here — it is not driven by these pins.

Note on id formats across providers (they are NOT interchangeable):
- Anthropic direct API uses hyphenated, dateless ids: ``claude-opus-5-5``.
- OpenRouter uses dotted slugs: ``anthropic/claude-opus-5.5``
  (https://openrouter.ai/anthropic/claude-opus-5.5).
"""

# Frontier reasoning tier (interview, seed, ontology, evaluation, execution
# analysis, consensus advocate). Anthropic-direct API id. Bump on each new
# Opus release.
DEFAULT_OPUS_MODEL = "claude-opus-5-5"

# Speed/judgment tier (QA verdicts, assertion extraction). Bump on each new
# Sonnet release.
DEFAULT_SONNET_MODEL = "claude-sonnet-5-5"

# Frugal execution tier for explicitly trusted decomposed children. Child status
# alone stays at the base tier; a deterministic trust issuer must authorize this
# RLM discount while top-level ACs keep the Sonnet default.
# Anthropic-direct API id. Bump on each new Haiku release.
DEFAULT_HAIKU_MODEL = "claude-haiku-5-5"

# OpenRouter-routed Opus for the multi-provider consensus roster. This is the
# OpenRouter slug (dotted ``claude-opus-5.5``), which differs from the
# Anthropic-direct id above — LiteLLM forwards it verbatim to OpenRouter, so it
# must match OpenRouter's published model id exactly or consensus voting fails.
DEFAULT_CONSENSUS_OPUS_MODEL = "openrouter/anthropic/claude-opus-5.5"


# Historical shipped default pins from prior releases, keyed by the *current*
# default that replaced them. A config persisted before a pin was bumped still
# contains the older literal (e.g. ``claude-opus-4-6`` was the frozen Opus
# default from 2026-02-28 until this change; ``claude-sonnet-4-20250514`` was
# the pre-EOL QA default). Backends that cannot run Claude model names
# (Codex/Copilot/Hermes/Kiro) must treat these legacy shipped defaults exactly
# like the current shipped default and normalize them to the ``"default"``
# sentinel — otherwise bumping a pin silently reclassifies an untouched shipped
# default in an already-persisted config as an explicit user override and leaks
# a Claude id to a backend that cannot execute it (Q00/ouroboros#1324 review).
LEGACY_DEFAULT_MODELS: dict[str, tuple[str, ...]] = {
    DEFAULT_OPUS_MODEL: ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-6"),
    DEFAULT_SONNET_MODEL: ("claude-sonnet-4-6", "claude-sonnet-4-20250514"),
    DEFAULT_HAIKU_MODEL: ("claude-haiku-4-5",),
    DEFAULT_CONSENSUS_OPUS_MODEL: (
        "openrouter/anthropic/claude-opus-5",
        "openrouter/anthropic/claude-opus-4.8",
        "openrouter/anthropic/claude-opus-4-6",
    ),
}


# The ``"default"`` sentinel: a tier holding it sends no per-call model
# override, so the runtime runs on its own configured model. Kept equal to
# ``backends.model_catalog.DEFAULT_MODEL_SENTINEL``; importing it here would be
# circular (the catalog imports ``ouroboros.config``).
RUNTIME_DEFAULT_MODEL = "default"

# Claude Code tier aliases. Claude Code resolves each to the newest model of that
# class for whichever provider it runs on (Anthropic, Bedrock, Vertex, a gateway,
# or the ``ANTHROPIC_DEFAULT_*_MODEL`` variables).
CLAUDE_TIER_ALIASES: dict[str, str] = {
    "frugal": "haiku",
    "standard": "sonnet",
    "frontier": "opus",
}

# Gemini CLI model-class aliases (https://geminicli.com/docs/cli/model/). The
# shipped ladder has never had a Google frontier rung.
GEMINI_TIER_ALIASES: dict[str, str] = {
    "frugal": "flash",
    "standard": "pro",
}

# The shipped ``economics.tiers`` ladder per vendor namespace: every runtime runs
# its own default model for the tier rather than an id Ouroboros hard-codes.
# Claude Code and Gemini CLI receive their tier aliases. Codex has no tier
# aliases, so its tiers inherit the model Codex is configured with.
SHIPPED_TIER_LADDER: dict[str, dict[str, str]] = {
    "openai": dict.fromkeys(("frugal", "standard", "frontier"), RUNTIME_DEFAULT_MODEL),
    "anthropic": dict(CLAUDE_TIER_ALIASES),
    "google": dict(GEMINI_TIER_ALIASES),
}

# Every concrete tier id an earlier release shipped in the ladder, by vendor
# namespace and tier. Same rationale as LEGACY_DEFAULT_MODELS
# (Q00/ouroboros#1324): a ``~/.ouroboros/config.yaml`` written by an older
# release carries the shipped ladder verbatim, so an untouched shipped id is
# otherwise indistinguishable from a deliberate override and per-call routing
# would keep forcing an id the runtime or account may not serve. Each one
# resolves to the current shipped value of the same provider and tier.
#
# Verified against git history of ``ouroboros.config.models`` (the tier defaults
# have only ever held these ids): openai ``gpt-4o-mini`` / ``gpt-4o`` / ``o3``,
# then ``gpt-5.1-codex-mini`` / ``gpt-5-codex`` / ``gpt-5.2``, then the Codex
# guide's ``gpt-6-luna`` / ``gpt-6.1-sol`` / ``gpt-6-astra``; anthropic
# ``claude-3-5-haiku`` / ``claude-sonnet-4-20250514`` /
# ``claude-opus-4-5-20251101`` through the 5.5 pins; google
# ``gemini-2.0-flash`` / ``gemini-2.5-pro``.
_SHIPPED_TIER_HISTORY: dict[str, dict[str, tuple[str, ...]]] = {
    "openai": {
        "frugal": ("gpt-4o-mini", "gpt-5.1-codex-mini", "gpt-6-luna"),
        "standard": ("gpt-4o", "gpt-5-codex", "gpt-6.1-sol"),
        "frontier": ("o3", "gpt-5.2", "gpt-6-astra"),
    },
    "anthropic": {
        "frugal": ("claude-3-5-haiku", "claude-haiku-4-5", DEFAULT_HAIKU_MODEL),
        "standard": ("claude-sonnet-4-20250514", "claude-sonnet-4-6", DEFAULT_SONNET_MODEL),
        "frontier": (
            "claude-opus-4-5-20251101",
            "claude-opus-4-6",
            "claude-opus-4-8",
            "claude-opus-5",
            DEFAULT_OPUS_MODEL,
        ),
    },
    "google": {
        "frugal": ("gemini-2.0-flash",),
        "standard": ("gemini-2.5-pro",),
    },
}

# Historical shipped tier id -> the current shipped value of its provider and
# tier. A deliberate override that is byte-for-byte equal to a historical id is
# normalized too: the persisted schema carries no provenance bit that could
# distinguish those cases. Explicit ids that were never shipped (for example a
# proxy-specific model) and every entry under a runtime namespace such as
# ``claude`` or ``codex`` remain untouched.
LEGACY_TIER_MODELS: dict[str, str] = {
    model: SHIPPED_TIER_LADDER[provider][tier]
    for provider, tiers in _SHIPPED_TIER_HISTORY.items()
    for tier, models in tiers.items()
    for model in models
}

# Provider each historical tier default shipped under. The same model-looking
# string under a DIFFERENT provider cannot be an untouched shipped default; it is
# necessarily an explicit/custom routing choice (often a proxy alias) and must be
# preserved verbatim.
LEGACY_TIER_MODEL_PROVIDERS: dict[str, str] = {
    model: provider
    for provider, tiers in _SHIPPED_TIER_HISTORY.items()
    for models in tiers.values()
    for model in models
}

# Tier each historical tier default shipped in. Several Codex ids now resolve to
# the same ``"default"`` sentinel, so resume replay needs the original tier to
# keep refusing a historical id moved to a different tier.
LEGACY_TIER_MODEL_TIERS: dict[str, str] = {
    model: tier
    for tiers in _SHIPPED_TIER_HISTORY.values()
    for tier, models in tiers.items()
    for model in models
}


# Codex model guide, verified 2026-10-18: focused/high-volume work uses Luna,
# complex coding uses Sol, hardest workflows use Astra. Availability depends on
# account, workspace, provider and CLI rollout, so these are only settings-UI
# suggestions (``backends.model_catalog``); the shipped ladder does not route to
# them.
CODEX_TIER_MODELS: dict[str, str] = {
    "frugal": "gpt-6-luna",
    "standard": "gpt-6.1-sol",
    "frontier": "gpt-6-astra",
}


def normalize_tier_model(model: str, *, provider: str | None = None) -> str:
    """Return the current shipped value for ``model`` if it is a historical shipped id.

    A tier model matching a historically shipped default (see
    :data:`LEGACY_TIER_MODELS`) resolves to the current shipped value of its
    provider and tier (a Claude alias, a Gemini alias, or the ``"default"``
    sentinel for Codex), even if the user deliberately typed that same id under
    the same provider: persisted config has no provenance with which to tell
    that override from an untouched old default. When ``provider`` is supplied,
    normalization is limited to the provider the historical value actually
    shipped under; a cross-provider occurrence is necessarily explicit and is
    preserved. Any id outside the historical set, including current shipped
    values and explicit never-shipped choices, is returned verbatim.
    """
    shipped_provider = LEGACY_TIER_MODEL_PROVIDERS.get(model)
    if provider is not None and model in LEGACY_TIER_MODELS and provider != shipped_provider:
        return model
    return LEGACY_TIER_MODELS.get(model, model)


def is_historical_tier_model(model: str, *, provider: str, tier: str) -> bool:
    """Whether ``model`` is an id a release shipped for ``provider``'s ``tier``."""
    return (
        LEGACY_TIER_MODEL_PROVIDERS.get(model) == provider
        and LEGACY_TIER_MODEL_TIERS.get(model) == tier
    )


def recognized_shipped_defaults(default_model: str) -> tuple[str, ...]:
    """Return every shipped-default value (current + historical) for a pin.

    The loader's backend normalization treats any of these as "the shipped
    default the user never chose," so a persisted config from a prior release
    still resolves to the backend-safe ``"default"`` sentinel after a pin bump.
    Genuinely explicit, never-shipped model ids are absent from this set and so
    remain preserved verbatim.
    """
    return (default_model, *LEGACY_DEFAULT_MODELS.get(default_model, ()))
