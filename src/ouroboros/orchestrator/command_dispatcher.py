"""Deterministic command dispatch for exact-prefix Codex skill intercepts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle

log = get_logger(__name__)

if TYPE_CHECKING:
    from ouroboros.mcp.server.adapter import MCPServerAdapter
    from ouroboros.orchestrator.codex_cli_runtime import SkillDispatchHandler, SkillInterceptRequest


_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"
_LATEST_SEED_ID_METADATA_KEY = "ouroboros_latest_seed_id"
_PENDING_STRUCTURED_INPUT_METADATA_KEY = "ouroboros_pending_structured_input"


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    """Return a stripped string metadata value when present."""
    value = metadata.get(key)
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _has_dispatch_value(value: Any) -> bool:
    """Return True when a dispatch argument already has a meaningful value."""
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _pending_structured_input_metadata(
    metadata: dict[str, Any] | None,
) -> dict[str, str] | None:
    """Return normalized pending structured-input metadata when present."""
    if not isinstance(metadata, dict):
        return None

    raw_value = metadata.get(_PENDING_STRUCTURED_INPUT_METADATA_KEY)
    if not isinstance(raw_value, dict):
        return None

    mcp_tool = _metadata_string(raw_value, "mcp_tool")
    skill_name = _metadata_string(raw_value, "skill_name")
    command_prefix = _metadata_string(raw_value, "command_prefix")
    session_id = _metadata_string(raw_value, "session_id")
    response_param = _metadata_string(raw_value, "response_param") or "answer"
    if not all((mcp_tool, skill_name, command_prefix, session_id)):
        return None

    return {
        "mcp_tool": mcp_tool,
        "skill_name": skill_name,
        "command_prefix": command_prefix,
        "session_id": session_id,
        "response_param": response_param,
    }


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

    def _resume_handle_backend(self) -> str:
        """Map the configured runtime backend to a persisted runtime-handle backend."""
        if self._runtime_backend == "codex":
            return "codex_cli"
        return self._runtime_backend

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
        arguments: dict[str, Any] = dict(intercept.mcp_args)
        if intercept.mcp_tool == "ouroboros_execute_seed" and current_handle is not None:
            latest_seed_id = _metadata_string(
                current_handle.metadata,
                _LATEST_SEED_ID_METADATA_KEY,
            )
            if latest_seed_id is not None and not any(
                _has_dispatch_value(arguments.get(key))
                for key in ("seed_id", "seed_path", "seed_content")
            ):
                arguments.pop("seed_path", None)
                arguments["seed_id"] = latest_seed_id

        if intercept.mcp_tool == "ouroboros_generate_seed" and current_handle is not None:
            latest_interview_session_id = _metadata_string(
                current_handle.metadata,
                _INTERVIEW_SESSION_METADATA_KEY,
            )
            if latest_interview_session_id is not None and not _has_dispatch_value(
                arguments.get("session_id")
            ):
                arguments["session_id"] = latest_interview_session_id

        if current_handle is None:
            return arguments

        pending_input = _pending_structured_input_metadata(current_handle.metadata)
        if pending_input is not None and pending_input["mcp_tool"] == intercept.mcp_tool:
            arguments["session_id"] = pending_input["session_id"]
            if intercept.first_argument is not None:
                arguments[pending_input["response_param"]] = intercept.first_argument
            return arguments

        if intercept.mcp_tool == "ouroboros_interview":
            session_id = current_handle.metadata.get(_INTERVIEW_SESSION_METADATA_KEY)
            if isinstance(session_id, str) and session_id.strip():
                # Preserve original frontmatter args (initial_context, cwd, etc.)
                # and overlay session_id + answer for the resume turn.
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
        metadata = dict(current_handle.metadata) if current_handle is not None else {}
        updated = False
        pending_input = _pending_structured_input_metadata(metadata)
        if pending_input is not None and pending_input["mcp_tool"] == intercept.mcp_tool:
            metadata.pop(_PENDING_STRUCTURED_INPUT_METADATA_KEY, None)
            updated = True

        latest_seed_id = _metadata_string(tool_result.meta, "seed_id")
        if latest_seed_id is not None:
            metadata[_LATEST_SEED_ID_METADATA_KEY] = latest_seed_id
            updated = True

        pending_session_id = _metadata_string(tool_result.meta, "session_id")
        if (
            isinstance(tool_result.meta.get("question_spec"), dict)
            and pending_session_id is not None
        ):
            metadata[_PENDING_STRUCTURED_INPUT_METADATA_KEY] = {
                "mcp_tool": intercept.mcp_tool,
                "skill_name": intercept.skill_name,
                "command_prefix": intercept.command_prefix,
                "session_id": pending_session_id,
                "response_param": _metadata_string(tool_result.meta, "response_param") or "answer",
            }
            updated = True

        if intercept.mcp_tool != "ouroboros_interview":
            if not updated:
                return current_handle
            updated_at = datetime.now(UTC).isoformat()
            if current_handle is not None:
                return replace(current_handle, metadata=metadata, updated_at=updated_at)
            return RuntimeHandle(
                backend=self._resume_handle_backend(),
                cwd=self._cwd,
                updated_at=updated_at,
                metadata=metadata,
            )

        session_id = tool_result.meta.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            if session_id is not None:
                log.warning(
                    "command_dispatcher.resume_handle.invalid_session_id",
                    session_id_type=type(session_id).__name__,
                    session_id_value=repr(session_id),
                )
            if not updated:
                return current_handle
            updated_at = datetime.now(UTC).isoformat()
            if current_handle is not None:
                return replace(current_handle, metadata=metadata, updated_at=updated_at)
            return RuntimeHandle(
                backend=self._resume_handle_backend(),
                cwd=self._cwd,
                updated_at=updated_at,
                metadata=metadata,
            )

        metadata[_INTERVIEW_SESSION_METADATA_KEY] = session_id.strip()
        updated = True
        updated_at = datetime.now(UTC).isoformat()

        if current_handle is not None:
            return replace(current_handle, metadata=metadata, updated_at=updated_at)

        return RuntimeHandle(
            backend=self._resume_handle_backend(),
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
        result_data: dict[str, Any] = {
            "subtype": result_subtype,
            "skill_name": intercept.skill_name,
            "command_prefix": intercept.command_prefix,
            "mcp_tool": intercept.mcp_tool,
            "mcp_args": tool_arguments,
            "tool_error": tool_result.is_error,
            **tool_result.meta,
        }
        if isinstance(tool_result.meta.get("question_spec"), dict):
            result_data["structured_input_request"] = True

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
