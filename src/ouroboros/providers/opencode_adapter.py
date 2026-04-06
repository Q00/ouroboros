"""OpenCode CLI adapter for LLM completion using local OpenCode authentication.

This adapter shells out to ``opencode run --format json`` in non-interactive mode,
allowing Ouroboros to use a local OpenCode CLI session for single-turn completion
tasks without requiring a separate API key.

The OpenCode CLI must be installed and authenticated before use.

.. note::

    Each invocation creates a top-level OpenCode session.  A future
    phase will reparent these sessions under the caller's session to
    prevent polluting the session picker (see GitHub #164 Phase 2).

Usage::

    adapter = OpenCodeLLMAdapter()
    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="Hello!")],
        config=CompletionConfig(model="default"),
    )
    if result.is_ok:
        print(result.value.content)

Custom CLI path::

    adapter = OpenCodeLLMAdapter(cli_path="/usr/local/bin/opencode")
    # or
    # export OUROBOROS_OPENCODE_CLI_PATH=/usr/local/bin/opencode
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any

import structlog

from ouroboros.config import get_opencode_cli_path
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
_MAX_OUROBOROS_DEPTH = 5

_RETRYABLE_ERROR_PATTERNS = (
    "rate limit",
    "temporarily unavailable",
    "timeout",
    "overloaded",
    "try again",
    "connection reset",
)


class OpenCodeLLMAdapter:
    """LLM adapter backed by local OpenCode CLI execution.

    Implements the :class:`ouroboros.providers.base.LLMAdapter` protocol by
    spawning ``opencode run --format json`` as a subprocess and collecting
    its output.  This lets Ouroboros use OpenCode's existing provider
    configuration instead of embedding a separate API key.

    Attributes:
        _provider_name: Backend identifier used in
            :class:`~ouroboros.core.errors.ProviderError` reports.
        _display_name: Human-readable name for log messages.
        _default_cli_name: Binary name looked up on ``PATH`` when no
            explicit path is given.
        _tempfile_prefix: Prefix for any temporary files created during
            execution.
        _process_shutdown_timeout_seconds: Grace period before
            ``SIGKILL`` when terminating a child process.
        _cli_path: Resolved path to the ``opencode`` binary.
        _cwd: Working directory passed to the subprocess.
        _permission_mode: OpenCode permission mode string.
        _allowed_tools: Optional tool allow-list injected into prompts.
        _max_turns: Maximum conversation turns per invocation.
        _on_message: Optional callback invoked with ``(role, content)``
            for each assistant response.
        _max_retries: Number of retry attempts on transient failures.
        _timeout: Optional per-invocation timeout in seconds.
    """

    _provider_name = "opencode"
    _display_name = "OpenCode"
    _default_cli_name = "opencode"
    _tempfile_prefix = "ouroboros-opencode-llm-"
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
        timeout: float | None = None,
    ) -> None:
        """Initialise the OpenCode CLI adapter.

        Args:
            cli_path: Explicit path to ``opencode``.  Resolved from
                :func:`~ouroboros.config.get_opencode_cli_path`, then
                ``PATH`` if not supplied.
            cwd: Working directory for the subprocess.  Defaults to
                the current process working directory.
            permission_mode: OpenCode permission mode string.  Stored
                for forward compatibility but currently a **no-op**:
                the ``opencode run`` CLI has no ``--permission-mode``
                flag — it runs non-interactively by default.  Same
                limitation applies to the Gemini CLI adapter.
            allowed_tools: Optional tool allow-list injected into the
                composed prompt.
            max_turns: Maximum conversation turns per CLI invocation.
            on_message: Optional ``(role, content)`` callback fired
                for each assistant response.
            max_retries: Number of retry attempts on transient errors.
            timeout: Per-invocation timeout in seconds.  ``None`` or
                non-positive values disable the timeout.
        """
        self._cli_path = self._resolve_cli_path(cli_path)
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._permission_mode = permission_mode or "acceptEdits"
        self._allowed_tools = list(allowed_tools) if allowed_tools is not None else None
        self._max_turns = max_turns
        self._on_message = on_message
        self._max_retries = max_retries
        self._timeout = timeout if timeout and timeout > 0 else None

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the OpenCode CLI binary path.

        Checks, in order: *cli_path* argument,
        :func:`~ouroboros.config.get_opencode_cli_path`, ``PATH``
        lookup, and finally the bare binary name as a last resort.

        Args:
            cli_path: Explicit override path, or ``None`` to auto-resolve.

        Returns:
            Absolute or bare binary path string.
        """
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = (
                get_opencode_cli_path()
                or shutil.which(self._default_cli_name)
                or self._default_cli_name
            )

        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
        return candidate

    def _normalize_model(self, model: str) -> str | None:
        """Normalize a model name for the OpenCode CLI.

        Strips whitespace and rejects the sentinel ``"default"`` value.
        Model names are validated against a strict character allow-list.

        Args:
            model: Raw model name string from
                :class:`~ouroboros.providers.base.CompletionConfig`.

        Returns:
            Sanitised model string, or ``None`` when the default model
            should be used.

        Raises:
            ValueError: If *model* contains characters outside the
                safe set defined by ``_SAFE_MODEL_NAME_PATTERN``.
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

        System messages are grouped under a ``## System Instructions``
        header, tool constraints are appended when ``_allowed_tools``
        is set, and user/assistant turns appear under
        ``## Conversation``.

        Args:
            messages: Ordered list of
                :class:`~ouroboros.providers.base.Message` objects.

        Returns:
            Concatenated markdown-formatted prompt string.
        """
        parts: list[str] = []

        system_messages = [
            message.content for message in messages if message.role == MessageRole.SYSTEM
        ]
        if system_messages:
            parts.append("## System Instructions")
            parts.append("\n\n".join(system_messages))

        if self._allowed_tools is not None:
            if self._allowed_tools:
                parts.append("## Tool Constraints")
                parts.append(
                    "Limit your tool usage to ONLY the following tools:\n"
                    + "\n".join(f"- {tool}" for tool in self._allowed_tools)
                )
            else:
                parts.append("## Tool Constraints")
                parts.append("Do NOT use any tools. Respond with text only.")

        if self._max_turns == 1:
            parts.append("## Execution Constraints")
            parts.append("Respond in a single turn. Do not ask follow-up questions.")
        elif self._max_turns > 1:
            parts.append("## Execution Constraints")
            parts.append(f"Complete your response within {self._max_turns} turns maximum.")

        conversation_parts: list[str] = []
        for message in messages:
            if message.role == MessageRole.SYSTEM:
                continue
            prefix = "User" if message.role == MessageRole.USER else "Assistant"
            conversation_parts.append(f"### {prefix}\n{message.content}")

        if conversation_parts:
            parts.append("## Conversation")
            parts.extend(conversation_parts)

        return "\n\n".join(parts)

    def _build_command(
        self,
        prompt: str,
        model: str | None = None,
    ) -> list[str]:
        """Assemble the ``opencode run`` CLI invocation.

        The prompt is **not** included in argv — it is piped via stdin
        to avoid OS ``ARG_MAX`` / ``MAX_ARG_STRLEN`` limits (~128 KB per
        single argument on Linux).

        Args:
            prompt: Unused — kept for signature compatibility.
                Prompt is fed via stdin in :meth:`complete`.
            model: Optional model override appended via ``--model``.

        Returns:
            Argument list suitable for
            :func:`asyncio.create_subprocess_exec`.
        """
        command = [
            self._cli_path,
            "run",
            "--format",
            "json",
        ]

        if model:
            command.extend(["--model", model])

        return command

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for child runtime processes.

        Strips Ouroboros runtime env vars to prevent recursive MCP
        startup loops and increments ``_OUROBOROS_DEPTH`` to enforce
        a maximum nesting limit.

        Returns:
            Copy of ``os.environ`` with Ouroboros keys removed and
            depth counter incremented.

        Raises:
            RuntimeError: If the nesting depth exceeds
                ``_MAX_OUROBOROS_DEPTH``.
        """
        env = os.environ.copy()
        # Prevent child from re-entering Ouroboros MCP
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        # Track and enforce recursion depth
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1
        if depth > _MAX_OUROBOROS_DEPTH:
            msg = f"Maximum Ouroboros nesting depth ({_MAX_OUROBOROS_DEPTH}) exceeded"
            raise RuntimeError(msg)
        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

    def _is_retryable(self, error_message: str) -> bool:
        """Check if an error message suggests a retryable failure.

        Matches against known transient-error substrings such as
        ``"rate limit"`` and ``"timeout"``.

        Args:
            error_message: The error string to inspect.

        Returns:
            ``True`` if any retryable pattern is found in the
            lower-cased message.
        """
        lower = error_message.lower()
        return any(pattern in lower for pattern in _RETRYABLE_ERROR_PATTERNS)

    def _extract_text_from_events(self, events: list[dict[str, Any]]) -> str:
        """Extract assistant text content from OpenCode JSON events.

        Only collects ``text`` event payloads (the model's own words).
        Tool outputs are intentionally excluded to prevent raw ``bash``,
        ``read``, etc. results from being returned as the completion
        text.

        Args:
            events: List of parsed JSON event dicts from the CLI
                stdout stream.

        Returns:
            Joined assistant text, or an empty string if no text was
            found.
        """
        text_parts: list[str] = []
        for event in events:
            event_type = event.get("type")
            if event_type == "text":
                part = event.get("part", {})
                text = part.get("text", "")
                if isinstance(text, str) and text.strip():
                    text_parts.append(text.strip())
        return "\n".join(text_parts)

    def _extract_error_from_events(self, events: list[dict[str, Any]]) -> str | None:
        """Extract a terminal error message from OpenCode JSON events.

        Only top-level ``error``-type events are treated as terminal.
        Tool-level errors (``tool_use`` with ``state.error``) are
        **not** surfaced here because OpenCode agents can self-correct
        by retrying or switching tools mid-run — the process exit code
        is the authoritative success/failure signal.

        Args:
            events: List of parsed JSON event dicts from the CLI
                stdout stream.

        Returns:
            Error message string, or ``None`` if no terminal error.
        """
        for event in events:
            # Top-level error events are always terminal
            if event.get("type") == "error":
                error = event.get("error", {})
                if isinstance(error, dict):
                    name = error.get("name", "")
                    data = error.get("data", {})
                    msg = data.get("message", "") if isinstance(data, dict) else ""
                    return msg or name or "Unknown error"
                return str(error) if error else None

        return None

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a single-turn completion request via the OpenCode CLI.

        Shells out to ``opencode run --format json`` and collects the
        final text output.  Retries on transient failures up to
        ``_max_retries`` times with exponential back-off.

        Args:
            messages: Conversation messages to be composed into a
                prompt.
            config: Completion configuration carrying the model name
                and other tuning knobs.

        Returns:
            :class:`~ouroboros.core.types.Result` wrapping either a
            :class:`~ouroboros.providers.base.CompletionResponse` or
            a :class:`~ouroboros.core.errors.ProviderError`.
        """
        prompt = self._build_prompt(messages)
        if len(prompt) > MAX_LLM_RESPONSE_LENGTH:
            return Result.err(
                ProviderError(
                    message=f"Prompt exceeds maximum length ({MAX_LLM_RESPONSE_LENGTH} chars)",
                    provider=self._provider_name,
                )
            )

        model = self._normalize_model(config.model)
        last_error: str = ""

        for attempt in range(1, self._max_retries + 1):
            try:
                result = await self._run_once(prompt, model)
            except Exception as exc:
                last_error = str(exc)
                if attempt < self._max_retries and self._is_retryable(last_error):
                    log.warning(
                        "opencode_adapter.retry",
                        attempt=attempt,
                        error=last_error,
                    )
                    await asyncio.sleep(min(attempt * 2, 10))
                    continue
                return Result.err(
                    ProviderError(
                        message=f"OpenCode CLI failed: {last_error}",
                        provider=self._provider_name,
                        details={"attempt": attempt},
                    )
                )

            if result.is_ok:
                return result
            if attempt < self._max_retries and self._is_retryable(result.error.message):
                log.warning(
                    "opencode_adapter.retry",
                    attempt=attempt,
                    error=result.error.message,
                )
                await asyncio.sleep(min(attempt * 2, 10))
                continue
            return result

        return Result.err(
            ProviderError(
                message=f"OpenCode CLI failed after {self._max_retries} attempts: {last_error}",
                provider=self._provider_name,
            )
        )

    async def _run_once(
        self,
        prompt: str,
        model: str | None,
    ) -> Result[CompletionResponse, ProviderError]:
        """Execute a single OpenCode CLI invocation.

        Spawns the subprocess, streams JSON events from stdout,
        collects stderr, and converts the output into a
        :class:`~ouroboros.providers.base.CompletionResponse`.

        Args:
            prompt: Fully composed prompt string.
            model: Normalised model name, or ``None`` for the
                default.

        Returns:
            :class:`~ouroboros.core.types.Result` wrapping either a
            :class:`~ouroboros.providers.base.CompletionResponse` or
            a :class:`~ouroboros.core.errors.ProviderError`.
        """
        command = self._build_command(prompt, model)

        try:
            env = self._build_child_env()
        except RuntimeError as exc:
            return Result.err(
                ProviderError(
                    message=str(exc),
                    provider=self._provider_name,
                )
            )

        log.debug(
            "opencode_adapter.exec",
            command=command[:3],
            cwd=self._cwd,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return Result.err(
                ProviderError(
                    message=(
                        f"OpenCode CLI not found at {self._cli_path}. "
                        "Install with: npm i -g opencode-ai@latest"
                    ),
                    provider=self._provider_name,
                )
            )

        # Feed prompt via stdin to avoid OS ARG_MAX / MAX_ARG_STRLEN limits.
        if prompt and process.stdin is not None:
            try:
                process.stdin.write(prompt.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                log.warning("opencode_adapter.stdin_write_failed", error=str(exc))
                with contextlib.suppress(OSError):
                    process.stdin.close()

        async def _collect() -> tuple[list[dict[str, Any]], int, str]:
            """Collect events, wait for exit, read stderr."""
            evts: list[dict[str, Any]] = []
            if process.stdout is not None:
                async for line in iter_stream_lines(process.stdout):
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict):
                            evts.append(event)
                    except json.JSONDecodeError:
                        continue

            rc = await process.wait()
            stderr = ""
            if process.stderr is not None:
                stderr_lines = await collect_stream_lines(process.stderr)
                stderr = "\n".join(stderr_lines)
            return evts, rc, stderr

        try:
            events, returncode, stderr_text = await asyncio.wait_for(
                _collect(),
                timeout=self._timeout,
            )
        except TimeoutError:
            await terminate_process(
                process,
                shutdown_timeout=self._process_shutdown_timeout_seconds,
            )
            return Result.err(
                ProviderError(
                    message=(f"OpenCode CLI timed out after {self._timeout}s"),
                    provider=self._provider_name,
                )
            )
        except asyncio.CancelledError:
            await terminate_process(
                process,
                shutdown_timeout=self._process_shutdown_timeout_seconds,
            )
            raise
        except Exception as exc:
            await terminate_process(
                process,
                shutdown_timeout=self._process_shutdown_timeout_seconds,
            )
            return Result.err(
                ProviderError(
                    message=f"OpenCode CLI execution error: {exc}",
                    provider=self._provider_name,
                )
            )

        # Check for errors in the event stream
        error_msg = self._extract_error_from_events(events)
        if error_msg:
            return Result.err(
                ProviderError(
                    message=f"OpenCode error: {error_msg}",
                    provider=self._provider_name,
                    details={"returncode": returncode},
                )
            )

        if returncode != 0:
            return Result.err(
                ProviderError(
                    message=(
                        f"OpenCode CLI exited with code {returncode}"
                        + (f": {stderr_text}" if stderr_text else "")
                    ),
                    provider=self._provider_name,
                    details={"returncode": returncode},
                )
            )

        content = self._extract_text_from_events(events)
        if not content:
            return Result.err(
                ProviderError(
                    message=f"Empty response from {self._display_name}",
                    provider=self._provider_name,
                    details={
                        "returncode": returncode,
                        "stderr": stderr_text.strip(),
                    },
                )
            )

        # Validate response length
        is_valid, _ = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "opencode_adapter.response.truncated",
                length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            content = content[:MAX_LLM_RESPONSE_LENGTH]

        if self._on_message:
            self._on_message("assistant", content)

        return Result.ok(
            CompletionResponse(
                content=content,
                model=model or "opencode-default",
                usage=UsageInfo(
                    prompt_tokens=0,
                    completion_tokens=0,
                    total_tokens=0,
                ),
                finish_reason="stop",
            )
        )


__all__ = ["OpenCodeLLMAdapter"]
