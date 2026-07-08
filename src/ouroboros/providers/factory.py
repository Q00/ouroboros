"""Factory helpers for LLM-only provider adapters."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from ouroboros.backends import resolve_llm_backend_name, soft_tool_enforcement_backends
from ouroboros.backends.factory_registry import get_backend_factory_spec
from ouroboros.config import (
    get_codex_cli_path,
    get_gemini_cli_path,
    get_gjc_cli_path,
    get_goose_cli_path,
    get_hermes_cli_path,
    get_llm_backend,
    get_llm_permission_mode,
    get_ourocode_cli_path,
    get_pi_cli_path,
    get_runtime_profile,
    get_zcode_cli_path,
)
from ouroboros.providers.base import LLMAdapter
from ouroboros.providers.claude_code_adapter import ClaudeCodeAdapter
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.copilot_cli_adapter import CopilotCliLLMAdapter
from ouroboros.providers.gemini_cli_adapter import GeminiCLIAdapter
from ouroboros.providers.gjc_llm_adapter import GjcLLMAdapter
from ouroboros.providers.goose_cli_adapter import GooseCliLLMAdapter
from ouroboros.providers.opencode_adapter import OpenCodeLLMAdapter
from ouroboros.providers.ourocode_llm_adapter import OurocodeLLMAdapter
from ouroboros.providers.pi_llm_adapter import PiLLMAdapter

if TYPE_CHECKING:
    from ouroboros.events.io_recorder import IOJournalRecorder

log = structlog.get_logger(__name__)

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
# Kiro is not listed globally because its behavior is conditional. Narrower
# permission modes map the envelope to native ``--trust-tools`` categories;
# ``bypassPermissions`` must instead use ``--trust-all-tools``, and the adapter
# emits its own soft-enforcement warning for that path.
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
_BACKENDS_WITH_SOFT_TOOL_ENFORCEMENT: frozenset[str] = soft_tool_enforcement_backends()


@dataclass(frozen=True, slots=True)
class _LLMAdapterRequest:
    permission_mode: str
    cli_path: str | Path | None
    cwd: str | Path | None
    allowed_tools: list[str] | None
    max_turns: int
    on_message: Callable[[str, str], None] | None
    api_key: str | None
    api_base: str | None
    timeout: float | None
    max_retries: int
    io_recorder: IOJournalRecorder | None
    strict_mcp_config: bool


def resolve_llm_backend(backend: str | None = None) -> str:
    """Resolve and validate the LLM adapter backend name."""
    candidate = (backend or get_llm_backend()).strip().lower()
    try:
        resolved = resolve_llm_backend_name(candidate)
    except ValueError as exc:
        msg = f"Unsupported LLM backend: {candidate}"
        raise ValueError(msg) from exc
    if resolved == "claude":
        return "claude_code"
    return resolved


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
    if use_case == "interview" and resolved in (
        "claude_code",
        "codex",
        "copilot",
        "gemini",
        "goose",
        "hermes",
        "opencode",
        "pi",
        "gjc",
    ):
        # Interview uses LLM to generate questions — no file writes, but
        # CLI sandbox modes block LLM output entirely. Must bypass.
        return "bypassPermissions"
    return get_llm_permission_mode(backend=resolved)


def _create_claude_code_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return ClaudeCodeAdapter(
        permission_mode=request.permission_mode,
        cli_path=request.cli_path,
        cwd=request.cwd,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        # Forwarded as-is.  The factory does NOT auto-derive
        # ``strict_mcp_config`` from ``use_case`` because non-MCP
        # interview entrypoints (CLI ``ooo init`` / ``ooo pm``) need
        # to keep plugin and project ``.mcp.json`` servers reachable.
        # Only the nested MCP-tool entrypoint
        # (``InterviewHandler.handle`` in
        # ``mcp/tools/authoring_handlers.py``) opts in.
        strict_mcp_config=request.strict_mcp_config,
    )


def _create_codex_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return CodexCliLLMAdapter(
        cli_path=request.cli_path or get_codex_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
        runtime_profile=get_runtime_profile(),
    )


def _create_copilot_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    from ouroboros.config import get_copilot_cli_path

    return CopilotCliLLMAdapter(
        cli_path=request.cli_path or get_copilot_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
        runtime_profile=get_runtime_profile(),
    )


def _create_gemini_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return GeminiCLIAdapter(
        cli_path=request.cli_path or get_gemini_cli_path(),
        cwd=request.cwd,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
        allowed_tools=request.allowed_tools,
    )


def _create_opencode_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return OpenCodeLLMAdapter(
        cli_path=request.cli_path,
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
    )


def _create_hermes_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    from ouroboros.providers.hermes_cli_adapter import HermesCliLLMAdapter

    return HermesCliLLMAdapter(
        cli_path=request.cli_path or get_hermes_cli_path(),
        cwd=request.cwd,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
    )


def _create_goose_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return GooseCliLLMAdapter(
        cli_path=request.cli_path or get_goose_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
    )


def _create_pi_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return PiLLMAdapter(
        cli_path=request.cli_path or get_pi_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
    )


def _create_gjc_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return GjcLLMAdapter(
        cli_path=request.cli_path or get_gjc_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
    )


def _create_ourocode_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    return OurocodeLLMAdapter(
        cli_path=request.cli_path or get_ourocode_cli_path(),
        cwd=request.cwd,
        timeout=request.timeout,
        io_recorder=request.io_recorder,
    )


def _create_zcode_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    from ouroboros.providers.zcode_cli_adapter import ZcodeCliLLMAdapter

    return ZcodeCliLLMAdapter(
        cli_path=request.cli_path or get_zcode_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        allowed_tools=request.allowed_tools,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
        runtime_profile=get_runtime_profile(),
    )


def _create_kiro_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    from ouroboros.config import get_kiro_cli_path
    from ouroboros.providers.kiro_adapter import KiroCodeAdapter

    return KiroCodeAdapter(
        cli_path=request.cli_path or get_kiro_cli_path(),
        cwd=request.cwd,
        allowed_tools=request.allowed_tools,
        permission_mode=request.permission_mode,
        max_turns=request.max_turns,
        on_message=request.on_message,
        timeout=request.timeout,
        max_retries=request.max_retries,
    )


def _create_litellm_adapter(request: _LLMAdapterRequest) -> LLMAdapter:
    try:
        from ouroboros.providers.litellm_adapter import LiteLLMAdapter
    except ImportError as exc:
        msg = (
            "litellm backend requested but litellm is not installed. "
            "Install with: pip install 'ouroboros-ai[litellm]'"
        )
        raise RuntimeError(msg) from exc

    return LiteLLMAdapter(
        api_key=request.api_key,
        api_base=request.api_base,
        timeout=request.timeout,
        max_retries=request.max_retries,
        io_recorder=request.io_recorder,
    )


_LLM_ADAPTER_FACTORIES: dict[str, Callable[[_LLMAdapterRequest], LLMAdapter]] = {
    "_create_claude_code_adapter": _create_claude_code_adapter,
    "_create_codex_adapter": _create_codex_adapter,
    "_create_copilot_adapter": _create_copilot_adapter,
    "_create_gemini_adapter": _create_gemini_adapter,
    "_create_opencode_adapter": _create_opencode_adapter,
    "_create_hermes_adapter": _create_hermes_adapter,
    "_create_goose_adapter": _create_goose_adapter,
    "_create_pi_adapter": _create_pi_adapter,
    "_create_gjc_adapter": _create_gjc_adapter,
    "_create_ourocode_adapter": _create_ourocode_adapter,
    "_create_kiro_adapter": _create_kiro_adapter,
    "_create_litellm_adapter": _create_litellm_adapter,
    "_create_zcode_adapter": _create_zcode_adapter,
}


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
    strict_mcp_config: bool = False,
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
                "detected post-hoc.  Use claude_code / codex "
                "if hard enforcement is required."
            ),
        )
    resolved_permission_mode = resolve_llm_permission_mode(
        backend=resolved_backend,
        permission_mode=permission_mode,
        use_case=use_case,
    )
    if io_recorder is not None and resolved_backend not in ("litellm", "ourocode"):
        log.warning(
            "create_llm_adapter.io_recorder_unsupported_backend",
            backend=resolved_backend,
            hint="Only LiteLLM and ourocode accept adapter-level IOJournalRecorder wiring.",
        )
    spec = get_backend_factory_spec(resolved_backend, kind="llm")
    if spec is None or spec.llm_adapter_factory is None:
        msg = f"Unsupported LLM backend: {resolved_backend}"
        raise ValueError(msg)
    builder = _LLM_ADAPTER_FACTORIES[spec.llm_adapter_factory]
    return builder(
        _LLMAdapterRequest(
            permission_mode=resolved_permission_mode,
            cli_path=cli_path,
            cwd=cwd,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            api_key=api_key,
            api_base=api_base,
            timeout=timeout,
            max_retries=max_retries,
            io_recorder=io_recorder,
            strict_mcp_config=strict_mcp_config,
        )
    )


__all__ = ["create_llm_adapter", "resolve_llm_backend", "resolve_llm_permission_mode"]
