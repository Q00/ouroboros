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

import os
from pathlib import Path
from typing import Any

import structlog

from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime, SkillDispatchHandler
from ouroboros.providers.gemini_event_normalizer import GeminiEventNormalizer

log = structlog.get_logger(__name__)

# Gemini CLI has no Codex-style permission mode flags.
# The mode names are kept for interface compatibility.
_GEMINI_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions"})
_GEMINI_DEFAULT_PERMISSION_MODE = "default"

#: Maximum Ouroboros nesting depth to prevent fork bombs
_MAX_OUROBOROS_DEPTH = 5


class GeminiCLIRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed Gemini CLI.

    Extends :class:`~ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime`
    with overrides specific to the Gemini CLI process model:

    - No Codex-style permission flags (Gemini manages permissions internally)
    - No session resumption (stateless execution model)
    - Plain-text and/or JSON event output normalization via GeminiEventNormalizer
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

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
    ) -> None:
        """Initialize the Gemini CLI runtime.

        Args:
            cli_path: Optional path to the gemini binary.
            permission_mode: Optional permission mode (ignored by Gemini).
            model: Optional model identifier.
            cwd: Optional working directory for the subprocess.
            skills_dir: Optional directory for skill definitions.
            skill_dispatcher: Optional handler for skill execution.
            llm_backend: Optional LLM backend identifier.
        """
        super().__init__(
            cli_path=cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=cwd,
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
            llm_backend=llm_backend,
        )
        # Initialize the stateless event normalizer
        self._normalizer = GeminiEventNormalizer(strict_json=False)

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

    # -- Environment and security ------------------------------------------

    def _build_child_env(self) -> dict[str, str]:
        """Build the environment variables for the child Gemini process.

        Implements the recursion guard (_OUROBOROS_DEPTH) to prevent
        accidental fork bombs when Ouroboros is used as a tool inside
        another Ouroboros session.

        Returns:
            Dictionary of environment variables for the subprocess.

        Raises:
            RuntimeError: When the maximum nesting depth is exceeded.
        """
        env = os.environ.copy()

        # Prevent child from re-entering Ouroboros MCP
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)

        # Track and enforce recursion depth to prevent fork bombs.
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1

        if depth > _MAX_OUROBOROS_DEPTH:
            msg = f"Maximum Ouroboros nesting depth ({_MAX_OUROBOROS_DEPTH}) exceeded"
            raise RuntimeError(msg)

        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

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

        Args:
            output_last_message_path: Unused — Gemini CLI writes all output
                to stdout rather than a separate output file.
            resume_session_id: Unused — Gemini CLI is stateless.
            prompt: The prompt text.

        Returns:
            List of command-line arguments.
        """
        # output_last_message_path and resume_session_id are
        # intentionally unused — Gemini CLI does not support them.
        del output_last_message_path, resume_session_id

        command = [
            self._cli_path,
            "--prompt",
            prompt or "",
            "--output-format",
            "stream-json",
            "--approval-mode",
            "yolo",
        ]
        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["--model", normalized_model])
        return command

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return False — Gemini CLI accepts the prompt via the --prompt flag."""
        return False

    def _requires_process_stdin(self) -> bool:
        """Return False — Gemini CLI doesn't need an interactive stdin pipe."""
        return False

    # -- Event parsing and normalization -----------------------------------

    def _extract_event_session_id(self, event: dict[str, Any]) -> str | None:
        """Extract a backend-native session identifier from a runtime event.

        Looks for session identifiers in the top level (standard keys),
        the ``metadata`` dict, and the ``raw`` dict (original payload).

        Args:
            event: Normalized event dict.

        Returns:
            Session identifier string, or None if not found.
        """
        # 1. Check top level (standard keys from parent)
        session_id = super()._extract_event_session_id(event)
        if session_id:
            return session_id

        # 2. Check metadata (normalized fields)
        metadata = event.get("metadata", {})
        if isinstance(metadata, dict):
            session_id = metadata.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()

        # 3. Check raw (original un-normalized payload)
        raw = event.get("raw")
        if isinstance(raw, dict):
            session_id = raw.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()

        return None

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a Gemini CLI output line into an internal event dict.

        Delegates to :class:`GeminiEventNormalizer` to handle both plain-text
        and structured NDJSON events.

        Args:
            line: A single line from the Gemini CLI stdout stream.

        Returns:
            Normalized event dict, or None for empty lines.
        """
        if not line.strip():
            return None

        return self._normalizer.normalize_line(line)

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert a Gemini CLI event into normalized AgentMessage values.

        Maps the normalized event types (thinking, message, tool_use,
        tool_result, error, etc.) onto the standard Ouroboros AgentMessage schema.

        Args:
            event: Normalized event dict from ``_parse_json_event``.
            current_handle: Current runtime handle, if any.

        Returns:
            List of normalized AgentMessage values.
        """
        event_type = event.get("type")
        content = event.get("content", "")
        metadata = event.get("metadata", {})
        is_error = event.get("is_error", False)

        # Truncate content using InputValidator for text-based events
        if event_type in ("text", "message", "thinking"):
            is_valid, _ = InputValidator.validate_llm_response(content)
            if not is_valid:
                log.warning(
                    "gemini.response.truncated",
                    event_type=event_type,
                    original_length=len(content),
                    max_length=MAX_LLM_RESPONSE_LENGTH,
                )
                content = content[:MAX_LLM_RESPONSE_LENGTH]

        if event_type in ("text", "message"):
            if not content:
                return []
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    resume_handle=current_handle,
                )
            ]

        if event_type == "thinking":
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

        if event_type == "tool_use":
            tool_name = metadata.get("name", "")
            tool_args = metadata.get("input", {})
            return [
                AgentMessage(
                    type="assistant",
                    content=content or f"Using tool: {tool_name}",
                    tool_name=tool_name,
                    data={"tool_input": tool_args},
                    resume_handle=current_handle,
                )
            ]

        if event_type == "tool_result":
            tool_name = metadata.get("name", "")
            return [
                AgentMessage(
                    type="tool",
                    content=content,
                    tool_name=tool_name,
                    data={"is_error": is_error},
                    resume_handle=current_handle,
                )
            ]

        if event_type == "error":
            return [
                AgentMessage(
                    type="system",
                    content=f"Gemini Error: {content}",
                    data={"is_error": True, "metadata": metadata},
                    resume_handle=current_handle,
                )
            ]

        # Ignore unknown or auxiliary events (session_started, done, etc.)
        # that don't map to messages.
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
