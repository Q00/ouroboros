"""Gemini's native assistant deltas work without a full terminal response."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter


class _Stream:
    def __init__(self, events: list[dict] | None = None) -> None:
        self.data = (
            b"" if events is None else b"".join(json.dumps(e).encode() + b"\n" for e in events)
        )

    async def read(self, n: int = -1) -> bytes:
        data, self.data = self.data, b""
        return data


class _Process:
    def __init__(self, events: list[dict]) -> None:
        self.stdin = None
        self.stdout = _Stream(events)
        self.stderr = _Stream()
        self.returncode = None

    async def wait(self) -> int:
        self.returncode = 0
        return 0


def _native_events() -> list[dict]:
    # Schema: google-gemini/gemini-cli@b8fa817155731c783d47dc6eaeee63163c0ec723
    # packages/core/src/output/types.ts MessageEvent and ResultEvent.
    # Upstream MessageEvent uses assistant (not the Gemini API's model role).
    # ResultEvent contains outcome/stats and does not carry response text.
    return [
        {"type": "message", "role": "user", "content": "Say hello"},
        *[
            {"type": "message", "role": "assistant", "content": text, "delta": True}
            for text in ("Hello", " world", "\n")
        ],
        {"type": "result", "status": "success", "stats": {}},
    ]


@pytest.mark.asyncio
async def test_native_assistant_deltas_reach_llm_completion() -> None:
    adapter = GeminiCLIAdapter(cli_path="gemini")
    with patch("asyncio.create_subprocess_exec", return_value=_Process(_native_events())):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Say hello")], CompletionConfig(model="default")
        )
    assert result.is_ok
    assert result.value.content == "Hello world\n"


@pytest.mark.asyncio
async def test_native_assistant_deltas_reach_runtime_final_answer(tmp_path: Path) -> None:
    runtime = GeminiCLIRuntime(
        cli_path=Path(os.environ["OUROBOROS_TEST_CLI_DIR"]) / "gemini", cwd=tmp_path
    )
    with (
        patch("asyncio.create_subprocess_exec", return_value=_Process(_native_events())),
        patch.object(runtime, "_process_group_id", return_value=None),
    ):
        result = await runtime.execute_task_to_result("Say hello")
    assert result.is_ok
    assert result.value.final_message == "Hello world\n"
    assert not any(
        m.type == "assistant" and m.content == "Say hello" for m in result.value.messages
    )


@pytest.mark.asyncio
async def test_legacy_model_messages_keep_line_separators() -> None:
    events = [
        {"type": "message", "role": "model", "content": text}
        for text in ("First message", "Second message")
    ]
    adapter = GeminiCLIAdapter(cli_path="gemini")
    with patch("asyncio.create_subprocess_exec", return_value=_Process(events)):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Question")], CompletionConfig(model="default")
        )
    assert result.is_ok
    assert result.value.content == "First message\nSecond message"
