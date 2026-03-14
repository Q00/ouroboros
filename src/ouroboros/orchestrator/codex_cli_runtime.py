"""Codex CLI runtime for Ouroboros orchestrator execution."""

from __future__ import annotations

import asyncio
import codecs
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
import tempfile
from typing import Any

import yaml

from ouroboros.codex import resolve_packaged_codex_skill_path
from ouroboros.codex_permissions import (
    build_codex_exec_permission_args,
    resolve_codex_permission_mode,
)
from ouroboros.config import get_codex_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle, TaskResult

log = get_logger(__name__)

_TOP_LEVEL_EVENT_MESSAGE_TYPES: dict[str, str] = {
    "error": "assistant",
}

_SKILL_COMMAND_PATTERN = re.compile(
    r"^\s*(?:(?P<ooo_prefix>ooo)\s+(?P<ooo_skill>[a-z0-9][a-z0-9_-]*)|"
    r"(?P<slash_prefix>/ouroboros:)(?P<slash_skill>[a-z0-9][a-z0-9_-]*))"
    r"(?:\s+(?P<remainder>.*))?$",
    re.IGNORECASE,
)
_MCP_TOOL_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_LINE_BUFFER_BYTES = 50 * 1024 * 1024  # 50 MB


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


class CodexCliRuntime:
    """Agent runtime that shells out to the locally installed Codex CLI."""

    _runtime_handle_backend = "codex_cli"
    _runtime_backend = "codex"
    _provider_name = "codex_cli"
    _runtime_error_type = "CodexCliError"
    _log_namespace = "codex_cli_runtime"
    _display_name = "Codex CLI"
    _default_cli_name = "codex"
    _default_llm_backend = "codex"
    _tempfile_prefix = "ouroboros-codex-"
    _skills_package_uri = "packaged://ouroboros.codex/skills"
    _process_shutdown_timeout_seconds = 5.0
    _max_resume_retries = 3

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
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = self._resolve_permission_mode(permission_mode)
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
            skills_dir=(
                str(self._skills_dir) if self._skills_dir is not None else self._skills_package_uri
            ),
        )

    def _resolve_permission_mode(self, permission_mode: str | None) -> str:
        """Validate and normalize the runtime permission mode."""
        return resolve_codex_permission_mode(
            permission_mode,
            default_mode="acceptEdits",
        )

    def _build_permission_args(self) -> list[str]:
        """Translate the configured permission mode into backend CLI flags."""
        return build_codex_exec_permission_args(
            self._permission_mode,
            default_mode="acceptEdits",
        )

    def _get_configured_cli_path(self) -> str | None:
        """Resolve an explicit CLI path from config helpers when available."""
        return get_codex_cli_path()

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the Codex CLI path from explicit, config, or PATH values."""
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = (
                self._get_configured_cli_path()
                or shutil.which(self._default_cli_name)
                or self._default_cli_name
            )

        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
        return candidate

    def _resolve_skills_dir(self, skills_dir: str | Path | None) -> Path | None:
        """Resolve an optional explicit skill override directory for intercept metadata."""
        if skills_dir is None:
            return None
        return Path(skills_dir).expanduser()

    def _normalize_model(self, model: str | None) -> str | None:
        """Normalize backend model values before passing them to the CLI."""
        if model is None:
            return None

        candidate = model.strip()
        if not candidate or candidate == "default":
            return None
        return candidate

    def _build_runtime_handle(
        self,
        session_id: str | None,
        current_handle: RuntimeHandle | None = None,
    ) -> RuntimeHandle | None:
        """Build a backend-neutral runtime handle for a Codex thread."""
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

        # current_handle is guaranteed None here (early return above).
        return RuntimeHandle(
            backend=self._runtime_handle_backend,
            kind="agent_runtime",
            native_session_id=session_id,
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            updated_at=datetime.now(UTC).isoformat(),
        )

    def _compose_prompt(
        self,
        prompt: str,
        system_prompt: str | None,
        tools: list[str] | None,
    ) -> str:
        """Compose a single prompt for Codex CLI exec mode."""
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

    def _extract_first_argument(self, remainder: str | None) -> str | None:
        """Extract the first positional argument from the intercepted command."""
        if not remainder or not remainder.strip():
            return None

        try:
            args = shlex.split(remainder)
        except ValueError:
            args = remainder.strip().split(maxsplit=1)

        return args[0] if args else None

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

    def _resolve_dispatch_templates(
        self,
        value: Any,
        *,
        first_argument: str | None,
    ) -> Any:
        """Resolve supported template placeholders into concrete MCP payload values."""
        if isinstance(value, str):
            if value == "$1":
                # Return empty string instead of None to avoid Path("None") downstream
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

    def _truncate_log_value(self, value: str | None, *, limit: int) -> str | None:
        """Trim long string values before including them in warning logs."""
        if value is None or len(value) <= limit:
            return value
        return f"{value[: limit - 3]}..."

    def _preview_dispatch_value(self, value: Any, *, limit: int = 160) -> Any:
        """Build a bounded preview of resolved MCP arguments for diagnostics."""
        if isinstance(value, str):
            return self._truncate_log_value(value, limit=limit)

        if isinstance(value, Mapping):
            return {
                key: self._preview_dispatch_value(item, limit=limit) for key, item in value.items()
            }

        if isinstance(value, list | tuple):
            return [self._preview_dispatch_value(item, limit=limit) for item in value]

        return value

    def _build_intercept_failure_context(
        self,
        intercept: SkillInterceptRequest,
    ) -> dict[str, Any]:
        """Collect diagnostic fields for intercept failures that fall through."""
        return {
            "skill": intercept.skill_name,
            "tool": intercept.mcp_tool,
            "command_prefix": intercept.command_prefix,
            "path": str(intercept.skill_path),
            "first_argument": self._truncate_log_value(intercept.first_argument, limit=120),
            "prompt_preview": self._truncate_log_value(intercept.prompt, limit=200),
            "mcp_arg_keys": tuple(sorted(intercept.mcp_args)),
            "mcp_args_preview": self._preview_dispatch_value(intercept.mcp_args),
            "fallback": f"pass_through_to_{self._runtime_backend}",
        }

    def _get_builtin_mcp_handlers(self) -> dict[str, Any]:
        """Load and cache local Ouroboros MCP handlers for exact-prefix dispatch."""
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

    async def _dispatch_skill_intercept_locally(
        self,
        intercept: SkillInterceptRequest,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Dispatch an exact-prefix intercept to the matching local MCP handler."""
        del current_handle  # Intercepted MCP tools do not resume backend CLI sessions.

        handler = self._get_mcp_tool_handler(intercept.mcp_tool)
        if handler is None:
            raise LookupError(f"No local handler registered for tool: {intercept.mcp_tool}")

        tool_result = await handler.handle(dict(intercept.mcp_args))
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
                    tool_input=dict(intercept.mcp_args),
                    content=f"Calling tool: {intercept.mcp_tool}",
                    handle=None,
                    extra_data={
                        "command_prefix": intercept.command_prefix,
                        "skill_name": intercept.skill_name,
                    },
                ),
                AgentMessage(
                    type="result",
                    content=str(error),
                    data=error_data,
                ),
            )

        resolved_result = tool_result.value
        result_text = resolved_result.text_content.strip() or f"{intercept.mcp_tool} completed."
        result_data: dict[str, Any] = {
            "subtype": "error" if resolved_result.is_error else "success",
            "tool_name": intercept.mcp_tool,
            "mcp_meta": dict(resolved_result.meta),
        }
        result_data.update(dict(resolved_result.meta))

        return (
            self._build_tool_message(
                tool_name=intercept.mcp_tool,
                tool_input=dict(intercept.mcp_args),
                content=f"Calling tool: {intercept.mcp_tool}",
                handle=None,
                extra_data={
                    "command_prefix": intercept.command_prefix,
                    "skill_name": intercept.skill_name,
                },
            ),
            AgentMessage(
                type="result",
                content=result_text,
                data=result_data,
            ),
        )

    def _resolve_packaged_skill(self, skill_name: str):
        """Resolve the packaged SKILL.md path for a backend command prefix."""
        return resolve_packaged_codex_skill_path(
            skill_name,
            skills_dir=self._skills_dir,
        )

    def _resolve_skill_intercept(self, prompt: str) -> SkillInterceptRequest | None:
        """Resolve a deterministic MCP intercept request from an exact skill prefix."""
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
            with self._resolve_packaged_skill(skill_name) as skill_md_path:
                frontmatter = self._load_skill_frontmatter(skill_md_path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.warning(
                f"{self._log_namespace}.skill_intercept_frontmatter_invalid",
                skill=skill_name,
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
                skill=skill_name,
                path=str(skill_md_path),
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

    async def _maybe_dispatch_skill_intercept(
        self,
        prompt: str,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Attempt deterministic skill dispatch before invoking Codex."""
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

    def _build_command(
        self,
        output_last_message_path: str,
        prompt: str,
        *,
        resume_session_id: str | None = None,
    ) -> list[str]:
        """Build the Codex CLI command for a new or resumed session."""
        command = [self._cli_path, "exec"]
        if resume_session_id:
            if not _SAFE_SESSION_ID_PATTERN.match(resume_session_id):
                raise ValueError(
                    f"Invalid resume_session_id: contains disallowed characters: "
                    f"{resume_session_id!r}"
                )
            command.extend(["resume", resume_session_id])

        command.extend(
            [
                "--json",
                "--skip-git-repo-check",
                "--output-last-message",
                output_last_message_path,
                "-C",
                self._cwd,
            ]
        )

        normalized_model = self._normalize_model(self._model)
        if normalized_model:
            command.extend(["--model", normalized_model])

        command.extend(self._build_permission_args())

        command.append(prompt)
        return command

    def _resolve_resume_session_id(
        self,
        current_handle: RuntimeHandle | None,
    ) -> str | None:
        """Resolve the backend-native session id used for CLI resume."""
        if current_handle is None:
            return None
        return current_handle.native_session_id

    def _requires_process_stdin(self) -> bool:
        """Return True when the runtime needs a writable stdin pipe."""
        return False

    async def _handle_runtime_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
        process: Any,
    ) -> tuple[AgentMessage, ...]:
        """Handle runtime-specific stream events before generic normalization."""
        del event, current_handle, process
        return ()

    def _prepare_runtime_event(
        self,
        event: dict[str, Any],
        *,
        previous_handle: RuntimeHandle | None,
        current_handle: RuntimeHandle | None,
        session_rebound: bool,
    ) -> dict[str, Any]:
        """Allow runtimes to enrich parsed events before normalization."""
        del previous_handle, current_handle, session_rebound
        return event

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
    ) -> list[str]:
        """Drain a subprocess stream without blocking the main event loop."""
        if stream is None:
            return []

        lines: list[str] = []
        async for line in self._iter_stream_lines(stream):
            if line:
                lines.append(line)
        return lines

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
    ) -> AsyncIterator[str]:
        """Yield decoded lines without relying on StreamReader.readline().

        Codex can emit JSONL events larger than the default asyncio stream limit.
        Reading fixed-size chunks avoids ``LimitOverrunError`` on oversized lines.
        """
        if stream is None:
            return

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        buffer_byte_estimate = 0

        while True:
            chunk = await stream.read(chunk_size)
            if not chunk:
                break

            decoded = decoder.decode(chunk)
            buffer += decoded
            # Track byte size incrementally: worst-case 4 bytes per char (UTF-8).
            buffer_byte_estimate += len(decoded) * 4
            if buffer_byte_estimate > _MAX_LINE_BUFFER_BYTES:
                log.error(
                    f"{self._log_namespace}.line_buffer_overflow",
                    buffer_size=len(buffer),
                    limit=_MAX_LINE_BUFFER_BYTES,
                )
                raise ProviderError(
                    f"JSONL line buffer exceeded {_MAX_LINE_BUFFER_BYTES} bytes"
                )
            while True:
                newline_index = buffer.find("\n")
                if newline_index < 0:
                    break

                line = buffer[:newline_index]
                buffer = buffer[newline_index + 1 :]
                # Recalculate estimate after draining consumed lines.
                buffer_byte_estimate = len(buffer) * 4
                yield line.rstrip("\r")

        buffer += decoder.decode(b"", final=True)
        if buffer:
            yield buffer.rstrip("\r")

    async def _terminate_process(self, process: Any) -> None:
        """Best-effort subprocess shutdown used when task consumption is cancelled."""
        if getattr(process, "returncode", None) is not None:
            return

        await self._close_process_stdin(process)

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
                error_type=type(exc).__name__,
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
        except Exception as exc:
            log.warning(
                f"{self._log_namespace}.process_wait_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        if not callable(kill):
            return

        try:
            kill()
        except ProcessLookupError:
            return
        except Exception as exc:
            log.warning(
                f"{self._log_namespace}.process_kill_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return

        with contextlib.suppress(asyncio.TimeoutError, ProcessLookupError, Exception):
            await asyncio.wait_for(
                process.wait(),
                timeout=self._process_shutdown_timeout_seconds,
            )

    async def _close_process_stdin(self, process: Any) -> None:
        """Best-effort stdin shutdown for runtimes that keep a writable pipe open."""
        stdin = getattr(process, "stdin", None)
        if stdin is None:
            return

        close = getattr(stdin, "close", None)
        if callable(close):
            with contextlib.suppress(BrokenPipeError, ConnectionResetError, OSError, RuntimeError):
                close()

        wait_closed = getattr(stdin, "wait_closed", None)
        if callable(wait_closed):
            with contextlib.suppress(
                BrokenPipeError,
                ConnectionResetError,
                OSError,
                RuntimeError,
                asyncio.CancelledError,
            ):
                await wait_closed()

    async def _observe_bound_runtime_handle(
        self,
        control_state: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a live runtime snapshot for the latest bound handle."""
        observed_handle = control_state.get("handle")
        if isinstance(observed_handle, RuntimeHandle):
            snapshot = observed_handle.snapshot()
        else:
            snapshot = {}

        process_id = control_state.get("process_id")
        if isinstance(process_id, int):
            snapshot["process_id"] = process_id

        returncode = control_state.get("returncode")
        if isinstance(returncode, int):
            snapshot["returncode"] = returncode

        runtime_status = control_state.get("runtime_status")
        if isinstance(runtime_status, str) and runtime_status:
            snapshot["lifecycle_state"] = runtime_status
        elif isinstance(returncode, int):
            snapshot["lifecycle_state"] = "completed" if returncode == 0 else "failed"

        if control_state.get("terminated") is True:
            snapshot["terminated"] = True
            snapshot["can_terminate"] = False

        return snapshot

    async def _terminate_bound_runtime_handle(
        self,
        process: Any,
        control_state: dict[str, Any],
    ) -> bool:
        """Terminate the live process behind a bound runtime handle."""
        if control_state.get("terminated") is True:
            return False

        process_returncode = getattr(process, "returncode", None)
        if process_returncode is not None:
            control_state["returncode"] = process_returncode
            control_state["runtime_status"] = "completed" if process_returncode == 0 else "failed"
            return False

        control_state["runtime_status"] = "terminating"
        await self._terminate_process(process)

        process_returncode = getattr(process, "returncode", None)
        control_state["terminated"] = True
        if isinstance(process_returncode, int):
            control_state["returncode"] = process_returncode
            if process_returncode < 0:
                control_state["runtime_status"] = "terminated"
            else:
                control_state["runtime_status"] = (
                    "completed" if process_returncode == 0 else "failed"
                )
        else:
            control_state["runtime_status"] = "terminated"

        return True

    def _bind_runtime_handle_controls(
        self,
        handle: RuntimeHandle | None,
        *,
        process: Any,
        control_state: dict[str, Any],
    ) -> RuntimeHandle | None:
        """Attach live observe/terminate callbacks to a runtime handle."""
        if handle is None:
            return None

        effective_handle = handle
        returncode = control_state.get("returncode")
        if control_state.get("terminated") is True and handle.lifecycle_state not in {
            "cancelled",
            "terminated",
        }:
            metadata = dict(handle.metadata)
            metadata["runtime_event_type"] = "session.terminated"
            effective_handle = replace(
                handle,
                updated_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )
        elif (
            isinstance(returncode, int)
            and not handle.is_terminal
            and handle.lifecycle_state not in {"cancelled", "terminated"}
        ):
            metadata = dict(handle.metadata)
            metadata["runtime_event_type"] = "run.completed" if returncode == 0 else "run.failed"
            effective_handle = replace(
                handle,
                updated_at=datetime.now(UTC).isoformat(),
                metadata=metadata,
            )

        if control_state.get("returncode") is None and control_state.get("terminated") is not True:
            control_state["runtime_status"] = effective_handle.lifecycle_state

        async def _observe(_handle: RuntimeHandle) -> dict[str, Any]:
            return await self._observe_bound_runtime_handle(control_state)

        async def _terminate(_handle: RuntimeHandle) -> bool:
            return await self._terminate_bound_runtime_handle(process, control_state)

        bound_handle = effective_handle.bind_controls(
            observe_callback=_observe,
            terminate_callback=_terminate,
        )
        control_state["handle"] = bound_handle
        return bound_handle

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a JSONL event line, returning None for non-JSON output."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        return event if isinstance(event, dict) else None

    def _extract_event_session_id(self, event: Mapping[str, Any]) -> str | None:
        """Extract a backend-native session identifier from a runtime event."""
        for key in ("thread_id", "session_id", "native_session_id", "run_id"):
            value = event.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        session = event.get("session")
        if isinstance(session, Mapping):
            value = session.get("id")
            if isinstance(value, str) and value.strip():
                return value.strip()

        return None

    def _extract_text(self, value: object) -> str:
        """Extract text recursively from a nested JSON-like structure."""
        if isinstance(value, str):
            return value.strip()

        if isinstance(value, list):
            parts = [self._extract_text(item) for item in value]
            return "\n".join(part for part in parts if part)

        if isinstance(value, dict):
            preferred_keys = (
                "text",
                "message",
                "output_text",
                "reasoning",
                "content",
                "summary",
                "title",
                "body",
                "details",
            )
            dict_parts: list[str] = []
            for key in preferred_keys:
                if key in value:
                    text = self._extract_text(value[key])
                    if text:
                        dict_parts.append(text)
            if dict_parts:
                return "\n".join(dict_parts)

            fallback_parts = [self._extract_text(item) for item in value.values()]
            return "\n".join(part for part in fallback_parts if part)

        return ""

    def _extract_command(self, item: dict[str, Any]) -> str:
        """Extract a shell command from a command execution item."""
        candidates = [
            item.get("command"),
            item.get("cmd"),
            item.get("command_line"),
        ]
        if isinstance(item.get("input"), dict):
            candidates.extend(
                [
                    item["input"].get("command"),
                    item["input"].get("cmd"),
                ]
            )

        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
            if isinstance(candidate, list) and candidate:
                return shlex.join(str(part) for part in candidate)
        return ""

    def _extract_tool_input(self, item: dict[str, Any]) -> dict[str, Any]:
        """Extract tool input payload from a Codex event item."""
        for key in ("input", "arguments", "args"):
            candidate = item.get(key)
            if isinstance(candidate, dict):
                return candidate
        return {}

    def _extract_path(self, item: dict[str, Any]) -> str:
        """Extract a file path from a file change event."""
        candidates: list[object] = [
            item.get("path"),
            item.get("file_path"),
            item.get("target_file"),
        ]

        if isinstance(item.get("changes"), list):
            for change in item["changes"]:
                if isinstance(change, dict):
                    candidates.extend(
                        [
                            change.get("path"),
                            change.get("file_path"),
                        ]
                    )

        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return ""

    def _build_tool_message(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        content: str,
        handle: RuntimeHandle | None,
        extra_data: dict[str, Any] | None = None,
    ) -> AgentMessage:
        data = {"tool_input": tool_input, **(extra_data or {})}
        return AgentMessage(
            type="assistant",
            content=content,
            tool_name=tool_name,
            data=data,
            resume_handle=handle,
        )

    def _convert_event(
        self,
        event: dict[str, Any],
        current_handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        """Convert a Codex JSON event into normalized AgentMessage values."""
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return []

        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            if isinstance(thread_id, str):
                handle = self._build_runtime_handle(thread_id, current_handle)
                return [
                    AgentMessage(
                        type="system",
                        content=f"Session initialized: {thread_id}",
                        data={"subtype": "init", "session_id": thread_id},
                        resume_handle=handle,
                    )
                ]
            return []

        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                return []

            item_type = item.get("type")
            if not isinstance(item_type, str):
                return []

            if item_type == "agent_message":
                content = self._extract_text(item)
                if not content:
                    return []
                return [
                    AgentMessage(type="assistant", content=content, resume_handle=current_handle)
                ]

            if item_type == "reasoning":
                content = self._extract_text(item)
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

            if item_type == "command_execution":
                command = self._extract_command(item)
                if not command:
                    return []
                return [
                    self._build_tool_message(
                        tool_name="Bash",
                        tool_input={"command": command},
                        content=f"Calling tool: Bash: {command}",
                        handle=current_handle,
                    )
                ]

            if item_type == "mcp_tool_call":
                tool_name = item.get("name") if isinstance(item.get("name"), str) else "mcp_tool"
                tool_input = self._extract_tool_input(item)
                return [
                    self._build_tool_message(
                        tool_name=tool_name,
                        tool_input=tool_input,
                        content=f"Calling tool: {tool_name}",
                        handle=current_handle,
                    )
                ]

            if item_type == "file_change":
                file_path = self._extract_path(item)
                if not file_path:
                    return []
                return [
                    self._build_tool_message(
                        tool_name="Edit",
                        tool_input={"file_path": file_path},
                        content=f"Calling tool: Edit: {file_path}",
                        handle=current_handle,
                    )
                ]

            if item_type == "web_search":
                query = self._extract_text(item)
                return [
                    self._build_tool_message(
                        tool_name="WebSearch",
                        tool_input={"query": query},
                        content=f"Calling tool: WebSearch: {query}"
                        if query
                        else "Calling tool: WebSearch",
                        handle=current_handle,
                    )
                ]

            if item_type == "todo_list":
                content = self._extract_text(item)
                if not content:
                    return []
                return [
                    AgentMessage(type="assistant", content=content, resume_handle=current_handle)
                ]

            if item_type == "error":
                content = self._extract_text(item) or f"{self._display_name} reported an error"
                return [
                    AgentMessage(
                        type="assistant",
                        content=content,
                        data={"subtype": "runtime_error"},
                        resume_handle=current_handle,
                    )
                ]

            return []

        # Handle turn-level lifecycle events from Codex CLI.
        # ``turn.failed`` is emitted when the backend API call itself fails
        # (e.g. network sandbox blocking outbound connections).  Without
        # explicit handling the event is silently dropped, leaving the
        # orchestrator session stuck in "running" forever.
        if event_type == "turn.failed":
            error_obj = event.get("error", {})
            error_msg = (
                error_obj.get("message", "") if isinstance(error_obj, dict) else str(error_obj)
            ) or f"{self._display_name} turn failed"
            log.error(
                f"{self._log_namespace}.turn_failed",
                error=error_msg,
            )
            return [
                AgentMessage(
                    type="result",
                    content=error_msg,
                    data={"subtype": "error", "error_type": "TurnFailed"},
                    resume_handle=current_handle,
                )
            ]

        if event_type == "turn.completed":
            return []  # benign lifecycle event; no action needed

        if event_type in _TOP_LEVEL_EVENT_MESSAGE_TYPES:
            content = self._extract_text(event)
            if not content:
                return []
            return [
                AgentMessage(
                    type=_TOP_LEVEL_EVENT_MESSAGE_TYPES[event_type],
                    content=content,
                    data={"subtype": event_type},
                    resume_handle=current_handle,
                )
            ]

        return []

    def _load_output_message(self, path: Path) -> str:
        """Load the final assistant message emitted by Codex, if any."""
        try:
            return path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _build_resume_recovery(
        self,
        *,
        attempted_resume_session_id: str | None,
        current_handle: RuntimeHandle | None,
        returncode: int,
        final_message: str,
        stderr_lines: list[str],
    ) -> tuple[RuntimeHandle | None, AgentMessage | None] | None:
        """Return a replacement-session recovery plan for resumable runtimes.

        Backends that can soft-recover a failed reconnect should override this
        hook and return a scrubbed handle plus an optional audit message. The
        default CLI runtime treats resume failures as terminal.
        """
        del attempted_resume_session_id, current_handle, returncode, final_message, stderr_lines
        return None

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        _resume_depth: int = 0,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task via Codex CLI and stream normalized messages."""
        # Note: CODEX_SANDBOX_NETWORK_DISABLED=1 does NOT necessarily mean
        # child codex exec will fail.  Codex may apply different seatbelt
        # profiles to MCP server children vs shell commands.  Log at debug
        # level for diagnostics only.
        if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
            log.debug(
                f"{self._log_namespace}.sandbox_env_detected",
                hint=(
                    "CODEX_SANDBOX_NETWORK_DISABLED=1 detected. "
                    "If child codex exec fails with network errors, "
                    "consider setting orchestrator.permission_mode = "
                    "'bypassPermissions' or running the MCP server "
                    "outside the sandbox."
                ),
            )

        current_handle = resume_handle or self._build_runtime_handle(resume_session_id)
        intercepted_messages = await self._maybe_dispatch_skill_intercept(prompt, current_handle)
        if intercepted_messages is not None:
            for message in intercepted_messages:
                if message.resume_handle is not None:
                    current_handle = message.resume_handle
                yield message
            return

        output_fd, output_path_str = tempfile.mkstemp(prefix=self._tempfile_prefix, suffix=".txt")
        os.close(output_fd)
        output_path = Path(output_path_str)

        composed_prompt = self._compose_prompt(prompt, system_prompt, tools)
        attempted_resume_session_id = self._resolve_resume_session_id(current_handle)
        command = self._build_command(
            output_last_message_path=str(output_path),
            prompt=composed_prompt,
            resume_session_id=attempted_resume_session_id,
        )

        log.info(
            f"{self._log_namespace}.task_started",
            command=command,
            cwd=self._cwd,
            has_resume_handle=current_handle is not None,
        )

        stderr_lines: list[str] = []
        last_content = ""
        yielded_final = False  # Track if a final (type="result") message was already emitted
        process: Any | None = None
        process_finished = False
        process_terminated = False
        control_state: dict[str, Any] | None = None
        stderr_task: asyncio.Task[list[str]] | None = None

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdin=(asyncio.subprocess.PIPE if self._requires_process_stdin() else None),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            yield AgentMessage(
                type="result",
                content=f"{self._display_name} not found: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            output_path.unlink(missing_ok=True)
            return
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to start {self._display_name}: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            output_path.unlink(missing_ok=True)
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
        stderr_task = asyncio.create_task(self._collect_stream_lines(process.stderr))

        try:
            if process.stdout is not None:
                async for line in self._iter_stream_lines(process.stdout):
                    if not line:
                        continue

                    event = self._parse_json_event(line)
                    if event is None:
                        continue

                    previous_handle = current_handle
                    session_rebound = False
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
                        session_rebound = (
                            previous_handle is not None
                            and previous_handle.native_session_id is not None
                            and previous_handle.native_session_id != event_session_id
                        )

                    event = self._prepare_runtime_event(
                        event,
                        previous_handle=previous_handle,
                        current_handle=current_handle,
                        session_rebound=session_rebound,
                    )

                    extra_messages = await self._handle_runtime_event(
                        event,
                        current_handle,
                        process,
                    )
                    for message in extra_messages:
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
                        yield message

                    for message in self._convert_event(event, current_handle):
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

            returncode = await process.wait()
            process_finished = True
            control_state["returncode"] = returncode
            if control_state.get("terminated") is True and returncode < 0:
                control_state["runtime_status"] = "terminated"
            else:
                control_state["runtime_status"] = "completed" if returncode == 0 else "failed"
            current_handle = self._bind_runtime_handle_controls(
                current_handle,
                process=process,
                control_state=control_state,
            )
            stderr_lines = await stderr_task

            # If a final result was already yielded during streaming
            # (e.g. from turn.failed handling), do not emit a second
            # result message that could incorrectly override the error.
            if yielded_final:
                return

            final_message = self._load_output_message(output_path)
            if not final_message:
                final_message = last_content or "\n".join(stderr_lines).strip()
            if not final_message:
                if returncode == 0:
                    final_message = f"{self._display_name} task completed."
                else:
                    final_message = f"{self._display_name} exited with code {returncode}."

            resume_recovery = self._build_resume_recovery(
                attempted_resume_session_id=attempted_resume_session_id,
                current_handle=current_handle,
                returncode=returncode,
                final_message=final_message,
                stderr_lines=stderr_lines,
            )
            if resume_recovery is not None:
                if _resume_depth >= self._max_resume_retries:
                    log.error(
                        f"{self._log_namespace}.resume_depth_exceeded",
                        depth=_resume_depth,
                        limit=self._max_resume_retries,
                    )
                    yield AgentMessage(
                        type="result",
                        content=(
                            f"{self._display_name} resume recovery exhausted "
                            f"after {self._max_resume_retries} attempts."
                        ),
                        data={"subtype": "error", "error_type": self._runtime_error_type},
                        resume_handle=current_handle,
                    )
                    return
                recovery_handle, recovery_message = resume_recovery
                if recovery_message is not None:
                    yield recovery_message
                async for message in self.execute_task(
                    prompt=prompt,
                    tools=tools,
                    system_prompt=system_prompt,
                    resume_handle=recovery_handle,
                    _resume_depth=_resume_depth + 1,
                ):
                    yield message
                return

            data: dict[str, Any] = {
                "subtype": "success" if returncode == 0 else "error",
                "returncode": returncode,
            }
            if current_handle is not None and current_handle.native_session_id:
                data["session_id"] = current_handle.native_session_id
            if returncode != 0:
                data["error_type"] = self._runtime_error_type

            yield AgentMessage(
                type="result",
                content=final_message,
                data=data,
                resume_handle=current_handle,
            )
        except asyncio.CancelledError:
            if process is not None:
                log.warning(f"{self._log_namespace}.task_cancelled", cwd=self._cwd)
                await self._terminate_process(process)
                process_terminated = True
                if control_state is not None:
                    control_state["terminated"] = True
                    control_state["returncode"] = getattr(process, "returncode", None)
                    control_state["runtime_status"] = "terminated"
            raise
        finally:
            if process is not None:
                if (
                    not process_finished
                    and not process_terminated
                    and getattr(process, "returncode", None) is None
                ):
                    await self._terminate_process(process)
                await self._close_process_stdin(process)
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            output_path.unlink(missing_ok=True)

    async def execute_task_to_result(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> Result[TaskResult, ProviderError]:
        """Execute a task and collect all messages into a TaskResult."""
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
                    details={"messages": [message.content for message in messages]},
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


__all__ = ["CodexCliRuntime", "SkillInterceptRequest"]
