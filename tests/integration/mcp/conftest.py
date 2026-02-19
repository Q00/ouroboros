"""Shared fixtures for MCP integration tests.

This module provides mock MCP server infrastructure for testing MCP client
and server adapters without requiring real external MCP servers.
"""

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

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


class MockMCPSession:
    """Mock MCP session that simulates the MCP SDK ClientSession.

    This class provides the same interface as the real MCP SDK ClientSession
    but operates entirely in memory without network communication.
    """

    def __init__(self, server_state: MockMCPServerState) -> None:
        """Initialize with server state.

        Args:
            server_state: The mock server state to use.
        """
        self._state = server_state
        self._initialized = False

    async def __aenter__(self) -> MockMCPSession:
        """Enter async context manager."""
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Exit async context manager."""
        pass

    async def initialize(self) -> MagicMock:
        """Initialize the session and return server info.

        Returns:
            Mock initialization result with server capabilities.
        """
        self._initialized = True
        self._state.initialized = True

        # Create mock init result
        result = MagicMock()
        result.protocolVersion = "1.0.0"
        result.capabilities = MagicMock()
        # MCP SDK returns None for missing capabilities, not False
        # The adapter checks "is not None" to determine capability presence
        result.capabilities.tools = MagicMock() if len(self._state.tools) > 0 else None
        result.capabilities.resources = MagicMock() if len(self._state.resources) > 0 else None
        result.capabilities.prompts = MagicMock() if len(self._state.prompts) > 0 else None
        result.capabilities.logging = MagicMock()

        return result

    async def list_tools(self) -> MagicMock:
        """List available tools.

        Returns:
            Mock result with tool list.
        """
        result = MagicMock()
        result.tools = []

        for name, (definition, _handler) in self._state.tools.items():
            mock_tool = MagicMock()
            mock_tool.name = name
            mock_tool.description = definition.description
            mock_tool.inputSchema = definition.to_input_schema()
            result.tools.append(mock_tool)

        return result

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> MagicMock:
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
            if asyncio.iscoroutinefunction(handler):
                text = await handler(arguments)
            else:
                text = handler(arguments)
        else:
            text = f"Tool {name} executed with {arguments}"

        # Create mock result
        result = MagicMock()
        mock_content = MagicMock()
        mock_content.text = text
        result.content = [mock_content]
        result.isError = False

        return result

    async def list_resources(self) -> MagicMock:
        """List available resources.

        Returns:
            Mock result with resource list.
        """
        result = MagicMock()
        result.resources = []

        for uri, (definition, _content) in self._state.resources.items():
            mock_resource = MagicMock()
            mock_resource.uri = uri
            mock_resource.name = definition.name
            mock_resource.description = definition.description
            mock_resource.mimeType = definition.mime_type
            result.resources.append(mock_resource)

        return result

    async def read_resource(self, uri: str) -> MagicMock:
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

        result = MagicMock()
        mock_content = MagicMock()
        mock_content.text = content
        mock_content.mimeType = "text/plain"
        result.contents = [mock_content]

        return result

    async def list_prompts(self) -> MagicMock:
        """List available prompts.

        Returns:
            Mock result with prompt list.
        """
        result = MagicMock()
        result.prompts = []

        for name, (definition, _template) in self._state.prompts.items():
            mock_prompt = MagicMock()
            mock_prompt.name = name
            mock_prompt.description = definition.description
            mock_prompt.arguments = [
                MagicMock(name=arg.name, description=arg.description, required=arg.required)
                for arg in definition.arguments
            ]
            result.prompts.append(mock_prompt)

        return result

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str],
    ) -> MagicMock:
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

        result = MagicMock()
        mock_message = MagicMock()
        mock_message.content = MagicMock()
        mock_message.content.text = filled
        result.messages = [mock_message]

        return result


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


def create_mock_session_factory(state: MockMCPServerState):
    """Create a factory function that returns mock sessions.

    Args:
        state: The server state to use.

    Returns:
        A context manager that yields (read_stream, write_stream).
    """
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_stdio_client(_params: Any) -> AsyncIterator[tuple[Any, Any]]:
        """Mock stdio client that yields mock streams."""
        # Create mock streams
        read_stream = MagicMock()
        write_stream = MagicMock()
        yield (read_stream, write_stream)

    return mock_stdio_client


def create_mock_client_session_class(state: MockMCPServerState) -> type:
    """Create a mock ClientSession class bound to a server state.

    Args:
        state: The server state to use.

    Returns:
        A mock ClientSession class.
    """

    class BoundMockSession(MockMCPSession):
        """A MockMCPSession bound to a specific state."""

        def __init__(self, read_stream: Any, write_stream: Any) -> None:
            """Initialize ignoring streams (they're mock objects)."""
            super().__init__(state)

    return BoundMockSession
