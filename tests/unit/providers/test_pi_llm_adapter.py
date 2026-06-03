"""Unit tests for the Pi LLM adapter."""

import json
from typing import Any
from unittest.mock import patch

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.pi_llm_adapter import PiLLMAdapter


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


def _pi_jsonl(*events: dict[str, object]) -> str:
    return "".join(f"{json.dumps(event)}\n" for event in events)


def test_builds_pi_json_command_with_prompt_and_model() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    command = adapter._build_command(
        output_last_message_path="/tmp/out.txt",
        output_schema_path=None,
        model="current",
        prompt="Hello Pi",
    )

    assert command == ["/tmp/pi", "--mode", "json", "--model", "current", "Hello Pi"]


def test_builds_pi_json_command_omits_default_model_sentinel() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    command = adapter._build_command(
        output_last_message_path="/tmp/out.txt",
        output_schema_path=None,
        model="default",
        prompt="Hello Pi",
    )

    assert command == ["/tmp/pi", "--mode", "json", "Hello Pi"]


def test_extracts_pi_session_and_streaming_delta() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    assert adapter._extract_session_id_from_event({"type": "session", "id": "abc123"}) == "abc123"
    assert (
        adapter._extract_text(
            {
                "type": "message_update",
                "assistantMessageEvent": {"delta": " partial "},
            }
        )
        == " partial "
    )


def test_extracts_pi_final_messages() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    assert (
        adapter._extract_text(
            {
                "type": "agent_end",
                "messages": [{"role": "assistant", "content": "done"}],
            }
        )
        == "done"
    )


def test_extracts_pi_final_transcript_assistant_only() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    assert (
        adapter._extract_text(
            {
                "type": "agent_end",
                "messages": [
                    {"role": "user", "content": "request"},
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Done."}],
                    },
                ],
            }
        )
        == "Done."
    )


def test_accumulates_pi_streaming_deltas() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    content = adapter._update_last_content("", "Hello")
    content = adapter._update_last_content(content, " world")
    content = adapter._update_last_content(content, "\nnext")

    assert content == "Hello world\nnext"


def test_terminal_pi_final_message_replaces_accumulated_deltas() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    delta = adapter._extract_text(
        {
            "type": "message_update",
            "assistantMessageEvent": {"delta": "Hello\n"},
        }
    )
    content = adapter._update_last_content("", delta)

    final = adapter._extract_text(
        {
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": "Hello"}],
        }
    )
    content = adapter._update_last_content(content, final)

    assert content == "Hello"


def test_extracts_pi_runtime_compatible_delta_shapes() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    cases = [
        ({"type": "message_update", "assistantMessageEvent": {"text": "hello"}}, "hello"),
        ({"type": "message_update", "assistantMessageEvent": {"content": "world"}}, "world"),
        ({"type": "message_update", "content": "top content"}, "top content"),
        ({"type": "message_update", "text": "top text"}, "top text"),
        ({"type": "message_update", "delta": {"text": "dict text"}}, "dict text"),
        ({"type": "message_update", "delta": {"content": "dict content"}}, "dict content"),
    ]

    for event, expected in cases:
        assert adapter._extract_text(event) == expected


def test_ignores_documented_text_end_as_streaming_delta() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    assert (
        adapter._extract_text(
            {
                "type": "message_update",
                "assistantMessageEvent": {"type": "text_end", "content": "hello"},
            }
        )
        == ""
    )


def test_extracts_pi_runtime_compatible_final_text_shapes() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    assert (
        adapter._extract_text(
            {
                "type": "agent_end",
                "messages": [{"role": "assistant", "text": "done from text"}],
            }
        )
        == "done from text"
    )
    assert (
        adapter._extract_text(
            {
                "type": "message_end",
                "message": {"role": "assistant", "text": "message text"},
            }
        )
        == "message text"
    )
    assert (
        adapter._extract_text(
            {
                "type": "turn_end",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "list "}, "content"],
                },
            }
        )
        == "list content"
    )


def test_unsupported_pi_events_do_not_fall_back_to_event_type_text() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    assert adapter._extract_text({"type": "message_update"}) == ""
    assert adapter._extract_text({"type": "agent_end", "messages": []}) == ""


def test_pi_session_metadata_is_not_completion_text() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    session_text = adapter._extract_text({"type": "session", "id": "pi-session-123"})
    content = adapter._update_last_content("", session_text)
    delta = adapter._extract_text(
        {
            "type": "message_update",
            "assistantMessageEvent": {"delta": "assistant only"},
        }
    )
    content = adapter._update_last_content(content, delta)

    assert session_text == ""
    assert content == "assistant only"


def test_pi_partial_content_ignores_session_metadata() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    content = ""
    for event in [
        {"type": "session", "id": "pi-session-123"},
        {"type": "message_update", "delta": "partial"},
    ]:
        content = adapter._update_last_content(content, adapter._extract_text(event))

    assert content == "partial"


def test_pi_prompt_is_not_written_to_stdin() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    assert adapter._prompt_stdin_bytes("Hello Pi") is None


def test_json_object_directive_requests_json_only() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    directive = adapter._build_response_format_directive({"type": "json_object"})

    assert directive is not None
    assert "ONLY a valid JSON object" in directive


def test_json_schema_directive_includes_schema_payload() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    directive = adapter._build_response_format_directive(
        {
            "type": "json_schema",
            "json_schema": {
                "schema": {
                    "type": "object",
                    "properties": {"approved": {"type": "boolean"}},
                    "required": ["approved"],
                }
            },
        }
    )

    assert directive is not None
    assert '"approved"' in directive
    assert "JSON schema:" in directive


@pytest.mark.asyncio
async def test_structured_json_object_response_extracts_json_payload() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")
    captured_prompt: str | None = None

    async def fake_create_subprocess_exec(*command: str, **_kwargs: Any) -> _FakeProcess:
        nonlocal captured_prompt
        captured_prompt = command[-1]
        return _FakeProcess(
            stdout=_pi_jsonl(
                {"type": "session", "id": "pi-session"},
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
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout=_pi_jsonl(
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
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project", max_retries=1)

    async def fake_create_subprocess_exec(*_command: str, **_kwargs: Any) -> _FakeProcess:
        return _FakeProcess(
            stdout=_pi_jsonl(
                {
                    "type": "agent_end",
                    "messages": [{"role": "assistant", "content": '{"approved": "yes"}'}],
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
    assert result.error.provider == "pi"
    assert "non-conforming output" in result.error.message
