"""Unit tests for the Pi LLM adapter."""

import pytest

from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.pi_llm_adapter import PiLLMAdapter


def test_builds_pi_json_command_with_prompt_and_model() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    command = adapter._build_command(
        output_last_message_path="/tmp/out.txt",
        output_schema_path=None,
        model="current",
        prompt="Hello Pi",
    )

    assert command == ["/tmp/pi", "--mode", "json", "--model", "current", "Hello Pi"]


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


@pytest.mark.asyncio
async def test_rejects_structured_response_format() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    result = await adapter.complete(
        [],
        CompletionConfig(
            model="default",
            response_format={"type": "json_object"},
        ),
    )

    assert result.is_err
    assert result.error.provider == "pi"
    assert "response_format" in result.error.message
