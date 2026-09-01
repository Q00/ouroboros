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
    on 0.14.5, 0.15.0, and 0.15.2 — passing ``--model`` is a hard
    ``Unknown option`` rejection, not a silent no-op). Model selection is done
    **outside** the runtime, via the zcode config file
    ``~/.zcode/cli/config.json`` under ``model.main``, or the interactive
    ``/model`` slash command. Any ``model`` value passed to the constructor is
    therefore intentionally ignored at the CLI boundary — a warning is
    emitted when a non-default model is requested so the misconfiguration
    (expected model vs. configured model) is visible.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any

import structlog

from ouroboros.core.filesystem_capability import (
    nofollow_directory_capabilities_available,
    open_nofollow_directory_chain,
)
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
)
from ouroboros.runtime.child_env import build_child_env
from ouroboros.zcode_cli_launcher import (
    resolve_zcode_command_prefix,
    resolve_zcode_electron_node_path,
)

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
# codex/copilot/kiro) — preserve that divergence. ELECTRON_RUN_AS_NODE is
# rebuilt only for an app-bundle electron-node launch so a parent Electron
# process cannot accidentally change PATH-wrapper or standalone-script
# behavior. ``OUROBOROS_RUNTIME`` is a legacy selector that can route nested
# ouroboros commands back into zcode, so strip it alongside the LLM selector.
# NODE_OPTIONS can preload arbitrary JavaScript before zcode starts, so never
# inherit it into either the bundled Electron or system Node launch.
_CHILD_ENV_STRIP_KEYS = (
    "OUROBOROS_AGENT_RUNTIME",
    "OUROBOROS_LLM_BACKEND",
    "OUROBOROS_RUNTIME",
    "ELECTRON_RUN_AS_NODE",
    "NODE_OPTIONS",
)

