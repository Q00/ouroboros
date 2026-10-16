"""Normalize Antigravity stream-json without altering Gemini's wire contract.

Supports nested init/step_update/result envelopes observed in agy print mode,
plus the tool_call fixture spelling. Unknown events remain non-actionable.
"""

from __future__ import annotations

import json
from typing import Any

from ouroboros.providers.gemini_event_normalizer import GeminiEventNormalizer

_TOOL_NAMES = {
    "view_file": "Read",
    "sed_file": "Read",
    "write_to_file": "Write",
    "replace_file_content": "Edit",
    "multi_replace_file_content": "Edit",
    "grep_search": "Grep",
    "find_by_name": "Glob",
    "list_dir": "LS",
    "run_command": "Bash",
}
_TOOL_ARGUMENTS = {
    "Read": {"AbsolutePath": "file_path", "TargetFile": "file_path"},
    "Write": {"TargetFile": "file_path", "CodeContent": "content"},
    "Edit": {"TargetFile": "file_path"},
    "Grep": {"SearchPath": "path", "Query": "pattern"},
    "Glob": {"SearchDirectory": "path", "Pattern": "pattern"},
    "LS": {"DirectoryPath": "path"},
    "Bash": {"CommandLine": "command", "Cwd": "cwd"},
}


def normalize_antigravity_tool(name: object, arguments: object) -> tuple[str, dict[str, Any]]:
    """Retain native arguments and add canonical hints only for known tools.

    Unknown names pass through unchanged; malformed arguments never invent paths
    or commands. Only nonempty strings qualify as canonical path/command hints.
    """
    native = name.strip() if isinstance(name, str) else ""
    canonical = _TOOL_NAMES.get(native, native)
    inputs = dict(arguments) if isinstance(arguments, dict) else {}
    for source, target in _TOOL_ARGUMENTS.get(canonical, {}).items():
        value = inputs.get(source)
        if isinstance(value, str) and value.strip():
            inputs.setdefault(target, value)
    return canonical, inputs


class AntigravityEventNormalizer(GeminiEventNormalizer):
    """Unpack native envelopes and preserve conversation and tool correlation."""

    def _normalize_json_dict(self, parsed: dict[str, Any], *, original: str) -> dict[str, Any]:
        kind = parsed.get("event")
        payload = parsed.get(kind) if isinstance(kind, str) else None
        if kind not in {"init", "step_update", "result"}:
            return super()._normalize_json_dict(parsed, original=original)
        if not isinstance(payload, dict):
            return self._build_event(
                event_type="unknown", content="", raw=parsed, metadata={}, is_error=False
            )
        metadata = dict(payload)
        session_id = parsed.get("conversation_id", payload.get("conversation_id"))
        if isinstance(session_id, str) and session_id.strip():
            metadata["session_id"] = session_id.strip()
        content = ""
        event_type = "init" if kind == "init" else "unknown"
        is_error = False
        if kind == "result":
            event_type = "result"
            response = payload.get("response")
            content = response if isinstance(response, str) else ""
            status = payload.get("status")
            is_error = bool(payload.get("error")) or (
                isinstance(status, str) and status.upper() not in {"SUCCESS", "COMPLETED"}
            )
            if is_error and not content:
                content = json.dumps(payload.get("error") or {"status": status})
        elif kind == "step_update":
            step_type = payload.get("step_type")
            if step_type == "agent_response":
                event_type = "message"
                delta = payload.get("text_delta")
                content = delta if isinstance(delta, str) else ""
                metadata["text_delta"] = True
            elif step_type in {"tool", "tool_call"}:
                info = payload.get("tool_info")
                info = info if isinstance(info, dict) else {}
                name, inputs = normalize_antigravity_tool(
                    payload.get("tool_name", info.get("name")),
                    payload.get("tool_input", info.get("parameters")),
                )
                metadata.update(name=name, input=inputs)
                state = payload.get("state")
                if state in {"DONE", "ERROR"}:
                    event_type = "tool_result"
                    is_error = state == "ERROR" or bool(info.get("error"))
                    # Native result objects vary by tool; retain them without
                    # guessing whether absence of output implies tool success.
                    output = info.get("error") if is_error else info.get("result", "")
                    content = output if isinstance(output, str) else json.dumps(output)
                elif state in {None, "ACTIVE"}:
                    event_type = "tool_use" if name else "unknown"
        return self._build_event(
            event_type=event_type,
            content=content,
            raw=parsed,
            metadata=metadata,
            is_error=is_error,
        )
