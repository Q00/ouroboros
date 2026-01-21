"""Unit tests for ouroboros.providers.base module."""

import pytest

from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)


class TestMessageRole:
    """Test MessageRole enum."""

    def test_message_role_values(self) -> None:
        """MessageRole has expected string values."""
        assert MessageRole.SYSTEM == "system"
        assert MessageRole.USER == "user"
        assert MessageRole.ASSISTANT == "assistant"

    def test_message_role_is_str(self) -> None:
        """MessageRole values are strings."""
        assert isinstance(MessageRole.SYSTEM.value, str)
        assert isinstance(MessageRole.USER.value, str)
        assert isinstance(MessageRole.ASSISTANT.value, str)


class TestMessage:
    """Test Message dataclass."""

    def test_message_creation(self) -> None:
        """Message can be created with role and content."""
        msg = Message(role=MessageRole.USER, content="Hello!")

        assert msg.role == MessageRole.USER
        assert msg.content == "Hello!"

    def test_message_is_frozen(self) -> None:
        """Message is immutable."""
        msg = Message(role=MessageRole.USER, content="Hello!")

        with pytest.raises(AttributeError):
            msg.content = "Changed"  # type: ignore[misc]

    def test_message_to_dict(self) -> None:
        """Message.to_dict() returns correct dictionary."""
        msg = Message(role=MessageRole.SYSTEM, content="You are helpful.")

        result = msg.to_dict()

        assert result == {"role": "system", "content": "You are helpful."}

    def test_message_to_dict_all_roles(self) -> None:
        """Message.to_dict() works for all roles."""
        messages = [
            Message(role=MessageRole.SYSTEM, content="System"),
            Message(role=MessageRole.USER, content="User"),
            Message(role=MessageRole.ASSISTANT, content="Assistant"),
        ]

        results = [m.to_dict() for m in messages]

        assert results == [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "User"},
            {"role": "assistant", "content": "Assistant"},
        ]


class TestCompletionConfig:
    """Test CompletionConfig dataclass."""

    def test_completion_config_defaults(self) -> None:
        """CompletionConfig has sensible defaults."""
        config = CompletionConfig(model="gpt-4")

        assert config.model == "gpt-4"
        assert config.temperature == 0.7
        assert config.max_tokens == 4096
        assert config.stop is None
        assert config.top_p == 1.0

    def test_completion_config_custom_values(self) -> None:
        """CompletionConfig accepts custom values."""
        config = CompletionConfig(
            model="openrouter/anthropic/claude-3-opus",
            temperature=0.3,
            max_tokens=1000,
            stop=["###", "END"],
            top_p=0.9,
        )

        assert config.model == "openrouter/anthropic/claude-3-opus"
        assert config.temperature == 0.3
        assert config.max_tokens == 1000
        assert config.stop == ["###", "END"]
        assert config.top_p == 0.9

    def test_completion_config_is_frozen(self) -> None:
        """CompletionConfig is immutable."""
        config = CompletionConfig(model="gpt-4")

        with pytest.raises(AttributeError):
            config.model = "changed"  # type: ignore[misc]


class TestUsageInfo:
    """Test UsageInfo dataclass."""

    def test_usage_info_creation(self) -> None:
        """UsageInfo can be created with token counts."""
        usage = UsageInfo(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
        )

        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

    def test_usage_info_is_frozen(self) -> None:
        """UsageInfo is immutable."""
        usage = UsageInfo(prompt_tokens=10, completion_tokens=5, total_tokens=15)

        with pytest.raises(AttributeError):
            usage.total_tokens = 100  # type: ignore[misc]


class TestCompletionResponse:
    """Test CompletionResponse dataclass."""

    def test_completion_response_creation(self) -> None:
        """CompletionResponse can be created with required fields."""
        usage = UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        response = CompletionResponse(
            content="Hello, how can I help?",
            model="gpt-4",
            usage=usage,
        )

        assert response.content == "Hello, how can I help?"
        assert response.model == "gpt-4"
        assert response.usage == usage
        assert response.finish_reason == "stop"  # default
        assert response.raw_response == {}  # default

    def test_completion_response_custom_finish_reason(self) -> None:
        """CompletionResponse accepts custom finish_reason."""
        usage = UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        response = CompletionResponse(
            content="Output truncated...",
            model="gpt-4",
            usage=usage,
            finish_reason="length",
        )

        assert response.finish_reason == "length"

    def test_completion_response_with_raw_response(self) -> None:
        """CompletionResponse can store raw provider response."""
        usage = UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        raw = {"id": "chatcmpl-123", "object": "chat.completion"}
        response = CompletionResponse(
            content="Hello!",
            model="gpt-4",
            usage=usage,
            raw_response=raw,
        )

        assert response.raw_response == raw

    def test_completion_response_is_frozen(self) -> None:
        """CompletionResponse is immutable."""
        usage = UsageInfo(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        response = CompletionResponse(content="Hello", model="gpt-4", usage=usage)

        with pytest.raises(AttributeError):
            response.content = "Changed"  # type: ignore[misc]


class TestLLMAdapterProtocol:
    """Test LLMAdapter protocol definition."""

    def test_protocol_is_importable(self) -> None:
        """LLMAdapter protocol can be imported."""
        from ouroboros.providers.base import LLMAdapter

        # Protocol exists and has complete method
        assert hasattr(LLMAdapter, "complete")

    def test_protocol_type_hints(self) -> None:
        """LLMAdapter.complete has correct type hints."""
        from typing import get_type_hints

        from ouroboros.providers.base import LLMAdapter

        hints = get_type_hints(LLMAdapter.complete)

        # Should have messages, config, and return type
        assert "messages" in hints
        assert "config" in hints
        assert "return" in hints
