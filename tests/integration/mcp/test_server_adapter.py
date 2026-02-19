"""Integration tests for MCPServerAdapter.

These tests verify that the MCPServerAdapter correctly handles tool
registration, resource handling, and the full server lifecycle.
"""

import asyncio

import pytest

from ouroboros.mcp.errors import MCPResourceNotFoundError, MCPToolError
from ouroboros.mcp.server.adapter import MCPServerAdapter, create_ouroboros_server
from ouroboros.mcp.server.security import AuthConfig, AuthMethod, RateLimitConfig
from ouroboros.mcp.types import (
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
        assert server.info.version == "1.0.0"
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

    def test_creates_server_with_defaults(self) -> None:
        """Factory creates server with default configuration."""
        server = create_ouroboros_server()

        assert server.info.name == "ouroboros-mcp"
        assert server.info.version == "1.0.0"

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