_ZCODE_SESSION_ID_RE = re.compile(
    r"sess_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)
_MAX_ZCODE_ROLLOUT_BYTES = 16 * 1024 * 1024
_MAX_ZCODE_TOOL_INPUT_DEPTH = 100


def _zcode_canonical_json_identity(value: Any) -> str | None:
    def encode(item: Any, depth: int) -> object | None:
        if depth > _MAX_ZCODE_TOOL_INPUT_DEPTH:
            return None
        if item is None:
            return ["null", None]
        if isinstance(item, bool):
            return ["bool", item]
        if isinstance(item, str):
            return ["str", item]
        if isinstance(item, int):
            return ["int", item]
        if isinstance(item, float):
            if not math.isfinite(item):
                return None
            return ["float", item]
        if isinstance(item, list):
            normalized_list = []
            for value_item in item:
                normalized_item = encode(value_item, depth + 1)
                if normalized_item is None:
                    return None
                normalized_list.append(normalized_item)
            return ["list", normalized_list]
        if isinstance(item, dict):
            normalized_dict = []
            if not all(isinstance(key, str) for key in item):
                return None
            for key, value_item in sorted(item.items()):
                if not isinstance(key, str):
                    return None
                normalized_item = encode(value_item, depth + 1)
                if normalized_item is None:
                    return None
                normalized_dict.append([key, normalized_item])
            return ["dict", normalized_dict]
        return None

    normalized = encode(value, 0)
    if normalized is None:
        return None
    try:
        return json.dumps(
            normalized,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        return None


def _zcode_tool_input_fingerprint(tool_input: dict[str, Any]) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    return _zcode_canonical_json_identity(tool_input)


def _zcode_message_identity(messages: list[Any]) -> str | None:
    normalized: list[Any] = []
    for message in messages:
        if isinstance(message, dict) and message.get("cacheControl") == {"type": "ephemeral"}:
            normalized.append(
                {key: value for key, value in message.items() if key != "cacheControl"}
            )
        else:
            normalized.append(message)
    return _zcode_canonical_json_identity(normalized)


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
    # Zcode is runtime-only (no LLM-completion adapter). Auxiliary built-in
    # skill handlers still need a completion-capable backend when the runtime
    # is constructed directly, so match the Antigravity/Grok fallback pattern.
    _default_llm_backend = "claude_code"
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
        cwd: str | Path | ResolvedWorkerCwd | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        startup_output_timeout_seconds: float | None = None,
        stdout_idle_timeout_seconds: float | None = None,
    ) -> None:
        """Initialize the Zcode CLI runtime.

        Args:
            cli_path: Optional path to the zcode CLI entry script
                (``zcode.cjs``) or executable. Official app-bundle scripts use
                ZCode's bundled Electron/Node runtime; standalone scripts use
                the system Node, and executable wrappers run directly.
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
                a hard ``Unknown option`` rejection, verified on 0.14.5,
                0.15.0, and 0.15.2). Set the model via
                ``~/.zcode/cli/config.json``
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
        self._requested_model = model
        super().__init__(
            cli_path=cli_path,
            permission_mode=permission_mode,
            model=None,
            cwd=cwd,
            skills_dir=skills_dir,
            skill_dispatcher=skill_dispatcher,
            llm_backend=llm_backend,
            startup_output_timeout_seconds=startup_output_timeout_seconds,
            stdout_idle_timeout_seconds=stdout_idle_timeout_seconds,
        )
        self._electron_node_path = resolve_zcode_electron_node_path(self._cli_path)
        # zcode has no --model flag, so a non-default model requested here
        # cannot reach the CLI. Surface it loudly rather than silently
        # dropping it — the caller believes a specific model was selected
        # when zcode will in fact use whatever ~/.zcode/cli/config.json
        # declares. Only an explicit, non-default id triggers this.
        requested_model = self._normalize_model(self._requested_model)
        if requested_model:
            log.warning(
                "zcode_cli_runtime.model_not_forwarded",
                requested_model=self._requested_model,
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
        env = build_child_env(
            strip_keys=_CHILD_ENV_STRIP_KEYS,
            max_depth=_MAX_OUROBOROS_DEPTH,
            depth_error_factory=lambda _depth, max_depth: RuntimeError(
                f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
            ),
        )
        if self._electron_node_path is not None:
            env["ELECTRON_RUN_AS_NODE"] = "1"
        return env

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
        # The shared AgentRuntime API may carry a routed per-call model. Zcode
        # has no --model flag, so accepting and dropping it is the only truthful
        # behavior; capability degradation is reported by the orchestrator.
        model: str | None = None,
    ) -> list[str]:
        """Build the zcode CLI command for headless execution.

        Measured interface (from `zcode --help`, verified against a live run).
        Real flags: ``--prompt`` (one-shot), ``--json`` (machine-readable
        summary), ``--cwd``, ``--mode`` (build|edit|plan|yolo),
        ``--resume <sessionId>``.

        Two install shapes must both work:

        - **App-bundle script** — ``zcode.cjs`` under
          ``/Applications/ZCode.app/...``. When its bundle metadata declares
          ``electron-node``, invoke the app's bundled Electron/Node runtime
          with ``ELECTRON_RUN_AS_NODE=1`` instead of the system ``node``.
        - **Standalone script** — a ``.cjs``/``.js``/``.mjs`` path outside a
          recognized app bundle. Invoked as ``node <cli_path> …``.
        - **PATH executable** — a ``zcode`` wrapper/binary resolved when no
          explicit path is configured. Must be called **directly**:
          ``node <executable>`` would parse the binary as JS and fail before
          zcode ever runs.

        The builder checks the official app-bundle metadata before falling
        back to the extension rule. Everything else is invoked directly.

        NOTE: zcode has **no** ``--non-interactive``, **no**
        ``--approval-mode``, and **no** ``--model`` flag. The first two were
        invented by an earlier draft copying the Codex adapter; ``--model``
        was the last un-measured artifact copied from the same source —
        verified absent on zcode 0.14.5, 0.15.0, and 0.15.2, where ``--model``
        is a hard ``Unknown option`` rejection that aborts the run before
        zcode does any work. ``--mode`` is the real permission surface,
        ``--prompt`` is already non-interactive (no TUI), and model selection
        lives in ``~/.zcode/cli/config.json`` (``model.main``), never on the
        CLI. Do not re-add ``--model`` here —
        :meth:`ZcodeCLIRuntime.__init__` warns when a non-default model is
        requested so the gap is visible.
        """
        del runtime_handle, reasoning_effort, model

        mode_flag = _ZCODE_PERMISSION_MODE_TO_FLAG.get(
            self._permission_mode,
            "edit",
        )
        cli_path = str(self._cli_path) if self._cli_path else None
        if cli_path is None:
            msg = "zcode CLI path could not be resolved (set OUROBOROS_ZCODE_CLI_PATH or orchestrator.zcode_cli_path)"
            raise RuntimeError(msg)
        prefix = resolve_zcode_command_prefix(cli_path)
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

    def execution_identity_contract(self) -> dict[str, Any]:
        """Return Zcode execution identity without pretending to observe a model.

        Zcode has no ``--model`` flag, and the selected model is owned by
        Zcode's mutable config. A constructor ``model`` value is therefore only
        a rejected request, not an observed effective model. Keep the requested
        value visible for audit, but never let it satisfy the runner's
        ``effective_model_observed`` guard.
        """
        requested_model = self._normalize_model(self._requested_model)
        normalized_llm_backend = (
            self._llm_backend.strip()
            if isinstance(self._llm_backend, str) and self._llm_backend.strip()
            else None
        )
        return {
            "kind": "zcode_cli_v1",
            "requested_model": requested_model,
            "effective_model_observed": False,
            "llm_backend": normalized_llm_backend,
            "resume_handle_selector": self.resume_handle_execution_identity_contract(None),
        }

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
            empty_tool_restriction_support=ParamSupport.IGNORED,
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
        *,
        item_scope: _CodexItemCorrelationScope | None = None,
    ) -> list[AgentMessage]:
        """Convert a zcode ``--prompt --json`` summary into AgentMessage values.

        Measured shape (verified against live app-bundle runs through ZCode's
        bundled Electron/Node runtime): zcode emits a SINGLE pretty-printed
        JSON object — NOT an NDJSON event stream — with top-level fields:

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
        if not isinstance(response, str) or not response.strip():
            return [
                AgentMessage(
                    type="result",
                    content=(
                        "Zcode CLI protocol error: JSON summary did not include "
                        "a non-empty response."
                    ),
                    data={
                        "subtype": "error",
                        "error_type": self._runtime_error_type,
                        "protocol_error": "missing_response",
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

        is_valid, _ = InputValidator.validate_llm_response(response)
        if not is_valid:
            log.warning(
                "zcode.response.truncated",
                original_length=len(response),
                max_length=MAX_LLM_RESPONSE_LENGTH,
            )
            response = response[:MAX_LLM_RESPONSE_LENGTH]

        receipt_messages = self._load_rollout_tool_receipts(event)
        return [
            *receipt_messages,
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
            ),
        ]

    def _read_trusted_rollout_history(self, session_id: str) -> list[dict[str, Any]] | None:
        """Return one immutable, filename-bound ZCode session history.

        Rollout records cross a verification-authority boundary.  Keep every
        file and record provenance check in this primitive so callers cannot
        accidentally select a plausible receipt from an otherwise ambiguous
        history.  ``None`` means the complete file failed trust validation.
        """
        rollout_path = Path.home() / ".zcode" / "cli" / "rollout" / f"model-io-{session_id}.jsonl"
        rollout_name = rollout_path.name
        try:
            # Hold a no-follow descriptor for every parent component and open
            # the leaf relative to that capability.  A lexical parent can be
            # replaced at any point after this call; the read remains bound to
            # the already-opened directory inode rather than re-resolving the
            # pathname (closing the parent-replacement TOCTOU).
            if not nofollow_directory_capabilities_available():
                return None
            parent_chain = open_nofollow_directory_chain(rollout_path.parent)
            flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0)
            )
            fd: int | None = None
            try:
                fd = os.open(rollout_name, flags, dir_fd=parent_chain.leaf_fd)
                fd_stat = os.fstat(fd)
                getuid = getattr(os, "getuid", None)
                current_uid = getuid() if callable(getuid) else None
                if (
                    not stat.S_ISREG(fd_stat.st_mode)
                    or (current_uid is not None and fd_stat.st_uid != current_uid)
                    or fd_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                    or fd_stat.st_size > _MAX_ZCODE_ROLLOUT_BYTES
                ):
                    return None
                chunks: list[bytes] = []
                remaining = _MAX_ZCODE_ROLLOUT_BYTES + 1
                while remaining:
                    chunk = os.read(fd, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                payload = b"".join(chunks)
                if len(payload) > _MAX_ZCODE_ROLLOUT_BYTES:
                    return None
                text = payload.decode("utf-8")
            finally:
                if fd is not None:
                    os.close(fd)
                parent_chain.close()
        except (OSError, UnicodeError):
            return None

        records: list[dict[str, Any]] = []
        previous_messages: list[Any] | None = None

        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                candidate = json.loads(line)
            except (json.JSONDecodeError, ValueError, RecursionError):
                return None
            if not isinstance(candidate, dict):
                return None
            record_session_id = candidate.get("sessionId")
            record_trace_id = candidate.get("traceId")
            record_turn_id = candidate.get("turnId")
            # This file is the authority boundary for one ZCode session, so
            # do not silently ignore foreign or malformed history entries.
            # Any nonblank record without a complete filename-bound identity
            # makes chronology ambiguous and cannot yield executable evidence.
            if not (
                isinstance(record_session_id, str)
                and record_session_id == session_id
                and isinstance(record_trace_id, str)
                and record_trace_id.strip()
                and isinstance(record_turn_id, str)
                and record_turn_id.strip()
            ):
                return None
            request = candidate.get("request")
            if not isinstance(request, dict):
                return None
            messages = request.get("messages")
            if not isinstance(messages, list):
                return None
            # ZCode normally writes cumulative ``full`` snapshots, but some
            # turns emit a ``delta`` snapshot after the initial request.  The
            # verifier consumes one canonical cumulative history; reconstruct
            # the delta only when its offset is an exact append point.  A
            # malformed/gapped delta remains fail-closed rather than silently
            # dropping tool receipts.
            messages_kind = request.get("messagesKind", "full")
            if messages_kind == "delta":
                offset = request.get("messageOffset")
                if (
                    previous_messages is None
                    or isinstance(offset, bool)
                    or not isinstance(offset, int)
                    or offset != len(previous_messages)
                ):
                    return None
                canonical_messages = [*previous_messages, *messages]
                request = {
                    **request,
                    "messages": canonical_messages,
                    "messagesKind": "full",
                    "messageOffset": 0,
                }
                candidate = {**candidate, "request": request}
            elif messages_kind != "full":
                return None
            previous_messages = list(request["messages"])
            records.append(candidate)
        return records

    def _load_rollout_tool_receipts(self, event: dict[str, Any]) -> list[AgentMessage]:
        """Load tool receipts bound to this exact Zcode summary.

        Zcode 0.16.1 still emits only a terminal JSON summary on stdout, but it
        persists the current turn's model/tool exchange under
        ``~/.zcode/cli/rollout/model-io-<sessionId>.jsonl``.  The verifier must
        not trust final assistant prose as executed evidence, so normalize only
        a record whose session, trace, and turn identifiers all match the
        summary returned by the child process.

        Any missing, oversized, malformed, symlinked, foreign-owned, or
        identifier-mismatched record yields no receipts and therefore preserves
        the existing fail-closed verifier behaviour.
        """
        session_id = event.get("sessionId")
        trace_id = event.get("traceId")
        turn_id = event.get("turnId")
        if not all(
            isinstance(value, str) and value.strip() for value in (session_id, trace_id, turn_id)
        ):
            return []
        assert isinstance(session_id, str)
        assert isinstance(trace_id, str)
        assert isinstance(turn_id, str)
        if _ZCODE_SESSION_ID_RE.fullmatch(session_id) is None:
            return []

        records = self._read_trusted_rollout_history(session_id)
        if records is None:
            return []

        matching_indexes = [
            index
            for index, candidate in enumerate(records)
            if candidate.get("sessionId") == session_id
            and candidate.get("traceId") == trace_id
            and candidate.get("turnId") == turn_id
        ]
        if not matching_indexes:
            return []

        # A terminal summary may only consume the current, uniquely ordered
        # receipt history. Any record for another trace/turn makes the file
        # ambiguous: the matching snapshot may be a stale replay regardless of
        # whether the foreign record appears before or after it.
        last_matching_index = matching_indexes[-1]
        if any(
            candidate.get("sessionId") == session_id
            and (candidate.get("traceId") != trace_id or candidate.get("turnId") != turn_id)
            for candidate in records
        ):
            return []

        # ZCode appends cumulative request.messages snapshots for one turn.
        # Repeated exact-identity records therefore must preserve the complete
        # prior snapshot as a prefix. A rewrite or conflicting call under the
        # same identity makes the entire history ambiguous and fail-closed.
        previous_messages: list[Any] | None = None

        for index in matching_indexes:
            candidate_request = records[index].get("request")
            candidate_messages = (
                candidate_request.get("messages") if isinstance(candidate_request, dict) else None
            )
            if not isinstance(candidate_messages, list):
                return []
            if previous_messages is not None:
                current_prefix_identity = _zcode_message_identity(
                    candidate_messages[: len(previous_messages)]
                )
                previous_identity = _zcode_message_identity(previous_messages)
                if (
                    current_prefix_identity is None
                    or previous_identity is None
                    or current_prefix_identity != previous_identity
                ):
                    return []
            previous_messages = candidate_messages

        matching = records[last_matching_index]

        request = matching.get("request")
        # ZCode stores the normalized conversation at request.messages.  The
        # nested provider body is an HTTP payload and may omit tool exchanges.
        messages = request.get("messages") if isinstance(request, dict) else None
        if not isinstance(messages, list):
            return []

        # Validate evidence-bearing shapes across the complete cumulative
        # snapshot before selecting the current-turn suffix.  Otherwise a
        # malformed older turn can be hidden by the user-boundary slice while
        # later receipts are still granted evidential force.
        for message in messages:
            if not isinstance(message, dict):
                return []
            role = message.get("role")
            tool_calls = message.get("toolCalls")
            if "toolCalls" in message:
                if role != "assistant" or not isinstance(tool_calls, list):
                    return []
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        return []
                    call_id = call.get("id")
                    tool_name = call.get("name")
                    tool_input = call.get("input")
                    if not (
                        isinstance(call_id, str)
                        and call_id.strip()
                        and isinstance(tool_name, str)
                        and tool_name.strip()
                        and isinstance(tool_input, dict)
                    ):
                        return []
                    if _zcode_tool_input_fingerprint(tool_input) is None:
                        return []
            if any(field in message for field in ("toolCallId", "toolName", "isError")):
                if role != "tool":
                    return []

        # Validate tool results in the complete cumulative history before
        # selecting the current-turn suffix.  Otherwise malformed or
        # unmatched results from an earlier turn can be hidden by the latest
        # user boundary while later receipts are still promoted.
        history_calls: dict[str, str] = {}
        history_results: dict[str, tuple[str, bool, str]] = {}
        for message in messages:
            tool_calls = message.get("toolCalls")
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    call_id = call["id"]
                    call_name = call["name"]
                    call_input = _zcode_tool_input_fingerprint(call["input"])
                    if call_input is None:
                        return []
                    signature = f"{call_name}\x00{call_input}"
                    prior_call = history_calls.get(call_id)
                    if prior_call is not None and prior_call != signature:
                        return []
                    history_calls[call_id] = signature
            if message.get("role") != "tool":
                continue
            result_id = message.get("toolCallId")
            result_name = message.get("toolName")
            result_error = message.get("isError")
            if (
                not isinstance(result_id, str)
                or not result_id.strip()
                or not isinstance(result_name, str)
                or not result_name.strip()
                or not isinstance(result_error, bool)
                or result_id not in history_calls
            ):
                return []
            expected_name = history_calls[result_id].split("\x00", 1)[0]
            if result_name != expected_name:
                return []
            history_result_signature = (
                result_name,
                result_error,
                str(message.get("content") or ""),
            )
            prior_result = history_results.get(result_id)
            if prior_result is not None and prior_result != history_result_signature:
                return []
            history_results[result_id] = history_result_signature

        # Every tool call in the cumulative snapshot must have exactly one
        # semantically matching result.  Checking only result-to-call would
        # allow a dangling call from an older turn to be hidden by the
        # current-turn suffix selected below.
        if history_calls.keys() != history_results.keys():
            return []

        # ``messagesKind=full`` snapshots contain the whole resumed session,
        # including tool receipts from older turns.  A terminal summary is
        # bound to one turn, so accept only the suffix after that turn's final
        # user message.  Without this boundary, a prior successful command
        # could be replayed as evidence for the current AC.
        user_boundaries = [
            index
            for index, message in enumerate(messages)
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        if not user_boundaries:
            return []
        boundary = user_boundaries[-1]
        prior_tool_ids = {
            identifier
            for message in messages[: boundary + 1]
            if isinstance(message, dict)
            for identifier in [
                *(call.get("id") for call in message.get("toolCalls", [])),
                message.get("toolCallId") if message.get("role") == "tool" else None,
            ]
            if isinstance(identifier, str)
        }
        messages = messages[boundary + 1 :]

        receipts: list[AgentMessage] = []
        pending: dict[str, tuple[str, str]] = {}
        completed: dict[str, tuple[str, str, bool, str]] = {}
        for message in messages:
            if not isinstance(message, dict):
                return []
            tool_calls = message.get("toolCalls")
            if "toolCalls" in message and not isinstance(tool_calls, list):
                return []
            if isinstance(tool_calls, list):
                for call in tool_calls:
                    if not isinstance(call, dict):
                        return []
                    call_id = call.get("id")
                    tool_name = call.get("name")
                    tool_input = call.get("input")
                    if not (
                        isinstance(call_id, str)
                        and call_id.strip()
                        and isinstance(tool_name, str)
                        and tool_name.strip()
                        and isinstance(tool_input, dict)
                    ):
                        return []
                    if call_id in prior_tool_ids:
                        return []
                    input_fingerprint = _zcode_tool_input_fingerprint(tool_input)
                    if input_fingerprint is None:
                        return []
                    call_signature = (tool_name, input_fingerprint)
                    # Each model-io record is cumulative; earlier calls are
                    # repeated in later requests. Deduplicate only byte-for-
                    # byte equivalent normalized calls. Reusing an id with a
                    # different name/input invalidates the whole receipt set.
                    previous_call = pending.get(call_id)
                    if previous_call is not None:
                        if previous_call != call_signature:
                            return []
                        continue
                    if call_id in completed:
                        if completed[call_id][:2] != call_signature:
                            return []
                        continue
                    pending[call_id] = call_signature
                    receipts.append(
                        AgentMessage(
                            type="tool",
                            content=f"{tool_name}: executed by Zcode",
                            tool_name=tool_name,
                            data={
                                "tool_input": tool_input,
                                "tool_call_id": call_id,
                                "zcode_rollout_bound": True,
                                "traceId": trace_id,
                                "turnId": turn_id,
                            },
                        )
                    )

            if message.get("role") != "tool":
                continue
            call_id = message.get("toolCallId")
            tool_name = message.get("toolName")
            if not isinstance(call_id, str):
                return []
            if call_id in prior_tool_ids:
                return []
            is_error = message.get("isError")
            if not isinstance(is_error, bool):
                return []
            content = str(message.get("content") or "")
            result_signature: tuple[str, bool, str] = (str(tool_name), is_error, content)
            if call_id in completed:
                prior = completed[call_id]
                if (prior[0], prior[2], prior[3]) != result_signature:
                    return []
                continue
            if call_id not in pending:
                return []
            expected_name, input_fingerprint = pending.pop(call_id)
            if tool_name != expected_name:
                return []
            completed[call_id] = (expected_name, input_fingerprint, is_error, content)
            receipts.append(
                AgentMessage(
                    type="tool_result",
                    content=content,
                    tool_name=expected_name,
                    data={
                        "subtype": "tool_result",
                        "tool_call_id": call_id,
                        "exit_code": 1 if is_error else 0,
                        "tool_result": {"is_error": is_error},
                        "zcode_rollout_bound": True,
                        "traceId": trace_id,
                        "turnId": turn_id,
                    },
                )
            )

        if pending:
            return []
        return receipts


__all__ = ["ZcodeCLIRuntime"]
