"""Hermes Agent adapter for Ouroboros orchestrator.

This module provides a HermesAgentRuntime that satisfies the AgentRuntime protocol
by shelling out to the Hermes CLI.
"""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
import contextlib
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import os
from pathlib import Path
import re
import shlex
import shutil
from typing import Any

import yaml

from ouroboros.codex import resolve_packaged_codex_skill_path
from ouroboros.config import get_hermes_cli_path
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    AgentRuntime,
    RuntimeHandle,
)

log = get_logger(__name__)

_SKILL_COMMAND_PATTERN = re.compile(
    r"^\s*(?:(?P<ooo_prefix>ooo)\s+(?P<ooo_skill>[a-z0-9][a-z0-9_-]*)|"
    r"(?P<slash_prefix>/ouroboros:)(?P<slash_skill>[a-z0-9][a-z0-9_-]*))"
    r"(?:\s+(?P<remainder>.*))?$",
    re.IGNORECASE,
)
_MCP_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"

# Hermes session ID format: YYYYMMDD_HHMMSS_xxxxxx
_HERMES_SESSION_ID_PATTERN = re.compile(r"^session_id:\s+(?P<session_id>\d{8}_\d{6}_[a-f0-9]+)\s*$")
_REASONING_HEADER_PREFIX = "┌─ Reasoning"
_REASONING_BOX_PREFIXES = ("│", "├", "└")
_HERMES_BANNER_LINE_PATTERN = re.compile(r"^[╭┌].*Hermes.*[╮┐]$")


def _strip_reasoning_prelude(content: str) -> str:
    """Remove Hermes quiet-mode reasoning decorations from leading output."""
    lines = content.splitlines()
    first_nonempty_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_nonempty_index is None:
        return ""

    header_line = lines[first_nonempty_index]
    if _HERMES_BANNER_LINE_PATTERN.fullmatch(header_line):
        return "\n".join(lines[first_nonempty_index + 1 :]).strip()

    if not header_line.startswith(_REASONING_HEADER_PREFIX):
        return content.strip()

    index = first_nonempty_index + 1
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith(_REASONING_BOX_PREFIXES):
            index += 1
            continue
        break

    return "\n".join(lines[index:]).strip()


def _parse_quiet_output(output: str) -> tuple[str, str | None]:
    """Extract the user-facing content and session id from Hermes quiet output."""
    session_id: str | None = None
    content_lines: list[str] = []

    for line in output.splitlines():
        match = _HERMES_SESSION_ID_PATTERN.fullmatch(line.strip())
        if match is not None and session_id is None:
            session_id = match.group("session_id")
            continue
        content_lines.append(line)

    content = "\n".join(content_lines)
    return _strip_reasoning_prelude(content), session_id


@dataclass(frozen=True, slots=True)
class SkillInterceptRequest:
    """Metadata for a deterministic MCP skill intercept."""

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


