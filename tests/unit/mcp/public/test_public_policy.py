from dataclasses import dataclass

import pytest

from ouroboros.mcp.public.policy import (
    PUBLIC_TOOL_POLICIES,
    PublicInputError,
    build_public_registry,
    validate_public_arguments,
)
from ouroboros.mcp.tools.registry import ToolRegistry
from ouroboros.mcp.types import MCPToolDefinition

EXPECTED_TOOLS = {
    "ouroboros_interview",
    "ouroboros_generate_seed",
    "ouroboros_evaluate",
    "ouroboros_session_status",
}


@dataclass
class StubHandler:
    definition: MCPToolDefinition

    async def handle(self, arguments):  # pragma: no cover - registry construction only
        raise AssertionError(arguments)


def source_registry(*names: str) -> ToolRegistry:
    registry = ToolRegistry()
    for name in names:
        registry.register(StubHandler(MCPToolDefinition(name=name, description=name)))
    return registry


def test_public_policy_exposes_only_focused_full_tools():
    assert set(PUBLIC_TOOL_POLICIES) == EXPECTED_TOOLS
    assert PUBLIC_TOOL_POLICIES["ouroboros_session_status"].read_only is True
    assert PUBLIC_TOOL_POLICIES["ouroboros_interview"].read_only is False
    assert PUBLIC_TOOL_POLICIES["ouroboros_session_status"].open_world is False
    assert all(policy.open_world is False for policy in PUBLIC_TOOL_POLICIES.values())
    assert all(policy.destructive is False for policy in PUBLIC_TOOL_POLICIES.values())


def test_public_registry_reuses_full_handlers_and_drops_internal_tools():
    source = source_registry(*EXPECTED_TOOLS, "ouroboros_execute_seed", "ouroboros_host_bridge")

    public = build_public_registry(source)

    assert {tool.name for tool in public.list_tools()} == EXPECTED_TOOLS
    for name in EXPECTED_TOOLS:
        assert public.get(name) is source.get(name)


def test_public_registry_fails_closed_when_required_full_tool_is_missing():
    with pytest.raises(ValueError, match="missing required Full tool"):
        build_public_registry(source_registry("ouroboros_interview"))


@pytest.mark.parametrize(
    "arguments",
    [
        {"workspace_root": "/tmp/project"},
        {"cwd": "/tmp/project"},
        {"working_dir": "/tmp/project"},
        {"path": "../../secret"},
        {"command": "rm -rf ."},
        {"shell": "bash"},
        {"api_key": "secret"},
        {"provider_key": "secret"},
        {"host_dispatch": {"prompt": "run"}},
        {"context": {"terminal": "run tests"}},
    ],
)
def test_public_arguments_reject_local_execution_and_provider_fields(arguments):
    with pytest.raises(PublicInputError):
        validate_public_arguments(arguments)


def test_public_arguments_accept_explicit_conversation_content():
    arguments = {
        "intent": "분기 보고서 초안을 완성한다",
        "context": {"provided_material": "매출은 전분기 대비 증가했다"},
        "acceptance_criteria": ["한 페이지", "수치 출처 표시"],
        "resume_handle": "opaque-user-handle",
    }

    assert validate_public_arguments(arguments) == arguments


def test_public_arguments_reject_fields_outside_each_tool_allowlist():
    with pytest.raises(PublicInputError, match="seed_content"):
        validate_public_arguments(
            {"session_id": "s", "artifact": "x", "seed_content": "project_dir: /tmp"},
            tool_name="ouroboros_evaluate",
        )
