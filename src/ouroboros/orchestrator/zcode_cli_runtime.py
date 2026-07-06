"""Zcode CLI runtime for Ouroboros orchestrator execution.

This module provides the ZcodeCLIRuntime that shells out to the locally
installed ``zcode`` CLI to execute agentic tasks.

Usage:
    runtime = ZcodeCLIRuntime(model="glm-5", cwd="/path/to/project")
    async for message in runtime.execute_task("Fix the bug in auth.py"):
        print(message.content)

Custom CLI Path:
    Set via constructor parameter or environment variable:
        runtime = ZcodeCLIRuntime(cli_path="/path/to/zcode")
        # or
        export OUROBOROS_ZCODE_CLI_PATH=/path/to/zcode
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
from ouroboros.runtime.child_env import build_child_env

log = structlog.get_logger(__name__)

# Zcode CLI permission mode mapping. Zcode uses similar approval modes to
# Gemini - map the Ouroboros permission vocabulary to the *non-blocking*
# native modes only.
#
# Ouroboros' ``"default"`` mode (interactive, prompt-driven) is intentionally
# absent: this runtime always launches zcode with ``--non-interactive`` so a
# subprocess that surfaces an approval prompt would wedge indefinitely. Callers
# that pass ``"default"`` are rejected at ``_resolve_permission_mode`` with a
# message pointing them at ``acceptEdits`` (conservative, non-blocking) or
# ``bypassPermissions`` (full bypass).
_ZCODE_PERMISSION_MODE_TO_FLAG = {
    "acceptEdits": "auto_edit",
    "bypassPermissions": "yolo",
}
_ZCODE_PERMISSION_MODES = frozenset(_ZCODE_PERMISSION_MODE_TO_FLAG)
# Match the orchestrator-wide ``acceptEdits`` default. Zcode's ``auto_edit``
# is non-blocking under ``--non-interactive`` (no approval prompts), so
# headless safety does not require silently jumping to ``yolo`` (full bypass)
# when ``permission_mode`` is omitted — operators must opt in to
# ``bypassPermissions`` explicitly.
_ZCODE_DEFAULT_PERMISSION_MODE = "acceptEdits"

#: Maximum Ouroboros nesting depth to prevent fork bombs
_MAX_OUROBOROS_DEPTH = 5
# Child-env strip set for Zcode. Zcode does NOT strip CLAUDECODE (unlike
# codex/copilot/kiro) — preserve that divergence; only the Ouroboros markers
# are removed.
_CHILD_ENV_STRIP_KEYS = ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND")


class ZcodeCLIRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the locally installed Zcode CLI.

    Extends :class:`~ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime`
    with overrides specific to the Zcode CLI process model:

    - No Codex-style permission flags (Zcode manages permissions internally)
    - Session resumption supported via ``--resume`` flag
    - Plain-text and/or JSON event output parsing
    """

    _runtime_handle_backend = "zcode_cli"
    _runtime_backend = "zcode"
    _requires_memory_gate = False
    _provider_name = "zcode_cli"
    _runtime_error_type = "ZcodeCliError"
    _log_namespace = "zcode_cli_runtime"
    _display_name = "Zcode CLI"
    _default_cli_name = "zcode"
    _default_llm_backend = "zcode"
    _tempfile_prefix = "ouroboros-zcode-"
    _skills_package_uri = "packaged://ouroboros.zcode/skills"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 3  # Zcode CLI supports session resumption

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
        """Initialize the Zcode CLI runtime.

        Args:
            cli_path: Optional path to the zcode binary.
            permission_mode: Ouroboros permission level. Recognized
                non-blocking modes (``acceptEdits`` → ``auto_edit``,
                ``bypassPermissions`` → ``yolo``) pass through.
                ``"default"`` is the orchestrator-wide setting that
                represents an interactive prompt; the headless Zcode
                runtime cannot honour it, so it is normalized to
                ``acceptEdits`` with an audit log instead of failing
                — that keeps a globally valid config working while
                avoiding the deadlock under ``--non-interactive``.
                Falls back to ``acceptEdits`` when omitted; operators
                must opt in to ``bypassPermissions`` explicitly.
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

    # -- Permission mode overrides -----------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the Zcode CLI permission mode.

        ``None`` and the orchestrator-wide ``"default"`` setting both
        resolve to :data:`_ZCODE_DEFAULT_PERMISSION_MODE`
        (``acceptEdits`` → ``auto_edit``, non-blocking under
        ``--non-interactive``). ``config.orchestrator.permission_mode``
        accepts ``"default"`` as a valid global setting, so the
        backend-specific contract narrows it at the boundary rather
        than turning a previously valid config into a hard error: a
        prompt-driven ``--approval-mode default`` would wedge a headless
        subprocess.

        Other recognized Ouroboros modes (``acceptEdits``,
        ``bypassPermissions``) pass through. Anything else raises
        ``ValueError`` instead of silently falling back — fail-open on
        a permission boundary would let a typo (or unchecked
        ``OUROBOROS_AGENT_PERMISSION_MODE`` value) escalate the runtime.
        Matches the Codex permission parser contract.
        """
        if permission_mode is None:
            return _ZCODE_DEFAULT_PERMISSION_MODE
        candidate = permission_mode.strip()
        if candidate in _ZCODE_PERMISSION_MODES:
            return candidate
        if candidate == "default":
            log.warning(
                "zcode_cli_runtime.permission_mode_coerced",
                requested="default",
                resolved=_ZCODE_DEFAULT_PERMISSION_MODE,
                reason=(
                    "Zcode runtime is headless (--non-interactive); the "
                    "interactive 'default' approval mode would block, so it "
                    "is normalized to the safe non-blocking equivalent."
                ),
            )
            return _ZCODE_DEFAULT_PERMISSION_MODE
        msg = (
            f"Unsupported Zcode permission mode: {permission_mode!r} "
            f"(expected one of {sorted(_ZCODE_PERMISSION_MODES)})"
        )
        raise ValueError(msg)

    def _build_permission_args(self) -> list[str]:
        """Return empty list — Zcode CLI has no Codex-style permission flags."""
        return []

    # -- Environment and security ------------------------------------------

    def _build_child_env(self) -> dict[str, str]:
        """Build child env with the recursion guard (matches #315 adapter pattern)."""
        return build_child_env(
            strip_keys=_CHILD_ENV_STRIP_KEYS,
            max_depth=_MAX_OUROBOROS_DEPTH,
            depth_error_factory=lambda _depth, max_depth: RuntimeError(
                f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
            ),
        )

    # -- CLI path resolution -----------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available.

        Reads from :func:`ouroboros.config.get_zcode_cli_path`, which checks
        ``OUROBOROS_ZCODE_CLI_PATH`` and persisted ``orchestrator.zcode_cli_path``.
        """
        from ouroboros.config import get_zcode_cli_path

        return get_zcode_cli_path()

    # -- Command construction ----------------------------------------------

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        runtime_handle: RuntimeHandle | None = None,
        # Accepted to honor the shared CodexCliRuntime contract, but ignored:
        # the Zcode CLI exposes no per-invocation effort flag (capabilities
        # declares reasoning_effort_support=IGNORED, so it is surfaced as advised).
        reasoning_effort: str | None = None,
    ) -> list[str]:
        """Build the Zcode CLI command arguments for non-interactive execution.

        Headless contract:
        - ``--prompt`` carries the request (Zcode's documented headless trigger).
        - ``--non-interactive`` disables TTY prompts so the subprocess never blocks.
        - ``--json`` emits JSON events on stdout.
        - ``--approval-mode`` is mapped from ``self._permission_mode``:
          ``acceptEdits`` → ``auto_edit`` (default; non-blocking) and
          ``bypassPermissions`` → ``yolo`` (full bypass). Zcode's native
          ``default`` mode is intentionally unreachable through this runtime
          — :meth:`_resolve_permission_mode` rejects it because a
          prompt-driven mode under ``--non-interactive`` would deadlock the
          subprocess. The fallback to ``auto_edit`` below is defensive only.
        """
        del runtime_handle, reasoning_effort

        approval_flag = _ZCODE_PERMISSION_MODE_TO_FLAG.get(
            self._permission_mode,
            "auto_edit",
        )
        command = [
            self._cli_path,
            "--json",
            "--prompt",
            prompt or "",
            "--non-interactive",
            "--approval-mode",
            approval_flag,
        ]
        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["--model", normalized_model])
        if resume_session_id:
            command.extend(["--resume", resume_session_id])
        return command

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return False — Zcode CLI accepts the prompt via the --prompt flag."""
        return False

    def _requires_process_stdin(self) -> bool:
        """Return False — Zcode CLI doesn't need an interactive stdin pipe."""
        return False

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Declare Zcode CLI's runtime feature contract.

        Zcode emits structured ``--json`` events and can use the shared
        skill dispatcher, and supports targeted session resume via ``--resume``.
        """
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,  # Zcode supports --resume flag
            structured_output=True,
            # System prompt is composed into the user message (inherited Codex
            # prompt builder), not passed as a native system directive. The
            # inherited builder also renders requested tool lists as prompt
            # guidance rather than enforcing a Zcode-native allow-list.
            system_prompt_support=ParamSupport.TRANSLATED,
            tool_restriction_support=ParamSupport.TRANSLATED,
            # Reasoning effort is advised, not enforced: no per-invocation effort
            # flag has been verified. Declared IGNORED (also the default) until a
            # real per-call mechanism is confirmed — revisit if the CLI exposes one.
            reasoning_effort_support=ParamSupport.IGNORED,
        )

    # -- Event parsing and normalization -----------------------------------

    def _extract_event_session_id(self, event: dict[str, Any]) -> str | None:
        """Extract a backend-native session id from a runtime event.

        Looks at standard top-level keys first, then ``metadata`` and the raw
        payload (where Zcode's ``init`` event lands its ``session_id``).
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

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert a Zcode CLI event into normalized AgentMessage values.

        Handles the Zcode ``--json`` event schema:

        - ``init`` — session metadata (session_id is captured by
          ``_extract_event_session_id``); produces no AgentMessage
        - ``message`` / ``text`` — assistant prose
        - ``thinking`` — internal reasoning
        - ``tool_use`` — tool invocation request
        - ``tool_result`` — tool output
        - ``error`` — error condition
        - ``result`` — terminal payload carrying the final response (Zcode emits
          the assistant's final answer here when no intermediate ``message``
          event was produced); the content is surfaced as the final assistant message.
        """
        event_type = event.get("type")
        content = event.get("content", "")
        metadata = event.get("metadata", {})
        is_error = event.get("is_error", False)

        # Truncate content using InputValidator for text-bearing events
        # (including the terminal `result` payload, since `content` may be long).
        if event_type in ("text", "message", "thinking", "result"):
            is_valid, _ = InputValidator.validate_llm_response(content)
            if not is_valid:
                log.warning(
                    "zcode.response.truncated",
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
                    content=f"Zcode Error: {content}",
                    data={"is_error": True, "metadata": metadata},
                    resume_handle=current_handle,
                )
            ]

        if event_type == "result":
            # Terminal event. If no content is present we still emit a marker message
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


__all__ = ["ZcodeCLIRuntime"]
