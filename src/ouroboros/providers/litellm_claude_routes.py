"""Claude routes whose request shape LiteLLM's bundled model map cannot translate.

LiteLLM decides how to send ``reasoning_effort`` and ``response_format`` to
Claude from its model map. For a model the map does not list, it refuses
``reasoning_effort`` or turns it into ``thinking.budget_tokens``, and turns a
JSON schema into a forced tool call. Current Claude models reject all three.
Those routes instead get the native fields the Claude 5.5 path already sends.

The decision reads the map shipped inside the installed LiteLLM wheel, not
``litellm.model_cost``: LiteLLM replaces that at import with a remote copy
unless ``LITELLM_LOCAL_MODEL_COST_MAP`` is set, so membership there would
depend on the network and the environment. The bundled map is fixed for a
pinned LiteLLM version, so the parent adapter and the sealed proof worker
reach the same answer.
"""

from __future__ import annotations

from functools import lru_cache

from ouroboros.providers.anthropic_models import is_claude_5_5, omit_claude_sampling

_MAPPED_PROVIDERS = frozenset({"anthropic", "openrouter"})


@lru_cache(maxsize=1)
def _bundled_claude_route_ids() -> frozenset[str]:
    """Lowercased Anthropic and OpenRouter ids in LiteLLM's bundled model map.

    Any failure to read the map yields an empty set, so every route counts as
    unlisted and gets the native fields.
    """
    try:
        from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap

        content = GetModelCostMap.load_local_model_cost_map()
    except Exception:  # noqa: BLE001 - an unreadable map means "not listed"
        return frozenset()
    if not isinstance(content, dict):
        return frozenset()
    return frozenset(
        key.lower()
        for key, entry in content.items()
        if isinstance(key, str)
        and isinstance(entry, dict)
        and entry.get("litellm_provider") in _MAPPED_PROVIDERS
    )


@lru_cache(maxsize=1024)
def native_claude_route(model: str) -> bool:
    """Whether a LiteLLM route must carry native Claude effort and JSON fields.

    True for the Claude 5.5 ids, and for any other Claude model that is not a
    legacy sampling family when it is sent directly (bare or ``anthropic/``)
    or through ``openrouter/anthropic/`` and LiteLLM's bundled map does not
    list that route. Listed models (Opus 4.6 to 4.8, Fable 5, ...), legacy
    families, Bedrock, Vertex and other providers keep LiteLLM's handling.
    """
    if is_claude_5_5(model):
        return True
    if not omit_claude_sampling(model):
        return False
    route = model.strip().lower()
    if route.startswith("openrouter/"):
        if not route.startswith("openrouter/anthropic/"):
            return False
    else:
        route = route.removeprefix("anthropic/")
        if "/" in route:
            return False
    return route not in _bundled_claude_route_ids()
