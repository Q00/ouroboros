"""Gemini CLI runtime for Ouroboros orchestrator execution.

Thin subclass of CodexCliRuntime that overrides CLI-specific behaviour
for the Google Gemini CLI (flag construction, permission mapping, session
resume mechanics).
"""

from __future__ import annotations

import re
from typing import Any

from ouroboros.config import get_gemini_cli_path
from ouroboros.gemini_permissions import (
    build_gemini_exec_permission_args,
    resolve_gemini_permission_mode,
)
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class GeminiCliRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed Gemini CLI."""

    _runtime_handle_backend = "gemini_cli"
    _runtime_backend = "gemini"
    _provider_name = "gemini_cli"
    _runtime_error_type = "GeminiCliError"
    _log_namespace = "gemini_cli_runtime"
    _display_name = "Gemini CLI"
    _default_cli_name = "gemini"
    _default_llm_backend = "gemini"
    _tempfile_prefix = "ouroboros-gemini-"

    # -- Permission helpers ------------------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the runtime permission mode."""
        return resolve_gemini_permission_mode(
            permission_mode,
            default_mode="acceptEdits",
        )

    def _build_permission_args(self) -> list[str]:
        """Translate the configured permission mode into backend CLI flags."""
        return build_gemini_exec_permission_args(
            self._permission_mode,
            default_mode="acceptEdits",
        )

    # -- CLI path resolution -----------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available."""
        return get_gemini_cli_path()

    # -- Command construction ----------------------------------------------

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        """Build the Gemini CLI command.  Prompt is fed via stdin."""
        command = [self._cli_path]

        if resume_session_id:
            if not _SAFE_SESSION_ID_PATTERN.match(resume_session_id):
                raise ValueError(
                    f"Invalid resume_session_id: contains disallowed characters: "
                    f"{resume_session_id!r}"
                )
            command.extend(["--resume", resume_session_id])

        # Non-interactive headless mode: -p "" means read prompt from stdin
        command.extend(
            [
                "-p",
                "",
                "-o",
                "stream-json",
            ]
        )

        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["-m", normalized_model])

        command.extend(self._build_permission_args())
        return command

    # -- Stdin / prompt feeding -------------------------------------------

    def _feeds_prompt_via_stdin(self) -> bool:
        """Gemini reads prompt from stdin when ``-p`` flag is present."""
        return True

    # -- Event parsing overrides ------------------------------------------

    def _extract_event_session_id(self, event: dict[str, Any]) -> str | None:
        """Extract a session identifier from a Gemini runtime event."""
        # Try Gemini-specific keys first, then fall back to parent logic.
        for key in ("session_id", "sessionId"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return super()._extract_event_session_id(event)


__all__ = ["GeminiCliRuntime"]
