"""Gemini CLI runtime for Ouroboros orchestrator execution.

This module provides the GeminiCLIRuntime that shells out to the locally
installed ``gemini`` CLI to execute agentic tasks.

Usage:
    runtime = GeminiCLIRuntime(model="gemini-2.5-pro", cwd="/path/to/project")
    async for message in runtime.execute_task("Fix the bug in auth.py"):
        print(message.content)

Custom CLI Path:
    Set via constructor parameter or environment variable:
        runtime = GeminiCLIRuntime(cli_path="/path/to/gemini")
        # or
        export OUROBOROS_GEMINI_CLI_PATH=/path/to/gemini
"""

from __future__ import annotations

import json
from typing import Any

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

# Gemini CLI has no Codex-style permission mode flags.
# The mode names are kept for interface compatibility.
_GEMINI_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions"})
_GEMINI_DEFAULT_PERMISSION_MODE = "default"


class GeminiCLIRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed Gemini CLI.

    Extends :class:`~ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime`
    with overrides specific to the Gemini CLI process model:

    - No Codex-style permission flags (Gemini manages permissions internally)
    - No session resumption (stateless execution model)
    - Plain-text and/or JSON event output normalization
    - Synthetic ``gemini.content`` event wrapping for plain-text lines

    Example:
        runtime = GeminiCLIRuntime(
            model="gemini-2.5-pro",
            cwd="/path/to/project",
        )
        async for message in runtime.execute_task(
            "Refactor the authentication module",
            tools=["Read", "Edit", "Bash"],
        ):
            print(message.content)
    """

    _runtime_handle_backend = "gemini_cli"
    _runtime_backend = "gemini"
    _requires_memory_gate = False
    _provider_name = "gemini_cli"
    _runtime_error_type = "GeminiCliError"
    _log_namespace = "gemini_cli_runtime"
    _display_name = "Gemini CLI"
    _default_cli_name = "gemini"
    _default_llm_backend = "gemini"
    _tempfile_prefix = "ouroboros-gemini-"
    _skills_package_uri = "packaged://ouroboros.gemini/skills"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 0  # Gemini CLI does not support session resumption

    # -- Permission mode overrides -----------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Normalize the permission mode for Gemini CLI.

        Gemini CLI has its own internal permission model.  Ouroboros modes
        are stored for metadata purposes but no permission flags are emitted.

        Args:
            permission_mode: Requested permission mode, or None for default.

        Returns:
            Resolved permission mode string.
        """
        if permission_mode and permission_mode in _GEMINI_PERMISSION_MODES:
            return permission_mode
        return _GEMINI_DEFAULT_PERMISSION_MODE

    def _build_permission_args(self) -> list[str]:
        """Return empty list — Gemini CLI has no Codex-style permission flags.

        Unlike Codex CLI, Gemini CLI manages its own permission model
        internally.  Ouroboros does not inject any permission-related flags
        into the subprocess command line.

        Returns:
            An empty list (no permission flags emitted).
        """
        return []

    # -- CLI path resolution -----------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available.

        Reads the configured Gemini CLI binary path from
        :func:`ouroboros.config.get_gemini_cli_path`, which checks the
        ``OUROBOROS_GEMINI_CLI_PATH`` environment variable and any persisted
        configuration store.

        Returns:
            Absolute path string to the Gemini CLI binary if configured,
            or ``None`` to fall back to searching ``$PATH`` for the default
            binary name (``gemini``).
        """
        from ouroboros.config import get_gemini_cli_path

        return get_gemini_cli_path()

    # -- Command construction ----------------------------------------------

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        """Build the Gemini CLI command arguments.

        The prompt is written to stdin by the base class; this method only
        constructs the base command with optional model selection.

        Args:
            output_last_message_path: Unused — Gemini CLI writes all output
                to stdout rather than a separate output file.
            resume_session_id: Unused — Gemini CLI is stateless.
            prompt: Unused here — written via stdin by the base execute loop.

        Returns:
            List of command-line arguments.
        """
        # output_last_message_path, resume_session_id, and prompt are
        # intentionally unused — Gemini CLI does not support them.
        del output_last_message_path, resume_session_id, prompt

        command = [self._cli_path]
        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["--model", normalized_model])
        return command

    # -- Event parsing and normalization -----------------------------------

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a Gemini CLI output line into an internal event dict.

        Attempts JSON parsing first (for future or ``--json`` mode output),
        then wraps plain-text lines as synthetic ``gemini.content`` events so
        the base execution loop can accumulate them into the final content.

        Args:
            line: A single line from the Gemini CLI stdout stream.

        Returns:
            Parsed event dict, or None for empty lines.
        """
        stripped = line.strip()
        if not stripped:
            return None

        # First, try to parse as JSON (structured output or future JSON mode).
        try:
            event = json.loads(stripped)
            if isinstance(event, dict):
                return event
        except json.JSONDecodeError:
            pass

        # Wrap plain text as a synthetic content event so the base loop
        # can track last_content and yield assistant messages.
        return {"type": "gemini.content", "text": stripped}

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert a Gemini CLI event into normalized AgentMessage values.

        Handles the synthetic ``gemini.content`` event type produced by
        :meth:`_parse_json_event` for plain-text output lines, and falls
        back to the parent class for any structured JSON events.

        Args:
            event: Parsed event dict (possibly synthetic).
            current_handle: Current runtime handle, if any.

        Returns:
            List of normalized AgentMessage values.
        """
        event_type = event.get("type")

        if event_type == "gemini.content":
            content = event.get("text", "")
            if not content:
                return []
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    resume_handle=current_handle,
                )
            ]

        # Fall back to parent class for any structured Codex-compatible events
        # (e.g., if a future Gemini CLI version emits JSON events).
        return super()._convert_event(event, current_handle)

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
        """Return None — Gemini CLI does not support session resumption.

        Args:
            attempted_resume_session_id: Unused.
            current_handle: Unused.
            returncode: Unused.
            final_message: Unused.
            stderr_lines: Unused.

        Returns:
            Always None.
        """
        del (
            attempted_resume_session_id,
            current_handle,
            returncode,
            final_message,
            stderr_lines,
        )
        return None


__all__ = ["GeminiCLIRuntime"]
