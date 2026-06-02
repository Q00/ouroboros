"""Unit tests for the Pi LLM adapter."""

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
        == "partial"
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
