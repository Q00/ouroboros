"""Antigravity CLI runtime for Ouroboros orchestrator execution.

Antigravity is Google's successor to the Gemini CLI: on 2026-06-18 the Gemini
CLI stops serving the AI Pro / Ultra / free consumer tiers and migrates those
users to the Antigravity CLI (the ``agy`` binary). This runtime shells out to
the locally installed ``agy`` to execute agentic tasks, reusing the Gemini
runtime's text-stream handling.

The ``agy`` headless contract differs from the Gemini CLI:

- ``agy -p <prompt> --output-format stream-json`` emits nested native
  events, including tool calls and terminal response text. The Antigravity
  normalizer preserves tool names, inputs, errors, and conversation identifiers.
  Plain-text fallback remains available for wrapped output.
- Permissions are all-or-nothing: ``--dangerously-skip-permissions``
  auto-approves every tool request. ``agy`` has no granular ``auto_edit``
  equivalent, so the non-blocking Ouroboros modes (``acceptEdits`` /
  ``bypassPermissions``) both map to that single skip flag — a headless
  subprocess must never surface an interactive approval prompt or it would
  wedge indefinitely.
- ``--model`` selects the session model. ``agy`` owns its own model catalog
  (``agy models``), so Antigravity is a sentinel-model backend: the orchestrator
  defers to the CLI's configured default and only forwards ``--model`` when an
  explicit, non-sentinel id is configured.

Usage:
    runtime = AntigravityCLIRuntime(cwd="/path/to/project")
    async for message in runtime.execute_task("Fix the bug in auth.py"):
        print(message.content)

Custom CLI Path:
    Set via constructor parameter or environment variable:
        runtime = AntigravityCLIRuntime(cli_path="/path/to/agy")
        # or
        export OUROBOROS_ANTIGRAVITY_CLI_PATH=/path/to/agy
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import structlog

from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime
from ouroboros.providers.antigravity_event_normalizer import (
    AntigravityEventNormalizer,
    is_legacy_gemini_event,
    is_recognized_gemini_event_type,
)

log = structlog.get_logger(__name__)

# ``agy`` exposes ``--dangerously-skip-permissions`` (auto-approve every tool
# request) but no granular accept-edits-only mode. Map the non-blocking
# Ouroboros permission vocabulary onto that single flag; ``"default"``
# (interactive) is normalized to ``acceptEdits`` because the headless runtime
# (``agy -p``) cannot honour an approval prompt without deadlocking.
_AGY_PERMISSION_MODES = frozenset({"acceptEdits", "bypassPermissions"})
_AGY_DEFAULT_PERMISSION_MODE = "acceptEdits"
# The sentinel ``"default"`` is the orchestrator-wide model placeholder for
# CLI-owned model selection; ``agy`` does not understand it as a ``--model``
# value, so it is never forwarded on the command line.
_SENTINEL_MODEL = "default"


class AntigravityCLIRuntime(GeminiCLIRuntime):
    """Agent runtime that shells out to the locally installed Antigravity CLI.

    Extends :class:`~ouroboros.orchestrator.gemini_cli_runtime.GeminiCLIRuntime`
    (Antigravity is Google's successor to the Gemini CLI) with the ``agy``
    process model:

    - ``agy -p`` headless invocation with ``--dangerously-skip-permissions``
    - Structured stdout normalized by :class:`AntigravityEventNormalizer`
    - Targeted native conversation resumption with ``--conversation``
    """

    _runtime_handle_backend = "antigravity_cli"
    _runtime_backend = "antigravity"
    _provider_name = "antigravity_cli"
    _runtime_error_type = "AntigravityCliError"
    _log_namespace = "antigravity_cli_runtime"
    _display_name = "Antigravity CLI"
    _default_cli_name = "agy"
    # Antigravity is runtime-only (no LLM-completion adapter); fall back to the
    # Claude completion backend for any auxiliary completion the base runtime
    # requests, matching the Hermes runtime's convention.
    _default_llm_backend = "claude_code"
    _tempfile_prefix = "ouroboros-antigravity-"
    _max_resume_retries = 0  # Never retry a failed resume as a fresh conversation.

    def _extract_event_session_id(self, event: dict[str, Any]) -> str | None:
        """Trust canonical native identity; preserve legacy Gemini extraction."""
        if not is_recognized_gemini_event_type(event.get("type")):
            return None
        raw = event.get("raw")
        if not isinstance(raw, dict) or is_legacy_gemini_event(raw):
            return super()._extract_event_session_id(event)
        if event.get("type") not in {"init", "message", "tool_use", "tool_result", "result"}:
            return None
        metadata = event.get("metadata")
        session_id = metadata.get("session_id") if isinstance(metadata, dict) else None
        return session_id.strip() if isinstance(session_id, str) and session_id.strip() else None

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        if not line.strip():
            return None
        event = AntigravityEventNormalizer().normalize_line(line)
        if event.get("type") == "text":
            log.warning(
                "antigravity_cli_runtime.unstructured_output",
                limitation="Tool usage is unknown for plain-text output; zero observed calls is not evidence of no tool use.",
            )
        return event

    def _convert_event(
        self, event: dict[str, Any], current_handle: RuntimeHandle | None, *, item_scope: Any = None
    ) -> list[AgentMessage]:
        messages = super()._convert_event(event, current_handle, item_scope=item_scope)
        metadata = event.get("metadata", {})
        if event.get("type") == "result" and event.get("is_error"):
            return [
                AgentMessage(
                    type="result",
                    content=event.get("content", ""),
                    data={"subtype": "error", "terminal": True, "metadata": metadata},
                    resume_handle=current_handle,
                )
            ]
        if metadata.get("text_delta"):
            messages = [replace(m, data={**m.data, "text_delta": True}) for m in messages]
        return messages

    # -- Permission mode overrides -----------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the Antigravity CLI permission mode.

        ``None`` and the orchestrator-wide ``"default"`` setting both resolve
        to :data:`_AGY_DEFAULT_PERMISSION_MODE` (``acceptEdits``). Both
        recognized non-blocking modes (``acceptEdits`` and
        ``bypassPermissions``) map to ``--dangerously-skip-permissions`` in
        :meth:`_build_command` because ``agy`` has no granular auto-edit mode
        and the headless ``agy -p`` invocation must never block on an approval
        prompt. Anything else raises ``ValueError`` rather than silently
        falling back, so a typo on a permission boundary cannot escalate the
        runtime.
        """
        if permission_mode is None:
            return _AGY_DEFAULT_PERMISSION_MODE
        candidate = permission_mode.strip()
        if candidate in _AGY_PERMISSION_MODES:
            return candidate
        if candidate == "default":
            log.warning(
                "antigravity_cli_runtime.permission_mode_coerced",
                requested="default",
                resolved=_AGY_DEFAULT_PERMISSION_MODE,
                reason=(
                    "Antigravity runtime is headless (agy -p); the interactive "
                    "'default' approval mode would block, so it is normalized to "
                    "the safe non-blocking equivalent."
                ),
            )
            return _AGY_DEFAULT_PERMISSION_MODE
        msg = (
            f"Unsupported Antigravity permission mode: {permission_mode!r} "
            f"(expected one of {sorted(_AGY_PERMISSION_MODES)})"
        )
        raise ValueError(msg)

    # -- Command construction ----------------------------------------------

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        runtime_handle: RuntimeHandle | None = None,
        # Accepted to honor the shared CodexCliRuntime contract, but ignored:
        # `agy` exposes no per-invocation effort flag (capabilities declares
        # reasoning_effort_support=IGNORED, so it is surfaced as advised).
        reasoning_effort: str | None = None,
    ) -> list[str]:
        """Build the Antigravity CLI command for non-interactive execution.

        Headless contract:
        - ``-p`` (alias ``--print`` / ``--prompt``) carries the request and
          prints the response to stdout.
        - ``--dangerously-skip-permissions`` auto-approves tool requests so the
          subprocess never blocks. Both ``acceptEdits`` and
          ``bypassPermissions`` resolve here — ``agy`` has no narrower mode.
        - ``--model`` is forwarded only for an explicit, non-sentinel id;
          ``agy`` otherwise uses its own configured default.
        """
        del output_last_message_path, reasoning_effort

        command = [
            self._cli_path,
            "-p",
            prompt or "",
            "--dangerously-skip-permissions",
            "--output-format",
            "stream-json",
        ]
        session_id = resume_session_id or self._resolve_resume_session_id(runtime_handle)
        if session_id:
            command.extend(["--conversation", session_id])
        normalized_model = self._normalize_model(self._model)
        if normalized_model and normalized_model != _SENTINEL_MODEL:
            command.extend(["--model", normalized_model])
        return command

    # -- CLI path resolution -----------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available.

        Reads from :func:`ouroboros.config.get_antigravity_cli_path`, which
        checks ``OUROBOROS_ANTIGRAVITY_CLI_PATH`` and persisted
        ``orchestrator.antigravity_cli_path``.
        """
        from ouroboros.config import get_antigravity_cli_path

        return get_antigravity_cli_path()

    # -- Final-message accumulation ----------------------------------------

    def _update_last_content(self, last_content: str, message: AgentMessage) -> str:
        """Reconstruct assistant deltas or legacy text, excluding tool output."""
        if message.tool_name or message.type != "assistant" or message.data.get("thinking"):
            return last_content
        if message.data.get("terminal"):
            return message.content
        if message.data.get("text_delta"):
            return last_content + message.content
        if not message.content:
            return last_content
        return f"{last_content}\n{message.content}" if last_content else message.content

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Declare the Antigravity CLI runtime feature contract.

        ``agy -p --output-format stream-json`` exposes structured tool events.
        Targeted resume uses the native ``--conversation`` option. Unsupported
        CLI versions fail explicitly and are never retried as fresh sessions.
        Tool restrictions and the system prompt are composed into the user
        message by the inherited prompt builder rather than enforced natively.
        """
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=True,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
            tool_restriction_support=ParamSupport.TRANSLATED,
            empty_tool_restriction_support=ParamSupport.IGNORED,
        )


__all__ = ["AntigravityCLIRuntime"]
