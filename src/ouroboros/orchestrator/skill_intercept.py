"""Runtime-agnostic skill intercept dispatch.

Extracted from ``CodexCliRuntime`` so that every subprocess-backed runtime
(Codex, Kiro, future headless CLIs) can uniformly honor ``ooo <skill>`` and
``/ouroboros:<skill>`` prefixes by dispatching to Ouroboros MCP handlers
*before* spawning the external CLI.  Without this step, selecting a non-Claude
runtime silently drops skill-dispatch behavior, which is the parity gap
flagged in the review of PR feat/kiro-cli-adapter.

The interceptor is composed — not inherited — so that runtimes can customize
log namespaces and legacy warning wording without taking on Codex-specific
semantics.

NOTE: ``CodexCliRuntime`` currently keeps its own inline copy of this logic
unchanged so that its extensive test suite (legacy log wording, frontmatter
edge cases) remains green. A follow-up change should migrate Codex to compose
``SkillInterceptor`` and delete the duplicate — see the PR description for
the migration plan. This file is the single source of truth for new runtimes
(Kiro, and any future headless backend).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    RuntimeHandle,
    SkillDispatchHandler,
)
from ouroboros.router import (
    InvalidInputReason,
    InvalidSkill,
    NotHandled,
    Resolved,
    ResolveRequest,
    resolve_skill_dispatch,
)

log = get_logger(__name__)


_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"


InvalidSkillLogFormatter = Callable[[InvalidSkill], tuple[str, str, str]]
"""Callback returning ``(warning_event, skill_name, error_text)`` for a bad skill.

