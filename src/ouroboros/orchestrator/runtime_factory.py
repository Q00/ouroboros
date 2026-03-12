"""Factory helpers for orchestrator agent runtimes."""

from __future__ import annotations

from pathlib import Path

from ouroboros.config import (
    get_agent_permission_mode,
    get_agent_runtime_backend,
    get_cli_path,
    get_codex_cli_path,
)
from ouroboros.orchestrator.adapter import AgentRuntime, ClaudeAgentAdapter
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.command_dispatcher import create_codex_command_dispatcher

_CLAUDE_BACKENDS = {"claude", "claude_code"}
_CODEX_BACKENDS = {"codex", "codex_cli"}


def resolve_agent_runtime_backend(backend: str | None = None) -> str:
    """Resolve and validate the orchestrator runtime backend name."""
    candidate = (backend or get_agent_runtime_backend()).strip().lower()
    if candidate in _CLAUDE_BACKENDS:
        return "claude"
    if candidate in _CODEX_BACKENDS:
        return "codex"

    msg = f"Unsupported orchestrator runtime backend: {candidate}"
    raise ValueError(msg)


def create_agent_runtime(
    *,
    backend: str | None = None,
    permission_mode: str | None = None,
    model: str | None = None,
    cli_path: str | Path | None = None,
    cwd: str | Path | None = None,
    llm_backend: str | None = None,
) -> AgentRuntime:
    """Create an orchestrator agent runtime from config or explicit options."""
    resolved_backend = resolve_agent_runtime_backend(backend)
    resolved_permission_mode = permission_mode or get_agent_permission_mode()
    if resolved_backend == "claude":
        return ClaudeAgentAdapter(
            permission_mode=resolved_permission_mode,
            model=model,
            cwd=cwd,
            cli_path=cli_path or get_cli_path(),
        )

    return CodexCliRuntime(
        cli_path=cli_path or get_codex_cli_path(),
        permission_mode=resolved_permission_mode,
        model=model,
        cwd=cwd,
        skill_dispatcher=create_codex_command_dispatcher(
            cwd=cwd,
            runtime_backend=resolved_backend,
            llm_backend=llm_backend,
        ),
        llm_backend=llm_backend,
    )


__all__ = ["create_agent_runtime", "resolve_agent_runtime_backend"]
