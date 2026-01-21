"""Base protocol and models for LLM provider adapters.

This module defines the LLMAdapter protocol and associated data models for
communicating with LLM providers in a unified way.
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result


class MessageRole(StrEnum):
    """Role of a message in the conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Message:
    """A single message in a conversation.

    Attributes:
        role: The role of the message sender.
        content: The text content of the message.
    """

    role: MessageRole
    content: str

    def to_dict(self) -> dict[str, str]:
        """Convert message to dict format for LLM API calls.

        Returns:
            Dictionary with 'role' and 'content' keys.
        """
        return {"role": self.role.value, "content": self.content}


@dataclass(frozen=True, slots=True)
class CompletionConfig:
    """Configuration for LLM completion requests.

    Attributes:
        model: The model identifier (e.g., 'openrouter/openai/gpt-4').
        temperature: Sampling temperature (0.0-2.0). Default 0.7.
        max_tokens: Maximum tokens to generate. Default 4096.
        stop: Optional stop sequences.
        top_p: Nucleus sampling parameter. Default 1.0.
    """

    model: str
    temperature: float = 0.7
    max_tokens: int = 4096
    stop: list[str] | None = None
    top_p: float = 1.0


@dataclass(frozen=True, slots=True)
class UsageInfo:
    """Token usage information from a completion.

    Attributes:
        prompt_tokens: Number of tokens in the prompt.
        completion_tokens: Number of tokens in the completion.
        total_tokens: Total tokens used (prompt + completion).
    """

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class CompletionResponse:
    """Response from an LLM completion request.

    Attributes:
        content: The generated text content.
        model: The model that generated the response.
        usage: Token usage information.
        finish_reason: Why the generation stopped (e.g., 'stop', 'length').
        raw_response: Optional raw response from the provider for debugging.
    """

    content: str
    model: str
    usage: UsageInfo
    finish_reason: str = "stop"
    raw_response: dict[str, object] = field(default_factory=dict)


class LLMAdapter(Protocol):
    """Protocol for LLM provider adapters.

    All LLM adapters must implement this protocol to provide a unified
    interface for making completion requests.

    Example:
        adapter: LLMAdapter = LiteLLMAdapter(api_key="...")
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="Hello!")],
            config=CompletionConfig(model="openrouter/openai/gpt-4"),
        )
        if result.is_ok:
            print(result.value.content)
        else:
            log.error("LLM call failed", error=result.error)
    """

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request to the LLM provider.

        This method handles retries internally and converts all expected
        failures to Result.err(ProviderError). Exceptions should only
        occur for programming errors (bugs).

        Args:
            messages: The conversation messages to send.
            config: Configuration for the completion request.

        Returns:
            Result containing either the completion response or a ProviderError.
        """
        ...
