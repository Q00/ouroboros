"""Gemini CLI adapter for LLM completion using local Gemini CLI authentication.

This adapter shells out to ``gemini`` in non-interactive mode, allowing
Ouroboros to use a local Gemini CLI session for single-turn completion tasks
without requiring an API key.

Usage:
    adapter = GeminiCLIAdapter()
    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="Hello!")],
        config=CompletionConfig(model="gemini-2.5-pro"),
    )

Custom CLI Path:
    Set via constructor parameter or environment variable:
        adapter = GeminiCLIAdapter(cli_path="/path/to/gemini")
        # or
        export OUROBOROS_GEMINI_CLI_PATH=/path/to/gemini
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
import contextlib
import os
from pathlib import Path
import re
import shutil
from typing import Any

import structlog

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
    "resource exhausted",
)


class GeminiCLIAdapter:
    """LLM adapter backed by local Gemini CLI execution.

    This adapter invokes the ``gemini`` CLI in non-interactive mode to
    satisfy LLM completion requests without requiring a direct API key.
    Authentication is delegated to the user's locally configured Gemini CLI
    session (``GOOGLE_API_KEY`` or OAuth).

    Example:
        adapter = GeminiCLIAdapter()
        result = await adapter.complete(
            messages=[Message(role=MessageRole.USER, content="Hello!")],
            config=CompletionConfig(model="gemini-2.5-pro"),
        )
        if result.is_ok:
            print(result.value.content)

    Example with custom CLI path:
        adapter = GeminiCLIAdapter(cli_path="/usr/local/bin/gemini")
    """

    _provider_name = "gemini_cli"
    _display_name = "Gemini CLI"
    _default_cli_name = "gemini"
    _tempfile_prefix = "ouroboros-gemini-llm-"
    _process_shutdown_timeout_seconds = 5.0

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        cwd: str | Path | None = None,
        allowed_tools: list[str] | None = None,
        max_turns: int = 1,
        max_retries: int = 3,
        on_message: Callable[[str, str], None] | None = None,
        timeout: float | None = None,
    ) -> None:
        """Initialize Gemini CLI adapter.

        Args:
            cli_path: Path to the Gemini CLI binary. If not provided,
                checks OUROBOROS_GEMINI_CLI_PATH env var, then resolves
                from PATH, then falls back to ``"gemini"``.
            cwd: Working directory for CLI invocations.
            allowed_tools: Explicit allow-list for tools. ``None`` keeps
                the default permissive mode. Use ``[]`` to forbid all tools.
            max_turns: Maximum turns for the conversation. Default 1 for
                single-response completions.
            max_retries: Maximum number of retry attempts for transient errors.
            on_message: Optional callback for streaming partial messages.
                Called with (type, content) tuples:
                - ("thinking", "partial text") for streamed content
            timeout: Optional per-request timeout in seconds.
        """
        self._cli_path = self._resolve_cli_path(cli_path)
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._allowed_tools = list(allowed_tools) if allowed_tools is not None else None
        self._max_turns = max_turns
        self._max_retries = max_retries
        self._on_message = on_message
        self._timeout = timeout if timeout and timeout > 0 else None

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available.

        Calls the project-level ``get_gemini_cli_path`` helper, which checks
        the ``OUROBOROS_GEMINI_CLI_PATH`` environment variable and any
        applicable ``config.yaml`` entries.

        Returns:
            Configured path string if an explicit setting exists, or ``None``
            when no configuration is present and PATH-based resolution should
            be used instead.
        """
        from ouroboros.config import get_gemini_cli_path

        return get_gemini_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the Gemini CLI binary path with a defined priority order.

        Resolution order:
            1. Explicit ``cli_path`` constructor parameter.
            2. ``OUROBOROS_GEMINI_CLI_PATH`` env var / ``config.yaml`` entry
               (via :meth:`_get_configured_cli_path`).
            3. ``gemini`` found on ``PATH`` (via :func:`shutil.which`).
            4. Bare ``"gemini"`` string (relies on shell PATH at invocation time).

        If the resolved path points to an existing file it is returned as an
        absolute string; otherwise the raw candidate string is returned so
        that the OS can resolve it at subprocess creation time.

        Args:
            cli_path: Optional explicit path supplied by the caller.  May be
                a :class:`~pathlib.Path` object or a plain string; ``~`` home
                directory expansion is applied automatically.

        Returns:
            Resolved CLI path string, either absolute (when the file exists)
            or the best-effort candidate for subprocess resolution.
        """
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
        """Normalize a model name for Gemini CLI.

        Args:
            model: Raw model name from CompletionConfig.

        Returns:
            Normalized model string or None if using CLI default.

        Raises:
            ValueError: If model contains characters outside the safe set.
        """
        candidate = model.strip()
        if not candidate or candidate == "default":
            return None
        if not _SAFE_MODEL_NAME_PATTERN.match(candidate):
            msg = f"Unsafe model name rejected: {candidate!r}"
            raise ValueError(msg)
        return candidate

    def _build_prompt(self, messages: list[Message]) -> str:
        """Build a plain-text prompt from conversation messages.

        Args:
            messages: List of conversation messages.

        Returns:
            Formatted prompt string.
        """
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
        elif self._allowed_tools is not None:
            # Explicit empty list means no tools allowed — text-only response
            parts.append("## Tool Constraints")
            parts.append("Do NOT use any tools or MCP calls. Respond with plain text only.")

        for message in messages:
            if message.role == MessageRole.SYSTEM:
                continue
            role = "User" if message.role == MessageRole.USER else "Assistant"
            parts.append(f"{role}: {message.content}")

        parts.append("Please respond to the above conversation.")
        return "\n\n".join(part for part in parts if part.strip())

    def _build_command(self, *, model: str | None) -> list[str]:
        """Build the ``gemini`` command for a one-shot completion.

        The prompt is always fed via stdin to avoid ARG_MAX limits.

        Args:
            model: Optional model override.

        Returns:
            List of command-line arguments.
        """
        command = [self._cli_path]
        if model:
            command.extend(["--model", model])
        return command

    def _is_retryable_error(self, message: str, *, stderr: str = "") -> bool:
        """Check whether an error looks transient.

        Args:
            message: The primary error message.
            stderr: Optional stderr output to also check for retryable patterns.

        Returns:
            True if the error appears transient and worth retrying.
        """
        combined = (message + " " + stderr).lower()
        return any(pattern in combined for pattern in _RETRYABLE_ERROR_PATTERNS)

    @staticmethod
    def _truncate_if_oversized(content: str, model: str) -> str:
        """Validate response length and truncate if it exceeds the safety limit.

        Uses :data:`~ouroboros.core.security.MAX_LLM_RESPONSE_LENGTH` as the
        ceiling.  When the content is oversized a ``WARNING`` log event is
        emitted so operators can detect runaway responses.

        Args:
            content: The raw LLM response string to validate.
            model: Model identifier included in the warning log for context.

        Returns:
            The original ``content`` when within limits, otherwise the first
            ``MAX_LLM_RESPONSE_LENGTH`` characters of ``content``.
        """
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

    @staticmethod
    def _build_child_env() -> dict[str, str]:
        """Build an isolated environment for child Gemini CLI processes.

        Starts from a copy of the current process environment and applies two
        safety measures:

        1. **Strips Ouroboros orchestration variables** (``OUROBOROS_AGENT_RUNTIME``
           and ``OUROBOROS_LLM_BACKEND``) so the spawned CLI does not
           accidentally attach to the parent MCP session and cause recursive
           startup loops.
        2. **Increments ``_OUROBOROS_DEPTH``** so nested invocations can detect
           how deeply they are nested and bail out if a depth limit is reached.

        Returns:
            A fresh ``dict[str, str]`` environment mapping suitable for passing
            directly to :func:`asyncio.create_subprocess_exec` as the ``env``
            keyword argument.
        """
        env = os.environ.copy()
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1
        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
    ) -> AsyncIterator[str]:
        """Yield decoded UTF-8 lines from a subprocess stream.

        Delegates to :func:`~ouroboros.providers.codex_cli_stream.iter_stream_lines`
        rather than using :meth:`asyncio.StreamReader.readline` directly; the
        chunk-based approach avoids readline's tendency to buffer indefinitely
        when the CLI omits trailing newlines on its last line.

        Args:
            stream: The :class:`asyncio.StreamReader` attached to the child
                process stdout or stderr, or ``None`` (yields nothing).
            chunk_size: Number of bytes to read per chunk.  Larger values
                reduce syscall overhead; default ``16384`` is a good balance
                for typical LLM responses.

        Yields:
            Decoded string lines (including any trailing newline characters
            preserved from the raw stream output).
        """
        async for line in iter_stream_lines(stream, chunk_size=chunk_size):
            yield line

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
    ) -> list[str]:
        """Drain an entire subprocess stream into a list of non-empty lines.

        Typically used to collect stderr after the process exits so that error
        details are available for diagnostics without blocking stdout collection.

        Args:
            stream: The :class:`asyncio.StreamReader` to drain, or ``None``
                (returns an empty list).

        Returns:
            List of non-empty decoded string lines from the stream.  Blank
            lines are omitted to keep error detail output compact.
        """
        return await collect_stream_lines(stream)

    async def _terminate_process(self, process: Any) -> None:
        """Attempt a graceful then forceful shutdown of a subprocess.

        Delegates to :func:`~ouroboros.providers.codex_cli_stream.terminate_process`
        with the configured :attr:`_process_shutdown_timeout_seconds` grace
        period.  The call is best-effort: any exceptions raised during shutdown
        are swallowed by the helper so that callers can safely invoke this from
        timeout and cancellation handlers without masking the original exception.

        Args:
            process: The :class:`asyncio.subprocess.Process` instance to
                terminate.  Accepts ``Any`` so that test doubles can be passed
                without strict type checking at call sites.
        """
        await terminate_process(
            process,
            shutdown_timeout=self._process_shutdown_timeout_seconds,
        )

    async def _complete_once(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Execute a single Gemini CLI completion attempt without retry logic.

        Builds the prompt from messages, spawns the gemini subprocess,
        feeds the prompt via stdin, and drains stdout and stderr concurrently.
        Handles process timeout and asyncio cancellation by terminating the
        child process before propagating the exception.

        This method is the inner loop body called by :meth:`complete`; it
        should not be called directly in normal usage.

        Args:
            messages: The conversation messages to convert into a prompt.
            config: Completion configuration including the model name and any
                provider-specific options.

        Returns:
            :class:`~ouroboros.core.types.Result` wrapping a
            :class:`~ouroboros.providers.base.CompletionResponse` on success,
            or a :class:`~ouroboros.core.errors.ProviderError` describing the
            failure (non-zero exit code, empty output, timeout, spawn error,
            unsafe model name, etc.).
        """
        prompt = self._build_prompt(messages)
        normalized_model: str | None
        try:
            normalized_model = self._normalize_model(config.model)
        except ValueError as exc:
            return Result.err(
                ProviderError(
                    message=str(exc),
                    provider=self._provider_name,
                    details={"model": config.model},
                )
            )

        command = self._build_command(model=normalized_model)
        prompt_bytes = prompt.encode("utf-8")

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

        if process.stdin is not None:
            process.stdin.write(prompt_bytes)
            await process.stdin.drain()
            process.stdin.close()

        stdout_chunks: list[str] = []
        stderr_lines: list[str] = []
        stderr_task = asyncio.create_task(self._collect_stream_lines(process.stderr))

        async def _read_stdout() -> None:
            async for raw_line in self._iter_stream_lines(process.stdout):
                line = raw_line.rstrip()
                if line:
                    stdout_chunks.append(line)
                    if self._on_message:
                        self._on_message("thinking", line)

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
                await stdout_task
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
            partial = "\n".join(stdout_chunks)
            return Result.err(
                ProviderError(
                    message=f"{self._display_name} request timed out after {self._timeout:.1f}s",
                    provider=self._provider_name,
                    details={
                        "timed_out": True,
                        "timeout_seconds": self._timeout,
                        "partial_content": partial,
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
            raise

        content = "\n".join(stdout_chunks)

        if process.returncode != 0:
            stderr_text = "\n".join(stderr_lines).strip()
            return Result.err(
                ProviderError(
                    message=content or f"{self._display_name} exited with code {process.returncode}",
                    provider=self._provider_name,
                    details={
                        "returncode": process.returncode,
                        "stderr": stderr_text,
                    },
                )
            )

        if not content:
            return Result.err(
                ProviderError(
                    message=f"Empty response from {self._display_name}",
                    provider=self._provider_name,
                    details={"returncode": process.returncode},
                )
            )

        content = self._truncate_if_oversized(content, normalized_model or "default")

        return Result.ok(
            CompletionResponse(
                content=content,
                model=normalized_model or "default",
                usage=UsageInfo(prompt_tokens=0, completion_tokens=0, total_tokens=0),
                finish_reason="stop",
                raw_response={"returncode": process.returncode},
            )
        )

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a completion request via Gemini CLI with light retry logic.

        Args:
            messages: The conversation messages to send.
            config: Configuration for the completion request.

        Returns:
            Result containing either the completion response or a ProviderError.
        """
        last_error: ProviderError | None = None

        for attempt in range(self._max_retries):
            result = await self._complete_once(messages, config)
            if result.is_ok:
                return result

            last_error = result.error
            if bool(result.error.details.get("timed_out")):
                return result
            stderr_detail = str(result.error.details.get("stderr", ""))
            if (
                not self._is_retryable_error(result.error.message, stderr=stderr_detail)
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


__all__ = ["GeminiCLIAdapter"]
