"""Read the token statistics in Gemini CLI stream-json and batch JSON output."""

from __future__ import annotations

from ouroboros.providers.base import UsageInfo


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _usage(prompt: object, completion: object, total: object) -> UsageInfo:
    prompt_tokens = _count(prompt)
    completion_tokens = _count(completion)
    allocated = prompt_tokens + completion_tokens
    return UsageInfo(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=allocated,
        unallocated_tokens=max(0, _count(total) - allocated),
    )


def gemini_usage_from_stats(stats: object) -> UsageInfo:
    """Preserve reported totals without counting cached/model breakdowns twice."""
    if not isinstance(stats, dict):
        return _usage(None, None, None)
    if any(key in stats for key in ("input_tokens", "output_tokens", "total_tokens")):
        return _usage(
            stats.get("input_tokens"), stats.get("output_tokens"), stats.get("total_tokens")
        )

    # Batch JSON exposes the telemetry model map rather than streaming totals.
    models = stats.get("models")
    if not isinstance(models, dict):
        return _usage(None, None, None)
    usages: list[UsageInfo] = []
    for model in models.values():
        tokens = model.get("tokens") if isinstance(model, dict) else None
        if isinstance(tokens, dict):
            usages.append(
                _usage(tokens.get("prompt"), tokens.get("candidates"), tokens.get("total"))
            )
    return UsageInfo(
        prompt_tokens=sum(item.prompt_tokens for item in usages),
        completion_tokens=sum(item.completion_tokens for item in usages),
        total_tokens=sum(item.total_tokens for item in usages),
        unallocated_tokens=sum(item.unallocated_tokens for item in usages),
    )
