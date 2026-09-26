"""Translate measured ACP session updates to Ouroboros ``AgentMessage`` events.

Copilot's ACP wire format is NOT its SDK ``assistant.*``/``tool.execution.*``
event format. Keep that preview-specific distinction at this boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
import re
from typing import Any

from ouroboros.orchestrator.adapter import AgentMessage, RuntimeHandle
from ouroboros.providers.ourocode_acp_client import AcpClientError

_MAX_TEXT = 64 * 1024
_MAX_ANSWER = 2 * 1024 * 1024
_SHELL_EXIT = re.compile(
    r"(?:^|\n)<shellId: [A-Za-z0-9_-]+ completed with exit code (-?\d{1,9})>\s*\Z"
)
_CORRELATION_FIELDS = {
    "agentId": "agent_id",
    "parentToolCallId": "parent_tool_call_id",
    "parentId": "parent_event_id",
    "id": "source_event_id",
    "timestamp": "source_timestamp",
    "mcpServerName": "mcp_server_name",
    "mcpToolName": "mcp_tool_name",
}
_USAGE_FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "totalTokens": "total_tokens",
    "cachedReadTokens": "cache_read_input_tokens",
    "cachedWriteTokens": "cache_creation_input_tokens",
}


def _text(value: object) -> str:
    if isinstance(value, str):
        return value[:_MAX_TEXT]
    if isinstance(value, list):
        return "\n".join(filter(None, (_text(item) for item in value)))[:_MAX_TEXT]
    if isinstance(value, Mapping):
        # Never serialize arbitrary output objects, images, or embedded resources.
        for key in ("text", "content", "message"):
            if key in value:
                result = _text(value[key])
                if result:
                    return result
    return ""


def _correlation(*sources: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        meta = source.get("_meta", {})
        for container in (source, meta):
            if not isinstance(container, Mapping):
                continue
            for source_key, target_key in _CORRELATION_FIELDS.items():
                value = container.get(source_key)
                if isinstance(value, str) and value:
                    result[target_key] = value[:1024]
    return result


def _tool_name(update: Mapping[str, Any]) -> str:
    meta = update.get("_meta", {})
    explicit = update.get("toolName")
    if not explicit and isinstance(meta, Mapping):
        explicit = meta.get("toolName")
    if isinstance(explicit, str) and explicit:
        return explicit[:200]
    kind = update.get("kind")
    if not isinstance(kind, str):
        return "Tool"
    raw = update.get("rawInput", {})
    if kind == "other" and isinstance(raw, Mapping) and isinstance(raw.get("agent_type"), str):
        return "Task"
    if kind == "read" and isinstance(raw, Mapping) and "pattern" in raw:
        if any(key in raw for key in ("output_mode", "-n", "-i", "head_limit", "glob")):
            return "Grep"
        return "Glob"
    return {
        "read": "Read",
        "search": "Grep",
        "edit": "Edit",
        "execute": "Bash",
        "fetch": "WebFetch",
        "think": "Task",
    }.get(kind, "Tool")


def _exit_codes(output: object, *, is_shell: bool) -> list[int]:
    """Read structured status or Copilot's measured, trailing shell-result footer."""
    codes: list[int] = []
    if isinstance(output, Mapping):
        codes = [
            output[key]
            for key in ("exitCode", "exit_code", "returncode")
            if type(output.get(key)) is int
        ]
        contents = output.get("contents")
        if is_shell and isinstance(contents, list):
            codes.extend(
                item["exitCode"]
                for item in contents
                if isinstance(item, Mapping)
                and item.get("type") == "shell_exit"
                and type(item.get("exitCode")) is int
            )
        text = output.get("content")
    else:
        text = output
    # `completed` is a tool lifecycle status, not a successful command verdict.
    if not codes and is_shell and isinstance(text, str):
        match = _SHELL_EXIT.search(text[-2048:])
        if match:
            codes.append(int(match.group(1)))
    return codes


