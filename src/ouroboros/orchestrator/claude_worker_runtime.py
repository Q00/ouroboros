"""Claude leader-driven worker runtime over ``claude -p --resume`` (stream JSON).

The SAME provider-neutral :class:`LeaderDrivenWorkerRuntime` that drives Codex
also drives Claude — only this thin transport differs. That is the whole point:
"any provider becomes a worker by supplying a transport, not a bespoke runtime."

Claude's headless surface (``claude -p <prompt> --output-format json``) returns a
``session_id`` and ``result``; ``claude -p --resume <session_id>`` continues it.
Verified 2026-06-21: unlike ``codex mcp-server`` (process-bound sessions), Claude
sessions can be disk-persisted and resumed across processes. Ouroboros keeps that
persistence opt-in, because default ``--no-session-persistence`` workers return a
JSON ``session_id`` that is only diagnostic, not a valid future resume target.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
import json
import os
from pathlib import Path
from typing import Any

from ouroboros.config import get_cli_path
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import CLAUDE_REASONING_EFFORT_LEVELS, ParamSupport
from ouroboros.orchestrator.worker_runtime import (
    LeaderDrivenWorkerRuntime,
    WorkerTurn,
)
from ouroboros.runtime.child_env import DEFAULT_OUROBOROS_STRIP_KEYS, build_child_env

log = get_logger(__name__)

# ouroboros permission modes that map to Claude's autonomous skip-permissions.
_SKIP_PERMISSION_MODES = frozenset({"bypasspermissions", "bypass", "danger-full-access"})
# Claude --permission-mode accepts these directly.
_CLAUDE_PERMISSION_MODES = frozenset({"default", "acceptedits", "plan", "acceptEdits"})

# ouroboros MCP tool prefixes DENIED in workers (recursion hardening). A claude
# worker natively inherits the user's ~/.claude MCP servers (native passthrough),
# which includes ouroboros itself → the worker could call ouroboros tools and
# re-enter the orchestrator. `--disallowedTools` denies both the plain and the
# plugin-namespaced registrations (verified: `mcp__plugin_ouroboros_ouroboros`
# is the live prefix). Combined with the `_OUROBOROS_DEPTH` env guard applied in
# `_child_env`, this is defense in depth — parity with the codex worker.
_RECURSION_GUARD_DISALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__ouroboros",
    "mcp__plugin_ouroboros_ouroboros",
)

# Cap on the number of ``--add-dir`` grants emitted for a worker so a seed with
# many ``context_references`` cannot blow up the argv. The pack (C2) already
# reaches the worker via the system prompt; ``--add-dir`` is the NATIVE channel
# that additionally grants the worker filesystem READ access to referenced dirs
# outside its cwd — a bounded optimization, not a correctness requirement.
_MAX_ADD_DIRS = 8


def _resolve_add_dirs(
    context_reference_dirs: Sequence[str],
    *,
    cwd: str | None,
) -> tuple[str, ...]:
    """Return existing, deduped, absolute dirs for ``--add-dir`` (capped).

    Best-effort and side-effect free: relative references are resolved against
    ``cwd`` (the worker's repo root), non-directories and duplicates are
    dropped, and the result is capped at :data:`_MAX_ADD_DIRS`. An empty input
    yields an empty tuple so the caller emits no ``--add-dir`` flag at all
    (byte-identical to the pre-C4 command).
    """
    base = Path(cwd) if cwd else None
    resolved: list[str] = []
    seen: set[str] = set()
    for raw in context_reference_dirs:
        candidate = (raw or "").strip()
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if not path.is_absolute() and base is not None:
            path = base / path
        try:
            if not path.is_dir():
                continue
            key = str(path.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        resolved.append(key)
        if len(resolved) >= _MAX_ADD_DIRS:
            break
    return tuple(resolved)


class ClaudeWorkerTransport:
    """Spawn/resume a Claude worker session via ``claude -p ... --output-format json``."""

    backend_name = "claude_mcp"

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        cwd: str | None = None,
        timeout: float | None = None,
        disallowed_tools: tuple[str, ...] = _RECURSION_GUARD_DISALLOWED_TOOLS,
        persist_sessions: bool = False,
        context_reference_dirs: Sequence[str] = (),
    ) -> None:
        self._cli_path = cli_path or get_cli_path() or "claude"
        # Claude sessions are CWD-SCOPED: ``--resume`` finds a conversation only
        # when run from the directory it was created in ("No conversation found"
        # otherwise). The transport pins the cwd so resume targets the same store.
        self._cwd = cwd
        self._timeout = timeout if timeout and timeout > 0 else None
        # Native passthrough keeps the worker's MCP surface, MINUS these tools
        # (recursion hardening — see _RECURSION_GUARD_DISALLOWED_TOOLS).
        self._disallowed_tools = disallowed_tools
        # OFF by default: a ``claude -p`` session ALWAYS lands in its cwd's project
        # dir, so persisting every worker would flood the human's ``/resume`` list
        # (one entry per AC × sub-AC × retry). When off we pass
        # ``--no-session-persistence`` (verified: returns a session_id but writes NO
        # session file → invisible in /resume) and skip fork/--name; the web
        # dashboard is the worker view instead. Opt in (native flag) to persist +
        # fork parent context + ``--name`` so a worker is openable/resumable natively.
        self._persist_sessions = persist_sessions
        # NATIVE context channel (C4): grant the worker READ access to the
        # seed's brownfield ``context_references`` dirs via ``--add-dir``.
        # Resolved once against cwd (existing dirs only, deduped, capped) so the
        # command builder just appends the flags. Empty ⇒ no ``--add-dir`` at all.
        self._add_dirs = _resolve_add_dirs(context_reference_dirs, cwd=cwd)

    @staticmethod
    def _permission_args(permission_mode: str | None) -> list[str]:
        normalized = (permission_mode or "").strip().lower()
        if not normalized:
            return []
        if normalized in _SKIP_PERMISSION_MODES:
            return ["--dangerously-skip-permissions"]
        if normalized in {m.lower() for m in _CLAUDE_PERMISSION_MODES}:
            return ["--permission-mode", permission_mode or ""]
        return []

    def _base_command(self, *, cwd: str | None) -> list[str]:
        command = [self._cli_path, "-p", "--output-format", "json"]
        if self._disallowed_tools:
            # Space-separated list per `claude --disallowedTools` semantics; denies
            # the ouroboros MCP tools so the worker cannot re-enter the orchestrator.
            command.extend(["--disallowedTools", " ".join(self._disallowed_tools)])
        for add_dir in self._add_dirs:
            # Native passthrough grants read access to referenced brownfield
            # dirs outside cwd. No-op when there are no context_references.
            command.extend(["--add-dir", add_dir])
        if not self._persist_sessions:
            # Don't write a resumable session file → no /resume flooding.
            command.append("--no-session-persistence")
        return command

    @staticmethod
    def _name_args(label: str | None) -> list[str]:
        # ``--name`` persists a ``custom-title`` + ``agent-name`` record into the
        # session store even in ``-p`` mode (verified), so the worker shows up in
        # the human's ``/resume`` picker under this label — the Claude analog of
        # the Codex session-index entry. Makes the sub-agent human-discoverable.
        label = (label or "").strip()
        return ["--name", label] if label else []

    @staticmethod
    def _child_env() -> dict[str, str]:
        # Strip the ouroboros discovery markers + the nested-session marker
        # CLAUDECODE, and increment the shared `_OUROBOROS_DEPTH` recursion guard
        # (raises past max depth) — the env-level backstop behind --disallowedTools.
        return build_child_env(
            strip_keys=(*DEFAULT_OUROBOROS_STRIP_KEYS, "CLAUDECODE"),
            depth_error_factory=lambda depth, max_depth: RuntimeError(
                f"Max ouroboros nesting depth ({max_depth}) exceeded (depth={depth})"
            ),
        )

    async def _run(self, command: list[str], prompt: str, cwd: str | None) -> WorkerTurn:
        try:
            proc = await asyncio.create_subprocess_exec(
                *command,
                cwd=cwd or os.getcwd(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._child_env(),
            )
        except FileNotFoundError as exc:
            return WorkerTurn(text="", is_error=True, error=f"claude CLI not found: {exc}")

        try:
            if self._timeout is not None:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self._timeout
                )
            else:
                stdout_b, stderr_b = await proc.communicate(prompt.encode("utf-8"))
        except TimeoutError:
            proc.kill()
            # Reap the SIGKILL'd child so it does not linger as a zombie. kill()
            # only sends the signal; wait() collects the exit status. It returns
            # promptly because SIGKILL is uncatchable.
            await proc.wait()
            return WorkerTurn(text="", is_error=True, error="claude worker turn timed out")

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()
        return self._parse_turn(stdout, stderr, proc.returncode)

    @staticmethod
    def _parse_turn(stdout: str, stderr: str, returncode: int | None) -> WorkerTurn:
        payload: dict[str, Any] | None = None
        # --output-format json emits one JSON object (last non-empty line is safest).
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                candidate = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break

        if payload is None:
            return WorkerTurn(
                text="",
                is_error=True,
                error=f"claude returned no JSON (rc={returncode}): {stderr or stdout[:200]}",
            )

        session_id = payload.get("session_id")
        result_text = payload.get("result")
        raw_usage = payload.get("usage")
        usage = dict(raw_usage) if isinstance(raw_usage, dict) else None
        is_error = bool(payload.get("is_error")) or returncode not in (0, None)
        return WorkerTurn(
            text=str(result_text) if result_text is not None else "",
            session_id=session_id if isinstance(session_id, str) and session_id else None,
            is_error=is_error,
            error=(stderr or None) if is_error else None,
            usage=usage,
        )

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
        command = self._base_command(cwd=cwd)
        if self._persist_sessions and fork_from_session_id:
            # Fork the host (parent) Claude session: the worker inherits the
            # human's conversation context but ``--fork-session`` mints a FRESH
            # session id, so the parent's live transcript is never mutated. The
            # forked child shows up in the same project's ``/resume`` picker — the
            # human can see and manage the sub-agent it spawned. We still append
            # the worker's assignment as a system directive on top of the fork.
            # (Fork requires a persisted session, so it is gated on the native flag.)
            command.extend(["--resume", fork_from_session_id, "--fork-session"])
        command.extend(self._permission_args(permission_mode))
        if self._persist_sessions:
            # ``--name`` only matters when the session is persisted (visible in
            # /resume). Skipped in dashboard-centric mode to avoid wasted args.
            command.extend(self._name_args(label))
        if system_prompt:
            command.extend(["--append-system-prompt", system_prompt])
        if model and model != "default":
            command.extend(["--model", model])
        if reasoning_effort and reasoning_effort in CLAUDE_REASONING_EFFORT_LEVELS:
            command.extend(["--effort", reasoning_effort])
        return await self._run(command, prompt, cwd)

    async def resume(
        self,
        *,
        session_id: str,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> WorkerTurn:
        if not self._persist_sessions:
            # Non-persisted (dashboard-centric) workers wrote no session file, so
            # there is nothing to ``--resume``. Fail clearly rather than silently
            # losing context (mirrors the codex process-bound resume error).
            return WorkerTurn(
                text="",
                session_id=session_id,
                is_error=True,
                error=(
                    "claude worker session is non-persisted (dashboard-centric mode) "
                    f"and cannot be resumed (session_id={session_id}). Enable "
                    "OUROBOROS_NATIVE_SESSION_INDEX for persisted, resumable workers."
                ),
            )
        # Claude sessions are disk-persisted → cross-process resume works, but
        # only from the SAME cwd the session was created in (cwd-scoped store).
        command = [*self._base_command(cwd=self._cwd), "--resume", session_id]
        if model and model != "default":
            command.extend(["--model", model])
        if reasoning_effort and reasoning_effort in CLAUDE_REASONING_EFFORT_LEVELS:
            command.extend(["--effort", reasoning_effort])
        return await self._run(command, prompt, self._cwd)


def build_claude_worker_runtime(
    *,
    cli_path: str | None = None,
    cwd: str | None = None,
    permission_mode: str | None = None,
    model: str | None = None,
    llm_backend: str | None = None,
    persist_sessions: bool = False,
    context_reference_dirs: Sequence[str] = (),
) -> LeaderDrivenWorkerRuntime:
    """Construct a leader-driven Claude worker runtime over ``claude -p --resume``.

    ``persist_sessions`` defaults to False (dashboard-centric): workers run with
    ``--no-session-persistence`` so they do not flood the human's ``/resume`` list.
    Opt in (native-session-index flag) to persist + fork parent context + ``--name``
    so a worker is openable/resumable in Claude natively.

    ``context_reference_dirs`` (C4 native context channel): brownfield reference
    directories the worker should be able to READ. Existing dirs are passed as
    ``--add-dir`` grants (deduped, capped). Empty (the default) is a byte-for-byte
    no-op — the worker command is identical to the pre-C4 invocation.
    """
    return LeaderDrivenWorkerRuntime(
        transport=ClaudeWorkerTransport(
            cli_path=cli_path,
            cwd=cwd,
            persist_sessions=persist_sessions,
            context_reference_dirs=context_reference_dirs,
        ),
        runtime_backend="claude_mcp",
        llm_backend=llm_backend or "claude",
        cwd=cwd,
        permission_mode=permission_mode,
        model=model,
        reasoning_effort_support=ParamSupport.NATIVE,
        enforceable_reasoning_efforts=CLAUDE_REASONING_EFFORT_LEVELS,
        # The transport routes a per-call model to ``claude --model`` (see
        # ClaudeWorkerTransport.spawn), so the worker enforces a model-tier
        # override natively — matching the in-process ClaudeAgentAdapter.
        model_override_support=ParamSupport.NATIVE,
        targeted_resume=persist_sessions,
    )


__all__ = ["ClaudeWorkerTransport", "build_claude_worker_runtime"]
