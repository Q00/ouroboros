"""Factory helpers for orchestrator agent runtimes."""

from __future__ import annotations

from pathlib import Path

from ouroboros.backends import resolve_runtime_backend_name, runtime_backend_choices
from ouroboros.config import (
    get_agent_permission_mode,
    get_agent_runtime_backend,
    get_cli_path,
    get_codex_cli_path,
    get_copilot_cli_path,
    get_gjc_cli_path,
    get_goose_cli_path,
    get_hermes_cli_path,
    get_kiro_cli_path,
    get_llm_backend,
    get_opencode_stdout_idle_timeout_seconds,
    get_runtime_profile,
)
from ouroboros.orchestrator.adapter import AgentRuntime, ClaudeAgentAdapter
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.command_dispatcher import create_codex_command_dispatcher
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime

_SUPPORTED_BACKENDS = runtime_backend_choices()


def resolve_agent_runtime_backend(backend: str | None = None) -> str:
    """Resolve and validate the orchestrator runtime backend name."""
    candidate = (backend or get_agent_runtime_backend()).strip().lower()
    try:
        return resolve_runtime_backend_name(candidate)
    except ValueError as exc:
        msg = (
            f"Unsupported orchestrator runtime backend: {candidate}. "
            f"Supported backends: {', '.join(_SUPPORTED_BACKENDS)}"
        )
        raise ValueError(msg) from exc


