"""OMP (Oh My Pi, ``omp`` CLI) runtime for Ouroboros orchestrator execution.

OMP is a Pi-family coding agent: ``omp --mode json <prompt>`` streams the same
JSONL event protocol as Pi (``session`` header, ``message_update`` deltas,
``message_end`` / ``turn_end`` / ``agent_end`` completions), so this runtime
mirrors :class:`ouroboros.orchestrator.pi_runtime.PiRuntime` with the
OMP-specific surface differences:

- Session resume uses ``--resume <id>`` (Pi uses ``--session <id>``).
- The tool allow-list vocabulary differs: ``Glob`` maps to OMP's ``glob``
  (not Pi's ``find``) and there is no ``ls`` built-in, so ``LS`` maps to
  ``glob`` as the closest directory-enumeration tool.
- ``--append-system-prompt``, ``--tools`` and ``--no-tools`` are documented
  flags of the ``omp`` CLI, so no capability probe is needed and those
  parameters are always passed natively.
- The generic ``default`` model sentinel is omitted rather than forwarded:
  OMP's ``--model`` does fuzzy model matching and has no ``default`` id.

Verified against omp v18's ``--help`` and JSON stream (2026-08).
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
from typing import Any

from ouroboros.core.errors import ProviderError
from ouroboros.core.session_signal import SessionSignalCapabilities
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    ResolvedWorkerCwd,
    RuntimeCapabilities,
    RuntimeHandle,
    SkillDispatchHandler,
    TaskResult,
    resolve_worker_cwd,
    worker_cwd_failure_message,
)
from ouroboros.orchestrator.skill_intercept import SkillInterceptor
from ouroboros.providers.codex_cli_stream import (
    iter_runtime_stream_lines,
    malformed_event_message,
    parse_json_event,
    terminate_runtime_process,
)

log = get_logger(__name__)

_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_LINE_BUFFER_BYTES = 50 * 1024 * 1024  # 50 MB

# Ouroboros speaks a Claude-style capitalized tool vocabulary while OMP's
# built-in tools are lowercase (``read``, ``bash``, ``edit``, ``write``,
# ``grep``, ``glob``). OMP has no ``ls`` built-in — ``glob`` is its directory
# enumeration surface — and ``find`` is only a Pi-compat alias, so both Glob
# and LS map to ``glob``. Unknown names pass through unchanged so
# extension/custom tool names (``mcp__*``, ``task``, ``todo``, …) keep
# working; OMP rejects genuinely unknown names at startup with a CLI usage
# error, which surfaces as a normal runtime error result.
_OMP_TOOL_FLAG_NAMES: dict[str, str] = {
    "Read": "read",
    "Write": "write",
    "Edit": "edit",
    "Bash": "bash",
    "Command": "bash",
    "Execute": "bash",
    "Glob": "glob",
    "Grep": "grep",
    "LS": "glob",
    "Ls": "glob",
}

# OMP's documented built-in tools (verified via the ``--tools`` validation
# error's valid-tool list). Kept as the unit-test lock for the mapping above.
OMP_BUILTIN_TOOLS = frozenset({"read", "write", "edit", "bash", "grep", "glob", "find", "lsp"})


class OmpRuntime:
    """Agent runtime that shells out to the locally installed OMP CLI.

    Invokes ``omp --mode json <prompt>`` and streams JSONL events.

    Event lifecycle (OMP JSON mode, Pi-compatible):
    - First line: session header ``{"type":"session","id":"<uuid>",...}``
    - ``message_update``: streaming content deltas (``thinking_delta`` /
      ``text_delta`` inside ``assistantMessageEvent``; only ``text_delta``
      is user-visible)
    - ``agent_end``: task complete, contains final messages array
    """

    _runtime_handle_backend = "omp"
    _runtime_backend = "omp"
    _requires_memory_gate = False
    _provider_name = "omp"
    _runtime_error_type = "OmpError"
    _log_namespace = "omp_runtime"
    _display_name = "OMP"
    _default_cli_name = "omp"
    _default_llm_backend = "omp"
    _tempfile_prefix = "ouroboros-omp-"
    _process_shutdown_timeout_seconds = 5.0
    _startup_output_timeout_seconds = 120.0
    _stdout_idle_timeout_seconds = 600.0
    _max_stderr_lines = 512

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | ResolvedWorkerCwd | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
        **_kwargs: Any,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode_requested = permission_mode is not None
        self._permission_mode = permission_mode
        self._model = model
        self._cwd = resolve_worker_cwd(cwd)
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or self._default_llm_backend
        self._skills_dir = Path(skills_dir).expanduser() if skills_dir is not None else None
        self._interceptor = SkillInterceptor(
            cwd=self._cwd,
            runtime_backend=self._runtime_backend,
            runtime_handle_backend=self._runtime_handle_backend,
            permission_mode=self._permission_mode,
            llm_backend=self._llm_backend,
            log_namespace=self._log_namespace,
            skills_dir=self._skills_dir,
            skill_dispatcher=self._skill_dispatcher,
        )
        if startup_output_timeout_seconds is not None:
            self._startup_output_timeout_seconds = (
                None if startup_output_timeout_seconds <= 0 else startup_output_timeout_seconds
            )
        if stdout_idle_timeout_seconds is not None:
            self._stdout_idle_timeout_seconds = (
                None if stdout_idle_timeout_seconds <= 0 else stdout_idle_timeout_seconds
            )

        log.info(
            f"{self._log_namespace}.initialized",
            cli_path=self._cli_path,
            cwd=self._cwd,
            model=model,
        )

    # -- AgentRuntime protocol properties ----------------------------------

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def llm_backend(self) -> str | None:
        return self._llm_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    @property
    def permission_mode_requested(self) -> bool:
        return self._permission_mode_requested

    @property
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            # ``--append-system-prompt`` and ``--tools`` are documented OMP
            # flags (present since the CLI's public release), so both the
            # system prompt and a non-empty tool allow-list are delivered as
            # native runtime parameters.
            system_prompt_support=ParamSupport.NATIVE,
            tool_restriction_support=ParamSupport.NATIVE,
            empty_tool_restriction_support=ParamSupport.NATIVE,
            permission_mode_support=ParamSupport.IGNORED,
            session_signals=SessionSignalCapabilities(
                inform_delivery=True,
                background_reply=True,
                after_turn_delivery=True,
            ),
        )

    # -- CLI resolution ----------------------------------------------------

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = shutil.which(self._default_cli_name) or self._default_cli_name
        path = Path(candidate).expanduser()
        return str(path) if path.exists() else candidate

    # -- Command building --------------------------------------------------

    def _build_command(
        self,
        *,
        prompt: str,
        resume_session_id: str | None = None,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
    ) -> list[str]:
        """Assemble the CLI argument list for ``omp --mode json <prompt>``.

        OMP's JSON mode accepts the task as a positional message argument
        (verified: stdin is not required and would duplicate the prompt).
        ``--append-system-prompt`` and ``--tools`` carry the system prompt
        and tool allow-list natively; ``--resume`` continues a prior
        session. The generic ``default`` model sentinel is omitted so OMP
        uses its own configured model.
        """
        command = [self._cli_path, "--mode", "json"]

        if self._model and self._model.strip() and self._model.strip() != "default":
            command.extend(["--model", self._model.strip()])

        if resume_session_id:
            if not _SAFE_SESSION_ID_PATTERN.match(resume_session_id):
                raise ValueError(f"Invalid resume_session_id: {resume_session_id!r}")
            command.extend(["--resume", resume_session_id])

        if system_prompt:
            command.extend(["--append-system-prompt", system_prompt])
        if tools:
            omp_names = ",".join(_OMP_TOOL_FLAG_NAMES.get(tool, tool) for tool in tools)
            command.extend(["--tools", omp_names])
        if tools == []:
            command.append("--no-tools")

        command.append(prompt)
        return command

    def _build_child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        return env

    # -- Stream parsing ----------------------------------------------------

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
    ) -> AsyncIterator[str]:
        async for line in iter_runtime_stream_lines(
            stream,
            display_name=self._display_name,
            first_chunk_timeout_seconds=first_chunk_timeout_seconds,
            chunk_timeout_seconds=chunk_timeout_seconds,
            max_buffer_bytes=_MAX_LINE_BUFFER_BYTES,
        ):
            yield line

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        max_lines: int | None = None,
    ) -> list[str]:
        if stream is None:
            return []
        if max_lines is not None and max_lines > 0:
            lines: deque[str] = deque(maxlen=max_lines)
        else:
            lines = deque()
        async for line in self._iter_stream_lines(stream):
            if line:
                lines.append(line)
        return list(lines)

    # -- Event parsing -----------------------------------------------------

    def _parse_event(self, line: str) -> dict[str, Any] | None:
        return parse_json_event(line)

    def _malformed_event_message(self, line: str) -> str:
        return malformed_event_message(line, display_name=self._display_name)

    def _extract_session_id(self, event: dict[str, Any]) -> str | None:
        """Extract session ID from the OMP session header event."""
        if event.get("type") == "session":
            sid = event.get("id")
            if isinstance(sid, str) and sid.strip():
                return sid.strip()
        return None

    def _extract_content_delta(self, event: dict[str, Any]) -> str | None:
        """Extract streaming text from OMP ``message_update`` events.

        OMP emits both ``thinking_delta`` and ``text_delta`` events inside
        ``assistantMessageEvent``; only ``text_delta`` is user-visible
        completion content, matching Pi's protocol shape.
        """
        if event.get("type") != "message_update":
            return None

        assistant_event = event.get("assistantMessageEvent")
        if isinstance(assistant_event, dict):
            assistant_event_type = assistant_event.get("type")
            if assistant_event_type and assistant_event_type != "text_delta":
                return None
            delta = assistant_event.get("delta")
            if isinstance(delta, str):
                return delta
            text = assistant_event.get("text") or assistant_event.get("content")
            if isinstance(text, str):
                return text

        # Compatibility fallback mirroring PiRuntime for event-shape drift.
        delta = event.get("delta") or event.get("content") or event.get("text")
        if isinstance(delta, str):
            return delta
        if isinstance(delta, dict):
            text = delta.get("text") or delta.get("content")
            return text if isinstance(text, str) else None
        return None

    def _extract_text_from_message(self, message: dict[str, Any]) -> str | None:
        """Extract user-visible text from an OMP assistant message payload."""
        content = message.get("content") or message.get("text") or ""
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            texts = []
            for item in content:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and (item.get("type") in {None, "text"}):
                        texts.append(text)
            joined = "".join(texts).strip()
            if joined:
                return joined
        return None

    def _extract_final_content(self, event: dict[str, Any]) -> str | None:
        """Extract final assistant text from OMP completion events."""
        event_type = event.get("type")
        if event_type == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                return self._extract_text_from_message(message)
            return None
        if event_type == "turn_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                return self._extract_text_from_message(message)
            return None
        if event_type != "agent_end":
            return None
        messages = event.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = self._extract_text_from_message(msg)
                if text:
                    return text
        return None

    def _extract_error_content(self, event: dict[str, Any]) -> str | None:
        """Extract OMP assistant error text from JSON events.

        OMP may report model/auth failures as assistant messages with
        ``stopReason: "error"`` while still exiting 0 (same family as Pi),
        so the runtime must treat those events as errors instead of
        relying only on process status.
        """

        def from_message(message: Any) -> str | None:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return None
            if message.get("stopReason") != "error":
                return None
            error = message.get("errorMessage") or message.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            return self._extract_text_from_message(message)

        event_type = event.get("type")
        if event_type in {"message_start", "message_end", "turn_end"}:
            return from_message(event.get("message"))
        if event_type == "agent_end":
            messages = event.get("messages") or []
            for msg in reversed(messages):
                error = from_message(msg)
                if error:
                    return error
        return None

    # -- Process management ------------------------------------------------

    async def _terminate_process(self, process: Any) -> None:
        await terminate_runtime_process(
            process,
            shutdown_timeout=self._process_shutdown_timeout_seconds,
            logger=log,
            log_namespace=self._log_namespace,
        )

    # -- RuntimeHandle management ------------------------------------------

    def _build_runtime_handle(
        self,
        session_id: str | None,
        current_handle: RuntimeHandle | None = None,
    ) -> RuntimeHandle | None:
        from dataclasses import replace
        from datetime import UTC, datetime

        if not session_id:
            return None
        updated_at = datetime.now(UTC).isoformat()
        if current_handle is not None:
            return replace(
                current_handle,
                backend=current_handle.backend or self._runtime_handle_backend,
                kind=current_handle.kind or "agent_runtime",
                native_session_id=session_id,
                cwd=current_handle.cwd or self._cwd,
                approval_mode=current_handle.approval_mode or self._permission_mode,
                updated_at=updated_at,
            )
        return RuntimeHandle(
            backend=self._runtime_handle_backend,
            kind="agent_runtime",
            native_session_id=session_id,
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            updated_at=updated_at,
        )

    # -- Main execute_task -------------------------------------------------

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        cwd_failure = worker_cwd_failure_message(
            self._cwd,
            runtime_backend=self._runtime_backend,
            resume_handle=resume_handle,
        )
        if cwd_failure is not None:
            yield cwd_failure
            return

        current_handle = resume_handle
        intercepted_messages = await self._interceptor.maybe_dispatch(prompt, current_handle)
        if intercepted_messages is not None:
            for message in intercepted_messages:
                if message.resume_handle is not None:
                    current_handle = message.resume_handle
                yield message
            return

        attempted_resume = (
            current_handle.native_session_id if current_handle is not None else resume_session_id
        )

        try:
            command = self._build_command(
                prompt=prompt,
                resume_session_id=attempted_resume,
                system_prompt=system_prompt,
                tools=tools,
            )
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to prepare {self._display_name}: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return

        log.info(
            f"{self._log_namespace}.task_started",
            command=command[:3],
            cwd=self._cwd,
        )

        process: Any | None = None
        process_finished = False
        process_terminated = False
        stderr_task: asyncio.Task[list[str]] | None = None

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_child_env(),
            )
        except FileNotFoundError as e:
            yield AgentMessage(
                type="result",
                content=(
                    f"{self._display_name} not found: {e}. "
                    "Install the omp CLI (Oh My Pi) and ensure `omp` is on PATH"
                ),
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to start {self._display_name}: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return

        stderr_task = asyncio.create_task(
            self._collect_stream_lines(process.stderr, max_lines=self._max_stderr_lines)
        )

        last_content = ""
        pending_final_content: str | None = None
        pending_error_content: str | None = None

        try:
            if process.stdout is not None:
                async for line in self._iter_stream_lines(
                    process.stdout,
                    first_chunk_timeout_seconds=self._startup_output_timeout_seconds,
                    chunk_timeout_seconds=self._stdout_idle_timeout_seconds,
                ):
                    if not line:
                        continue
                    event = self._parse_event(line)
                    if event is None:
                        if process is not None:
                            await self._terminate_process(process)
                        process_terminated = True
                        yield AgentMessage(
                            type="result",
                            content=self._malformed_event_message(line),
                            data={
                                "subtype": "error",
                                "error_type": "MalformedOmpEvent",
                            },
                            resume_handle=current_handle,
                        )
                        return

                    # Session header — extract ID
                    sid = self._extract_session_id(event)
                    if sid:
                        current_handle = self._build_runtime_handle(sid, current_handle)

                    # Streaming content
                    delta = self._extract_content_delta(event)
                    if delta:
                        last_content += delta
                        yield AgentMessage(
                            type="assistant",
                            content=delta,
                            data={"event_type": "message_update"},
                            resume_handle=current_handle,
                        )
                        continue

                    # Completion events carry the final assistant text, but
                    # process exit status still decides success vs error.
                    final_content = self._extract_final_content(event)
                    if final_content:
                        pending_final_content = final_content
                    error_content = self._extract_error_content(event)
                    if error_content:
                        pending_error_content = error_content
                    if event.get("type") == "agent_end":
                        break

        except TimeoutError as e:
            if process is not None:
                await self._terminate_process(process)
            process_terminated = True
            if stderr_task is not None:
                stderr_lines = await stderr_task
            else:
                stderr_lines = []
            final_message = "\n".join(stderr_lines).strip() or str(e)
            yield AgentMessage(
                type="result",
                content=final_message,
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate_process(process)
                process_terminated = True
            raise
        else:
            returncode = await process.wait()
            process_finished = True

            stderr_lines = await stderr_task if stderr_task else []
            if returncode != 0 or pending_error_content:
                stderr_text = "\n".join(stderr_lines).strip()
                if returncode == 0 and pending_error_content:
                    final_message = pending_error_content
                else:
                    final_message = (
                        stderr_text
                        or pending_error_content
                        or pending_final_content
                        or last_content
                        or ""
                    )
                if not final_message:
                    final_message = f"{self._display_name} exited with code {returncode}."
                yield AgentMessage(
                    type="result",
                    content=final_message,
                    data={
                        "subtype": "error",
                        "returncode": returncode,
                        "error_type": self._runtime_error_type,
                    },
                    resume_handle=current_handle,
                )
            else:
                final_message = (
                    pending_final_content or last_content or f"{self._display_name} task completed."
                )
                yield AgentMessage(
                    type="result",
                    content=final_message,
                    data={"subtype": "success", "returncode": returncode},
                    resume_handle=current_handle,
                )
        finally:
            if process is not None:
                if (
                    not process_finished
                    and not process_terminated
                    and getattr(process, "returncode", None) is None
                ):
                    await self._terminate_process(process)
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
        messages: list[AgentMessage] = []
        final_message = ""
        success = True
        final_handle = resume_handle

        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)
            if message.resume_handle is not None:
                final_handle = message.resume_handle
            if message.is_final:
                final_message = message.content
                success = not message.is_error

        if not success:
            return Result.err(
                ProviderError(
                    message=final_message,
                    provider=self._provider_name,
                    details={"messages": [m.content for m in messages]},
                )
            )

        return Result.ok(
            TaskResult(
                success=success,
                final_message=final_message,
                messages=tuple(messages),
                session_id=final_handle.native_session_id if final_handle else None,
                resume_handle=final_handle,
            )
        )


__all__ = ["OMP_BUILTIN_TOOLS", "OmpRuntime"]
