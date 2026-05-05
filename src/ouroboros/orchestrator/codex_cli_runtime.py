"""Codex CLI runtime for Ouroboros orchestrator execution."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import AsyncIterator, Mapping
import contextlib
from dataclasses import replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from typing import Any

from ouroboros.codex.cli_policy import (
    DEFAULT_CODEX_CHILD_SESSION_ENV_KEYS,
    DEFAULT_MAX_OUROBOROS_DEPTH,
    build_codex_child_env,
    resolve_codex_cli_path,
)
from ouroboros.codex.runtime_profile import resolve_codex_profile
from ouroboros.codex_permissions import (
    build_codex_exec_permission_args,
    resolve_codex_permission_mode,
)
from ouroboros.config import get_codex_cli_path
from ouroboros.core.errors import ProviderError
from ouroboros.core.types import Result
from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeHandle,
    SkillDispatchHandler,
    TaskResult,
)
from ouroboros.providers.base import CompletionConfig
from ouroboros.providers.profiles import resolve_completion_profile
from ouroboros.router import (
    InvalidInputReason,
    InvalidSkill,
    NotHandled,
    Resolved,
    ResolveRequest,
    resolve_skill_dispatch,
)

log = get_logger(__name__)

_TOP_LEVEL_EVENT_MESSAGE_TYPES: dict[str, str] = {
    "error": "assistant",
}

_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"

_SAFE_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_LINE_BUFFER_BYTES = 50 * 1024 * 1024  # 50 MB
_RUNTIME_PROFILE_ROLE_PREFIX = "agent_runtime"
_RUNTIME_PROFILE_METADATA_KEYS = (
    "llm_profile",
    "ouroboros_profile",
    "agent_runtime_profile",
)
_RUNTIME_CODEX_PROFILE_METADATA_KEYS = (
    "codex_profile",
    "codex_cli_profile",
)


class CodexCliRuntime:
    """Agent runtime that shells out to the locally installed Codex CLI."""

    _runtime_handle_backend = "codex_cli"
    _runtime_backend = "codex"
    _requires_memory_gate = True
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
    _max_ouroboros_depth = DEFAULT_MAX_OUROBOROS_DEPTH
    _startup_output_timeout_seconds = 60.0
    _stdout_idle_timeout_seconds = 300.0
    _max_stderr_lines = 512
    _child_session_env_keys = DEFAULT_CODEX_CHILD_SESSION_ENV_KEYS

    def __init__(
        self,
        cli_path: str | Path | None = None,
        permission_mode: str | None = None,
        model: str | None = None,
        cwd: str | Path | None = None,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        llm_backend: str | None = None,
        runtime_profile: str | None = None,
    ) -> None:
        self._cli_path = self._resolve_cli_path(cli_path)
        self._permission_mode = self._resolve_permission_mode(permission_mode)
        self._model = model
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._skills_dir = self._resolve_skills_dir(skills_dir)
        self._skill_dispatcher = skill_dispatcher
        self._llm_backend = llm_backend or self._default_llm_backend
        self._runtime_profile = runtime_profile
        self._codex_profile = resolve_codex_profile(
            runtime_profile,
            logger=log,
            log_namespace=self._log_namespace,
        )
        self._builtin_mcp_handlers: dict[str, Any] | None = None

        log.info(
            f"{self._log_namespace}.initialized",
            cli_path=self._cli_path,
            permission_mode=permission_mode,
            model=model,
            cwd=self._cwd,
            runtime_profile=runtime_profile,
            codex_profile=self._codex_profile,
            skills_dir=(
                str(self._skills_dir) if self._skills_dir is not None else self._skills_package_uri
            ),
        )

    # -- AgentRuntime protocol properties ----------------------------------

    @property
    def runtime_backend(self) -> str:
        return self._runtime_handle_backend

    @property
    def llm_backend(self) -> str | None:
        return self._llm_backend

    @property
    def working_directory(self) -> str | None:
        return self._cwd

    @property
    def permission_mode(self) -> str | None:
        return self._permission_mode

    @property
    def cli_path(self) -> str:
        """Resolved Codex CLI path used for subprocess execution."""
        return self._cli_path

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
        resolution = resolve_codex_cli_path(
            explicit_cli_path=cli_path,
            configured_cli_path=self._get_configured_cli_path(),
            default_cli_name=self._default_cli_name,
            logger=log,
            log_namespace=self._log_namespace,
        )
        return resolution.cli_path

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

    def _runtime_profile_from_metadata(self, runtime_handle: RuntimeHandle | None) -> str | None:
        """Return an explicit provider-neutral profile from runtime metadata."""
        metadata = runtime_handle.metadata if runtime_handle is not None else {}
        for key in _RUNTIME_PROFILE_METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _codex_profile_from_metadata(self, runtime_handle: RuntimeHandle | None) -> str | None:
        """Return an explicit Codex-native profile from runtime metadata."""
        metadata = runtime_handle.metadata if runtime_handle is not None else {}
        for key in _RUNTIME_CODEX_PROFILE_METADATA_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _runtime_profile_role(self, runtime_handle: RuntimeHandle | None) -> str:
        """Build the logical role key used for agent-runtime profile lookup."""
        metadata = runtime_handle.metadata if runtime_handle is not None else {}
        role = metadata.get("llm_role") or metadata.get("agent_runtime_role")
        if isinstance(role, str) and role.strip():
            return role.strip()

        session_role = metadata.get("session_role")
        if isinstance(session_role, str) and session_role.strip():
            normalized_role = session_role.strip().lower().replace("-", "_")
            return f"{_RUNTIME_PROFILE_ROLE_PREFIX}_{normalized_role}"

        if runtime_handle is not None and runtime_handle.kind:
            normalized_kind = runtime_handle.kind.strip().lower().replace("-", "_")
            if normalized_kind == _RUNTIME_PROFILE_ROLE_PREFIX:
                return _RUNTIME_PROFILE_ROLE_PREFIX
            if normalized_kind.startswith(f"{_RUNTIME_PROFILE_ROLE_PREFIX}_"):
                return normalized_kind
            if normalized_kind:
                return f"{_RUNTIME_PROFILE_ROLE_PREFIX}_{normalized_kind}"

        return _RUNTIME_PROFILE_ROLE_PREFIX

    def _resolve_runtime_codex_config(
        self,
        runtime_handle: RuntimeHandle | None,
    ) -> tuple[str | None, str | None]:
        """Resolve model/profile settings for an agent-runtime task."""
        native_profile = self._codex_profile_from_metadata(runtime_handle)
        if native_profile:
            return None, native_profile

        profile_name = self._runtime_profile_from_metadata(runtime_handle)
        role = None if profile_name else self._runtime_profile_role(runtime_handle)
        resolved = resolve_completion_profile(
            CompletionConfig(model="default", profile=profile_name, role=role),
            backend="codex",
        )
        return resolved.config.model, resolved.backend_profile

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
        intercept: Resolved,
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

    def _build_tool_arguments(
        self,
        intercept: Resolved,
        current_handle: RuntimeHandle | None,
    ) -> dict[str, Any]:
        """Build the MCP argument payload for an intercepted skill."""
        if intercept.mcp_tool != "ouroboros_interview" or current_handle is None:
            return dict(intercept.mcp_args)

        session_id = current_handle.metadata.get(_INTERVIEW_SESSION_METADATA_KEY)
        if not isinstance(session_id, str) or not session_id.strip():
            return dict(intercept.mcp_args)

        # Resume turn: drop initial_context so InterviewHandler branches on
        # session_id instead of starting a new interview.
        arguments: dict[str, Any] = dict(intercept.mcp_args)
        arguments.pop("initial_context", None)
        arguments["session_id"] = session_id.strip()
        if intercept.first_argument is not None:
            arguments["answer"] = intercept.first_argument
        return arguments

    def _build_resume_handle(
        self,
        current_handle: RuntimeHandle | None,
        intercept: Resolved,
        tool_result: Any,
    ) -> RuntimeHandle | None:
        """Attach interview session metadata to the runtime handle."""
        if intercept.mcp_tool != "ouroboros_interview":
            return current_handle

        session_id = tool_result.meta.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            if session_id is not None:
                log.warning(
                    "codex_cli_runtime.resume_handle.invalid_session_id",
                    session_id_type=type(session_id).__name__,
                    session_id_value=repr(session_id),
                )
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
        intercept: Resolved,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Dispatch an exact-prefix intercept to the matching local MCP handler."""
        handler = self._get_mcp_tool_handler(intercept.mcp_tool)
        if handler is None:
            raise LookupError(f"No local handler registered for tool: {intercept.mcp_tool}")

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

        resolved_result = tool_result.value
        resume_handle = self._build_resume_handle(current_handle, intercept, resolved_result)
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

    def _invalid_skill_log_name(self, dispatch_result: InvalidSkill) -> str:
        """Infer the skill name field used by legacy runtime warning logs."""
        skill_path = dispatch_result.skill_path
        if skill_path.name == "SKILL.md" and skill_path.parent.name:
            return skill_path.parent.name
        return skill_path.stem or str(skill_path)

    def _invalid_skill_log_error(self, dispatch_result: InvalidSkill) -> str:
        """Format invalid-skill errors with the legacy Codex wording."""
        if dispatch_result.reason == "SKILL.md frontmatter must be a mapping":
            return f"Frontmatter must be a mapping in {dispatch_result.skill_path}"
        if self._is_legacy_mcp_args_validation_error(dispatch_result.reason):
            return "mcp_args must be a mapping with string keys and YAML-safe values"
        return dispatch_result.reason

    def _is_legacy_mcp_args_validation_error(self, reason: str) -> bool:
        """Collapse granular router mcp_args validation errors for legacy logs."""
        if reason == "mcp_args must be a mapping with string keys and YAML-safe values":
            return False
        return (
            reason.startswith("mcp_args.")
            or reason.startswith("mcp_args[")
            or reason.endswith("keys must be non-empty strings")
        )

    def _log_invalid_skill_intercept(self, dispatch_result: InvalidSkill) -> None:
        """Preserve runtime-owned warnings for matched skills with bad metadata."""
        warning_event = f"{self._log_namespace}.skill_intercept_frontmatter_invalid"
        if (
            dispatch_result.category is InvalidInputReason.FRONTMATTER_INVALID
            and dispatch_result.reason.startswith("missing required frontmatter key:")
        ):
            warning_event = f"{self._log_namespace}.skill_intercept_frontmatter_missing"

        log.warning(
            warning_event,
            skill=self._invalid_skill_log_name(dispatch_result),
            path=str(dispatch_result.skill_path),
            error=self._invalid_skill_log_error(dispatch_result),
        )

    async def _maybe_dispatch_skill_intercept(
        self,
        prompt: str,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Attempt deterministic skill dispatch before invoking Codex."""
        dispatch_result = resolve_skill_dispatch(
            ResolveRequest(
                prompt=prompt,
                cwd=self._cwd,
                skills_dir=self._skills_dir,
            )
        )
        if isinstance(dispatch_result, NotHandled):
            return None
        if isinstance(dispatch_result, InvalidSkill):
            self._log_invalid_skill_intercept(dispatch_result)
            return None
        intercept = dispatch_result

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
        *,
        resume_session_id: str | None = None,
        prompt: str | None = None,
        runtime_handle: RuntimeHandle | None = None,
    ) -> list[str]:
        """Build the CLI command args.  Prompt is fed via stdin separately."""
        command = [self._cli_path, "exec"]

        # Codex accepts one active --profile. The backend runtime profile is
        # the worker-isolation boundary, so it owns that singular flag when
        # configured; role/task profile resolution may still contribute a
        # model fallback below, but not a second --profile.
        if self._codex_profile:
            command.extend(["--profile", self._codex_profile])

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
        else:
            runtime_model, runtime_profile = self._resolve_runtime_codex_config(runtime_handle)
            if runtime_profile and not self._codex_profile:
                command.extend(["--profile", runtime_profile])
            else:
                normalized_runtime_model = self._normalize_model(runtime_model)
                if normalized_runtime_model:
                    command.extend(["--model", normalized_runtime_model])

        command.extend(self._build_permission_args())
        if resume_session_id:
            if not _SAFE_SESSION_ID_PATTERN.match(resume_session_id):
                raise ValueError(
                    f"Invalid resume_session_id: contains disallowed characters: "
                    f"{resume_session_id!r}"
                )
            command.extend(["resume", resume_session_id])
        return command

    def _build_resume_retry_metadata(self, resume_session_id: str | None) -> dict[str, Any]:
        """Return retry metadata for resume failures that happen before reconnect."""
        if not resume_session_id:
            return {}
        return {
            "recoverable": True,
            "recovery": {
                "kind": "resume_retry",
                "reason": "resume_bootstrap_failed",
                "resume_session_id": resume_session_id,
            },
        }

    def _resolve_resume_session_id(
        self,
        current_handle: RuntimeHandle | None,
    ) -> str | None:
        """Resolve the backend-native session id used for CLI resume."""
        if current_handle is None:
            return None
        return current_handle.native_session_id

    def _build_child_env(self) -> dict[str, str]:
        """Build an isolated environment for child runtime processes.

        Strips ``OUROBOROS_AGENT_RUNTIME`` and ``OUROBOROS_LLM_BACKEND`` so
        that a child Codex process does not re-load the Ouroboros MCP server,
        preventing the recursive startup loop described in #185. Also strips
        parent Codex thread/session env so nested ``codex exec`` starts a fresh
        subprocess instead of inheriting the current agent thread.
        """
        return build_codex_child_env(
            max_depth=self._max_ouroboros_depth,
            child_session_env_keys=self._child_session_env_keys,
            depth_error_factory=lambda _depth, max_depth: RuntimeError(
                f"Maximum Ouroboros nesting depth ({max_depth}) exceeded"
            ),
        )

    def _requires_process_stdin(self) -> bool:
        """Return True when the runtime needs a writable stdin pipe."""
        return True

    def _feeds_prompt_via_stdin(self) -> bool:
        """Return True when prompt should be written to stdin (Codex default).

        Override to False for runtimes that accept the prompt as a CLI
        positional argument (e.g. ``opencode run <prompt>``).
        """
        return True

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
        *,
        max_lines: int | None = None,
    ) -> list[str]:
        """Drain a subprocess stream without blocking the main event loop."""
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

    async def _iter_stream_lines(
        self,
        stream: asyncio.StreamReader | None,
        *,
        chunk_size: int = 16384,
        first_chunk_timeout_seconds: float | None = None,
        chunk_timeout_seconds: float | None = None,
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
            # Track byte size incrementally: worst-case 4 bytes per char (UTF-8).
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

            # Shallow fallback: collect only top-level string values to avoid
            # recursive data leakage (credentials, PII, tool outputs).
            shallow_parts = [v.strip() for v in value.values() if isinstance(v, str) and v.strip()]
            return "\n".join(shallow_parts)

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
    ) -> AsyncIterator[AgentMessage]:
        """Execute a task via Codex CLI and stream normalized messages."""
        async for msg in self._execute_task_impl(
            prompt=prompt,
            tools=tools,
            system_prompt=system_prompt,
            resume_handle=resume_handle,
            resume_session_id=resume_session_id,
            _resume_depth=0,
        ):
            yield msg

    async def _execute_task_impl(
        self,
        prompt: str,
        tools: list[str] | None = None,
        system_prompt: str | None = None,
        resume_handle: RuntimeHandle | None = None,
        resume_session_id: str | None = None,
        _resume_depth: int = 0,
    ) -> AsyncIterator[AgentMessage]:
        """Internal implementation with resume-depth tracking."""
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
        try:
            command = self._build_command(
                output_last_message_path=str(output_path),
                resume_session_id=attempted_resume_session_id,
                prompt=composed_prompt,
                runtime_handle=current_handle,
            )
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to prepare {self._display_name}: {e}",
                data={
                    "subtype": "error",
                    "error_type": type(e).__name__,
                    **self._build_resume_retry_metadata(attempted_resume_session_id),
                },
                resume_handle=current_handle,
            )
            output_path.unlink(missing_ok=True)
            return

        log.info(
            f"{self._log_namespace}.task_started",
            command=command,
            cwd=self._cwd,
            has_resume_handle=current_handle is not None,
        )

        stderr_lines: list[str] = []
        last_content = ""
        saw_runtime_event = False
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
                env=self._build_child_env(),
            )
        except FileNotFoundError as e:
            yield AgentMessage(
                type="result",
                content=f"{self._display_name} not found: {e}",
                data={
                    "subtype": "error",
                    "error_type": type(e).__name__,
                    **self._build_resume_retry_metadata(attempted_resume_session_id),
                },
                resume_handle=current_handle,
            )
            output_path.unlink(missing_ok=True)
            return
        except Exception as e:
            yield AgentMessage(
                type="result",
                content=f"Failed to start {self._display_name}: {e}",
                data={
                    "subtype": "error",
                    "error_type": type(e).__name__,
                    **self._build_resume_retry_metadata(attempted_resume_session_id),
                },
                resume_handle=current_handle,
            )
            output_path.unlink(missing_ok=True)
            return

        # Feed prompt via stdin to avoid OS ARG_MAX limits (~262KB on macOS).
        # Runtimes that accept prompt as a CLI arg (e.g. opencode) skip this.
        process_stdin = getattr(process, "stdin", None)
        if composed_prompt and process_stdin is not None and self._feeds_prompt_via_stdin():
            process_stdin.write(composed_prompt.encode("utf-8"))
            await process_stdin.drain()
            process_stdin.close()

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
                    saw_runtime_event = True

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

        except TimeoutError as e:
            if process is not None and control_state is not None:
                await self._terminate_bound_runtime_handle(process, control_state)
                current_handle = self._bind_runtime_handle_controls(
                    current_handle,
                    process=process,
                    control_state=control_state,
                )
            process_finished = getattr(process, "returncode", None) is not None
            process_terminated = True
            if stderr_task is not None:
                stderr_lines = await stderr_task
            final_message = "\n".join(stderr_lines).strip()
            if not final_message:
                final_message = f"{self._display_name} became unresponsive and was terminated: {e}"
            data = {
                "subtype": "error",
                "error_type": type(e).__name__,
            }
            data.update(self._build_resume_retry_metadata(attempted_resume_session_id))
            yield AgentMessage(
                type="result",
                content=final_message,
                data=data,
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
                    control_state["returncode"] = getattr(process, "returncode", None)
                    control_state["runtime_status"] = "terminated"
            raise
        else:
            # Normal completion path — stdout stream finished without timeout.
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
                async for message in self._execute_task_impl(
                    prompt=prompt,
                    tools=tools,
                    system_prompt=system_prompt,
                    resume_handle=recovery_handle,
                    _resume_depth=_resume_depth + 1,
                ):
                    yield message
                return

            result_data: dict[str, Any] = {
                "subtype": "success" if returncode == 0 else "error",
                "returncode": returncode,
            }
            if current_handle is not None and current_handle.native_session_id:
                result_data["session_id"] = current_handle.native_session_id
            if returncode != 0:
                result_data["error_type"] = self._runtime_error_type
                if attempted_resume_session_id and not saw_runtime_event:
                    result_data.update(
                        self._build_resume_retry_metadata(attempted_resume_session_id)
                    )

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


__all__ = ["CodexCliRuntime"]
