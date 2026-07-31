"""Shared fixtures for MCP integration tests.

This module provides mock MCP server infrastructure for testing MCP client
and server adapters without requiring real external MCP servers.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPPromptArgument,
    MCPPromptDefinition,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPServerConfig,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
    TransportType,
)

# ---------------------------------------------------------------------------
# Mock MCP Server Components
# ---------------------------------------------------------------------------


@dataclass
class MockMCPServerState:
    """State for a mock MCP server.

    Simulates the state of an MCP server including registered tools,
    resources, and prompts.
    """

    name: str = "mock-server"
    version: str = "1.0.0"
    tools: dict[str, tuple[MCPToolDefinition, Any]] = field(default_factory=dict)
    resources: dict[str, tuple[MCPResourceDefinition, str]] = field(default_factory=dict)
    prompts: dict[str, tuple[MCPPromptDefinition, str]] = field(default_factory=dict)
    initialized: bool = False
    call_log: list[dict[str, Any]] = field(default_factory=list)

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: tuple[MCPToolParameter, ...],
        handler: Any = None,
    ) -> None:
        """Register a tool with the mock server."""
        definition = MCPToolDefinition(
            name=name,
            description=description,
            parameters=parameters,
            server_name=self.name,
        )
        self.tools[name] = (definition, handler)

    def register_resource(
        self,
        uri: str,
        name: str,
        description: str,
        content: str,
    ) -> None:
        """Register a resource with the mock server."""
        definition = MCPResourceDefinition(
            uri=uri,
            name=name,
            description=description,
        )
        self.resources[uri] = (definition, content)

    def register_prompt(
        self,
        name: str,
        description: str,
        arguments: tuple[MCPPromptArgument, ...],
        template: str,
    ) -> None:
        """Register a prompt with the mock server."""
        definition = MCPPromptDefinition(
            name=name,
            description=description,
            arguments=arguments,
        )
        self.prompts[name] = (definition, template)


class MockMCPClient:
    """In-memory test double for the public MCP SDK v2 ``Client``."""

    def __init__(self, server_state: MockMCPServerState) -> None:
        """Initialize with server state.

        Args:
            server_state: The mock server state to use.
        """
        self._state = server_state
        from mcp import types as sdk_types

        self.server_info = sdk_types.Implementation(
            name=server_state.name, version=server_state.version
        )
        self.protocol_version = "2026-07-28"
        self.server_capabilities = sdk_types.ServerCapabilities(
            tools=sdk_types.ToolsCapability() if server_state.tools else None,
            resources=sdk_types.ResourcesCapability() if server_state.resources else None,
            prompts=sdk_types.PromptsCapability() if server_state.prompts else None,
            logging=sdk_types.LoggingCapability(),
        )
        self.instructions = None
        self.session = SimpleNamespace(
            discover_result=SimpleNamespace(supported_versions=(self.protocol_version,))
        )

    async def __aenter__(self) -> MockMCPClient:
        """Enter async context manager."""
        self._state.initialized = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit async context manager."""
        pass

    async def list_tools(self, *, cursor: str | None = None) -> Any:
        """List available tools.

        Returns:
            Mock result with tool list.
        """
        from mcp import types as sdk_types

        from ouroboros.mcp.sdk_mapping import tool_to_sdk

        del cursor
        return sdk_types.ListToolsResult(
            tools=[tool_to_sdk(definition) for definition, _handler in self._state.tools.values()]
        )

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Any:
        """Call a tool.

        Args:
            name: Tool name.
            arguments: Tool arguments.

        Returns:
            Mock tool result.

        Raises:
            ValueError: If tool not found.
        """
        self._state.call_log.append(
            {
                "type": "call_tool",
                "name": name,
                "arguments": arguments,
            }
        )

        if name not in self._state.tools:
            raise ValueError(f"Tool not found: {name}")

        _definition, handler = self._state.tools[name]

        # Execute handler if provided
        if handler is not None:
            text = handler(arguments)
            if inspect.isawaitable(text):
                text = await text
        else:
            text = f"Tool {name} executed with {arguments}"

        from mcp import types as sdk_types

        return sdk_types.CallToolResult(content=[sdk_types.TextContent(text=str(text))])

    async def list_resources(self, *, cursor: str | None = None) -> Any:
        """List available resources.

        Returns:
            Mock result with resource list.
        """
        from mcp import types as sdk_types

        from ouroboros.mcp.sdk_mapping import resource_to_sdk

        del cursor
        return sdk_types.ListResourcesResult(
            resources=[
                resource_to_sdk(definition)
                for definition, _content in self._state.resources.values()
            ]
        )

    async def read_resource(self, uri: str) -> Any:
        """Read a resource.

        Args:
            uri: Resource URI.

        Returns:
            Mock resource content.

        Raises:
            ValueError: If resource not found.
        """
        self._state.call_log.append(
            {
                "type": "read_resource",
                "uri": uri,
            }
        )

        if uri not in self._state.resources:
            raise ValueError(f"Resource not found: {uri}")

        _definition, content = self._state.resources[uri]

        from mcp import types as sdk_types

        return sdk_types.ReadResourceResult(
            contents=[sdk_types.TextResourceContents(uri=uri, text=content, mime_type="text/plain")]
        )

    async def list_prompts(self, *, cursor: str | None = None) -> Any:
        """List available prompts.

        Returns:
            Mock result with prompt list.
        """
        from mcp import types as sdk_types

        from ouroboros.mcp.sdk_mapping import prompt_to_sdk

        del cursor
        return sdk_types.ListPromptsResult(
            prompts=[
                prompt_to_sdk(definition) for definition, _template in self._state.prompts.values()
            ]
        )

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
    ) -> Any:
        """Get a filled prompt.

        Args:
            name: Prompt name.
            arguments: Prompt arguments.

        Returns:
            Mock prompt result.

        Raises:
            ValueError: If prompt not found.
        """
        self._state.call_log.append(
            {
                "type": "get_prompt",
                "name": name,
                "arguments": arguments,
            }
        )

        if name not in self._state.prompts:
            raise ValueError(f"Prompt not found: {name}")

        _definition, template = self._state.prompts[name]

        # Simple template substitution
        filled = template
        for key, value in arguments.items():
            filled = filled.replace(f"{{{key}}}", value)

        from mcp import types as sdk_types

        return sdk_types.GetPromptResult(
            messages=[
                sdk_types.PromptMessage(
                    role="user",
                    content=sdk_types.TextContent(text=filled),
                )
            ]
        )


