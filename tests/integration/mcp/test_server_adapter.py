"""Integration tests for MCPServerAdapter.

These tests verify that the MCPServerAdapter correctly handles tool
registration, resource handling, and the full server lifecycle.
"""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ouroboros import __version__
from ouroboros.bigbang.interview import InterviewRound, InterviewState, InterviewStatus
from ouroboros.config.models import (
    EvaluationConfig,
    LLMConfig,
    OrchestratorConfig,
    OuroborosConfig,
    ResilienceConfig,
    RuntimeProfileConfig,
)
from ouroboros.core.types import Result
from ouroboros.interview_adapters import ReferenceCue, ReferenceOrigin
from ouroboros.mcp.errors import MCPResourceNotFoundError, MCPToolError
from ouroboros.mcp.server.adapter import MCPServerAdapter, create_ouroboros_server
from ouroboros.mcp.server.security import AuthConfig, AuthMethod, RateLimitConfig
from ouroboros.mcp.tools.authoring_handlers import GenerateSeedHandler
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPToolDefinition,
    MCPToolResult,
    ToolInputType,
)

from .conftest import (
    AddToolHandler,
    DynamicResourceHandler,
    EchoToolHandler,
    FailingToolHandler,
    GreetingPromptHandler,
    StaticResourceHandler,
)


class TestMCPServerAdapterLifecycle:
    """Test MCPServerAdapter lifecycle operations."""

    def test_create_server_with_defaults(self) -> None:
        """Server can be created with default configuration."""
        server = MCPServerAdapter()

        assert server.info.name == "ouroboros-mcp"
        assert server.info.version == __version__
        assert server.info.capabilities.tools is False
        assert server.info.capabilities.resources is False
        assert server.info.capabilities.prompts is False
        assert server.info.capabilities.logging is True

    def test_create_server_with_custom_config(self) -> None:
        """Server can be created with custom configuration."""
        server = MCPServerAdapter(
            name="custom-server",
            version="2.0.0",
        )

        assert server.info.name == "custom-server"
        assert server.info.version == "2.0.0"

    def test_create_server_with_security_config(self) -> None:
        """Server can be created with security configuration."""
        auth_config = AuthConfig(
            method=AuthMethod.API_KEY,
            api_keys=frozenset(["test-key"]),
            required=True,
        )
        rate_limit_config = RateLimitConfig(
            enabled=True,
            requests_per_minute=100,
        )

        server = MCPServerAdapter(
            auth_config=auth_config,
            rate_limit_config=rate_limit_config,
        )

        assert server.info.name == "ouroboros-mcp"


class TestMCPServerAdapterToolRegistration:
    """Test MCPServerAdapter tool registration."""

    def test_register_single_tool(
        self,
        echo_handler: EchoToolHandler,
    ) -> None:
        """Single tool can be registered."""
        server = MCPServerAdapter()

        server.register_tool(echo_handler)

        assert server.info.capabilities.tools is True
        assert len(server.info.tools) == 1
        assert server.info.tools[0].name == "echo"

    @pytest.mark.asyncio
    async def test_call_tool_preserves_structured_mcp_error_details(
        self,
        echo_handler: EchoToolHandler,
    ) -> None:
        """Recovery receipts must survive the server adapter unchanged."""
        server = MCPServerAdapter()
        server.register_tool(echo_handler)
        expected = MCPToolError(
            "Detached worker acceptance is pending",
            tool_name="echo",
            error_code="detached_job_acceptance_pending",
            details={"job_id": "job_1", "status": "acceptance_pending"},
        )
        echo_handler.handle = AsyncMock(side_effect=expected)  # type: ignore[method-assign]

        result = await server.call_tool("echo", {})

        assert result.is_err
        assert result.error is expected
        assert result.error.details == {
            "job_id": "job_1",
            "status": "acceptance_pending",
        }

    def test_register_multiple_tools(
        self,
        echo_handler: EchoToolHandler,
        add_handler: AddToolHandler,
    ) -> None:
        """Multiple tools can be registered."""
        server = MCPServerAdapter()

        server.register_tool(echo_handler)
        server.register_tool(add_handler)

        assert len(server.info.tools) == 2
        tool_names = {t.name for t in server.info.tools}
        assert "echo" in tool_names
        assert "add" in tool_names

    def test_tool_definition_preserved(
        self,
        echo_handler: EchoToolHandler,
    ) -> None:
        """Tool definition details are preserved after registration."""
        server = MCPServerAdapter()

        server.register_tool(echo_handler)

        tool = server.info.tools[0]
        assert tool.name == "echo"
        assert tool.description == "Echoes the input message"
        assert len(tool.parameters) == 1
        assert tool.parameters[0].name == "message"
        assert tool.parameters[0].type == ToolInputType.STRING
        assert tool.parameters[0].required is True


class TestMCPServerAdapterToolExecution:
    """Test MCPServerAdapter tool execution."""

    @pytest.mark.asyncio
    async def test_call_echo_tool(
        self,
        echo_handler: EchoToolHandler,
    ) -> None:
        """Echo tool executes and returns result."""
        server = MCPServerAdapter()
        server.register_tool(echo_handler)

        result = await server.call_tool("echo", {"message": "Hello!"})

        assert result.is_ok
        assert result.value.text_content == "Echo: Hello!"
        assert result.value.is_error is False

    @pytest.mark.asyncio
    async def test_call_add_tool(
        self,
        add_handler: AddToolHandler,
    ) -> None:
        """Add tool executes with numeric arguments."""
        server = MCPServerAdapter()
        server.register_tool(add_handler)

        result = await server.call_tool("add", {"a": 10, "b": 20})

        assert result.is_ok
        assert result.value.text_content == "30"

    @pytest.mark.asyncio
    async def test_call_tool_not_found(self) -> None:
        """Calling unregistered tool returns error."""
        server = MCPServerAdapter()

        result = await server.call_tool("nonexistent", {})

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)
        assert result.error.resource_type == "tool"
        assert result.error.resource_id == "nonexistent"

    @pytest.mark.asyncio
    async def test_call_tool_with_handler_error(
        self,
        failing_handler: FailingToolHandler,
    ) -> None:
        """Handler error is caught and returned as Result error."""
        server = MCPServerAdapter()
        server.register_tool(failing_handler)

        result = await server.call_tool("fail", {})

        assert result.is_err
        assert isinstance(result.error, MCPToolError)
        assert "Intentional failure" in str(result.error)

    @pytest.mark.asyncio
    async def test_list_tools_returns_all_registered(
        self,
        echo_handler: EchoToolHandler,
        add_handler: AddToolHandler,
    ) -> None:
        """list_tools returns all registered tool definitions."""
        server = MCPServerAdapter()
        server.register_tool(echo_handler)
        server.register_tool(add_handler)

        tools = await server.list_tools()

        assert len(tools) == 2
        tool_names = {t.name for t in tools}
        assert "echo" in tool_names
        assert "add" in tool_names