def create_agent_runtime(
    *,
    backend: str | None = None,
    permission_mode: str | None = None,
    model: str | None = None,
    cli_path: str | Path | None = None,
    cwd: str | Path | None = None,
    llm_backend: str | None = None,
    startup_output_timeout_seconds: float | None = None,
    stdout_idle_timeout_seconds: float | None = None,
) -> AgentRuntime:
    """Create an orchestrator agent runtime from config or explicit options."""
    resolved_backend = resolve_agent_runtime_backend(backend)
    resolved_permission_mode = permission_mode or get_agent_permission_mode(
        backend=resolved_backend
    )
    resolved_llm_backend = llm_backend or get_llm_backend()
    if resolved_backend == "claude":
        return ClaudeAgentAdapter(
            permission_mode=resolved_permission_mode,
            model=model,
            cwd=cwd,
            cli_path=cli_path or get_cli_path(),
        )

    runtime_kwargs = {
        "permission_mode": resolved_permission_mode,
        "model": model,
        "cwd": cwd,
        "skill_dispatcher": create_codex_command_dispatcher(
            cwd=cwd,
            runtime_backend=resolved_backend,
            llm_backend=resolved_llm_backend,
        ),
        "llm_backend": resolved_llm_backend,
    }
    if resolved_backend == "codex":
        return CodexCliRuntime(
            cli_path=cli_path or get_codex_cli_path(),
            runtime_profile=get_runtime_profile(),
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
            **runtime_kwargs,
        )

    if resolved_backend == "codex_mcp":
        # Leader-driven worker pool over `codex mcp-server` (codex/codex-reply).
        # No skill_dispatcher / timeout knobs — the worker runtime is pure
        # spawn/resume transport below the AgentRuntime seam.
        from ouroboros.config import get_native_session_index_enabled
        from ouroboros.orchestrator.codex_mcp_runtime import build_codex_mcp_worker_runtime

        return build_codex_mcp_worker_runtime(
            cli_path=cli_path or get_codex_cli_path(),
            cwd=cwd,
            permission_mode=resolved_permission_mode,
            model=model,
            llm_backend=resolved_llm_backend,
            # Dashboard is the default worker view; only dump workers into the
            # Codex app's session list when the human explicitly opts in.
            index_sessions=get_native_session_index_enabled(),
        )

    if resolved_backend == "claude_mcp":
        # Same worker pool, Claude transport (`claude -p --resume` stream-json) —
        # proves the leader-driven seam is provider-neutral.
        from ouroboros.config import get_native_session_index_enabled
        from ouroboros.orchestrator.claude_worker_runtime import build_claude_worker_runtime

        return build_claude_worker_runtime(
            cli_path=cli_path or get_cli_path(),
            cwd=cwd,
            permission_mode=resolved_permission_mode,
            model=model,
            llm_backend=resolved_llm_backend,
            # Dashboard is the default worker view; only persist (→ visible &
            # resumable in /resume, with fork + --name) when the human opts in.
            persist_sessions=get_native_session_index_enabled(),
        )

    if resolved_backend == "opencode":
        from ouroboros.config import get_opencode_cli_path

        # OpenCodeRuntime is the SUBPROCESS orchestrator (`ouroboros run`).
        # It shells out to `opencode run --pure` — no bridge plugin exists
        # in that context.  Hardcode "subprocess" so handlers never emit
        # dead _subagent envelopes, regardless of what config.yaml says.
        # Plugin mode is exclusively an MCP-server concern (composition
        # root in create_ouroboros_server reads config there).
        return OpenCodeRuntime(
            cli_path=cli_path or get_opencode_cli_path(),
            opencode_mode="subprocess",
            stdout_idle_timeout_seconds=(
                stdout_idle_timeout_seconds
                if stdout_idle_timeout_seconds is not None
                else get_opencode_stdout_idle_timeout_seconds()
            ),
            **runtime_kwargs,
        )

    if resolved_backend == "hermes":
        from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime

        return HermesCliRuntime(
            cli_path=cli_path or get_hermes_cli_path(),
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
            **runtime_kwargs,
        )

    if resolved_backend == "gemini":
        from ouroboros.config import get_gemini_cli_path
        from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime

        return GeminiCLIRuntime(
            cli_path=cli_path or get_gemini_cli_path(),
            **runtime_kwargs,
        )

    if resolved_backend == "antigravity":
        from ouroboros.config import get_antigravity_cli_path
        from ouroboros.orchestrator.antigravity_cli_runtime import AntigravityCLIRuntime

        return AntigravityCLIRuntime(
            cli_path=cli_path or get_antigravity_cli_path(),
            **runtime_kwargs,
        )

    if resolved_backend == "grok":
        from ouroboros.config import get_grok_cli_path
        from ouroboros.orchestrator.grok_cli_runtime import GrokCliRuntime

        return GrokCliRuntime(
            cli_path=cli_path or get_grok_cli_path(),
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
            **runtime_kwargs,
        )

    if resolved_backend == "kiro":
        from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

        return KiroAgentAdapter(
            cli_path=cli_path or get_kiro_cli_path(),
            **runtime_kwargs,
        )

    if resolved_backend == "copilot":
        from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime

        return CopilotCliRuntime(
            cli_path=cli_path or get_copilot_cli_path(),
            runtime_profile=get_runtime_profile(),
            **runtime_kwargs,
        )

    if resolved_backend == "goose":
        from ouroboros.orchestrator.goose_runtime import GooseCliRuntime

        return GooseCliRuntime(
            cli_path=cli_path or get_goose_cli_path(),
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
            **runtime_kwargs,
        )

    if resolved_backend == "pi":
        from ouroboros.config import get_pi_cli_path
        from ouroboros.orchestrator.pi_runtime import PiRuntime

        return PiRuntime(
            cli_path=cli_path or get_pi_cli_path(),
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
            **runtime_kwargs,
        )

    if resolved_backend == "gjc":
        from ouroboros.orchestrator.gjc_runtime import GjcRuntime

        return GjcRuntime(
            cli_path=cli_path or get_gjc_cli_path(),
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
            **runtime_kwargs,
        )

    msg = (
        f"Unsupported orchestrator runtime backend: {resolved_backend}. "
        f"Supported backends: {', '.join(_SUPPORTED_BACKENDS)}"
    )
    raise ValueError(msg)


__all__ = ["create_agent_runtime", "resolve_agent_runtime_backend"]