# ---------------------------------------------------------------------------
# Mock Tool Handlers
# ---------------------------------------------------------------------------


class EchoToolHandler:
    """A simple echo tool handler for testing."""

    @property
    def definition(self) -> MCPToolDefinition:
        """Return tool definition."""
        return MCPToolDefinition(
            name="echo",
            description="Echoes the input message",
            parameters=(
                MCPToolParameter(
                    name="message",
                    type=ToolInputType.STRING,
                    description="The message to echo",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle the echo tool call."""
        message = arguments.get("message", "")
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=f"Echo: {message}"),),
            )
        )


class AddToolHandler:
    """A simple addition tool handler for testing."""

    @property
    def definition(self) -> MCPToolDefinition:
        """Return tool definition."""
        return MCPToolDefinition(
            name="add",
            description="Adds two numbers",
            parameters=(
                MCPToolParameter(
                    name="a",
                    type=ToolInputType.NUMBER,
                    description="First number",
                    required=True,
                ),
                MCPToolParameter(
                    name="b",
                    type=ToolInputType.NUMBER,
                    description="Second number",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle the add tool call."""
        a = arguments.get("a", 0)
        b = arguments.get("b", 0)
        result = a + b
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=str(result)),),
            )
        )


class FailingToolHandler:
    """A tool handler that always fails for testing error handling."""

    @property
    def definition(self) -> MCPToolDefinition:
        """Return tool definition."""
        return MCPToolDefinition(
            name="fail",
            description="A tool that always fails",
            parameters=(),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle by raising an error."""
        raise RuntimeError("Intentional failure for testing")


class SlowToolHandler:
    """A tool handler that takes time to execute for timeout testing."""

    def __init__(self, delay: float = 2.0) -> None:
        """Initialize with configurable delay.

        Args:
            delay: How long to wait before returning (seconds).
        """
        self._delay = delay

    @property
    def definition(self) -> MCPToolDefinition:
        """Return tool definition."""
        return MCPToolDefinition(
            name="slow",
            description="A slow tool for timeout testing",
            parameters=(
                MCPToolParameter(
                    name="data",
                    type=ToolInputType.STRING,
                    description="Some data",
                    required=False,
                    default="default",
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle with delay."""
        await asyncio.sleep(self._delay)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="Slow result"),),
            )
        )


# ---------------------------------------------------------------------------
# Mock Resource Handlers
# ---------------------------------------------------------------------------