Runtimes pass this hook when they need to preserve legacy warning wording.
Codex uses it to keep ``codex_cli_runtime.skill_intercept_frontmatter_*``
events verbatim; new runtimes can omit the hook and fall back to a generic
formatter.
"""


class SkillInterceptor:
    """Deterministic ``ooo`` / ``/ouroboros:`` prefix dispatcher.

    A runtime builds this once in ``__init__`` and calls :meth:`maybe_dispatch`
    at the top of ``execute_task``.  When the prompt matches a packaged skill,
    the interceptor invokes the matching MCP handler (or the runtime-supplied
    ``skill_dispatcher``) and returns a ready-to-yield sequence of
    ``AgentMessage``.  When the prompt does not match — or when the dispatch
    produced a recoverable error — the caller falls through to the subprocess.
    """

    def __init__(
        self,
        *,
        cwd: str | Path,
        runtime_backend: str,
        runtime_handle_backend: str,
        permission_mode: str | None,
        llm_backend: str | None,
        log_namespace: str,
        skills_dir: str | Path | None = None,
        skill_dispatcher: SkillDispatchHandler | None = None,
        invalid_skill_log_formatter: InvalidSkillLogFormatter | None = None,
    ) -> None:
        self._cwd = str(Path(cwd).expanduser())
        self._runtime_backend = runtime_backend
        self._runtime_handle_backend = runtime_handle_backend
        self._permission_mode = permission_mode
        self._llm_backend = llm_backend
        self._log_namespace = log_namespace
        self._skills_dir = Path(skills_dir).expanduser() if skills_dir is not None else None
        self._skill_dispatcher = skill_dispatcher
        self._invalid_skill_log_formatter = invalid_skill_log_formatter
        self._builtin_mcp_handlers: dict[str, Any] | None = None

    # -- public entry point -------------------------------------------------

    async def maybe_dispatch(
        self,
        prompt: str,
        current_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...] | None:
        """Attempt deterministic skill dispatch before invoking the runtime CLI."""
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
            self._log_invalid_skill(dispatch_result)
            return None

        intercept = dispatch_result
        dispatcher: Callable[
            [Resolved, RuntimeHandle | None],
            Awaitable[tuple[AgentMessage, ...] | None],
        ] = self._skill_dispatcher or self._dispatch_locally

        try:
            dispatched_messages = await dispatcher(intercept, current_handle)
        except Exception as e:  # noqa: BLE001 — parity with Codex
            log.warning(
                f"{self._log_namespace}.skill_intercept_dispatch_failed",
                **self._build_failure_context(intercept),
                error_type=type(e).__name__,
                error=str(e),
                exc_info=True,
            )
            return None

        recoverable_error = self._extract_recoverable_dispatch_error(dispatched_messages)
        if recoverable_error is not None:
            log.warning(
                f"{self._log_namespace}.skill_intercept_dispatch_failed",
                **self._build_failure_context(intercept),
                error_type=recoverable_error.data.get("error_type"),
                error=recoverable_error.content,
                recoverable=True,
            )
            return None

        return dispatched_messages

    # -- dispatcher fallback ------------------------------------------------

    async def _dispatch_locally(
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

    # -- helpers ------------------------------------------------------------

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
        return self._get_builtin_mcp_handlers().get(tool_name)

    def _build_tool_arguments(
        self,
        intercept: Resolved,
        current_handle: RuntimeHandle | None,
    ) -> dict[str, Any]:
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
        if intercept.mcp_tool != "ouroboros_interview":
            return current_handle

        session_id = tool_result.meta.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            if session_id is not None:
                log.warning(
                    f"{self._log_namespace}.resume_handle.invalid_session_id",
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
            backend=self._runtime_handle_backend,
            cwd=self._cwd,
            approval_mode=self._permission_mode,
            updated_at=updated_at,
            metadata=metadata,
        )

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

    def _build_failure_context(self, intercept: Resolved) -> dict[str, Any]:
        return {
            "skill": intercept.skill_name,
            "tool": intercept.mcp_tool,
            "command_prefix": intercept.command_prefix,
            "path": str(intercept.skill_path),
            "first_argument": _truncate(intercept.first_argument, limit=120),
            "prompt_preview": _truncate(intercept.prompt, limit=200),
            "mcp_arg_keys": tuple(sorted(intercept.mcp_args)),
            "mcp_args_preview": _preview(intercept.mcp_args),
            "fallback": f"pass_through_to_{self._runtime_backend}",
        }

    def _log_invalid_skill(self, dispatch_result: InvalidSkill) -> None:
        if self._invalid_skill_log_formatter is not None:
            event, skill, error = self._invalid_skill_log_formatter(dispatch_result)
        else:
            event = f"{self._log_namespace}.skill_intercept_frontmatter_invalid"
            if (
                dispatch_result.category is InvalidInputReason.FRONTMATTER_INVALID
                and dispatch_result.reason.startswith("missing required frontmatter key:")
            ):
                event = f"{self._log_namespace}.skill_intercept_frontmatter_missing"
            skill = _default_invalid_skill_name(dispatch_result)
            error = dispatch_result.reason

        log.warning(
            event,
            skill=skill,
            path=str(dispatch_result.skill_path),
            error=error,
        )

    @staticmethod
    def _extract_recoverable_dispatch_error(
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


# -- module-level helpers --------------------------------------------------


def _truncate(value: str | None, *, limit: int) -> str | None:
    if value is None or len(value) <= limit:
        return value
    return f"{value[: limit - 3]}..."


def _preview(value: Any, *, limit: int = 160) -> Any:
    if isinstance(value, str):
        return _truncate(value, limit=limit)
    if isinstance(value, Mapping):
        return {key: _preview(item, limit=limit) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_preview(item, limit=limit) for item in value]
    return value


def _default_invalid_skill_name(dispatch_result: InvalidSkill) -> str:
    skill_path = dispatch_result.skill_path
    if skill_path.name == "SKILL.md" and skill_path.parent.name:
        return skill_path.parent.name
    return skill_path.stem or str(skill_path)


__all__ = ["InvalidSkillLogFormatter", "SkillInterceptor"]
