"""Fail-closed tool and input policy for the public ChatGPT Work surface."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ouroboros.mcp.tools.registry import ToolRegistry


class PublicInputError(ValueError):
    """Raised when public input attempts to cross the hosted boundary."""


@dataclass(frozen=True, slots=True)
class PublicToolPolicy:
    """OpenAI-facing annotations for one existing Full tool."""

    read_only: bool
    open_world: bool = False
    destructive: bool = False


PUBLIC_TOOL_POLICIES: dict[str, PublicToolPolicy] = {
    "ouroboros_interview": PublicToolPolicy(read_only=False),
    "ouroboros_generate_seed": PublicToolPolicy(read_only=False),
    "ouroboros_evaluate": PublicToolPolicy(read_only=False),
    "ouroboros_session_status": PublicToolPolicy(read_only=True),
}

PUBLIC_TOOL_FIELDS: dict[str, frozenset[str]] = {
    "ouroboros_interview": frozenset(
        {"initial_context", "session_id", "answer", "last_question", "interview_id"}
    ),
    "ouroboros_generate_seed": frozenset(
        {"session_id", "ambiguity_score", "client_gates", "force"}
    ),
    "ouroboros_evaluate": frozenset(
        {
            "session_id",
            "artifact",
            "acceptance_criterion",
            "acceptance_criteria",
            "artifact_type",
            "trigger_consensus",
        }
    ),
    "ouroboros_session_status": frozenset({"session_id"}),
}

FORBIDDEN_PUBLIC_FIELDS = frozenset(
    {
        "workspace_root",
        "workspace_id",
        "cwd",
        "working_dir",
        "path",
        "file_path",
        "command",
        "shell",
        "terminal",
        "api_key",
        "provider_key",
        "host_dispatch",
        "host_work_order",
    }
)


def validate_public_arguments(
    arguments: Mapping[str, Any], *, tool_name: str | None = None
) -> Mapping[str, Any]:
    """Accept only declared public fields and reject hosted-boundary escapes."""
    if tool_name is not None:
        allowed = PUBLIC_TOOL_FIELDS.get(tool_name)
        if allowed is None:
            raise PublicInputError(f"tool is not public: {tool_name}")
        unexpected = sorted(set(arguments) - allowed)
        if unexpected:
            raise PublicInputError(f"public input field is not allowed: {unexpected[0]}")

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, nested in value.items():
                if str(key).casefold() in FORBIDDEN_PUBLIC_FIELDS:
                    raise PublicInputError(f"public input field is not allowed: {key}")
                visit(nested)
        elif isinstance(value, (list, tuple)):
            for nested in value:
                visit(nested)

    visit(arguments)
    return arguments


def build_public_registry(source: ToolRegistry) -> ToolRegistry:
    """Reuse the required Full handlers and omit every internal-only tool."""
    missing = [name for name in PUBLIC_TOOL_POLICIES if source.get(name) is None]
    if missing:
        raise ValueError(f"missing required Full tool: {', '.join(missing)}")

    public = ToolRegistry()
    for name in PUBLIC_TOOL_POLICIES:
        handler = source.get(name)
        assert handler is not None
        public.register(handler, category="public")
    return public