class TestMCPServerAdapterResourceRegistration:
    """Test MCPServerAdapter resource registration."""

    def test_register_single_resource(
        self,
        static_resource_handler: StaticResourceHandler,
    ) -> None:
        """Single resource can be registered."""
        server = MCPServerAdapter()

        server.register_resource(static_resource_handler)

        assert server.info.capabilities.resources is True
        assert len(server.info.resources) == 1
        assert server.info.resources[0].uri == "test://static"

    def test_register_multiple_resources(self) -> None:
        """Multiple resources can be registered."""
        server = MCPServerAdapter()

        handler1 = StaticResourceHandler(uri="test://resource1", name="Resource 1")
        handler2 = StaticResourceHandler(uri="test://resource2", name="Resource 2")

        server.register_resource(handler1)
        server.register_resource(handler2)

        assert len(server.info.resources) == 2
        uris = {r.uri for r in server.info.resources}
        assert "test://resource1" in uris
        assert "test://resource2" in uris


class TestMCPServerAdapterResourceReading:
    """Test MCPServerAdapter resource reading."""

    @pytest.mark.asyncio
    async def test_read_static_resource(
        self,
        static_resource_handler: StaticResourceHandler,
    ) -> None:
        """Static resource can be read."""
        server = MCPServerAdapter()
        server.register_resource(static_resource_handler)

        result = await server.read_resource("test://static")

        assert result.is_ok
        assert result.value.uri == "test://static"
        assert result.value.text == "Static content"

    @pytest.mark.asyncio
    async def test_read_dynamic_resource(self) -> None:
        """Dynamic resource generates content correctly."""
        server = MCPServerAdapter()

        handler = DynamicResourceHandler(uri_prefix="test://dynamic")
        handler.set_data("key1", "value1")
        server.register_resource(handler)

        result = await server.read_resource("test://dynamic/key1")

        assert result.is_ok
        assert result.value.text == "value1"

    @pytest.mark.asyncio
    async def test_read_resource_not_found(self) -> None:
        """Reading unregistered resource returns error."""
        server = MCPServerAdapter()

        result = await server.read_resource("test://nonexistent")

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)

    @pytest.mark.asyncio
    async def test_list_resources_returns_all_registered(self) -> None:
        """list_resources returns all registered resource definitions."""
        server = MCPServerAdapter()

        handler1 = StaticResourceHandler(uri="test://r1", name="Resource 1")
        handler2 = StaticResourceHandler(uri="test://r2", name="Resource 2")

        server.register_resource(handler1)
        server.register_resource(handler2)

        resources = await server.list_resources()

        assert len(resources) == 2
        uris = {r.uri for r in resources}
        assert "test://r1" in uris
        assert "test://r2" in uris


class TestMCPServerAdapterPromptRegistration:
    """Test MCPServerAdapter prompt registration."""

    def test_register_prompt(
        self,
        greeting_prompt_handler: GreetingPromptHandler,
    ) -> None:
        """Prompt can be registered."""
        server = MCPServerAdapter()

        server.register_prompt(greeting_prompt_handler)

        assert server.info.capabilities.prompts is True
        assert len(server.info.prompts) == 1
        assert server.info.prompts[0].name == "greeting"


class TestMCPServerAdapterPromptGeneration:
    """Test MCPServerAdapter prompt generation."""

    @pytest.mark.asyncio
    async def test_get_prompt(
        self,
        greeting_prompt_handler: GreetingPromptHandler,
    ) -> None:
        """Prompt can be retrieved and filled."""
        server = MCPServerAdapter()
        server.register_prompt(greeting_prompt_handler)

        result = await server.get_prompt("greeting", {"name": "Bob"})

        assert result.is_ok
        assert result.value == "Hello, Bob!"

    @pytest.mark.asyncio
    async def test_get_prompt_not_found(self) -> None:
        """Getting unregistered prompt returns error."""
        server = MCPServerAdapter()

        result = await server.get_prompt("nonexistent", {})

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)

    @pytest.mark.asyncio
    async def test_list_prompts_returns_all_registered(
        self,
        greeting_prompt_handler: GreetingPromptHandler,
    ) -> None:
        """list_prompts returns all registered prompt definitions."""
        server = MCPServerAdapter()
        server.register_prompt(greeting_prompt_handler)

        prompts = await server.list_prompts()

        assert len(prompts) == 1
        assert prompts[0].name == "greeting"

    @pytest.mark.asyncio
    async def test_public_v2_boundary_preserves_wire_argument_names(self) -> None:
        """Prompt arguments are MCP wire names, not Python identifiers."""
        from mcp import Client
        from mcp.server import MCPServer

        class FilePromptHandler:
            @property
            def definition(self) -> MCPPromptDefinition:
                return MCPPromptDefinition(
                    name="review-file",
                    arguments=(MCPPromptArgument(name="file-path", required=True),),
                )

            async def handle(self, arguments: dict[str, str]):
                return Result.ok(f"Review {arguments['file-path']}")

        adapter = MCPServerAdapter()
        adapter.register_prompt(FilePromptHandler())

        with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
            await adapter.serve(transport="stdio")

        async with Client(adapter._mcp_server, mode="auto") as client:
            listed = (await client.list_prompts()).prompts[0]
            rendered = await client.get_prompt(
                "review-file",
                {"file-path": "docs/seed.yaml"},
            )

        assert listed.arguments is not None
        assert listed.arguments[0].name == "file-path"
        assert rendered.messages[0].content.text == "Review docs/seed.yaml"


