"""Single source of truth for default Claude and Codex model pins.

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
- Anthropic direct API uses hyphenated, dateless ids: ``claude-opus-4-8``.
- OpenRouter uses dotted slugs: ``anthropic/claude-opus-4.8``
  (https://openrouter.ai/anthropic/claude-opus-4.8).
"""

# Frontier reasoning tier (interview, seed, ontology, evaluation, execution
# analysis, consensus advocate). Anthropic-direct API id. Bump on each new
# Opus release.
DEFAULT_OPUS_MODEL = "claude-opus-4-8"

# Speed/judgment tier (QA verdicts, assertion extraction). Bump on each new
# Sonnet release.
DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"

# Frugal execution tier for decomposed children (model-tier routing): the RLM
# thesis says a verified-MECE child is easy enough to run on the cheapest model,
# so decomposed leaves route here while top-level ACs keep the Sonnet default.
# Anthropic-direct API id. Bump on each new Haiku release.
DEFAULT_HAIKU_MODEL = "claude-haiku-4-5"

DEFAULT_CODEX_LUNA_MODEL = "gpt-5.6-luna"
DEFAULT_CODEX_TERRA_MODEL = "gpt-5.6-terra"
DEFAULT_CODEX_SOL_MODEL = "gpt-5.6-sol"
DEFAULT_CODEX_TIER_MODELS = (
    DEFAULT_CODEX_LUNA_MODEL,
    DEFAULT_CODEX_TERRA_MODEL,
    DEFAULT_CODEX_SOL_MODEL,
)

# OpenRouter-routed Opus for the multi-provider consensus roster. This is the
# OpenRouter slug (dotted ``claude-opus-4.8``), which differs from the
# Anthropic-direct id above — LiteLLM forwards it verbatim to OpenRouter, so it
# must match OpenRouter's published model id exactly or consensus voting fails.
DEFAULT_CONSENSUS_OPUS_MODEL = "openrouter/anthropic/claude-opus-4.8"


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
    DEFAULT_OPUS_MODEL: ("claude-opus-4-6",),
    DEFAULT_SONNET_MODEL: ("claude-sonnet-4-20250514",),
    DEFAULT_CONSENSUS_OPUS_MODEL: ("openrouter/anthropic/claude-opus-4-6",),
}


# Historical shipped default pins for the ``economics.tiers`` model ladder,
# keyed by the legacy id and valued by the CURRENT shipped id that replaced it.
# Same rationale as LEGACY_DEFAULT_MODELS (Q00/ouroboros#1324), applied to the
# tier ladder that model-tier routing reads: a user's persisted
# ``~/.ouroboros/config.yaml`` written by an older release still carries the OLD
# shipped tier defaults verbatim (the defaults ship as concrete ids inside
# EconomicsConfig, not a ``"default"`` sentinel). When a pin is bumped, an
# untouched shipped default in that persisted config is otherwise
# indistinguishable from a deliberate user override — so per-call model-tier
# routing would enforce a retired id (e.g. ``--model gpt-4o``) that the current
# provider map cannot execute, failing every AC. Normalizing a legacy shipped id
# to its current replacement keeps the shipped defaults live across a bump. This
# necessarily means a deliberate override that is byte-for-byte equal to an old
# shipped default is normalized too: the persisted schema carries no provenance
# bit that could distinguish those cases. Explicit ids that were never shipped
# (for example a proxy-specific model) remain untouched.
#
# Verified against git history of ``ouroboros.config.models`` (the tier defaults
# have only ever held these ids): openai frugal ``gpt-4o-mini`` -> standard
# ``gpt-4o`` -> frontier ``o3`` became ``gpt-5.1-codex-mini`` / ``gpt-5-codex`` /
# ``gpt-5.2`` and then the current GPT-5.6 Luna/Terra/Sol family; anthropic
# frugal ``claude-3-5-haiku``, standard
# ``claude-sonnet-4-20250514``, frontier ``claude-opus-4-5-20251101`` (and the
# later frontier ``claude-opus-4-6``) became the DEFAULT_*_MODEL pins. The google
# tier ids (``gemini-2.0-flash`` / ``gemini-2.5-pro``) have never been bumped, so
# they carry no entry and are preserved as-is.
LEGACY_TIER_MODELS: dict[str, str] = {
    # openai (Codex CLI) tier ids
    "gpt-4o-mini": DEFAULT_CODEX_LUNA_MODEL,
    "gpt-5.1-codex-mini": DEFAULT_CODEX_LUNA_MODEL,
    "gpt-4o": DEFAULT_CODEX_TERRA_MODEL,
    "gpt-5-codex": DEFAULT_CODEX_TERRA_MODEL,
    "o3": DEFAULT_CODEX_SOL_MODEL,
    "gpt-5.2": DEFAULT_CODEX_SOL_MODEL,
    # anthropic tier ids
    "claude-3-5-haiku": DEFAULT_HAIKU_MODEL,
    "claude-sonnet-4-20250514": DEFAULT_SONNET_MODEL,
    "claude-opus-4-5-20251101": DEFAULT_OPUS_MODEL,
    "claude-opus-4-6": DEFAULT_OPUS_MODEL,
}

# Provider each historical tier default shipped under. The same model-looking
# string under a DIFFERENT provider cannot be an untouched shipped default; it is
# necessarily an explicit/custom routing choice (often a proxy alias) and must be
# preserved verbatim.
LEGACY_TIER_MODEL_PROVIDERS: dict[str, str] = {
    "gpt-4o-mini": "openai",
    "gpt-5.1-codex-mini": "openai",
    "gpt-4o": "openai",
    "gpt-5-codex": "openai",
    "o3": "openai",
    "gpt-5.2": "openai",
    "claude-3-5-haiku": "anthropic",
    "claude-sonnet-4-20250514": "anthropic",
    "claude-opus-4-5-20251101": "anthropic",
    "claude-opus-4-6": "anthropic",
}


def normalize_tier_model(model: str, *, provider: str | None = None) -> str:
    """Return the current shipped id for ``model`` if it is a legacy shipped default.

    A tier model matching a historically shipped default (see
    :data:`LEGACY_TIER_MODELS`) resolves to its current replacement, even if the
    user deliberately typed that same historical id under the same provider:
    persisted config has no provenance with which to tell that override from an
    untouched old default. When ``provider`` is supplied, normalization is limited
    to the provider the historical value actually shipped under; a cross-provider
    occurrence is necessarily explicit and is preserved. Any id outside the
    historical set — including current shipped defaults and explicit never-shipped
    choices — is returned verbatim.
    """
    shipped_provider = LEGACY_TIER_MODEL_PROVIDERS.get(model)
    if provider is not None and model in LEGACY_TIER_MODELS and provider != shipped_provider:
        return model
    return LEGACY_TIER_MODELS.get(model, model)


def recognized_shipped_defaults(default_model: str) -> tuple[str, ...]:
    """Return every shipped-default value (current + historical) for a pin.

    The loader's backend normalization treats any of these as "the shipped
    default the user never chose," so a persisted config from a prior release
    still resolves to the backend-safe ``"default"`` sentinel after a pin bump.
    Genuinely explicit, never-shipped model ids are absent from this set and so
    remain preserved verbatim.
    """
    return (default_model, *LEGACY_DEFAULT_MODELS.get(default_model, ()))
