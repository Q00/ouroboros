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

# OpenRouter-routed Opus for the multi-provider consensus roster. This is the
# OpenRouter slug (dotted ``claude-opus-4.8``), which differs from the
# Anthropic-direct id above — LiteLLM forwards it verbatim to OpenRouter, so it
# must match OpenRouter's published model id exactly or consensus voting fails.
DEFAULT_CONSENSUS_OPUS_MODEL = "openrouter/anthropic/claude-opus-4.8"
