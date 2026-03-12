"""Deterministic command dispatch for exact-prefix Codex skill intercepts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle

if TYPE_CHECKING:
    from ouroboros.mcp.server.adapter import MCPServerAdapter
    from ouroboros.orchestrator.codex_cli_runtime import SkillDispatchHandler, SkillInterceptRequest


_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"


class CodexCommandDispatcher:
    """Dispatch exact-prefix Codex skill intercepts through Ouroboros MCP handlers."""

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        runtime_backend: str = "codex",
        llm_backend: str | None = None,
    ) -> None:
        self._cwd = str(Path(cwd).expanduser()) if cwd is not None else os.getcwd()
        self._runtime_backend = runtime_backend
        self._llm_backend = llm_backend
        self._server: MCPServerAdapter | None = None

    def _get_server(self) -> MCPServerAdapter:
        """Create the in-process MCP server lazily on first dispatch."""
        if self._server is None:
            from ouroboros.mcp.server.adapter import create_ouroboros_server

            self._server = create_ouroboros_server(
                name="ouroboros-codex-dispatch",
                version="1.0.0",
                runtime_backend=self._runtime_backend,
                llm_backend=self._llm_backend,
            )
        return self._server

    def _build_tool_arguments(
        self,
        intercept: SkillInterceptRequest,
        current_handle: RuntimeHandle | None,
    ) -> dict[str, Any]:
        """Build the MCP argument payload for an intercepted skill."""
        if intercept.mcp_tool != "ouroboros_interview" or current_handle is None:
            return dict(intercept.mcp_args)

        session_id = current_handle.metadata.get(_INTERVIEW_SESSION_METADATA_KEY)
        if not isinstance(session_id, str) or not session_id.strip():
            return dict(intercept.mcp_args)

        arguments: dict[str, Any] = {"session_id": session_id.strip()}
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
            backend="codex_cli",
            cwd=self._cwd,
            updated_at=updated_at,
            metadata=metadata,
        )

    def _build_tool_call_message(
        self,
        intercept: SkillInterceptRequest,
        tool_arguments: dict[str, Any],
        *,
        resume_handle: RuntimeHandle | None,
    ) -> AgentMessage:
        """Build the assistant message announcing the intercepted tool call."""
        return AgentMessage(
            type="assistant",
            content=f"Calling tool: {intercept.mcp_tool}",
            tool_name=intercept.mcp_tool,
            data={
                "tool_input": tool_arguments,
                "skill_name": intercept.skill_name,
                "command_prefix": intercept.command_prefix,
            },
            resume_handle=resume_handle,
        )

    def _build_recoverable_failure_messages(
        self,
        intercept: SkillInterceptRequest,
        tool_arguments: dict[str, Any],
        error: Any,
        *,
        resume_handle: RuntimeHandle | None,
    ) -> tuple[AgentMessage, ...]:
        """Return recoverable failure messages so the runtime can log and fall through."""
        error_data: dict[str, Any] = {
            "subtype": "error",
            "error_type": type(error).__name__,
            "recoverable": True,
        }
        if hasattr(error, "is_retriable"):
            error_data["is_retriable"] = bool(error.is_retriable)
        if hasattr(error, "details") and isinstance(error.details, dict):
            error_data["meta"] = dict(error.details)

        return (
            self._build_tool_call_message(
                intercept,
                tool_arguments,
                resume_handle=resume_handle,
            ),
            AgentMessage(
                type="result",
                content=str(error),
                data=error_data,
                resume_handle=resume_handle,
            ),
        )

    async def dispatch(
        self,
        intercept: SkillInterceptRequest,
        current_handle: RuntimeHandle | None = None,
    ) -> tuple[AgentMessage, ...] | None:
        """Dispatch an intercepted command to its backing Ouroboros MCP tool."""
        tool_arguments = self._build_tool_arguments(intercept, current_handle)
        try:
            result = await self._get_server().call_tool(
                intercept.mcp_tool,
                tool_arguments,
            )
        except Exception as e:
            return self._build_recoverable_failure_messages(
                intercept,
                tool_arguments,
                e,
                resume_handle=current_handle,
            )

        if result.is_err:
            return self._build_recoverable_failure_messages(
                intercept,
                tool_arguments,
                result.error,
                resume_handle=current_handle,
            )

        tool_result = result.value
        resume_handle = self._build_resume_handle(current_handle, intercept, tool_result)
        content = tool_result.text_content.strip() or f"{intercept.command_prefix} completed."
        result_subtype = "error" if tool_result.is_error else "success"
        if intercept.mcp_tool == "ouroboros_interview":
            result_subtype = "success"
        result_data: dict[str, Any] = {
            "subtype": result_subtype,
            "skill_name": intercept.skill_name,
            "command_prefix": intercept.command_prefix,
            "mcp_tool": intercept.mcp_tool,
            "mcp_args": tool_arguments,
            "tool_error": tool_result.is_error,
            **tool_result.meta,
        }

        return (
            self._build_tool_call_message(
                intercept,
                tool_arguments,
                resume_handle=resume_handle,
            ),
            AgentMessage(
                type="result",
                content=content,
                data=result_data,
                resume_handle=resume_handle,
            ),
        )


def create_codex_command_dispatcher(
    *,
    cwd: str | Path | None = None,
    runtime_backend: str = "codex",
    llm_backend: str | None = None,
) -> SkillDispatchHandler:
    """Create a skill dispatcher for deterministic Codex intercepts."""
    dispatcher = CodexCommandDispatcher(
        cwd=cwd,
        runtime_backend=runtime_backend,
        llm_backend=llm_backend,
    )
    return dispatcher.dispatch


__all__ = ["CodexCommandDispatcher", "create_codex_command_dispatcher"]
