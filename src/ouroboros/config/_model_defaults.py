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
"""

# Frontier reasoning tier (interview, seed, ontology, evaluation, execution
# analysis, consensus advocate). Bump on each new Opus release.
DEFAULT_OPUS_MODEL = "claude-opus-4-8"

# Speed/judgment tier (QA verdicts, assertion extraction). Bump on each new
# Sonnet release.
DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"

# OpenRouter-routed Opus for the multi-provider consensus roster. Mirrors
# ``DEFAULT_OPUS_MODEL`` behind the ``openrouter/anthropic/`` prefix.
DEFAULT_CONSENSUS_OPUS_MODEL = "openrouter/anthropic/claude-opus-4-8"
