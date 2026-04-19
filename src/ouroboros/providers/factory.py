"""Factory helpers for LLM-only provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import structlog

from ouroboros.config import (
    get_codex_cli_path,
    get_gemini_cli_path,
    get_llm_backend,
    get_llm_permission_mode,
)
from ouroboros.providers.base import LLMAdapter
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter
from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter

log = structlog.get_logger(__name__)

_CLAUDE_CODE_BACKENDS = {"claude", "claude_code"}
_CODEX_BACKENDS = {"codex", "codex_cli"}
_GEMINI_BACKENDS = {"gemini", "gemini_cli"}
_OPENCODE_BACKENDS = {"opencode", "opencode_cli"}
_LITELLM_BACKENDS = {"litellm", "openai", "openrouter"}
_LLM_USE_CASES = frozenset({"default", "interview"})

# Resolved backend names whose adapter enforces the ``allowed_tools``
# envelope *softly* — the restriction is injected into the prompt rather
# than into a hard CLI/SDK flag, because the underlying runtime has no
# native allow-listing surface.  Callers still pass the envelope normally;
# the adapter is responsible for making the trade-off visible (structured
# warnings on init and on per-event violations, audit metadata marking the
# session as soft-enforced).
#
# Gemini is the concrete case today: ``GeminiCLIAdapter`` cooperates by
# prepending a ``<tool_envelope>`` directive to the system prompt and
# emits ``gemini_cli_adapter.tool_envelope_violation`` for any ``tool_use``
# stream event that names a tool outside the envelope.  Hard enforcement
# would require either a Gemini CLI flag that does not exist yet or a
# sandboxed subprocess surface that is out of scope for this slice.
#
# Claude/Codex/OpenCode enforce hard (SDK ``allowed_tools``, CLI
# ``--sandbox``).  LiteLLM is not listed at all because it is a
# completion-only API that never executes tools from the adapter —
# enforcement is vacuously satisfied on that path.
_BACKENDS_WITH_SOFT_TOOL_ENFORCEMENT: frozenset[str] = frozenset({"gemini"})


def resolve_llm_backend(backend: str | None = None) -> str:
    """Resolve and validate the LLM adapter backend name."""
    candidate = (backend or get_llm_backend()).strip().lower()
    if candidate in _CLAUDE_CODE_BACKENDS:
        return "claude_code"
    if candidate in _CODEX_BACKENDS:
        return "codex"
    if candidate in _GEMINI_BACKENDS:
        return "gemini"
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
    if use_case == "interview" and resolved in ("claude_code", "codex", "gemini", "opencode"):
        # Interview uses LLM to generate questions — no file writes, but
        # CLI sandbox modes block LLM output entirely. Must bypass.
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
    # Backends in ``_BACKENDS_WITH_SOFT_TOOL_ENFORCEMENT`` accept the
    # envelope but enforce it via prompt injection + post-hoc detection
    # rather than a hard runtime flag.  The session role's UX stays
    # uninterrupted (no fail-fast in user-facing flows), while the
    # trade-off surfaces as a structured warning at adapter
    # construction and per-violation events at runtime.  Operators can
    # tell a soft-enforced session apart from a hard one at audit time.
    if allowed_tools is not None and resolved_backend in _BACKENDS_WITH_SOFT_TOOL_ENFORCEMENT:
        log.warning(
            "create_llm_adapter.soft_tool_enforcement_backend",
            backend=resolved_backend,
            allowed_tools=list(allowed_tools),
            hint=(
                "This backend has no hard allowed_tools surface.  Envelope "
                "is injected as a prompt directive and violations are "
                "detected post-hoc.  Use claude_code / codex / opencode "
                "if hard enforcement is required."
            ),
        )
    resolved_permission_mode = resolve_llm_permission_mode(
        backend=resolved_backend,
        permission_mode=permission_mode,
        use_case=use_case,
    )
    if resolved_backend == "claude_code":
        return ClaudeCodeAdapter(
            permission_mode=resolved_permission_mode,
            cli_path=cli_path,
            cwd=cwd,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            timeout=timeout,
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
    if resolved_backend == "gemini":
        return GeminiCLIAdapter(
            cli_path=cli_path or get_gemini_cli_path(),
            cwd=cwd,
            max_turns=max_turns,
            on_message=on_message,
            timeout=timeout,
            max_retries=max_retries,
            allowed_tools=allowed_tools,
        )
    if resolved_backend == "opencode":
        return OpenCodeLLMAdapter(
            cli_path=cli_path,
            cwd=cwd,
            permission_mode=resolved_permission_mode,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            timeout=timeout,
            max_retries=max_retries,
        )
    # litellm is the fallback
    try:
        from ouroboros.providers.litellm_adapter import LiteLLMAdapter
    except ImportError as exc:
        msg = (
            "litellm backend requested but litellm is not installed. "
            "Install with: pip install 'ouroboros-ai[litellm]'"
        )
        raise RuntimeError(msg) from exc

    return LiteLLMAdapter(
        api_key=api_key,
        api_base=api_base,
        timeout=timeout,
        max_retries=max_retries,
    )


__all__ = ["create_llm_adapter", "resolve_llm_backend", "resolve_llm_permission_mode"]
