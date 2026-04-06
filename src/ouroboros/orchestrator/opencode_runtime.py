"""OpenCode CLI runtime for Ouroboros orchestrator execution.

This module provides an ``AgentRuntime`` implementation that drives workflows
through the OpenCode CLI (``opencode run --format json``).  It follows the same
subprocess-based architecture as :class:`CodexCliRuntime` but is adapted for
OpenCode's JSON event format and session model.

Key features:
    - Subprocess execution via ``opencode run --format json``
    - Event normalization through :mod:`opencode_event_normalizer`
    - Session resume via ``--session <id>`` / ``--continue``
    - Subagent support: evaluation and LLM sub-tasks can run as child
      sessions within the same OpenCode instance rather than spawning
      independent processes
    - Skill interception compatible with Ouroboros MCP dispatch
"""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
import contextlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shlex
import shutil
from typing import Any

import yaml

from ouroboros.codex import resolve_packaged_codex_skill_path
from ouroboros.config import get_opencode_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle, TaskResult
from ouroboros.orchestrator.opencode_event_normalizer import (
    OpenCodeEventContext,
    OpenCodeEventNormalizer,
)

log = get_logger(__name__)

_SKILL_COMMAND_PATTERN = re.compile(
    r"^\s*(?:(?P<ooo_prefix>ooo)\s+(?P<ooo_skill>[a-z0-9][a-z0-9_-]*)|"
    r"(?P<slash_prefix>/ouroboros:)(?P<slash_skill>[a-z0-9][a-z0-9_-]*))"
    r"(?:\s+(?P<remainder>.*))?$",
    re.IGNORECASE,
)
_MCP_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_LINE_BUFFER_BYTES = 50 * 1024 * 1024  # 50 MB

_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"


@dataclass(frozen=True, slots=True)
class SkillInterceptRequest:
    """Metadata for a deterministic MCP skill intercept.

    Attributes:
        skill_name: Normalised skill name (e.g. ``"interview"``).
        command_prefix: Original prefix that triggered the intercept
            (``"ooo interview"`` or ``"/ouroboros:interview"``).
        prompt: Full prompt string that matched the skill pattern.
        skill_path: Filesystem path to the resolved ``SKILL.md``.
        mcp_tool: MCP tool name extracted from the YAML frontmatter.
        mcp_args: Default argument dict from the YAML frontmatter.
        first_argument: First positional argument following the
            command prefix, or ``None``.
    """

    skill_name: str
    command_prefix: str
    prompt: str
    skill_path: Path
    mcp_tool: str
    mcp_args: dict[str, Any]
    first_argument: str | None


type SkillDispatchHandler = Callable[
    [SkillInterceptRequest, RuntimeHandle | None],
    Awaitable[tuple[AgentMessage, ...] | None],
]


