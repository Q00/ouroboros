"""GitHub Copilot CLI adapter for LLM completion via local Copilot authentication.

This adapter shells out to ``copilot -p "<prompt>"`` in non-interactive mode,
allowing Ouroboros to use a local Copilot CLI session for single-turn
completion tasks without requiring a direct OpenAI/Anthropic API key. Auth
flows through ``GH_TOKEN`` / ``GITHUB_TOKEN`` (per Copilot CLI documentation).

The adapter mirrors :mod:`ouroboros.providers.codex_cli_adapter`: shared
recursion guard via ``_OUROBOROS_DEPTH``, stripped child env, sandbox-class
flag mapping, JSONL stream parsing, retry on transient errors, and graceful
subprocess teardown. Differences from Codex:

* No ``--output-schema`` flag; ``response_format`` falls back to a prompt
  directive plus post-hoc JSON extraction (same workaround as Gemini).
* No ``--output-last-message`` file; the assistant reply is reconstructed
  from the JSONL event stream.
* The hard tool envelope is expressed via ``--available-tools`` (allowlist)
  plus ``--allow-tool`` (skip prompts) plus ``--add-dir`` (cwd allowlist).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import json
import os
from pathlib import Path
import re
from typing import Any

import structlog

from ouroboros.config import get_copilot_cli_path
from ouroboros.copilot.cli_policy import (
    DEFAULT_COPILOT_CHILD_SESSION_ENV_KEYS,
    DEFAULT_MAX_OUROBOROS_DEPTH,
    build_copilot_child_env,
    resolve_copilot_cli_path,
)
from ouroboros.copilot.model_discovery import map_to_copilot_model
from ouroboros.copilot.runtime_profile import resolve_copilot_agent
from ouroboros.copilot_permissions import (
    build_copilot_exec_permission_args,
    resolve_copilot_permission_mode,
)
from ouroboros.core.errors import ProviderError
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.core.types import Result
from ouroboros.providers.base import (
    CompletionConfig,
    CompletionResponse,
    Message,
    MessageRole,
    UsageInfo,
)
from ouroboros.providers.copilot_cli_stream import (
    collect_stream_lines,
    iter_stream_lines,
    terminate_process,
)
from ouroboros.providers.profiles import resolve_completion_profile_result

log = structlog.get_logger()

_SAFE_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_./:@-]+$")

_RETRYABLE_ERROR_PATTERNS = (
    "rate limit",
    "temporarily unavailable",
    "timeout",
    "overloaded",
    "try again",
    "connection reset",
    "quota exceeded",
    "github api error",
)

_AUTH_ERROR_PATTERNS = (
    "401 unauthorized",
    "403 forbidden",
    "gh_token",
    "github_token",
    "missing token",
    "not authenticated",
    "authentication required",
)

_JSON_OBJECT_DIRECTIVE = (
    "Respond with a single JSON object. Do not include prose, "
    "Markdown fences, or commentary outside the JSON value."
)

_JSON_SCHEMA_DIRECTIVE_TEMPLATE = (
    "Respond with a single JSON value that strictly conforms to the "
    "following JSON Schema. Do not include prose, Markdown fences, or "
    "commentary outside the JSON value.\n\nSchema:\n{schema}"
)


class CopilotCliLLMAdapter:
    """LLM adapter backed by local GitHub Copilot CLI execution."""

    _provider_name = "copilot_cli"
    _display_name = "GitHub Copilot CLI"
    _default_cli_name = "copilot"
    _tempfile_prefix = "ouroboros-copilot-llm-"
    _process_shutdown_timeout_seconds = 5.0
    _log_namespace = "copilot_cli_adapter"
    _max_ouroboros_depth = DEFAULT_MAX_OUROBOROS_DEPTH
    _child_session_env_keys = DEFAULT_COPILOT_CHILD_SESSION_ENV_KEYS

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 1,
        on_message: Callable[[str, str], None] | None = None,
        max_retries: int = 3,
        timeout: float | None = None,
        runtime_profile: str | None = None,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._permission_mode = self._resolve_permission_mode(permission_mode)
        self._allowed_tools = list(allowed_tools) if allowed_tools is not None else None
        self._max_turns = max_turns
        self._on_message = on_message
        self._max_retries = max_retries
        self._timeout = timeout if timeout and timeout > 0 else None
        self._runtime_profile = runtime_profile
        self._copilot_agent = resolve_copilot_agent(
            runtime_profile,
            logger=log,
            log_namespace=self._log_namespace,
        )

    # ------------------------------------------------------------------ setup

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        return resolve_copilot_permission_mode(permission_mode, default_mode="default")

    def _build_permission_args(self) -> list[str]:
        return build_copilot_exec_permission_args(
            self._permission_mode,
            default_mode="default",
        )

    def _get_configured_cli_path(self) -> str | None:
        return get_copilot_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        resolution = resolve_copilot_cli_path(
            explicit_cli_path=cli_path,
            configured_cli_path=self._get_configured_cli_path(),
            default_cli_name=self._default_cli_name,
            logger=log,
            log_namespace=self._log_namespace,
        )
        return resolution.cli_path

    def _normalize_model(self, model: str) -> str | None:
        candidate = model.strip()
        if not candidate or candidate == "default":
            return None
        if not _SAFE_MODEL_NAME_PATTERN.match(candidate):
            msg = f"Unsafe model name rejected: {candidate!r}"
            raise ValueError(msg)
        return candidate

    # ----------------------------------------------------------- prompt build

    def _build_prompt(
        self,
        messages: list[Message],
        *,
        max_turns: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> str:
        """Build a plain-text prompt from conversation messages.

        Copilot CLI accepts a single text blob via ``-p``; we serialise the
        conversation into a labelled transcript with optional system, tool, and
        execution-budget sections. Mirrors the Codex prompt builder.
        """
        parts: list[str] = []
        effective_max_turns = max_turns if max_turns is not None else self._max_turns

        system_messages = [
            message.content for message in messages if message.role == MessageRole.SYSTEM
        ]
        if system_messages:
            parts.append("## System Instructions")
            parts.append("\n\n".join(system_messages))

        if self._allowed_tools:
            parts.append("## Tool Constraints")
            parts.append(
                "If you need tools, prefer using only the following tools:\n"
                + "\n".join(f"- {tool}" for tool in self._allowed_tools)
            )
        elif self._allowed_tools is not None:
            parts.append("## Tool Constraints")
            parts.append("Do NOT use any tools or MCP calls. Respond with plain text only.")

        if effective_max_turns > 0:
            parts.append("## Execution Budget")
            if self._allowed_tools == []:
                parts.append(
                    "Answer directly in plain text and avoid turning this into a "
                    "multi-step tool workflow."
                )
            else:
                parts.append(
                    f"Keep the work within at most {effective_max_turns} tool-assisted turns if possible."
                )

        directive = self._build_response_format_directive(response_format)
        if directive:
            parts.append("## Response Format")
            parts.append(directive)

        for message in messages:
            if message.role == MessageRole.SYSTEM:
                continue
            role = "User" if message.role == MessageRole.USER else "Assistant"
            parts.append(f"{role}: {message.content}")

        parts.append("Please respond to the above conversation.")
        return "\n\n".join(part for part in parts if part.strip())

    @staticmethod
    def _build_response_format_directive(
        response_format: dict[str, object] | None,
    ) -> str | None:
        """Translate an OpenAI-style ``response_format`` into a prompt directive.

        Copilot CLI has no native JSON-schema flag, so structured output is
        enforced by instructing the model to emit a single JSON value. Callers
        are expected to validate/extract JSON from the response themselves;
        the adapter still hands back ``content`` exactly as Copilot wrote it.
        """
        if not response_format:
            return None
        schema_type = response_format.get("type")
        if schema_type == "json_object":
            return _JSON_OBJECT_DIRECTIVE
        if schema_type == "json_schema":
            schema = response_format.get("json_schema")
            if isinstance(schema, dict):
                payload = schema.get("schema") if "schema" in schema else schema
                try:
                    rendered = json.dumps(payload, indent=2, sort_keys=True)
                except (TypeError, ValueError):
                    rendered = str(payload)
                return _JSON_SCHEMA_DIRECTIVE_TEMPLATE.format(schema=rendered)
        return None

    # ----------------------------------------------------------- command build

    def _build_command(
        self,
        *,
        model: str | None,
        agent: str | None = None,
    ) -> list[str]:
        """Build the ``copilot -p`` command for a one-shot completion."""
        command = [self._cli_path, "--no-color", "--log-level", "none"]

        # Bound filesystem access to the cwd Ouroboros passed in. This is the
        # sandbox-write boundary; combined with the permission-flag mapping
        # it gives us "workspace-write" semantics analogous to Codex.
        command.extend(["--add-dir", self._cwd])

        # Hard tool envelope. ``--available-tools`` is the only flag with
        # allowlist semantics — anything outside it is invisible to the model.
        # ``--allow-tool`` is added in parallel so the allowed tools also skip
        # confirmation prompts (required for non-interactive ``-p`` mode).
        if self._allowed_tools:
            tool_list = ",".join(self._allowed_tools)
            command.extend([f"--available-tools={tool_list}"])
            command.extend([f"--allow-tool={tool_list}"])

        command.extend(self._build_permission_args())

        # Agent profile (Copilot's ``--agent``) takes precedence over per-call
        # ``--model`` selection, mirroring the Codex ``--profile`` precedence.
        if self._copilot_agent:
            command.extend(["--agent", self._copilot_agent])
            if agent and agent != self._copilot_agent:
                log.warning(
                    f"{self._log_namespace}.profile_override_ignored",
                    requested=agent,
                    active=self._copilot_agent,
                )
        elif agent:
            command.extend(["--agent", agent])
        elif model:
            mapped_model = map_to_copilot_model(model)
            if mapped_model != model:
                log.debug(
                    f"{self._log_namespace}.model_mapped",
                    requested=model,
                    resolved=mapped_model,
                )
            command.extend(["--model", mapped_model])

        return command

    # --------------------------------------------------------- event handling

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    def _extract_text(self, value: object) -> str:
        if isinstance(value, str):
            return value.strip()

        if isinstance(value, list):
            parts = [self._extract_text(item) for item in value]
            return "\n".join(part for part in parts if part)

        if isinstance(value, dict):
            preferred_keys = (
                "text",
                "message",
                "output_text",
                "content",
                "summary",
                "details",
                "command",
            )
            dict_parts: list[str] = []
            for key in preferred_keys:
                if key in value:
                    text = self._extract_text(value[key])
                    if text:
                        dict_parts.append(text)
            if dict_parts:
                return "\n".join(dict_parts)

            shallow_parts = [v.strip() for v in value.values() if isinstance(v, str) and v.strip()]
            return "\n".join(shallow_parts)

        return ""

    def _extract_session_id_from_event(self, event: dict[str, Any]) -> str | None:
        for key in ("session_id", "sessionId", "thread_id", "threadId"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _emit_callback_for_event(self, event: dict[str, Any]) -> None:
        if self._on_message is None:
            return

        event_type = event.get("type") or ""
        if not isinstance(event_type, str):
            return

        if event_type in {"reasoning", "thinking", "agent.message", "message"}:
            content = self._extract_text(event)
            if content:
                self._on_message("thinking", content)
            return

        if event_type in {"tool_use", "tool.start", "tool_call"}:
            tool_name = event.get("name") or event.get("tool") or "tool"
            self._on_message("tool", str(tool_name))

    def _extract_stdout_errors(self, stdout_lines: list[str]) -> list[str]:
        """Pull error event messages from a Copilot JSONL stdout stream."""
        errors: list[str] = []
        for line in stdout_lines:
            event = self._parse_json_event(line)
            if not event:
                continue
            event_type = event.get("type")
            if event_type not in {"error", "turn.failed", "fatal"}:
                continue
            payload = event.get("error") if event_type == "turn.failed" else event
            if isinstance(payload, dict):
                msg = payload.get("message")
                if isinstance(msg, str) and msg.strip():
                    errors.append(msg.strip())
                    continue
            if isinstance(payload, str) and payload.strip():
                errors.append(payload.strip())
        return errors

    def _fallback_content(self, stdout_lines: list[str], stderr: str) -> str:
        """Build a fallback response from JSON events or stderr."""
        for line in reversed(stdout_lines):
            event = self._parse_json_event(line)
            if not event:
                continue
            content = self._extract_text(event)
            if content:
                return content
        # No JSON events — Copilot may have streamed plain text. Use the
        # concatenation of all non-empty stdout lines as the response.
        text = "\n".join(line for line in stdout_lines if line.strip())
        return text or stderr.strip()

    # ------------------------------------------------------ stream/process io

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
    ) -> AsyncIterator[str]:
        async for line in iter_stream_lines(stream, chunk_size=chunk_size):
            yield line

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
    ) -> list[str]:
        return await collect_stream_lines(stream)

    async def _terminate_process(self, process: Any) -> None:
        await terminate_process(
            process,
            shutdown_timeout=self._process_shutdown_timeout_seconds,
        )

    @staticmethod
    def _truncate_if_oversized(content: str, model: str) -> str:
        is_valid, _ = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "llm.response.truncated",
                model=model,
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            return content[:MAX_LLM_RESPONSE_LENGTH]
        return content

    def _is_retryable_error(self, message: str) -> bool:
        lowered = message.lower()
        return any(pattern in lowered for pattern in _RETRYABLE_ERROR_PATTERNS)

    @staticmethod
    def _looks_like_auth_error(text: str) -> bool:
        lowered = text.lower()
        return any(pattern in lowered for pattern in _AUTH_ERROR_PATTERNS)

    @classmethod
    def _build_child_env(cls) -> dict[str, str]:
        """Build an isolated environment for child Copilot processes."""
        return build_copilot_child_env(
            max_depth=cls._max_ouroboros_depth,
            child_session_env_keys=cls._child_session_env_keys,
            depth_error_factory=lambda depth, max_depth: ProviderError(
                message=f"Maximum Ouroboros nesting depth ({max_depth}) exceeded",
                provider=cls._provider_name,
                details={"depth": depth},
            ),
        )

    async def _collect_legacy_process_output(
        self,
        process: Any,
    ) -> tuple[list[str], list[str], str | None, str]:
        """Fallback path for tests/wrappers that only expose ``communicate()``."""
        if self._timeout is not None:
            async with asyncio.timeout(self._timeout):
                stdout_bytes, stderr_bytes = await process.communicate()
        else:
            stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        stdout_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        session_id: str | None = None
        last_content = ""

        for line in stdout_lines:
            event = self._parse_json_event(line)
            if event is None:
                continue
            extracted = self._extract_session_id_from_event(event)
            if extracted:
                session_id = extracted
            self._emit_callback_for_event(event)
            event_content = self._extract_text(event)
            if event_content:
                last_content = event_content

        return stdout_lines, stderr_lines, session_id, last_content

    # ----------------------------------------------------------- main complete

    async def _complete_once(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Execute a single Copilot CLI completion request."""
        profile_result = resolve_completion_profile_result(config, backend="copilot")
        if profile_result.is_err:
            return Result.err(profile_result.error)
        resolved = profile_result.value
        effective_config = resolved.config
        prompt = self._build_prompt(
            messages,
            max_turns=effective_config.max_turns,
            response_format=config.response_format,
        )
        normalized_model = (
            None
            if resolved.backend_profile and not self._copilot_agent
            else self._normalize_model(effective_config.model)
        )

        command = self._build_command(
            model=normalized_model,
            agent=resolved.backend_profile,
        )
        # Append the prompt last so any future flag additions before it don't
        # accidentally swallow the prompt as a flag value.
        command.extend(["-p", prompt])

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
        except ProviderError as exc:
            return Result.err(exc)
        except Exception as exc:
            return Result.err(
                ProviderError(
                    message=f"Failed to start {self._display_name}: {exc}",
                    provider=self._provider_name,
                    details={"cli_path": self._cli_path, "error_type": type(exc).__name__},
                )
            )

        # Close stdin immediately — the prompt was passed via ``-p`` so the
        # child has nothing to read. Leaving stdin open can keep some Copilot
        # builds waiting for terminal input.
        if process.stdin is not None:
            with contextlib.suppress(Exception):
                process.stdin.close()

        if not hasattr(process, "stdout") or not callable(getattr(process, "wait", None)):
            return await self._handle_legacy_process(
                process,
                normalized_model=normalized_model,
            )

        return await self._handle_streaming_process(
            process,
            normalized_model=normalized_model,
        )

    async def _handle_legacy_process(
        self,
        process: Any,
        *,
        normalized_model: str | None,
    ) -> Result[CompletionResponse, ProviderError]:
        (
            stdout_lines,
            stderr_lines,
            session_id,
            last_content,
        ) = await self._collect_legacy_process_output(process)

        content = last_content or self._fallback_content(stdout_lines, "\n".join(stderr_lines))

        if process.returncode != 0:
            return self._error_from_process(
                process=process,
                content=content,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                session_id=session_id,
            )

        if not content:
            return Result.err(
                ProviderError(
                    message=f"Empty response from {self._display_name}",
                    provider=self._provider_name,
                    details={"session_id": session_id},
                )
            )

        content = self._truncate_if_oversized(content, normalized_model or "default")
        return Result.ok(
            CompletionResponse(
                content=content,
                model=normalized_model or "default",
                usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                finish_reason="stop",
                raw_response={
                    "session_id": session_id,
                    "returncode": process.returncode,
                    "usage_estimated": True,
                },
            )
        )

    async def _handle_streaming_process(
        self,
        process: Any,
        *,
        normalized_model: str | None,
    ) -> Result[CompletionResponse, ProviderError]:
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        session_id: str | None = None
        last_content = ""
        stderr_task = asyncio.create_task(self._collect_stream_lines(process.stderr))

        async def _read_stdout() -> None:
            nonlocal session_id, last_content
            async for raw_line in self._iter_stream_lines(process.stdout):
                line = raw_line.strip()
                if not line:
                    continue

                stdout_lines.append(line)
                event = self._parse_json_event(line)
                if event is None:
                    continue

                event_session_id = self._extract_session_id_from_event(event)
                if event_session_id:
                    session_id = event_session_id

                self._emit_callback_for_event(event)
                event_content = self._extract_text(event)
                if event_content:
                    last_content = event_content

        stdout_task = asyncio.create_task(_read_stdout())

        try:
            if self._timeout is None:
                await process.wait()
            else:
                async with asyncio.timeout(self._timeout):
                    await process.wait()
            await stdout_task
            stderr_lines = await stderr_task
        except ProviderError as exc:
            await self._terminate_process(process)
            await self._cancel_tasks(stdout_task, stderr_task)
            return Result.err(
                ProviderError(
                    message=exc.message,
                    provider=self._provider_name,
                    details={
                        **exc.details,
                        "session_id": session_id,
                        "returncode": getattr(process, "returncode", None),
                    },
                )
            )
        except TimeoutError:
            await self._terminate_process(process)
            await self._cancel_tasks(stdout_task, stderr_task)
            with contextlib.suppress(Exception):
                stderr_lines = await stderr_task
            content = last_content or "\n".join(stderr_lines).strip()
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} request timed out after {self._timeout:.1f}s",
                    provider=self._provider_name,
                    details={
                        "timed_out": True,
                        "timeout_seconds": self._timeout,
                        "session_id": session_id,
                        "partial_content": content,
                        "returncode": getattr(process, "returncode", None),
                        "stderr": "\n".join(stderr_lines).strip(),
                    },
                )
            )
        except asyncio.CancelledError:
            await self._terminate_process(process)
            await self._cancel_tasks(stdout_task, stderr_task)
            raise

        content = last_content or self._fallback_content(stdout_lines, "\n".join(stderr_lines))

        if process.returncode != 0:
            return self._error_from_process(
                process=process,
                content=content,
                stdout_lines=stdout_lines,
                stderr_lines=stderr_lines,
                session_id=session_id,
            )

        if not content:
            return Result.err(
                ProviderError(
                    message=f"Empty response from {self._display_name}",
                    provider=self._provider_name,
                    details={"session_id": session_id},
                )
            )

        content = self._truncate_if_oversized(content, normalized_model or "default")
        return Result.ok(
            CompletionResponse(
                content=content,
                model=normalized_model or "default",
                usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                finish_reason="stop",
                raw_response={
                    "session_id": session_id,
                    "returncode": process.returncode,
                    "usage_estimated": True,
                },
            )
        )

    @staticmethod
    async def _cancel_tasks(*tasks: asyncio.Task[Any]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    def _error_from_process(
        self,
        *,
        process: Any,
        content: str,
        stdout_lines: list[str],
        stderr_lines: list[str],
        session_id: str | None,
    ) -> Result[CompletionResponse, ProviderError]:
        stdout_errors = self._extract_stdout_errors(stdout_lines)
        stderr_text = "\n".join(stderr_lines).strip()
        message = (
            (stdout_errors[-1] if stdout_errors else None)
            or content
            or stderr_text
            or f"{self._display_name} exited with code {process.returncode}"
        )

        details: dict[str, object] = {
            "returncode": process.returncode,
            "session_id": session_id,
            "stderr": stderr_text,
            "stdout_errors": stdout_errors,
        }

        if self._looks_like_auth_error(message) or self._looks_like_auth_error(stderr_text):
            details["auth_error"] = True
            message = (
                f"{self._display_name} authentication failed. Set GH_TOKEN or "
                f"GITHUB_TOKEN to a token with the 'Copilot Requests' "
                f"permission. Original error: {message}"
            )

        return Result.err(
            ProviderError(
                message=message,
                provider=self._provider_name,
                details=details,
            )
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request via Copilot CLI with light retry logic."""
        last_error: ProviderError | None = None

        for attempt in range(self._max_retries):
            result = await self._complete_once(messages, config)
            if result.is_ok:
                return result

            last_error = result.error
            if bool(result.error.details.get("timed_out")):
                return result
            if bool(result.error.details.get("auth_error")):
                # Auth errors are not retried — fail fast so the user sees the
                # GH_TOKEN/GITHUB_TOKEN hint without waiting for backoff.
                return result
            if (
                not self._is_retryable_error(result.error.message)
                or attempt >= self._max_retries - 1
            ):
                return result

            await asyncio.sleep(2**attempt)

        return Result.err(
            last_error
            or ProviderError(
                f"{self._display_name} request failed",
                provider=self._provider_name,
            )
        )


__all__ = ["CopilotCliLLMAdapter"]
