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
    # Real zcode `--mode` values (from `zcode --help`): build | edit | plan | yolo.
    "acceptEdits": "edit",          # accept edits, non-blocking under --prompt
    "bypassPermissions": "yolo",    # full bypass
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
        """Build the zcode CLI command for headless execution.

        Measured interface (from `zcode --help`, verified against a live run):
        invocation is `node <cli_path> ...` where cli_path resolves to the
        zcode.cjs app-bundle script. Real flags: ``--prompt`` (one-shot),
        ``--json`` (machine-readable summary), ``--cwd``, ``--mode``
        (build|edit|plan|yolo), ``--resume <sessionId>``.

        NOTE: zcode has **no** ``--non-interactive`` and **no**
        ``--approval-mode`` flag (an earlier draft invented them by copying the
        Codex adapter). ``--mode`` is the real permission surface, and
        ``--prompt`` is already non-interactive (no TUI), so nothing else is
        needed to keep the subprocess headless.
        """
        del runtime_handle, reasoning_effort

        mode_flag = _ZCODE_PERMISSION_MODE_TO_FLAG.get(
            self._permission_mode,
            "edit",
        )
        command = [
            "node",
            self._cli_path,
            "--json",
            "--prompt",
            prompt or "",
            "--mode",
            mode_flag,
        ]
        cwd = getattr(self, "_cwd", None)
        if cwd:
            command.extend(["--cwd", str(cwd)])
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

    async def _iter_stream_lines(
        self,
        stream: Any,
        **_kwargs: Any,
    ) -> Any:
        """Yield zcode's full stdout as a single "line".

        Measured behaviour: ``zcode --prompt --json`` emits ONE pretty-printed
        JSON summary object (multi-line), not an NDJSON event stream. The
        inherited pipeline json-parses each yielded line, so we read the whole
        buffer and yield it once — downstream ``_parse_json_event`` then sees
        the complete document.
        """
        data = await stream.read()
        text = data.decode("utf-8", errors="replace").strip() if data else ""
        if text:
            yield text

    def _extract_event_session_id(self, event: dict[str, Any]) -> str | None:
        """Extract the zcode session id for ``--resume``.

        zcode's ``--prompt --json`` summary carries a top-level ``sessionId``
        of the form ``sess_<uuid>`` — exactly what ``--resume`` consumes. Fall
        back to the inherited keys for any future streaming shape.
        """
        sid = event.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
        return super()._extract_event_session_id(event)

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert a zcode ``--prompt --json`` summary into AgentMessage values.

        Measured shape (verified against live runs of
        ``node zcode.cjs --prompt ... --json``): zcode emits a SINGLE
        pretty-printed JSON object — NOT an NDJSON event stream — with
        top-level fields:

        - ``sessionId`` (sess_<uuid>) — captured for ``--resume`` by
          :meth:`_extract_event_session_id`.
        - ``response`` — the assistant's final text answer.
        - ``usage`` / ``eventCount`` / ``projection`` / ``traceId`` / ``turnId``
          — carried as metadata.

        Intermediate tool calls are reflected only in ``eventCount`` and token
        usage; they are not emitted as discrete stdout events. The whole turn
        is therefore surfaced as one terminal assistant message. If a future
        zcode build adds streamed events, handle them here.
        """
        response = event.get("response")
        if not isinstance(response, str) or not response:
            return []

        is_valid, _ = InputValidator.validate_llm_response(response)
        if not is_valid:
            log.warning(
                "zcode.response.truncated",
                original_length=len(response),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            response = response[:MAX_LLM_RESPONSE_LENGTH]

        return [
            AgentMessage(
                type="assistant",
                content=response,
                data={
                    "terminal": True,
                    "traceId": event.get("traceId"),
                    "turnId": event.get("turnId"),
                    "usage": event.get("usage"),
                    "projection": event.get("projection"),
                    "eventCount": event.get("eventCount"),
                },
                resume_handle=current_handle,
            )
        ]


__all__ = ["ZcodeCLIRuntime"]
