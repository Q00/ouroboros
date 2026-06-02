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


def test_accumulates_pi_streaming_deltas() -> None:
    adapter = PiLLMAdapter(cli_path="/tmp/pi", cwd="/tmp/project")

    content = adapter._update_last_content("", "Hello")
    content = adapter._update_last_content(content, " world")
    content = adapter._update_last_content(content, "\nnext")

    assert content == "Hello world\nnext"


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
