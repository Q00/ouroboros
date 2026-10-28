"""Token reporting for native one-shot Codex and Pi-family CLI completions."""

from __future__ import annotations

from collections.abc import Iterable
import json

from ouroboros.providers.base import UsageInfo


def native_cli_usage(lines: Iterable[str], *, provider: str) -> UsageInfo:
    """Map one-shot final usage without recounting repeated event data."""
    usage = UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    if provider in {"pi", "omp"}:
        return _pi_family_usage(lines)
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


def _token_count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def _pi_family_usage(lines: Iterable[str]) -> UsageInfo:
    # agent_end contains the current run's messages. message_end and turn_end
    # repeat those messages, so adding their metrics would count each twice.
    snapshot: list[object] = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict) or event.get("type") != "agent_end":
            continue
        if isinstance(event.get("messages"), list):
            snapshot = event["messages"]

    prompt = completion = unallocated = 0
    for message in snapshot:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        counts = message.get("usage")
        if not isinstance(counts, dict):
            continue
        input_count = _token_count(counts.get("input"))
        output_count = _token_count(counts.get("output"))
        caches = _token_count(counts.get("cacheRead")) + _token_count(counts.get("cacheWrite"))
        allocated = input_count + output_count
        # The reported total includes cache counts; retention/reasoning fields
        # are subsets and must not be added on top of this total.
        total = max(_token_count(counts.get("totalTokens")), allocated + caches)
        prompt += input_count
        completion += output_count
        unallocated += total - allocated
    return UsageInfo(prompt, completion, prompt + completion, unallocated_tokens=unallocated)
