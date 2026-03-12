"""Codex CLI runtime for Ouroboros orchestrator execution."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
import contextlib
from dataclasses import dataclass
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
        self._permission_mode = resolve_codex_permission_mode(
            permission_mode,
            default_mode="acceptEdits",
        )
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._skills_dir = self._resolve_skills_dir(skills_dir)
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or "codex"
        self._builtin_mcp_handlers: dict[str, Any] | None = None

        log.info(
            "codex_cli_runtime.initialized",
            cli_path=self._cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=self._cwd,
            skills_dir=str(self._skills_dir),
        )

    def _resolve_cli_path(self, cli_path: str | Path | None) -> str:
        """Resolve the Codex CLI path from explicit, config, or PATH values."""
        if cli_path is not None:
            candidate = str(Path(cli_path).expanduser())
        else:
            candidate = get_codex_cli_path() or shutil.which("codex") or "codex"

        path = Path(candidate).expanduser()
        if path.exists():
            return str(path)
        return candidate

    def _resolve_skills_dir(self, skills_dir: str | Path | None) -> Path:
        """Resolve the packaged skills directory used for intercept metadata."""
        if skills_dir is not None:
            return Path(skills_dir).expanduser()

        for parent in Path(__file__).resolve().parents:
            candidate = parent / "skills"
            if candidate.is_dir():
                return candidate

        return Path("skills")

    def _build_runtime_handle(self, session_id: str | None) -> RuntimeHandle | None:
        """Build a backend-neutral runtime handle for a Codex thread."""
        if not session_id:
            return None

        return RuntimeHandle(
            backend="codex_cli",
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
                key: self._preview_dispatch_value(item, limit=limit)
                for key, item in value.items()
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
            "fallback": "pass_through_to_codex",
        }

    def _get_builtin_mcp_handlers(self) -> dict[str, Any]:
        """Load and cache local Ouroboros MCP handlers for exact-prefix dispatch."""
        if self._builtin_mcp_handlers is None:
            from ouroboros.mcp.tools.definitions import get_ouroboros_tools

            self._builtin_mcp_handlers = {
                handler.definition.name: handler
                for handler in get_ouroboros_tools(
                    runtime_backend="codex", llm_backend=self._llm_backend,
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
        del current_handle  # Intercepted MCP tools do not resume Codex CLI sessions.

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
        skill_md_path = self._skills_dir / skill_name / "SKILL.md"
        if not skill_md_path.is_file():
            return None

        try:
            frontmatter = self._load_skill_frontmatter(skill_md_path)
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.warning(
                "codex_cli_runtime.skill_intercept_frontmatter_invalid",
                skill=skill_name,
                path=str(skill_md_path),
                error=str(e),
            )
            return None

        normalized, validation_error = self._normalize_mcp_frontmatter(frontmatter)
        if normalized is None:
            warning_event = "codex_cli_runtime.skill_intercept_frontmatter_invalid"
            if validation_error and validation_error.startswith("missing required frontmatter key:"):
                warning_event = "codex_cli_runtime.skill_intercept_frontmatter_missing"

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
                "codex_cli_runtime.skill_intercept_dispatch_failed",
                **self._build_intercept_failure_context(intercept),
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            return None

        recoverable_error = self._extract_recoverable_dispatch_error(dispatched_messages)
        if recoverable_error is not None:
            log.warning(
                "codex_cli_runtime.skill_intercept_dispatch_failed",
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

        if self._model:
            command.extend(["--model", self._model])

        command.extend(
            build_codex_exec_permission_args(
                self._permission_mode,
                default_mode="acceptEdits",
            )
        )

        command.append(prompt)
        return command

    async def _collect_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
    ) -> list[str]:
        """Drain a subprocess stream without blocking the main event loop."""
        if stream is None:
            return []

        lines: list[str] = []
        while True:
            raw_line = await stream.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                lines.append(line)
        return lines

    def _parse_json_event(self, line: str) -> dict[str, Any] | None:
        """Parse a JSONL event line, returning None for non-JSON output."""
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None

        return event if isinstance(event, dict) else None

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
            parts: list[str] = []
            for key in preferred_keys:
                if key in value:
                    text = self._extract_text(value[key])
                    if text:
                        parts.append(text)
            if parts:
                return "\n".join(parts)

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
                handle = self._build_runtime_handle(thread_id)
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
                return [AgentMessage(type="assistant", content=content, resume_handle=current_handle)]

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
                        content=f"Calling tool: WebSearch: {query}" if query else "Calling tool: WebSearch",
                        handle=current_handle,
                    )
                ]

            if item_type == "todo_list":
                content = self._extract_text(item)
                if not content:
                    return []
                return [AgentMessage(type="assistant", content=content, resume_handle=current_handle)]

            if item_type == "error":
                content = self._extract_text(item) or "Codex CLI reported an error"
                return [
                    AgentMessage(
                        type="assistant",
                        content=content,
                        data={"subtype": "runtime_error"},
                        resume_handle=current_handle,
                    )
                ]

            return []

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

    async def execute_task(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task via Codex CLI and stream normalized messages."""
        current_handle = resume_handle or self._build_runtime_handle(resume_session_id)
        intercepted_messages = await self._maybe_dispatch_skill_intercept(prompt, current_handle)
        if intercepted_messages is not None:
            for message in intercepted_messages:
                if message.resume_handle is not None:
                    current_handle = message.resume_handle
                yield message
            return

        output_fd, output_path_str = tempfile.mkstemp(prefix="ouroboros-codex-", suffix=".txt")
        os.close(output_fd)
        output_path = Path(output_path_str)

        composed_prompt = self._compose_prompt(prompt, system_prompt, tools)
        command = self._build_command(
            output_last_message_path=str(output_path),
            prompt=composed_prompt,
            resume_session_id=current_handle.native_session_id if current_handle else None,
        )

        log.info(
            "codex_cli_runtime.task_started",
            command=command,
            cwd=self._cwd,
            has_resume_handle=current_handle is not None,
        )

        stderr_lines: list[str] = []
        last_content = ""

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=self._cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            yield AgentMessage(
                type="result",
                content=f"Codex CLI not found: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            output_path.unlink(missing_ok=True)
            return
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to start Codex CLI: {e}",
                data={"subtype": "error", "error_type": type(e).__name__},
                resume_handle=current_handle,
            )
            output_path.unlink(missing_ok=True)
            return

        stderr_task = asyncio.create_task(self._collect_stream_lines(process.stderr))

        try:
            if process.stdout is not None:
                while True:
                    raw_line = await process.stdout.readline()
                    if not raw_line:
                        break

                    line = raw_line.decode("utf-8", errors="replace").rstrip()
                    if not line:
                        continue

                    event = self._parse_json_event(line)
                    if event is None:
                        continue

                    for message in self._convert_event(event, current_handle):
                        if message.resume_handle is not None:
                            current_handle = message.resume_handle
                        if message.content:
                            last_content = message.content
                        yield message

            returncode = await process.wait()
            stderr_lines = await stderr_task
            final_message = self._load_output_message(output_path)
            if not final_message:
                final_message = last_content or "\n".join(stderr_lines).strip()
            if not final_message:
                if returncode == 0:
                    final_message = "Codex CLI task completed."
                else:
                    final_message = f"Codex CLI exited with code {returncode}."

            data: dict[str, Any] = {
                "subtype": "success" if returncode == 0 else "error",
                "returncode": returncode,
            }
            if current_handle is not None and current_handle.native_session_id:
                data["session_id"] = current_handle.native_session_id
            if returncode != 0:
                data["error_type"] = "CodexCliError"

            yield AgentMessage(
                type="result",
                content=final_message,
                data=data,
                resume_handle=current_handle,
            )
        finally:
            if not stderr_task.done():
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
                    provider="codex_cli",
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
