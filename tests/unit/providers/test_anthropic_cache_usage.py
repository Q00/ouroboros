"""Direct Anthropic SDK cache token counts survive public completion parsing."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.config import get_default_config
from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.base import CompletionConfig, Message, MessageRole, UsageInfo


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cache_fields,expected_remainder",
    [
        ({"cache_read_input_tokens": 100}, 100),
        ({"cache_creation_input_tokens": 20}, 20),
        ({"cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}, 120),
        (
            {
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 20,
                "cache_creation": {"ephemeral_5m_input_tokens": 12, "ephemeral_1h_input_tokens": 8},
            },
            120,
        ),
        ({}, 0),
        ({"cache_read_input_tokens": None, "cache_creation_input_tokens": None}, 0),
    ],
    ids=["cache-read", "cache-write", "combined", "retention-breakdown", "missing", "none"],
)
async def test_sdk_completion_preserves_additive_cache_tokens(
    cache_fields, expected_remainder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("ouroboros.providers.profiles.load_config", get_default_config)
    adapter = AnthropicAdapter(api_key="test-placeholder")
    client = MagicMock()
    client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="Answer")],
            model="claude-sonnet-5-5",
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=4, **cache_fields),
        )
    )
    adapter._client = client
    result = await adapter.complete(
        [Message(role=MessageRole.USER, content="Question")],
        CompletionConfig(model="claude-sonnet-5-5"),
    )
    assert result.is_ok
    assert result.value.content == "Answer"
    assert result.value.usage == UsageInfo(10, 4, 14, unallocated_tokens=expected_remainder)
