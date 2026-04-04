"""Project runtime messages into shared workflow/session update shapes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from ouroboros.mcp.types import MCPToolResult
from ouroboros.orchestrator.adapter import (
    AgentMessage,
    runtime_handle_capability_graph,
    runtime_handle_control_plane,
    runtime_handle_tool_catalog,
)
from ouroboros.orchestrator.mcp_tools import serialize_tool_definition, serialize_tool_result

_RUNTIME_SESSION_STARTED_EVENT_TYPES = frozenset({"session.started", "thread.started"})
_RUNTIME_SESSION_READY_EVENT_TYPES = frozenset(
    {
        "runtime.connected",
        "runtime.ready",
        "session.bound",
        "session.created",
        "session.ready",
    }
)
_RUNTIME_SESSION_RESUMED_EVENT_TYPES = frozenset({"session.resumed"})
_RUNTIME_COMPLETED_EVENT_TYPES = frozenset(
    {
        "result.completed",
        "run.completed",
        "session.completed",
        "task.completed",
        "turn.completed",
    }
)
_RUNTIME_FAILED_EVENT_TYPES = frozenset(
    {
        "error",
        "result.failed",
        "run.failed",
        "session.failed",
        "task.failed",
        "turn.failed",
    }
)


@dataclass(frozen=True, slots=True)
class ProjectedRuntimeMessage:
    """Backend-neutral projection used by workflow state and event emitters."""

    message_type: str
    content: str
    tool_name: str | None = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    tool_result: dict[str, Any] | None = None
    thinking: str | None = None
    runtime_signal: str | None = None
    runtime_status: str | None = None
    runtime_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_tool_call(self) -> bool:
        """Whether this projection represents a tool invocation."""
        return self.message_type == "tool" and self.tool_name is not None

    @property
    def is_tool_result(self) -> bool:
        """Whether this projection represents a tool completion/update."""
        return self.message_type == "tool_result" and self.tool_name is not None


def project_runtime_message(message: AgentMessage) -> ProjectedRuntimeMessage:
    """Project a streamed runtime message into shared workflow/state fields."""
    tool_name = message_tool_name(message)
    tool_input = message_tool_input(message)
    tool_result = message_tool_result(message)
    thinking = _message_thinking(message)
    # Cache shared inputs and derive message_type + runtime signal in two passes
    # to avoid 3x redundant derive_runtime_signal() calls.
    _event_type = runtime_event_type(message)
    _subtype = message_subtype(message)
    _signal_kwargs = {
        "runtime_event_type": _event_type,
        "subtype": _subtype,
        "is_final": message.is_final,
        "is_error": message.is_error,
    }
    # First pass: derive signal from raw message.type to normalize message_type.
    _raw_signal, _raw_status = derive_runtime_signal(
        message_type=message.type,
        **_signal_kwargs,
    )
    message_type = _normalized_message_type_from_signal(
        message,
        tool_name,
        _raw_signal,
        _raw_status,
    )
    # Second pass only if message_type changed (e.g. subtype → "tool_result").
    if message_type == message.type:
        runtime_signal, runtime_status = _raw_signal, _raw_status
    else:
        runtime_signal, runtime_status = derive_runtime_signal(
            message_type=message_type,
            **_signal_kwargs,
        )

    content = message.content.strip()
    if not content and message_type == "tool":
        content = _build_tool_content(tool_name, tool_input)
    if not content and message_type == "tool_result":
        content = _extract_tool_result_text(tool_result)
    if not content and thinking:
        content = thinking

    return ProjectedRuntimeMessage(
        message_type=message_type,
        content=content,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_result=tool_result,
        thinking=thinking,
        runtime_signal=runtime_signal,
        runtime_status=runtime_status,
        runtime_metadata=serialize_runtime_message_metadata(
            message,
            content=content,
            tool_name=tool_name,
            tool_input=tool_input,
            tool_result=tool_result,
            thinking=thinking,
            runtime_signal=runtime_signal,
            runtime_status=runtime_status,
        ),
    )


def normalized_message_type(message: AgentMessage) -> str:
    """Collapse runtime-specific message details into shared progress categories."""
    runtime_signal, runtime_status = derive_runtime_signal(
        message_type=message.type,
        runtime_event_type=runtime_event_type(message),
        subtype=message_subtype(message),
        is_final=message.is_final,
        is_error=message.is_error,
    )
    return _normalized_message_type_from_signal(
        message,
        message_tool_name(message),
        runtime_signal,
        runtime_status,
    )


def _normalized_message_type_from_signal(
    message: AgentMessage,
    tool_name: str | None,
    runtime_signal: str | None,
    runtime_status: str | None,
) -> str:
    """Derive normalized message type from pre-computed runtime signal."""
    subtype = message.data.get("subtype")
    if subtype == "tool_result":
        return "tool_result"
    if runtime_signal is not None and runtime_status in {"completed", "failed"}:
        return "result"
    if tool_name:
        return "tool"
    if message.is_final:
        return "result"
    return message.type


def message_tool_name(message: AgentMessage) -> str | None:
    """Resolve the tool name from either the message envelope or payload."""
    if message.tool_name:
        return message.tool_name
    data_tool_name = message.data.get("tool_name")
    if isinstance(data_tool_name, str) and data_tool_name.strip():
        return data_tool_name.strip()
    return None


def message_tool_input(message: AgentMessage) -> dict[str, Any]:
    """Return structured tool input when present."""
    tool_input = message.data.get("tool_input")
    return dict(tool_input) if isinstance(tool_input, dict) else {}


def message_tool_result(message: AgentMessage) -> dict[str, Any] | None:
    """Return normalized MCP-compatible tool result data when present."""
    return _normalize_tool_result_payload(message.data.get("tool_result"))


def serialize_runtime_message_metadata(
    message: AgentMessage,
    *,
    content: str | None = None,
    tool_name: str | None = None,
    tool_input: dict[str, Any] | None = None,
    tool_result: dict[str, Any] | None = None,
    thinking: str | None = None,
    runtime_signal: str | None = None,
    runtime_status: str | None = None,
) -> dict[str, Any]:
    """Serialize shared runtime metadata for persisted progress/audit events."""
    from ouroboros.orchestrator.workflow_state import resolve_ac_marker_update

    metadata: dict[str, Any] = {}
    if runtime_signal is None or runtime_status is None:
        runtime_signal, runtime_status = derive_runtime_signal(
            message_type=normalized_message_type(message),
            runtime_event_type=runtime_event_type(message),
            subtype=message_subtype(message),
            is_final=message.is_final,
            is_error=message.is_error,
        )

    runtime_handle = message.resume_handle
    if runtime_handle is not None:
        metadata["runtime"] = runtime_handle.to_session_state_dict()
        metadata["runtime_backend"] = runtime_handle.backend
        handle_runtime_event_type = runtime_handle.metadata.get("runtime_event_type")
        if isinstance(handle_runtime_event_type, str) and handle_runtime_event_type:
            metadata["runtime_event_type"] = handle_runtime_event_type
        tool_catalog = runtime_handle_tool_catalog(runtime_handle)
        if tool_catalog is not None:
            metadata["tool_catalog"] = tool_catalog
        capability_graph = runtime_handle_capability_graph(runtime_handle)
        if capability_graph is not None:
            metadata["capability_graph"] = capability_graph
        control_plane = runtime_handle_control_plane(runtime_handle)
        if control_plane is not None:
            metadata["control_plane"] = control_plane
        turn_id = runtime_handle.metadata.get("turn_id")
        if isinstance(turn_id, str) and turn_id.strip():
            metadata["turn_id"] = turn_id.strip()
        turn_number = runtime_handle.metadata.get("turn_number")
        if isinstance(turn_number, int) and turn_number > 0:
            metadata["turn_number"] = turn_number
        recovery_discontinuity = runtime_handle.metadata.get("recovery_discontinuity")
        if isinstance(recovery_discontinuity, Mapping):
            metadata["recovery_discontinuity"] = dict(recovery_discontinuity)

    subtype = message_subtype(message)
    if subtype:
        metadata["subtype"] = subtype

    session_id = message.data.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        metadata["session_id"] = session_id.strip()
    elif runtime_handle is not None and runtime_handle.native_session_id:
        metadata["session_id"] = runtime_handle.native_session_id

    server_session_id = message.data.get("server_session_id")
    if isinstance(server_session_id, str) and server_session_id.strip():
        metadata["server_session_id"] = server_session_id.strip()
    elif runtime_handle is not None and runtime_handle.server_session_id:
        metadata["server_session_id"] = runtime_handle.server_session_id

    resume_session_id = message.data.get("resume_session_id")
    if isinstance(resume_session_id, str) and resume_session_id.strip():
        metadata["resume_session_id"] = resume_session_id.strip()
    elif runtime_handle is not None and runtime_handle.resume_session_id:
        metadata["resume_session_id"] = runtime_handle.resume_session_id

    error_type = message.data.get("error_type")
    if isinstance(error_type, str) and error_type.strip():
        metadata["error_type"] = error_type.strip()

    permission_request_id = message.data.get("permission_request_id")
    if isinstance(permission_request_id, str) and permission_request_id.strip():
        metadata["permission_request_id"] = permission_request_id.strip()

    permission_decision = message.data.get("permission_decision")
    if isinstance(permission_decision, str) and permission_decision.strip():
        metadata["permission_decision"] = permission_decision.strip()

    permission_approved = message.data.get("permission_approved")
    if isinstance(permission_approved, bool):
        metadata["permission_approved"] = permission_approved

    recovery = message.data.get("recovery")
    if isinstance(recovery, Mapping):
        metadata["recovery"] = _clone_metadata_value(recovery)

    catalog_mismatch = message.data.get("catalog_mismatch")
    if isinstance(catalog_mismatch, Mapping):
        metadata["catalog_mismatch"] = _clone_metadata_value(catalog_mismatch)

    if tool_name:
        metadata["tool_name"] = tool_name

    if tool_input:
        metadata["tool_input"] = dict(tool_input)

    if thinking:
        metadata["thinking"] = thinking

    if runtime_signal:
        metadata["runtime_signal"] = runtime_signal

    if runtime_status:
        metadata["runtime_status"] = runtime_status

    tool_definition = message.data.get("tool_definition")
    if tool_definition is not None:
        metadata["tool_definition"] = serialize_tool_definition(tool_definition)

    if tool_result is not None:
        metadata["tool_result"] = tool_result

    tool_call_id = message.data.get("tool_call_id")
    if isinstance(tool_call_id, str) and tool_call_id.strip():
        metadata["tool_call_id"] = tool_call_id.strip()

    content_part_index = message.data.get("content_part_index")
    if isinstance(content_part_index, int) and content_part_index >= 0:
        metadata["content_part_index"] = content_part_index

    content_part_type = message.data.get("content_part_type")
    if isinstance(content_part_type, str) and content_part_type.strip():
        metadata["content_part_type"] = content_part_type.strip()

    marker_content = content if content is not None else message.content.strip()
    ac_tracking = resolve_ac_marker_update(marker_content, message.data)
    if not ac_tracking.is_empty:
        metadata["ac_tracking"] = ac_tracking.to_dict()

    return metadata


def derive_runtime_signal(
    *,
    message_type: str,
    runtime_event_type: str | None = None,
    subtype: str | None = None,
    is_final: bool = False,
    is_error: bool = False,
) -> tuple[str | None, str | None]:
    """Map backend-native runtime state into shared signal and status categories."""
    normalized_event_type = runtime_event_type.strip().lower() if runtime_event_type else None
    normalized_subtype = subtype.strip().lower() if subtype else None

    if is_final:
        return (
            "session_failed" if is_error else "session_completed",
            "failed" if is_error else "completed",
        )

    if normalized_event_type in _RUNTIME_FAILED_EVENT_TYPES:
        return ("session_failed", "failed")

    if (
        normalized_event_type in _RUNTIME_COMPLETED_EVENT_TYPES
        or normalized_subtype == "result_progress"
    ):
        return ("session_completed", "completed")

    if normalized_subtype == "permission_resolved":
        return ("permission_resolved", "running")

    if normalized_event_type in _RUNTIME_SESSION_RESUMED_EVENT_TYPES:
        return ("session_resumed", "running")

    if (
        normalized_event_type in _RUNTIME_SESSION_STARTED_EVENT_TYPES
        or normalized_subtype == "init"
    ):
        return ("session_started", "running")

    if normalized_event_type in _RUNTIME_SESSION_READY_EVENT_TYPES:
        return ("session_ready", "running")

    if message_type == "tool_result":
        return ("tool_completed", "running")

    if message_type == "tool":
        return ("tool_called", "running")

    return (None, None)


def message_subtype(message: AgentMessage) -> str | None:
    """Return the normalized subtype when present."""
    subtype = message.data.get("subtype")
    if isinstance(subtype, str) and subtype.strip():
        return subtype.strip()
    return None


def runtime_event_type(message: AgentMessage) -> str | None:
    """Resolve the normalized runtime event type from the message or handle."""
    message_event_type = message.data.get("runtime_event_type")
    if isinstance(message_event_type, str) and message_event_type.strip():
        return message_event_type.strip().lower()

    runtime_handle = message.resume_handle
    if runtime_handle is None:
        return None

    handle_event_type = runtime_handle.metadata.get("runtime_event_type")
    if isinstance(handle_event_type, str) and handle_event_type.strip():
        return handle_event_type.strip().lower()
    return None


def _message_thinking(message: AgentMessage) -> str | None:
    """Extract normalized thinking text when present."""
    thinking = message.data.get("thinking")
    if isinstance(thinking, str) and thinking.strip():
        return thinking.strip()
    return None


def _build_tool_content(tool_name: str | None, tool_input: dict[str, Any]) -> str:
    """Synthesize a stable tool-call description when runtimes omit content."""
    if not tool_name:
        return ""

    detail = (
        tool_input.get("command")
        or tool_input.get("file_path")
        or tool_input.get("path")
        or tool_input.get("pattern")
        or tool_input.get("query")
    )
    if isinstance(detail, str) and detail.strip():
        return f"Calling tool: {tool_name}: {detail.strip()}"
    return f"Calling tool: {tool_name}"


def _clone_metadata_value(value: Any) -> Any:
    """Clone nested runtime metadata into plain Python containers."""
    if isinstance(value, Mapping):
        return {str(key): _clone_metadata_value(item) for key, item in value.items()}

    if isinstance(value, list | tuple):
        return [_clone_metadata_value(item) for item in value]

    return value


def _normalize_tool_result_payload(tool_result: object) -> dict[str, Any] | None:
    """Normalize an MCP tool result object or mapping into projection-safe data."""
    if isinstance(tool_result, MCPToolResult):
        return serialize_tool_result(tool_result)

    if not isinstance(tool_result, Mapping):
        return None

    normalized: dict[str, Any] = {
        "content": [],
        "text_content": "",
        "is_error": False,
        "meta": {},
    }

    raw_content = tool_result.get("content")
    if isinstance(raw_content, list | tuple):
        content_items: list[dict[str, Any]] = []
        text_fragments: list[str] = []
        for item in raw_content:
            if not isinstance(item, Mapping):
                continue
            serialized_item = {
                "type": item.get("type"),
                "text": item.get("text"),
                "data": item.get("data"),
                "mime_type": item.get("mime_type"),
                "uri": item.get("uri"),
            }
            content_items.append(serialized_item)
            if serialized_item["type"] == "text":
                text = serialized_item["text"]
                if isinstance(text, str) and text.strip():
                    text_fragments.append(text.strip())
        normalized["content"] = content_items
        if text_fragments:
            normalized["text_content"] = "\n".join(text_fragments)

    text_content = tool_result.get("text_content")
    if isinstance(text_content, str) and text_content.strip():
        normalized["text_content"] = text_content.strip()

    is_error = tool_result.get("is_error")
    if isinstance(is_error, bool):
        normalized["is_error"] = is_error

    meta = tool_result.get("meta")
    if isinstance(meta, Mapping):
        normalized["meta"] = dict(meta)

    return normalized


def _extract_tool_result_text(tool_result: object) -> str:
    """Extract a readable text payload from an MCP-compatible tool result."""
    normalized_tool_result = _normalize_tool_result_payload(tool_result)
    if normalized_tool_result is not None:
        value = normalized_tool_result.get("text_content")
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


__all__ = [
    "ProjectedRuntimeMessage",
    "derive_runtime_signal",
    "message_tool_input",
    "message_tool_name",
    "message_tool_result",
    "message_subtype",
    "normalized_message_type",
    "project_runtime_message",
    "runtime_event_type",
    "serialize_runtime_message_metadata",
]