class HermesCliRuntime(AgentRuntime):
    """Orchestrator runtime that executes tasks via the Hermes CLI."""

    _runtime_handle_backend = "hermes_cli"
    _runtime_backend = "hermes"
    _default_cli_name = "hermes"
    _log_namespace = "hermes_cli_runtime"
    _default_llm_backend = "claude_code"
    _display_name = "Hermes CLI"
    _process_shutdown_timeout_seconds = 5.0
    _max_ouroboros_depth = 5
    _startup_output_timeout_seconds = 60.0
    _stdout_idle_timeout_seconds = 300.0
    _max_stderr_lines = 512

    def __init__(
        self,
        *,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = permission_mode or "default"
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._skills_dir = Path(skills_dir).expanduser() if skills_dir else None
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or self._default_llm_backend
        self._builtin_mcp_handlers: dict[str, Any] | None = None

        log.info(
            f"{self._log_namespace}.initialized",
            cli_path=self._cli_path,
            permission_mode=self._permission_mode,
            model=model,
            cwd=self._cwd,
        )

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the Hermes CLI path."""
        if cli_path is not None:
            return str(Path(cli_path).expanduser())

        configured = get_hermes_cli_path()
        if configured:
            return configured

        return shutil.which(self._default_cli_name) or self._default_cli_name

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for child runtime processes."""
        env = os.environ.copy()
        for key in ("OUROBOROS_AGENT_RUNTIME", "OUROBOROS_LLM_BACKEND"):
            env.pop(key, None)

        try:
            depth = int(env.get("_OUROBOROS_DEPTH", "0")) + 1
        except (ValueError, TypeError):
            depth = 1

        if depth > self._max_ouroboros_depth:
            msg = f"Maximum Ouroboros nesting depth ({self._max_ouroboros_depth}) exceeded"
            raise RuntimeError(msg)

        env["_OUROBOROS_DEPTH"] = str(depth)
        return env

    def _build_runtime_handle(
        self,
        session_id: str | None,
        current_handle: RuntimeHandle | None = None,
    ) -> RuntimeHandle | None:
        """Build a backend-neutral runtime handle for a Hermes thread."""
        if not session_id:
            return None

        if current_handle is not None:
            return replace(
                current_handle,
                native_session_id=session_id,
                updated_at=datetime.now(UTC).isoformat(),
            )

        return RuntimeHandle(
            backend=self._runtime_handle_backend,
            native_session_id=session_id,
            cwd=self._cwd,
            updated_at=datetime.now(UTC).isoformat(),
        )

    def _compose_prompt(
        self,
        prompt: str,
        system_prompt: str | None,
        tools: list[str] | None,
    ) -> str:
        """Compose a single prompt for Hermes."""
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

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
    ) -> AsyncIterator[str]:
        """Yield decoded lines from a subprocess stream with timeout guards."""
        if stream is None:
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
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
                    chunk = await asyncio.wait_for(stream.read(chunk_size), timeout=timeout_seconds)
            except TimeoutError as exc:
                phase = "startup" if not saw_chunk else "idle"
                raise TimeoutError(
                    f"{self._display_name} produced no stdout during {phase} "
                    f"window ({timeout_seconds:.0f}s)"
                ) from exc

            if not chunk:
                break

            saw_chunk = True
            buffer += decoder.decode(chunk)
            while True:
                newline_index = buffer.find("\n")
                if newline_index < 0:
                    break
                line = buffer[:newline_index]
                buffer = buffer[newline_index + 1 :]
                yield line.rstrip("\r")

        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer.rstrip("\r")

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        max_lines: int | None = None,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
    ) -> list[str]:
        """Drain a subprocess stream into a list of decoded lines."""
        if stream is None:
            return []

        lines: deque[str] = deque(maxlen=max_lines) if max_lines is not None else deque()
        async for line in self._iter_stream_lines(
            stream,
            first_chunk_timeout_seconds=first_chunk_timeout_seconds,
            chunk_timeout_seconds=chunk_timeout_seconds,
        ):
            if line:
                lines.append(line)
        return list(lines)

    async def _terminate_process(self, process: Any) -> None:
        """Best-effort subprocess shutdown used for cancellations and timeouts."""
        if getattr(process, "returncode", None) is not None:
            return

        terminate = getattr(process, "terminate", None)
        kill = getattr(process, "kill", None)
        wait = getattr(process, "wait", None)

        try:
            if callable(terminate):
                terminate()
            elif callable(kill):
                kill()
            else:
                return
        except ProcessLookupError:
            return

        if not callable(wait):
            return

        try:
            await asyncio.wait_for(wait(), timeout=self._process_shutdown_timeout_seconds)
            return
        except (ProcessLookupError, TimeoutError):
            pass

        if callable(kill):
            with contextlib.suppress(ProcessLookupError):
                kill()
            with contextlib.suppress(ProcessLookupError, TimeoutError):
                await asyncio.wait_for(wait(), timeout=self._process_shutdown_timeout_seconds)

    async def execute_task(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        handle: RuntimeHandle | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task via Hermes CLI."""

        # 1. Attempt deterministic skill dispatch before invoking Hermes
        intercepted_messages = await self._maybe_dispatch_skill_intercept(prompt, handle)
        if intercepted_messages:
            for message in intercepted_messages:
                yield message
            return

        full_prompt = self._compose_prompt(prompt, system_prompt, tools)

        args = [self._cli_path, "chat"]
        if handle and handle.native_session_id:
            args.extend(["--resume", handle.native_session_id])

        # Use quiet mode for programmatic output
        args.extend(["-Q", "--source", "tool"])

        if self._model:
            args.extend(["--model", self._model])

        args.extend(["-q", full_prompt])

        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._build_child_env(),
        )

        stdout_task = asyncio.create_task(
            self._collect_stream_lines(
                process.stdout,
                first_chunk_timeout_seconds=self._startup_output_timeout_seconds,
                chunk_timeout_seconds=self._stdout_idle_timeout_seconds,
            )
        )
        stderr_task = asyncio.create_task(
            self._collect_stream_lines(
                process.stderr,
                max_lines=self._max_stderr_lines,
            )
        )

        try:
            stdout_lines, stderr_lines = await asyncio.gather(stdout_task, stderr_task)
            returncode = await process.wait()
        except asyncio.CancelledError:
            await self._terminate_process(process)
            raise
        except TimeoutError as e:
            await self._terminate_process(process)
            for task in (stdout_task, stderr_task):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await task
            yield AgentMessage(
                type="result",
                content=f"Hermes execution failed:\n{e}",
                data={"subtype": "error", "error_type": "TimeoutError"},
                resume_handle=handle,
            )
            return

        output = "\n".join(stdout_lines).strip()
        error = "\n".join(stderr_lines).strip()

        if returncode != 0:
            failure_content = error or output or f"Hermes exited with code {returncode}"
            yield AgentMessage(
                type="result",
                content=f"Hermes execution failed:\n{failure_content}",
                data={"subtype": "error", "exit_code": returncode},
                resume_handle=handle,
            )
            return

        clean_content, session_id = _parse_quiet_output(output)

        new_handle = self._build_runtime_handle(session_id, handle)

        yield AgentMessage(
            type="result",
            content=clean_content,
            data={"subtype": "success", "session_id": session_id},
            resume_handle=new_handle,
        )

    # -- Skill Intercept & Dispatch -----------------------------------------

    async def _maybe_dispatch_skill_intercept(
        self,
        prompt: str,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Attempt deterministic skill dispatch before invoking Hermes."""
        intercept = self._resolve_skill_intercept(prompt)
        if intercept is None:
            return None

        dispatcher = self._skill_dispatcher or self._dispatch_skill_intercept_locally
        try:
            dispatched_messages = await dispatcher(intercept, current_handle)
        except Exception as e:
            log.warning(
                f"{self._log_namespace}.skill_intercept_dispatch_failed",
                **self._build_intercept_failure_context(intercept),
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            return None

        recoverable_error = self._extract_recoverable_dispatch_error(dispatched_messages)
        if recoverable_error is not None:
            log.warning(
                f"{self._log_namespace}.skill_intercept_dispatch_failed",
                **self._build_intercept_failure_context(intercept),
                error_type=recoverable_error.data.get("error_type"),
                error=recoverable_error.content,
                recoverable=True,
            )
            return None

        return dispatched_messages

    def _build_intercept_failure_context(
        self,
        intercept: SkillInterceptRequest,
    ) -> dict[str, Any]:
        """Build structured log context for intercept failures."""
        return {
            "skill": intercept.skill_name,
            "tool": intercept.mcp_tool,
            "command_prefix": intercept.command_prefix,
            "path": str(intercept.skill_path),
        }

    def _extract_recoverable_dispatch_error(
        self,
        dispatched_messages: tuple[AgentMessage, ...] | None,
    ) -> AgentMessage | None:
        """Identify final recoverable intercept failures that should fall through."""
        if not dispatched_messages:
            return None

        final_message = next(
            (
                message
                for message in reversed(dispatched_messages)
                if message.is_final and message.is_error
            ),
            None,
        )
        if final_message is None:
            return None

        data = final_message.data
        metadata_candidates = (
            data,
            data.get("meta") if isinstance(data.get("meta"), Mapping) else None,
            data.get("mcp_meta") if isinstance(data.get("mcp_meta"), Mapping) else None,
        )

        for metadata in metadata_candidates:
            if not isinstance(metadata, Mapping):
                continue
            if metadata.get("recoverable") is True:
                return final_message
            if metadata.get("is_retriable") is True or metadata.get("retriable") is True:
                return final_message

        if final_message.data.get("error_type") in {"MCPConnectionError", "MCPTimeoutError"}:
            return final_message

        return None

    def _resolve_skill_intercept(self, prompt: str) -> SkillInterceptRequest | None:
        """Resolve a deterministic MCP intercept request from an exact skill prefix."""
        match = _SKILL_COMMAND_PATTERN.match(prompt)
        if match is None:
            return None

        skill_key = (match.group("ooo_skill") or match.group("slash_skill") or "").lower()
        if not skill_key:
            return None

        command_prefix = (
            f"ooo {skill_key}"
            if match.group("ooo_skill") is not None
            else f"/ouroboros:{skill_key}"
        )
        try:
            with resolve_packaged_codex_skill_path(
                skill_key,
                skills_dir=self._skills_dir,
            ) as skill_md_path:
                frontmatter = self._load_skill_frontmatter(skill_md_path)
                resolved_skill_path = Path(str(skill_md_path))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.warning(
                f"{self._log_namespace}.skill_intercept_frontmatter_invalid",
                skill=skill_key,
                path=str(skill_md_path),
                error=str(e),
            )
            return None

        normalized, validation_error = self._normalize_mcp_frontmatter(frontmatter)
        if normalized is None:
            warning_event = f"{self._log_namespace}.skill_intercept_frontmatter_invalid"
            if validation_error and validation_error.startswith(
                "missing required frontmatter key:"
            ):
                warning_event = f"{self._log_namespace}.skill_intercept_frontmatter_missing"

            log.warning(
                warning_event,
                skill=skill_key,
                path=str(skill_md_path),
                error=validation_error,
            )
            return None

        mcp_tool, mcp_args = normalized
        first_arg = self._extract_first_argument(match.group("remainder"))

        return SkillInterceptRequest(
            skill_name=skill_key,
            command_prefix=command_prefix,
            prompt=prompt,
            skill_path=resolved_skill_path,
            mcp_tool=mcp_tool,
            mcp_args=self._resolve_dispatch_templates(mcp_args, first_argument=first_arg),
            first_argument=first_arg,
        )

    def _load_skill_frontmatter(self, skill_md_path: Path) -> dict[str, Any]:
        """Load YAML frontmatter from a packaged SKILL.md file."""
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
        """Validate and normalize MCP dispatch metadata from frontmatter."""
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
        if not self._is_valid_dispatch_mapping(raw_mcp_args):
            return None, "mcp_args must be a mapping with string keys and YAML-safe values"

        return (mcp_tool, self._clone_dispatch_value(raw_mcp_args)), None

    def _is_valid_dispatch_mapping(self, value: Any) -> bool:
        """Validate dispatch args are mapping-shaped and recursively serializable."""
        if not isinstance(value, Mapping):
            return False

        return all(
            isinstance(key, str) and bool(key.strip()) and self._is_valid_dispatch_value(item)
            for key, item in value.items()
        )

    def _is_valid_dispatch_value(self, value: Any) -> bool:
        """Validate a dispatch template value recursively."""
        if value is None or isinstance(value, str | int | float | bool):
            return True

        if isinstance(value, Mapping):
            return self._is_valid_dispatch_mapping(value)

        if isinstance(value, list | tuple):
            return all(self._is_valid_dispatch_value(item) for item in value)

        return False

    def _clone_dispatch_value(self, value: Any) -> Any:
        """Clone validated dispatch metadata into plain Python containers."""
        if isinstance(value, Mapping):
            return {key: self._clone_dispatch_value(item) for key, item in value.items()}

        if isinstance(value, list | tuple):
            return [self._clone_dispatch_value(item) for item in value]

        return value

    def _extract_first_argument(self, remainder: str | None) -> str | None:
        """Extract the first positional argument from the intercepted command."""
        if not remainder or not remainder.strip():
            return None
        try:
            args = shlex.split(remainder)
            return args[0] if args else None
        except Exception:
            return remainder.strip().split(maxsplit=1)[0]

    def _build_tool_message(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        content: str,
        handle: RuntimeHandle | None,
        extra_data: dict[str, Any] | None = None,
    ) -> AgentMessage:
        """Build the assistant message announcing an intercepted tool call."""
        data = {"tool_input": tool_input}
        if extra_data:
            data.update(extra_data)

        return AgentMessage(
            type="assistant",
            content=content,
            tool_name=tool_name,
            data=data,
            resume_handle=handle,
        )

    def _resolve_dispatch_templates(self, value: Any, *, first_argument: str | None) -> Any:
        """Resolve template placeholders."""
        if isinstance(value, str):
            if value == "$1":
                return first_argument or ""
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

    def _get_builtin_mcp_handlers(self) -> dict[str, Any]:
        """Load and cache local Ouroboros MCP handlers."""
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
        """Look up a local MCP handler by tool name."""
        return self._get_builtin_mcp_handlers().get(tool_name)

    def _build_tool_arguments(
        self,
        intercept: SkillInterceptRequest,
        current_handle: RuntimeHandle | None,
    ) -> dict[str, Any]:
        """Build MCP arguments, preserving interview sessions across turns."""
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
        """Attach interview session metadata to the runtime handle."""
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
    ) -> tuple[AgentMessage, ...]:
        """Dispatch intercept to local MCP handler."""
        handler = self._get_mcp_tool_handler(intercept.mcp_tool)
        if handler is None:
            raise LookupError(f"No local handler for tool: {intercept.mcp_tool}")

        tool_arguments = self._build_tool_arguments(intercept, current_handle)
        tool_result = await handler.handle(tool_arguments)
        if tool_result.is_err:
            error = tool_result.error
            error_data = {
                "subtype": "error",
                "error_type": type(error).__name__,
                "recoverable": True,
            }
            if hasattr(error, "is_retriable"):
                error_data["is_retriable"] = bool(error.is_retriable)
            if hasattr(error, "details") and isinstance(error.details, dict):
                error_data["meta"] = dict(error.details)

            return (
                self._build_tool_message(
                    tool_name=intercept.mcp_tool,
                    tool_input=tool_arguments,
                    content=f"Calling tool: {intercept.mcp_tool}",
                    handle=current_handle,
                    extra_data={
                        "command_prefix": intercept.command_prefix,
                        "skill_name": intercept.skill_name,
                    },
                ),
                AgentMessage(
                    type="result",
                    content=str(error),
                    data=error_data,
                    resume_handle=current_handle,
                ),
            )

        resolved = tool_result.value
        resume_handle = self._build_resume_handle(current_handle, intercept, resolved)
        result_text = resolved.text_content.strip() or f"{intercept.mcp_tool} completed."
        result_data: dict[str, Any] = {
            "subtype": "error" if resolved.is_error else "success",
            "tool_name": intercept.mcp_tool,
            "mcp_meta": dict(resolved.meta),
        }
        result_data.update(dict(resolved.meta))

        return (
            self._build_tool_message(
                tool_name=intercept.mcp_tool,
                tool_input=tool_arguments,
                content=f"Calling tool: {intercept.mcp_tool}",
                handle=resume_handle,
                extra_data={
                    "command_prefix": intercept.command_prefix,
                    "skill_name": intercept.skill_name,
                },
            ),
            AgentMessage(
                type="result",
                content=result_text,
                data=result_data,
                resume_handle=resume_handle,
            ),
        )

    async def execute_task_to_result(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        handle: RuntimeHandle | None = None,
        **kwargs: Any,
    ) -> Result[AgentMessage, RuntimeError]:
        """Execute a task and return the final message."""
        last_message = None
        async for message in self.execute_task(
            prompt,
            system_prompt=system_prompt,
            tools=tools,
            handle=handle,
            **kwargs,
        ):
            last_message = message

        if (
            last_message
            and last_message.type == "result"
            and last_message.data.get("subtype") == "success"
        ):
            return Result.ok(last_message)

        return Result.err(RuntimeError(last_message.content if last_message else "Task failed"))
