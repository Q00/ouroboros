"""Unit tests for the OMP LLM adapter."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.omp_llm_adapter import OmpLLMAdapter


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
    def close(self) -> None:
        pass


class _FakeProcess:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdin = _FakeStdin()
        self.stdout = _FakeStream(stdout)
        self.stderr = _FakeStream(stderr)
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode


def _omp_jsonl(*events: dict[str, object]) -> str:
    return "".join(f"{json.dumps(event)}\n" for event in events)


def test_builds_omp_json_command_with_prompt_and_model() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    command = adapter._build_command(
        output_last_message_path="/tmp/out.txt",
        output_schema_path=None,
        model="current",
        prompt="Hello OMP",
    )

    assert command == ["/tmp/omp", "--mode", "json", "--model", "current", "Hello OMP"]


def test_builds_omp_json_command_omits_default_model_sentinel() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    command = adapter._build_command(
        output_last_message_path="/tmp/out.txt",
        output_schema_path=None,
        model="default",
        prompt="Hello OMP",
    )

    assert command == ["/tmp/omp", "--mode", "json", "Hello OMP"]


def test_omp_model_failure_diagnostics_do_not_claim_codex_remediation() -> None:
    """OMP inherits subprocess plumbing, not Codex App/CLI diagnostics."""
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    details = adapter._codex_failure_details(
        returncode=1,
        session_id="s-1",
        stderr="boom",
        stdout_errors=[],
        message="failed",
        cli_path="/tmp/omp",
    )

    assert details == {
        "returncode": 1,
        "session_id": "s-1",
        "stderr": "boom",
        "stdout_errors": [],
    }


def test_extracts_omp_session_and_streaming_delta() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    session_id = adapter._extract_session_id_from_event({"type": "session", "id": "omp-1"})
    delta = adapter._extract_content_delta(
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Hi"},
        }
    )

    assert session_id == "omp-1"
    assert delta == "Hi"


def test_extracts_omp_final_messages() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    text = adapter._extract_final_text(
        {
            "type": "agent_end",
            "messages": [
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
            ],
        }
    )

    assert text == "a"


def test_accumulates_omp_streaming_deltas() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    content = ""
    for event in (
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
        },
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": " world"},
        },
    ):
        content = adapter._update_last_content(content, adapter._extract_text(event))

    assert content == "Hello world"


def test_terminal_omp_final_message_replaces_accumulated_deltas() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    content = ""
    for event in (
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Hello"},
        },
        {
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": "Hello"}],
        },
    ):
        content = adapter._update_last_content(content, adapter._extract_text(event))

    assert content == "Hello"


def test_thinking_deltas_are_not_completion_text() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    assert (
        adapter._extract_text(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "thinking_delta", "delta": "internal"},
            }
        )
        == ""
    )


def test_unsupported_omp_events_do_not_fall_back_to_event_type_text() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    assert adapter._extract_text({"type": "agent_end", "messages": []}) == ""


def test_omp_session_metadata_is_not_completion_text() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    content = adapter._extract_text(
        {
            "type": "session",
            "id": "session-abc",
            "cwd": "/tmp",
        }
    )

    assert content == ""


def test_omp_prompt_is_not_written_to_stdin() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    assert adapter._prompt_stdin_bytes("Hello OMP") is None


def test_extracts_omp_zero_exit_error_event_content() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    error = adapter._extract_error_content(
        {
            "type": "turn_end",
            "message": {
                "role": "assistant",
                "stopReason": "error",
                "errorMessage": "quota exceeded",
            },
        }
    )

    assert error == "quota exceeded"


@pytest.mark.asyncio
async def test_structured_json_object_response_extracts_json_payload() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")
    captured_prompt: str | None = None

    async def fake_create_subprocess_exec(*command: str, **_kwargs: Any) -> _FakeProcess:
        nonlocal captured_prompt
        captured_prompt = command[-1]
        return _FakeProcess(
            stdout=_omp_jsonl(
                {"type": "session", "id": "omp-session"},
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": 'Sure:\n```json\n{"approved": true}\n```',
                        }
                    ],
                },
            )
        )

    with patch(
        "ouroboros.providers.codex_cli_adapter.asyncio.create_subprocess_exec",
        side_effect=fake_create_subprocess_exec,
    ):
        result = await adapter.complete(
            [Message(role=MessageRole.USER, content="Return a verdict.")],
            CompletionConfig(model="default", response_format={"type": "json_object"}),
        )

    assert result.is_ok
    assert result.value.content == '{"approved": true}'
    assert captured_prompt is not None
    assert "ONLY a valid JSON object" in captured_prompt


@pytest.mark.asyncio
async def test_structured_json_schema_response_validates_payload() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout=_omp_jsonl(
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": '{"approved": true}'}],
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
                        "required": ["approved"],
                    },
                },
            ),
        )

    assert result.is_ok
    assert json.loads(result.value.content) == {"approved": True}


@pytest.mark.asyncio
async def test_structured_json_schema_response_rejects_nonconforming_payload() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project", max_retries=1)

    async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout=_omp_jsonl(
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": "not json"}],
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
                        "required": ["approved"],
                    },
                },
            ),
        )

    assert result.is_err
    assert "non-conforming output" in result.error.message
    assert result.error.provider == "omp"


@pytest.mark.asyncio
async def test_zero_exit_omp_error_event_returns_provider_error() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout=_omp_jsonl(
                {"type": "session", "id": "omp-session"},
                {
                    "type": "agent_end",
                    "messages": [
                        {
                            "role": "assistant",
                            "stopReason": "error",
                            "errorMessage": "provider auth failed",
                        }
                    ],
                },
            )
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
    assert "provider auth failed" in result.error.message
    assert result.error.details["session_id"] == "omp-session"


@pytest.mark.asyncio
async def test_omp_completion_uses_configured_cli_path() -> None:
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    def fake_which(name: str) -> str | None:
        return "/opt/omp/bin/omp" if name == "/opt/omp/bin/omp" else None

    with (
        patch("ouroboros.config._omp_cli.get_omp_cli_path", return_value="/opt/omp/bin/omp"),
        patch("shutil.which", side_effect=fake_which),
    ):
        assert adapter._get_configured_cli_path() == "/opt/omp/bin/omp"


@pytest.mark.asyncio
async def test_omp_configured_cli_path_falls_back_to_path_when_stale() -> None:
    """PR #2299 round 5: direct adapter construction skips a stale configured path."""
    adapter = OmpLLMAdapter(cli_path="/tmp/omp", cwd="/tmp/project")

    def fake_which(name: str) -> str | None:
        return "/usr/bin/omp" if name == "omp" else None

    with (
        patch(
            "ouroboros.config._omp_cli.get_omp_cli_path",
            return_value="/missing/configured/omp",
        ),
        patch("shutil.which", side_effect=fake_which),
    ):
        assert adapter._get_configured_cli_path() == "/usr/bin/omp"