class StaticResourceHandler:
    """A static resource handler for testing."""

    def __init__(
        self,
        uri: str = "test://static",
        name: str = "Static Resource",
        content: str = "Static content",
    ) -> None:
        """Initialize with static content.

        Args:
            uri: Resource URI.
            name: Resource name.
            content: Resource content.
        """
        self._uri = uri
        self._name = name
        self._content = content

    @property
    def definitions(self) -> Sequence[MCPResourceDefinition]:
        """Return resource definitions."""
        return [
            MCPResourceDefinition(
                uri=self._uri,
                name=self._name,
                description=f"Static resource: {self._name}",
            )
        ]

    async def handle(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Handle resource read."""
        if uri != self._uri:
            return Result.err(MCPServerError(f"Resource not found: {uri}"))
        return Result.ok(
            MCPResourceContent(
                uri=uri,
                text=self._content,
            )
        )


class DynamicResourceHandler:
    """A dynamic resource handler that generates content."""

    def __init__(self, uri_prefix: str = "test://dynamic") -> None:
        """Initialize with URI prefix.

        Args:
            uri_prefix: Prefix for resource URIs.
        """
        self._uri_prefix = uri_prefix
        self._data: dict[str, str] = {}

    def set_data(self, key: str, value: str) -> None:
        """Set data for a resource key.

        Args:
            key: Resource key.
            value: Resource value.
        """
        self._data[key] = value

    @property
    def definitions(self) -> Sequence[MCPResourceDefinition]:
        """Return resource definitions."""
        return [
            MCPResourceDefinition(
                uri=f"{self._uri_prefix}/{key}",
                name=f"Dynamic: {key}",
                description=f"Dynamic resource: {key}",
            )
            for key in self._data
        ]

    async def handle(
        self,
        uri: str,
    ) -> Result[MCPResourceContent, MCPServerError]:
        """Handle resource read."""
        if not uri.startswith(self._uri_prefix):
            return Result.err(MCPServerError(f"Resource not found: {uri}"))

        key = uri[len(self._uri_prefix) + 1 :]
        if key not in self._data:
            return Result.err(MCPServerError(f"Resource not found: {uri}"))

        return Result.ok(
            MCPResourceContent(
                uri=uri,
                text=self._data[key],
            )
        )


# ---------------------------------------------------------------------------
# Mock Prompt Handlers
# ---------------------------------------------------------------------------


class GreetingPromptHandler:
    """A greeting prompt handler for testing."""

    @property
    def definition(self) -> MCPPromptDefinition:
        """Return prompt definition."""
        return MCPPromptDefinition(
            name="greeting",
            description="Generate a greeting",
            arguments=(
                MCPPromptArgument(
                    name="name",
                    description="Name to greet",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, str],
    ) -> Result[str, MCPServerError]:
        """Handle prompt request."""
        name = arguments.get("name", "World")
        return Result.ok(f"Hello, {name}!")


# ---------------------------------------------------------------------------
# Pytest Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_server_state() -> MockMCPServerState:
    """Create a fresh mock server state."""
    return MockMCPServerState()


@pytest.fixture
def configured_mock_server() -> MockMCPServerState:
    """Create a mock server with pre-registered tools and resources."""
    state = MockMCPServerState(name="test-server")

    # Register tools
    state.register_tool(
        name="echo",
        description="Echoes the input message",
        parameters=(
            MCPToolParameter(
                name="message",
                type=ToolInputType.STRING,
                description="The message to echo",
                required=True,
            ),
        ),
        handler=lambda args: f"Echo: {args.get('message', '')}",
    )

    state.register_tool(
        name="add",
        description="Adds two numbers",
        parameters=(
            MCPToolParameter(
                name="a",
                type=ToolInputType.NUMBER,
                description="First number",
                required=True,
            ),
            MCPToolParameter(
                name="b",
                type=ToolInputType.NUMBER,
                description="Second number",
                required=True,
            ),
        ),
        handler=lambda args: str(args.get("a", 0) + args.get("b", 0)),
    )

    # Register resources
    state.register_resource(
        uri="test://config",
        name="Configuration",
        description="System configuration",
        content='{"version": "1.0.0", "debug": false}',
    )

    state.register_resource(
        uri="test://status",
        name="Status",
        description="Current status",
        content="OK",
    )

    # Register prompts
    state.register_prompt(
        name="greeting",
        description="Generate a greeting",
        arguments=(MCPPromptArgument(name="name", description="Name to greet", required=True),),
        template="Hello, {name}! Welcome to the system.",
    )

    return state


@pytest.fixture
def echo_handler() -> EchoToolHandler:
    """Create an echo tool handler."""
    return EchoToolHandler()


@pytest.fixture
def add_handler() -> AddToolHandler:
    """Create an add tool handler."""
    return AddToolHandler()


@pytest.fixture
def failing_handler() -> FailingToolHandler:
    """Create a failing tool handler."""
    return FailingToolHandler()


@pytest.fixture
def slow_handler() -> SlowToolHandler:
    """Create a slow tool handler."""
    return SlowToolHandler(delay=0.5)


@pytest.fixture
def static_resource_handler() -> StaticResourceHandler:
    """Create a static resource handler."""
    return StaticResourceHandler()


@pytest.fixture
def greeting_prompt_handler() -> GreetingPromptHandler:
    """Create a greeting prompt handler."""
    return GreetingPromptHandler()


@pytest.fixture
def stdio_server_config() -> MCPServerConfig:
    """Create a sample stdio server configuration."""
    return MCPServerConfig(
        name="test-server",
        transport=TransportType.STDIO,
        command="python",
        args=("-m", "mock_mcp_server"),
        timeout=30.0,
    )


@pytest.fixture
def second_server_config() -> MCPServerConfig:
    """Create a second server configuration for multi-server tests."""
    return MCPServerConfig(
        name="second-server",
        transport=TransportType.STDIO,
        command="python",
        args=("-m", "mock_mcp_server_2"),
        timeout=30.0,
    )


def create_mock_sdk_client_resources(state: MockMCPServerState) -> Any:
    """Return adapter-owned resources backed by an in-memory SDK v2 client."""
    from ouroboros.mcp.client.sdk_factory import SDKClientResources

    return SDKClientResources(client=MockMCPClient(state))
