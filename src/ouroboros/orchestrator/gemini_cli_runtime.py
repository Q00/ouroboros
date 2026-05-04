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
        """
        if permission_mode and permission_mode in _GEMINI_PERMISSION_MODES:
            return permission_mode
        return _GEMINI_DEFAULT_PERMISSION_MODE

    def _build_permission_args(self) -> list[str]:
        """Return empty list — Gemini CLI has no Codex-style permission flags."""
        return []

    # -- Environment and security ------------------------------------------

    def _build_child_env(self) -> dict[str, str]:
        """Build child env with the recursion guard (matches #315 adapter pattern)."""
        env = os.environ.copy()

        # Prevent child from re-entering Ouroboros MCP
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)

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

        Reads from :func:`ouroboros.config.get_gemini_cli_path`, which checks
        ``OUROBOROS_GEMINI_CLI_PATH`` and persisted ``orchestrator.gemini_cli_path``.
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
        runtime_handle: RuntimeHandle | None = None,
    ) -> list[str]:
        """Build the Gemini CLI command arguments for non-interactive execution.

        Headless contract:
        - ``--prompt`` carries the request (Gemini's documented headless trigger).
        - ``--non-interactive`` disables TTY prompts so the subprocess never blocks.
        - ``--output-format stream-json`` emits NDJSON events on stdout.
        - ``--approval-mode yolo`` skips interactive approvals (required for headless).
        """
        del output_last_message_path, resume_session_id, runtime_handle

        command = [
            self._cli_path,
            "--prompt",
            prompt or "",
            "--non-interactive",
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
        """Extract a backend-native session id from a runtime event.

        Looks at standard top-level keys first, then ``metadata`` and the raw
        payload (where Gemini's ``init`` event lands its ``session_id``).
        """
        session_id = super()._extract_event_session_id(event)
        if session_id:
            return session_id

        metadata = event.get("metadata", {})
        if isinstance(metadata, dict):
            session_id = metadata.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()

        raw = event.get("raw")
        if isinstance(raw, dict):
            session_id = raw.get("session_id")
            if isinstance(session_id, str) and session_id.strip():
                return session_id.strip()

        return None

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a Gemini CLI output line into an internal event dict."""
        if not line.strip():
            return None

        return self._normalizer.normalize_line(line)

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert a Gemini CLI event into normalized AgentMessage values.

        Handles the documented Gemini ``stream-json`` schema:

        - ``init`` — session metadata (session_id is captured by
          ``_extract_event_session_id``); produces no AgentMessage
        - ``message`` / ``text`` — assistant prose
        - ``thinking`` — internal reasoning
        - ``tool_use`` — tool invocation request
        - ``tool_result`` — tool output
        - ``error`` — error condition
        - ``result`` — terminal payload carrying the final response (Gemini emits
          the assistant's final answer here when no intermediate ``message``
          event was produced); the normalizer extracts ``response`` into
          ``content``, so we surface it as the final assistant message.
        """
        event_type = event.get("type")
        content = event.get("content", "")
        metadata = event.get("metadata", {})
        is_error = event.get("is_error", False)

        # Truncate content using InputValidator for text-bearing events
        # (including the terminal `result` payload, since `response` may be long).
        if event_type in ("text", "message", "thinking", "result"):
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

        if event_type == "result":
            # Terminal event. The normalizer maps `response` into `content`;
            # if no response text is present we still emit a marker message
            # carrying the metadata so downstream callers see the completion.
            if not content:
                return [
                    AgentMessage(
                        type="assistant",
                        content="",
                        data={"terminal": True, "metadata": metadata},
                        resume_handle=current_handle,
                    )
                ]
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    data={"terminal": True, "metadata": metadata},
                    resume_handle=current_handle,
                )
            ]

        # Ignore other auxiliary events (init/done/etc.) that don't map
        # to messages; init's session_id is captured separately above.
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
        """Return None — Gemini CLI does not support session resumption."""
        del (
            attempted_resume_session_id,
            current_handle,
            returncode,
            final_message,
            stderr_lines,
        )
        return None


__all__ = ["GeminiCLIRuntime"]
