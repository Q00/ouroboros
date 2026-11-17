"""Request capabilities of Claude model identifiers.

Anthropic removed sampling parameters (``temperature``, ``top_p``, ``top_k``)
from Claude Opus 4.7 and every later Claude model, and assistant-turn prefill
from Claude Opus/Sonnet 4.6 onward; sending either returns HTTP 400. Claude
4.x before that accepts at most one of ``temperature`` / ``top_p``, and Claude
3.x accepts both. The checks below FAIL SAFE: only recognised legacy families
are allowed these fields, so new, aliased, and unknown Claude ids get neither.
"""

import json
import re

CLAUDE_5_5_MODELS = frozenset({"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"})

# Substrings that mark a model string as Claude, whatever the route prefix.
# ``anthropic`` covers every Anthropic route (``anthropic/``,
# ``openrouter/anthropic/``, Bedrock ``anthropic.`` ids), which serve only Claude.
_CLAUDE_MARKERS = ("claude", "fable", "mythos", "anthropic")
# Runtime aliases and the backend sentinel resolve to whatever Claude model is
# current, which Ouroboros cannot know, so they are never treated as legacy.
_CLAUDE_ALIASES = frozenset({"opus", "sonnet", "haiku", "default"})

# Optional dated snapshot (``-20250514``) or ``-latest`` pointer.
_SNAPSHOT = r"(?:-(?:\d{8}|latest))?"
_CLAUDE_3 = re.compile(rf"^claude-3(?:-\d)?-(?:opus|sonnet|haiku){_SNAPSHOT}$")
# Claude 4, 4.0, 4.1, 4.5 and 4.6 (Opus 4.7 and later removed sampling).
_CLAUDE_4_SAMPLING = re.compile(rf"^claude-(?:opus|sonnet|haiku)-4(?:-[0156])?{_SNAPSHOT}$")
# Claude 4, 4.0, 4.1 and 4.5 (4.6 and later removed assistant prefill).
_CLAUDE_4_PREFILL = re.compile(rf"^claude-(?:opus|sonnet|haiku)-4(?:-[015])?{_SNAPSHOT}$")
_BEDROCK_VERSION = re.compile(r"-v\d+$")


def is_claude_5_5(model: str) -> bool:
    """Recognize direct Anthropic IDs and their distinct OpenRouter slugs."""
    normalized = model.lower()
    if normalized.startswith("openrouter/anthropic/"):
        return normalized.removeprefix("openrouter/anthropic/").replace("5.5", "5-5") in (
            CLAUDE_5_5_MODELS
        )
    return normalized.removeprefix("anthropic/") in CLAUDE_5_5_MODELS


def _is_claude(model: str) -> bool:
    """Whether a model string targets Claude.

    A bare alias (``opus``) counts, and an alias on an Anthropic route
    (``anthropic/default``, ``openrouter/anthropic/sonnet``) counts through the
    ``anthropic`` marker. An alias on another provider (``openai/default``)
    does not, because that route does not serve Claude.
    """
    normalized = model.strip().lower()
    return normalized in _CLAUDE_ALIASES or any(m in normalized for m in _CLAUDE_MARKERS)


def _anthropic_model_name(model: str) -> str:
    """Reduce a routed model string to its bare Anthropic model name.

    Handles route prefixes (``anthropic/``, ``openrouter/anthropic/``,
    ``bedrock/converse/``, ``vertex_ai/``, ARNs), Bedrock ids
    (``us.anthropic.claude-sonnet-4-5-20250929-v1:0``), Vertex snapshots
    (``claude-opus-4-1@20250805``), OpenRouter variants (``:thinking``) and
    dotted OpenRouter versions (``claude-opus-4.6``).
    """
    name = model.strip().lower().rsplit("/", 1)[-1]
    _, bedrock, rest = name.partition("anthropic.")
    if bedrock:
        name = rest
    name = name.split("@", 1)[0].split(":", 1)[0]
    return _BEDROCK_VERSION.sub("", name).replace(".", "-")


def claude_accepts_sampling(model: str) -> bool:
    """Whether a Claude model is a legacy family documented to accept sampling.

    True only for Claude 3.x and Claude 4 through 4.6. Every other model
    string, including aliases, ``default`` and unknown future ids, is False.
    """
    name = _anthropic_model_name(model)
    return bool(_CLAUDE_3.match(name) or _CLAUDE_4_SAMPLING.match(name))


def claude_accepts_temperature_and_top_p(model: str) -> bool:
    """Whether a Claude model accepts ``temperature`` and ``top_p`` together.

    Only Claude 3.x does; Claude 4 through 4.6 reject the pair with HTTP 400.
    """
    return bool(_CLAUDE_3.match(_anthropic_model_name(model)))


def claude_accepts_prefill(model: str) -> bool:
    """Whether a Claude model accepts an assistant-turn prefill.

    True only for Claude 3.x and Claude 4 through 4.5.
    """
    name = _anthropic_model_name(model)
    return bool(_CLAUDE_3.match(name) or _CLAUDE_4_PREFILL.match(name))


def omit_claude_sampling(model: str) -> bool:
    """Whether a routed request must omit sampling because it targets Claude.

    True for any Claude model string (``claude``, ``fable``, ``mythos`` or
    ``anthropic`` anywhere, so every Anthropic route including prefixed aliases
    such as ``anthropic/opus``, or exactly ``opus`` / ``sonnet`` / ``haiku`` /
    ``default``) that is not a recognised legacy sampling family. Non-Claude
    model strings return False so their routes keep their own behavior.
    """
    return _is_claude(model) and not claude_accepts_sampling(model)


def response_format_instruction(response_format: dict[str, object]) -> str:
    """Steer JSON output on Anthropic models that reject assistant prefill."""
    if response_format.get("type") == "json_schema":
        schema = response_format.get("json_schema", {})
        if isinstance(schema, dict) and isinstance(schema.get("schema"), dict):
            schema = schema["schema"]
        schema_json = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        return (
            "Respond only with valid JSON that conforms to this JSON schema. "
            f"Do not include prose or Markdown.\nSchema: {schema_json}"
        )
    return "Respond only with a valid JSON object. Do not include prose or Markdown."
