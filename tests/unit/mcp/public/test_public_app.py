from dataclasses import dataclass
from pathlib import Path

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.public.app import PublicToolHandler, create_public_server
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.types import ContentType, MCPContentItem, MCPToolDefinition, MCPToolResult


@dataclass
class StubHandler:
    definition: MCPToolDefinition
    seen: dict | None = None

    async def handle(self, arguments):
        self.seen = arguments
        return arguments

    async def close(self):
        self.closed = True


def full_server():
    server = MCPServerAdapter(name="full")
    for name in (
        "ouroboros_interview",
        "ouroboros_generate_seed",
        "ouroboros_evaluate",
        "ouroboros_session_status",
        "ouroboros_execute_seed",
    ):
        server.register_tool(StubHandler(MCPToolDefinition(name=name, description=name)))
    return server


@pytest.mark.asyncio
async def test_public_shutdown_closes_full_source_resources():
    source = full_server()
    closed = False

    async def shutdown():
        nonlocal closed
        closed = True

    source.shutdown = shutdown
    public = create_public_server(source=source)

    await public.shutdown()

    assert closed is True


def test_public_server_reuses_only_allowed_full_handlers():
    source = full_server()

    public = create_public_server(source=source)

    assert {tool.name for tool in public.info.tools} == {
        "ouroboros_interview",
        "ouroboros_generate_seed",
        "ouroboros_evaluate",
        "ouroboros_session_status",
    }
    assert public.info.resources == ()
    assert public.info.prompts == ()
    assert public._instructions.startswith("Use Ouroboros when")
    assert public.get_tool_handler("ouroboros_session_status").annotations.readOnlyHint is True
    assert public.get_tool_handler("ouroboros_interview").annotations.readOnlyHint is False
    assert public.get_tool_handler("ouroboros_session_status").annotations.openWorldHint is False
    assert all(
        public.get_tool_handler(name).annotations.openWorldHint is False
        for name in {tool.name for tool in public.info.tools}
    )
    schemas = {tool.name: tool.to_input_schema() for tool in public.info.tools}
    assert "cwd" not in schemas["ouroboros_interview"]["properties"]
    assert "working_dir" not in schemas["ouroboros_evaluate"]["properties"]


@pytest.mark.asyncio
async def test_public_interview_forces_literal_context():
    source = full_server()
    public = create_public_server(source=source)

    await public.call_tool("ouroboros_interview", {"initial_context": "/etc/passwd"})

    original = source.get_tool_handler("ouroboros_interview")
    assert original.seen["initial_context"] == "User-provided context:\n/etc/passwd"


@pytest.mark.asyncio
async def test_public_evaluate_uses_ephemeral_working_directory():
    source = full_server()
    public = create_public_server(source=source)

    await public.call_tool("ouroboros_evaluate", {"session_id": "s", "artifact": "draft"})

    original = source.get_tool_handler("ouroboros_evaluate")
    assert original.seen["working_dir"].startswith("/tmp/ouroboros-public-eval-")
    assert Path(original.seen["working_dir"]).is_dir()

    await public.shutdown()

    assert not Path(original.seen["working_dir"]).exists()


@pytest.mark.asyncio
async def test_public_server_validates_arguments_before_full_handler():
    source = full_server()
    public = create_public_server(source=source)

    result = await public.call_tool("ouroboros_interview", {"workspace_root": "/tmp"})

    assert result.is_err
    original = source.get_tool_handler("ouroboros_interview")
    assert original.seen is None


def test_server_adapter_exposes_registered_handler_without_mutation():
    source = full_server()

    handler = source.get_tool_handler("ouroboros_interview")

    assert handler is not None
    assert handler is source.get_tool_handler("ouroboros_interview")
    assert source.get_tool_handler("missing") is None


@pytest.mark.asyncio
async def test_public_handler_removes_internal_response_metadata():
    handler = StubHandler(MCPToolDefinition(name="ouroboros_session_status", description="internal"))

    async def handle(arguments):
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="visible"),),
                meta={
                    "session_id": "session-1",
                    "status": "running",
                    "internal_reasoning": ["secret"],
                    "host_action": "spawn_subagents",
                    "provider_key": "secret",
                },
                structured_content={"internal_dispatch": "secret"},
            )
        )

    handler.handle = handle

    result = await PublicToolHandler(handler).handle({"session_id": "session-1"})

    assert result.is_ok
    assert result.value.meta == {"session_id": "session-1", "status": "running"}
    assert result.value.structured_content is None
    assert result.value.text_content == "visible"


def test_public_descriptions_hide_local_plugin_runtime_terms():
    public = create_public_server(source=full_server())

    descriptions = " ".join(tool.description for tool in public.info.tools).casefold()

    assert "opencode" not in descriptions
    assert "task pane" not in descriptions
    assert "delegated_to_subagent" not in descriptions
