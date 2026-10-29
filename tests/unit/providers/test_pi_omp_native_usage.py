"""Pi-family final run snapshots count each assistant response once."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole, UsageInfo
from ouroboros.providers.omp_llm_adapter import OmpLLMAdapter
from ouroboros.providers.pi_llm_adapter import PiLLMAdapter


class _Stream:
    def __init__(self, text: str = "") -> None:
        self.data = text.encode()

    async def read(self, n: int = -1) -> bytes:
        data, self.data = self.data, b""
        return data


class _StreamingProcess:
    def __init__(self, text: str) -> None:
        self.stdin = None
        self.stdout = _Stream(text)
        self.stderr = _Stream()
        self.returncode = 0

    async def wait(self) -> int:
        return 0


class _CommunicateProcess:
    def __init__(self, text: str) -> None:
        self.stdin = None
        self.data = text.encode()
        self.returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.data, b""


def _events(repetitions: int) -> list[dict]:
    first = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Intermediate answer"}],
        "usage": {"input": 10, "output": 4, "cacheRead": 100, "cacheWrite": 20, "totalTokens": 134},
    }
    second = {
        "role": "assistant",
        "content": [{"type": "text", "text": "Final answer"}],
        "usage": {
            "input": 5,
            "output": 2,
            "cacheRead": 10,
            "cacheWrite": 4,
            "totalTokens": 21,
            "reasoning": 1,
            "cacheWrite1h": 2,
        },
    }
    tool_result = {"role": "toolResult", "content": [{"type": "text", "text": "Data"}]}
    messages = [{"role": "user", "content": "Question"}, first, tool_result, second]
    # Official AgentEvent lifecycle repeats message data; agent_end is per-run.
    return [
        {"type": "message_end", "message": first},
        {"type": "turn_end", "message": first, "toolResults": [tool_result]},
        {"type": "message_end", "message": second},
        {"type": "turn_end", "message": second, "toolResults": []},
        *[{"type": "agent_end", "messages": messages} for _ in range(repetitions)],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter_type", [PiLLMAdapter, OmpLLMAdapter])
@pytest.mark.parametrize("process_type", [_StreamingProcess, _CommunicateProcess])
@pytest.mark.parametrize("final_repetitions", [1, 2], ids=["single-final", "repeat-final"])
async def test_one_shot_usage_counts_snapshot_once(adapter_type, process_type, final_repetitions):
    stream = "\n".join(json.dumps(event) for event in _events(final_repetitions))
    adapter = adapter_type(cli_path="/unused/pi")
    with patch("asyncio.create_subprocess_exec", return_value=process_type(stream)):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Question")], CompletionConfig(model="default")
        )
    assert result.is_ok
    assert result.value.content == "Final answer"
    assert result.value.usage == UsageInfo(15, 6, 21, unallocated_tokens=134)


def test_missing_final_snapshot_keeps_zero_fallback() -> None:
    from ouroboros.providers.native_cli_usage import native_cli_usage

    lines = [json.dumps(event) for event in _events(0)]
    assert native_cli_usage(lines, provider="pi") == UsageInfo(0, 0, 0)


def test_distinct_identical_messages_in_snapshot_are_each_counted() -> None:
    from ouroboros.providers.native_cli_usage import native_cli_usage

    message = {"role": "assistant", "usage": {"input": 3, "output": 2, "totalTokens": 5}}
    lines = [json.dumps({"type": "agent_end", "messages": [message, message]})]
    assert native_cli_usage(lines, provider="omp") == UsageInfo(6, 4, 10)
