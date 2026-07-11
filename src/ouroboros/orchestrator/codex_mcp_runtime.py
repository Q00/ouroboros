"""Codex leader-driven worker runtime over ``codex mcp-server``.

The SAME codex binary, driven as ``codex mcp-server`` (the ``codex`` /
``codex-reply`` MCP tools) instead of one-shot ``codex exec``, becomes an
externally-addressable worker pool: ouroboros calls ``codex`` to start a session
(receiving a ``threadId``) and ``codex-reply`` to continue it. This proves the
runtime×backend thesis — the codex BACKEND presents
``SubagentOrchestration.EXTERNAL_LEADER_DRIVEN`` under this RUNTIME even though
``codex exec`` (``CodexCliRuntime``) is INTERNAL.

This module is just the thin :class:`LeaderDrivenWorkerTransport`; all
``AgentRuntime`` mechanics live in :class:`LeaderDrivenWorkerRuntime`.

Resume semantics (verified 2026-06-21): ``codex mcp-server`` sessions are
PROCESS-BOUND (in-memory) — a ``threadId`` is only addressable by the
``codex-reply`` of the SAME server process that created it. With spawn-per-call
(one server process per turn, chosen for concurrency safety), a cross-process
``codex-reply`` returns "Session not found". So this transport supports
single-turn execution natively (a single ``codex`` call is itself a complete
agentic turn — codex loops internally until done); robust multi-turn resume
needs a persistent per-worker connection pool (anyio cross-task lifecycle), a
deliberate follow-up. ``resume`` surfaces a clear error rather than silently
losing thread continuity. Contrast ``CodexCliRuntime`` (``codex exec resume`` is
disk-persisted and resumes cross-process) and the Claude worker (``claude -p
--resume`` is also disk-persisted).
"""

from __future__ import annotations

import os
from typing import Any

from ouroboros.codex.cli_policy import build_codex_child_env, resolve_codex_cli_path
from ouroboros.config import get_codex_cli_path
from ouroboros.mcp.types import MCPServerConfig, TransportType
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.codex_mcp_session_pool import (
    DEFAULT_SESSION_IDLE_TIMEOUT,
    MCPSessionActor,
)
from ouroboros.orchestrator.codex_session_index import (
    derive_session_label,
    register_codex_session,
)
from ouroboros.orchestrator.worker_runtime import (
    LeaderDrivenWorkerRuntime,
    WorkerTurn,
)

log = get_logger(__name__)

# Codex enforces only these reasoning-effort levels via ``model_reasoning_effort``;
# a level outside the set is dropped, so it is advised rather than enforced.
_CODEX_REASONING_EFFORT_LEVELS: frozenset[str] = frozenset(
    {"minimal", "low", "medium", "high", "xhigh"}
)

# Codex sandbox / approval-policy values accepted by the ``codex`` MCP tool.
_SANDBOX_READ_ONLY = "read-only"
_SANDBOX_WORKSPACE_WRITE = "workspace-write"
_SANDBOX_DANGER_FULL_ACCESS = "danger-full-access"
# Permission modes that should keep the worker read-only (it must not edit files).
_READ_ONLY_PERMISSION_MODES = frozenset({"read-only", "plan", "default", "ask"})

# MCP servers disabled in spawned workers via the ``codex`` tool's ``config``
# override (a per-server deep-merge — verified to disable ONLY these while
# preserving every other native server, e.g. computer-use ``node_repl``). The
# worker is a full codex session that natively loads the user's ~/.codex MCP
# config (native passthrough); ``ouroboros`` itself is registered there, which
# would let a worker call ouroboros tools and re-enter the orchestrator. The
# ``_OUROBOROS_DEPTH`` env guard already bounds that at depth 5, but a worker
# has no reason to hold the orchestrator's own tools — disabling it removes the
# self-recursion vector entirely while keeping native passthrough for the rest.
_RECURSION_GUARD_DISABLED_MCP_SERVERS: tuple[str, ...] = ("ouroboros",)


def _map_permission_mode(permission_mode: str | None) -> tuple[str, str]:
    """Map ouroboros ``permission_mode`` → codex ``(sandbox, approval-policy)``.

    Defaults to autonomous workspace-write (an execution worker writes code).
    Explicit ``bypassPermissions`` removes both approvals and the sandbox, matching
    the Codex CLI runtime's native unrestricted mapping. A read-only-leaning mode
    keeps the worker from editing.
    """
    normalized = (permission_mode or "").strip().lower()
    if normalized in _READ_ONLY_PERMISSION_MODES:
        return _SANDBOX_READ_ONLY, "on-request"
    if normalized == "bypasspermissions":
        return _SANDBOX_DANGER_FULL_ACCESS, "never"
    return _SANDBOX_WORKSPACE_WRITE, "never"


