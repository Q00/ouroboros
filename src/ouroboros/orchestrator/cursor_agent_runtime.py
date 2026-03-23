"""Cursor Agent CLI runtime for Ouroboros orchestrator execution.

This adapter uses the locally installed ``cursor-agent`` CLI to spawn
headless agent sessions that can read/write files and execute commands
in the local workspace — the same pattern as CodexCliRuntime but
targeting the Cursor Agent CLI.

Install:
    curl https://cursor.com/install -fsSL | bash

Authenticate:
    cursor-agent login          # browser OAuth
    # or
    export CURSOR_API_KEY=<key>  # headless / CI

Usage:
    Set ``runtime_backend: cursor`` in ``~/.ouroboros/config.yaml``, or
    ``export OUROBOROS_AGENT_RUNTIME=cursor``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime

log = get_logger(__name__)


class CursorAgentRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed ``cursor-agent`` CLI.

    Inherits the full subprocess-based execution, NDJSON streaming, session
    resume, and skill dispatch infrastructure from :class:`CodexCliRuntime`.
    Only the CLI-specific details (binary name, command flags, permission
    model) are overridden.

    ``cursor-agent`` supports:

    - ``-p`` (print / headless mode) with ``--output-format stream-json``
    - ``-f`` (force: auto-approve file writes and shell commands)
    - ``--model <name>`` (model selection, e.g. gpt-5, sonnet-4)
    - ``--resume <chatId>`` (session resume)
    - ``--workspace <path>`` (working directory)
    - ``--approve-mcps`` (auto-approve MCP servers in headless mode)
    """

    # ── Class-level overrides ────────────────────────────────────────────
    _runtime_handle_backend = "cursor_agent"
    _runtime_backend = "cursor"
    _provider_name = "cursor_agent"
    _runtime_error_type = "CursorAgentError"
    _log_namespace = "cursor_agent_runtime"
    _display_name = "Cursor Agent"
    _default_cli_name = "cursor-agent"
    _default_llm_backend = "cursor"
    _tempfile_prefix = "ouroboros-cursor-"
    _skills_package_uri = "packaged://ouroboros.cursor/skills"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 3

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: Any = None,
        llm_backend: str | None = None,
    ) -> None:
        self._check_auth()
        super().__init__(
            cli_path=cli_path,
            permission_mode=permission_mode or "default",
            model=model,
            cwd=cwd,
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
            llm_backend=llm_backend or self._default_llm_backend,
        )

    # ── Authentication ───────────────────────────────────────────────────

    @staticmethod
    def _check_auth() -> None:
        """Verify cursor-agent is authenticated.

        Raises:
            ValueError: If cursor-agent is not found or not authenticated.
        """
        from ouroboros.providers.cursor_agent_adapter import check_cursor_agent_auth

        check_cursor_agent_auth()

    # ── CLI path resolution ──────────────────────────────────────────────

    def _get_configured_cli_path(self) -> str | None:
        """Resolve cursor-agent CLI path from environment or known locations."""
        env_path = os.environ.get("CURSOR_AGENT_PATH")
        if env_path:
            return env_path

        home = Path.home()
        for candidate in (
            home / ".local" / "bin" / "cursor-agent",
            Path("/usr/local/bin/cursor-agent"),
        ):
            if candidate.exists():
                return str(candidate)

        return None

    # ── Permission flags ─────────────────────────────────────────────────

    def _build_permission_args(self) -> list[str]:
        """Translate permission mode into cursor-agent CLI flags.

        Mapping:
            - ``bypassPermissions`` → ``--force`` (no confirmations at all)
            - ``acceptEdits`` → ``--force`` (auto-approve writes and commands)
            - ``default`` → no flags (agent asks for confirmation)

        cursor-agent uses ``--force`` for auto-approval, unlike Codex which
        has a granular ``--ask-for-approval`` system.
        """
        if self._permission_mode in ("bypassPermissions", "acceptEdits"):
            return ["--force"]
        return []

    # ── Command construction ─────────────────────────────────────────────

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        """Build the cursor-agent CLI command.

        Key differences from Codex CLI:

        - ``-p`` for headless/print mode
        - ``--output-format stream-json`` for NDJSON events
        - ``--workspace`` instead of ``-C``
        - ``--resume <chatId>`` for session resume
        - Prompt is passed as a positional argument, not via stdin
        """
        command = [self._cli_path]

        if resume_session_id:
            command.extend(["--resume", resume_session_id])

        command.extend(["-p", "--output-format", "stream-json"])
        command.extend(["--workspace", self._cwd])

        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["--model", normalized_model])

        command.extend(self._build_permission_args())
        command.append("--approve-mcps")

        if prompt and not resume_session_id:
            command.append(prompt)

        return command

    # ── Stdin/prompt handling ────────────────────────────────────────────

    def _requires_process_stdin(self) -> bool:
        """cursor-agent takes prompt as positional arg, not stdin."""
        return False

    def _feeds_prompt_via_stdin(self) -> bool:
        """cursor-agent takes prompt as positional arg, not stdin."""
        return False

    # ── Model normalization ──────────────────────────────────────────────

    def _normalize_model(self, model: str | None) -> str | None:
        """Pass model name through unchanged.

        cursor-agent accepts short names like ``gpt-5``, ``sonnet-4``,
        ``sonnet-4-thinking``, etc.
        """
        return model