class OpenCodeRuntime:
    """Agent runtime that shells out to the locally installed OpenCode CLI.

    This runtime implements the ``AgentRuntime`` protocol by invoking
    ``opencode run --format json <prompt>`` and streaming the resulting
    JSON events through :class:`OpenCodeEventNormalizer`.

    Subagent support:
        OpenCode natively supports child sessions (subagents) through
        its ``task`` tool.  When the orchestrator needs to run
        evaluation or other LLM sub-tasks, this runtime can continue
        the same OpenCode session via ``--session <id>`` rather than
        spawning a completely independent process.  This keeps context,
        tool state, and provider authentication shared across parent
        and child tasks.

    Attributes:
        _runtime_handle_backend: Backend tag written into
            :class:`~ouroboros.orchestrator.adapter.RuntimeHandle`.
        _runtime_backend: Backend identifier for MCP tool lookup.
        _requires_memory_gate: Whether memory-gate middleware is
            needed (always ``False`` for OpenCode).
        _provider_name: Provider label for error reports.
        _runtime_error_type: Error type string emitted on failure.
        _log_namespace: Structured-log event prefix.
        _display_name: Human-readable runtime name.
        _default_cli_name: Binary name resolved from ``PATH``.
        _default_llm_backend: Fallback LLM backend identifier.
        _tempfile_prefix: Prefix for temporary files.
        _process_shutdown_timeout_seconds: Grace period before
            ``SIGKILL`` on child process shutdown.
        _max_resume_retries: Maximum session resume attempts.
        _max_ouroboros_depth: Recursion depth cap.
        _startup_output_timeout_seconds: Timeout waiting for first
            stdout output from the CLI.
        _stdout_idle_timeout_seconds: Timeout between successive
            stdout chunks.
        _max_stderr_lines: Tail-cap for collected stderr lines.
        _cli_path: Resolved path to the ``opencode`` binary.
        _permission_mode: OpenCode permission mode string.
        _model: Optional model override passed via ``--model``.
        _cwd: Working directory for subprocess execution.
        _skills_dir: Optional override directory for packaged skills.
        _skill_dispatcher: Optional external skill dispatch handler.
        _llm_backend: Active LLM backend identifier.
        _builtin_mcp_handlers: Lazily-loaded local MCP handler cache.
    """

    _runtime_handle_backend = "opencode"
    _runtime_backend = "opencode"
    _requires_memory_gate = False
    _provider_name = "opencode"
    _runtime_error_type = "OpenCodeError"
    _log_namespace = "opencode_runtime"
    _display_name = "OpenCode"
    _default_cli_name = "opencode"
    _default_llm_backend = "opencode"
    _tempfile_prefix = "ouroboros-opencode-"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 3
    _max_ouroboros_depth = 5
    _startup_output_timeout_seconds = 120.0
    _stdout_idle_timeout_seconds = 600.0
    _max_stderr_lines = 512

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
        """Initialise the OpenCode runtime.

        Args:
            cli_path: Explicit path to the ``opencode`` binary.
                Resolved from
                :func:`~ouroboros.config.get_opencode_cli_path`,
                then ``PATH`` if not supplied.
            permission_mode: OpenCode permission mode string.
                Stored for forward compatibility and surfaced in
                :attr:`RuntimeHandle.approval_mode`, but currently a
                **no-op**: ``opencode run`` has no permission flag.
            model: Optional model override passed to the CLI via
                ``--model``.
            cwd: Working directory for the subprocess.  Defaults to
                the current process working directory.
            skills_dir: Optional directory containing custom skill
                overrides.
            skill_dispatcher: Optional external callable for skill
                intercept dispatch.  Falls back to the built-in
                local MCP handler when ``None``.
            llm_backend: LLM backend identifier used when loading
                MCP tool handlers.
        """
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = permission_mode or "bypassPermissions"
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._skills_dir = self._resolve_skills_dir(skills_dir)
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or self._default_llm_backend
        self._builtin_mcp_handlers: dict[str, Any] | None = None

        log.info(
            f"{self._log_namespace}.initialized",
            cli_path=self._cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=self._cwd,
        )

    # -- AgentRuntime protocol properties ----------------------------------

    @property
    def runtime_backend(self) -> str:
        """Return the backend identifier for this runtime.

        Returns:
            The ``_runtime_handle_backend`` class attribute.
        """
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        """Return the working directory used by this runtime.

        Returns:
            Absolute path string, or ``None`` if unset.
        """
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        """Return the OpenCode permission mode.

        Returns:
            Permission mode string (e.g. ``"bypassPermissions"``).
        """
        return self._permission_mode

    # -- CLI resolution ----------------------------------------------------

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the OpenCode CLI binary path.

        Checks, in order: *cli_path* argument,
        :func:`~ouroboros.config.get_opencode_cli_path`, ``PATH``
        lookup, and finally the bare binary name.

        Args:
            cli_path: Explicit override path, or ``None`` to
                auto-resolve.

        Returns:
            Absolute or bare binary path string.
        """
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = (
                get_opencode_cli_path()
                or shutil.which(self._default_cli_name)
                or self._default_cli_name
            )

        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
        return candidate

    def _resolve_skills_dir(self, skills_dir: str | Path | None) -> Path | None:
        """Resolve an optional explicit skill override directory.

        Args:
            skills_dir: Directory path, or ``None`` to skip.

        Returns:
            Expanded :class:`~pathlib.Path`, or ``None`` when no
            override is configured.
        """
        if skills_dir is None:
            return None
        return Path(skills_dir).expanduser()

    def _normalize_model(self, model: str | None) -> str | None:
        """Normalize backend model values before passing to the CLI.

        Strips whitespace and converts the ``"default"`` sentinel to
        ``None``.

        Args:
            model: Raw model name string or ``None``.

        Returns:
            Sanitised model string, or ``None`` when the default
            model should be used.
        """
        if model is None:
            return None
        candidate = model.strip()
        if not candidate or candidate == "default":
            return None
        return candidate

    # -- RuntimeHandle management ------------------------------------------

    def _build_runtime_handle(
        self,
        session_id: str | None,
        current_handle: RuntimeHandle | None = None,
    ) -> RuntimeHandle | None:
        """Build or update a runtime handle for an OpenCode session.

        When *current_handle* is provided its fields are carried
        forward and the session ID is updated in place.  Otherwise a
        fresh :class:`~ouroboros.orchestrator.adapter.RuntimeHandle`
        is created.

        Args:
            session_id: OpenCode session identifier, or ``None``.
            current_handle: Existing handle to update, or ``None``
                to create a new one.

        Returns:
            Updated or new
            :class:`~ouroboros.orchestrator.adapter.RuntimeHandle`,
            or ``None`` when *session_id* is falsy.
        """
        if not session_id:
            return None

        if current_handle is not None:
            return replace(
                current_handle,
                backend=current_handle.backend or self._runtime_handle_backend,
                kind=current_handle.kind or "agent_runtime",
                native_session_id=session_id,
                cwd=current_handle.cwd or self._cwd,
                approval_mode=current_handle.approval_mode or self._permission_mode,
                updated_at=datetime.now(UTC).isoformat(),
                metadata=dict(current_handle.metadata),
            )

        return RuntimeHandle(
            backend=self._runtime_handle_backend,
            kind="agent_runtime",
            native_session_id=session_id,
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            updated_at=datetime.now(UTC).isoformat(),
        )

    # -- Prompt composition ------------------------------------------------

    def _compose_prompt(
        self,
        prompt: str,
        system_prompt: str | None,
        tools: list[str] | None,
    ) -> str:
        """Compose a single prompt string for the OpenCode CLI.

        Assembles system instructions, tool guidance, and the user
        prompt into a markdown-structured string.

        Args:
            prompt: Primary task prompt text.
            system_prompt: Optional system instructions prepended
                under a ``## System Instructions`` header.
            tools: Optional tool names rendered under a
                ``## Tooling Guidance`` header.

        Returns:
            Concatenated prompt string.
        """
        parts: list[str] = []

        if system_prompt:
            parts.append(f"## System Instructions\n{system_prompt}")

        if tools:
            tool_list = "\n".join(f"- {tool}" for tool in tools)
            parts.append(
                "## Tooling Guidance\n"
                "Prefer to solve the task using the following tool set when possible:\n"
                f"{tool_list}"
            )

        parts.append(prompt)
        return "\n\n".join(part for part in parts if part.strip())

    # -- Command building --------------------------------------------------

    def _build_command(
        self,
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
    ) -> list[str]:
        """Assemble the CLI argument list for ``opencode run``.

        The prompt is **not** included in argv — it is piped via stdin
        to avoid OS ``ARG_MAX`` / ``MAX_ARG_STRLEN`` limits (~128 KB per
        argument on Linux).  OpenCode auto-detects piped stdin when
        ``!process.stdin.isTTY``.

        Args:
            resume_session_id: Optional session ID for
                ``--session`` based resume.
            prompt: Unused — kept for signature compatibility.
                Prompt is fed via :meth:`execute_task` stdin pipe.

        Returns:
            Argument list suitable for
            :func:`asyncio.create_subprocess_exec`.

        Raises:
            ValueError: If *resume_session_id* contains disallowed
                characters.
        """
        command = [self._cli_path, "run", "--format", "json"]

        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["--model", normalized_model])

        if resume_session_id:
            if not _SAFE_SESSION_ID_PATTERN.match(resume_session_id):
                raise ValueError(
                    f"Invalid resume_session_id: contains disallowed characters: "
                    f"{resume_session_id!r}"
                )
            command.extend(["--session", resume_session_id])

        return command

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for child runtime processes.

        Strips Ouroboros runtime env vars to prevent recursive MCP
        startup loops and tracks nesting depth to prevent fork bombs.

        Returns:
            Copy of ``os.environ`` with Ouroboros keys removed and
            depth counter incremented.

        Raises:
            RuntimeError: If the nesting depth exceeds
                ``_max_ouroboros_depth``.
        """
        env = os.environ.copy()
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)
        # Track and enforce recursion depth
        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1
        if depth > self._max_ouroboros_depth:
            msg = f"Maximum Ouroboros nesting depth ({self._max_ouroboros_depth}) exceeded"
            raise RuntimeError(msg)
        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

    # -- Stream parsing ----------------------------------------------------

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield decoded lines from a subprocess stdout stream.

        Reads raw bytes in *chunk_size* increments, decodes UTF-8
        with replacement, and splits on newline boundaries.  Enforces
        ``_MAX_LINE_BUFFER_BYTES`` to prevent unbounded memory use.

        Args:
            stream: Async stream reader from the subprocess, or
                ``None`` (returns immediately).
            chunk_size: Number of bytes to read per iteration.
            first_chunk_timeout_seconds: Maximum wait for the very
                first chunk (startup guard).
            chunk_timeout_seconds: Maximum wait between subsequent
                chunks (idle guard).

        Yields:
            Individual newline-delimited strings with trailing
            ``\\r`` stripped.

        Raises:
            TimeoutError: If a timeout is exceeded.
            ProviderError: If the internal line buffer overflows.
        """
        if stream is None:
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        buffer_byte_estimate = 0
        saw_chunk = False

        while True:
            timeout_seconds: float | None = None
            if not saw_chunk:
                timeout_seconds = first_chunk_timeout_seconds
            elif chunk_timeout_seconds is not None:
                timeout_seconds = chunk_timeout_seconds

            try:
                if timeout_seconds is None:
                    chunk = await stream.read(chunk_size)
                else:
                    chunk = await asyncio.wait_for(
                        stream.read(chunk_size),
                        timeout=timeout_seconds,
                    )
            except TimeoutError as exc:
                phase = "startup" if not saw_chunk else "idle"
                raise TimeoutError(
                    f"{self._display_name} produced no stdout during {phase} "
                    f"window ({timeout_seconds:.0f}s)"
                ) from exc
            if not chunk:
                break

            saw_chunk = True
            decoded = decoder.decode(chunk)
            buffer += decoded
            buffer_byte_estimate += len(decoded) * 4
            if buffer_byte_estimate > _MAX_LINE_BUFFER_BYTES:
                log.error(
                    f"{self._log_namespace}.line_buffer_overflow",
                    buffer_size=len(buffer),
                    limit=_MAX_LINE_BUFFER_BYTES,
                )
                raise ProviderError(f"JSONL line buffer exceeded {_MAX_LINE_BUFFER_BYTES} bytes")
            while True:
                newline_index = buffer.find("\n")
                if newline_index < 0:
                    break
                line = buffer[:newline_index]
                buffer = buffer[newline_index + 1 :]
                buffer_byte_estimate = len(buffer) * 4
                yield line.rstrip("\r")

        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer.rstrip("\r")

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        max_lines: int | None = None,
    ) -> list[str]:
        """Drain a subprocess stream into a list of lines.

        Collects all lines without blocking the event loop.  When
        *max_lines* is set the collection is tail-capped via a
        :class:`~collections.deque`.

        Args:
            stream: Async stream reader, or ``None``.
            max_lines: Optional cap on retained lines (keeps the
                most recent).

        Returns:
            List of decoded line strings.
        """
        if stream is None:
            return []

        if max_lines is not None and max_lines > 0:
            lines: deque[str] = deque(maxlen=max_lines)
        else:
            lines = deque()
        async for line in self._iter_stream_lines(stream):
            if line:
                lines.append(line)
        return list(lines)

    # -- Process management ------------------------------------------------

    async def _terminate_process(self, process: Any) -> None:
        """Best-effort subprocess shutdown.

        Sends ``SIGTERM``, waits up to
        ``_process_shutdown_timeout_seconds``, then escalates to
        ``SIGKILL`` if the process is still alive.

        Args:
            process: Subprocess object (must expose ``terminate``,
                ``kill``, and ``wait`` methods).
        """
        if getattr(process, "returncode", None) is not None:
            return

        terminate = getattr(process, "terminate", None)
        kill = getattr(process, "kill", None)

        try:
            if callable(terminate):
                terminate()
            elif callable(kill):
                kill()
            else:
                return
        except ProcessLookupError:
            return
        except Exception as exc:
            log.warning(
                f"{self._log_namespace}.process_terminate_failed",
                error=str(exc),
            )
            return

        try:
            await asyncio.wait_for(
                process.wait(),
                timeout=self._process_shutdown_timeout_seconds,
            )
            return
        except (TimeoutError, ProcessLookupError):
            pass

        if callable(kill):
            with contextlib.suppress(ProcessLookupError, Exception):
                kill()
            with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, Exception):
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self._process_shutdown_timeout_seconds,
                )

    # -- Event conversion --------------------------------------------------

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a single JSON line into a dict.

        Args:
            line: Raw string from the subprocess stdout stream.

        Returns:
            Parsed dict, or ``None`` if the line is not valid JSON
            or is not a dict.
        """
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    def _extract_event_session_id(self, event: dict[str, Any]) -> str | None:
        """Extract the OpenCode session ID from a JSON event.

        Args:
            event: Parsed JSON event dict.

        Returns:
            Stripped session ID string, or ``None`` if absent.
        """
        session_id = event.get("sessionID")
        if isinstance(session_id, str) and session_id.strip():
            return session_id.strip()
        return None

    def _convert_event(
        self,
        event: dict[str, Any],
        context: OpenCodeEventContext,
    ) -> list[AgentMessage]:
        """Convert an OpenCode JSON event into normalised messages.

        Delegates to
        :meth:`OpenCodeEventNormalizer.normalize` and returns the
        result as a mutable list.

        Args:
            event: Parsed JSON event dict.
            context: Accumulated session context for handle tracking.

        Returns:
            List of
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`
            values (possibly empty).
        """
        return list(OpenCodeEventNormalizer.normalize(event, context))

    # -- Skill interception ------------------------------------------------

    def _extract_first_argument(self, remainder: str | None) -> str | None:
        """Extract the first positional argument from a command tail.

        Uses :func:`shlex.split` for proper quoting, falling back
        to a naive whitespace split on parse errors.

        Args:
            remainder: The command string following the skill prefix,
                or ``None``.

        Returns:
            First argument string, or ``None`` if *remainder* is
            empty.
        """
        if not remainder or not remainder.strip():
            return None
        try:
            args = shlex.split(remainder)
        except ValueError:
            args = remainder.strip().split(maxsplit=1)
        return args[0] if args else None

    def _load_skill_frontmatter(self, skill_md_path: Path) -> dict[str, Any]:
        """Load YAML frontmatter from a packaged ``SKILL.md`` file.

        Parses the leading ``---``-delimited YAML block and returns
        it as a plain dict.

        Args:
            skill_md_path: Filesystem path to the ``SKILL.md`` file.

        Returns:
            Parsed frontmatter dict, or an empty dict if no
            frontmatter is present.

        Raises:
            ValueError: If frontmatter delimiters are malformed or
                the parsed value is not a mapping.
        """
        content = skill_md_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}

        closing_index = next(
            (index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"),
            None,
        )
        if closing_index is None:
            msg = f"Unterminated frontmatter in {skill_md_path}"
            raise ValueError(msg)

        raw_frontmatter = "\n".join(lines[1:closing_index]).strip()
        if not raw_frontmatter:
            return {}

        parsed = yaml.safe_load(raw_frontmatter)
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            msg = f"Frontmatter must be a mapping in {skill_md_path}"
            raise ValueError(msg)
        return parsed

    def _normalize_mcp_frontmatter(
        self,
        frontmatter: dict[str, Any],
    ) -> tuple[tuple[str, dict[str, Any]] | None, str | None]:
        """Validate and normalise MCP dispatch metadata from frontmatter.

        Checks that ``mcp_tool`` and ``mcp_args`` keys are present
        and well-typed.

        Args:
            frontmatter: Parsed YAML frontmatter dict.

        Returns:
            A 2-tuple ``(normalised, error)``.  On success
            *normalised* is ``(mcp_tool, mcp_args)`` and *error* is
            ``None``.  On failure *normalised* is ``None`` and
            *error* contains a human-readable reason string.
        """
        raw_mcp_tool = frontmatter.get("mcp_tool")
        if raw_mcp_tool is None:
            return None, "missing required frontmatter key: mcp_tool"
        if not isinstance(raw_mcp_tool, str) or not raw_mcp_tool.strip():
            return None, "mcp_tool must be a non-empty string"

        mcp_tool = raw_mcp_tool.strip()
        if _MCP_TOOL_NAME_PATTERN.fullmatch(mcp_tool) is None:
            return None, "mcp_tool must contain only letters, digits, and underscores"

        if "mcp_args" not in frontmatter:
            return None, "missing required frontmatter key: mcp_args"

        raw_mcp_args = frontmatter.get("mcp_args")
        if not isinstance(raw_mcp_args, Mapping):
            return None, "mcp_args must be a mapping with string keys and YAML-safe values"

        return (mcp_tool, dict(raw_mcp_args)), None

    def _get_builtin_mcp_handlers(self) -> dict[str, Any]:
        """Load and cache local Ouroboros MCP handlers.

        Lazily imports
        :func:`~ouroboros.mcp.tools.definitions.get_ouroboros_tools`
        and indexes handlers by tool name for exact-prefix dispatch.

        Returns:
            Dict mapping MCP tool names to handler objects.
        """
        if self._builtin_mcp_handlers is None:
            from ouroboros.mcp.tools.definitions import get_ouroboros_tools

            self._builtin_mcp_handlers = {
                handler.definition.name: handler
                for handler in get_ouroboros_tools(
                    runtime_backend=self._runtime_backend,
                    llm_backend=self._llm_backend,
                )
            }
        return self._builtin_mcp_handlers

    def _get_mcp_tool_handler(self, tool_name: str) -> Any | None:
        """Look up a local MCP handler by tool name.

        Args:
            tool_name: Registered MCP tool name.

        Returns:
            Handler object, or ``None`` if not registered.
        """
        return self._get_builtin_mcp_handlers().get(tool_name)

    def _build_tool_arguments(
        self,
        intercept: SkillInterceptRequest,
        current_handle: RuntimeHandle | None,
    ) -> dict[str, Any]:
        """Build the MCP argument payload for an intercepted skill.

        For ``ouroboros_interview`` tools, injects the persisted
        interview session ID and the first positional argument as
        ``answer`` when available.

        Args:
            intercept: Resolved skill intercept metadata.
            current_handle: Active runtime handle carrying session
                metadata, or ``None``.

        Returns:
            Dict of MCP tool arguments ready for dispatch.
        """
        if intercept.mcp_tool != "ouroboros_interview" or current_handle is None:
            return dict(intercept.mcp_args)

        session_id = current_handle.metadata.get(_INTERVIEW_SESSION_METADATA_KEY)
        if not isinstance(session_id, str) or not session_id.strip():
            return dict(intercept.mcp_args)

        arguments: dict[str, Any] = dict(intercept.mcp_args)
        arguments["session_id"] = session_id.strip()
        if intercept.first_argument is not None:
            arguments["answer"] = intercept.first_argument
        return arguments

    def _build_resume_handle(
        self,
        current_handle: RuntimeHandle | None,
        intercept: SkillInterceptRequest,
        tool_result: Any,
    ) -> RuntimeHandle | None:
        """Attach interview session metadata to the runtime handle.

        After an ``ouroboros_interview`` tool call, persists the
        returned ``session_id`` into the handle's metadata so
        subsequent turns can resume the same interview.

        Args:
            current_handle: Existing handle, or ``None``.
            intercept: Skill intercept metadata.
            tool_result: MCP tool result containing a ``meta`` dict.

        Returns:
            Updated or new
            :class:`~ouroboros.orchestrator.adapter.RuntimeHandle`,
            or ``None`` if no session ID was found.
        """
        if intercept.mcp_tool != "ouroboros_interview":
            return current_handle

        session_id = tool_result.meta.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            return current_handle

        metadata = dict(current_handle.metadata) if current_handle is not None else {}
        metadata[_INTERVIEW_SESSION_METADATA_KEY] = session_id.strip()
        updated_at = datetime.now(UTC).isoformat()

        if current_handle is not None:
            return replace(current_handle, metadata=metadata, updated_at=updated_at)

        return RuntimeHandle(
            backend=self.runtime_backend,
            cwd=self.working_directory,
            approval_mode=self.permission_mode,
            updated_at=updated_at,
            metadata=metadata,
        )

    async def _dispatch_skill_intercept_locally(
        self,
        intercept: SkillInterceptRequest,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Dispatch an exact-prefix intercept to a local MCP handler.

        Looks up the handler by ``intercept.mcp_tool``, invokes it,
        and wraps the outcome in a pair of
        :class:`~ouroboros.orchestrator.adapter.AgentMessage` values
        (tool call + result).

        Args:
            intercept: Resolved skill intercept metadata.
            current_handle: Active runtime handle, or ``None``.

        Returns:
            Tuple of ``(assistant, result)``
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`
            values, or ``None`` if dispatch failed.

        Raises:
            LookupError: If no local handler is registered for the
                requested tool.
        """
        handler = self._get_mcp_tool_handler(intercept.mcp_tool)
        if handler is None:
            raise LookupError(f"No local handler registered for tool: {intercept.mcp_tool}")

        tool_arguments = self._build_tool_arguments(intercept, current_handle)
        tool_result = await handler.handle(tool_arguments)
        if tool_result.is_err:
            error = tool_result.error
            error_data: dict[str, Any] = {
                "subtype": "error",
                "error_type": type(error).__name__,
                "recoverable": True,
            }
            return (
                AgentMessage(
                    type="assistant",
                    content=f"Calling tool: {intercept.mcp_tool}",
                    tool_name=intercept.mcp_tool,
                    data={"tool_input": tool_arguments},
                    resume_handle=current_handle,
                ),
                AgentMessage(
                    type="result",
                    content=str(error),
                    data=error_data,
                    resume_handle=current_handle,
                ),
            )

        resolved_result = tool_result.value
        resume_handle = self._build_resume_handle(current_handle, intercept, resolved_result)
        result_text = resolved_result.text_content.strip() or f"{intercept.mcp_tool} completed."
        result_data: dict[str, Any] = {
            "subtype": "error" if resolved_result.is_error else "success",
            "tool_name": intercept.mcp_tool,
        }

        return (
            AgentMessage(
                type="assistant",
                content=f"Calling tool: {intercept.mcp_tool}",
                tool_name=intercept.mcp_tool,
                data={"tool_input": tool_arguments},
                resume_handle=resume_handle,
            ),
            AgentMessage(
                type="result",
                content=result_text,
                data=result_data,
                resume_handle=resume_handle,
            ),
        )

    def _resolve_skill_intercept(self, prompt: str) -> SkillInterceptRequest | None:
        """Resolve a deterministic MCP intercept from a skill command.

        Matches the prompt against ``_SKILL_COMMAND_PATTERN``, locates
        the corresponding ``SKILL.md``, and extracts MCP dispatch
        metadata from its YAML frontmatter.

        Args:
            prompt: Raw prompt string to test for skill prefixes.

        Returns:
            :class:`SkillInterceptRequest` when the prompt matches a
            known skill with valid MCP frontmatter, or ``None``.
        """
        match = _SKILL_COMMAND_PATTERN.match(prompt)
        if match is None:
            return None

        skill_name = (match.group("ooo_skill") or match.group("slash_skill") or "").lower()
        if not skill_name:
            return None

        command_prefix = (
            f"ooo {skill_name}"
            if match.group("ooo_skill") is not None
            else f"/ouroboros:{skill_name}"
        )
        try:
            with resolve_packaged_codex_skill_path(
                skill_name,
                skills_dir=self._skills_dir,
            ) as skill_md_path:
                frontmatter = self._load_skill_frontmatter(skill_md_path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.warning(
                f"{self._log_namespace}.skill_intercept_frontmatter_invalid",
                skill=skill_name,
                error=str(e),
            )
            return None

        normalized, validation_error = self._normalize_mcp_frontmatter(frontmatter)
        if normalized is None:
            if validation_error and not validation_error.startswith(
                "missing required frontmatter key:"
            ):
                log.warning(
                    f"{self._log_namespace}.skill_intercept_frontmatter_invalid",
                    skill=skill_name,
                    error=validation_error,
                )
            return None

        mcp_tool, mcp_args = normalized
        first_argument = self._extract_first_argument(match.group("remainder"))
        return SkillInterceptRequest(
            skill_name=skill_name,
            command_prefix=command_prefix,
            prompt=prompt,
            skill_path=skill_md_path,
            mcp_tool=mcp_tool,
            mcp_args=self._resolve_dispatch_templates(
                mcp_args,
                first_argument=first_argument,
            ),
            first_argument=first_argument,
        )

    def _resolve_dispatch_templates(
        self,
        value: Any,
        *,
        first_argument: str | None,
    ) -> Any:
        """Resolve ``$1`` / ``$CWD`` placeholders in MCP dispatch arguments.

        Mirrors :meth:`CodexCliRuntime._resolve_dispatch_templates` so that
        shipped skill frontmatter like ``mcp_args: {seed_path: "$1"}`` is
        expanded before the MCP tool call.

        Args:
            value: Argument tree (str, dict, list, or primitive).
            first_argument: The first positional word after the command prefix.

        Returns:
            Expanded copy of *value*.
        """
        if isinstance(value, str):
            if value == "$1":
                return first_argument if first_argument is not None else ""
            if value == "$CWD":
                return self._cwd
            return value

        if isinstance(value, Mapping):
            return {
                key: self._resolve_dispatch_templates(item, first_argument=first_argument)
                for key, item in value.items()
            }

        if isinstance(value, list):
            return [
                self._resolve_dispatch_templates(item, first_argument=first_argument)
                for item in value
            ]

        return value

    async def _maybe_dispatch_skill_intercept(
        self,
        prompt: str,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Attempt deterministic skill dispatch before invoking OpenCode.

        Falls back to ``None`` (no interception) when the prompt does
        not match a known skill, when dispatch raises, or when the
        result contains a recoverable error that should be retried by
        the main CLI path.

        Args:
            prompt: Raw prompt string.
            current_handle: Active runtime handle, or ``None``.

        Returns:
            Tuple of
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`
            values if dispatched, or ``None`` to fall through to the
            CLI path.
        """
        intercept = self._resolve_skill_intercept(prompt)
        if intercept is None:
            return None

        dispatcher = self._skill_dispatcher or self._dispatch_skill_intercept_locally
        try:
            dispatched_messages = await dispatcher(intercept, current_handle)
        except Exception as e:
            log.warning(
                f"{self._log_namespace}.skill_intercept_dispatch_failed",
                skill=intercept.skill_name,
                error=str(e),
            )
            return None

        # Check for recoverable errors that should fall through to OpenCode
        if dispatched_messages:
            final_msg = next(
                (m for m in reversed(dispatched_messages) if m.is_final and m.is_error),
                None,
            )
            if final_msg is not None:
                data = final_msg.data
                if data.get("recoverable") is True or data.get("is_retriable") is True:
                    return None

        return dispatched_messages

    # -- Runtime handle controls -------------------------------------------

    def _bind_runtime_handle_controls(
        self,
        handle: RuntimeHandle | None,
        *,
        process: Any,
        control_state: dict[str, Any],
    ) -> RuntimeHandle | None:
        """Attach live observe/terminate callbacks to a runtime handle.

        The callbacks close over *process* and *control_state* so
        callers can inspect or terminate the subprocess through the
        handle abstraction.

        Args:
            handle: Runtime handle to bind, or ``None``.
            process: Live subprocess object.
            control_state: Mutable dict tracking PID, return code,
                and termination flag.

        Returns:
            Handle with bound controls, or ``None`` when *handle*
            is ``None``.
        """
        if handle is None:
            return None

        async def _observe(_handle: RuntimeHandle) -> dict[str, Any]:
            snapshot = _handle.snapshot()
            pid = control_state.get("process_id")
            if isinstance(pid, int):
                snapshot["process_id"] = pid
            rc = control_state.get("returncode")
            if isinstance(rc, int):
                snapshot["returncode"] = rc
                snapshot["lifecycle_state"] = "completed" if rc == 0 else "failed"
            return snapshot

        async def _terminate(_handle: RuntimeHandle) -> bool:
            if control_state.get("terminated") is True:
                return False
            if getattr(process, "returncode", None) is not None:
                return False
            control_state["runtime_status"] = "terminating"
            await self._terminate_process(process)
            control_state["terminated"] = True
            rc = getattr(process, "returncode", None)
            control_state["returncode"] = rc
            control_state["runtime_status"] = (
                "completed" if rc == 0 else ("terminated" if rc and rc < 0 else "failed")
            )
            return True

        return handle.bind_controls(
            observe_callback=_observe,
            terminate_callback=_terminate,
        )

    # -- Main execute_task -------------------------------------------------

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task via the OpenCode CLI and stream messages.

        Tries skill interception first; if no intercept matches,
        spawns ``opencode run --format json``, streams JSON events,
        normalises them into
        :class:`~ouroboros.orchestrator.adapter.AgentMessage` values,
        and yields each one.

        Args:
            prompt: Primary task prompt text.
            tools: Optional tool names injected into the prompt.
            system_prompt: Optional system instructions prepended to
                the prompt.
            resume_handle: Existing
                :class:`~ouroboros.orchestrator.adapter.RuntimeHandle`
                for session resume.
            resume_session_id: Alternative session ID string for
                resume (used when *resume_handle* is ``None``).

        Yields:
            :class:`~ouroboros.orchestrator.adapter.AgentMessage`
            values as they arrive from the CLI.
        """
        current_handle = resume_handle or self._build_runtime_handle(resume_session_id)

        # Try skill interception first
        intercepted = await self._maybe_dispatch_skill_intercept(prompt, current_handle)
        if intercepted is not None:
            for message in intercepted:
                if message.resume_handle is not None:
                    current_handle = message.resume_handle
                yield message
            return

        composed_prompt = self._compose_prompt(prompt, system_prompt, tools)
        attempted_resume_session_id = (
            current_handle.native_session_id if current_handle is not None else resume_session_id
        )

        try:
            command = self._build_command(
                resume_session_id=attempted_resume_session_id,
            )
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to prepare {self._display_name}: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return

        log.info(
            f"{self._log_namespace}.task_started",
            command=command[:4],
            cwd=self._cwd,
            has_resume_handle=current_handle is not None,
        )

        stderr_lines: list[str] = []
        last_content = ""
        yielded_final = False
        process: Any | None = None
        process_finished = False
        process_terminated = False
        control_state: dict[str, Any] | None = None
        stderr_task: asyncio.Task[list[str]] | None = None

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_child_env(),
            )
        except FileNotFoundError as e:
            yield AgentMessage(
                type="result",
                content=(
                    f"{self._display_name} not found: {e}. "
                    "Install with: npm i -g opencode-ai@latest"
                ),
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to start {self._display_name}: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return

        control_state = {
            "handle": current_handle,
            "process_id": getattr(process, "pid", None),
            "returncode": getattr(process, "returncode", None),
            "runtime_status": (
                current_handle.lifecycle_state if current_handle is not None else "starting"
            ),
            "terminated": False,
        }
        current_handle = self._bind_runtime_handle_controls(
            current_handle,
            process=process,
            control_state=control_state,
        )

        # Feed prompt via stdin to avoid OS ARG_MAX / MAX_ARG_STRLEN limits
        # (~128 KB per single argument on Linux).  OpenCode auto-reads stdin
        # when !process.stdin.isTTY, so piping works out of the box.
        if composed_prompt and process.stdin is not None:
            try:
                process.stdin.write(composed_prompt.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError, OSError) as exc:
                # OpenCode exited before consuming stdin — close the pipe and
                # fall through to normal stderr-based failure reporting.
                log.warning(
                    f"{self._log_namespace}.stdin_write_failed",
                    error=str(exc),
                )
                with contextlib.suppress(OSError):
                    process.stdin.close()

        stderr_task = asyncio.create_task(
            self._collect_stream_lines(
                process.stderr,
                max_lines=self._max_stderr_lines,
            )
        )

        try:
            if process.stdout is not None:
                async for line in self._iter_stream_lines(
                    process.stdout,
                    first_chunk_timeout_seconds=self._startup_output_timeout_seconds,
                    chunk_timeout_seconds=self._stdout_idle_timeout_seconds,
                ):
                    if not line:
                        continue

                    event = self._parse_json_event(line)
                    if event is None:
                        continue

                    # Track session ID from events
                    event_session_id = self._extract_event_session_id(event)
                    if event_session_id and (
                        current_handle is None
                        or current_handle.native_session_id != event_session_id
                    ):
                        current_handle = self._build_runtime_handle(
                            event_session_id,
                            current_handle,
                        )
                        current_handle = self._bind_runtime_handle_controls(
                            current_handle,
                            process=process,
                            control_state=control_state,
                        )

                    context = OpenCodeEventContext(
                        session_id=event_session_id,
                        current_handle=current_handle,
                    )

                    for message in self._convert_event(event, context):
                        if message.resume_handle is not None:
                            current_handle = message.resume_handle
                            current_handle = self._bind_runtime_handle_controls(
                                current_handle,
                                process=process,
                                control_state=control_state,
                            )
                            message = replace(message, resume_handle=current_handle)
                        if message.content:
                            last_content = message.content
                        if message.is_final:
                            yielded_final = True
                        yield message

        except TimeoutError as e:
            if process is not None and control_state is not None:
                await self._terminate_process(process)
                control_state["terminated"] = True
            process_terminated = True
            if stderr_task is not None:
                stderr_lines = await stderr_task
            final_message = "\n".join(stderr_lines).strip()
            if not final_message:
                final_message = f"{self._display_name} became unresponsive and was terminated: {e}"
            yield AgentMessage(
                type="result",
                content=final_message,
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            return
        except asyncio.CancelledError:
            if process is not None:
                log.warning(f"{self._log_namespace}.task_cancelled", cwd=self._cwd)
                await self._terminate_process(process)
                process_terminated = True
                if control_state is not None:
                    control_state["terminated"] = True
            raise
        else:
            # Normal completion — stdout stream finished
            returncode = await process.wait()
            process_finished = True
            control_state["returncode"] = returncode
            control_state["runtime_status"] = "completed" if returncode == 0 else "failed"
            current_handle = self._bind_runtime_handle_controls(
                current_handle,
                process=process,
                control_state=control_state,
            )
            stderr_lines = await stderr_task

            if yielded_final:
                return

            final_message = last_content or "\n".join(stderr_lines).strip()
            # On failure, prefer stderr over stale last_content (which may be
            # a lifecycle marker or tool summary rather than the real error).
            if returncode != 0:
                stderr_text = "\n".join(stderr_lines).strip()
                final_message = stderr_text or last_content or ""
            if not final_message:
                if returncode == 0:
                    final_message = f"{self._display_name} task completed."
                else:
                    final_message = f"{self._display_name} exited with code {returncode}."

            result_data: dict[str, Any] = {
                "subtype": ("error" if returncode != 0 else "success"),
                "returncode": returncode,
            }
            if current_handle is not None and current_handle.native_session_id:
                result_data["session_id"] = current_handle.native_session_id
            if returncode != 0:
                result_data["error_type"] = self._runtime_error_type

            yield AgentMessage(
                type="result",
                content=final_message,
                data=result_data,
                resume_handle=current_handle,
            )
        finally:
            if process is not None:
                if (
                    not process_finished
                    and not process_terminated
                    and getattr(process, "returncode", None) is None
                ):
                    await self._terminate_process(process)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and collect all messages into a result.

        Convenience wrapper around :meth:`execute_task` that
        accumulates every yielded message and returns a single
        :class:`~ouroboros.core.types.Result`.

        Args:
            prompt: Primary task prompt text.
            tools: Optional tool names injected into the prompt.
            system_prompt: Optional system instructions prepended to
                the prompt.
            resume_handle: Existing
                :class:`~ouroboros.orchestrator.adapter.RuntimeHandle`
                for session resume.
            resume_session_id: Alternative session ID string for
                resume.

        Returns:
            :class:`~ouroboros.core.types.Result` wrapping either a
            :class:`~ouroboros.orchestrator.adapter.TaskResult` or a
            :class:`~ouroboros.core.errors.ProviderError`.
        """
        messages: list[AgentMessage] = []
        final_message = ""
        success = True
        final_handle = resume_handle

        async for message in self.execute_task(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
        ):
            messages.append(message)
            if message.resume_handle is not None:
                final_handle = message.resume_handle
            if message.is_final:
                final_message = message.content
                success = not message.is_error

        if not success:
            return Result.err(
                ProviderError(
                    message=final_message,
                    provider=self._provider_name,
                    details={"messages": [m.content for m in messages]},
                )
            )

        return Result.ok(
            TaskResult(
                success=success,
                final_message=final_message,
                messages=tuple(messages),
                session_id=final_handle.native_session_id if final_handle else None,
                resume_handle=final_handle,
            )
        )


__all__ = ["OpenCodeRuntime", "SkillInterceptRequest"]
