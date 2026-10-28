"""Gemini native and batch token statistics reach CompletionResponse.usage."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole, UsageInfo
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter


class _Stream:
    def __init__(self, data: str = "") -> None:
        self.data = data.encode()

    async def read(self, n: int = -1) -> bytes:
        data, self.data = self.data, b""
        return data


class _Process:
    def __init__(self, event: dict, *, indent: int | None = None) -> None:
        self.stdout = _Stream(json.dumps(event, indent=indent) + "\n")
        self.stderr = _Stream()
        self.returncode = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize("indent", [None, 2], ids=["compact", "multiline"])
@pytest.mark.parametrize(
    "event,expected",
    [
        (
            {
                "type": "result",
                "response": "Answer",
                "status": "success",
                "stats": {
                    "input_tokens": 12,
                    "output_tokens": 8,
                    "total_tokens": 25,
                    "cached": 3,
                    "models": {"gemini-flash": {"input_tokens": 12, "output_tokens": 8}},
                },
            },
            UsageInfo(prompt_tokens=12, completion_tokens=8, total_tokens=20, unallocated_tokens=5),
        ),
        (
            {
                "response": "Answer",
                "stats": {
                    "models": {
                        "gemini-flash": {"tokens": {"prompt": 12, "candidates": 8, "total": 25}},
                        "gemini-pro": {"tokens": {"prompt": 5, "candidates": 3, "total": 10}},
                    }
                },
            },
            UsageInfo(
                prompt_tokens=17, completion_tokens=11, total_tokens=28, unallocated_tokens=7
            ),
        ),
    ],
    ids=["native-stream-stats", "batch-multiple-models"],
)
async def test_reported_token_counts_are_not_discarded(event, expected, indent) -> None:
    # Official schema/formatter at google-gemini/gemini-cli@b8fa817155731c783d47dc6eaeee63163c0ec723.
    adapter = GeminiCLIAdapter(cli_path="gemini")
    with patch("asyncio.create_subprocess_exec", return_value=_Process(event, indent=indent)):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Question")], CompletionConfig(model="default")
        )
    assert result.is_ok
    assert result.value.usage == expected


@pytest.mark.parametrize(
    "stats,expected",
    [
        (None, UsageInfo(0, 0, 0)),
        ({"input_tokens": 5, "output_tokens": 2}, UsageInfo(5, 2, 7)),
        ({"total_tokens": 12}, UsageInfo(0, 0, 0, unallocated_tokens=12)),
        ({"input_tokens": True, "output_tokens": -1, "total_tokens": "12"}, UsageInfo(0, 0, 0)),
        ({"models": {"legacy": {}}}, UsageInfo(0, 0, 0)),
    ],
)
def test_missing_or_partial_stats_keep_truthful_allocations(stats, expected) -> None:
    from ouroboros.providers.gemini_usage import gemini_usage_from_stats

    assert gemini_usage_from_stats(stats) == expected
