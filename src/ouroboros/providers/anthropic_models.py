"""Request capabilities for the concrete Claude 5.5 API model identifiers."""

import json

CLAUDE_5_5_MODELS = frozenset({"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5"})


def is_claude_5_5(model: str) -> bool:
    """Recognize direct Anthropic IDs and their distinct OpenRouter slugs."""
    normalized = model.lower()
    if normalized.startswith("openrouter/anthropic/"):
        return normalized.removeprefix("openrouter/anthropic/").replace("5.5", "5-5") in (
            CLAUDE_5_5_MODELS
        )
    return normalized.removeprefix("anthropic/") in CLAUDE_5_5_MODELS


def response_format_instruction(response_format: dict[str, object]) -> str:
    """Steer JSON output on Anthropic models that reject assistant prefill."""
    if response_format.get("type") == "json_schema":
        schema_json = json.dumps(
            response_format.get("json_schema", {}), sort_keys=True, separators=(",", ":")
        )
        return (
            "Respond only with valid JSON that conforms to this JSON schema. "
            f"Do not include prose or Markdown.\nSchema: {schema_json}"
        )
    return "Respond only with a valid JSON object. Do not include prose or Markdown."
