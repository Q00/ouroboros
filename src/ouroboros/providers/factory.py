"""Factory helpers for LLM-only provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from ouroboros.config import (
    get_codex_cli_path,
    get_gemini_cli_path,
    get_llm_backend,
    get_llm_permission_mode,
    get_runtime_profile,
)
from ouroboros.providers.base import LLMAdapter
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter
from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter

if TYPE_CHECKING:
    from ouroboros.events.io_recorder import IOJournalRecorder

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
# Gemini: ``GeminiCLIAdapter`` prepends a ``<tool_envelope>`` directive
# to the system prompt and emits
# ``gemini_cli_adapter.tool_envelope_violation`` for any out-of-envelope
# ``tool_use`` stream event.  Hard enforcement would need a Gemini CLI
# flag that does not exist.
#
# OpenCode: ``OpenCodeLLMAdapter`` injects a ``## Tool Constraints``
# section into the composed prompt and emits
# ``opencode_adapter.tool_envelope_violation`` for any ``tool_use``
# event outside the envelope.  The ``opencode run`` CLI has no
# ``--permission-mode``/``--allowed-tools`` flag either (see the
# adapter docstring), so enforcement is cooperative here as well.
#
# Claude Code and Codex remain hard-enforced (SDK ``allowed_tools`` and
# ``--sandbox`` respectively).  LiteLLM is *not* listed: it is a
# completion-only API that never executes tools from the adapter, so an
# envelope has nothing to restrict on that path (enforcement is
# vacuously satisfied).
_BACKENDS_WITH_SOFT_TOOL_ENFORCEMENT: frozenset[str] = frozenset({"gemini", "opencode"})


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
    io_recorder: IOJournalRecorder | None = None,
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
    if io_recorder is not None and resolved_backend != "litellm":
        log.warning(
            "create_llm_adapter.io_recorder_unsupported_backend",
            backend=resolved_backend,
            hint="Only LiteLLM currently accepts adapter-level IOJournalRecorder wiring.",
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
            runtime_profile=get_runtime_profile(),
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
        io_recorder=io_recorder,
    )


__all__ = ["create_llm_adapter", "resolve_llm_backend", "resolve_llm_permission_mode"]
