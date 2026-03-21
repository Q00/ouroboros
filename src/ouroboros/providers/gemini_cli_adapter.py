"""Gemini CLI adapter for LLM completion using local Gemini CLI.

This adapter shells out to `gemini -p` in non-interactive mode, allowing
Ouroboros to use a local Gemini CLI session for single-turn completion tasks.
"""

from __future__ import annotations

from ouroboros.config import get_gemini_cli_path
from ouroboros.gemini_permissions import (
    build_gemini_exec_permission_args,
    resolve_gemini_permission_mode,
)
from ouroboros.providers.codex_cli_adapter import CodexCliLLMAdapter


class GeminiCliLLMAdapter(CodexCliLLMAdapter):
    """LLM adapter backed by local Gemini CLI execution."""

    _provider_name = "gemini_cli"
    _display_name = "Gemini CLI"
    _default_cli_name = "gemini"
    _tempfile_prefix = "ouroboros-gemini-llm-"
    _schema_tempfile_prefix = "ouroboros-gemini-schema-"

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the adapter permission mode."""
        return resolve_gemini_permission_mode(permission_mode, default_mode="default")

    def _build_permission_args(self) -> list[str]:
        """Translate the configured permission mode into backend CLI flags."""
        return build_gemini_exec_permission_args(
            self._permission_mode,
            default_mode="default",
        )

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available."""
        return get_gemini_cli_path()

    def _build_command(
        self,
        *,
        output_last_message_path: str,
        output_schema_path: str | None,
        model: str | None,
    ) -> list[str]:
        """Build the Gemini CLI command for a one-shot completion.

        Gemini uses ``-p`` for non-interactive headless mode.
        The prompt is fed via stdin (Gemini appends stdin to ``-p``).
        """
        command = [
            self._cli_path,
            "-p",
            "",  # empty prompt flag; actual prompt comes via stdin
            "-o",
            "json",
        ]

        command.extend(self._build_permission_args())

        if model:
            command.extend(["-m", model])

        return command

    def _extract_session_id(self, stdout_lines: list[str]) -> str | None:
        """Extract a session id from Gemini JSONL stdout."""
        for line in stdout_lines:
            event = self._parse_json_event(line)
            if not event:
                continue
            if isinstance(event.get("session_id"), str):
                return event["session_id"]
        return None

    def _extract_session_id_from_event(self, event: dict, /) -> str | None:
        """Extract a session id from a single Gemini event."""
        if isinstance(event.get("session_id"), str):
            return event["session_id"]
        return None


__all__ = ["GeminiCliLLMAdapter"]
