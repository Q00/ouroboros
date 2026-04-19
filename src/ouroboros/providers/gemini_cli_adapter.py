"""Gemini CLI adapter for LLM completion using local Gemini authentication.

This adapter shells out to ``gemini -p`` in non-interactive (headless) mode,
allowing Ouroboros to use a local Gemini CLI session for single-turn completion
tasks without requiring a separate API key.

The Gemini CLI must be installed (``npm install -g @google/gemini-cli``) and
authenticated (``gemini auth``) before use.

Usage::

    adapter = GeminiCLIAdapter()
    result = await adapter.complete(
        messages=[Message(role=MessageRole.USER, content="Hello!")],
        config=CompletionConfig(model="gemini-2.5-flash"),
    )
    if result.is_ok:
        print(result.value.content)

Custom CLI path::

    adapter = GeminiCLIAdapter(cli_path="/usr/local/bin/gemini")
    # or
    # export OUROBOROS_GEMINI_CLI_PATH=/usr/local/bin/gemini
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
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
from ouroboros.providers.codex_cli_stream import iter_stream_lines, terminate_process

log = structlog.get_logger()

_SAFE_MODEL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_./:@-]+$")

_RETRYABLE_ERROR_PATTERNS = (
    "rate limit",
    "temporarily unavailable",
    "timeout",
    "overloaded",
    "try again",
    "quota",
    "resource exhausted",
)

# Gemini CLI exit codes
_EXIT_AUTH_ERROR = 41
_EXIT_INPUT_ERROR = 42
_EXIT_CONFIG_ERROR = 52
_EXIT_TURN_LIMIT = 53

# Default model when none is specified
_DEFAULT_MODEL = "gemini-2.5-flash"

# Maximum response size to guard against runaway output
_MAX_RESPONSE_BYTES = MAX_LLM_RESPONSE_LENGTH

# Guard against recursive ouroboros invocations
_MAX_OUROBOROS_DEPTH = 5


class GeminiCLIAdapter:
    """LLM adapter backed by local Gemini CLI execution.

    Implements the :class:`ouroboros.providers.base.LLMAdapter` protocol by
    spawning ``gemini -p`` as a subprocess and parsing its JSON output.  This
    lets you use the Gemini CLI's existing OAuth session instead of embedding
    an API key.

    Attributes:
        cli_path: Path to the ``gemini`` binary.  Resolved from constructor,
            ``OUROBOROS_GEMINI_CLI_PATH`` environment variable or ``PATH``.
        model: Gemini model name (e.g. ``gemini-2.5-flash``).
        cwd: Working directory for the subprocess.
        timeout: Maximum seconds to wait for a response.  ``None`` means no
            limit.
    """

    _provider_name = "gemini_cli"
    _display_name = "Gemini CLI"
    _default_cli_name = "gemini"
    _process_shutdown_timeout_seconds = 5.0

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        max_turns: int = 1,
        timeout: float | None = 120.0,
        max_retries: int = 3,
        on_message: Callable[[str, str], None] | None = None,
        allowed_tools: list[str] | None = None,
    ) -> None:
        """Initialise the Gemini CLI adapter.

        Args:
            cli_path: Explicit path to the ``gemini`` binary.  Falls back to
                ``OUROBOROS_GEMINI_CLI_PATH`` env var, then ``PATH``.
            model: Gemini model identifier.  Defaults to ``gemini-2.5-flash``.
            cwd: Working directory for the subprocess.
            max_turns: Maximum conversational turns (kept for API symmetry; the
                Gemini CLI non-interactive mode always runs a single turn).
            timeout: Subprocess timeout in seconds.
            max_retries: Maximum number of retries on transient errors.
            on_message: Optional callback invoked with ``(type, content)`` for
                streaming events — ``"thinking"`` for text fragments, ``"tool"``
                for tool-use events.
            allowed_tools: Engine-derived tool envelope.  The Gemini CLI does
                not expose an ``--allowed-tools`` flag, so enforcement here is
                *soft*: the envelope is injected into the system prompt as a
                hard instruction and out-of-envelope ``tool_use`` events in
                the stream are reported via
                ``gemini_cli_adapter.tool_envelope_violation``.  The soft
                nature is recorded up-front via
                ``gemini_cli_adapter.soft_tool_enforcement`` so operators can
                tell Gemini sessions apart from the hard-enforced
                Claude/Codex/OpenCode ones at audit time.
        """
        self._cli_path: Path = self._resolve_cli_path(cli_path)
        self._model: str = model or _DEFAULT_MODEL
        self._cwd: Path | None = Path(cwd).resolve() if cwd else None
        self._timeout: float | None = timeout
        self._max_retries: int = max_retries
        self._on_message: Callable[[str, str], None] | None = on_message
        self._allowed_tools: tuple[str, ...] | None = (
            tuple(allowed_tools) if allowed_tools is not None else None
        )

        log.info(
            "gemini_cli_adapter.initialized",
            cli_path=str(self._cli_path),
            model=self._model,
        )
        if self._allowed_tools is not None:
            log.warning(
                "gemini_cli_adapter.soft_tool_enforcement",
                allowed_tools=list(self._allowed_tools),
                reason=(
                    "Gemini CLI has no native allowed_tools flag; the "
                    "envelope is injected as a system-prompt instruction "
                    "and violations are detected post-hoc in the tool_use "
                    "stream.  Enforcement is cooperative, not mandatory."
                ),
            )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[Message],
        config: CompletionConfig,
    ) -> Result[CompletionResponse, ProviderError]:
        """Make a single-turn completion request via the Gemini CLI.

        Retries on transient errors (rate limits, timeouts) with exponential
        back-off.

        Args:
            messages: Conversation messages.  System messages are prepended to
                the user prompt; the final user message is the primary prompt.
            config: Completion configuration (model, temperature, etc.).

        Returns:
            :class:`~ouroboros.core.types.Result` wrapping either a
            :class:`~ouroboros.providers.base.CompletionResponse` or a
            :class:`~ouroboros.core.errors.ProviderError`.
        """
        prompt = self._build_prompt(messages)
        model = self._resolve_model(config.model)

        last_error: ProviderError | None = None
        backoff = 2.0

        for attempt in range(self._max_retries):
            result = await self._execute_request(prompt, model, config)

            if result.is_ok:
                if attempt > 0:
                    log.info("gemini_cli_adapter.retry_succeeded", attempts=attempt + 1)
                return result

            error_msg = result.error.message
            if self._is_retryable(error_msg) and attempt < self._max_retries - 1:
                log.warning(
                    "gemini_cli_adapter.retryable_error",
                    error=error_msg,
                    attempt=attempt + 1,
                    backoff_seconds=backoff,
                )
                last_error = result.error
                await asyncio.sleep(backoff)
                backoff *= 2
                continue

            return result

        log.error("gemini_cli_adapter.max_retries_exceeded", max_retries=self._max_retries)
        return Result.err(last_error or ProviderError(message="Max retries exceeded"))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_request(
        self,
        prompt: str,
        model: str,
        config: CompletionConfig | None = None,
    ) -> Result[CompletionResponse, ProviderError]:
        """Spawn ``gemini -p`` and parse the stream-json output.

        Uses ``--output-format stream-json`` so that intermediate tool-use
        events can be surfaced via the ``on_message`` callback.

        Args:
            prompt: The fully-formatted prompt string.
            model: The Gemini model identifier.
            config: Optional completion configuration for generation controls.

        Returns:
            A :class:`~ouroboros.core.types.Result` with the completion or
            an error.
        """
        cmd = self._build_command(prompt, model, config)
        log.debug("gemini_cli_adapter.spawning", cmd=cmd)

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._cwd) if self._cwd else None,
                env=self._build_env(),
            )
        except FileNotFoundError:
            return Result.err(
                ProviderError(
                    message=(
                        f"Gemini CLI not found at '{self._cli_path}'. "
                        "Install it with: npm install -g @google/gemini-cli"
                    ),
                    provider=self._provider_name,
                )
            )
        except Exception as exc:
            return Result.err(
                ProviderError(
                    message=f"Failed to spawn Gemini CLI: {exc}",
                    provider=self._provider_name,
                )
            )

        try:
            return await asyncio.wait_for(
                self._collect_response(process),
                timeout=self._timeout,
            )
        except TimeoutError:
            await terminate_process(
                process,
                shutdown_timeout=self._process_shutdown_timeout_seconds,
            )
            return Result.err(
                ProviderError(
                    message=f"Gemini CLI timed out after {self._timeout}s",
                    provider=self._provider_name,
                    details={"timeout_seconds": self._timeout},
                )
            )

    async def _collect_response(
        self,
        process: asyncio.subprocess.Process,
    ) -> Result[CompletionResponse, ProviderError]:
        """Read stream-json lines from the subprocess and build a response.

        The ``stream-json`` format emits one JSON object per line.  Known
        event types:

        - ``init``: session metadata (session_id, model)
        - ``message``: a conversational turn (role, content)
        - ``tool_use``: a tool invocation request
        - ``tool_result``: the result of a tool invocation
        - ``error``: a non-fatal error or warning
        - ``result``: final aggregated stats and full response text

        Args:
            process: Running Gemini CLI subprocess.

        Returns:
            A :class:`~ouroboros.core.types.Result` with the parsed response.
        """
        content_parts: list[str] = []
        session_id: str | None = None
        model_name: str | None = None
        final_response: str | None = None
        error_seen: str | None = None
        total_bytes = 0

        async for line in iter_stream_lines(process.stdout):
            line = line.strip()
            if not line:
                continue

            total_bytes += len(line)
            if total_bytes > _MAX_RESPONSE_BYTES:
                await terminate_process(
                    process, shutdown_timeout=self._process_shutdown_timeout_seconds
                )
                return Result.err(
                    ProviderError(
                        message=f"Gemini CLI response exceeded {_MAX_RESPONSE_BYTES} bytes",
                        provider=self._provider_name,
                    )
                )

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                # Plain-text line — accumulate it (fallback for non-JSON output)
                content_parts.append(line)
                continue

            event_type = event.get("type", "")

            # Handle batch JSON format (--output-format json): a single
            # object with "response", "stats" and "error" fields, no "type".
            if not event_type and "response" in event:
                final_response = event.get("response") or None
                if event.get("error"):
                    err = event["error"]
                    error_seen = err.get("message", str(err))
                continue

            if event_type == "init":
                session_id = event.get("session_id")
                model_name = event.get("model")
                log.debug(
                    "gemini_cli_adapter.session_init",
                    session_id=session_id,
                    model=model_name,
                )

            elif event_type == "message":
                role = event.get("role", "")
                text = event.get("content", "")
                if role == "model" and text:
                    content_parts.append(text)
                    if self._on_message and text.strip():
                        self._on_message("thinking", text.strip())

            elif event_type == "tool_use":
                tool_name = event.get("name", "unknown")
                tool_input = event.get("input", {})
                detail = self._format_tool_detail(tool_name, tool_input)
                log.debug("gemini_cli_adapter.tool_use", tool=tool_name)
                # Post-hoc soft enforcement: if an envelope was declared and
                # Gemini invoked a tool outside it, record the violation as
                # a structured warning so operators can audit drift.  The
                # CLI has already issued the tool call by the time we see
                # the event, so this is detection rather than prevention —
                # that is the documented trade-off of Gemini's soft mode.
                if (
                    self._allowed_tools is not None
                    and tool_name not in self._allowed_tools
                    and tool_name != "unknown"
                ):
                    log.warning(
                        "gemini_cli_adapter.tool_envelope_violation",
                        tool=tool_name,
                        allowed_tools=list(self._allowed_tools),
                    )
                if self._on_message:
                    self._on_message("tool", detail)

            elif event_type == "error":
                error_msg = event.get("message", "")
                if error_msg:
                    log.warning("gemini_cli_adapter.stream_error", error=error_msg)
                    error_seen = error_msg

            elif event_type == "result":
                # ``result`` carries the full aggregated response — prefer this
                # over the piecemeal content_parts accumulation.
                final_response = event.get("response") or None
                if event.get("error"):
                    err = event["error"]
                    error_seen = err.get("message", str(err))

        returncode = await process.wait()

        # A non-zero exit code always means failure.
        if returncode != 0:
            stderr_lines: list[str] = []
            async for line in iter_stream_lines(process.stderr):
                if line.strip():
                    stderr_lines.append(line.strip())
            stderr_text = "\n".join(stderr_lines[:10])
            # Include any stream-level error text so callers can detect
            # retryable conditions (e.g. "rate limit exceeded") even when
            # the process exits with a non-zero code.
            detail = error_seen or stderr_text
            return Result.err(
                ProviderError(
                    message=(
                        f"Gemini CLI exited with code {returncode}"
                        + (f": {detail}" if detail else "")
                    ),
                    provider=self._provider_name,
                    details={"returncode": returncode, "stderr": stderr_text},
                )
            )

        # Prefer the aggregated result event; fall back to accumulated parts.
        content = final_response if final_response is not None else "\n".join(content_parts)

        # Only treat stream-level errors as fatal when there is no usable
        # content.  Non-fatal warnings (e.g. deprecation notices) may appear
        # alongside a valid response.
        if error_seen and not content:
            return Result.err(
                ProviderError(
                    message=f"Gemini CLI reported an error: {error_seen}",
                    provider=self._provider_name,
                    details={"session_id": session_id},
                )
            )

        if not content:
            return Result.err(
                ProviderError(
                    message="Gemini CLI produced an empty response",
                    provider=self._provider_name,
                    details={"session_id": session_id},
                )
            )

        # Validate and truncate oversized responses
        is_valid, _ = InputValidator.validate_llm_response(content)
        if not is_valid:
            log.warning(
                "llm.response.truncated",
                model=model_name or self._model,
                original_length=len(content),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            content = content[:MAX_LLM_RESPONSE_LENGTH]

        log.info(
            "gemini_cli_adapter.request_completed",
            content_length=len(content),
            session_id=session_id,
        )

        return Result.ok(
            CompletionResponse(
                content=content,
                model=model_name or self._model,
                usage=UsageInfo(
                    prompt_tokens=0,  # Gemini CLI does not expose token counts
                    completion_tokens=0,
                    total_tokens=0,
                ),
                finish_reason="stop",
                raw_response={"session_id": session_id},
            )
        )

    def _build_command(
        self,
        prompt: str,
        model: str,
        config: CompletionConfig | None = None,
    ) -> list[str]:
        """Build the ``gemini`` subprocess command list.

        Forwards generation controls (``temperature``, ``max_tokens``, etc.)
        from *config* when the Gemini CLI supports them.  Controls that the
        CLI does not accept are logged and silently dropped so callers get a
        clear signal rather than a silent degradation.

        Args:
            prompt: The formatted prompt text.
            model: The Gemini model identifier.
            config: Optional completion configuration.

        Returns:
            Argument list suitable for :func:`asyncio.create_subprocess_exec`.
        """
        output_format = "stream-json"

        # Use plain JSON when a structured schema is requested — the Gemini
        # CLI's ``--output-format json`` returns a single object which is
        # easier to parse for schema-constrained responses.
        if config and config.response_format:
            fmt_type = config.response_format.get("type", "")
            if fmt_type in ("json_object", "json_schema"):
                output_format = "json"
                log.info(
                    "gemini_cli_adapter.structured_output_requested",
                    format_type=fmt_type,
                    note="Gemini CLI does not support server-side JSON schema "
                    "enforcement; output may not conform to the requested schema",
                )

        cmd: list[str] = [
            str(self._cli_path),
            "--output-format",
            output_format,
            "--model",
            model,
            "-p",
            prompt,
        ]

        # Forward generation controls that the Gemini CLI accepts.
        # Unsupported controls are logged so callers can diagnose parity gaps.
        if config:
            _UNSUPPORTED: list[str] = []

            if config.temperature != 0.7:  # non-default
                # Gemini CLI does not expose --temperature yet
                _UNSUPPORTED.append(f"temperature={config.temperature}")

            if config.max_tokens != 4096:  # non-default
                _UNSUPPORTED.append(f"max_tokens={config.max_tokens}")

            if config.top_p != 1.0:  # non-default
                _UNSUPPORTED.append(f"top_p={config.top_p}")

            if config.stop:
                _UNSUPPORTED.append(f"stop={config.stop}")

            if _UNSUPPORTED:
                log.warning(
                    "gemini_cli_adapter.unsupported_generation_controls",
                    controls=_UNSUPPORTED,
                    hint="Gemini CLI does not yet expose these flags; "
                    "the request will use model defaults",
                )

        return cmd

    def _build_env(self) -> dict[str, str]:
        """Build the environment for the subprocess.

        Returns a copy of the current environment with recursion-depth
        tracking.  Raises :class:`ProviderError` if the maximum nesting
        depth is exceeded.

        Returns:
            Environment dictionary for the subprocess.

        Raises:
            ProviderError: If ``_OUROBOROS_DEPTH`` exceeds the limit.
        """
        env = os.environ.copy()
        # Strip vars that could trigger recursive ouroboros startup
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1
        if depth > _MAX_OUROBOROS_DEPTH:
            raise ProviderError(
                message=f"Maximum Ouroboros nesting depth ({_MAX_OUROBOROS_DEPTH}) exceeded",
                provider=self._provider_name,
                details={"depth": depth},
            )
        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

    def _resolve_model(self, config_model: str) -> str:
        """Select the effective Gemini model name.

        Strips any ``gemini/`` provider prefix that callers may include.

        Args:
            config_model: Model string from :class:`CompletionConfig`.

        Returns:
            Bare Gemini model identifier.
        """
        if not config_model or config_model == "default":
            return self._model
        # Strip optional provider prefix
        for prefix in ("gemini/", "google/"):
            if config_model.startswith(prefix):
                config_model = config_model[len(prefix) :]
        if not _SAFE_MODEL_NAME_PATTERN.match(config_model):
            log.warning(
                "gemini_cli_adapter.unsafe_model_name",
                model=config_model,
                fallback=self._model,
            )
            return self._model
        return config_model

    def _resolve_cli_path(self, cli_path: str | Path | None) -> Path:
        """Resolve the path to the ``gemini`` binary.

        Priority:
        1. Explicit *cli_path* constructor argument
        2. ``OUROBOROS_GEMINI_CLI_PATH`` environment variable
        3. ``gemini`` resolved via ``PATH``

        Args:
            cli_path: Explicit CLI path from the constructor (may be ``None``).

        Returns:
            Resolved :class:`~pathlib.Path` to the binary.

        Raises:
            :exc:`ProviderError` is **not** raised here — a bad path is caught
            later in :meth:`_execute_request` when the subprocess fails to
            start.
        """
        if cli_path:
            return Path(cli_path).expanduser().resolve()

        env_path = os.environ.get("OUROBOROS_GEMINI_CLI_PATH", "").strip()
        if env_path:
            return Path(env_path).expanduser().resolve()

        found = shutil.which(self._default_cli_name)
        if found:
            return Path(found)

        # Return a non-existent path — error surfaces on first use.
        return Path(self._default_cli_name)

    @staticmethod
    def _is_retryable(error_msg: str) -> bool:
        """Return ``True`` if the error is likely transient.

        Args:
            error_msg: Error message string.

        Returns:
            Whether the error is worth retrying.
        """
        lower = error_msg.lower()
        return any(pattern in lower for pattern in _RETRYABLE_ERROR_PATTERNS)

    @staticmethod
    def _format_tool_detail(tool_name: str, tool_input: dict[str, Any]) -> str:
        """Format a tool-use event for the ``on_message`` callback.

        Args:
            tool_name: Name of the tool.
            tool_input: Tool input parameters.

        Returns:
            Human-readable string like ``"read_file: /path/to/file"``.
        """
        # Try common input keys for a useful detail snippet
        for key in ("path", "file_path", "command", "pattern", "query", "url"):
            value = tool_input.get(key)
            if value:
                detail = str(value)
                if len(detail) > 60:
                    detail = detail[:57] + "..."
                return f"{tool_name}: {detail}"
        return tool_name

    def _build_prompt(self, messages: list[Message]) -> str:
        """Combine conversation messages into a single prompt string.

        System messages are rendered first as an authoritative instruction
        block.  Subsequent user/assistant turns are formatted as a dialogue.
        The final user message acts as the primary request.

        When ``allowed_tools`` is set on the adapter, an engine-owned
        ``<tool_envelope>`` block is prepended to the system instructions.
        The Gemini CLI has no native allow-listing surface so this
        injection is the mechanism by which the engine's policy envelope
        travels into the model's context.  Violations (Gemini invoking a
        tool outside the envelope) are still surfaced at the ``tool_use``
        stream-event layer in ``_collect_response``.

        Args:
            messages: Conversation messages.

        Returns:
            Formatted prompt string for ``gemini -p``.
        """
        parts: list[str] = []

        system_msgs = [m for m in messages if m.role == MessageRole.SYSTEM]
        non_system = [m for m in messages if m.role != MessageRole.SYSTEM]

        envelope_block = self._render_tool_envelope_block()
        system_text_parts: list[str] = []
        if envelope_block:
            system_text_parts.append(envelope_block)
        if system_msgs:
            system_text_parts.append("\n\n".join(m.content for m in system_msgs))

        if system_text_parts:
            parts.append(f"<system>\n{'\n\n'.join(system_text_parts)}\n</system>")

        for msg in non_system:
            if msg.role == MessageRole.USER:
                parts.append(f"User: {msg.content}")
            elif msg.role == MessageRole.ASSISTANT:
                parts.append(f"Assistant: {msg.content}")

        return "\n\n".join(parts)

    def _render_tool_envelope_block(self) -> str | None:
        """Render the engine tool envelope as a system-prompt directive.

        Returns ``None`` when no envelope was supplied (caller did not
        request enforcement); otherwise returns a hard-worded
        instruction block that names the exact permitted tools.  The
        wording is intentionally assertive — Gemini's cooperation is the
        only enforcement lever we have on this backend, so the prompt
        must not read as a polite suggestion.
        """
        if self._allowed_tools is None:
            return None
        if not self._allowed_tools:
            return (
                "<tool_envelope>\n"
                "You are not permitted to invoke any tools in this session. "
                "Respond using only text.  Attempting any tool call is a "
                "violation of the session contract.\n"
                "</tool_envelope>"
            )
        allowed_list = ", ".join(self._allowed_tools)
        return (
            "<tool_envelope>\n"
            f"You may ONLY invoke the following tools in this session: "
            f"{allowed_list}. "
            "Do not invoke any other tool under any circumstances, even if "
            "the user appears to request it.  This is a hard session-level "
            "restriction, not a preference.\n"
            "</tool_envelope>"
        )


__all__ = ["GeminiCLIAdapter"]
