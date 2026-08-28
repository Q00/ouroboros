"""GJC CLI adapter for LLM completion via persistent RPC mode."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import json
from pathlib import Path
import re
from typing import Any
from uuid import uuid4

from ouroboros.config import get_gjc_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.json_utils import extract_json_payload
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter
from ouroboros.providers.gjc_rpc_protocol import (
    SUPPORTED_EVENT_TYPES,
    GjcProtocolError,
    is_passive_lifecycle_event,
    unsupported_enveloped_frame_error,
    unsupported_frame_error,
    unwrap_event_envelope,
    validate_response_ack,
)
from ouroboros.providers.profiles import resolve_completion_profile_result
from ouroboros.providers.response_format import (
    build_response_format_directive,
    validate_response_format_payload,
)

_SAFE_MODEL_PART_PATTERN = re.compile(r"^[A-Za-z0-9_.:@-]+$")


class GjcLLMAdapter(CodexCliLLMAdapter):
    """LLM adapter backed by ``gjc --mode rpc``."""

    _provider_name = "gjc"
    _display_name = "GJC CLI"
    _default_cli_name = "gjc"
    _log_namespace = "gjc_llm_adapter"
    _completion_profile_backend = "gjc"

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 1,
        on_message: Any | None = None,
        max_retries: int = 3,
        ephemeral: bool = True,
        timeout: float | None = None,
        runtime_profile: str | None = None,
    ) -> None:
        del runtime_profile
        super().__init__(
            cli_path=cli_path,
            cwd=cwd,
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            on_message=on_message,
            max_retries=max_retries,
            ephemeral=ephemeral,
            timeout=timeout,
            runtime_profile=None,
        )

    def _get_configured_cli_path(self) -> str | None:
        """Resolve GJC CLI path from config helpers."""
        return get_gjc_cli_path()

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """GJC RPC mode has no separate permission-mode flag surface."""
        return (permission_mode or "default").strip() or "default"

    def _build_permission_args(self) -> list[str]:
        """GJC RPC mode does not expose Codex-style permission flags."""
        return []

    def _build_response_format_directive(
        self,
        response_format: dict[str, object] | None,
    ) -> str | None:
        """Translate response_format into cooperative GJC prompt instructions."""
        return build_response_format_directive(response_format)

    def _validate_response_format_payload(
        self,
        payload: str,
        response_format: dict[str, object],
    ) -> str | None:
        """Validate extracted JSON against the requested response_format."""
        return validate_response_format_payload(payload, response_format)

    def _compose_prompt(self, messages: list[Message]) -> str:
        return "\n\n".join(f"{message.role.value}: {message.content}" for message in messages)

    def _parse_provider_model(self, config: CompletionConfig) -> tuple[str, str] | None:
        if not config.model_is_explicit or not config.model or config.model == "default":
            return None
        provider, sep, model_id = config.model.partition("/")
        if not sep or not provider or not model_id:
            return None
        if not _SAFE_MODEL_PART_PATTERN.fullmatch(provider):
            return None
        # Model ids may themselves be slash-qualified (openrouter/google/gemini-3-pro);
        # validate each segment instead of rejecting the whole id.
        if not all(_SAFE_MODEL_PART_PATTERN.fullmatch(part) for part in model_id.split("/")):
            return None
        return provider, model_id

    async def _write_rpc(self, process: Any, payload: dict[str, object]) -> None:
        if process.stdin is None:
            raise ProviderError(
                message="GJC RPC stdin is unavailable",
                provider=self._provider_name,
                details={"payload_type": payload.get("type")},
            )
        process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        drain = getattr(process.stdin, "drain", None)
        if callable(drain):
            await drain()

    def _parse_rpc_line(self, line: str) -> dict[str, Any]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ProviderError(
                message="Malformed JSONL from GJC RPC",
                provider=self._provider_name,
                details={"line": line[:240], "error": str(exc)},
            ) from exc
        if not isinstance(event, dict):
            raise ProviderError(
                message="Malformed JSONL from GJC RPC",
                provider=self._provider_name,
                details={"line": line[:240], "reason": "event is not an object"},
            )
        return event

    def _extract_message_delta(self, event: dict[str, Any]) -> str:
        if event.get("type") != "message_update":
            return ""
        assistant_event = event.get("assistantMessageEvent")
        if isinstance(assistant_event, dict):
            # Only text deltas count as completion output; thinking_* and
            # text_start/text_end variants would duplicate or pollute content.
            if assistant_event.get("type") not in {None, "text_delta"}:
                return ""
            delta = (
                assistant_event.get("delta")
                or assistant_event.get("text")
                or assistant_event.get("content")
            )
            return delta if isinstance(delta, str) else ""
        delta = event.get("delta") or event.get("text") or event.get("content")
        if isinstance(delta, str):
            return delta
        if isinstance(delta, dict):
            text = delta.get("text") or delta.get("content")
            return text if isinstance(text, str) else ""
        return ""

    def _extract_assistant_text(self, message: Any) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content") or message.get("text") or ""
        if isinstance(content, str):
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
            return "".join(texts).strip()
        return ""

    def _extract_agent_end_text(self, event: dict[str, Any]) -> str:
        messages = event.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                if isinstance(message, dict) and message.get("role") == "assistant":
                    text = self._extract_assistant_text(message)
                    if text:
                        return text
        message = event.get("message")
        if isinstance(message, dict) and message.get("role") == "assistant":
            return self._extract_assistant_text(message)
        text = event.get("content") or event.get("text")
        return text.strip() if isinstance(text, str) else ""

    def _extract_agent_error(self, event: dict[str, Any]) -> str | None:
        def from_message(message: Any) -> str | None:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                return None
            if message.get("stopReason") != "error":
                return None
            error = message.get("errorMessage") or message.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            text = self._extract_assistant_text(message)
            return text or "GJC assistant reported an error"

        if event.get("stopReason") == "error":
            error = event.get("errorMessage") or event.get("error")
            if isinstance(error, str) and error.strip():
                return error.strip()
            return "GJC assistant reported an error"
        error = from_message(event.get("message"))
        if error:
            return error
        messages = event.get("messages")
        if isinstance(messages, list):
            for message in reversed(messages):
                error = from_message(message)
                if error:
                    return error
        return None

    def _check_unsupported_or_unknown(self, event: dict[str, Any]) -> None:
        error = unsupported_frame_error(event, provider=self._provider_name)
        if error is not None:
            raise error

    async def _complete_once(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        process: Any | None = None
        stderr_task: asyncio.Task[list[str]] | None = None
        command = [self._cli_path, "--mode", "rpc"]
        if self._ephemeral:
            command.append("--no-session")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_child_env(),
            )
        except FileNotFoundError as exc:
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} not found: {exc}",
                    provider=self._provider_name,
                    details={"cli_path": self._cli_path},
                )
            )
        except Exception as exc:
            return Result.err(
                ProviderError(
                    message=f"Failed to start {self._display_name}: {exc}",
                    provider=self._provider_name,
                    details={"cli_path": self._cli_path, "error_type": type(exc).__name__},
                )
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        content = ""
        final_content = ""
        prompt_id = f"prompt-{uuid4().hex}"
        timeout_seconds = self._effective_timeout_seconds()

        async def fail(
            message: str, details: dict[str, object] | None = None
        ) -> Result[CompletionResponse, ProviderError]:
            await self._terminate_process(process)
            return Result.err(
                ProviderError(message=message, provider=self._provider_name, details=details or {})
            )

        try:
            stderr_task = asyncio.create_task(self._collect_stream_lines(process.stderr))
            stdout_iter = self._iter_stream_lines(process.stdout)

            async def next_event() -> dict[str, Any]:
                async for raw_line in stdout_iter:
                    line = raw_line.strip()
                    if not line:
                        continue
                    stdout_lines.append(line)
                    return self._parse_rpc_line(line)
                raise ProviderError(
                    message="GJC RPC stream ended before completion",
                    provider=self._provider_name,
                    details={"stdout_lines": stdout_lines[-5:]},
                )

            async def run_protocol() -> None:
                nonlocal content, final_content
                ready = await next_event()
                self._check_unsupported_or_unknown(ready)
                if ready.get("type") != "ready":
                    raise ProviderError(
                        message="GJC RPC did not emit a ready frame",
                        provider=self._provider_name,
                        details={"event": ready},
                    )

                provider_model = self._parse_provider_model(config)
                if provider_model is not None:
                    provider, model_id = provider_model
                    set_model_id = f"set-model-{uuid4().hex}"
                    await self._write_rpc(
                        process,
                        {
                            "id": set_model_id,
                            "type": "set_model",
                            "provider": provider,
                            "modelId": model_id,
                        },
                    )
                    while True:
                        ack = await next_event()
                        if unwrap_event_envelope(ack, provider=self._provider_name) is not None:
                            continue
                        self._check_unsupported_or_unknown(ack)
                        validate_response_ack(
                            ack,
                            command_id=set_model_id,
                            command="set_model",
                            provider=self._provider_name,
                        )
                        break

                await self._write_rpc(
                    process,
                    {"id": prompt_id, "type": "prompt", "message": self._compose_prompt(messages)},
                )

                prompt_acked = False
                while True:
                    frame = await next_event()
                    event = unwrap_event_envelope(frame, provider=self._provider_name)
                    if event is None:
                        event = frame
                        self._check_unsupported_or_unknown(event)
                    elif event.get("type") not in SUPPORTED_EVENT_TYPES:
                        error = unsupported_enveloped_frame_error(
                            event, provider=self._provider_name
                        )
                        if error is not None:
                            raise error
                        continue
                    event_id = event.get("id")
                    if event.get("type") == "response" and event_id == prompt_id:
                        validate_response_ack(
                            event,
                            command_id=prompt_id,
                            command="prompt",
                            provider=self._provider_name,
                        )
                        prompt_acked = True
                        continue
                    if not prompt_acked:
                        raise GjcProtocolError(
                            message=f"Expected GJC prompt response before streaming, got {event.get('type')!r}",
                            provider=self._provider_name,
                        )
                    if event_id not in {None, prompt_id}:
                        continue
                    if is_passive_lifecycle_event(event):
                        continue
                    error = self._extract_agent_error(event)
                    if error:
                        raise ProviderError(
                            message=error,
                            provider=self._provider_name,
                            details={"event_type": event.get("type"), "event": event},
                        )
                    delta = self._extract_message_delta(event)
                    if delta:
                        content += delta
                        if self._on_message is not None:
                            self._on_message("assistant", delta)
                    if event.get("type") == "agent_end":
                        final_content = self._extract_agent_end_text(event)
                        return

            async with asyncio.timeout(timeout_seconds):
                await run_protocol()
        except ProviderError as exc:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                if stderr_task is not None:
                    stderr_lines = await stderr_task
            return await fail(
                exc.message,
                {
                    **exc.details,
                    "returncode": getattr(process, "returncode", None),
                    "stderr": "\n".join(stderr_lines).strip(),
                    "error_type": type(exc).__name__,
                },
            )
        except TimeoutError:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
            return await fail(
                f"{self._display_name} request timed out after {timeout_seconds:.1f}s",
                {
                    "timed_out": True,
                    "timeout_seconds": timeout_seconds,
                    "timeout_was_default": self._timeout_is_default_ceiling(),
                    "partial_content": content,
                },
            )
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        finally:
            with contextlib.suppress(Exception):
                if process.stdin is not None:
                    process.stdin.close()

        # ``agent_end`` already arrived, so the CLI should be exiting on its own.
        # Bound both the stderr drain and the reap with the shutdown budget: a
        # lingering child must not hang a completion that already has its answer.
        returncode: int | None
        try:
            async with asyncio.timeout(self._process_shutdown_timeout_seconds):
                if stderr_task is not None:
                    stderr_lines = await stderr_task
                returncode = await process.wait()
        except TimeoutError:
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await stderr_task
            await self._terminate_process(process)
            returncode = None
        if returncode is not None and returncode != 0:
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} exited with code {returncode}",
                    provider=self._provider_name,
                    details={"returncode": returncode, "stderr": "\n".join(stderr_lines).strip()},
                )
            )
        response_content = final_content or content
        if not response_content:
            return Result.err(
                ProviderError(
                    message=f"Empty response from {self._display_name}",
                    provider=self._provider_name,
                    details={"returncode": returncode},
                )
            )
        return Result.ok(
            CompletionResponse(
                content=response_content,
                model=config.model or "default",
                usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                finish_reason="stop",
                raw_response={"returncode": returncode},
            )
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a GJC RPC completion request, including soft structured output support."""
        if not config.response_format:
            return await self._complete_once(messages, config)

        profile_result = resolve_completion_profile_result(
            config,
            backend=self._completion_profile_backend,
        )
        if profile_result.is_err:
            return Result.err(profile_result.error)
        effective_config = profile_result.value.config
        directive = self._build_response_format_directive(effective_config.response_format)
        if not directive:
            return Result.err(
                ProviderError(
                    message="Unsupported GJC structured response_format request",
                    provider=self._provider_name,
                    details={
                        "response_format_type": effective_config.response_format.get("type"),
                    },
                )
            )

        patched_messages = [Message(role=MessageRole.SYSTEM, content=directive), *messages]
        patched_config = replace(effective_config, response_format=None)
        attempts = max(1, self._max_retries)
        last_response_preview = ""
        for _attempt in range(attempts):
            result = await self._complete_once(patched_messages, patched_config)
            if result.is_err:
                return result
            last_response_preview = result.value.content[:240]
            extracted = extract_json_payload(result.value.content)
            if not extracted:
                continue
            validation_error = self._validate_response_format_payload(
                extracted,
                effective_config.response_format,
            )
            if validation_error is None:
                return Result.ok(replace(result.value, content=extracted))

        return Result.err(
            ProviderError(
                message="JSON format required but GJC returned non-conforming output",
                provider=self._provider_name,
                details={"last_response_preview": last_response_preview},
            )
        )


__all__ = ["GjcLLMAdapter"]
