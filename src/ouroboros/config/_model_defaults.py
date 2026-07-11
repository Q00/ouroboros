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
# to its current replacement keeps the shipped defaults live across a bump while
# leaving a genuinely explicit, never-shipped id (a proxy-specific model) alone.
#
# Verified against git history of ``ouroboros.config.models`` (the tier defaults
# have only ever held these ids): openai frugal ``gpt-4o-mini`` -> standard
# ``gpt-4o`` -> frontier ``o3`` became ``gpt-5.1-codex-mini`` / ``gpt-5-codex`` /
# ``gpt-5.2``; anthropic frugal ``claude-3-5-haiku``, standard
# ``claude-sonnet-4-20250514``, frontier ``claude-opus-4-5-20251101`` (and the
# later frontier ``claude-opus-4-6``) became the DEFAULT_*_MODEL pins. The google
# tier ids (``gemini-2.0-flash`` / ``gemini-2.5-pro``) have never been bumped, so
# they carry no entry and are preserved as-is.
LEGACY_TIER_MODELS: dict[str, str] = {
    # openai (Codex CLI) tier ids
    "gpt-4o-mini": "gpt-5.1-codex-mini",
    "gpt-4o": "gpt-5-codex",
    "o3": "gpt-5.2",
    # anthropic tier ids
    "claude-3-5-haiku": DEFAULT_HAIKU_MODEL,
    "claude-sonnet-4-20250514": DEFAULT_SONNET_MODEL,
    "claude-opus-4-5-20251101": DEFAULT_OPUS_MODEL,
    "claude-opus-4-6": DEFAULT_OPUS_MODEL,
}


def normalize_tier_model(model: str) -> str:
    """Return the current shipped id for ``model`` if it is a legacy shipped default.

    A tier model matching a historically shipped default (see
    :data:`LEGACY_TIER_MODELS`) resolves to its current replacement; any other id
    — including the current shipped defaults and genuinely explicit user choices —
    is returned verbatim.
    """
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
