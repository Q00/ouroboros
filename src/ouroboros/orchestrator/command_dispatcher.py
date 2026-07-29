"""Deterministic command dispatch for exact-prefix Codex skill intercepts."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from types import CodeType
from typing import TYPE_CHECKING, Any

from ouroboros.observability.logging import get_logger
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    ResolvedWorkerCwd,
    RuntimeHandle,
    SkillDispatchHandler,
    resolve_worker_cwd,
    worker_cwd_failure_message,
)
from ouroboros.router.types import Resolved

log = get_logger(__name__)

if TYPE_CHECKING:
    from ouroboros.mcp.server.adapter import MCPServerAdapter


_INTERVIEW_SESSION_METADATA_KEY = "ouroboros_interview_session_id"


class CodexCommandDispatcher:
    """Dispatch exact-prefix Codex skill intercepts through Ouroboros MCP handlers."""

    def __init__(
        self,
        *,
        cwd: str | Path | ResolvedWorkerCwd | None = None,
        runtime_backend: str = "codex",
        llm_backend: str | None = None,
    ) -> None:
        self._cwd = resolve_worker_cwd(cwd)
        self._runtime_backend = runtime_backend
        self._llm_backend = llm_backend
        self._server: MCPServerAdapter | None = None

    def stable_identity_contract(self) -> dict[str, str | None]:
        """Return the portable identity for Ouroboros-owned dispatch authority."""
        return {
            "kind": "ouroboros_codex_command_dispatcher_v1",
            "cwd": self._cwd,
            "runtime_backend": self._runtime_backend,
            "llm_backend": self._llm_backend,
            "implementation_sha256": self._dispatcher_implementation_digest(),
        }

    def _dispatcher_implementation_digest(self) -> str:
        """Return a digest covering the transitive dispatch implementation."""
        payload = {
            name: self._callable_implementation_digest(getattr(self, name))
            for name in (
                "_resume_handle_backend",
                "_get_server",
                "_build_tool_arguments",
                "_build_resume_handle",
                "_build_tool_call_message",
                "_build_recoverable_failure_messages",
                "dispatch",
            )
        }
        payload["globals"] = {
            "_INTERVIEW_SESSION_METADATA_KEY": _INTERVIEW_SESSION_METADATA_KEY,
        }
        from ouroboros.mcp.server.adapter import MCPServerAdapter, create_ouroboros_server

        payload.update(
            {
                "external:create_ouroboros_server": self._callable_implementation_digest(
                    create_ouroboros_server
                ),
                "external:MCPServerAdapter.call_tool": self._callable_implementation_digest(
                    MCPServerAdapter.call_tool
                ),
            }
        )
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _callable_implementation_digest(callable_obj: object) -> str | None:
        """Return a stable digest for a Python callable's implementation."""
        function = getattr(callable_obj, "__func__", callable_obj)
        code = getattr(function, "__code__", None)
        if code is None:
            return None
        payload = {
            "module": getattr(function, "__module__", None),
            "qualname": getattr(function, "__qualname__", None),
            "code": CodexCommandDispatcher._code_object_identity_payload(code),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _code_object_identity_payload(code: CodeType) -> dict[str, object]:
        """Serialize a code object without process-local repr addresses.

        ``repr(code.co_consts)`` includes memory addresses for nested code
        objects.  Portable dispatcher identity must be stable across fresh
        interpreters while still changing when nested implementation code
        changes.
        """

        def _const_payload(value: object) -> object:
            if isinstance(value, CodeType):
                return {
                    "type": "code",
                    "payload": CodexCommandDispatcher._code_object_identity_payload(value),
                }
            if isinstance(value, tuple):
                return {"type": "tuple", "items": [_const_payload(item) for item in value]}
            if isinstance(value, frozenset):
                return {
                    "type": "frozenset",
                    "items": sorted(
                        (_const_payload(item) for item in value),
                        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
                    ),
                }
            if isinstance(value, bytes):
                return {"type": "bytes", "hex": value.hex()}
            if value is None or isinstance(value, str | int | float | bool):
                return value
            return {"type": type(value).__name__, "repr": repr(value)}

        return {
            "argcount": code.co_argcount,
            "posonlyargcount": code.co_posonlyargcount,
            "kwonlyargcount": code.co_kwonlyargcount,
            "nlocals": code.co_nlocals,
            "stacksize": code.co_stacksize,
            "flags": code.co_flags,
            "bytecode": code.co_code.hex(),
            "consts": [_const_payload(const) for const in code.co_consts],
            "names": list(code.co_names),
            "varnames": list(code.co_varnames),
            "freevars": list(code.co_freevars),
            "cellvars": list(code.co_cellvars),
        }

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
        # session_id instead of starting a new interview. Other frontmatter
        # args (cwd, etc.) are preserved.
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
                    "command_dispatcher.resume_handle.invalid_session_id",
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
            backend=self._resume_handle_backend(),
            cwd=self._cwd,
            updated_at=updated_at,
            metadata=metadata,
        )

    def _build_tool_call_message(
        self,
        intercept: Resolved,
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
        intercept: Resolved,
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
        intercept: Resolved,
        current_handle: RuntimeHandle | None = None,
    ) -> tuple[AgentMessage, ...] | None:
        """Dispatch an intercepted command to its backing Ouroboros MCP tool."""
        cwd_failure = worker_cwd_failure_message(
            self._cwd,
            runtime_backend=self._runtime_backend,
            resume_handle=current_handle,
        )
        if cwd_failure is not None:
            return (cwd_failure,)

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
    cwd: str | Path | ResolvedWorkerCwd | None = None,
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
