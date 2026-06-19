"""Antigravity CLI runtime for Ouroboros orchestrator execution.

Antigravity is Google's successor to the Gemini CLI: on 2026-06-18 the Gemini
CLI stops serving the AI Pro / Ultra / free consumer tiers and migrates those
users to the Antigravity CLI (the ``agy`` binary). This runtime shells out to
the locally installed ``agy`` to execute agentic tasks, reusing the Gemini
runtime's text-stream handling.

The ``agy`` headless contract differs from the Gemini CLI:

- ``agy -p <prompt>`` runs a single prompt non-interactively and prints the
  response to stdout. There is **no** ``--output-format`` flag, so ``agy``
  emits plain text rather than ``stream-json`` events. The inherited
  :class:`~ouroboros.providers.gemini_event_normalizer.GeminiEventNormalizer`
  treats each plain line as a ``text`` event, so the assistant response is
  surfaced as normal assistant messages.
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

import structlog

from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.gemini_cli_runtime import GeminiCLIRuntime

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
    - Plain-text stdout (no ``stream-json``) normalized via the inherited
      :class:`GeminiEventNormalizer`
    - No session resumption (stateless execution model)
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
    _max_resume_retries = 0  # agy print mode does not support targeted resume

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
        del output_last_message_path, resume_session_id, runtime_handle, reasoning_effort

        command = [
            self._cli_path,
            "-p",
            prompt or "",
            "--dangerously-skip-permissions",
        ]
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
        """Accumulate plain-text lines into the final message.

        ``agy -p`` prints the full response as plain-text lines (no token
        deltas, no terminal JSON payload), so each stdout line is surfaced as a
        separate assistant message. The base "keep the latest message" fallback
        would therefore truncate a multi-line answer to its last line. Join the
        non-empty assistant lines with newlines so the orchestrator's
        ``final_message`` reconstructs the complete response.
        """
        if not message.content:
            return last_content
        return f"{last_content}\n{message.content}" if last_content else message.content

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Declare the Antigravity CLI runtime feature contract.

        ``agy -p`` prints plain text (no ``stream-json``), so structured event
        output is unavailable and the native CLI exposes no targeted session
        resume; recovery happens at the Ouroboros checkpoint/lineage layer.
        Tool restrictions and the system prompt are composed into the user
        message by the inherited prompt builder rather than enforced natively.
        """
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=False,
            structured_output=False,
            system_prompt_support=ParamSupport.TRANSLATED,
            tool_restriction_support=ParamSupport.TRANSLATED,
        )


__all__ = ["AntigravityCLIRuntime"]
