"""Kiro CLI agent runtime adapter via subprocess.

Calls ``kiro-cli chat --no-interactive --trust-all-tools`` for autonomous
code execution tasks.  Implements the AgentRuntime protocol so it can be
used as a drop-in replacement for ClaudeAgentAdapter / CodexCliRuntime.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
import contextlib
import os
from pathlib import Path
import re
import shutil

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeCapabilities,
    RuntimeHandle,
    SkillDispatchHandler,
    TaskResult,
)
from ouroboros.orchestrator.skill_intercept import SkillInterceptor

# Kiro CLI headless mode (https://kiro.dev/docs/cli/headless/) supports skill
# dispatch (via our interceptor). It does **not** surface a session id on
# stdout or stderr during an ``--no-interactive`` invocation — session ids
# are only discoverable after the fact via ``kiro-cli chat --list-sessions``.
# That means Ouroboros cannot reliably capture a resumable handle from a
# normal headless run, so ``targeted_resume`` is declared False here. The
# ``--resume-id <session_id>`` argv plumbing still exists for the case where
# a caller provides an externally-sourced session id, but we do not advertise
# native resume capability we cannot actually honor end-to-end. Future work:
# wire ``--list-sessions -f json`` into ``execute_task`` completion and flip
# this to True.
# ``structured_output`` is False because Kiro headless emits plain-text
# stdout lines, not JSONL event streams.
_KIRO_CAPABILITIES = RuntimeCapabilities(
    skill_dispatch=True,
    targeted_resume=False,
    structured_output=False,
)

log = get_logger(__name__)

# Kiro CLI in ``--no-interactive`` mode emits terminal prompt markers and
# color escapes on stdout (e.g. ``\x1b[38;5;141m> \x1b[0m`` before the
# actual content). Downstream message consumers and log collectors want
# clean text, so we strip SGR/CSI escapes from every stdout line.
_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text)


_DEFAULT_TIMEOUT = 600.0
_DEFAULT_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 2
_RETRYABLE_EXIT_CODES = (1, 137)
_PROCESS_SHUTDOWN_TIMEOUT = 5.0

_MODEL_NAME_MAP: dict[str, str] = {
    "claude-sonnet-4-6": "claude-sonnet-4.6",
    "claude-opus-4-6": "claude-opus-4.6",
    "claude-sonnet-4-5": "claude-sonnet-4.5",
    "claude-opus-4-5": "claude-opus-4.5",
    "claude-haiku-4-5": "claude-haiku-4.5",
}

# Environment keys stripped from child processes to prevent recursive MCP
# startup and nested session detection conflicts.
_STRIPPED_ENV_KEYS = (
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_LLM_BACKEND",
    "CLAUDECODE",
)

# Session ids flow from subprocess output into argv on the next resume turn;
# validating keeps shell metacharacters and path traversal out of the command
# line. Matches the pattern used by Codex CLI runtime.
_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    """Gracefully terminate, then force-kill a subprocess."""
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=_PROCESS_SHUTDOWN_TIMEOUT)
    except (TimeoutError, ProcessLookupError):
        pass
    if proc.returncode is None:
        try:
            proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=_PROCESS_SHUTDOWN_TIMEOUT)
        except (TimeoutError, ProcessLookupError):
            pass


class KiroAgentAdapter:
    """Agent runtime using Kiro CLI subprocess.

    Implements the AgentRuntime protocol for autonomous task execution.
    Maps ``permission_mode`` onto kiro-cli ``--trust-tools`` / ``--trust-all-tools``
    flags to honor the runtime safety contract.
    """

    _runtime_backend_name = "kiro"
    _max_ouroboros_depth = 5
    _startup_output_timeout = 60.0
    _stdout_idle_timeout = 300.0
    _max_stderr_lines = 512

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        permission_mode: str | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        skills_dir: str | Path | None = None,
        **_kwargs: object,  # absorb extra kwargs from factory for forward compat
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._permission_mode = permission_mode or "acceptEdits"
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or "kiro"
        self._skills_dir = Path(skills_dir).expanduser() if skills_dir is not None else None
        self._interceptor = SkillInterceptor(
            cwd=self._cwd,
            runtime_backend=self._runtime_backend_name,
            runtime_handle_backend=self._runtime_backend_name,
            permission_mode=self._permission_mode,
            llm_backend=self._llm_backend,
            log_namespace="kiro_agent",
            skills_dir=self._skills_dir,
            skill_dispatcher=self._skill_dispatcher,
        )
        log.info("kiro_agent.init", cli_path=self._cli_path, cwd=self._cwd)

    # -- AgentRuntime protocol properties --

    @property
    def runtime_backend(self) -> str:
        return self._runtime_backend_name

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return _KIRO_CAPABILITIES

    # -- Internal helpers --

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        if cli_path:
            return str(cli_path)
        from ouroboros.config import get_kiro_cli_path

        configured = get_kiro_cli_path()
        if configured:
            return configured
        return shutil.which("kiro-cli") or "kiro-cli"

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for the child kiro-cli process.

        Strips keys that would cause recursive MCP startup or nested session
        conflicts, and enforces a recursion depth ceiling.
        """
        env = os.environ.copy()
        for key in _STRIPPED_ENV_KEYS:
            env.pop(key, None)
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1
        if depth > self._max_ouroboros_depth:
            msg = f"Maximum Ouroboros nesting depth ({self._max_ouroboros_depth}) exceeded"
            raise RuntimeError(msg)
        env["_OUROBOROS_DEPTH"] = str(depth)
        env["OUROBOROS_SUBAGENT"] = "1"
        return env

    def _build_permission_args(self) -> list[str]:
        """Map permission_mode onto kiro-cli trust flags.

        Mapping:
        - ``default``           → ``--trust-tools=''`` (no tool trust)
        - ``acceptEdits``       → ``--trust-all-tools`` (full auto)
        - ``bypassPermissions`` → ``--trust-all-tools``
        """
        if self._permission_mode == "default":
            return ["--trust-tools="]
        return ["--trust-all-tools"]

    def _build_cmd(
        self,
        prompt: str,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        resume_session_id: str | None = None,
    ) -> list[str]:
        cmd = [self._cli_path, "chat", "--no-interactive"]
        cmd.extend(self._build_permission_args())
        if self._model:
            mapped = _MODEL_NAME_MAP.get(self._model, self._model)
            cmd.extend(["--model", mapped])
        if resume_session_id:
            # Kiro CLI 2.2+ exposes three resume flags (see
            # https://kiro.dev/docs/cli/headless/ and ``kiro-cli chat --help``):
            #   -r/--resume           → "resume most recent in this directory"
            #   --resume-id <id>      → targeted resume by session id
            #   --resume-picker       → interactive, not usable in headless
            #
            # Passing bare ``--resume`` with an id in hand is the silent
            # degradation the maintainer review flagged: the caller asked for
            # a specific session and we would have resumed whatever was most
            # recent instead. Use ``--resume-id`` so the requested session is
            # actually honored.
            if not _SAFE_SESSION_ID_PATTERN.match(resume_session_id):
                msg = (
                    "Invalid resume_session_id: contains disallowed characters: "
                    f"{resume_session_id!r}"
                )
                raise ValueError(msg)
            cmd.extend(["--resume-id", resume_session_id])

        parts: list[str] = []
        if system_prompt:
            parts.append(f"<system>\n{system_prompt}\n</system>")
        if tools is not None and len(tools) == 0:
            parts.append(
                "IMPORTANT: You MUST NOT use any tools in this response. Respond with text only."
            )
        elif tools is not None and len(tools) > 0:
            allowed = ", ".join(tools)
            parts.append(
                f"You may ONLY use the following tools: {allowed}. Do not use any other tools."
            )
        parts.append(prompt)
        cmd.append("\n\n".join(parts))
        return cmd

    async def _collect_stderr(
        self,
        stream: asyncio.StreamReader | None,
    ) -> list[str]:
        """Drain stderr concurrently without blocking stdout processing."""
        if stream is None:
            return []
        lines: deque[str] = deque(maxlen=self._max_stderr_lines)
        try:
            async for raw_line in stream:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    lines.append(line)
        except Exception:
            pass
        return list(lines)

    def _build_runtime_handle(self, proc: asyncio.subprocess.Process) -> RuntimeHandle:
        return RuntimeHandle(
            backend=self._runtime_backend_name,
            native_session_id=None,
            metadata={"pid": getattr(proc, "pid", None)},
        )

    # -- AgentRuntime protocol methods --

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task and stream normalized messages.

        Before spawning ``kiro-cli``, attempt deterministic skill dispatch so
        that ``ooo <skill>`` and ``/ouroboros:<skill>`` prompts route through
        the matching Ouroboros MCP tool. Without this step, selecting the Kiro
        backend would silently drop a runtime behavior that Claude and Codex
        both preserve.
        """
        current_handle = resume_handle
        intercepted_messages = await self._interceptor.maybe_dispatch(prompt, current_handle)
        if intercepted_messages is not None:
            for message in intercepted_messages:
                if message.resume_handle is not None:
                    current_handle = message.resume_handle
                yield message
            return

        cmd = self._build_cmd(prompt, system_prompt, tools, resume_session_id)
        env = self._build_child_env()

        yield AgentMessage(
            type="system",
            content=f"Starting Kiro CLI: {self._cli_path}",
            data={"subtype": "init", "cli_path": self._cli_path},
            resume_handle=current_handle,
        )

        proc: asyncio.subprocess.Process | None = None
        stderr_task: asyncio.Task[list[str]] | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
            )
            current_handle = self._build_runtime_handle(proc)

            # H2: drain stderr concurrently to prevent pipe buffer deadlock
            stderr_task = asyncio.create_task(self._collect_stderr(proc.stderr))

            # C1: read stdout with per-line idle timeout
            collected: list[str] = []
            saw_output = False
            while True:
                timeout = (
                    self._startup_output_timeout if not saw_output else self._stdout_idle_timeout
                )
                try:
                    raw_line = await asyncio.wait_for(
                        proc.stdout.readline(),
                        timeout=timeout,
                    )
                except TimeoutError:
                    phase = "startup" if not saw_output else "idle"
                    log.warning("kiro_agent.stdout_timeout", phase=phase, timeout=timeout)
                    await _terminate_process(proc)
                    stderr_lines = await stderr_task if stderr_task else []
                    detail = "\n".join(stderr_lines).strip()
                    yield AgentMessage(
                        type="result",
                        content=f"Kiro CLI became unresponsive ({phase} timeout): {detail}"
                        if detail
                        else f"Kiro CLI became unresponsive ({phase} timeout after {timeout}s)",
                        data={"subtype": "error", "error_type": "TimeoutError"},
                        resume_handle=current_handle,
                    )
                    return

                if not raw_line:  # EOF
                    break

                line = _strip_ansi(raw_line.decode(errors="replace")).rstrip()
                # Drop Kiro's leading prompt marker if the reset escape landed
                # after it and survived the strip.
                if line.startswith("> "):
                    line = line[2:].lstrip()
                if line:
                    saw_output = True
                    collected.append(line)
                    yield AgentMessage(
                        type="assistant",
                        content=line,
                        resume_handle=current_handle,
                    )

            # Normal completion — wait for process exit
            await asyncio.wait_for(proc.wait(), timeout=_PROCESS_SHUTDOWN_TIMEOUT)
            stderr_lines = await stderr_task if stderr_task else []

            if proc.returncode == 0:
                final = "\n".join(collected)
                yield AgentMessage(
                    type="result",
                    content=final,
                    data={"subtype": "success"},
                    resume_handle=current_handle,
                )
            else:
                stderr_text = "\n".join(stderr_lines).strip()
                yield AgentMessage(
                    type="result",
                    content=f"Kiro CLI failed (exit {proc.returncode}): {stderr_text}",
                    data={"subtype": "error", "exit_code": proc.returncode},
                    resume_handle=current_handle,
                )

        except asyncio.CancelledError:
            # C2: clean up subprocess on task cancellation
            if proc is not None:
                log.warning("kiro_agent.task_cancelled", cwd=self._cwd)
                await _terminate_process(proc)
            raise
        except FileNotFoundError:
            yield AgentMessage(
                type="result",
                content=f"Kiro CLI not found at: {self._cli_path}",
                data={"subtype": "error"},
                resume_handle=current_handle,
            )
        finally:
            if proc is not None:
                await _terminate_process(proc)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute with retry logic, return collected result."""
        last_error: ProviderError | None = None

        for attempt in range(_DEFAULT_MAX_RETRIES):
            messages: list[AgentMessage] = []
            async for msg in self.execute_task(
                prompt, tools, system_prompt, resume_handle, resume_session_id
            ):
                messages.append(msg)

            if not messages:
                last_error = ProviderError("No messages from Kiro CLI")
                continue

            final = messages[-1]
            if final.is_final and not final.is_error:
                return Result.ok(
                    TaskResult(
                        success=True,
                        final_message=final.content,
                        messages=tuple(messages),
                        resume_handle=final.resume_handle,
                    )
                )

            error_msg = final.content if final.is_final else "Unknown error"
            if not self._is_retryable(error_msg) or attempt >= _DEFAULT_MAX_RETRIES - 1:
                return Result.err(ProviderError(error_msg))

            last_error = ProviderError(error_msg)
            log.warning("kiro_agent.retrying", attempt=attempt + 1, error=error_msg)
            await asyncio.sleep(_RETRY_BASE_DELAY**attempt)

        return Result.err(last_error or ProviderError("Max retries exceeded"))

    @staticmethod
    def _is_retryable(error_msg: str) -> bool:
        return "timed out" in error_msg.lower() or any(
            f"exit {code}" in error_msg for code in _RETRYABLE_EXIT_CODES
        )


__all__ = ["KiroAgentAdapter"]
