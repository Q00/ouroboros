"""OpenCode 1.x final step token groups reach completion usage once per part."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole, UsageInfo
from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter


class _Stream:
    def __init__(self, text: str = "") -> None:
        self.data = text.encode()

    async def read(self, n: int = -1) -> bytes:
        data, self.data = self.data, b""
        return data


class _Process:
    def __init__(self, events: list[dict]) -> None:
        self.stdin = None
        self.stdout = _Stream("\n".join(json.dumps(event) for event in events))
        self.stderr = _Stream()
        self.returncode = 0

    async def wait(self) -> int:
        return 0


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_part", [False, True])
async def test_native_step_finish_usage_is_not_discarded_or_recounted(repeat_part: bool) -> None:
    # Official v1.2.0 ffc000de8e446c63d41a2e352d119d9ff43530d0 run.ts emits step_finish.
    first = {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "id": "part-one",
            "tokens": {
                "input": 10,
                "output": 4,
                "reasoning": 2,
                "cache": {"read": 5, "write": 3},
                "total": 22,
            },
        },
    }
    second = {
        "type": "step_finish",
        "part": {
            "type": "step-finish",
            "id": "part-two",
            "tokens": {
                "input": 7,
                "output": 3,
                "reasoning": 0,
                "cache": {"read": 0, "write": 0},
                "total": 10,
            },
        },
    }
    events = [
        first,
        *([first] if repeat_part else []),
        second,
        {"type": "text", "part": {"type": "text", "text": "Answer"}},
    ]
    adapter = OpenCodeLLMAdapter(cli_path="/unused/opencode")
    with patch("asyncio.create_subprocess_exec", return_value=_Process(events)):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Question")], CompletionConfig(model="default")
        )
    assert result.is_ok
    assert result.value.content == "Answer"
    assert result.value.usage == UsageInfo(17, 7, 24, unallocated_tokens=8)


@pytest.mark.parametrize("reported_total,expected_unallocated", [(22, 8), (None, 8)])
def test_native_total_or_groups_do_not_add_raw_output_reasoning_again(
    reported_total, expected_unallocated
) -> None:
    from ouroboros.providers.native_cli_usage import opencode_cli_usage

    # Official getUsage stores raw usage.outputTokens without subtracting reasoning.
    tokens = {"input": 10, "output": 4, "reasoning": 2, "cache": {"read": 5, "write": 3}}
    if reported_total is not None:
        tokens["total"] = reported_total
    events = [
        {"type": "step_finish", "part": {"id": "part-one", "type": "step-finish", "tokens": tokens}}
    ]
    assert opencode_cli_usage(events) == UsageInfo(
        10, 4, 14, unallocated_tokens=expected_unallocated
    )


def test_latest_part_report_replaces_earlier_and_distinct_equal_parts_stay_distinct() -> None:
    from ouroboros.providers.native_cli_usage import opencode_cli_usage

    def report(part_id: str, input_count: int) -> dict:
        return {
            "type": "step_finish",
            "part": {
                "id": part_id,
                "type": "step-finish",
                "tokens": {
                    "input": input_count,
                    "output": 2,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                },
            },
        }

    events = [report("part-one", 3), report("part-one", 5), report("part-two", 5)]
    assert opencode_cli_usage(events) == UsageInfo(10, 4, 14)
