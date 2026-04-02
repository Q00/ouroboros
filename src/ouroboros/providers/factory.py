"""Factory helpers for LLM-only provider adapters.

This module provides the central adapter-creation API used throughout
Ouroboros to instantiate the correct :class:`~ouroboros.providers.base.LLMAdapter`
implementation for a given backend name.

Supported backends
------------------

+---------------------+-------------------+------------------------------------------+
| Backend aliases     | Canonical name    | Adapter class                            |
+=====================+===================+==========================================+
| ``claude``          | ``claude_code``   | :class:`ClaudeCodeAdapter`               |
| ``claude_code``     |                   |                                          |
+---------------------+-------------------+------------------------------------------+
| ``codex``           | ``codex``         | :class:`CodexCliLLMAdapter`              |
| ``codex_cli``       |                   |                                          |
+---------------------+-------------------+------------------------------------------+
| ``gemini``          | ``gemini``        | :class:`GeminiCLIAdapter`                |
| ``gemini_cli``      |                   |                                          |
+---------------------+-------------------+------------------------------------------+
| ``litellm``         | ``litellm``       | ``LiteLLMAdapter`` (optional dependency) |
| ``openai``          |                   |                                          |
| ``openrouter``      |                   |                                          |
+---------------------+-------------------+------------------------------------------+

The active backend is determined by the ``OUROBOROS_LLM_BACKEND`` environment
variable (or ``llm_backend`` in ``config.yaml``).  All factory functions accept
an explicit ``backend`` override that takes precedence over the configuration.

Usage::

    from ouroboros.providers.factory import create_llm_adapter

    adapter = create_llm_adapter(backend="gemini", timeout=30.0)
    result = await adapter.complete(messages, config)
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

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

# TODO: uncomment when OpenCode adapter is shipped
# from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter

_CLAUDE_CODE_BACKENDS = {"claude", "claude_code"}
_CODEX_BACKENDS = {"codex", "codex_cli"}
_GEMINI_BACKENDS = {"gemini", "gemini_cli"}
_OPENCODE_BACKENDS = {"opencode", "opencode_cli"}
_LITELLM_BACKENDS = {"litellm", "openai", "openrouter"}
_LLM_USE_CASES = frozenset({"default", "interview"})


def resolve_llm_backend(backend: str | None = None) -> str:
    """Resolve and validate the LLM adapter backend name.

    Normalises aliases (e.g. ``"claude"`` → ``"claude_code"``, ``"gemini_cli"``
    → ``"gemini"``) so the rest of the codebase only needs to handle a small
    set of canonical names.

    When *backend* is ``None`` the value is read from the active Ouroboros
    configuration via :func:`~ouroboros.config.get_llm_backend`.

    Args:
        backend: Raw backend name supplied by the caller, or ``None`` to
            fall back to the value from environment / configuration file.

    Returns:
        One of the canonical backend strings:
        ``"claude_code"``, ``"codex"``, ``"gemini"``, or ``"litellm"``.

    Raises:
        ValueError: If the resolved backend name does not match any known
            alias, or if the ``opencode`` backend is requested (not yet
            implemented).

    Example::

        >>> resolve_llm_backend("gemini_cli")
        'gemini'
        >>> resolve_llm_backend("claude")
        'claude_code'
    """
    candidate = (backend or get_llm_backend()).strip().lower()
    if candidate in _CLAUDE_CODE_BACKENDS:
        return "claude_code"
    if candidate in _CODEX_BACKENDS:
        return "codex"
    if candidate in _GEMINI_BACKENDS:
        return "gemini"
    if candidate in _OPENCODE_BACKENDS:
        msg = (
            "OpenCode LLM adapter is not yet available. "
            "Supported backends: claude_code, codex, litellm"
        )
        raise ValueError(msg)
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
    """Resolve the permission mode for an LLM adapter construction request.

    Permission modes control how much the spawned adapter is allowed to do
    inside the Ouroboros execution sandbox (e.g. ``"default"``,
    ``"bypassPermissions"``, ``"readOnly"``).

    Resolution order
    ^^^^^^^^^^^^^^^^

    1. If *permission_mode* is given explicitly it is returned as-is.
    2. Otherwise the mode is looked up from the active Ouroboros configuration
       via :func:`~ouroboros.config.get_llm_permission_mode`.

    Special case — ``"interview"`` use case
    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

    The interview workflow only uses the LLM to generate clarifying questions
    (no file writes).  However, the Codex read-only sandbox blocks all output
    from the LLM subprocess.  To prevent silent failures the permission mode is
    automatically overridden to ``"bypassPermissions"`` for the ``claude_code``
    and ``codex`` backends when ``use_case="interview"``.

    Args:
        backend: Raw backend name (passed to :func:`resolve_llm_backend`), or
            ``None`` to use the configured default.
        permission_mode: Explicit permission mode string.  When provided it
            short-circuits all other resolution logic.
        use_case: One of ``"default"`` or ``"interview"``.  Controls whether
            the interview-specific override applies.

    Returns:
        Resolved permission mode string (e.g. ``"default"``,
        ``"bypassPermissions"``, ``"readOnly"``).

    Raises:
        ValueError: If *use_case* is not one of the supported values, or if
            :func:`resolve_llm_backend` raises for an unsupported *backend*.

    Example::

        >>> resolve_llm_permission_mode("codex", use_case="interview")
        'bypassPermissions'
        >>> resolve_llm_permission_mode("gemini", use_case="interview")
        'default'  # gemini does not need the bypass
    """
    if permission_mode:
        return permission_mode

    if use_case not in _LLM_USE_CASES:
        msg = f"Unsupported LLM use case: {use_case}"
        raise ValueError(msg)

    resolved = resolve_llm_backend(backend)
    if use_case == "interview" and resolved in ("claude_code", "codex"):
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
    """Create an :class:`~ouroboros.providers.base.LLMAdapter` from config or explicit options.

    This is the primary entry point for obtaining a configured adapter.  The
    correct implementation is selected by resolving *backend* (via
    :func:`resolve_llm_backend`) and then constructing the adapter with the
    provided options.  Parameters that are not applicable to the selected
    backend are silently ignored (e.g. *api_key* is irrelevant for CLI-based
    adapters).

    Args:
        backend: LLM backend name or alias (e.g. ``"gemini"``, ``"claude"``,
            ``"codex"``, ``"litellm"``).  Defaults to the value from
            ``OUROBOROS_LLM_BACKEND`` / ``config.yaml``.
        permission_mode: Override the permission mode for the adapter.  When
            ``None`` the mode is resolved automatically via
            :func:`resolve_llm_permission_mode`.
        use_case: Workflow context — ``"default"`` or ``"interview"``.
            Influences the automatic permission-mode selection.
        cli_path: Explicit path to the CLI binary for CLI-based adapters
            (``claude``, ``codex``, ``gemini``).  Falls back to the configured
            or ``PATH``-resolved binary when ``None``.
        cwd: Working directory for CLI subprocess invocations.  Defaults to
            the current process working directory when ``None``.
        allowed_tools: Allow-list of tool names passed to the adapter.
            ``None`` keeps the adapter's default permissive mode; ``[]``
            disables all tools.
        max_turns: Maximum conversation turns for multi-turn adapters.
            Defaults to ``1`` (single-response completion).
        on_message: Optional streaming callback invoked with ``(type, content)``
            tuples as the adapter receives partial output from the underlying
            CLI or API.
        api_key: API key for HTTP-based backends (``litellm`` / ``openai``).
            Ignored for CLI-based adapters.
        api_base: Base URL override for HTTP-based backends.  Ignored for
            CLI-based adapters.
        timeout: Per-request timeout in seconds.  ``None`` means no timeout.
        max_retries: Number of retry attempts for transient errors.  Defaults
            to ``3``.

    Returns:
        A fully constructed :class:`~ouroboros.providers.base.LLMAdapter`
        instance ready for use.

    Raises:
        ValueError: If *backend* cannot be resolved (unknown or not-yet-
            implemented alias such as ``"opencode"``).
        RuntimeError: If the ``litellm`` backend is requested but the
            ``litellm`` package is not installed.

    Example::

        from ouroboros.providers.factory import create_llm_adapter

        # Use the configured default backend
        adapter = create_llm_adapter()

        # Explicitly request the Gemini CLI adapter
        adapter = create_llm_adapter(
            backend="gemini",
            timeout=30.0,
            max_retries=2,
        )

        result = await adapter.complete(messages, config)
    """
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
            on_message=on_message,
            timeout=timeout,
            max_retries=max_retries,
        )
    # opencode is rejected at resolve time; this is a defensive fallback
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


__all__ = [
    "GeminiCLIAdapter",
    "create_llm_adapter",
    "resolve_llm_backend",
    "resolve_llm_permission_mode",
]
