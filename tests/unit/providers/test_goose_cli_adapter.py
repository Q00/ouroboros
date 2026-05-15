"""Unit tests for the Goose CLI-backed LLM adapter."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.goose_cli_adapter import GooseCliLLMAdapter


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


class TestGooseCliLLMAdapter:
    def test_build_command_uses_goose_run_stream_json(self) -> None:
        adapter = GooseCliLLMAdapter(cli_path="/tmp/goose", max_turns=3)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model="gpt-5.5",
        )

        assert command[:4] == ["/tmp/goose", "run", "--output-format", "stream-json"]
        assert "--no-session" in command
        assert "--max-turns" in command
        assert "3" in command
        assert command[-2:] == ["--model", "gpt-5.5"]
        assert "--output-last-message" not in command
        assert "--output-schema" not in command

    def test_build_command_can_use_named_session_when_not_ephemeral(self) -> None:
        adapter = GooseCliLLMAdapter(cli_path="/tmp/goose", ephemeral=False)

        command = adapter._build_command(
            output_last_message_path="/tmp/out.txt",
            output_schema_path=None,
            model=None,
        )

        assert "--name" in command
        assert any(part.startswith("ouroboros-llm-") for part in command)
        assert "--no-session" not in command

    def test_permission_mode_sets_goose_mode_in_child_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OUROBOROS_LLM_BACKEND", "goose")
        monkeypatch.setenv("OUROBOROS_RUNTIME", "goose")
        adapter = GooseCliLLMAdapter(
            cli_path="/tmp/goose", cwd="/tmp/project", permission_mode="approve"
        )

        env = adapter._build_env_for_instance()

        assert env["GOOSE_MODE"] == "approve"
        assert env["GOOSE_WORKING_DIR"] == "/tmp/project"
        assert env["_OUROBOROS_NESTED"] == "1"
        assert "OUROBOROS_LLM_BACKEND" not in env
        assert "OUROBOROS_RUNTIME" not in env

    @pytest.mark.asyncio
    async def test_complete_success_from_goose_stream_json(self) -> None:
        adapter = GooseCliLLMAdapter(cli_path="/tmp/goose", cwd="/tmp/project")
        fake_processes: list[_FakeProcess] = []

        async def fake_create_subprocess_exec(*command: str, **kwargs: Any) -> _FakeProcess:
            assert command[:4] == ("/tmp/goose", "run", "--output-format", "stream-json")
            assert kwargs["cwd"] == "/tmp/project"
            process = _FakeProcess(
                stdout=_jsonl(
                    {"type": "init", "session_id": "sess-123"},
                    {"type": "message", "role": "assistant", "content": "Final answer"},
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
        assert result.value.model == "default"
        assert result.value.raw_response["session_id"] == "sess-123"
        assert fake_processes[0].stdin.closed is True
        assert b"Summarize this" in fake_processes[0].stdin.data

    @pytest.mark.asyncio
    async def test_complete_accumulates_stream_chunks_for_completion_fallback(self) -> None:
        adapter = GooseCliLLMAdapter(cli_path="/tmp/goose", cwd="/tmp/project")

        async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout=_jsonl(
                    {"type": "init", "session_id": "sess-123"},
                    {"type": "message", "role": "assistant", "content": '{"ok":'},
                    {"type": "message", "role": "assistant", "content": " true}"},
                )
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Return JSON.")],
                CompletionConfig(model="default"),
            )

        assert result.is_ok
        assert result.value.content == '{"ok": true}'

    @pytest.mark.asyncio
    async def test_complete_extracts_json_when_response_format_requested(self) -> None:
        adapter = GooseCliLLMAdapter(cli_path="/tmp/goose")

        async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout=_jsonl(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": '```json\n{"approved": true}\n```',
                    }
                )
            )

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

    @pytest.mark.asyncio
    async def test_complete_reports_goose_errors(self) -> None:
        adapter = GooseCliLLMAdapter(cli_path="/tmp/goose")

        async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
            return _FakeProcess(
                stdout=_jsonl({"type": "error", "message": "goose failure"}),
                stderr="stderr details",
                returncode=2,
            )

        with patch(
            "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            result = await adapter.complete(
                [Message(role=MessageRole.USER, content="Do thing.")],
                CompletionConfig(model="default"),
            )

        assert result.is_err
        assert result.error.provider == "goose_cli"
        assert "goose failure" in result.error.message
        assert result.error.details["returncode"] == 2

    def test_cli_path_uses_env_or_config_helper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OUROBOROS_GOOSE_CLI_PATH", "/tmp/goose")
        monkeypatch.setenv("PATH", os.environ.get("PATH", ""))

        adapter = GooseCliLLMAdapter()

        assert adapter._cli_path == "/tmp/goose"
