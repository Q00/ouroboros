"""Deterministic command dispatch for exact-prefix Codex skill intercepts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from functools import lru_cache
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
from ouroboros.orchestrator.interview_session import (
    INTERVIEW_CALIBRATION_METADATA_KEY as _INTERVIEW_CALIBRATION_METADATA_KEY,
)
from ouroboros.orchestrator.interview_session import (
    INTERVIEW_SESSION_METADATA_KEY as _INTERVIEW_SESSION_METADATA_KEY,
)
from ouroboros.orchestrator.interview_session import (
    build_interview_tool_arguments,
)
from ouroboros.router.types import Resolved

log = get_logger(__name__)

if TYPE_CHECKING:
    from ouroboros.mcp.server.adapter import MCPServerAdapter


@lru_cache(maxsize=1)
def _ouroboros_package_build_digest() -> str:
    """Hash the installed Ouroboros Python build using install-path-neutral names."""
    package_root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for source_path in sorted(package_root.rglob("*.py")):
        relative_path = source_path.relative_to(package_root).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


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
            "_INTERVIEW_CALIBRATION_METADATA_KEY": _INTERVIEW_CALIBRATION_METADATA_KEY,
        }
        from ouroboros.mcp.server.adapter import MCPServerAdapter, create_ouroboros_server
        from ouroboros.orchestrator.runner import OrchestratorRunner

        payload.update(
            {
                "external:build_interview_tool_arguments": self._callable_implementation_digest(
                    build_interview_tool_arguments
                ),
                "external:create_ouroboros_server": self._callable_implementation_digest(
                    create_ouroboros_server
                ),
                "external:MCPServerAdapter.call_tool": self._callable_implementation_digest(
                    MCPServerAdapter.call_tool
                ),
                "external:worker_cwd_failure_message": self._callable_implementation_digest(
                    worker_cwd_failure_message
                ),
                "external:OrchestratorRunner": self._class_implementation_digest(
                    OrchestratorRunner
                ),
                "package_build_sha256": _ouroboros_package_build_digest(),
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
            "defaults": CodexCommandDispatcher._identity_value_payload(
                getattr(function, "__defaults__", None)
            ),
            "kwdefaults": CodexCommandDispatcher._identity_value_payload(
                getattr(function, "__kwdefaults__", None)
            ),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _class_implementation_digest(class_obj: type[object]) -> str:
        """Hash every callable declared by a behavior-owning class."""
        payload: dict[str, object] = {}
        for name, member in sorted(vars(class_obj).items()):
            if isinstance(member, staticmethod | classmethod):
                member = member.__func__
            if isinstance(member, property):
                payload[name] = {
                    accessor: CodexCommandDispatcher._callable_implementation_digest(function)
                    for accessor, function in (
                        ("get", member.fget),
                        ("set", member.fset),
                        ("delete", member.fdel),
                    )
                    if function is not None
                }
                continue
            callable_digest = CodexCommandDispatcher._callable_implementation_digest(member)
            if callable_digest is not None:
                payload[name] = callable_digest
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _identity_value_payload(value: object) -> object:
        """Serialize implementation values without process-local addresses."""
        if isinstance(value, CodeType):
            return {
                "type": "code",
                "payload": CodexCommandDispatcher._code_object_identity_payload(value),
            }
        if isinstance(value, tuple):
            return {
                "type": "tuple",
                "items": [CodexCommandDispatcher._identity_value_payload(item) for item in value],
            }
        if isinstance(value, list):
            return [CodexCommandDispatcher._identity_value_payload(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): CodexCommandDispatcher._identity_value_payload(item)
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            }
        if isinstance(value, frozenset):
            return {
                "type": "frozenset",
                "items": sorted(
                    (CodexCommandDispatcher._identity_value_payload(item) for item in value),
                    key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
                ),
            }
        if isinstance(value, bytes):
            return {"type": "bytes", "hex": value.hex()}
        if isinstance(value, Path):
            return {"type": "path", "value": value.as_posix()}
        if value is None or isinstance(value, str | int | float | bool):
            return value
        value_type = type(value)
        return {
            "type": f"{value_type.__module__}.{value_type.__qualname__}",
        }

    @staticmethod
    def _code_object_identity_payload(code: CodeType) -> dict[str, object]:
        """Serialize a code object without process-local repr addresses.

        ``repr(code.co_consts)`` includes memory addresses for nested code
        objects.  Portable dispatcher identity must be stable across fresh
        interpreters while still changing when nested implementation code
        changes.
        """

        return {
            "argcount": code.co_argcount,
            "posonlyargcount": code.co_posonlyargcount,
            "kwonlyargcount": code.co_kwonlyargcount,
            "nlocals": code.co_nlocals,
            "stacksize": code.co_stacksize,
            "flags": code.co_flags,
            "bytecode": code.co_code.hex(),
            "consts": [
                CodexCommandDispatcher._identity_value_payload(const) for const in code.co_consts
            ],
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
                project_dir=self._cwd,
            )
        return self._server

    def _build_tool_arguments(
        self,
        intercept: Resolved,
        current_handle: RuntimeHandle | None,
    ) -> dict[str, Any]:
        """Build the MCP argument payload for an intercepted skill."""
        return build_interview_tool_arguments(intercept, current_handle)

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
        calibration = tool_result.meta.get("interview_calibration")
        valid_session_id = isinstance(session_id, str) and bool(session_id.strip())
        valid_calibration = isinstance(calibration, Mapping)
        if not valid_session_id and not valid_calibration:
            if session_id is not None and not valid_session_id:
                log.warning(
                    "command_dispatcher.resume_handle.invalid_session_id",
                    session_id_type=type(session_id).__name__,
                    session_id_value=repr(session_id),
                )
            return current_handle

        metadata = dict(current_handle.metadata) if current_handle is not None else {}
        if valid_session_id:
            metadata[_INTERVIEW_SESSION_METADATA_KEY] = session_id.strip()
        if valid_calibration:
            metadata[_INTERVIEW_CALIBRATION_METADATA_KEY] = dict(calibration)
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
