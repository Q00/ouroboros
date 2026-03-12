"""Factory helpers for LLM-only provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ouroboros.config import get_llm_backend, get_llm_permission_mode
from ouroboros.providers.base import LLMAdapter
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.litellm_adapter import LiteLLMAdapter

_CLAUDE_CODE_BACKENDS = {"claude", "claude_code"}
_CODEX_BACKENDS = {"codex", "codex_cli"}
_LITELLM_BACKENDS = {"litellm", "openai", "openrouter"}
_LLM_USE_CASES = frozenset({"default", "interview"})


def resolve_llm_backend(backend: str | None = None) -> str:
    """Resolve and validate the LLM adapter backend name."""
    candidate = (backend or get_llm_backend()).strip().lower()
    if candidate in _CLAUDE_CODE_BACKENDS:
        return "claude_code"
    if candidate in _CODEX_BACKENDS:
        return "codex"
    if candidate in _LITELLM_BACKENDS:
        return "litellm"

    msg = f"Unsupported LLM backend: {candidate}"
    raise ValueError(msg)


def resolve_llm_permission_mode(
    backend: str | None = None,
    *,
    permission_mode: str | None = None,
    use_case: Literal["default", "interview"] = "default",
) -> str:
    """Resolve permission mode for an LLM adapter construction request."""
    if permission_mode:
        return permission_mode

    if use_case not in _LLM_USE_CASES:
        msg = f"Unsupported LLM use case: {use_case}"
        raise ValueError(msg)

    resolved = resolve_llm_backend(backend)
    if use_case == "interview" and resolved in ("claude_code", "codex"):
        # Interview needs broad read access regardless of backend
        return "bypassPermissions" if resolved == "claude_code" else "acceptEdits"

    return get_llm_permission_mode()


def create_llm_adapter(
    *,
    backend: str | None = None,
    permission_mode: str | None = None,
    use_case: Literal["default", "interview"] = "default",
    cli_path: str | Path | None = None,
    cwd: str | Path | None = None,
    allowed_tools: list[str] | None = None,
    max_turns: int = 1,
    on_message: Callable[[str, str], None] | None = None,
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float = 60.0,
    max_retries: int = 3,
) -> LLMAdapter:
    """Create an LLM adapter from config or explicit options."""
    resolved_backend = resolve_llm_backend(backend)
    resolved_permission_mode = resolve_llm_permission_mode(
        backend=resolved_backend,
        permission_mode=permission_mode,
        use_case=use_case,
    )
    if resolved_backend == "claude_code":
        return ClaudeCodeAdapter(
            permission_mode=resolved_permission_mode,
            cli_path=cli_path,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
        )
    if resolved_backend == "codex":
        return CodexCliLLMAdapter(
            cli_path=cli_path,
            cwd=cwd,
            permission_mode=resolved_permission_mode,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            max_retries=max_retries,
        )

    return LiteLLMAdapter(
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        max_retries=max_retries,
    )


__all__ = ["create_llm_adapter", "resolve_llm_backend", "resolve_llm_permission_mode"]