class CodexMcpWorkerTransport:
    """Spawn/resume a Codex worker session over ``codex mcp-server``."""

    backend_name = "codex_mcp"

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        connect_timeout: float = 30.0,
        idle_timeout: float = DEFAULT_SESSION_IDLE_TIMEOUT,
        disabled_mcp_servers: tuple[str, ...] = _RECURSION_GUARD_DISABLED_MCP_SERVERS,
        index_sessions: bool = False,
        codex_home: str | None = None,
    ) -> None:
        resolution = resolve_codex_cli_path(
            explicit_cli_path=cli_path,
            configured_cli_path=get_codex_cli_path(),
            default_cli_name="codex",
            logger=log,
            log_namespace="codex_mcp_runtime",
        )
        self._cli_path = resolution.cli_path
        self._connect_timeout = connect_timeout
        self._idle_timeout = idle_timeout
        # Native passthrough keeps the worker's MCP surface, MINUS these servers
        # (recursion hardening — see _RECURSION_GUARD_DISABLED_MCP_SERVERS).
        self._disabled_mcp_servers = disabled_mcp_servers
        # When True, append each worker session to the Codex app's session index
        # so a human can SEE/resume the sub-agent in the app (best-effort).
        self._index_sessions = index_sessions
        self._codex_home = codex_home
        # threadId → persistent session actor. A worker's session stays warm
        # across turns so codex-reply reaches the SAME (process-bound) server.
        self._pool: dict[str, MCPSessionActor] = {}

    def _server_config(self) -> MCPServerConfig:
        # Strip ouroboros MCP env vars so the spawned codex server cannot recurse
        # back into the ouroboros MCP server (#185 child-env discipline).
        child_env = build_codex_child_env(
            depth_error_factory=lambda depth, max_depth: RuntimeError(
                f"Max ouroboros nesting depth ({max_depth}) exceeded (depth={depth})"
            ),
        )
        env = {k: v for k, v in child_env.items() if isinstance(v, str)}
        return MCPServerConfig(
            name="codex-mcp-worker",
            transport=TransportType.STDIO,
            command=self._cli_path,
            args=("mcp-server",),
            env=env,
            timeout=self._connect_timeout,
        )

    @staticmethod
    def _parse_turn(result: Any) -> WorkerTurn:
        structured = result.structured_content or {}
        thread_id = structured.get("threadId") if isinstance(structured, dict) else None
        text = ""
        if isinstance(structured, dict) and isinstance(structured.get("content"), str):
            text = structured["content"]
        if not text:
            text = result.text_content
        is_error = bool(result.is_error)
        return WorkerTurn(
            text=text,
            session_id=thread_id if isinstance(thread_id, str) and thread_id else None,
            is_error=is_error,
            # Codex returns "Session not found" as an is_error tool result (not an
            # MCP protocol error); carry it in ``error`` so ``resume`` can explain
            # the process-bound cause.
            error=text if is_error else None,
        )

    @staticmethod
    def _turn_from_call(result: Any) -> WorkerTurn:
        if result.is_err:
            return WorkerTurn(text="", session_id=None, is_error=True, error=result.error)
        return CodexMcpWorkerTransport._parse_turn(result.value)

    async def spawn(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
        cwd: str | None,
        permission_mode: str | None,
        model: str | None,
        reasoning_effort: str | None,
        fork_from_session_id: str | None = None,
        label: str | None = None,
    ) -> WorkerTurn:
        # ``fork_from_session_id`` is intentionally ignored: a delegated host
        # session is the human's Claude conversation, which a codex mcp-server
        # cannot fork. The worker spawns a clean codex thread instead (no host
        # transcript is touched, so there is no pollution risk). ``label`` (the
        # human-facing name) is used for the Codex session-index entry below.
        sandbox, approval = _map_permission_mode(permission_mode)
        arguments: dict[str, Any] = {
            "prompt": prompt,
            "sandbox": sandbox,
            "approval-policy": approval,
            "cwd": cwd or os.getcwd(),
        }
        if system_prompt:
            # Native developer-role directive (not embedded in the user prompt).
            arguments["developer-instructions"] = system_prompt
        if model and model != "default":
            arguments["model"] = model

        # Build the codex ``config`` override (deep-merged onto ~/.codex/config.toml):
        # effort + per-server MCP disable for recursion hardening (native
        # passthrough preserves every server NOT listed here).
        config: dict[str, Any] = {}
        if reasoning_effort and reasoning_effort in _CODEX_REASONING_EFFORT_LEVELS:
            config["model_reasoning_effort"] = reasoning_effort
        if self._disabled_mcp_servers:
            config["mcp_servers"] = {
                name: {"enabled": False} for name in self._disabled_mcp_servers
            }
        if config:
            arguments["config"] = config

        # A fresh persistent session for this worker; keep it warm if the turn
        # yields a threadId so codex-reply can reach the SAME server process.
        actor = MCPSessionActor(self._server_config(), idle_timeout=self._idle_timeout)
        turn = self._turn_from_call(await actor.call("codex", arguments))
        if turn.session_id and not turn.is_error:
            self._pool[turn.session_id] = actor
            if self._index_sessions:
                # Best-effort: make this sub-agent visible/resumable in the Codex app.
                # Prefer the runtime-supplied label (shared with the Claude --name
                # surface) so both providers show the SAME human-facing name.
                register_codex_session(
                    turn.session_id,
                    label or derive_session_label(prompt),
                    codex_home=self._codex_home,
                )
        else:
            await actor.aclose()
        return turn

    async def resume(
        self,
        *,
        session_id: str,
        prompt: str,
        permission_mode: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> WorkerTurn:
        # ``codex-reply`` accepts neither control. Keep the provider-neutral
        # transport contract without smuggling unsupported arguments into MCP.
        del permission_mode, model, reasoning_effort
        actor = self._pool.get(session_id)
        if actor is None or not actor.is_alive:
            # The warm session was reaped (idle TTL / closed) — process-bound
            # sessions cannot be revived cross-process. Fail clearly.
            self._pool.pop(session_id, None)
            if actor is not None:
                await actor.aclose()
            return WorkerTurn(
                text="",
                session_id=session_id,
                is_error=True,
                error=(
                    "codex mcp-server session is process-bound and the warm "
                    f"connection for threadId={session_id} is no longer live "
                    "(reaped by idle TTL or closed). Re-spawn instead of resuming."
                ),
            )
        return self._turn_from_call(
            await actor.call("codex-reply", {"threadId": session_id, "prompt": prompt})
        )

    async def aclose(self) -> None:
        """Close every warm session connection (called on runtime teardown)."""
        actors = list(self._pool.values())
        self._pool.clear()
        for actor in actors:
            await actor.aclose()


def build_codex_mcp_worker_runtime(
    *,
    cli_path: str | None = None,
    cwd: str | None = None,
    permission_mode: str | None = None,
    model: str | None = None,
    llm_backend: str | None = None,
    index_sessions: bool = False,
) -> LeaderDrivenWorkerRuntime:
    """Construct a leader-driven Codex worker runtime over ``codex mcp-server``.

    ``index_sessions`` defaults to False: the primary worker-visibility surface is
    the provider-agnostic web dashboard, which groups every worker under one run
    (no flooding). Registering each worker in the Codex app's session index would
    dump N (ACs × sub-ACs × retries) ``ooo:`` entries into the human's Codex
    conversation list, so it is OPT-IN — enable via ``OUROBOROS_NATIVE_SESSION_INDEX``
    (wired through :func:`runtime_factory.create_agent_runtime`) when you
    specifically want to open a worker in the Codex app. See codex_session_index.
    """
    return LeaderDrivenWorkerRuntime(
        transport=CodexMcpWorkerTransport(cli_path=cli_path, index_sessions=index_sessions),
        runtime_backend="codex_mcp",
        llm_backend=llm_backend or "codex",
        cwd=cwd,
        permission_mode=permission_mode,
        model=model,
        # A fresh ``codex`` call accepts model_reasoning_effort, but the warm
        # ``codex-reply`` resume leg cannot retarget it. The capability describes
        # the whole per-call runtime contract, so claiming NATIVE would make
        # resumed rows falsely appear enforced; retain the truthful IGNORED
        # default, matching model_override_support below.
        # model_override_support is intentionally left at its IGNORED default: a
        # per-call model can be pinned only at thread creation (the ``codex`` call),
        # but ``codex-reply`` (this runtime's resume path) has no model argument, so
        # a warm thread cannot be re-targeted mid-session. Declaring NATIVE would be
        # a lie for the resume leg, so the runtime honestly opts out — mirroring how
        # it declines targeted_resume for the same process-bound reason.
        # codex mcp-server sessions are PROCESS-BOUND (in-memory threadId,
        # addressable only by the SAME server process) and the warm pool is
        # aclose()d after each parallel run, so a persisted RuntimeHandle is
        # always dead on reload: resume() returns a guaranteed terminal
        # "session is process-bound" error instead of respawning fresh. So this
        # runtime must NOT advertise targeted_resume nor emit a resumable handle
        # — unlike the disk-persisted Claude (claude -p --resume) and codex exec
        # backends. Mirrors build_claude_worker_runtime's targeted_resume gate.
        targeted_resume=False,
    )


__all__ = ["CodexMcpWorkerTransport", "build_codex_mcp_worker_runtime"]
