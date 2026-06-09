"""GJC CLI RPC runtime for Ouroboros orchestrator execution."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator
import contextlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any
from uuid import uuid4

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
from ouroboros.providers.gjc_rpc_protocol import (
    GjcCommandError,
    GjcProtocolError,
    UnsupportedGjcRpcFrame,
    is_passive_lifecycle_event,
    unsupported_frame_error,
    validate_response_ack,
)

log = get_logger(__name__)

_MAX_LINE_BUFFER_BYTES = 50 * 1024 * 1024

class MalformedGjcEvent(ProviderError):
    """Raised when GJC emits malformed JSONL."""


class GjcExitError(ProviderError):
    """Raised when GJC exits non-zero."""


class GjcRuntime:
    """Agent runtime that drives ``gjc --mode rpc`` over persistent stdio."""

    _runtime_handle_backend = "gjc"
    _runtime_backend = "gjc"
    _requires_memory_gate = False
    _provider_name = "gjc"
    _runtime_error_type = "GjcError"
    _log_namespace = "gjc_runtime"
    _display_name = "GJC"
    _default_cli_name = "gjc"
    _default_llm_backend = "gjc"
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
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
        **_kwargs: Any,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = permission_mode
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
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
            self._startup_output_timeout_seconds = None if startup_output_timeout_seconds <= 0 else startup_output_timeout_seconds
        if stdout_idle_timeout_seconds is not None:
            self._stdout_idle_timeout_seconds = None if stdout_idle_timeout_seconds <= 0 else stdout_idle_timeout_seconds
        log.info(f"{self._log_namespace}.initialized", cli_path=self._cli_path, cwd=self._cwd, model=model)

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
    def capabilities(self) -> RuntimeCapabilities:
        return RuntimeCapabilities(skill_dispatch=True, targeted_resume=False, structured_output=True)

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = shutil.which(self._default_cli_name) or self._default_cli_name
        path = Path(candidate).expanduser()
        return str(path) if path.exists() else candidate

    def _build_command(self, *, prompt: str) -> list[str]:
        return [self._cli_path, "--mode", "rpc"]

    def _build_child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        return env

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
            timeout_seconds = first_chunk_timeout_seconds if not saw_chunk else chunk_timeout_seconds
            try:
                chunk = await stream.read(16384) if timeout_seconds is None else await asyncio.wait_for(stream.read(16384), timeout=timeout_seconds)
            except TimeoutError as exc:
                phase = "startup" if not saw_chunk else "idle"
                raise TimeoutError(f"{self._display_name} produced no stdout during {phase} window ({timeout_seconds:.0f}s)") from exc
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

    async def _collect_stream_lines(self, stream: asyncio.StreamReader | None, *, max_lines: int | None = None) -> list[str]:
        if stream is None:
            return []
        lines: deque[str] = deque(maxlen=max_lines if max_lines and max_lines > 0 else None)
        async for line in self._iter_stream_lines(stream):
            if line:
                lines.append(line)
        return list(lines)

    def _parse_event(self, line: str) -> dict[str, Any]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MalformedGjcEvent(message=self._malformed_event_message(line), provider=self._provider_name) from exc
        if not isinstance(event, dict):
            raise MalformedGjcEvent(message=self._malformed_event_message(line), provider=self._provider_name)
        return event

    def _malformed_event_message(self, line: str) -> str:
        preview = line.strip()
        if len(preview) > 240:
            preview = f"{preview[:237]}..."
        return f"Malformed {self._display_name} JSON event: {preview}"

    def _extract_content_delta(self, event: dict[str, Any]) -> str | None:
        if event.get("type") != "message_update":
            return None
        assistant_event = event.get("assistantMessageEvent")
        if isinstance(assistant_event, dict):
            if assistant_event.get("type") not in {None, "text_delta"}:
                return None
            delta = assistant_event.get("delta")
            if isinstance(delta, str):
                return delta
            text = assistant_event.get("text") or assistant_event.get("content")
            return text if isinstance(text, str) else None
        return None

    def _extract_text_from_message(self, message: dict[str, Any]) -> str | None:
        content = message.get("content") or message.get("text") or ""
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            texts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    texts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if isinstance(text, str) and item.get("type") in {None, "text"}:
                        texts.append(text)
            joined = "".join(texts).strip()
            if joined:
                return joined
        return None

    def _extract_final_content(self, event: dict[str, Any]) -> str | None:
        event_type = event.get("type")
        if event_type in {"message_end", "turn_end"}:
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                return self._extract_text_from_message(message)
            return None
        if event_type != "agent_end":
            return None
        for msg in reversed(event.get("messages") or []):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                text = self._extract_text_from_message(msg)
                if text:
                    return text
        return None

    def _extract_error_content(self, event: dict[str, Any]) -> str | None:
        def from_message(message: Any) -> str | None:
            if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("stopReason") != "error":
                return None
            error = message.get("errorMessage") or message.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            return self._extract_text_from_message(message)
        if event.get("type") in {"message_start", "message_end", "turn_end"}:
            return from_message(event.get("message"))
        if event.get("type") == "agent_end":
            for msg in reversed(event.get("messages") or []):
                error = from_message(msg)
                if error:
                    return error
        return None

    def _provider_error(self, exc: Exception) -> ProviderError:
        if isinstance(exc, ProviderError):
            return exc
        return ProviderError(message=str(exc), provider=self._provider_name)

    def _check_unsupported_or_unknown(self, event: dict[str, Any]) -> None:
        error = unsupported_frame_error(event, provider=self._provider_name)
        if error is not None:
            log.warning(f"{self._log_namespace}.unsupported_frame", frame_type=error.details.get("frame_type"), id=error.details.get("id"))
            raise error

    def _model_override(self) -> tuple[str, str] | None:
        if not self._model or "/" not in self._model:
            return None
        provider, model_id = self._model.split("/", 1)
        provider = provider.strip()
        model_id = model_id.strip()
        if not provider or not model_id:
            return None
        return provider, model_id

    async def _send_command(self, process: Any, payload: dict[str, Any]) -> None:
        stdin = getattr(process, "stdin", None)
        if stdin is None:
            raise GjcProtocolError(message="GJC stdin pipe is unavailable", provider=self._provider_name)
        stdin.write((json.dumps(payload) + "\n").encode())
        drain = getattr(stdin, "drain", None)
        if callable(drain):
            await drain()

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
                await asyncio.wait_for(process.wait(), timeout=self._process_shutdown_timeout_seconds)

    def _close_stdin(self, process: Any) -> None:
        stdin = getattr(process, "stdin", None)
        if stdin is not None:
            with contextlib.suppress(Exception):
                stdin.close()

    def _error_message(self, exc: Exception, stderr_lines: list[str] | None = None) -> str:
        stderr_text = "\n".join(stderr_lines or []).strip()
        return stderr_text or str(exc)

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        current_handle = None
        if resume_handle is not None or resume_session_id is not None:
            log.info(f"{self._log_namespace}.resume_ignored_non_resumable")
        intercepted_messages = await self._interceptor.maybe_dispatch(prompt, None)
        if intercepted_messages is not None:
            for message in intercepted_messages:
                yield AgentMessage(type=message.type, content=message.content, data=message.data, tool_name=message.tool_name)
            return
        parts = []
        if system_prompt:
            parts.append(f"## System Instructions\n{system_prompt}")
        if tools:
            parts.append("## Tooling Guidance\nPrefer these tools:\n" + "\n".join(f"- {tool}" for tool in tools))
        parts.append(prompt)
        composed_prompt = "\n\n".join(part for part in parts if part.strip())
        command = self._build_command(prompt=composed_prompt)
        log.info(f"{self._log_namespace}.task_started", command=command, cwd=self._cwd)
        process: Any | None = None
        stderr_task: asyncio.Task[list[str]] | None = None
        process_finished = False
        process_terminated = False
        spawn_started = time.monotonic()
        task_started = spawn_started
        last_content = ""
        pending_final_content: str | None = None
        pending_error_content: str | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_child_env(),
            )
            stderr_task = asyncio.create_task(self._collect_stream_lines(process.stderr, max_lines=self._max_stderr_lines))
            line_iter = self._iter_stream_lines(process.stdout, first_chunk_timeout_seconds=self._startup_output_timeout_seconds, chunk_timeout_seconds=self._stdout_idle_timeout_seconds)
            try:
                ready_event = self._parse_event(await anext(line_iter))
                self._check_unsupported_or_unknown(ready_event)
            except StopAsyncIteration as exc:
                raise TimeoutError("GJC produced no ready event") from exc
            if ready_event.get("type") != "ready":
                raise GjcProtocolError(message=f"Expected GJC ready frame before any other frame, got {ready_event.get('type')!r}", provider=self._provider_name)
            spawn_to_ready_ms = int((time.monotonic() - spawn_started) * 1000)
            log.info(f"{self._log_namespace}.ready_received", spawn_to_ready_ms=spawn_to_ready_ms)
            override = self._model_override()
            if override is not None:
                model_command_id = f"set_model-{uuid4().hex}"
                await self._send_command(process, {"id": model_command_id, "type": "set_model", "provider": override[0], "modelId": override[1]})
                event = self._parse_event(await anext(line_iter))
                self._check_unsupported_or_unknown(event)
                validate_response_ack(event, command_id=model_command_id, command="set_model", provider=self._provider_name)
                log.info(f"{self._log_namespace}.model_acknowledged")
            prompt_command_id = f"prompt-{uuid4().hex}"
            await self._send_command(process, {"id": prompt_command_id, "type": "prompt", "message": composed_prompt})
            acked = False
            async for line in line_iter:
                if not line:
                    continue
                event = self._parse_event(line)
                event_type = event.get("type")
                self._check_unsupported_or_unknown(event)
                if event_type == "response" and event.get("id") == prompt_command_id:
                    validate_response_ack(event, command_id=prompt_command_id, command="prompt", provider=self._provider_name)
                    if not acked:
                        acked = True
                        prompt_ack_ms = int((time.monotonic() - task_started) * 1000)
                        log.info(f"{self._log_namespace}.prompt_acknowledged", prompt_ack_ms=prompt_ack_ms)
                    continue
                if not acked:
                    raise GjcProtocolError(message=f"Expected GJC prompt response before streaming, got {event_type!r}", provider=self._provider_name)
                if is_passive_lifecycle_event(event):
                    log.debug(f"{self._log_namespace}.passive_lifecycle_event", event_type=event_type)
                    continue
                error_content = self._extract_error_content(event)
                if error_content:
                    pending_error_content = error_content
                delta = self._extract_content_delta(event)
                if delta:
                    last_content += delta
                    yield AgentMessage(type="assistant", content=delta, data={"event_type": "message_update"})
                    continue
                final_content = self._extract_final_content(event)
                if final_content:
                    pending_final_content = final_content
                if event_type == "agent_end":
                    break
            else:
                raise GjcProtocolError(message="GJC stream ended before agent_end", provider=self._provider_name)
            self._close_stdin(process)
            returncode = await process.wait()
            process_finished = True
            stderr_lines = await stderr_task if stderr_task else []
            task_wall_ms = int((time.monotonic() - task_started) * 1000)
            if returncode != 0:
                raise GjcExitError(message="\n".join(stderr_lines).strip() or f"GJC exited with code {returncode}.", provider=self._provider_name, details={"returncode": returncode})
            if pending_error_content:
                raise ProviderError(message=pending_error_content, provider=self._provider_name)
            final_message = pending_final_content or last_content or "GJC task completed."
            log.info(f"{self._log_namespace}.task_completed", task_wall_ms=task_wall_ms, returncode=returncode)
            yield AgentMessage(type="result", content=final_message, data={"subtype": "success", "returncode": returncode})
        except (TimeoutError, MalformedGjcEvent, GjcProtocolError, GjcCommandError, UnsupportedGjcRpcFrame, GjcExitError, ProviderError) as exc:
            if process is not None and not isinstance(exc, GjcExitError):
                await self._terminate_process(process)
                process_terminated = True
            stderr_lines = await stderr_task if stderr_task else []
            provider_exc = self._provider_error(exc)
            log.info(f"{self._log_namespace}.task_failed", error_type=type(exc).__name__, error=str(exc))
            yield AgentMessage(type="result", content=(self._error_message(provider_exc, stderr_lines) if isinstance(exc, TimeoutError) else provider_exc.message), data={"subtype": "error", "error_type": type(exc).__name__, **({"returncode": provider_exc.details.get("returncode")} if isinstance(provider_exc.details, dict) and "returncode" in provider_exc.details else {})}, resume_handle=current_handle)
        except asyncio.CancelledError:
            if process is not None:
                await self._terminate_process(process)
            raise
        except FileNotFoundError as exc:
            yield AgentMessage(type="result", content=f"{self._display_name} not found: {exc}", data={"subtype": "error", "error_type": type(exc).__name__})
        except Exception as exc:
            if process is not None:
                await self._terminate_process(process)
                process_terminated = True
            yield AgentMessage(type="result", content=f"Failed to run {self._display_name}: {exc}", data={"subtype": "error", "error_type": type(exc).__name__})
        finally:
            if process is not None:
                self._close_stdin(process)
                if not process_finished and not process_terminated and getattr(process, "returncode", None) is None:
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
        async for message in self.execute_task(prompt=prompt, tools=tools, system_prompt=system_prompt, resume_handle=resume_handle, resume_session_id=resume_session_id):
            messages.append(message)
            if message.is_final:
                final_message = message.content
                success = not message.is_error
        if not success:
            return Result.err(ProviderError(message=final_message, provider=self._provider_name, details={"messages": [message.content for message in messages]}))
        return Result.ok(TaskResult(success=success, final_message=final_message, messages=tuple(messages), session_id=None, resume_handle=None))


__all__ = [
    "GjcRuntime",
    "GjcCommandError",
    "GjcExitError",
    "GjcProtocolError",
    "MalformedGjcEvent",
    "UnsupportedGjcRpcFrame",
]
