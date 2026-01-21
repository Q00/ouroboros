"""LLM provider adapters for Ouroboros.

This module provides unified access to LLM providers through the LLMAdapter
protocol and LiteLLMAdapter implementation.
"""

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
    # Implementations
    "LiteLLMAdapter",
]