class TestMCPServerAdapterIntegration:
    """Integration tests for complete server workflows."""

    @pytest.mark.asyncio
    async def test_full_tool_workflow(
        self,
        echo_handler: EchoToolHandler,
        add_handler: AddToolHandler,
    ) -> None:
        """Complete workflow: register, list, call multiple tools."""
        server = MCPServerAdapter(name="integration-test")

        # Register tools
        server.register_tool(echo_handler)
        server.register_tool(add_handler)

        # Verify registration
        tools = await server.list_tools()
        assert len(tools) == 2

        # Call echo tool
        echo_result = await server.call_tool("echo", {"message": "Integration test"})
        assert echo_result.is_ok
        assert "Integration test" in echo_result.value.text_content

        # Call add tool
        add_result = await server.call_tool("add", {"a": 100, "b": 50})
        assert add_result.is_ok
        assert add_result.value.text_content == "150"

    @pytest.mark.asyncio
    async def test_full_resource_workflow(self) -> None:
        """Complete workflow: register, list, read multiple resources."""
        server = MCPServerAdapter(name="resource-test")

        # Register resources
        config_handler = StaticResourceHandler(
            uri="ouroboros://config",
            name="Configuration",
            content='{"debug": true}',
        )
        status_handler = StaticResourceHandler(
            uri="ouroboros://status",
            name="Status",
            content="RUNNING",
        )

        server.register_resource(config_handler)
        server.register_resource(status_handler)

        # Verify registration
        resources = await server.list_resources()
        assert len(resources) == 2

        # Read resources
        config_result = await server.read_resource("ouroboros://config")
        assert config_result.is_ok
        assert config_result.value.text == '{"debug": true}'

        status_result = await server.read_resource("ouroboros://status")
        assert status_result.is_ok
        assert status_result.value.text == "RUNNING"

    @pytest.mark.asyncio
    async def test_mixed_handler_types(
        self,
        echo_handler: EchoToolHandler,
        static_resource_handler: StaticResourceHandler,
        greeting_prompt_handler: GreetingPromptHandler,
    ) -> None:
        """Server can handle tools, resources, and prompts together."""
        server = MCPServerAdapter(name="mixed-test")

        # Register all handler types
        server.register_tool(echo_handler)
        server.register_resource(static_resource_handler)
        server.register_prompt(greeting_prompt_handler)

        # Verify capabilities
        info = server.info
        assert info.capabilities.tools is True
        assert info.capabilities.resources is True
        assert info.capabilities.prompts is True

        # Execute tool
        tool_result = await server.call_tool("echo", {"message": "mixed"})
        assert tool_result.is_ok

        # Read resource
        resource_result = await server.read_resource("test://static")
        assert resource_result.is_ok

        # Get prompt
        prompt_result = await server.get_prompt("greeting", {"name": "Mixed"})
        assert prompt_result.is_ok

    @pytest.mark.asyncio
    async def test_public_v2_client_discovers_and_invokes_all_primitives(
        self,
        echo_handler: EchoToolHandler,
        static_resource_handler: StaticResourceHandler,
        greeting_prompt_handler: GreetingPromptHandler,
    ) -> None:
        """The public v2 Client sees the adapter's complete MCP surface."""
        from mcp import Client
        from mcp.server import MCPServer

        adapter = MCPServerAdapter(name="boundary-test", version="2.3.4")
        adapter.register_tool(echo_handler)
        adapter.register_resource(static_resource_handler)
        adapter.register_prompt(greeting_prompt_handler)

        with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
            await adapter.serve(transport="stdio")

        async with Client(adapter._mcp_server, mode="auto") as client:
            tools = await client.list_tools()
            resources = await client.list_resources()
            prompts = await client.list_prompts()
            tool_result = await client.call_tool("echo", {"message": "v2"})
            resource_result = await client.read_resource("test://static")
            prompt_result = await client.get_prompt("greeting", {"name": "MCP"})

        assert [tool.name for tool in tools.tools] == ["echo"]
        assert [str(resource.uri) for resource in resources.resources] == ["test://static"]
        assert [prompt.name for prompt in prompts.prompts] == ["greeting"]
        assert tool_result.content[0].text == "Echo: v2"
        assert resource_result.contents[0].text == "Static content"
        assert prompt_result.messages[0].content.text == "Hello, MCP!"

    @pytest.mark.asyncio
    async def test_public_v2_generate_seed_preserves_typed_reopen_error(self) -> None:
        """The public generate-seed path exposes machine-readable recovery data."""
        from mcp import Client
        from mcp.server import MCPServer
        from mcp.shared.exceptions import MCPError as SDKMCPError

        state = InterviewState(
            interview_id="session-unresolved-reference",
            initial_context="Build an issue tool",
            status=InterviewStatus.COMPLETED,
            ambiguity_score=0.1,
            rounds=[
                InterviewRound(
                    round_number=1,
                    question="What outcome matters most?",
                    user_response="Fast triage.",
                )
            ],
            reference_cues=(
                ReferenceCue(
                    reference_id="linear",
                    label="Linear-like",
                    origin=ReferenceOrigin.USER_TEXT,
                ),
            ),
        )
        handler = GenerateSeedHandler(
            agent_runtime_backend="opencode",
            opencode_mode="plugin",
        )
        adapter = MCPServerAdapter(name="typed-error-boundary")
        adapter.register_tool(handler)

        with (
            patch(
                "ouroboros.mcp.tools.authoring_handlers._plugin_load_state",
                AsyncMock(return_value=Result.ok(state)),
            ),
            patch.object(MCPServer, "run_stdio_async", new=AsyncMock()),
        ):
            await adapter.serve(transport="stdio")
            async with Client(adapter._mcp_server, mode="auto") as client:
                with pytest.raises(SDKMCPError) as error_info:
                    await client.call_tool(
                        "ouroboros_generate_seed",
                        {"session_id": state.interview_id},
                    )

        assert error_info.value.data == {
            "error_code": "interview_reopen_required",
            "details": {
                "code": "interview_reopen_required",
                "blockers": [
                    {
                        "candidate_id": "reference-0:contrast-required",
                        "code": "reference_confirmation_required",
                        "reason": "required_unknown",
                        "section": "context",
                        "reference_ids": ["linear"],
                    }
                ],
            },
        }

    @pytest.mark.asyncio
    async def test_public_v2_boundary_keeps_untyped_errors_as_tool_results(self) -> None:
        """Existing untyped failures remain ordinary MCP execution-error results."""
        from mcp import Client
        from mcp.server import MCPServer

        class UntypedErrorTool:
            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name="untyped_error",
                    description="Return an ordinary tool failure",
                )

            async def handle(self, arguments: dict[str, object]):
                del arguments
                return Result.err(
                    MCPToolError(
                        "Ordinary tool failure",
                        tool_name="untyped_error",
                    )
                )

        adapter = MCPServerAdapter(name="untyped-error-boundary")
        adapter.register_tool(UntypedErrorTool())

        with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
            await adapter.serve(transport="stdio")

        async with Client(adapter._mcp_server, mode="auto") as client:
            result = await client.call_tool("untyped_error", {})

        assert result.is_error is True
        assert result.content[0].text == "Ordinary tool failure"

    @pytest.mark.asyncio
    async def test_public_v2_boundary_does_not_expose_non_json_tool_error_details(
        self,
    ) -> None:
        """Malformed typed details fall back to the SDK's ordinary safe error result."""
        from mcp import Client
        from mcp.server import MCPServer

        class UnsafeDetails:
            def __repr__(self) -> str:
                return "private-runtime-object"

        class UnsafeTypedErrorTool:
            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name="unsafe_typed_error",
                    description="Return malformed typed details",
                )

            async def handle(self, arguments: dict[str, object]):
                del arguments
                return Result.err(
                    MCPToolError(
                        "Typed tool failure",
                        tool_name="unsafe_typed_error",
                        error_code="unsafe_details_probe",
                        details={"private": UnsafeDetails()},
                    )
                )

        adapter = MCPServerAdapter(name="unsafe-details-boundary")
        adapter.register_tool(UnsafeTypedErrorTool())

        with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
            await adapter.serve(transport="stdio")

        async with Client(adapter._mcp_server, mode="auto") as client:
            result = await client.call_tool("unsafe_typed_error", {})

        assert result.is_error is True
        assert result.content[0].text == "Typed tool failure"
        assert "private-runtime-object" not in result.content[0].text

    @pytest.mark.asyncio
    async def test_public_v2_boundary_preserves_canonical_schema_and_binary_resource(
        self,
    ) -> None:
        """The production adapter path is lossless, not only the standalone mapper."""
        from mcp import Client
        from mcp.server import MCPServer

        input_schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$defs": {"payload": {"type": "array", "items": {"type": "integer"}}},
            "type": "object",
            "properties": {"payload": {"$ref": "#/$defs/payload"}},
            "required": ["payload"],
            "additionalProperties": False,
        }
        output_schema = {
            "type": "object",
            "properties": {"result": {"$ref": "#/$defs/payload"}},
            "$defs": input_schema["$defs"],
            "required": ["result"],
        }

        class ComplexTool:
            @property
            def definition(self) -> MCPToolDefinition:
                return MCPToolDefinition(
                    name="complex",
                    description="Preserve a canonical schema",
                    input_schema=input_schema,
                    output_schema=output_schema,
                    title="Complex schema",
                    meta={"owner": "ouroboros"},
                )

            async def handle(self, arguments: dict[str, object]):
                payload = arguments["payload"]
                return Result.ok(
                    MCPToolResult(
                        content=(MCPContentItem(type=ContentType.TEXT, text="ok"),),
                        structured_content={"result": payload},
                    )
                )

        class BinaryResource:
            @property
            def definitions(self):
                return (
                    MCPResourceDefinition(
                        uri="test://binary",
                        name="Binary payload",
                        title="Binary",
                        description="Three bytes",
                        mime_type="application/octet-stream",
                        size=3,
                        meta={"owner": "ouroboros"},
                    ),
                )

            async def handle(self, uri: str):
                return Result.ok(
                    MCPResourceContent(
                        uri=uri,
                        blob="AAEC",
                        mime_type="application/octet-stream",
                        meta={"owner": "ouroboros"},
                    )
                )

        adapter = MCPServerAdapter()
        adapter.register_tool(ComplexTool())
        adapter.register_resource(BinaryResource())

        with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
            await adapter.serve(transport="stdio")

        async with Client(adapter._mcp_server, mode="auto") as client:
            tool = (await client.list_tools()).tools[0]
            resource = (await client.list_resources()).resources[0]
            called = await client.call_tool("complex", {"payload": [1, 2, 3]})
            read = await client.read_resource("test://binary")

            assert client.server_info.version == __version__

        assert tool.input_schema == input_schema
        assert tool.output_schema == output_schema
        assert tool.title == "Complex schema"
        assert tool.meta == {"owner": "ouroboros"}
        assert called.structured_content == {"result": [1, 2, 3]}
        assert resource.name == "Binary payload"
        assert resource.title == "Binary"
        assert resource.description == "Three bytes"
        assert resource.mime_type == "application/octet-stream"
        assert resource.size == 3
        assert resource.meta == {"owner": "ouroboros"}
        assert read.contents[0].blob == "AAEC"
        assert read.contents[0].mime_type == "application/octet-stream"
        assert read.contents[0].meta == {"owner": "ouroboros"}

    @pytest.mark.asyncio
    async def test_server_info_updates_dynamically(self) -> None:
        """Server info reflects current state as handlers are added."""
        server = MCPServerAdapter()

        # Initially empty
        assert server.info.capabilities.tools is False
        assert len(server.info.tools) == 0

        # Add first tool
        server.register_tool(EchoToolHandler())
        assert server.info.capabilities.tools is True
        assert len(server.info.tools) == 1

        # Add second tool
        server.register_tool(AddToolHandler())
        assert len(server.info.tools) == 2

        # Add resource
        server.register_resource(StaticResourceHandler())
        assert server.info.capabilities.resources is True

        # Add prompt
        server.register_prompt(GreetingPromptHandler())
        assert server.info.capabilities.prompts is True


