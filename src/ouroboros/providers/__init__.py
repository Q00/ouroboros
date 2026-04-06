"""LLM provider adapters for Ouroboros.

This module provides unified access to LLM providers through the LLMAdapter
protocol, plus factory helpers for selecting local Claude Code or LiteLLM-backed
providers from configuration.
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
from ouroboros.providers.factory import (
    create_llm_adapter,
    resolve_llm_backend,
    resolve_llm_permission_mode,
)


def __getattr__(name: str) -> object:
    """Lazy import for optional adapters to avoid hard dependency on optional packages."""
    if name == "LiteLLMAdapter":
        from ouroboros.providers.litellm_adapter import LiteLLMAdapter

        return LiteLLMAdapter
    if name == "CodexCliLLMAdapter":
        from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter

        return CodexCliLLMAdapter
    if name == "OpenCodeLLMAdapter":
        from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter

        return OpenCodeLLMAdapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
    "CodexCliLLMAdapter",
    "OpenCodeLLMAdapter",
    "LiteLLMAdapter",
    # Factory helpers
    "create_llm_adapter",
    "resolve_llm_backend",
    "resolve_llm_permission_mode",
]
