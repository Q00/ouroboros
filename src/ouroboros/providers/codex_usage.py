"""Token reporting for a native one-shot Codex CLI completion."""

from __future__ import annotations

from collections.abc import Iterable
import json

from ouroboros.providers.base import UsageInfo


def codex_cli_usage(lines: Iterable[str], *, provider: str) -> UsageInfo:
    """Read the last valid completed-turn report without adding cache subsets."""
    usage = UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    if provider != "codex_cli":
        return usage
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "turn.completed":
            continue
        counts = event.get("usage")
        if not isinstance(counts, dict):
            continue
        prompt = counts.get("input_tokens")
        completion = counts.get("output_tokens")
        if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
            continue
        usage = UsageInfo(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
        )
    return usage
