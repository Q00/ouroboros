"""Use installed SDK types while replacing the paid query boundary."""

import pytest

from ouroboros.providers.base import CompletionConfig, Message, MessageRole
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter

sdk = pytest.importorskip("claude_agent_sdk")


@pytest.mark.asyncio
async def test_installed_sdk_options_and_result_preserve_completion(monkeypatch) -> None:
    captured = []

    async def query(*, prompt, options):
        captured.append((prompt, options))
        yield sdk.SystemMessage(subtype="init", data={"session_id": "synthetic-session"})
        yield sdk.ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="synthetic-session",
            total_cost_usd=0.0,
            result="synthetic response",
            usage={"input_tokens": 3, "output_tokens": 5},
        )

    monkeypatch.setattr(sdk, "query", query)
    adapter = ClaudeCodeAdapter()
    result = await adapter.complete(
        [
            Message(role=MessageRole.SYSTEM, content="synthetic instruction"),
            Message(role=MessageRole.USER, content="synthetic input"),
        ],
        CompletionConfig(model="claude-sonnet-5-5", max_tokens=32),
    )

    assert result.is_ok, result.error if result.is_err else None
    assert result.value.content == "synthetic response"
    assert result.value.raw_response["session_id"] == "synthetic-session"
    assert len(captured) == 1
    prompt, options = captured[0]
    assert isinstance(options, sdk.ClaudeAgentOptions)
    assert options.model == "claude-sonnet-5-5"
    assert options.system_prompt == "synthetic instruction"
    assert "synthetic input" in prompt
    assert "synthetic instruction" not in prompt