class TestCreateOuroborosServer:
    """Test the create_ouroboros_server factory function."""

    EXPECTED_OUROBOROS_SERVER_TOOLS = {
        "ouroboros_ac_dashboard",
        "ouroboros_ac_tree_hud",
        "ouroboros_auto",
        "ouroboros_brownfield",
        "ouroboros_cancel_execution",
        "ouroboros_cancel_job",
        "ouroboros_checklist_verify",
        "ouroboros_evaluate",
        "ouroboros_evolve_rewind",
        "ouroboros_evolve_step",
        "ouroboros_execute_seed",
        "ouroboros_fetch_artifact",
        "ouroboros_generate_seed",
        "ouroboros_interview",
        "ouroboros_job_result",
        "ouroboros_job_status",
        "ouroboros_job_wait",
        "ouroboros_lateral_think",
        "ouroboros_lineage_status",
        "ouroboros_measure_drift",
        "ouroboros_pm_interview",
        "ouroboros_project_status",
        "ouroboros_qa",
        "ouroboros_query_events",
        "ouroboros_query_projection",
        "ouroboros_ralph",
        "ouroboros_record_conductor_decision",
        "ouroboros_session_status",
        "ouroboros_session_signal",
        "ouroboros_session_signal_targets",
        "ouroboros_start_auto",
        "ouroboros_start_evaluate",
        "ouroboros_start_evolve_step",
        "ouroboros_start_execute_seed",
        "ouroboros_start_ralph",
        # Added in #1754. This set held the re-entry tool's ABSENCE in place:
        # `skills/interview/SKILL.md` documented the tool while this pin
        # asserted the shipped server did not have it, so the wiring gap had a
        # guardian rather than merely lacking a test.
        "ouroboros_submit_fanout_results",
    }

    def test_creates_server_with_defaults(self) -> None:
        """Factory creates server with default configuration."""
        server = create_ouroboros_server()

        assert server.info.name == "ouroboros-mcp"
        assert server.info.version == __version__
        tool_names = {tool.name for tool in server.info.tools}
        assert len(tool_names) == 36
        assert tool_names == self.EXPECTED_OUROBOROS_SERVER_TOOLS

    def test_checklist_verify_reuses_the_registered_evaluator(self) -> None:
        """Checklist verification must not create a parallel evaluation authority."""
        from ouroboros.mcp.tools.evaluation_handlers import (
            ChecklistVerifyHandler,
            EvaluateHandler,
        )

        server = create_ouroboros_server()
        evaluate = server._tool_handlers["ouroboros_evaluate"]
        checklist = server._tool_handlers["ouroboros_checklist_verify"]

        assert isinstance(evaluate, EvaluateHandler)
        assert isinstance(checklist, ChecklistVerifyHandler)
        assert checklist.evaluate_handler is evaluate

    @pytest.mark.asyncio
    async def test_public_client_initializes_lists_and_routes_checklist_verify(self) -> None:
        """The shipped server exposes and invokes checklist verify across MCP v2."""
        from mcp import Client
        from mcp.server import MCPServer

        server = create_ouroboros_server(runtime_backend="claude_mcp")
        with patch.object(MCPServer, "run_stdio_async", new=AsyncMock()):
            await server.serve(transport="stdio")

        async with Client(server._mcp_server, mode="auto") as client:
            tool_names = {tool.name for tool in (await client.list_tools()).tools}
            result = await client.call_tool(
                "ouroboros_checklist_verify",
                {
                    "session_id": "ses_issue_1978",
                    "seed_content": "goal: missing acceptance criteria",
                    "artifact": "candidate",
                },
            )

            assert client.server_info.name == "ouroboros-mcp"

        assert "ouroboros_checklist_verify" in tool_names
        assert result.is_error is True
        assert "Seed validation failed" in result.content[0].text

    @pytest.mark.asyncio
    async def test_synapse_control_and_execution_paths_use_durable_relay(self) -> None:
        """Control persists remotely while each execution process owns its live queue."""
        from ouroboros.mcp.tools.evolution_handlers import EvolveStepHandler
        from ouroboros.mcp.tools.execution_handlers import (
            ExecuteSeedHandler,
            StartExecuteSeedHandler,
        )
        from ouroboros.mcp.tools.synapse_handler import SynapseSignalHandler
        from ouroboros.persistence.event_store import EventStore

        captured_runner_kwargs: dict[str, object] = {}

        class CapturingRunner:
            def __init__(self, **kwargs: object) -> None:
                captured_runner_kwargs.update(kwargs)

            async def execute_seed(self, **_kwargs: object) -> str:
                return "evolve execution completed"

        store = EventStore("sqlite+aiosqlite:///:memory:")
        with patch("ouroboros.orchestrator.runner.OrchestratorRunner", CapturingRunner):
            server = create_ouroboros_server(event_store=store)

        try:
            runtime_context = server._runtime_context
            execute = server._tool_handlers["ouroboros_execute_seed"]
            start_execute = server._tool_handlers["ouroboros_start_execute_seed"]
            evolve = server._tool_handlers["ouroboros_evolve_step"]
            signal = server._tool_handlers["ouroboros_session_signal"]

            assert runtime_context is not None
            assert runtime_context.synapse is not None
            assert isinstance(execute, ExecuteSeedHandler)
            assert isinstance(start_execute, StartExecuteSeedHandler)
            assert isinstance(evolve, EvolveStepHandler)
            assert isinstance(signal, SynapseSignalHandler)
            assert execute.session_signal_hub is runtime_context.synapse
            assert start_execute._execute_handler.session_signal_hub is runtime_context.synapse
            assert signal.mailbox.delivery_queue is None

            assert evolve.evolutionary_loop is not None
            result = await evolve.evolutionary_loop.executor(
                MagicMock(),
                execution_id="evolve:lin_test:generation:1",
            )

            assert result == "evolve execution completed"
            assert captured_runner_kwargs["session_signal_hub"] is runtime_context.synapse
        finally:
            await server.shutdown()

    def test_create_server_forwards_bridge_context_to_auto_handler(self) -> None:
        """Auto resume rebuilds should retain bridge access from server wiring."""
        from ouroboros.mcp.tools.auto_handler import AutoHandler, StartAutoHandler

        class FakeBridge:
            manager = object()
            tool_prefix = "bridge__"

        bridge = FakeBridge()
        server = create_ouroboros_server(mcp_bridge=bridge)
        auto = server._tool_handlers["ouroboros_auto"]
        start_auto = server._tool_handlers["ouroboros_start_auto"]

        assert isinstance(auto, AutoHandler)
        assert isinstance(start_auto, StartAutoHandler)
        assert auto.mcp_manager is bridge.manager
        assert auto.mcp_tool_prefix == "bridge__"
        assert start_auto._inner_auto.mcp_manager is bridge.manager
        assert start_auto._inner_auto.mcp_tool_prefix == "bridge__"

    def test_create_server_start_auto_reuses_auto_dependency_graph(self) -> None:
        """Async auto must share the same injected handlers as synchronous auto."""
        from ouroboros.mcp.tools.auto_handler import AutoHandler, StartAutoHandler

        server = create_ouroboros_server()
        auto = server._tool_handlers["ouroboros_auto"]
        start_auto = server._tool_handlers["ouroboros_start_auto"]

        assert isinstance(auto, AutoHandler)
        assert isinstance(start_auto, StartAutoHandler)
        assert start_auto._inner_auto.interview_handler is auto.interview_handler
        assert start_auto._inner_auto.generate_seed_handler is auto.generate_seed_handler
        assert start_auto._inner_auto.start_execute_seed_handler is auto.start_execute_seed_handler

    def test_create_server_registers_start_ralph_alias(self) -> None:
        """The composition root must expose the start_ralph alias too."""
        from ouroboros.mcp.tools.ralph_handlers import RalphHandler, StartRalphHandler

        server = create_ouroboros_server()

        assert isinstance(server._tool_handlers["ouroboros_ralph"], RalphHandler)
        assert isinstance(server._tool_handlers["ouroboros_start_ralph"], StartRalphHandler)
        assert (
            server._tool_handlers["ouroboros_start_ralph"].definition.description
            == StartRalphHandler().definition.description
        )

    def test_creates_server_with_custom_config(self) -> None:
        """Factory creates server with custom configuration."""
        server = create_ouroboros_server(
            name="custom",
            version="3.0.0",
        )

        assert server.info.name == "custom"
        assert server.info.version == "3.0.0"

    def test_creates_server_with_security(self) -> None:
        """Factory creates server with security configuration."""
        auth = AuthConfig(
            method=AuthMethod.API_KEY,
            api_keys=frozenset(["test-key"]),
            required=True,
        )
        rate_limit = RateLimitConfig(enabled=True)

        server = create_ouroboros_server(
            auth_config=auth,
            rate_limit_config=rate_limit,
        )

        # Server should be created without error
        assert server.info.name == "ouroboros-mcp"

    def test_codex_runtime_uses_backend_without_claude_model_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex runtime wiring does not inject Claude-only default models."""
        monkeypatch.delenv("OUROBOROS_EXECUTION_MODEL", raising=False)
        monkeypatch.delenv("OUROBOROS_VALIDATION_MODEL", raising=False)

        with patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime:
            mock_create_runtime.return_value = MagicMock()

            create_ouroboros_server(runtime_backend="codex")

        mock_create_runtime.assert_called_once()
        assert mock_create_runtime.call_args.kwargs["backend"] == "codex"
        assert mock_create_runtime.call_args.kwargs["model"] is None

    def test_codex_llm_backend_is_forwarded_to_adapter_factory(self) -> None:
        """LLM-only backend selection is routed through the shared adapter factory.

        Config is isolated (no ``runtime_profile.stages``) so the explicit
        global ``llm_backend`` is the source of truth for every stage and all
        adapters collapse to one backend. Per-stage override precedence (which
        would beat this global) is covered by
        ``test_runtime_profile_stages_drive_internal_llm_backends``.
        """
        config = OuroborosConfig(
            orchestrator=OrchestratorConfig(runtime_backend="codex", default_max_turns=15),
        )
        with (
            patch("ouroboros.config.load_config", return_value=config),
            patch("ouroboros.config.loader.load_config", return_value=config),
            patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
            patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
        ):
            mock_create_llm_adapter.return_value = MagicMock()
            mock_create_runtime.return_value = MagicMock()

            create_ouroboros_server(runtime_backend="codex", llm_backend="codex")

        assert len(mock_create_llm_adapter.call_args_list) == 3
        assert [call.kwargs["backend"] for call in mock_create_llm_adapter.call_args_list] == [
            "codex",
            "codex",
            "codex",
        ]
        assert [
            call.kwargs["frugality_proof"] for call in mock_create_llm_adapter.call_args_list
        ] == [False, True, True]
        assert {call.kwargs["max_turns"] for call in mock_create_llm_adapter.call_args_list} == {15}

    def test_evolution_adapter_factory_resolves_live_backend_with_cwd(self) -> None:
        """Per-call evolution adapter factory must not freeze startup llm_backend."""
        with (
            patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
            patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
            patch("ouroboros.evolution.wonder.WonderEngine") as mock_wonder_engine,
            patch("ouroboros.evolution.reflect.ReflectEngine") as mock_reflect_engine,
        ):
            mock_create_llm_adapter.return_value = MagicMock()
            mock_create_runtime.return_value = MagicMock()

            create_ouroboros_server(runtime_backend="codex", llm_backend="codex")

            initial_kwargs = mock_create_llm_adapter.call_args.kwargs
            factory = mock_wonder_engine.call_args.kwargs["adapter_factory"]
            assert mock_wonder_engine.call_args.kwargs["adapter_backend"] == "codex"
            assert mock_reflect_engine.call_args.kwargs["adapter_factory"] is factory
            assert mock_reflect_engine.call_args.kwargs["adapter_backend"] == "codex"

            factory()

        assert initial_kwargs["backend"] == "codex"
        assert mock_create_llm_adapter.call_args.kwargs["backend"] == "codex"
        assert mock_create_llm_adapter.call_args.kwargs["cwd"] == initial_kwargs["cwd"]
        assert mock_create_llm_adapter.call_args.kwargs["max_turns"] == 10

    def test_evolution_adapter_factory_uses_stage_backend_without_explicit_override(self) -> None:
        """Per-call evolution adapter factory resolves the Reflect stage backend."""
        with (
            patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
            patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
            patch("ouroboros.evolution.wonder.WonderEngine") as mock_wonder_engine,
            patch("ouroboros.evolution.reflect.ReflectEngine"),
        ):
            mock_create_llm_adapter.return_value = MagicMock()
            mock_create_runtime.return_value = MagicMock()

            create_ouroboros_server(runtime_backend="codex")

            factory = mock_wonder_engine.call_args.kwargs["adapter_factory"]
            assert mock_wonder_engine.call_args.kwargs["adapter_backend"] == "codex"
            factory()

        assert mock_create_llm_adapter.call_args.kwargs["backend"] == "codex"

    def test_runtime_profile_stages_drive_internal_llm_backends(self) -> None:
        """Stage Agent selections are the backend source for internal LLM calls."""
        config = OuroborosConfig(
            orchestrator=OrchestratorConfig(
                runtime_backend="claude",
                runtime_profile=RuntimeProfileConfig(
                    stages={
                        "execute": "opencode",
                        "evaluate": "gemini",
                        "reflect": "codex",
                    }
                ),
            ),
            evaluation=EvaluationConfig(semantic_model="gpt-5"),
            resilience=ResilienceConfig(reflect_model="gpt-5"),
        )

        with (
            patch("ouroboros.config.load_config", return_value=config),
            patch("ouroboros.config.loader.load_config", return_value=config),
            patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
            patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
            patch("ouroboros.evolution.wonder.WonderEngine") as mock_wonder_engine,
            patch("ouroboros.evolution.reflect.ReflectEngine") as mock_reflect_engine,
        ):
            mock_create_llm_adapter.return_value = MagicMock()
            mock_create_runtime.return_value = MagicMock()

            server = create_ouroboros_server(runtime_backend="claude", opencode_mode="subprocess")

            reflect_factory = mock_reflect_engine.call_args.kwargs["adapter_factory"]
            reflect_factory()

        adapter_backends = [
            call.kwargs["backend"] for call in mock_create_llm_adapter.call_args_list
        ]
        assert adapter_backends[:3] == ["claude", "gemini", "codex"]
        assert adapter_backends[-1] == "codex"
        assert mock_create_runtime.call_args_list[0].kwargs["backend"] == "opencode"

        execute = server._tool_handlers["ouroboros_execute_seed"]
        evaluate = server._tool_handlers["ouroboros_evaluate"]
        qa = server._tool_handlers["ouroboros_qa"]
        lateral = server._tool_handlers["ouroboros_lateral_think"]
        interview = server._tool_handlers["ouroboros_interview"]

        assert execute.agent_runtime_backend == "opencode"
        assert execute.llm_backend == "gemini"
        assert evaluate.agent_runtime_backend == "gemini"
        assert evaluate.llm_backend == "gemini"
        assert qa.agent_runtime_backend == "gemini"
        assert qa.llm_backend == "gemini"
        assert lateral.agent_runtime_backend == "codex"
        assert interview.agent_runtime_backend == "claude"
        assert interview.llm_backend == "claude"
        assert mock_wonder_engine.call_args.kwargs["adapter_backend"] == "codex"
        assert mock_reflect_engine.call_args.kwargs["adapter_backend"] == "codex"

    @pytest.mark.asyncio
    async def test_mcp_host_capability_is_not_inferred_from_reflect_worker(self) -> None:
        """A server-composed Gemini worker cannot label a Claude host sequential."""
        from ouroboros.mcp.host_context import (
            DispatchAuthority,
            HostFamily,
            HostIdentityStatus,
            MCPHostContext,
            use_mcp_host_context,
        )

        config = OuroborosConfig(
            orchestrator=OrchestratorConfig(
                runtime_backend="claude",
                runtime_profile=RuntimeProfileConfig(stages={"reflect": "gemini"}),
            ),
        )
        with (
            patch("ouroboros.config.load_config", return_value=config),
            patch("ouroboros.config.loader.load_config", return_value=config),
            patch("ouroboros.providers.create_llm_adapter", return_value=MagicMock()),
            patch("ouroboros.orchestrator.create_agent_runtime", return_value=MagicMock()),
        ):
            server = create_ouroboros_server(runtime_backend="claude")

        lateral = server._tool_handlers["ouroboros_lateral_think"]
        assert lateral.agent_runtime_backend == "gemini"
        host = MCPHostContext(
            host_family=HostFamily.CLAUDE_CODE,
            identity_status=HostIdentityStatus.KNOWN,
            dispatch_authority=DispatchAuthority.MCP_HOST,
        )
        with use_mcp_host_context(host):
            result = await lateral.handle(
                {
                    "problem_context": "stuck on X",
                    "current_approach": "tried Y",
                    "persona": "all",
                }
            )

        assert result.is_ok
        assert result.unwrap().meta["dispatch_mode"] == "host_decides"
        assert result.unwrap().meta["host_action"] == "dispatch_subagents_if_supported"

    def test_legacy_llm_backend_config_override_honored_at_composition_root(self) -> None:
        """No per-stage profile: config.llm.backend drives internal LLM roles.

        Regression: the composition root re-implemented role→backend resolution
        and dropped the documented `llm.backend` override, so roles fell back to
        orchestrator.runtime_backend instead. It must honor the same precedence
        as get_llm_backend_for_role().
        """
        config = OuroborosConfig(
            orchestrator=OrchestratorConfig(runtime_backend="claude"),
            llm=LLMConfig(backend="codex"),
        )
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("ouroboros.config.load_config", return_value=config),
            patch("ouroboros.config.loader.load_config", return_value=config),
            patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
            patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
        ):
            mock_create_llm_adapter.return_value = MagicMock()
            mock_create_runtime.return_value = MagicMock()
            server = create_ouroboros_server(runtime_backend="claude")

        interview = server._tool_handlers["ouroboros_interview"]
        qa = server._tool_handlers["ouroboros_qa"]
        evaluate = server._tool_handlers["ouroboros_evaluate"]
        # Internal LLM roles honor the legacy llm.backend override...
        assert interview.llm_backend == "codex"
        assert qa.llm_backend == "codex"
        assert evaluate.llm_backend == "codex"
        # ...while the agent runtime axis still follows orchestrator.runtime_backend.
        assert interview.agent_runtime_backend == "claude"

    def test_env_llm_backend_override_honored_at_composition_root(self) -> None:
        config = OuroborosConfig(
            orchestrator=OrchestratorConfig(runtime_backend="claude"),
            llm=LLMConfig(backend="claude_code"),
        )
        with (
            patch.dict(os.environ, {"OUROBOROS_LLM_BACKEND": "codex"}, clear=True),
            patch("ouroboros.config.load_config", return_value=config),
            patch("ouroboros.config.loader.load_config", return_value=config),
            patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
            patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
        ):
            mock_create_llm_adapter.return_value = MagicMock()
            mock_create_runtime.return_value = MagicMock()
            server = create_ouroboros_server(runtime_backend="claude")

        assert server._tool_handlers["ouroboros_qa"].llm_backend == "codex"

    def test_opencode_backend_is_accepted_at_server_creation(self) -> None:
        """OpenCode backend is forwarded through the shared adapter factory.

        Config is isolated (no ``runtime_profile.stages`` overrides) so the
        explicit global ``runtime_backend`` is the source of truth. This makes
        the test deterministic regardless of the developer's local
        ``~/.ouroboros/config.yaml``; per-stage override precedence is covered
        by ``test_runtime_profile_stages_drive_internal_llm_backends``.
        """
        config = OuroborosConfig(
            orchestrator=OrchestratorConfig(runtime_backend="opencode"),
        )

        with (
            patch("ouroboros.config.load_config", return_value=config),
            patch("ouroboros.config.loader.load_config", return_value=config),
            patch("ouroboros.providers.create_llm_adapter") as mock_create_llm_adapter,
            patch("ouroboros.orchestrator.create_agent_runtime") as mock_create_runtime,
        ):
            mock_create_llm_adapter.return_value = MagicMock()
            mock_create_runtime.return_value = MagicMock()

            create_ouroboros_server(runtime_backend="opencode", llm_backend="opencode")

        assert len(mock_create_llm_adapter.call_args_list) == 3
        assert [call.kwargs["backend"] for call in mock_create_llm_adapter.call_args_list] == [
            "opencode",
            "opencode",
            "opencode",
        ]
        assert [
            call.kwargs["frugality_proof"] for call in mock_create_llm_adapter.call_args_list
        ] == [False, True, True]
        mock_create_runtime.assert_called_once()
        assert mock_create_runtime.call_args.kwargs["backend"] == "opencode"


class TestMCPServerAdapterConcurrency:
    """Test MCPServerAdapter concurrent operations."""

    @pytest.mark.asyncio
    async def test_concurrent_tool_calls(
        self,
        echo_handler: EchoToolHandler,
    ) -> None:
        """Multiple concurrent tool calls are handled correctly."""
        server = MCPServerAdapter()
        server.register_tool(echo_handler)

        # Call tool concurrently
        tasks = [server.call_tool("echo", {"message": f"Message {i}"}) for i in range(10)]

        results = await asyncio.gather(*tasks)

        # All should succeed
        for i, result in enumerate(results):
            assert result.is_ok
            assert f"Message {i}" in result.value.text_content

    @pytest.mark.asyncio
    async def test_concurrent_resource_reads(self) -> None:
        """Multiple concurrent resource reads are handled correctly."""
        server = MCPServerAdapter()

        # Register multiple resources
        for i in range(5):
            handler = StaticResourceHandler(
                uri=f"test://resource{i}",
                name=f"Resource {i}",
                content=f"Content {i}",
            )
            server.register_resource(handler)

        # Read concurrently
        tasks = [server.read_resource(f"test://resource{i}") for i in range(5)]

        results = await asyncio.gather(*tasks)

        # All should succeed
        for i, result in enumerate(results):
            assert result.is_ok
            assert result.value.text == f"Content {i}"

    @pytest.mark.asyncio
    async def test_mixed_concurrent_operations(
        self,
        echo_handler: EchoToolHandler,
        static_resource_handler: StaticResourceHandler,
    ) -> None:
        """Mixed concurrent operations (tools + resources) work correctly."""
        server = MCPServerAdapter()
        server.register_tool(echo_handler)
        server.register_resource(static_resource_handler)

        # Mix of tool calls and resource reads
        tasks = [
            server.call_tool("echo", {"message": "concurrent"}),
            server.read_resource("test://static"),
            server.call_tool("echo", {"message": "test"}),
            server.read_resource("test://static"),
            server.call_tool("echo", {"message": "mix"}),
        ]

        results = await asyncio.gather(*tasks)

        # All should succeed
        assert all(r.is_ok for r in results)
