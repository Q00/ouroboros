"""Factory helpers for orchestrator agent runtimes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ouroboros.backends import resolve_runtime_backend_name, runtime_backend_choices
from ouroboros.backends.factory_registry import get_backend_factory_spec
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
    get_zcode_cli_path,
)
from ouroboros.orchestrator.adapter import AgentRuntime, ClaudeAgentAdapter
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime
from ouroboros.orchestrator.command_dispatcher import create_codex_command_dispatcher
from ouroboros.orchestrator.opencode_runtime import OpenCodeRuntime

_SUPPORTED_BACKENDS = runtime_backend_choices()


@dataclass(frozen=True, slots=True)
class _AgentRuntimeRequest:
    backend: str
    permission_mode: str
    model: str | None
    cli_path: str | Path | None
    cwd: str | Path | None
    llm_backend: str
    runtime_kwargs: dict[str, object] | None
    startup_output_timeout_seconds: float | None
    stdout_idle_timeout_seconds: float | None


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


def _runtime_kwargs(request: _AgentRuntimeRequest) -> dict[str, object]:
    if request.runtime_kwargs is None:
        msg = f"Runtime kwargs were not prepared for backend: {request.backend}"
        raise RuntimeError(msg)
    return request.runtime_kwargs


def _create_claude_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    return ClaudeAgentAdapter(
        permission_mode=request.permission_mode,
        model=request.model,
        cwd=request.cwd,
        cli_path=request.cli_path or get_cli_path(),
    )


def _create_codex_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    return CodexCliRuntime(
        cli_path=request.cli_path or get_codex_cli_path(),
        runtime_profile=get_runtime_profile(),
        startup_output_timeout_seconds=request.startup_output_timeout_seconds,
        stdout_idle_timeout_seconds=request.stdout_idle_timeout_seconds,
        **_runtime_kwargs(request),
    )


def _create_codex_mcp_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.config import get_native_session_index_enabled
    from ouroboros.orchestrator.codex_mcp_runtime import build_codex_mcp_worker_runtime

    return build_codex_mcp_worker_runtime(
        cli_path=request.cli_path or get_codex_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        model=request.model,
        llm_backend=request.llm_backend,
        # Dashboard is the default worker view; only dump workers into the
        # Codex app's session list when the human explicitly opts in.
        index_sessions=get_native_session_index_enabled(),
    )


def _create_claude_mcp_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.config import get_native_session_index_enabled
    from ouroboros.orchestrator.claude_worker_runtime import build_claude_worker_runtime

    return build_claude_worker_runtime(
        cli_path=request.cli_path or get_cli_path(),
        cwd=request.cwd,
        permission_mode=request.permission_mode,
        model=request.model,
        llm_backend=request.llm_backend,
        # Dashboard is the default worker view; only persist (→ visible &
        # resumable in /resume, with fork + --name) when the human opts in.
        persist_sessions=get_native_session_index_enabled(),
    )


def _create_opencode_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.config import get_opencode_cli_path

    # OpenCodeRuntime is the SUBPROCESS orchestrator (`ouroboros run`).
    # It shells out to `opencode run --pure` — no bridge plugin exists
    # in that context.  Hardcode "subprocess" so handlers never emit
    # dead _subagent envelopes, regardless of what config.yaml says.
    # Plugin mode is exclusively an MCP-server concern (composition
    # root in create_ouroboros_server reads config there).
    return OpenCodeRuntime(
        cli_path=request.cli_path or get_opencode_cli_path(),
        opencode_mode="subprocess",
        stdout_idle_timeout_seconds=(
            request.stdout_idle_timeout_seconds
            if request.stdout_idle_timeout_seconds is not None
            else get_opencode_stdout_idle_timeout_seconds()
        ),
        **_runtime_kwargs(request),
    )


def _create_hermes_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.orchestrator.hermes_runtime import HermesCliRuntime

    return HermesCliRuntime(
        cli_path=request.cli_path or get_hermes_cli_path(),
        startup_output_timeout_seconds=request.startup_output_timeout_seconds,
        stdout_idle_timeout_seconds=request.stdout_idle_timeout_seconds,
        **_runtime_kwargs(request),
    )


def _create_gemini_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.config import get_gemini_cli_path
    from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime

    return GeminiCLIRuntime(
        cli_path=request.cli_path or get_gemini_cli_path(),
        **_runtime_kwargs(request),
    )


def _create_antigravity_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.config import get_antigravity_cli_path
    from ouroboros.orchestrator.antigravity_cli_runtime import AntigravityCLIRuntime

    return AntigravityCLIRuntime(
        cli_path=request.cli_path or get_antigravity_cli_path(),
        **_runtime_kwargs(request),
    )


def _create_grok_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.config import get_grok_cli_path
    from ouroboros.orchestrator.grok_cli_runtime import GrokCliRuntime

    return GrokCliRuntime(
        cli_path=request.cli_path or get_grok_cli_path(),
        startup_output_timeout_seconds=request.startup_output_timeout_seconds,
        stdout_idle_timeout_seconds=request.stdout_idle_timeout_seconds,
        **_runtime_kwargs(request),
    )


def _create_kiro_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.orchestrator.kiro_adapter import KiroAgentAdapter

    return KiroAgentAdapter(
        cli_path=request.cli_path or get_kiro_cli_path(),
        **_runtime_kwargs(request),
    )


def _create_copilot_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.orchestrator.copilot_cli_runtime import CopilotCliRuntime

    return CopilotCliRuntime(
        cli_path=request.cli_path or get_copilot_cli_path(),
        runtime_profile=get_runtime_profile(),
        **_runtime_kwargs(request),
    )


def _create_goose_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.orchestrator.goose_runtime import GooseCliRuntime

    return GooseCliRuntime(
        cli_path=request.cli_path or get_goose_cli_path(),
        startup_output_timeout_seconds=request.startup_output_timeout_seconds,
        stdout_idle_timeout_seconds=request.stdout_idle_timeout_seconds,
        **_runtime_kwargs(request),
    )


def _create_pi_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.config import get_pi_cli_path
    from ouroboros.orchestrator.pi_runtime import PiRuntime

    return PiRuntime(
        cli_path=request.cli_path or get_pi_cli_path(),
        startup_output_timeout_seconds=request.startup_output_timeout_seconds,
        stdout_idle_timeout_seconds=request.stdout_idle_timeout_seconds,
        **_runtime_kwargs(request),
    )


def _create_gjc_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.orchestrator.gjc_runtime import GjcRuntime

    return GjcRuntime(
        cli_path=request.cli_path or get_gjc_cli_path(),
        startup_output_timeout_seconds=request.startup_output_timeout_seconds,
        stdout_idle_timeout_seconds=request.stdout_idle_timeout_seconds,
        **_runtime_kwargs(request),
    )


def _create_zcode_runtime(request: _AgentRuntimeRequest) -> AgentRuntime:
    from ouroboros.orchestrator.zcode_cli_runtime import ZcodeCLIRuntime

    return ZcodeCLIRuntime(
        cli_path=request.cli_path or get_zcode_cli_path(),
        startup_output_timeout_seconds=request.startup_output_timeout_seconds,
        stdout_idle_timeout_seconds=request.stdout_idle_timeout_seconds,
        **_runtime_kwargs(request),
    )


_AGENT_RUNTIME_FACTORIES: dict[str, Callable[[_AgentRuntimeRequest], AgentRuntime]] = {
    "_create_claude_runtime": _create_claude_runtime,
    "_create_codex_runtime": _create_codex_runtime,
    "_create_codex_mcp_runtime": _create_codex_mcp_runtime,
    "_create_claude_mcp_runtime": _create_claude_mcp_runtime,
    "_create_opencode_runtime": _create_opencode_runtime,
    "_create_hermes_runtime": _create_hermes_runtime,
    "_create_gemini_runtime": _create_gemini_runtime,
    "_create_antigravity_runtime": _create_antigravity_runtime,
    "_create_grok_runtime": _create_grok_runtime,
    "_create_kiro_runtime": _create_kiro_runtime,
    "_create_copilot_runtime": _create_copilot_runtime,
    "_create_goose_runtime": _create_goose_runtime,
    "_create_pi_runtime": _create_pi_runtime,
    "_create_gjc_runtime": _create_gjc_runtime,
    "_create_zcode_runtime": _create_zcode_runtime,
}


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
    runtime_kwargs = None
    if resolved_backend != "claude":
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

    spec = get_backend_factory_spec(resolved_backend, kind="runtime")
    if spec is None or spec.agent_runtime_factory is None:
        msg = (
            f"Unsupported orchestrator runtime backend: {resolved_backend}. "
            f"Supported backends: {', '.join(_SUPPORTED_BACKENDS)}"
        )
        raise ValueError(msg)
    builder = _AGENT_RUNTIME_FACTORIES[spec.agent_runtime_factory]
    return builder(
        _AgentRuntimeRequest(
            backend=resolved_backend,
            permission_mode=resolved_permission_mode,
            model=model,
            cli_path=cli_path,
            cwd=cwd,
            llm_backend=resolved_llm_backend,
            runtime_kwargs=runtime_kwargs,
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
        )
    )


__all__ = ["create_agent_runtime", "resolve_agent_runtime_backend"]
