"""LLM provider adapters for Ouroboros.

This module provides unified access to LLM providers through the LLMAdapter
protocol. The default adapter is AnthropicAdapter (direct Claude API calls).
LiteLLMAdapter is available for multi-provider routing via OpenRouter.
"""

from ouroboros.providers.anthropic_adapter import AnthropicAdapter
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    LLMAdapter,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

__all__ = [
    # Protocol
    "LLMAdapter",
    # Models
    "Message",
    "MessageRole",
    "CompletionConfig",
    "CompletionResponse",
    "UsageInfo",
    # Implementations (AnthropicAdapter is the recommended default)
    "AnthropicAdapter",
    "LiteLLMAdapter",
]
