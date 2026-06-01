"""Pi (pi.dev) CLI runtime for Ouroboros orchestrator execution.

Drives workflows through the Pi CLI (`pi --mode json`), streaming JSONL
events and normalising them into :class:`AgentMessage` values.

Pi JSON mode reference: https://pi.dev/docs/latest/json
"""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeHandle,
    SkillDispatchHandler,
    TaskResult,
)

log = get_logger(__name__)

_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_LINE_BUFFER_BYTES = 50 * 1024 * 1024  # 50 MB


class PiRuntime:
    """Agent runtime that shells out to the locally installed Pi CLI.

    Invokes ``pi --mode json <prompt>`` and streams JSONL events.

    Event lifecycle (pi.dev JSON mode):
    - First line: session header ``{"type":"session","id":"<uuid>",...}``
    - ``message_update``: streaming content deltas
    - ``agent_end``: task complete, contains final messages array
    """

    _runtime_handle_backend = "pi"
    _runtime_backend = "pi"
    _requires_memory_gate = False
    _provider_name = "pi"
    _runtime_error_type = "PiError"
    _log_namespace = "pi_runtime"
    _display_name = "Pi"
    _default_cli_name = "pi"
    _default_llm_backend = "pi"
    _tempfile_prefix = "ouroboros-pi-"
    _process_shutdown_timeout_seconds = 5.0
    _startup_output_timeout_seconds = 120.0
    _stdout_idle_timeout_seconds = 600.0
    _max_stderr_lines = 512

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        **_kwargs: Any,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = permission_mode
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or self._default_llm_backend

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

    # -- CLI resolution ----------------------------------------------------

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = shutil.which(self._default_cli_name) or self._default_cli_name
        path = Path(candidate).expanduser()
        return str(path) if path.exists() else candidate

    # -- Command building --------------------------------------------------

    def _build_command(self, *, resume_session_id: str | None = None) -> list[str]:
        """Assemble the CLI argument list for ``pi --mode json``.

        Prompt is piped via stdin (avoids ARG_MAX limits).
        Pi reads stdin when it detects a non-TTY input stream.
        """
        command = [self._cli_path, "--mode", "json"]

        if self._model:
            command.extend(["--model", self._model.strip()])

        if resume_session_id:
            if not _SAFE_SESSION_ID_PATTERN.match(resume_session_id):
                raise ValueError(f"Invalid resume_session_id: {resume_session_id!r}")
            command.extend(["--session", resume_session_id])

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
        if stream is None:
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        buffer_byte_estimate = 0
        saw_chunk = False

        while True:
            timeout_seconds: float | None = None
            if not saw_chunk:
                timeout_seconds = first_chunk_timeout_seconds
            elif chunk_timeout_seconds is not None:
                timeout_seconds = chunk_timeout_seconds

            try:
                if timeout_seconds is None:
                    chunk = await stream.read(16384)
                else:
                    chunk = await asyncio.wait_for(stream.read(16384), timeout=timeout_seconds)
            except TimeoutError as exc:
                phase = "startup" if not saw_chunk else "idle"
                raise TimeoutError(
                    f"{self._display_name} produced no stdout during {phase} "
                    f"window ({timeout_seconds:.0f}s)"
                ) from exc
            if not chunk:
                break

            saw_chunk = True
            decoded = decoder.decode(chunk)
            buffer += decoded
            buffer_byte_estimate += len(decoded) * 4
            if buffer_byte_estimate > _MAX_LINE_BUFFER_BYTES:
                raise ProviderError(f"JSONL line buffer exceeded {_MAX_LINE_BUFFER_BYTES} bytes")
            while True:
                newline_index = buffer.find("\n")
                if newline_index < 0:
                    break
                line = buffer[:newline_index]
                buffer = buffer[newline_index + 1 :]
                buffer_byte_estimate = len(buffer) * 4
                yield line.rstrip("\r")

        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer.rstrip("\r")

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
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    def _extract_session_id(self, event: dict[str, Any]) -> str | None:
        """Extract session ID from the pi session header event."""
        if event.get("type") == "session":
            sid = event.get("id")
            if isinstance(sid, str) and sid.strip():
                return sid.strip()
        return None

    def _extract_content_delta(self, event: dict[str, Any]) -> str | None:
        """Extract streaming content from message_update events."""
        if event.get("type") != "message_update":
            return None
        delta = event.get("delta") or event.get("content") or event.get("text")
        if isinstance(delta, str):
            return delta
        if isinstance(delta, dict):
            return delta.get("text") or delta.get("content")
        return None

    def _extract_final_content(self, event: dict[str, Any]) -> str | None:
        """Extract final assistant message from agent_end event."""
        if event.get("type") != "agent_end":
            return None
        messages = event.get("messages") or []
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                content = msg.get("content") or msg.get("text") or ""
                if isinstance(content, str) and content.strip():
                    return content.strip()
                if isinstance(content, list):
                    texts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict) and c.get("type") == "text"
                    ]
                    joined = "".join(texts).strip()
                    if joined:
                        return joined
        return None

    # -- Process management ------------------------------------------------

    async def _terminate_process(self, process: Any) -> None:
        if getattr(process, "returncode", None) is not None:
            return
        terminate = getattr(process, "terminate", None)
        kill = getattr(process, "kill", None)
        try:
            if callable(terminate):
                terminate()
            elif callable(kill):
                kill()
            else:
                return
        except ProcessLookupError:
            return
        except Exception as exc:
            log.warning(f"{self._log_namespace}.process_terminate_failed", error=str(exc))
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=self._process_shutdown_timeout_seconds)
            return
        except (TimeoutError, ProcessLookupError):
            pass

        if callable(kill):
            with contextlib.suppress(ProcessLookupError, Exception):
                kill()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, Exception):
                await asyncio.wait_for(
                    process.wait(), timeout=self._process_shutdown_timeout_seconds
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
        current_handle = resume_handle
        attempted_resume = (
            current_handle.native_session_id if current_handle is not None else resume_session_id
        )

        composed_parts = []
        if system_prompt:
            composed_parts.append(f"## System Instructions\n{system_prompt}")
        if tools:
            tool_list = "\n".join(f"- {t}" for t in tools)
            composed_parts.append(f"## Tooling Guidance\nPrefer these tools:\n{tool_list}")
        composed_parts.append(prompt)
        composed_prompt = "\n\n".join(p for p in composed_parts if p.strip())

        try:
            command = self._build_command(resume_session_id=attempted_resume)
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
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_child_env(),
            )
        except FileNotFoundError as e:
            yield AgentMessage(
                type="result",
                content=(
                    f"{self._display_name} not found: {e}. "
                    "Install with: npm install -g --ignore-scripts @earendil-works/pi-coding-agent"
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

        if process.stdin is not None:
            try:
                if composed_prompt:
                    process.stdin.write(composed_prompt.encode("utf-8"))
                    await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                log.warning(f"{self._log_namespace}.stdin_write_failed", error=str(exc))
                with contextlib.suppress(OSError):
                    process.stdin.close()

        stderr_task = asyncio.create_task(
            self._collect_stream_lines(process.stderr, max_lines=self._max_stderr_lines)
        )

        last_content = ""
        yielded_final = False

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
                        continue

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

                    # Task complete
                    if event.get("type") == "agent_end":
                        final_content = self._extract_final_content(event) or last_content
                        yielded_final = True
                        yield AgentMessage(
                            type="result",
                            content=final_content,
                            data={"subtype": "success"},
                            resume_handle=current_handle,
                        )

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

            if yielded_final:
                return

            stderr_lines = await stderr_task if stderr_task else []
            if returncode != 0:
                final_message = "\n".join(stderr_lines).strip() or last_content or ""
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
                final_message = last_content or f"{self._display_name} task completed."
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


__all__ = ["PiRuntime"]
