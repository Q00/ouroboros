"""Behavioral compatibility matrix for the official MCP Python SDK v2.

These tests intentionally exercise the SDK's public high-level ``Client``
boundary.  Ouroboros delegates era negotiation, protocol envelopes, result
typing, and wire validation to that boundary rather than maintaining a second
protocol implementation.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.client import Client, Transport
from mcp.server import MCPServer, Server, ServerRequestContext
from mcp.shared.memory import MessageStream, create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import (
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    BlobResourceContents,
    CallToolRequestParams,
    CallToolResult,
    ErrorData,
    GetPromptRequestParams,
    GetPromptResult,
    Implementation,
    InitializeResult,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    ListPromptsResult,
    ListResourcesResult,
    ListToolsResult,
    PaginatedRequestParams,
    Prompt,
    PromptArgument,
    PromptMessage,
    ReadResourceRequestParams,
    ReadResourceResult,
    Resource,
    ServerCapabilities,
    TextContent,
    TextResourceContents,
    Tool,
)
from mcp_types.version import LATEST_HANDSHAKE_VERSION
import pytest

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

APPLICATION_VERSION = "9.8.7"


def _modern_mcpserver() -> MCPServer:
    server = MCPServer(
        "ouroboros-compatibility",
        version=APPLICATION_VERSION,
        instructions="Keep application handles explicit.",
    )

    @server.tool(description="Return a nested JSON document.")
    def inspect_payload(
        payload: list[dict[str, int | str | None]],
    ) -> list[dict[str, int | str | None]]:
        return payload

    @server.resource("test://status", mime_type="text/plain")
    def status() -> str:
        return "ready"

    @server.prompt(description="Create a concise greeting.")
    def greeting(name: str) -> str:
        return f"Hello, {name}."

    return server


async def test_auto_mode_discovers_mcpserver_without_legacy_initialize() -> None:
    """Modern auto mode adopts discovery and keeps app/protocol versions distinct."""
    server = _modern_mcpserver()

    async with Client(server, mode="auto") as client:
        assert client.protocol_version == LATEST_PROTOCOL_VERSION == "2026-07-28"
        assert client.server_info == Implementation(
            name="ouroboros-compatibility",
            version=APPLICATION_VERSION,
        )
        assert client.server_info.version != client.protocol_version
        assert client.instructions == "Keep application handles explicit."
        assert client.session.discover_result is not None
        assert client.session.initialize_result is None

        tools = await client.list_tools()
        resources = await client.list_resources()
        prompts = await client.list_prompts()

    assert [tool.name for tool in tools.tools] == ["inspect_payload"]
    assert [str(resource.uri) for resource in resources.resources] == ["test://status"]
    assert [prompt.name for prompt in prompts.prompts] == ["greeting"]


async def test_mcpserver_round_trips_schema_structured_output_and_prompt() -> None:
    """The high-level server preserves generated schemas and all three primitives."""
    server = _modern_mcpserver()
    payload = [{"count": 2, "label": "ready", "optional": None}]

    async with Client(server, mode="auto") as client:
        listed_tool = (await client.list_tools()).tools[0]
        called = await client.call_tool("inspect_payload", {"payload": payload})
        resource = await client.read_resource("test://status")
        prompt_list = await client.list_prompts()
        prompt = await client.get_prompt("greeting", {"name": "Ada"})

    assert listed_tool.input_schema["properties"]["payload"]["items"]["additionalProperties"]
    assert listed_tool.output_schema is not None
    assert listed_tool.output_schema["properties"]["result"]["type"] == "array"
    assert called.structured_content == {"result": payload}
    assert called.result_type == "complete"
    assert resource.result_type == "complete"
    assert [item.text for item in resource.contents] == ["ready"]
    assert prompt_list.prompts[0].arguments == [PromptArgument(name="name", required=True)]
    assert prompt.result_type == "complete"
    assert prompt.messages == [
        PromptMessage(role="user", content=TextContent(type="text", text="Hello, Ada."))
    ]


def _lossless_protocol_server() -> Server[Any]:
    input_schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"json": {}},
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/json"}},
        "required": ["value"],
        "unevaluatedProperties": False,
    }
    output_schema: dict[str, Any] = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"anything": {}},
        "$ref": "#/$defs/anything",
    }

    async def list_tools(
        ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool(
                    name="identity",
                    description="Return any JSON value unchanged.",
                    input_schema=input_schema,
                    output_schema=output_schema,
                ),
                # The SDK currently treats ``structured_content is None`` as
                # absent when validating an output schema.  Keep a no-schema
                # endpoint so the wire's explicit JSON-null support remains
                # covered independently of that upstream validator behavior.
                Tool(
                    name="identity-null",
                    description="Return JSON null unchanged.",
                    input_schema=input_schema,
                ),
            ]
        )

    async def call_tool(
        ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        assert params.arguments is not None
        return CallToolResult(
            content=[],
            structured_content=params.arguments["value"],
            result_type="complete",
        )

    async def list_resources(
        ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListResourcesResult:
        return ListResourcesResult(
            resources=[Resource(uri="test://bundle", name="bundle", mime_type="multipart/mixed")]
        )

    async def read_resource(
        ctx: ServerRequestContext[Any], params: ReadResourceRequestParams
    ) -> ReadResourceResult:
        return ReadResourceResult(
            contents=[
                TextResourceContents(
                    uri=params.uri,
                    text="first",
                    mime_type="text/plain",
                ),
                BlobResourceContents(
                    uri=params.uri,
                    blob="AAEC",
                    mime_type="application/octet-stream",
                ),
            ]
        )

    async def list_prompts(
        ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListPromptsResult:
        return ListPromptsResult(
            prompts=[
                Prompt(
                    name="review",
                    description="Review a named artifact.",
                    arguments=[PromptArgument(name="artifact", required=True)],
                )
            ]
        )

    async def get_prompt(
        ctx: ServerRequestContext[Any], params: GetPromptRequestParams
    ) -> GetPromptResult:
        arguments = params.arguments or {}
        return GetPromptResult(
            description="Review request",
            messages=[
                PromptMessage(
                    role="user",
                    content=TextContent(type="text", text=f"Review {arguments['artifact']}"),
                )
            ],
        )

    return Server(
        "lossless-protocol-fixture",
        version="1.2.3",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_read_resource=read_resource,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
    )


@pytest.mark.parametrize(
    "value",
    [
        pytest.param({"nested": [1, None, True]}, id="object"),
        pytest.param(["root", 2, False], id="array"),
        pytest.param("root-string", id="string"),
        pytest.param(42, id="number"),
        pytest.param(True, id="boolean"),
        pytest.param(
            None,
            id="null",
            marks=pytest.mark.xfail(
                strict=True,
                reason=(
                    "mcp 2.0.0 drops explicit structuredContent:null and its output-schema "
                    "validator treats null as an absent field"
                ),
            ),
        ),
    ],
)
async def test_modern_lowlevel_boundary_preserves_arbitrary_json(value: Any) -> None:
    """JSON Schema 2020-12 and every JSON root survive the official client boundary."""
    server = _lossless_protocol_server()

    async with Client(server, mode="auto") as client:
        listed = (await client.list_tools()).tools[0]
        tool_name = "identity-null" if value is None else "identity"
        result = await client.call_tool(tool_name, {"value": value})

    assert listed.input_schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"json": {}},
        "type": "object",
        "properties": {"value": {"$ref": "#/$defs/json"}},
        "required": ["value"],
        "unevaluatedProperties": False,
    }
    assert listed.output_schema == {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"anything": {}},
        "$ref": "#/$defs/anything",
    }
    assert result.structured_content == value
    assert "structured_content" in result.model_fields_set
    assert result.result_type == "complete"


async def test_modern_lowlevel_boundary_preserves_multi_content_and_prompts() -> None:
    """Ordered resource contents and prompt list/get survive the same client boundary."""
    server = _lossless_protocol_server()

    async with Client(server, mode="auto") as client:
        resource = await client.read_resource("test://bundle")
        prompts = await client.list_prompts()
        prompt = await client.get_prompt("review", {"artifact": "seed.yaml"})

    assert len(resource.contents) == 2
    assert isinstance(resource.contents[0], TextResourceContents)
    assert resource.contents[0].text == "first"
    assert isinstance(resource.contents[1], BlobResourceContents)
    assert resource.contents[1].blob == "AAEC"
    assert resource.result_type == "complete"
    assert [item.name for item in prompts.prompts] == ["review"]
    assert prompt.messages[0].content == TextContent(type="text", text="Review seed.yaml")
    assert prompt.result_type == "complete"


async def test_auto_mode_falls_back_after_legacy_method_not_found() -> None:
    """A hand-played legacy peer rejects discovery, then completes initialize."""
    methods_seen: list[str] = []

    async def scripted_legacy_server(streams: MessageStream) -> None:
        server_read, server_write = streams
        async for message in server_read:
            assert isinstance(message, SessionMessage)
            frame = message.message
            assert isinstance(frame, JSONRPCRequest | JSONRPCNotification)
            methods_seen.append(frame.method)
            if isinstance(frame, JSONRPCNotification):
                continue
            if frame.method == "server/discover":
                await server_write.send(
                    SessionMessage(
                        JSONRPCError(
                            jsonrpc="2.0",
                            id=frame.id,
                            error=ErrorData(code=METHOD_NOT_FOUND, message="Method not found"),
                        )
                    )
                )
            elif frame.method == "initialize":
                initialized = InitializeResult(
                    protocol_version=LATEST_HANDSHAKE_VERSION,
                    capabilities=ServerCapabilities(),
                    server_info=Implementation(name="legacy-only", version="0.4.0"),
                )
                await server_write.send(
                    SessionMessage(
                        JSONRPCResponse(
                            jsonrpc="2.0",
                            id=frame.id,
                            result=initialized.model_dump(
                                by_alias=True,
                                mode="json",
                                exclude_none=True,
                            ),
                        )
                    )
                )

    @asynccontextmanager
    async def legacy_transport() -> AsyncIterator[tuple[Any, Any]]:
        async with (
            create_client_server_memory_streams() as ((client_read, client_write), server_streams),
            anyio.create_task_group() as task_group,
        ):
            task_group.start_soon(scripted_legacy_server, server_streams)
            yield client_read, client_write
            task_group.cancel_scope.cancel()

    transport: Transport = legacy_transport()
    with anyio.fail_after(5):
        async with Client(transport, mode="auto") as client:
            assert client.protocol_version == LATEST_HANDSHAKE_VERSION
            assert client.server_info == Implementation(name="legacy-only", version="0.4.0")
            assert client.session.discover_result is None
            assert client.session.initialize_result is not None

    assert methods_seen == ["server/discover", "initialize", "notifications/initialized"]