@dataclass
class CopilotAcpEventTranslator:
    """Per-turn correlation and final-answer state; never shared across sessions."""

    session_id: str
    empty_tool_marker: str | None = None
    _tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    _finished_tools: set[str] = field(default_factory=set)
    _answer: list[str] = field(default_factory=list)
    _answer_size: int = 0
    _answer_truncated: bool = False

    def translate(
        self, frame: Mapping[str, Any], handle: RuntimeHandle | None = None
    ) -> list[AgentMessage]:
        method = frame.get("method")
        if method is None and isinstance(frame.get("result"), Mapping):
            return [self.finish(frame["result"], handle)]
        params = frame.get("params", {})
        if not isinstance(params, Mapping) or params.get("sessionId") != self.session_id:
            return []
        if method == "session/request_permission":
            outcome = frame.get("_permission_result", {}).get("outcome", {})
            approved = outcome.get("outcome") == "selected"
            call = params.get("toolCall", {})
            return [
                AgentMessage(
                    type="system",
                    content=f"Copilot tool permission {'allowed once' if approved else 'denied'}",
                    data={
                        **self._base("permission_resolved", frame, params, call),
                        "subtype": "permission_resolved",
                        "permission_request_id": str(frame.get("id", "")),
                        "permission_decision": "allow_once" if approved else "deny",
                        "permission_approved": approved,
                        "tool_call_id": call.get("toolCallId")
                        if isinstance(call, Mapping)
                        else None,
                    },
                    resume_handle=handle,
                )
            ]
        if method != "session/update":
            return []
        update = params.get("update")
        if not isinstance(update, Mapping) or not isinstance(update.get("sessionUpdate"), str):
            raise AcpClientError("Malformed ACP session/update", error_type="malformed_response")
        update_type = update["sessionUpdate"]
        base = self._base(update_type, frame, params, update)
        if update_type == "agent_thought_chunk":
            return []
        if update_type == "agent_message_chunk":
            content = update.get("content")
            if not isinstance(content, Mapping) or content.get("type") != "text":
                return []
            text = content.get("text")
            if not isinstance(text, str):
                raise AcpClientError("Malformed ACP text chunk", error_type="malformed_response")
            if not text:
                return []
            if (
                self.empty_tool_marker
                and not self._answer
                and (
                    text.startswith("Info: Disabled tools:")
                    or text
                    == f'Info: Unknown tool name in the tool allowlist: "{self.empty_tool_marker}"'
                )
            ):
                return [
                    AgentMessage(
                        type="system",
                        content="Copilot ACP tool-less envelope is active",
                        data={**base, "runtime_event_type": "runtime.info"},
                        resume_handle=handle,
                    )
                ]
            # Child messages remain correlated activity, not the parent's final answer.
            if "parent_tool_call_id" not in base:
                remaining = max(0, _MAX_ANSWER - self._answer_size)
                if remaining:
                    self._answer.append(text[:remaining])
                self._answer_size += min(len(text), remaining)
                self._answer_truncated |= len(text) > remaining
            return [
                AgentMessage(
                    type="assistant",
                    content=chunk,
                    data={
                        **base,
                        "runtime_event_type": "assistant.message_delta",
                        "content_delta": chunk,
                        "content_part_type": "text",
                    },
                    resume_handle=handle,
                )
                for offset in range(0, len(text), _MAX_TEXT)
                for chunk in (text[offset : offset + _MAX_TEXT],)
            ]
        if update_type in {"tool_call", "tool_call_update"}:
            return self._tool_update(update, base, handle)
        if update_type == "plan":
            entries = update.get("entries", [])
            content = (
                "\n".join(
                    f"{entry.get('status', 'pending')}: {entry.get('content', '')}"
                    for entry in entries
                    if isinstance(entry, Mapping)
                )[:_MAX_TEXT]
                if isinstance(entries, list)
                else ""
            )
            return [
                AgentMessage(
                    type="system",
                    content=content or "Copilot updated its plan",
                    data={**base, "runtime_event_type": "agent.plan"},
                    resume_handle=handle,
                )
            ]
        # Config/command discovery and context-window telemetry are not task output.
        # Unknown preview extensions are ignored, not interpreted as SDK events.
        return []

    def _base(self, update_type: str, *sources: object) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "acp_session_id": self.session_id,
            "runtime_transport": "acp",
            "acp_update_type": update_type,
            "runtime_event_type": f"acp.{update_type}",
            **_correlation(*sources),
        }

    def _tool_update(
        self,
        update: Mapping[str, Any],
        base: dict[str, Any],
        handle: RuntimeHandle | None,
    ) -> list[AgentMessage]:
        tool_id = update.get("toolCallId")
        if not isinstance(tool_id, str) or not tool_id:
            raise AcpClientError(
                "ACP tool update omitted toolCallId", error_type="malformed_response"
            )
        if tool_id in self._finished_tools:
            return []
        first = tool_id not in self._tools
        if first and len(self._tools) + len(self._finished_tools) >= 4096:
            raise AcpClientError("ACP tool correlation limit exceeded", error_type="protocol_error")
        previous = self._tools.get(tool_id, {})
        state = {**previous, **update}
        status = state.get("status")
        if status is not None and not isinstance(status, str):
            raise AcpClientError("Malformed ACP tool status", error_type="malformed_response")
        if isinstance(previous.get("_meta"), Mapping) and isinstance(update.get("_meta"), Mapping):
            state["_meta"] = {**previous["_meta"], **update["_meta"]}
        self._tools[tool_id] = state
        tool_name = _tool_name(state)
        raw_input = state.get("rawInput", {})
        tool_input = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        data = {
            **_correlation(state),
            **base,
            "tool_call_id": tool_id,
            "tool_input": tool_input,
            "tool_kind": state.get("kind"),
        }
        messages: list[AgentMessage] = []
        if first:
            if "parent_tool_call_id" not in data:
                self._answer.clear()
                self._answer_size = 0
                self._answer_truncated = False
            messages.append(
                AgentMessage(
                    type="assistant",
                    content=_text(state.get("title")) or f"Calling tool: {tool_name}",
                    tool_name=tool_name,
                    data={**data, "runtime_event_type": "tool.started"},
                    resume_handle=handle,
                )
            )
        if status not in {"completed", "failed"}:
            if not first or update.get("sessionUpdate") == "tool_call_update":
                output = _text(update.get("content")) or _text(update.get("rawOutput"))
                messages.append(
                    AgentMessage(
                        type="system",
                        content=output or _text(state.get("title")) or "Tool running",
                        tool_name=tool_name,
                        data={
                            **data,
                            "subtype": "tool_progress",
                            "runtime_event_type": "tool.progress",
                            "status": status or "in_progress",
                            "tool_output": output,
                        },
                        resume_handle=handle,
                    )
                )
            return messages
        self._finished_tools.add(tool_id)
        self._tools.pop(tool_id, None)
        output = state.get("rawOutput")
        text = _text(output) or _text(state.get("content"))
        is_error = status == "failed"
        if isinstance(output, Mapping):
            is_error |= output.get("isError") is True or output.get("success") is False
        codes = _exit_codes(output, is_shell=tool_name == "Bash")
        if codes:
            data["exit_code"] = next((code for code in codes if code != 0), codes[0])
            is_error |= any(code != 0 for code in codes)
        messages.append(
            AgentMessage(
                type="tool",
                content=text,
                tool_name=tool_name,
                data={
                    **data,
                    "subtype": "tool_result",
                    "runtime_event_type": "tool.result",
                    "status": status,
                    "is_error": is_error,
                    "tool_result": {
                        "content": [{"type": "text", "text": text}],
                        "isError": is_error,
                    },
                },
                resume_handle=handle,
            )
        )
        return messages

    def finish(self, result: Mapping[str, Any], handle: RuntimeHandle | None) -> AgentMessage:
        stop_reason = result.get("stopReason")
        success = stop_reason == "end_turn"
        data = {
            **self._base("prompt_result"),
            "runtime_event_type": "turn.completed" if success else "turn.failed",
            "subtype": "success" if success else "error",
            "stop_reason": stop_reason,
        }
        if not success:
            data["error_type"] = "CopilotAcpTurnStopped"
        if self._answer_truncated:
            data["response_truncated"] = True
        if "usage" in result:
            usage = result["usage"]
            normalized: dict[str, int | float] = {}
            if isinstance(usage, Mapping):
                for source_key, target_key in _USAGE_FIELDS.items():
                    if source_key not in usage:
                        continue
                    value = usage[source_key]
                    try:
                        valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
                    except OverflowError:
                        valid = False
                    if not valid:
                        data["usage_invalid"] = True
                        break
                    normalized[target_key] = value
                if normalized and "usage_invalid" not in data:
                    data["usage"] = normalized
            else:
                data["usage_invalid"] = True
        text = "".join(self._answer)
        if not text:
            text = (
                "Copilot ACP task completed." if success else f"Copilot ACP stopped: {stop_reason}"
            )
        return AgentMessage(type="result", content=text, data=data, resume_handle=handle)


__all__ = ["CopilotAcpEventTranslator"]
