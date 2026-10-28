"""Native Codex turn usage reaches either completion response path."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole, UsageInfo
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter


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


@pytest.mark.asyncio
@pytest.mark.parametrize("process_type", [_StreamingProcess, _CommunicateProcess])
@pytest.mark.parametrize("final_repetitions", [1, 2], ids=["single-final", "repeat-final"])
async def test_native_usage_survives_success_paths(process_type, final_repetitions) -> None:
    # Official Usage/TurnCompletedEvent: openai/codex@26ad281fd1ca712e4f0c7920a6273cf18c961b8a.
    event = {
        "type": "turn.completed",
        "usage": {
            "input_tokens": 100,
            "cached_input_tokens": 80,
            "cache_write_input_tokens": 5,
            "output_tokens": 20,
            "reasoning_output_tokens": 7,
        },
    }
    text = "\n".join(json.dumps(event) for _ in range(final_repetitions))

    async def create(*command: str, **kwargs: object):
        Path(command[command.index("--output-last-message") + 1]).write_text("Answer")
        return process_type(text)

    adapter = CodexCliLLMAdapter(cli_path="/unused/codex")
    with patch("asyncio.create_subprocess_exec", side_effect=create):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Question")], CompletionConfig(model="default")
        )
    assert result.is_ok
    assert result.value.content == "Answer"
    assert result.value.usage == UsageInfo(
        prompt_tokens=100, completion_tokens=20, total_tokens=120
    )


def test_latest_native_turn_report_replaces_earlier_report() -> None:
    from ouroboros.providers.native_cli_usage import native_cli_usage

    lines = [
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 2}}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 15, "output_tokens": 3}}),
    ]
    assert native_cli_usage(lines, provider="codex_cli") == UsageInfo(15, 3, 18)
    assert native_cli_usage(lines, provider="pi") == UsageInfo(0, 0, 0)


def test_missing_or_incomplete_native_usage_keeps_zero_fallback() -> None:
    from ouroboros.providers.native_cli_usage import native_cli_usage

    lines = [
        "plain output",
        json.dumps({"type": "turn.completed"}),
        json.dumps({"type": "turn.completed", "usage": {"input_tokens": True, "output_tokens": 2}}),
    ]
    assert native_cli_usage(lines, provider="codex_cli") == UsageInfo(0, 0, 0)
