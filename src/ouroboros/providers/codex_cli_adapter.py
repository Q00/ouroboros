"""Codex CLI adapter for LLM completion using local Codex authentication.

This adapter shells out to `codex exec` in non-interactive mode, allowing
Ouroboros to use a local Codex CLI session for single-turn completion tasks
without requiring an API key.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from typing import Any

import structlog

from ouroboros.codex_permissions import (
    build_codex_exec_permission_args,
    resolve_codex_permission_mode,
)
from ouroboros.config import get_codex_cli_path
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
from ouroboros.providers.codex_cli_stream import (
    collect_stream_lines,
    iter_stream_lines,
    terminate_process,
)

log = structlog.get_logger()

_SAFE_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_./:@-]+$")

_RETRYABLE_ERROR_PATTERNS = (
    "rate limit",
    "temporarily unavailable",
    "timeout",
    "overloaded",
    "try again",
    "connection reset",
)


class CodexCliLLMAdapter:
    """LLM adapter backed by local Codex CLI execution."""

    _provider_name = "codex_cli"
    _display_name = "Codex CLI"
    _default_cli_name = "codex"
    _tempfile_prefix = "ouroboros-codex-llm-"
    _schema_tempfile_prefix = "ouroboros-codex-schema-"
    _process_shutdown_timeout_seconds = 5.0

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
        ephemeral: bool = True,
        timeout: float | None = None,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._permission_mode = self._resolve_permission_mode(permission_mode)
        self._allowed_tools = allowed_tools or []
        self._max_turns = max_turns
        self._on_message = on_message
        self._max_retries = max_retries
        self._ephemeral = ephemeral
        self._timeout = timeout if timeout and timeout > 0 else None

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the adapter permission mode."""
        return resolve_codex_permission_mode(permission_mode, default_mode="default")

    def _build_permission_args(self) -> list[str]:
        """Translate the configured permission mode into backend CLI flags."""
        return build_codex_exec_permission_args(
            self._permission_mode,
            default_mode="default",
        )

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available."""
        return get_codex_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve Codex CLI path from explicit path, config, or PATH."""
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = (
                self._get_configured_cli_path()
                or shutil.which(self._default_cli_name)
                or self._default_cli_name
            )

        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
        return candidate

    def _normalize_model(self, model: str) -> str | None:
        """Normalize a model name for Codex CLI.

        Raises:
            ValueError: If *model* contains characters outside the safe set.
        """
        candidate = model.strip()
        if not candidate or candidate == "default":
            return None
        if not _SAFE_MODEL_NAME_PATTERN.match(candidate):
            msg = f"Unsafe model name rejected: {candidate!r}"
            raise ValueError(msg)
        return candidate

    def _build_prompt(self, messages: list[Message]) -> str:
        """Build a plain-text prompt from conversation messages."""
        parts: list[str] = []

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

        if self._max_turns > 0:
            parts.append("## Execution Budget")
            parts.append(
                f"Keep the work within at most {self._max_turns} tool-assisted turns if possible."
            )

        for message in messages:
            if message.role == MessageRole.SYSTEM:
                continue
            role = "User" if message.role == MessageRole.USER else "Assistant"
            parts.append(f"{role}: {message.content}")

        parts.append("Please respond to the above conversation.")
        return "\n\n".join(part for part in parts if part.strip())

    def _build_output_schema(
        self,
        response_format: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Build a JSON Schema payload for `codex exec --output-schema`."""
        if not response_format:
            return None

        schema_type = response_format.get("type")
        if schema_type == "json_schema":
            schema = response_format.get("json_schema")
            return schema if isinstance(schema, dict) else None
        if schema_type == "json_object":
            return {"type": "object"}
        return None

    def _build_command(
        self,
        *,
        output_last_message_path: str,
        output_schema_path: str | None,
        model: str | None,
    ) -> list[str]:
        """Build the `codex exec` command for a one-shot completion.

        The prompt is always fed via stdin to avoid ARG_MAX limits.
        """
        command = [
            self._cli_path,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-C",
            self._cwd,
            "--output-last-message",
            output_last_message_path,
        ]

        command.extend(self._build_permission_args())

        if self._ephemeral:
            command.append("--ephemeral")

        if output_schema_path:
            command.extend(["--output-schema", output_schema_path])

        if model:
            command.extend(["--model", model])

        return command

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a JSONL event line, returning None for non-JSON output."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        return event if isinstance(event, dict) else None

    def _extract_text(self, value: object) -> str:
        """Extract text recursively from a nested JSON-like structure."""
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

            # Do not recurse into arbitrary dict values to prevent data leakage
            return ""

        return ""

    def _extract_session_id(self, stdout_lines: list[str]) -> str | None:
        """Extract a Codex thread id from JSONL stdout."""
        for line in stdout_lines:
            event = self._parse_json_event(line)
            if not event:
                continue
            if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
                return event["thread_id"]
        return None

    def _extract_session_id_from_event(self, event: dict[str, Any]) -> str | None:
        """Extract a Codex thread id from a single runtime event."""
        if event.get("type") == "thread.started" and isinstance(event.get("thread_id"), str):
            return event["thread_id"]
        return None

    def _extract_tool_input(self, item: dict[str, Any]) -> dict[str, Any]:
        """Extract tool input payload from a Codex event item."""
        for key in ("input", "arguments", "args"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                return candidate
        return {}

    def _extract_path(self, item: dict[str, Any]) -> str:
        """Extract a file path from a file change event."""
        candidates: list[object] = [
            item.get("path"),
            item.get("file_path"),
            item.get("target_file"),
        ]

        if isinstance(item.get("changes"), list):
            for change in item["changes"]:
                if isinstance(change, dict):
                    candidates.extend(
                        [
                            change.get("path"),
                            change.get("file_path"),
                        ]
                    )

        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    def _fallback_content(self, stdout_lines: list[str], stderr: str) -> str:
        """Build a fallback response from JSON events or stderr."""
        for line in reversed(stdout_lines):
            event = self._parse_json_event(line)
            if not event:
                continue
            item = event.get("item")
            if isinstance(item, dict):
                content = self._extract_text(item)
                if content:
                    return content

        return stderr.strip()

    def _format_tool_info(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Format tool name and input details for debug callbacks."""
        detail = ""
        if tool_name == "Bash":
            detail = str(tool_input.get("command", ""))
        elif tool_name in {"Edit", "Write", "Read"}:
            detail = str(tool_input.get("file_path", ""))
        elif tool_name in {"Glob", "Grep"}:
            detail = str(tool_input.get("pattern", ""))
        elif tool_name == "WebSearch":
            detail = str(tool_input.get("query", ""))
        elif tool_name.startswith("mcp__") or tool_name == "mcp_tool":
            detail = next((str(value) for value in tool_input.values() if value), "")

        if detail:
            detail = detail[:77] + "..." if len(detail) > 80 else detail
            return f"{tool_name}: {detail}"
        return tool_name

    def _emit_callback_for_event(self, event: dict[str, Any]) -> None:
        """Emit best-effort debug callbacks from Codex JSON events."""
        if self._on_message is None:
            return

        if event.get("type") != "item.completed":
            return

        item = event.get("item")
        if not isinstance(item, dict):
            return

        item_type = item.get("type")
        if not isinstance(item_type, str):
            return

        if item_type in {"agent_message", "reasoning", "todo_list"}:
            content = self._extract_text(item)
            if content:
                self._on_message("thinking", content)
            return

        if item_type == "command_execution":
            command = self._extract_text({"command": item.get("command")}) or ""
            tool_info = self._format_tool_info("Bash", {"command": command})
            self._on_message("tool", tool_info)
            return

        if item_type == "mcp_tool_call":
            tool_name = item.get("name") if isinstance(item.get("name"), str) else "mcp_tool"
            tool_info = self._format_tool_info(tool_name, self._extract_tool_input(item))
            self._on_message("tool", tool_info)
            return

        if item_type == "file_change":
            tool_info = self._format_tool_info("Edit", {"file_path": self._extract_path(item)})
            self._on_message("tool", tool_info)
            return

        if item_type == "web_search":
            tool_info = self._format_tool_info("WebSearch", {"query": self._extract_text(item)})
            self._on_message("tool", tool_info)

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
    ) -> AsyncIterator[str]:
        """Yield decoded lines without relying on StreamReader.readline()."""
        async for line in iter_stream_lines(stream, chunk_size=chunk_size):
            yield line

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
    ) -> list[str]:
        """Drain a subprocess stream without blocking stdout event parsing."""
        return await collect_stream_lines(stream)

    async def _terminate_process(self, process: Any) -> None:
        """Best-effort subprocess shutdown used for timeouts and cancellation."""
        await terminate_process(
            process,
            shutdown_timeout=self._process_shutdown_timeout_seconds,
        )

    def _read_output_message(self, output_path: Path) -> str:
        """Read the output-last-message file if the backend wrote one."""
        try:
            return output_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    @staticmethod
    def _truncate_if_oversized(content: str, model: str) -> str:
        """Validate and truncate oversized LLM responses."""
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
        """Check whether an error looks transient."""
        lowered = message.lower()
        return any(pattern in lowered for pattern in _RETRYABLE_ERROR_PATTERNS)

    async def _collect_legacy_process_output(
        self,
        process: Any,
    ) -> tuple[list[str], list[str], str | None, str]:
        """Fallback for tests or wrappers that only expose communicate()."""
        if self._timeout is not None:
            async with asyncio.timeout(self._timeout):
                stdout_bytes, stderr_bytes = await process.communicate()
        else:
            stdout_bytes, stderr_bytes = await process.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        stdout_lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        stderr_lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        session_id = self._extract_session_id(stdout_lines)
        last_content = ""

        for line in stdout_lines:
            event = self._parse_json_event(line)
            if event is None:
                continue
            self._emit_callback_for_event(event)
            event_content = self._extract_text(event.get("item") or event)
            if event_content:
                last_content = event_content

        return stdout_lines, stderr_lines, session_id, last_content

    async def _complete_once(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Execute a single Codex CLI completion request."""
        prompt = self._build_prompt(messages)
        normalized_model = self._normalize_model(config.model)
        output_fd, output_path_str = tempfile.mkstemp(prefix=self._tempfile_prefix, suffix=".txt")
        os.close(output_fd)
        output_path = Path(output_path_str)

        schema_path: Path | None = None
        schema = self._build_output_schema(config.response_format)
        if schema is not None:
            schema_fd, schema_path_str = tempfile.mkstemp(
                prefix=self._schema_tempfile_prefix,
                suffix=".json",
            )
            os.close(schema_fd)
            schema_path = Path(schema_path_str)
            schema_path.write_text(json.dumps(schema), encoding="utf-8")

        command = self._build_command(
            output_last_message_path=str(output_path),
            output_schema_path=str(schema_path) if schema_path else None,
            model=normalized_model,
        )

        prompt_bytes = prompt.encode("utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            output_path.unlink(missing_ok=True)
            if schema_path:
                schema_path.unlink(missing_ok=True)
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} not found: {exc}",
                    provider=self._provider_name,
                    details={"cli_path": self._cli_path},
                )
            )
        except Exception as exc:
            output_path.unlink(missing_ok=True)
            if schema_path:
                schema_path.unlink(missing_ok=True)
            return Result.err(
                ProviderError(
                    message=f"Failed to start {self._display_name}: {exc}",
                    provider=self._provider_name,
                    details={"cli_path": self._cli_path, "error_type": type(exc).__name__},
                )
            )

        # Feed prompt via stdin to avoid ARG_MAX limits
        if process.stdin is not None:
            process.stdin.write(prompt_bytes)
            await process.stdin.drain()
            process.stdin.close()

        if not hasattr(process, "stdout") or not callable(getattr(process, "wait", None)):
            (
                stdout_lines,
                stderr_lines,
                session_id,
                last_content,
            ) = await self._collect_legacy_process_output(process)
            content = self._read_output_message(output_path)
            output_path.unlink(missing_ok=True)
            if schema_path:
                schema_path.unlink(missing_ok=True)

            if not content:
                content = last_content or self._fallback_content(
                    stdout_lines,
                    "\n".join(stderr_lines),
                )

            if process.returncode != 0:
                return Result.err(
                    ProviderError(
                        message=content
                        or f"{self._display_name} exited with code {process.returncode}",
                        provider=self._provider_name,
                        details={
                            "returncode": process.returncode,
                            "session_id": session_id,
                            "stderr": "\n".join(stderr_lines).strip(),
                        },
                    )
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
                    },
                )
            )

        stdout_lines = []
        stderr_lines = []
        session_id = None
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
                event_content = self._extract_text(event.get("item") or event)
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
        except TimeoutError:
            await self._terminate_process(process)
            if not stdout_task.done():
                stdout_task.cancel()
            if not stderr_task.done():
                stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(
                    stdout_task,
                    timeout=self._process_shutdown_timeout_seconds,
                )
            with contextlib.suppress(asyncio.CancelledError, Exception):
                stderr_lines = await asyncio.wait_for(
                    stderr_task,
                    timeout=self._process_shutdown_timeout_seconds,
                )

            content = (
                self._read_output_message(output_path)
                or last_content
                or "\n".join(stderr_lines).strip()
            )
            output_path.unlink(missing_ok=True)
            if schema_path:
                schema_path.unlink(missing_ok=True)

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
            stdout_task.cancel()
            stderr_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stdout_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
            output_path.unlink(missing_ok=True)
            if schema_path:
                schema_path.unlink(missing_ok=True)
            raise

        content = self._read_output_message(output_path)
        output_path.unlink(missing_ok=True)
        if schema_path:
            schema_path.unlink(missing_ok=True)

        if not content:
            content = last_content or self._fallback_content(stdout_lines, "\n".join(stderr_lines))

        if process.returncode != 0:
            return Result.err(
                ProviderError(
                    message=content
                    or f"{self._display_name} exited with code {process.returncode}",
                    provider=self._provider_name,
                    details={
                        "returncode": process.returncode,
                        "session_id": session_id,
                        "stderr": "\n".join(stderr_lines).strip(),
                    },
                )
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
                },
            )
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request via Codex CLI with light retry logic."""
        last_error: ProviderError | None = None

        for attempt in range(self._max_retries):
            result = await self._complete_once(messages, config)
            if result.is_ok:
                return result

            last_error = result.error
            if bool(result.error.details.get("timed_out")):
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


__all__ = ["CodexCliLLMAdapter"]
