"""Hermes Agent adapter for Ouroboros orchestrator.

This module provides a HermesAgentRuntime that satisfies the AgentRuntime protocol
by shelling out to the Hermes CLI.
"""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator, Mapping
import contextlib
from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import shutil
from typing import Any

from ouroboros.config import get_hermes_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    AgentRuntime,
    RuntimeHandle,
    SkillDispatchHandler,
    TaskResult,
)
from ouroboros.router import (
    InvalidInputReason,
    InvalidSkill,
    NotHandled,
    Resolved,
    ResolveRequest,
    resolve_skill_dispatch,
)

log = get_logger(__name__)

_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"

# Hermes session ID format: YYYYMMDD_HHMMSS_xxxxxx
_HERMES_SESSION_ID_PATTERN = re.compile(r"^session_id:\s+(?P<session_id>\d{8}_\d{6}_[a-f0-9]+)\s*$")
_REASONING_HEADER_PREFIX = "┌─ Reasoning"
_REASONING_BOX_PREFIXES = ("│", "├", "└")
_HERMES_BANNER_LINE_PATTERN = re.compile(r"^[╭┌].*Hermes.*[╮┐]$")


def _strip_reasoning_prelude(content: str) -> str:
    """Remove Hermes quiet-mode reasoning decorations from leading output."""
    lines = content.splitlines()
    first_nonempty_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_nonempty_index is None:
        return ""

    header_line = lines[first_nonempty_index]
    if _HERMES_BANNER_LINE_PATTERN.fullmatch(header_line):
        return "\n".join(lines[first_nonempty_index + 1 :]).strip()

    if not header_line.startswith(_REASONING_HEADER_PREFIX):
        return content.strip()

    index = first_nonempty_index + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(_REASONING_BOX_PREFIXES):
            index += 1
            continue
        break

    return "\n".join(lines[index:]).strip()


def _parse_quiet_output(output: str) -> tuple[str, str | None]:
    """Extract the user-facing content and session id from Hermes quiet output."""
    session_id: str | None = None
    content_lines: list[str] = []

    for line in output.splitlines():
        match = _HERMES_SESSION_ID_PATTERN.fullmatch(line.strip())
        if match is not None and session_id is None:
            session_id = match.group("session_id")
            continue
        content_lines.append(line)

    content = "\n".join(content_lines)
    return _strip_reasoning_prelude(content), session_id


