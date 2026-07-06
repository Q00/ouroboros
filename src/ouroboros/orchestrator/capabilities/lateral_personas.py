"""Lateral persona capability metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LateralPersonaMetadata:
    """Structured metadata for one lateral persona subagent lane."""

    persona_id: str
    role: str
    prompt: Mapping[str, Any]
    response_payload_ref: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe capability metadata."""
        return {
            "persona_id": self.persona_id,
            "role": self.role,
            "prompt": dict(self.prompt),
            "response_payload_ref": dict(self.response_payload_ref),
        }


@dataclass(frozen=True, slots=True)
class LateralPersonaPanelMetadata:
    """Structured metadata for lateral multi-persona orchestration."""

    panel_id: str
    mcp_tool: str
    dispatch_modes: tuple[str, ...]
    parallel_preference: str
    sequential_fallback: Mapping[str, Any]
    personas: tuple[LateralPersonaMetadata, ...]
    request_model_schema: Mapping[str, Any]
    response_payload_refs: Mapping[str, Any]
    runtime_instruction: str
    legacy_dispatch_modes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-safe capability metadata."""
        return {
            "panel_id": self.panel_id,
            "mcp_tool": self.mcp_tool,
            "dispatch_modes": list(self.dispatch_modes),
            "legacy_dispatch_modes": list(self.legacy_dispatch_modes),
            "parallel_preference": self.parallel_preference,
            "sequential_fallback": dict(self.sequential_fallback),
            "personas": [persona.to_dict() for persona in self.personas],
            "request_model_schema": dict(self.request_model_schema),
            "response_payload_refs": dict(self.response_payload_refs),
            "runtime_instruction": self.runtime_instruction,
        }


def _lateral_persona_panel_request_schema() -> dict[str, Any]:
    """Return the structured request model for lateral persona panels."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["problem_context", "current_approach"],
        "properties": {
            "problem_context": {
                "type": "string",
                "minLength": 1,
                "description": "The stuck state or problem being reframed.",
            },
            "current_approach": {
                "type": "string",
                "minLength": 1,
                "description": "What has already been tried.",
            },
            "persona": {
                "type": "string",
                "enum": [
                    "hacker",
                    "researcher",
                    "simplifier",
                    "architect",
                    "contrarian",
                    "all",
                ],
            },
            "personas": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": [
                        "hacker",
                        "researcher",
                        "simplifier",
                        "architect",
                        "contrarian",
                    ],
                },
            },
            "failed_attempts": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
    }


def _lateral_persona_panel_metadata() -> LateralPersonaPanelMetadata:
    """Return the metadata contract for lateral persona panel dispatch."""
    persona_roles = (
        ("hacker", "Finds unconventional workarounds"),
        ("researcher", "Seeks additional information"),
        ("simplifier", "Reduces complexity"),
        ("architect", "Restructures the approach"),
        ("contrarian", "Challenges assumptions"),
    )
    return LateralPersonaPanelMetadata(
        panel_id="lateral_persona_panel.v1",
        mcp_tool="ouroboros_lateral_think",
        dispatch_modes=("plugin", "sequential"),
        legacy_dispatch_modes=("inline_fallback",),
        parallel_preference="parallel_when_runtime_supports_subagents",
        sequential_fallback={
            "supported": True,
            "mode": "sequential_persona_payload_dispatch",
            "trigger": "runtime_has_no_native_parallel_subagent_primitive",
        },
        personas=tuple(
            LateralPersonaMetadata(
                persona_id=persona_id,
                role=role,
                prompt={
                    "source": "build_lateral_multi_subagent",
                    "payload_field": "payloads[].prompt",
                    "context_field": "payloads[].context",
                    "requires_prose_parsing": False,
                },
                response_payload_ref={
                    "plugin": "MCPToolResult.meta._subagents[persona_id]",
                    "inline_meta": "MCPToolResult.meta.payloads[persona_id]",
                    "inline_content": (
                        "content sentinel ouroboros-lateral-inline-dispatch-v1.payloads[persona_id]"
                    ),
                },
            )
            for persona_id, role in persona_roles
        ),
        request_model_schema=_lateral_persona_panel_request_schema(),
        response_payload_refs={
            "plugin": "MCPToolResult.meta._subagents",
            "inline_meta": "MCPToolResult.meta.payloads",
            "inline_content": ("content sentinel ouroboros-lateral-inline-dispatch-v1.payloads"),
            "result_correlation_key": "context.persona",
            "requires_prose_parsing": False,
        },
        runtime_instruction=(
            "Call ouroboros_lateral_think first. If the response delegates via "
            "_subagents, consume those payloads. If it returns sequential "
            "payload metadata and the runtime has a native subagent primitive, "
            "dispatch each structured payload by context.persona; otherwise "
            "process those payloads sequentially. Treat inline_fallback as a "
            "legacy alias for sequential."
        ),
    )


def _pm_interview_subagent_metadata() -> dict[str, Any]:
    """Return the metadata contract for PM interview subagent dispatch."""
    return {
        "directive": "run_pm_interview_subagent",
        "mcp_tool": "ouroboros_pm_interview",
        "dispatch_modes": ["plugin"],
        "payload_builder": "build_pm_interview_subagent",
        "request_model_schema": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "session_id": {"type": "string"},
                "action": {
                    "type": "string",
                    "enum": ["start", "answer", "resume", "generate", "select_repos"],
                    "default": "start",
                },
                "initial_context": {"type": "string"},
                "answer": {"type": "string"},
                "cwd": {"type": "string"},
                "selected_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
        },
        "response_payload_refs": {
            "plugin": "MCPToolResult.meta._subagent",
            "content_json": "MCPToolResult.content[0].text._subagent",
            "result_correlation_key": "context.session_id",
            "requires_prose_parsing": False,
        },
        "subagent_context_keys": [
            "session_id",
            "action",
            "initial_context",
            "answer",
            "cwd",
            "selected_repos",
        ],
        "runtime_instruction": (
            "Dispatch PM interview work through the `_subagent` payload produced "
            "by build_pm_interview_subagent. Preserve session_id/action context "
            "and consume the structured payload directly; do not infer PM "
            "subagent routing from prose."
        ),
    }


__all__ = [
    "LateralPersonaMetadata",
    "LateralPersonaPanelMetadata",
    "_lateral_persona_panel_metadata",
    "_lateral_persona_panel_request_schema",
    "_pm_interview_subagent_metadata",
]
