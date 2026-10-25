"""Unit tests for the megacode harness-backed LLM adapter."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.megacode_llm_adapter import MEGACODE_LLM_MODE, MegacodeLLMAdapter


class _FakeStream:
    def __init__(self, text: str = "") -> None:
        self._buffer = text.encode("utf-8")
        self._cursor = 0

    async def read(self, chunk_size: int = 16384) -> bytes:
        if self._cursor >= len(self._buffer):
            return b""
        next_cursor = min(self._cursor + chunk_size, len(self._buffer))
        chunk = self._buffer[self._cursor : next_cursor]
        self._cursor = next_cursor
        return chunk


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = returncode
        self._final_returncode = returncode

    async def wait(self) -> int:
        self.returncode = self._final_returncode
        return self.returncode

    def terminate(self) -> None:
        self.returncode = self._final_returncode

    def kill(self) -> None:
        self.returncode = self._final_returncode


def _jsonl(*events: dict[str, object]) -> str:
    return "\n".join(json.dumps(event) for event in events) + "\n"


def _result(text: str | None, *, is_error: bool = False, error: str | None = None) -> dict:
    return {
        "type": "result",
        "v": 1,
        "session_id": "S1",
        "outcome": "model_failed" if is_error else "completed",
        "is_error": is_error,
        "result": text,
        "error": error,
    }


class TestMegacodeLLMAdapter:
    def test_build_command_runs_one_headless_turn_in_llm_mode(self) -> None:
        adapter = MegacodeLLMAdapter(cli_path="/tmp/harness", max_turns=3)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model="mega:full",
        )

        assert command[:4] == ["/tmp/harness", "-p", "--output-format", "stream-json"]
        assert command[command.index("--mode") + 1] == MEGACODE_LLM_MODE
        assert command[command.index("--max-iterations") + 1] == "3"
        assert command[-2:] == ["--model", "mega:full"]

    def test_sentinel_model_is_not_forwarded(self) -> None:
        adapter = MegacodeLLMAdapter(cli_path="/tmp/harness")
        assert adapter._normalize_model("default") is None

    @pytest.mark.asyncio
    async def test_complete_returns_the_result_record_answer(self) -> None:
        adapter = MegacodeLLMAdapter(cli_path="/tmp/harness", cwd="/tmp/project")
        fake_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            assert command[:2] == ("/tmp/harness", "-p")
            assert kwargs["cwd"] == "/tmp/project"
            process = _FakeProcess(
                stdout=_jsonl(
                    {"type": "init", "v": 1, "session_id": "S1", "model": "mega:full"},
                    {"type": "user", "text": "Summarize this."},
                    {"type": "reasoning", "text": "thinking"},
                    {"type": "assistant", "text": "draft"},
                    _result("Final answer"),
                )
            )
            fake_processes.append(process)
            return process

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Summarize this.")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert result.value.content == "Final answer"
        assert result.value.raw_response["session_id"] == "S1"
        assert fake_processes[0].stdin.closed is True
        assert b"Summarize this" in fake_processes[0].stdin.data

    @pytest.mark.asyncio
    async def test_complete_surfaces_the_result_error(self) -> None:
        adapter = MegacodeLLMAdapter(cli_path="/tmp/harness", max_retries=1)

        async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout=_jsonl(_result(None, is_error=True, error="io: 401 Unauthorized")),
                returncode=1,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="hi")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert "401 Unauthorized" in json.dumps(result.error.details, default=str) + str(
            result.error.message
        )

    @pytest.mark.asyncio
    async def test_complete_extracts_json_when_response_format_requested(self) -> None:
        adapter = MegacodeLLMAdapter(cli_path="/tmp/harness")

        async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(stdout=_jsonl(_result('```json\n{"approved": true}\n```')))

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return a verdict.")],
                CompletionConfig(
                    model="default",
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "type": "object",
                            "properties": {"approved": {"type": "boolean"}},
                        },
                    },
                ),
            )

        assert result.is_ok
        assert result.value.content == '{"approved": true}'

    def test_factory_builds_megacode_adapter(self) -> None:
        from ouroboros.providers.factory import create_llm_adapter

        with patch("ouroboros.config.get_megacode_cli_path", return_value="/tmp/harness"):
            adapter = create_llm_adapter(backend="megacode", cwd="/tmp")

        assert isinstance(adapter, MegacodeLLMAdapter)
