"""Zcode CLI runtime for Ouroboros orchestrator execution.

This module provides the ZcodeCLIRuntime that shells out to the locally
installed ``zcode`` CLI to execute agentic tasks.

Usage:
    runtime = ZcodeCLIRuntime(cwd="/path/to/project")
    async for message in runtime.execute_task("Fix the bug in auth.py"):
        print(message.content)

Custom CLI Path:
    Set via constructor parameter or environment variable:
        runtime = ZcodeCLIRuntime(cli_path="/path/to/zcode")
        # or
        export OUROBOROS_ZCODE_CLI_PATH=/path/to/zcode

Model selection:
    zcode has **no** ``--model`` CLI flag (verified against ``zcode --help``
    on 0.14.5 and 0.15.0 — passing ``--model`` is a hard ``Unknown option``
    rejection, not a silent no-op). Model selection is done **outside** the
    runtime, via the zcode config file ``~/.zcode/cli/config.json`` under
    ``model.main``, or the interactive ``/model`` slash command. Any ``model``
    value passed to the constructor is therefore intentionally ignored at the
    CLI boundary — a warning is emitted when a non-default model is requested
    so the misconfiguration (expected model vs. configured model) is visible.
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

# Zcode CLI permission mode mapping. Zcode exposes its permission surface via
# the ``--mode`` flag (build | edit | plan | yolo) — there is no
# ``--approval-mode`` and no ``--non-interactive`` flag (an earlier draft
# invented both by copying the Codex adapter). ``--prompt`` is already a
# non-interactive one-shot invocation (no TUI, no approval prompt), so the
# only permission knob to map is ``--mode``.
#
# Ouroboros' ``"default"`` mode has no zcode-native ``--mode`` equivalent
# (zcode's vocabulary is build/edit/plan/yolo), so callers that pass
# ``"default"`` are normalized at ``_resolve_permission_mode`` to the safe
# default (``acceptEdits`` → ``edit``). Anything outside the recognized
# vocabulary is rejected rather than silently escalating.
_ZCODE_PERMISSION_MODE_TO_FLAG = {
    # Real zcode `--mode` values (from `zcode --help`): build | edit | plan | yolo.
    "acceptEdits": "edit",  # accept edits
    "bypassPermissions": "yolo",  # full bypass
}
_ZCODE_PERMISSION_MODES = frozenset(_ZCODE_PERMISSION_MODE_TO_FLAG)
# Match the orchestrator-wide ``acceptEdits`` default. Operators must opt in
# to ``bypassPermissions`` explicitly — the runtime never silently jumps to
# ``yolo`` when ``permission_mode`` is omitted.
_ZCODE_DEFAULT_PERMISSION_MODE = "acceptEdits"

#: Maximum Ouroboros nesting depth to prevent fork bombs
_MAX_OUROBOROS_DEPTH = 5
# Child-env strip set for Zcode. Zcode does NOT strip CLAUDECODE (unlike
# codex/copilot/kiro) — preserve that divergence; only the Ouroboros markers
# are removed.
_CHILD_ENV_STRIP_KEYS = (
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_RUNTIME",
)


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
    # zcode ``--prompt --json`` buffers its whole summary until completion and
    # stays silent until then — unlike Codex, which streams events continuously
    # and resets the inherited "first chunk" watchdog on every chunk. The
    # parent default of 60s would therefore cap the ENTIRE task at 60s on any
    # caller that doesn't explicitly disable the guard (e.g. a direct
    # ``create_agent_runtime(backend="zcode")``), killing healthy long runs as
    # "produced no stdout". Disable it at the class level — callers that want
    # a cap can still pass an explicit ``startup_output_timeout_seconds`` (the
    # execute-seed path already passes ``0`` for the same reason).
    _startup_output_timeout_seconds = None

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
    ) -> None:
        """Initialize the Zcode CLI runtime.

        Args:
            cli_path: Optional path to the zcode CLI entry script
                (``zcode.cjs``). The runtime invokes it as
                ``node <cli_path>``, so this points at the app-bundle
                script rather than a bare binary.
            permission_mode: Ouroboros permission level. Recognized
                modes map to zcode ``--mode`` values
                (``acceptEdits`` → ``edit``,
                ``bypassPermissions`` → ``yolo``) and pass through.
                ``"default"`` has no zcode-native ``--mode``
                equivalent (zcode's vocabulary is build/edit/plan/yolo),
                so it is normalized to ``acceptEdits`` with an audit
                log instead of failing — that keeps a globally valid
                config working. Falls back to ``acceptEdits`` when
                omitted; operators must opt in to
                ``bypassPermissions`` explicitly.
            model: Optional model identifier. **Ignored at the CLI
                boundary** — zcode has no ``--model`` flag (passing one is
                a hard ``Unknown option`` rejection, verified on 0.14.5 and
                0.15.0). Set the model via ``~/.zcode/cli/config.json``
                (``model.main``). A non-default value here emits a warning
                so the divergence between the requested model and the
                model zcode actually uses is visible; the value is never
                forwarded to the subprocess.
            cwd: Optional working directory for the subprocess.
            skills_dir: Optional directory for skill definitions.
            skill_dispatcher: Optional handler for skill execution.
            llm_backend: Optional LLM backend identifier.
            startup_output_timeout_seconds: Override the watchdog that
                aborts a subprocess which emits no first stdout chunk
                within the deadline. Passed straight through to the
                Codex base runtime; ``0`` or negative disables the guard.
                The MCP execute-seed path sets this to ``0`` to keep
                long agent runs alive — Zcode buffers its whole JSON
                summary until completion and would otherwise be killed
                as "produced no stdout" before the summary lands.
            stdout_idle_timeout_seconds: Override the inter-chunk idle
                watchdog. Same forwarding / disable contract as above.
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
        # zcode has no --model flag, so a non-default model requested here
        # cannot reach the CLI. Surface it loudly rather than silently
        # dropping it — the caller believes a specific model was selected
        # when zcode will in fact use whatever ~/.zcode/cli/config.json
        # declares. Only an explicit, non-default id triggers this.
        requested_model = self._normalize_model(self._model)
        if requested_model:
            log.warning(
                "zcode_cli_runtime.model_not_forwarded",
                requested_model=self._model,
                reason=(
                    "zcode has no --model CLI flag; set model.main in "
                    "~/.zcode/cli/config.json to select the model."
                ),
            )

    # -- Permission mode overrides -----------------------------------------

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the Zcode CLI permission mode.

        ``None`` and the orchestrator-wide ``"default"`` setting both
        resolve to :data:`_ZCODE_DEFAULT_PERMISSION_MODE`
        (``acceptEdits`` → zcode ``--mode edit``).
        ``config.orchestrator.permission_mode`` accepts ``"default"`` as
        a valid global setting, but zcode's ``--mode`` vocabulary is
        build/edit/plan/yolo — there is no ``default`` value to pass
        through — so the backend-specific contract narrows it at the
        boundary rather than turning a previously valid config into a
        hard error.

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
                    "Zcode --mode has no 'default' value (vocabulary is "
                    "build/edit/plan/yolo); normalized to the safe default."
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

        Measured interface (from `zcode --help`, verified against a live run).
        Real flags: ``--prompt`` (one-shot), ``--json`` (machine-readable
        summary), ``--cwd``, ``--mode`` (build|edit|plan|yolo),
        ``--resume <sessionId>``.

        Two install shapes must both work:

        - **App-bundle script** — ``zcode.cjs`` under
          ``/Applications/ZCode.app/...``. Invoked as ``node <cli_path> …``.
        - **PATH executable** — a ``zcode`` wrapper/binary resolved when no
          explicit path is configured. Must be called **directly**:
          ``node <executable>`` would parse the binary as JS and fail before
          zcode ever runs.

        The builder distinguishes by extension: ``.cjs``/``.js``/``.mjs``
        scripts get the ``node`` prefix; everything else is invoked straight.

        NOTE: zcode has **no** ``--non-interactive``, **no**
        ``--approval-mode``, and **no** ``--model`` flag. The first two were
        invented by an earlier draft copying the Codex adapter; ``--model``
        was the last un-measured artifact copied from the same source —
        verified absent on zcode 0.14.5 and 0.15.0, where ``--model`` is a
        hard ``Unknown option`` rejection that aborts the run before zcode
        does any work. ``--mode`` is the real permission surface, ``--prompt``
        is already non-interactive (no TUI), and model selection lives in
        ``~/.zcode/cli/config.json`` (``model.main``), never on the CLI. Do
        not re-add ``--model`` here — :meth:`ZcodeCLIRuntime.__init__` warns
        when a non-default model is requested so the gap is visible.
        """
        del runtime_handle, reasoning_effort

        mode_flag = _ZCODE_PERMISSION_MODE_TO_FLAG.get(
            self._permission_mode,
            "edit",
        )
        cli_path = str(self._cli_path) if self._cli_path else None
        if cli_path is None:
            msg = "zcode CLI path could not be resolved (set OUROBOROS_ZCODE_CLI_PATH or orchestrator.zcode_cli_path)"
            raise RuntimeError(msg)
        # node script (app bundle) → `node <script>`; PATH executable → direct.
        prefix: list[str] = (
            ["node", cli_path] if cli_path.endswith((".cjs", ".js", ".mjs")) else [cli_path]
        )
        command = prefix + [
            "--json",
            "--prompt",
            prompt or "",
            "--mode",
            mode_flag,
        ]
        cwd = getattr(self, "_cwd", None)
        if cwd:
            command.extend(["--cwd", str(cwd)])
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
        *,
        chunk_size: int = 16384,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
        **_kwargs: Any,
    ) -> Any:
        """Yield zcode's full stdout as a single reassembled "line".

        Measured behaviour: ``zcode --prompt --json`` emits ONE pretty-printed
        JSON summary object (multi-line), not an NDJSON event stream. The
        inherited pipeline json-parses each yielded line, so we must hand it
        the complete document in one piece.

        Rather than ``await stream.read()`` to EOF (which silently drops the
        parent's ``first_chunk_timeout_seconds`` / ``chunk_timeout_seconds``
        watchdogs and can wedge the orchestrator on a zcode process that stays
        alive but emits nothing — auth prompt, provider stall, model hang), we
        delegate the chunked read to :meth:`CodexCliRuntime._iter_stream_lines`
        so the watchdogs still fire and raise ``TimeoutError``. Every decoded
        line is buffered and then joined into one document and yielded once,
        so downstream ``_parse_json_event`` sees the whole summary object while
        ``execute_task``'s ``except TimeoutError`` recovery path keeps working.
        """
        chunks: list[str] = []
        async for line in super()._iter_stream_lines(
            stream,
            chunk_size=chunk_size,
            first_chunk_timeout_seconds=first_chunk_timeout_seconds,
            chunk_timeout_seconds=chunk_timeout_seconds,
        ):
            if line:
                chunks.append(line)
        text = "\n".join(chunks).strip()
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
