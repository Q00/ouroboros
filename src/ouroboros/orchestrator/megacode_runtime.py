"""Megacode harness runtime for Ouroboros orchestrator execution.

This module provides :class:`MegacodeRuntime`, which shells out to the locally
installed megacode **harness** CLI (the ``harness`` binary) to execute agentic
tasks. The harness owns authentication and model choice (``harness login`` for
hosted ``mega:*`` models, or provider keys), so Ouroboros only forwards an
explicit ``--model``.

Headless contract (``harness -p``, documented in the harness book):

- ``-p`` with no value reads the whole prompt from stdin. Feeding the prompt
  this way avoids ARG_MAX and keeps the child off the parent's stdin, which is
  the MCP transport when Ouroboros runs as a harness MCP server.
- ``--output-format stream-json`` prints one JSON record per line: ``init``,
  ``user``, ``reasoning``, ``assistant``, ``tool_use``, ``tool_result``,
  ``approval``, ``compacted`` and a final ``result``.
- Exit codes: 0 the turn answered (``outcome`` may still be ``tool_failed``),
  1 no answer, 2 a refused flag or prompt.
- ``--mode`` selects a ``[modes.<name>]`` table. Workers run in
  :data:`MEGACODE_WORKER_MODE`, which ``ouroboros setup --runtime megacode``
  writes without the ``mcp`` and ``skill`` tools and with nothing gated, so a
  worker can neither re-enter Ouroboros nor stall on an approval nobody can
  answer.
- A headless run cannot continue a session (``resume -p`` is a documented
  harness limit), so every call is a fresh session and recovery stays at the
  Ouroboros lineage layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import structlog

from ouroboros.config import resolve_megacode_cli_path
from ouroboros.core.security import MAX_LLM_RESPONSE_LENGTH, InputValidator
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ParamSupport,
    ResolvedWorkerCwd,
    RuntimeCapabilities,
    RuntimeHandle,
)
from ouroboros.orchestrator.codex_cli_runtime import (
    CodexCliRuntime,
    SkillDispatchHandler,
    _CodexItemCorrelationScope,
    _CodexToolCall,
)
from ouroboros.runtime.child_env import DEFAULT_OUROBOROS_STRIP_KEYS, build_child_env

log = structlog.get_logger(__name__)

#: ``[modes.<name>]`` table the worker runs in (written by setup).
MEGACODE_WORKER_MODE = "ouroboros-worker"

# Ouroboros permission vocabulary. The harness has no per-call approval flag
# that fits a headless worker; the worker mode already gates nothing, so the
# permission level only decides the sandbox: ``bypassPermissions`` turns it off
# (network and writes outside the project), anything else keeps the harness's
# configured sandbox.
_MEGACODE_PERMISSION_MODES = frozenset({"acceptEdits", "bypassPermissions"})
_MEGACODE_DEFAULT_PERMISSION_MODE = "acceptEdits"
# The orchestrator-wide CLI-owned model placeholder; never forwarded.
_SENTINEL_MODEL = "default"

#: Maximum Ouroboros nesting depth to prevent fork bombs.
_MAX_OUROBOROS_DEPTH = 5
_CHILD_ENV_STRIP_KEYS = DEFAULT_OUROBOROS_STRIP_KEYS

# Harness built-in tool names mapped onto the shared Ouroboros tool vocabulary.
_TOOL_NAME_MAP = {
    "bash": "Bash",
    "exec": "Bash",
    "write_file": "Write",
    "edit_file": "Edit",
    "read_file": "Read",
    "glob": "Glob",
    "search": "Grep",
}
_PATH_TOOLS = frozenset({"Write", "Edit", "Read"})


class MegacodeRuntime(CodexCliRuntime):
    """Agent runtime that shells out to the megacode harness CLI.

    Extends :class:`~ouroboros.orchestrator.codex_cli_runtime.CodexCliRuntime`
    with the ``harness -p`` process model:

    - prompt on stdin, ``--output-format stream-json`` records on stdout
    - ``tool_use`` / ``tool_result`` projected as correlated tool messages
    - ``--mode ouroboros-worker`` for an ungated, Ouroboros-free worker
    - no session resumption (headless resume is unsupported by the harness)
    """

    _runtime_handle_backend = "megacode"
    _runtime_backend = "megacode"
    _requires_memory_gate = False
    _provider_name = "megacode_cli"
    _runtime_error_type = "MegacodeCliError"
    _log_namespace = "megacode_runtime"
    _display_name = "Megacode harness"
    _default_cli_name = "harness"
    _default_llm_backend = "megacode"
    _tempfile_prefix = "ouroboros-megacode-"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 0  # headless `resume -p` is not supported by the harness

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | ResolvedWorkerCwd | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
    ) -> None:
        """Initialize the megacode harness runtime.

        Args:
            cli_path: Optional path to the ``harness`` binary.
            permission_mode: Ouroboros permission level. ``bypassPermissions``
                runs the worker with ``--sandbox off``; ``acceptEdits``,
                ``"default"`` and ``None`` keep the harness's configured sandbox.
            model: Optional harness model id such as ``mega:full`` (sentinel
                ``"default"`` is not forwarded).
            cwd: Optional working directory for the subprocess.
            skills_dir: Optional directory for skill definitions.
            skill_dispatcher: Optional handler for skill execution.
            llm_backend: Optional LLM backend identifier.
            startup_output_timeout_seconds: Optional startup watchdog override.
            stdout_idle_timeout_seconds: Optional idle watchdog override.
        """
        super().__init__(
            cli_path=cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=cwd,
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
            llm_backend=llm_backend,
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
        )

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Apply the shared executable boundary, including direct construction."""
        resolved = resolve_megacode_cli_path(cli_path)
        if resolved is None:
            raise FileNotFoundError(
                "No supported Megacode executable found; use an absolute path or a bare name on an absolute PATH entry"
            )
        return resolved

    # -- Permission mode overrides -----------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the permission mode.

        ``None`` and the interactive ``"default"`` both resolve to
        ``acceptEdits``: a headless harness turn cannot ask anyone. Anything
        outside the vocabulary raises ``ValueError`` so a typo on a permission
        boundary cannot escalate the runtime.
        """
        if permission_mode is None:
            return _MEGACODE_DEFAULT_PERMISSION_MODE
        candidate = permission_mode.strip()
        if candidate in _MEGACODE_PERMISSION_MODES:
            return candidate
        if candidate == "default":
            log.warning(
                "megacode_runtime.permission_mode_coerced",
                requested="default",
                resolved=_MEGACODE_DEFAULT_PERMISSION_MODE,
                reason=(
                    "The megacode runtime is headless (harness -p); the interactive "
                    "'default' approval mode cannot be served, so it is normalized "
                    "to the non-blocking equivalent."
                ),
            )
            return _MEGACODE_DEFAULT_PERMISSION_MODE
        msg = (
            f"Unsupported megacode permission mode: {permission_mode!r} "
            f"(expected one of {sorted(_MEGACODE_PERMISSION_MODES)})"
        )
        raise ValueError(msg)

    def _build_permission_args(self) -> list[str]:
        """Return empty list — the sandbox flag is added in :meth:`_build_command`."""
        return []

    # -- Environment and security ------------------------------------------

    def _build_child_env(self) -> dict[str, str]:
        """Build child env with the recursion guard."""
        return build_child_env(
            strip_keys=_CHILD_ENV_STRIP_KEYS,
            max_depth=_MAX_OUROBOROS_DEPTH,
            depth_error_factory=lambda _depth, max_depth: RuntimeError(
                f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
            ),
        )

    # -- CLI path resolution -----------------------------------------------

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available."""
        from ouroboros.config import get_megacode_cli_path

        return get_megacode_cli_path()

    # -- Command construction ----------------------------------------------

    def _build_command(
        self,
        output_last_message_path: str,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        runtime_handle: RuntimeHandle | None = None,
        # Accepted to honor the shared CodexCliRuntime contract. The harness's
        # ``--thinking`` level is not routed in v1 (capabilities declares
        # reasoning_effort_support=IGNORED, so it is surfaced as advised).
        reasoning_effort: str | None = None,
        model: str | None = None,
    ) -> list[str]:
        """Build the ``harness -p`` command; the prompt is fed via stdin."""
        del output_last_message_path, resume_session_id, prompt, runtime_handle
        del reasoning_effort

        command = [
            self._cli_path,
            "-p",
            "--output-format",
            "stream-json",
            "--mode",
            MEGACODE_WORKER_MODE,
        ]
        if self._permission_mode == "bypassPermissions":
            command.extend(["--sandbox", "off"])
        normalized_model = self._normalize_model(model or self._model)
        if normalized_model and normalized_model != _SENTINEL_MODEL:
            command.extend(["--model", normalized_model])
        return command

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return True — ``harness -p`` with no value reads the prompt from stdin."""
        return True

    def _requires_process_stdin(self) -> bool:
        """Return True — the child must get its own stdin pipe, never the parent's."""
        return True

    @property
    def capabilities(self) -> RuntimeCapabilities:
        """Declare the megacode harness runtime feature contract.

        The harness emits structured ``stream-json`` records and can use the
        shared skill dispatcher. Targeted resume is unavailable because a
        headless harness run cannot continue a session.
        """
        return RuntimeCapabilities(
            skill_dispatch=True,
            targeted_resume=False,
            structured_output=True,
            system_prompt_support=ParamSupport.TRANSLATED,
            tool_restriction_support=ParamSupport.TRANSLATED,
            empty_tool_restriction_support=ParamSupport.IGNORED,
            reasoning_effort_support=ParamSupport.IGNORED,
        )

    # -- Event parsing and normalization -----------------------------------

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
        *,
        item_scope: _CodexItemCorrelationScope | None = None,
    ) -> list[AgentMessage]:
        """Convert one harness ``stream-json`` record into ``AgentMessage`` values."""
        del item_scope
        record_type = event.get("type")

        if record_type == "init":
            session_id = event.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                return []
            return [
                AgentMessage(
                    type="system",
                    content=f"Session initialized: {session_id}",
                    data={
                        "subtype": "init",
                        "session_id": session_id,
                        "model": event.get("model"),
                        "mode": event.get("mode"),
                    },
                    resume_handle=current_handle,
                )
            ]

        if record_type in ("assistant", "reasoning"):
            content = self._bounded_text(event.get("text"), record_type=record_type)
            if not content:
                return []
            data = {"thinking": content} if record_type == "reasoning" else {}
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    data=data,
                    resume_handle=current_handle,
                )
            ]

        if record_type == "tool_use":
            call = self._tool_call(event)
            if call is None:
                return []
            return [self._build_tool_start_message(call, current_handle)]

        if record_type == "tool_result":
            call = self._tool_call(event)
            if call is None:
                return []
            metadata, is_error = self._tool_result_metadata(event)
            return [
                self._build_tool_result_message(
                    call,
                    metadata=metadata,
                    is_error=is_error,
                    handle=current_handle,
                )
            ]

        if record_type == "approval":
            decision = event.get("decision")
            if decision in ("allowed_once", "allowed_for_turn", "allowed_for_session"):
                return []
            return [
                AgentMessage(
                    type="system",
                    content=(
                        f"Megacode harness refused {event.get('tool')}: "
                        f"{event.get('reason') or decision}"
                    ),
                    data={"subtype": "approval", "decision": decision, "is_error": True},
                    resume_handle=current_handle,
                )
            ]

        if record_type == "result":
            # The final answer is the model's last response text; an errored
            # turn carries its reason in ``error`` and exits non-zero, so the
            # base runtime turns this content into the failed result message.
            if event.get("is_error"):
                content = str(event.get("error") or f"outcome: {event.get('outcome')}")
            else:
                content = self._bounded_text(event.get("result"), record_type="result")
            if not content:
                return []
            return [
                AgentMessage(
                    type="assistant",
                    content=content,
                    data={
                        "terminal": True,
                        "outcome": event.get("outcome"),
                        "usage": event.get("usage"),
                        "denied": event.get("denied"),
                        "is_error": bool(event.get("is_error")),
                    },
                    resume_handle=current_handle,
                )
            ]

        return []

    @staticmethod
    def _bounded_text(value: object, *, record_type: str) -> str:
        if not isinstance(value, str) or not value:
            return ""
        is_valid, _ = InputValidator.validate_llm_response(value)
        if not is_valid:
            log.warning(
                "megacode.response.truncated",
                record_type=record_type,
                original_length=len(value),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            return value[:MAX_LLM_RESPONSE_LENGTH]
        return value

    @staticmethod
    def _tool_call(event: dict[str, Any]) -> _CodexToolCall | None:
        """Normalize a ``tool_use``/``tool_result`` pair onto one tool call."""
        raw_tool = event.get("tool")
        if not isinstance(raw_tool, str) or not raw_tool:
            return None
        tool_name = _TOOL_NAME_MAP.get(raw_tool, raw_tool)
        raw_input = event.get("input")
        tool_input = dict(raw_input) if isinstance(raw_input, dict) else {}
        if tool_name in _PATH_TOOLS and "path" in tool_input:
            tool_input.setdefault("file_path", tool_input["path"])
        program = tool_input.get("program")
        if raw_tool == "exec" and isinstance(program, str) and program:
            args = tool_input.get("args")
            argv = [program, *(str(arg) for arg in args)] if isinstance(args, list) else [program]
            tool_input.setdefault("command", " ".join(argv))
        call_id = event.get("id")
        detail = tool_input.get("command") or tool_input.get("file_path") or ""
        return _CodexToolCall(
            tool_name=tool_name,
            start_content=f"Calling tool: {tool_name}" + (f": {detail}" if detail else ""),
            tool_call_id=call_id if isinstance(call_id, str) and call_id else None,
            tool_input=tool_input,
        )

    @staticmethod
    def _tool_result_metadata(event: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Return result metadata and the failure verdict for a ``tool_result``.

        ``status`` is ``failed`` only when the harness could not run the call;
        a command that ran and exited non-zero is ``ok`` with ``output.code``,
        so a non-zero ``code`` is a failure too.
        """
        failed = event.get("status") == "failed"
        output = event.get("output")
        metadata: dict[str, Any] = {}
        if failed:
            metadata["stderr"] = str(event.get("error") or "tool call failed")
            return metadata, True
        if isinstance(output, dict) and "code" in output:
            code = output.get("code")
            stdout = output.get("stdout")
            stderr = output.get("stderr")
            if isinstance(stdout, str):
                metadata["stdout"] = stdout
            if isinstance(stderr, str):
                metadata["stderr"] = stderr
            if isinstance(code, int) and not isinstance(code, bool):
                metadata["exit_code"] = code
                return metadata, code != 0
            return metadata, True
        if isinstance(output, str):
            metadata["output"] = output
        elif output is not None:
            metadata["output"] = json.dumps(output, ensure_ascii=False, sort_keys=True)
        return metadata, False

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
        """Return None — headless harness runs cannot resume a session."""
        del (
            attempted_resume_session_id,
            current_handle,
            returncode,
            final_message,
            stderr_lines,
        )
        return None


__all__ = ["MEGACODE_WORKER_MODE", "MegacodeRuntime"]
