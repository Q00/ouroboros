"""Sampling compatibility for LiteLLM's direct OpenAI reasoning route."""

import re

import litellm


def omit_openai_reasoning_sampling(model: str, effort: str | None) -> bool:
    """Whether direct OpenAI reasoning requests must omit sampling controls.

    Unknown reasoning model metadata is treated conservatively: unless LiteLLM
    explicitly records support for effort ``none``, sampling is omitted.
    Other provider prefixes, including OpenRouter, retain their own behavior.
    """
    name = model.lower()
    if "/" in name:
        provider, name = name.split("/", 1)
        if provider != "openai":
            return False
    if not (re.match(r"^o\d+(?:-|$)", name) or name.startswith("gpt-5")):
        return False
    if re.match(r"^gpt-5(?:\.\d+)?-chat(?:-|$)", name):
        return False
    metadata = litellm.model_cost.get(name, {})
    supports_none = metadata.get("supports_none_reasoning_effort") is True
    return not supports_none or bool(effort and effort != "none")
