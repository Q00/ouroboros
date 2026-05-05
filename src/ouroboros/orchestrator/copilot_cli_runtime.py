"""GitHub Copilot CLI runtime for Ouroboros orchestrator execution.

Provides :class:`CopilotCliRuntime` that shells out to the locally installed
``copilot`` CLI in non-interactive (``-p``) mode to execute agentic tasks.

Mirrors the Gemini runtime pattern: extends :class:`CodexCliRuntime` and
overrides only the methods that differ from the Codex contract. Reuses the
permission flag mapping, child-env recursion guard, and CLI path resolution
helpers already shipped for the Copilot LLM adapter.

Usage:
    runtime = CopilotCliRuntime(model="claude-opus-4.6", cwd="/path/to/project")
    async for message in runtime.execute_task("Fix the bug in auth.py"):
        print(message.content)

Custom CLI path:
    Set via constructor parameter or environment variable:
        runtime = CopilotCliRuntime(cli_path="/path/to/copilot")
        # or
        export OUROBOROS_COPILOT_CLI_PATH=/path/to/copilot
"""

from __future__ import annotations

from pathlib import Path

import structlog

from ouroboros.copilot.cli_policy import (
    DEFAULT_MAX_OUROBOROS_DEPTH,
    build_copilot_child_env,
)
from ouroboros.copilot.model_discovery import map_to_copilot_model
from ouroboros.copilot_permissions import (
    build_copilot_exec_permission_args,
    resolve_copilot_permission_mode,
)
from ouroboros.orchestrator.adapter import RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime, SkillDispatchHandler

log = structlog.get_logger(__name__)

# Copilot CLI accepts the same three permission mode names that Ouroboros
# uses everywhere; the mapping to ``--allow-tool`` / ``--deny-tool`` /
# ``--allow-all`` flags lives in ``copilot_permissions``.
_COPILOT_PERMISSION_MODES = frozenset({"default", "acceptEdits", "bypassPermissions"})
_COPILOT_DEFAULT_PERMISSION_MODE = "default"

#: Maximum Ouroboros nesting depth to prevent fork bombs when Copilot
#: spawns Ouroboros which spawns Copilot.
_MAX_OUROBOROS_DEPTH = DEFAULT_MAX_OUROBOROS_DEPTH


class CopilotCliRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed Copilot CLI.

    Extends :class:`CodexCliRuntime` with overrides specific to the Copilot
    CLI process model:

    - Permission flags translated through the Copilot envelope
      (``--add-dir`` boundary plus ``--available-tools`` / ``--allow-tool``
      / ``--allow-all-tools`` / ``--allow-all``).
    - Prompt is passed via the ``-p <prompt>`` flag, not stdin.
    - No ``--output-last-message`` flag (Copilot reconstructs the assistant
      reply from the JSONL event stream).
    - No session resumption (Copilot CLI does not expose a resume API).
    - Hyphenated Anthropic model IDs are auto-mapped to the dotted Copilot
      form via :func:`map_to_copilot_model` so existing per-role overrides
      keep working when users switch backends.
    """

    _runtime_handle_backend = "copilot_cli"
    _runtime_backend = "copilot"
    _requires_memory_gate = False
    _provider_name = "copilot_cli"
    _runtime_error_type = "CopilotCliError"
    _log_namespace = "copilot_cli_runtime"
    _display_name = "Copilot CLI"
    _default_cli_name = "copilot"
    _default_llm_backend = "copilot"
    _tempfile_prefix = "ouroboros-copilot-"
    _skills_package_uri = "packaged://ouroboros.copilot/skills"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 0  # Copilot CLI does not support session resumption

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
        super().__init__(
            cli_path=cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=cwd,
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
            llm_backend=llm_backend,
        )

    # -- Permission mode overrides -----------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Normalize the permission mode for Copilot CLI."""
        return resolve_copilot_permission_mode(
            permission_mode,
            default_mode=_COPILOT_DEFAULT_PERMISSION_MODE,
        )

    def _build_permission_args(self) -> list[str]:
        """Map the resolved permission mode to Copilot CLI flags."""
        return build_copilot_exec_permission_args(
            self._permission_mode,
            default_mode=_COPILOT_DEFAULT_PERMISSION_MODE,
        )

    # -- Environment and security ------------------------------------------

    def _build_child_env(self) -> dict[str, str]:
        """Build child env with the Copilot recursion guard."""
        return build_copilot_child_env(
            max_depth=_MAX_OUROBOROS_DEPTH,
            depth_error_factory=lambda _depth, max_depth: RuntimeError(
                f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
            ),
        )

    # -- CLI path resolution -----------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available.

        Reads from :func:`ouroboros.config.get_copilot_cli_path`, which checks
        ``OUROBOROS_COPILOT_CLI_PATH`` and persisted
        ``orchestrator.copilot_cli_path``.
        """
        from ouroboros.config import get_copilot_cli_path

        return get_copilot_cli_path()

    # -- Command construction ----------------------------------------------

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        runtime_handle: RuntimeHandle | None = None,
    ) -> list[str]:
        """Build the Copilot CLI command for non-interactive execution.

        Headless contract:
        - ``-p <PROMPT>`` carries the request (Copilot's documented one-shot trigger).
        - ``--no-color`` keeps stdout JSONL parser-friendly.
        - ``--log-level none`` suppresses non-event log lines.
        - ``--add-dir <CWD>`` pins the sandbox-write boundary.
        - Permission flags are derived from the resolved permission mode.
        - ``--model`` is appended after auto-mapping hyphenated Anthropic IDs
          to the dotted Copilot form Copilot CLI expects.

        Copilot CLI does not support a session-resume flag, so
        ``resume_session_id`` is ignored. ``output_last_message_path`` is
        also unused (the assistant reply is reconstructed from the JSONL
        event stream by ``_convert_event``).
        """
        del output_last_message_path, resume_session_id, runtime_handle

        command = [
            self._cli_path,
            "--no-color",
            "--log-level",
            "none",
            "--add-dir",
            self._cwd,
        ]
        command.extend(self._build_permission_args())

        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            mapped = map_to_copilot_model(normalized_model)
            command.extend(["--model", mapped])

        command.extend(["-p", prompt or ""])
        return command

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return False — Copilot CLI accepts the prompt via the ``-p`` flag."""
        return False

    def _requires_process_stdin(self) -> bool:
        """Return False — Copilot CLI does not need a writable stdin pipe."""
        return False

    # -- Session resumption ------------------------------------------------

    def _build_resume_recovery(
        self,
        *,
        attempted_resume_session_id: str | None,
        current_handle: RuntimeHandle | None,
        returncode: int,
        final_message: str,
        stderr_lines: list[str],
    ) -> tuple[RuntimeHandle | None, object | None] | None:
        """Return None — Copilot CLI does not support session resumption."""
        del (
            attempted_resume_session_id,
            current_handle,
            returncode,
            final_message,
            stderr_lines,
        )
        return None


__all__ = ["CopilotCliRuntime"]
