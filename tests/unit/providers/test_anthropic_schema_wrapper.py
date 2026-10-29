"""Anthropic JSON steering sends the schema body, excluding format metadata."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config import get_default_config
from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.anthropic_models import response_format_instruction
from ouroboros.providers.base import CompletionConfig, Message, MessageRole

SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer", "minimum": 0, "maximum": 10}},
    "required": ["score"],
    "additionalProperties": False,
}


@pytest.mark.parametrize("wrapped", [False, True], ids=["bare-schema", "openai-wrapper"])
def test_schema_instruction_renders_caller_constraints(wrapped: bool) -> None:
    supplied = {"name": "score_result", "strict": True, "schema": SCHEMA} if wrapped else SCHEMA
    directive = response_format_instruction({"type": "json_schema", "json_schema": supplied})
    assert json.loads(directive.split("Schema: ", 1)[1]) == SCHEMA


@pytest.mark.asyncio
async def test_wrapped_schema_reaches_anthropic_request_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ouroboros.providers.profiles.load_config", get_default_config)
    adapter = AnthropicAdapter(api_key="test-placeholder")
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"score":7}')],
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
    )
    adapter._client = client
    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="Rate this result")],
        CompletionConfig(
            model="claude-sonnet-5-5",
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "score_result", "strict": True, "schema": SCHEMA},
            },
        ),
    )
    assert result.is_ok
    rendered = client.messages.create.await_args.kwargs["system"]
    assert json.loads(rendered.split("Schema: ", 1)[1]) == SCHEMA