class HermesCliRuntime(AgentRuntime):
    """Orchestrator runtime that executes tasks via the Hermes CLI."""

    _runtime_handle_backend = "hermes_cli"
    _runtime_backend = "hermes"
    _default_cli_name = "hermes"
    _log_namespace = "hermes_cli_runtime"
    _default_llm_backend = "claude_code"
    _display_name = "Hermes CLI"
    _process_shutdown_timeout_seconds = 5.0
    _max_ouroboros_depth = 5
    _startup_output_timeout_seconds = 60.0
    _stdout_idle_timeout_seconds = 300.0
    _max_stderr_lines = 512

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = permission_mode or "default"
        self._provider = provider
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._skills_dir = Path(skills_dir).expanduser() if skills_dir else None
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or self._default_llm_backend
        self._builtin_mcp_handlers: dict[str, Any] | None = None

        log.info(
            f"{self._log_namespace}.initialized",
            cli_path=self._cli_path,
            permission_mode=self._permission_mode,
            model=model,
            cwd=self._cwd,
        )

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    @property
    def llm_backend(self) -> str | None:
        return self._llm_backend

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the Hermes CLI path."""
        if cli_path is not None:
            return str(Path(cli_path).expanduser())

        configured = get_hermes_cli_path()
        if configured:
            return configured

        return shutil.which(self._default_cli_name) or self._default_cli_name

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for child runtime processes."""
        env = os.environ.copy()
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)

        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1

        if depth > self._max_ouroboros_depth:
            msg = f"Maximum Ouroboros nesting depth ({self._max_ouroboros_depth}) exceeded"
            raise RuntimeError(msg)

        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

    def _build_runtime_handle(
        self,
        session_id: str | None,
        current_handle: RuntimeHandle | None = None,
    ) -> RuntimeHandle | None:
        """Build a backend-neutral runtime handle for a Hermes thread."""
        if not session_id:
            return None

        if current_handle is not None:
            return replace(
                current_handle,
                native_session_id=session_id,
                updated_at=datetime.now(UTC).isoformat(),
            )

        return RuntimeHandle(
            backend=self._runtime_handle_backend,
            native_session_id=session_id,
            cwd=self._cwd,
            updated_at=datetime.now(UTC).isoformat(),
        )

    def _compose_prompt(
        self,
        prompt: str,
        system_prompt: str | None,
        tools: list[str] | None,
    ) -> str:
        """Compose a single prompt for Hermes."""
        parts: list[str] = []

        if system_prompt:
            parts.append(f"## System Instructions\n{system_prompt}")

        if tools:
            tool_list = "\n".join(f"- {tool}" for tool in tools)
            parts.append(
                "## Tooling Guidance\n"
                "Prefer to solve the task using the following tool set when possible:\n"
                f"{tool_list}"
            )

        parts.append(prompt)
        return "\n\n".join(part for part in parts if part.strip())

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield decoded lines from a subprocess stream with timeout guards."""
        if stream is None:
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        saw_chunk = False

        while True:
            timeout_seconds: float | None = None
            if not saw_chunk:
                timeout_seconds = first_chunk_timeout_seconds
            elif chunk_timeout_seconds is not None:
                timeout_seconds = chunk_timeout_seconds

            try:
                if timeout_seconds is None:
                    chunk = await stream.read(chunk_size)
                else:
                    chunk = await asyncio.wait_for(stream.read(chunk_size), timeout=timeout_seconds)
            except TimeoutError as exc:
                phase = "startup" if not saw_chunk else "idle"
                raise TimeoutError(
                    f"{self._display_name} produced no stdout during {phase} "
                    f"window ({timeout_seconds:.0f}s)"
                ) from exc

            if not chunk:
                break

            saw_chunk = True
            buffer += decoder.decode(chunk)
            while True:
                newline_index = buffer.find("\n")
                if newline_index < 0:
                    break
                line = buffer[:newline_index]
                buffer = buffer[newline_index + 1 :]
                yield line.rstrip("\r")

        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer.rstrip("\r")

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        max_lines: int | None = None,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
    ) -> list[str]:
        """Drain a subprocess stream into a list of decoded lines."""
        if stream is None:
            return []

        lines: deque[str] = deque(maxlen=max_lines) if max_lines is not None else deque()
        async for line in self._iter_stream_lines(
            stream,
            first_chunk_timeout_seconds=first_chunk_timeout_seconds,
            chunk_timeout_seconds=chunk_timeout_seconds,
        ):
            if line:
                lines.append(line)
        return list(lines)

    async def _terminate_process(self, process: Any) -> None:
        """Best-effort subprocess shutdown used for cancellations and timeouts."""
        if getattr(process, "returncode", None) is not None:
            return

        terminate = getattr(process, "terminate", None)
        kill = getattr(process, "kill", None)
        wait = getattr(process, "wait", None)

        try:
            if callable(terminate):
                terminate()
            elif callable(kill):
                kill()
            else:
                return
        except ProcessLookupError:
            return

        if not callable(wait):
            return

        try:
            await asyncio.wait_for(wait(), timeout=self._process_shutdown_timeout_seconds)
            return
        except (ProcessLookupError, TimeoutError):
            pass

        if callable(kill):
            with contextlib.suppress(ProcessLookupError):
                kill()
            with contextlib.suppress(ProcessLookupError, TimeoutError):
                await asyncio.wait_for(wait(), timeout=self._process_shutdown_timeout_seconds)

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task via Hermes CLI.

        Conforms to the ``AgentRuntime`` protocol: callers pass a
        backend-neutral ``resume_handle`` (preferred) or a legacy
        ``resume_session_id``; Hermes resolves both into its native
        ``hermes chat --resume <session_id>`` invocation so that
        multi-turn orchestrator flows resume the prior session instead
        of starting a fresh one.
        """

        # Resolve the effective resume handle. Prefer the backend-neutral
        # handle; fall back to the legacy session id for callers that have
        # not migrated yet.
        handle: RuntimeHandle | None = resume_handle
        if handle is None and resume_session_id:
            handle = self._build_runtime_handle(resume_session_id, None)

        # 1. Resolve deterministic skill dispatch once before invoking Hermes.
        dispatch_result = resolve_skill_dispatch(
            ResolveRequest(
                prompt=prompt,
                cwd=self._cwd,
                skills_dir=self._skills_dir,
            )
        )
        intercepted_messages: tuple[AgentMessage, ...] | None = None
        if isinstance(dispatch_result, InvalidSkill):
            self._log_invalid_skill_intercept(dispatch_result)
        elif not isinstance(dispatch_result, NotHandled):
            intercepted_messages = await self._maybe_dispatch_skill_intercept(
                dispatch_result,
                handle,
            )
        if intercepted_messages:
            for message in intercepted_messages:
                yield message
            return

        full_prompt = self._compose_prompt(prompt, system_prompt, tools)

        args = [self._cli_path, "chat"]
        if handle and handle.native_session_id:
            args.extend(["--resume", handle.native_session_id])

        # Use quiet mode for programmatic output
        args.extend(["-Q", "--source", "tool"])

        if self._provider:
            args.extend(["--provider", self._provider])

        if self._model:
            args.extend(["--model", self._model])

        args.extend(["-q", full_prompt])

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._build_child_env(),
        )

        stdout_task = asyncio.create_task(
            self._collect_stream_lines(
                process.stdout,
                first_chunk_timeout_seconds=self._startup_output_timeout_seconds,
                chunk_timeout_seconds=self._stdout_idle_timeout_seconds,
            )
        )
        stderr_task = asyncio.create_task(
            self._collect_stream_lines(
                process.stderr,
                max_lines=self._max_stderr_lines,
            )
        )

        try:
            stdout_lines, stderr_lines = await asyncio.gather(stdout_task, stderr_task)
            returncode = await process.wait()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        except TimeoutError as e:
            await self._terminate_process(process)
            for task in (stdout_task, stderr_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await task
            yield AgentMessage(
                type="result",
                content=f"Hermes execution failed:\n{e}",
                data={"subtype": "error", "error_type": "TimeoutError"},
                resume_handle=handle,
            )
            return

        output = "\n".join(stdout_lines).strip()
        error = "\n".join(stderr_lines).strip()

        if returncode != 0:
            failure_content = error or output or f"Hermes exited with code {returncode}"
            yield AgentMessage(
                type="result",
                content=f"Hermes execution failed:\n{failure_content}",
                data={"subtype": "error", "exit_code": returncode},
                resume_handle=handle,
            )
            return

        clean_content, session_id = _parse_quiet_output(output)

        new_handle = self._build_runtime_handle(session_id, handle)

        yield AgentMessage(
            type="result",
            content=clean_content,
            data={"subtype": "success", "session_id": session_id},
            resume_handle=new_handle,
        )

    # -- Skill Intercept & Dispatch -----------------------------------------

    async def _maybe_dispatch_skill_intercept(
        self,
        intercept: Resolved,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Attempt deterministic skill dispatch before invoking Hermes."""
        dispatcher = self._skill_dispatcher or self._dispatch_skill_intercept_locally
        try:
            dispatched_messages = await dispatcher(intercept, current_handle)
        except Exception as e:
            log.warning(
                f"{self._log_namespace}.skill_intercept_dispatch_failed",
                **self._build_intercept_failure_context(intercept),
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            return None

        recoverable_error = self._extract_recoverable_dispatch_error(dispatched_messages)
        if recoverable_error is not None:
            log.warning(
                f"{self._log_namespace}.skill_intercept_dispatch_failed",
                **self._build_intercept_failure_context(intercept),
                error_type=recoverable_error.data.get("error_type"),
                error=recoverable_error.content,
                recoverable=True,
            )
            return None

        return dispatched_messages

    def _build_intercept_failure_context(
        self,
        intercept: Resolved,
    ) -> dict[str, Any]:
        """Build structured log context for intercept failures."""
        return {
            "skill": intercept.skill_name,
            "tool": intercept.mcp_tool,
            "command_prefix": intercept.command_prefix,
            "path": str(intercept.skill_path),
        }

    def _extract_recoverable_dispatch_error(
        self,
        dispatched_messages: tuple[AgentMessage, ...] | None,
    ) -> AgentMessage | None:
        """Identify final recoverable intercept failures that should fall through."""
        if not dispatched_messages:
            return None

        final_message = next(
            (
                message
                for message in reversed(dispatched_messages)
                if message.is_final and message.is_error
            ),
            None,
        )
        if final_message is None:
            return None

        data = final_message.data
        metadata_candidates = (
            data,
            data.get("meta") if isinstance(data.get("meta"), Mapping) else None,
            data.get("mcp_meta") if isinstance(data.get("mcp_meta"), Mapping) else None,
        )

        for metadata in metadata_candidates:
            if not isinstance(metadata, Mapping):
                continue
            if metadata.get("recoverable") is True:
                return final_message
            if metadata.get("is_retriable") is True or metadata.get("retriable") is True:
                return final_message

        if final_message.data.get("error_type") in {"MCPConnectionError", "MCPTimeoutError"}:
            return final_message

        return None

    def _invalid_skill_log_name(self, dispatch_result: InvalidSkill) -> str:
        """Infer the legacy runtime skill field from the resolved skill path."""
        skill_path = dispatch_result.skill_path
        if skill_path.name == "SKILL.md" and skill_path.parent.name:
            return skill_path.parent.name
        return skill_path.stem or str(skill_path)

    def _invalid_skill_log_error(self, dispatch_result: InvalidSkill) -> str:
        """Format invalid-skill errors with the legacy Hermes wording."""
        if dispatch_result.reason == "SKILL.md frontmatter must be a mapping":
            return f"Frontmatter must be a mapping in {dispatch_result.skill_path}"
        if self._is_legacy_mcp_args_validation_error(dispatch_result.reason):
            return "mcp_args must be a mapping with string keys and YAML-safe values"
        return dispatch_result.reason

    def _is_legacy_mcp_args_validation_error(self, reason: str) -> bool:
        """Map shared-router granular mcp_args errors back to Hermes' legacy text."""
        if reason == "mcp_args must be a mapping with string keys and YAML-safe values":
            return False
        return (
            reason.startswith("mcp_args.")
            or reason.startswith("mcp_args[")
            or reason.endswith("keys must be non-empty strings")
        )

    def _invalid_skill_log_context(self, dispatch_result: InvalidSkill) -> dict[str, str]:
        """Build the legacy Hermes invalid-frontmatter warning payload."""
        return {
            "skill": self._invalid_skill_log_name(dispatch_result),
            "path": str(dispatch_result.skill_path),
            "error": self._invalid_skill_log_error(dispatch_result),
        }

    def _log_invalid_skill_intercept(self, dispatch_result: InvalidSkill) -> None:
        """Preserve runtime-owned warnings for matched skills with bad metadata."""
        warning_event = f"{self._log_namespace}.skill_intercept_frontmatter_invalid"
        if (
            dispatch_result.category is InvalidInputReason.FRONTMATTER_INVALID
            and dispatch_result.reason.startswith("missing required frontmatter key:")
        ):
            warning_event = f"{self._log_namespace}.skill_intercept_frontmatter_missing"

        log.warning(
            warning_event,
            **self._invalid_skill_log_context(dispatch_result),
        )

    def _build_tool_message(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        content: str,
        handle: RuntimeHandle | None,
        extra_data: dict[str, Any] | None = None,
    ) -> AgentMessage:
        """Build the assistant message announcing an intercepted tool call."""
        data = {"tool_input": tool_input}
        if extra_data:
            data.update(extra_data)

        return AgentMessage(
            type="assistant",
            content=content,
            tool_name=tool_name,
            data=data,
            resume_handle=handle,
        )

    def _get_builtin_mcp_handlers(self) -> dict[str, Any]:
        """Load and cache local Ouroboros MCP handlers."""
        if self._builtin_mcp_handlers is None:
            from ouroboros.mcp.tools.definitions import get_ouroboros_tools

            self._builtin_mcp_handlers = {
                handler.definition.name: handler
                for handler in get_ouroboros_tools(
                    runtime_backend=self._runtime_backend,
                    llm_backend=self._llm_backend,
                )
            }
        return self._builtin_mcp_handlers

    def _get_mcp_tool_handler(self, tool_name: str) -> Any | None:
        """Look up a local MCP handler by tool name."""
        return self._get_builtin_mcp_handlers().get(tool_name)

    def _build_tool_arguments(
        self,
        intercept: Resolved,
        current_handle: RuntimeHandle | None,
    ) -> dict[str, Any]:
        """Build MCP arguments, preserving interview sessions across turns."""
        if intercept.mcp_tool != "ouroboros_interview" or current_handle is None:
            return dict(intercept.mcp_args)

        session_id = current_handle.metadata.get(_INTERVIEW_SESSION_METADATA_KEY)
        if not isinstance(session_id, str) or not session_id.strip():
            return dict(intercept.mcp_args)

        # Resume turn: drop initial_context so InterviewHandler branches on
        # session_id instead of starting a new interview.
        arguments: dict[str, Any] = dict(intercept.mcp_args)
        arguments.pop("initial_context", None)
        arguments["session_id"] = session_id.strip()
        if intercept.first_argument is not None:
            arguments["answer"] = intercept.first_argument
        return arguments

    def _build_resume_handle(
        self,
        current_handle: RuntimeHandle | None,
        intercept: Resolved,
        tool_result: Any,
    ) -> RuntimeHandle | None:
        """Attach interview session metadata to the runtime handle."""
        if intercept.mcp_tool != "ouroboros_interview":
            return current_handle

        session_id = tool_result.meta.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return current_handle

        metadata = dict(current_handle.metadata) if current_handle is not None else {}
        metadata[_INTERVIEW_SESSION_METADATA_KEY] = session_id.strip()
        updated_at = datetime.now(UTC).isoformat()

        if current_handle is not None:
            return replace(current_handle, metadata=metadata, updated_at=updated_at)

        return RuntimeHandle(
            backend=self.runtime_backend,
            cwd=self.working_directory,
            approval_mode=self.permission_mode,
            updated_at=updated_at,
            metadata=metadata,
        )

    async def _dispatch_skill_intercept_locally(
        self,
        intercept: Resolved,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...]:
        """Dispatch intercept to local MCP handler."""
        mcp_tool = intercept.mcp_tool
        handler = self._get_mcp_tool_handler(mcp_tool)
        if handler is None:
            raise LookupError(f"No local handler for tool: {mcp_tool}")

        tool_arguments = self._build_tool_arguments(intercept, current_handle)
        tool_result = await handler.handle(tool_arguments)
        if tool_result.is_err:
            error = tool_result.error
            error_data = {
                "subtype": "error",
                "error_type": type(error).__name__,
                "recoverable": True,
            }
            if hasattr(error, "is_retriable"):
                error_data["is_retriable"] = bool(error.is_retriable)
            if hasattr(error, "details") and isinstance(error.details, dict):
                error_data["meta"] = dict(error.details)

            return (
                self._build_tool_message(
                    tool_name=mcp_tool,
                    tool_input=tool_arguments,
                    content=f"Calling tool: {mcp_tool}",
                    handle=current_handle,
                    extra_data={
                        "command_prefix": intercept.command_prefix,
                        "skill_name": intercept.skill_name,
                    },
                ),
                AgentMessage(
                    type="result",
                    content=str(error),
                    data=error_data,
                    resume_handle=current_handle,
                ),
            )

        resolved = tool_result.value
        resume_handle = self._build_resume_handle(current_handle, intercept, resolved)
        result_text = resolved.text_content.strip() or f"{mcp_tool} completed."
        result_data: dict[str, Any] = {
            "subtype": "error" if resolved.is_error else "success",
            "tool_name": mcp_tool,
            "mcp_meta": dict(resolved.meta),
        }
        result_data.update(dict(resolved.meta))

        return (
            self._build_tool_message(
                tool_name=mcp_tool,
                tool_input=tool_arguments,
                content=f"Calling tool: {mcp_tool}",
                handle=resume_handle,
                extra_data={
                    "command_prefix": intercept.command_prefix,
                    "skill_name": intercept.skill_name,
                },
            ),
            AgentMessage(
                type="result",
                content=result_text,
                data=result_data,
                resume_handle=resume_handle,
            ),
        )

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and collect all messages into a ``TaskResult``.

        Conforms to the ``AgentRuntime`` contract so Hermes is fully
        substitutable for other runtimes. Generic consumers receive the
        standard ``TaskResult`` shape (``success``/``final_message``/
        ``messages``/``session_id``/``resume_handle``) rather than a
        single ``AgentMessage``.
        """
        messages: list[AgentMessage] = []
        final_message = ""
        success = True
        session_id: str | None = None
        final_resume_handle: RuntimeHandle | None = resume_handle

        async for message in self.execute_task(
            prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)

            if message.resume_handle is not None:
                final_resume_handle = message.resume_handle

            if message.is_final:
                final_message = message.content
                success = not message.is_error
                session_id = message.data.get("session_id")
                if session_id and final_resume_handle is None:
                    final_resume_handle = self._build_runtime_handle(session_id, None)

        if not success:
            return Result.err(
                ProviderError(
                    message=final_message or "Hermes task failed",
                    details={"messages": [m.content for m in messages]},
                )
            )

        if session_id is None and final_resume_handle is not None:
            session_id = final_resume_handle.native_session_id

        return Result.ok(
            TaskResult(
                success=success,
                final_message=final_message,
                messages=tuple(messages),
                session_id=session_id,
                resume_handle=final_resume_handle,
            )
        )
