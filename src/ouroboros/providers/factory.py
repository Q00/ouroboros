"""Factory helpers for LLM-only provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ouroboros.config import (
    get_codex_cli_path,
    get_llm_backend,
    get_llm_permission_mode,
)
from ouroboros.providers.base import LLMAdapter
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter

# TODO: uncomment when OpenCode adapter is shipped
# from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter

_CLAUDE_CODE_BACKENDS = {"claude", "claude_code"}
_CODEX_BACKENDS = {"codex", "codex_cli"}
_OPENCODE_BACKENDS = {"opencode", "opencode_cli"}
_LITELLM_BACKENDS = {"litellm", "openai", "openrouter"}
_LLM_USE_CASES = frozenset({"default", "interview"})


def resolve_llm_backend(backend: str | None = None) -> str:
    """Resolve and validate the LLM adapter backend name."""
    candidate = (backend or get_llm_backend()).strip().lower()
    if candidate in _CLAUDE_CODE_BACKENDS:
        return "claude_code"
    if candidate in _CODEX_BACKENDS:
        return "codex"
    if candidate in _OPENCODE_BACKENDS:
        return "opencode"
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
    if use_case == "interview" and resolved in ("claude_code", "codex", "opencode"):
        # Interview uses LLM to generate questions — no file writes, but
        # codex read-only sandbox blocks LLM output entirely. Must bypass.
        return "bypassPermissions"

    return get_llm_permission_mode(backend=resolved)


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
    timeout: float | None = None,
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
            cli_path=cli_path or get_codex_cli_path(),
            cwd=cwd,
            permission_mode=resolved_permission_mode,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            timeout=timeout,
            max_retries=max_retries,
        )
    if resolved_backend == "opencode":
        msg = (
            "OpenCode LLM adapter is not yet available. "
            "Supported backends: claude_code, codex, litellm"
        )
        raise NotImplementedError(msg)

    from ouroboros.providers.litellm_adapter import LiteLLMAdapter

    return LiteLLMAdapter(
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        max_retries=max_retries,
    )


__all__ = ["create_llm_adapter", "resolve_llm_backend", "resolve_llm_permission_mode"]
