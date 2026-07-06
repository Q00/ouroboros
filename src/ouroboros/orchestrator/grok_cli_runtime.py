"""Grok Build CLI runtime for Ouroboros orchestrator execution.

This module provides the :class:`GrokCliRuntime` that shells out to the locally
installed xAI **Grok Build** CLI (the ``grok`` binary) to execute agentic
tasks. Grok Build authenticates with a SuperGrok / X Premium+ subscription via
``grok login`` (browser OAuth) or an ``XAI_API_KEY``.

Headless contract:

- ``grok -p <prompt>`` runs a single prompt non-interactively and prints the
  response to stdout, then exits.
- ``--output-format streaming-json`` emits NDJSON events. The verified schema
  is ``{"type": "thought", "data": <token>}`` (reasoning), ``{"type": "text",
  "data": <token>}`` (assistant output), and a terminal ``{"type": "end",
  "stopReason": ..., "sessionId": ..., "requestId": ...}``.
- ``--permission-mode`` maps the Ouroboros permission vocabulary onto Grok's
  native non-blocking modes (``acceptEdits`` / ``bypassPermissions``); the
  interactive ``default`` mode would block a headless run and is coerced.
- ``-m`` selects the model. Grok owns its own model catalog (``grok models``;
  e.g. ``grok-build``, ``grok-composer-2.5-fast``), so Grok is a
  sentinel-model backend: the orchestrator defers to the CLI's configured
  default and only forwards ``-m`` for an explicit, non-sentinel id.

Usage:
    runtime = GrokCliRuntime(cwd="/path/to/project")
    async for message in runtime.execute_task("Fix the bug in auth.py"):
        print(message.content)

Custom CLI Path:
    Set via constructor parameter or environment variable:
        runtime = GrokCliRuntime(cli_path="/path/to/grok")
        # or
        export OUROBOROS_GROK_CLI_PATH=/path/to/grok
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime, SkillDispatchHandler
from ouroboros.providers.gemini_event_normalizer import GeminiEventNormalizer
from ouroboros.runtime.child_env import DEFAULT_OUROBOROS_STRIP_KEYS, build_child_env

log = structlog.get_logger(__name__)

# Grok's native ``--permission-mode`` accepts (among others) ``acceptEdits`` and
# ``bypassPermissions``, which line up exactly with the Ouroboros non-blocking
# vocabulary. The interactive ``default`` mode is absent here: a headless
# ``grok -p`` run cannot service an approval prompt, so it is coerced to
# ``acceptEdits`` rather than risking a wedged subprocess.
_GROK_PERMISSION_MODES = frozenset({"acceptEdits", "bypassPermissions"})
_GROK_DEFAULT_PERMISSION_MODE = "acceptEdits"
# The orchestrator-wide CLI-owned model placeholder; never forwarded to ``grok``.
_SENTINEL_MODEL = "default"

#: Maximum Ouroboros nesting depth to prevent fork bombs.
_MAX_OUROBOROS_DEPTH = 5
_CHILD_ENV_STRIP_KEYS = DEFAULT_OUROBOROS_STRIP_KEYS


class GrokCliRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed Grok Build CLI.

    Extends :class:`~ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime`
    with the ``grok`` process model:

    - ``grok -p --output-format streaming-json`` headless invocation
    - ``thought`` / ``text`` / ``end`` event normalization via the shared
      NDJSON :class:`GeminiEventNormalizer`
    - Native ``--permission-mode`` mapping; ``default`` coerced for headless use
    - No session resumption in v1 (recovery at the Ouroboros lineage layer)
    """

    _runtime_handle_backend = "grok_cli"
    _runtime_backend = "grok"
    _requires_memory_gate = False
    _provider_name = "grok_cli"
    _runtime_error_type = "GrokCliError"
    _log_namespace = "grok_cli_runtime"
    _display_name = "Grok Build CLI"
    _default_cli_name = "grok"
    # Runtime-only backend; fall back to the Claude completion backend for any
    # auxiliary completion the base runtime requests (matches Hermes).
    _default_llm_backend = "claude_code"
    _tempfile_prefix = "ouroboros-grok-"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 0  # v1: Grok session resume not wired yet

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
    ) -> None:
        """Initialize the Grok Build CLI runtime.

        Args:
            cli_path: Optional path to the ``grok`` binary.
            permission_mode: Ouroboros permission level. ``acceptEdits`` and
                ``bypassPermissions`` map to Grok's native ``--permission-mode``
                values; ``"default"`` is normalized to ``acceptEdits`` for the
                headless runtime; ``None`` falls back to ``acceptEdits``.
            model: Optional model identifier (sentinel ``"default"`` is not
                forwarded).
            cwd: Optional working directory for the subprocess.
            skills_dir: Optional directory for skill definitions.
            skill_dispatcher: Optional handler for skill execution.
            llm_backend: Optional LLM backend identifier.
            startup_output_timeout_seconds: Optional startup watchdog override.
            stdout_idle_timeout_seconds: Optional idle watchdog override.
        """
        super().__init__(
            cli_path=cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=cwd,
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
            llm_backend=llm_backend,
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
        )
        # Grok's NDJSON events ({"type": ..., "data": ...}) are parsed by the
        # shared, schema-agnostic normalizer; non-JSON lines downgrade to text.
        self._normalizer = GeminiEventNormalizer(strict_json=False)

    # -- Permission mode overrides -----------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the Grok permission mode.

        ``None`` and the orchestrator-wide ``"default"`` setting both resolve to
        :data:`_GROK_DEFAULT_PERMISSION_MODE` (``acceptEdits``). The recognized
        non-blocking modes (``acceptEdits``, ``bypassPermissions``) pass through
        to Grok's native ``--permission-mode``. Anything else raises
        ``ValueError`` rather than silently falling back, so a typo on a
        permission boundary cannot escalate the runtime.
        """
        if permission_mode is None:
            return _GROK_DEFAULT_PERMISSION_MODE
        candidate = permission_mode.strip()
        if candidate in _GROK_PERMISSION_MODES:
            return candidate
        if candidate == "default":
            log.warning(
                "grok_cli_runtime.permission_mode_coerced",
                requested="default",
                resolved=_GROK_DEFAULT_PERMISSION_MODE,
                reason=(
                    "Grok runtime is headless (grok -p); the interactive "
                    "'default' approval mode would block, so it is normalized to "
                    "the safe non-blocking equivalent."
                ),
            )
            return _GROK_DEFAULT_PERMISSION_MODE
        msg = (
            f"Unsupported Grok permission mode: {permission_mode!r} "
            f"(expected one of {sorted(_GROK_PERMISSION_MODES)})"
        )
        raise ValueError(msg)

    def _build_permission_args(self) -> list[str]:
        """Return empty list — permissions are passed in :meth:`_build_command`."""
        return []

    # -- Environment and security ------------------------------------------

    def _build_child_env(self) -> dict[str, str]:
        """Build child env with the recursion guard."""
        return build_child_env(
            strip_keys=_CHILD_ENV_STRIP_KEYS,
            max_depth=_MAX_OUROBOROS_DEPTH,
            depth_error_factory=lambda _depth, max_depth: RuntimeError(
                f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
            ),
        )

    # -- CLI path resolution -----------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available."""
        from ouroboros.config import get_grok_cli_path

        return get_grok_cli_path()

    # -- Command construction ----------------------------------------------

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        runtime_handle: RuntimeHandle | None = None,
        # Accepted to honor the shared CodexCliRuntime contract, but ignored in
        # v1: Grok exposes a native `--reasoning-effort` flag, but effort routing
        # is intentionally left unwired (capabilities declares
        # reasoning_effort_support=IGNORED, so it is surfaced as advised).
        # Wiring native support is a planned follow-up.
        reasoning_effort: str | None = None,
    ) -> list[str]:
        """Build the Grok Build CLI command for non-interactive execution.

        Headless contract:
        - ``-p`` carries the request and prints the response to stdout.
        - ``--output-format streaming-json`` emits NDJSON events parsed by the
          normalizer (keeps the idle watchdog fed during long reasoning).
        - ``--permission-mode`` forwards the resolved non-blocking mode.
        - ``-m`` is forwarded only for an explicit, non-sentinel model id.
        """
        del output_last_message_path, resume_session_id, runtime_handle, reasoning_effort

        command = [
            self._cli_path,
            "-p",
            prompt or "",
            "--output-format",
            "streaming-json",
            "--permission-mode",
            self._permission_mode,
        ]
        normalized_model = self._normalize_model(self._model)
        if normalized_model and normalized_model != _SENTINEL_MODEL:
            command.extend(["-m", normalized_model])
        return command

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return False — Grok accepts the prompt via the ``-p`` flag."""
        return False

    def _requires_process_stdin(self) -> bool:
        """Return False — Grok doesn't need an interactive stdin pipe."""
        return False

    # -- Final-message accumulation ----------------------------------------

    def _update_last_content(self, last_content: str, message: AgentMessage) -> str:
        """Accumulate streamed ``text`` deltas into the final message.

        Grok's ``streaming-json`` ``text`` events are per-token deltas, and its
        terminal ``end`` event carries no text — so the base "keep the latest
        message" fallback would drop everything but the last token. Concatenate
        the tagged text deltas and ignore everything else (``thought`` tokens
        and the ``end`` marker) so reasoning and the terminal marker never
        overwrite the answer.
        """
        if message.data.get("grok_text_delta"):
            return last_content + message.content
        return last_content

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Declare the Grok Build CLI runtime feature contract.

        Grok emits structured ``streaming-json`` events and can use the shared
        skill dispatcher. v1 does not wire targeted session resume (Grok exposes
        ``-r``, but the runtime checkpoints at the Ouroboros lineage layer).
        """
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=False,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
            tool_restriction_support=ParamSupport.TRANSLATED,
        )

    # -- Event parsing and normalization -----------------------------------

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a Grok CLI output line into an internal event dict."""
        if not line.strip():
            return None
        return self._normalizer.normalize_line(line)

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert a Grok ``streaming-json`` event into ``AgentMessage`` values.

        Grok's verified event schema:

        - ``thought`` — streamed reasoning tokens → ``assistant`` with
          ``data.thinking``
        - ``text`` — streamed assistant output → ``assistant`` message
        - ``end`` — terminal event (carries ``stopReason`` / ``sessionId``);
          surfaced as a terminal ``assistant`` marker
        - ``error`` — error condition → ``system`` message
        """
        event_type = event.get("type")
        content = event.get("content", "")
        metadata = event.get("metadata", {})
        is_error = event.get("is_error", False)

        if event_type in ("text", "thought") and content:
            is_valid, _ = InputValidator.validate_llm_response(content)
            if not is_valid:
                log.warning(
                    "grok.response.truncated",
                    event_type=event_type,
                    original_length=len(content),
                    max_length=MAX_LLM_RESPONSE_LENGTH,
                )
                content = content[:MAX_LLM_RESPONSE_LENGTH]

        if event_type == "text":
            if not content:
                return []
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    # Tag streamed text deltas so _update_last_content
                    # accumulates them into the final message (see below).
                    data={"grok_text_delta": True},
                    resume_handle=current_handle,
                )
            ]

        if event_type == "thought":
            if not content:
                return []
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    data={"thinking": content},
                    resume_handle=current_handle,
                )
            ]

        if event_type == "error" or is_error:
            return [
                AgentMessage(
                    type="system",
                    content=f"Grok Error: {content}" if content else "Grok Error",
                    data={"is_error": True, "metadata": metadata},
                    resume_handle=current_handle,
                )
            ]

        if event_type == "end":
            # Terminal event. Assistant text already arrived via `text` events;
            # surface a terminal marker carrying the stop metadata.
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    data={"terminal": True, "metadata": metadata},
                    resume_handle=current_handle,
                )
            ]

        # Ignore auxiliary events that don't map to messages.
        return []

    # -- Session resumption ------------------------------------------------

    def _build_resume_recovery(
        self,
        *,
        attempted_resume_session_id: str | None,
        current_handle: RuntimeHandle | None,
        returncode: int,
        final_message: str,
        stderr_lines: list[str],
    ) -> tuple[RuntimeHandle | None, AgentMessage | None] | None:
        """Return None — Grok session resume is not wired in v1."""
        del (
            attempted_resume_session_id,
            current_handle,
            returncode,
            final_message,
            stderr_lines,
        )
        return None


__all__ = ["GrokCliRuntime"]
