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
from typing import Any

import structlog

from ouroboros.copilot.cli_policy import (
    DEFAULT_MAX_OUROBOROS_DEPTH,
    build_copilot_child_env,
)
from ouroboros.copilot.model_discovery import map_to_copilot_model
from ouroboros.copilot.runtime_profile import resolve_copilot_agent
from ouroboros.copilot_permissions import (
    build_copilot_exec_permission_args,
    resolve_copilot_permission_mode,
)
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.orchestrator.codex_cli_runtime import CodexCliRuntime, SkillDispatchHandler
from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.profiles import resolve_completion_profile

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
        runtime_profile: str | None = None,
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
        self._runtime_profile = runtime_profile
        self._copilot_agent = resolve_copilot_agent(
            runtime_profile,
            logger=log,
            log_namespace=self._log_namespace,
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
        del output_last_message_path, resume_session_id

        command = [
            self._cli_path,
            "--no-color",
            "--log-level",
            "none",
            "--add-dir",
            self._cwd,
        ]
        command.extend(self._build_permission_args())

        if self._copilot_agent:
            command.extend(["--agent", self._copilot_agent])
        else:
            normalized_model = self._normalize_model(self._model)
            if normalized_model:
                mapped = map_to_copilot_model(normalized_model)
                command.extend(["--model", mapped])
            else:
                runtime_model, runtime_agent = self._resolve_runtime_copilot_config(runtime_handle)
                if runtime_agent:
                    command.extend(["--agent", runtime_agent])
                else:
                    normalized_runtime_model = self._normalize_model(runtime_model)
                    if normalized_runtime_model:
                        mapped = map_to_copilot_model(normalized_runtime_model)
                        command.extend(["--model", mapped])

        command.extend(["-p", prompt or ""])
        return command

    def _resolve_runtime_copilot_config(
        self,
        runtime_handle: RuntimeHandle | None,
    ) -> tuple[str | None, str | None]:
        """Resolve model/agent settings for a Copilot agent-runtime task."""
        profile_name = self._runtime_profile_from_metadata(runtime_handle)
        if profile_name:
            native_agent = resolve_copilot_agent(
                profile_name,
                logger=log,
                log_namespace=self._log_namespace,
            )
            if native_agent:
                return None, native_agent

        role = None if profile_name else self._runtime_profile_role(runtime_handle)
        resolved = resolve_completion_profile(
            CompletionConfig(model="default", profile=profile_name, role=role),
            backend="copilot",
        )
        return resolved.config.model, resolved.backend_profile

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return False — Copilot CLI accepts the prompt via the ``-p`` flag."""
        return False

    def _requires_process_stdin(self) -> bool:
        """Return False — Copilot CLI does not need a writable stdin pipe."""
        return False

    # -- Event conversion ---------------------------------------------------

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert Copilot JSONL events into normalized runtime messages.

        Copilot CLI emits a different event schema from Codex CLI. Reusing the
        Codex parser would drop successful ``agent.message`` events and make
        the runtime fall back to a generic completion string. Keep the mapping
        deliberately small and schema-tolerant so the orchestrator returns the
        model's actual answer while still preserving lifecycle and tool hints.
        """
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return []

        if event_type in {"session.started", "session.created", "session.ready"}:
            session_id = self._extract_event_session_id(event)
            if not session_id:
                return []
            handle = self._build_runtime_handle(session_id, current_handle)
            return [
                AgentMessage(
                    type="system",
                    content=f"Session initialized: {session_id}",
                    data={"subtype": "init", "session_id": session_id},
                    resume_handle=handle,
                )
            ]

        if event_type in {"agent.message", "message"}:
            content = self._extract_text(event)
            if not content:
                return []
            return [AgentMessage(type="assistant", content=content, resume_handle=current_handle)]

        if event_type in {"reasoning", "thinking"}:
            content = self._extract_text(event)
            if not content:
                return []
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    data={"thinking": content, "subtype": event_type},
                    resume_handle=current_handle,
                )
            ]

        if event_type in {"tool_use", "tool.start", "tool_call"}:
            tool_name_obj = event.get("name") or event.get("tool")
            tool_name = (
                tool_name_obj if isinstance(tool_name_obj, str) and tool_name_obj else "tool"
            )
            tool_input = event.get("input") if isinstance(event.get("input"), dict) else {}
            return [
                self._build_tool_message(
                    tool_name=tool_name,
                    tool_input=tool_input,
                    content=f"Calling tool: {tool_name}",
                    handle=current_handle,
                    extra_data={"subtype": event_type},
                )
            ]

        if event_type in {"error", "turn.failed", "fatal"}:
            payload = event.get("error") if event_type == "turn.failed" else event
            content = self._extract_text(payload) or f"{self._display_name} reported an error"
            return [
                AgentMessage(
                    type="result",
                    content=content,
                    data={"subtype": "error", "error_type": "CopilotCliError"},
                    resume_handle=current_handle,
                )
            ]

        if event_type in {"turn.completed", "run.completed", "session.ended"}:
            return []

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
