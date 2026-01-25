"""Tests for MCP server adapter."""

from typing import Any
from unittest.mock import AsyncMock

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPResourceNotFoundError, MCPServerError
from ouroboros.mcp.server.adapter import MCPServerAdapter
from ouroboros.mcp.server.protocol import ResourceHandler, ToolHandler
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPResourceContent,
    MCPResourceDefinition,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)


class MockToolHandler:
    """Mock tool handler for testing."""

    def __init__(self, name: str = "test_tool") -> None:
        self._name = name
        self.handle_mock = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Success"),),
                )
            )
        )

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name=self._name,
            description="A test tool",
            parameters=(
                MCPToolParameter(
                    name="input",
                    type=ToolInputType.STRING,
                    description="Input value",
                ),
            ),
        )

    async def handle(self, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]:
        return await self.handle_mock(arguments)


class MockResourceHandler:
    """Mock resource handler for testing."""

    def __init__(self, uri: str = "test://resource") -> None:
        self._uri = uri
        self.handle_mock = AsyncMock(
            return_value=Result.ok(
                MCPResourceContent(uri=uri, text="Resource content")
            )
        )

    @property
    def definitions(self) -> list[MCPResourceDefinition]:
        return [
            MCPResourceDefinition(
                uri=self._uri,
                name="Test Resource",
                description="A test resource",
            )
        ]

    async def handle(self, uri: str) -> Result[MCPResourceContent, MCPServerError]:
        return await self.handle_mock(uri)


class TestMCPServerAdapter:
    """Test MCPServerAdapter class."""

    def test_adapter_creation(self) -> None:
        """Adapter is created with correct defaults."""
        adapter = MCPServerAdapter()
        assert adapter.info.name == "ouroboros-mcp"
        assert adapter.info.version == "1.0.0"

    def test_adapter_custom_name(self) -> None:
        """Adapter accepts custom name and version."""
        adapter = MCPServerAdapter(name="custom-server", version="2.0.0")
        assert adapter.info.name == "custom-server"
        assert adapter.info.version == "2.0.0"


class TestMCPServerAdapterTools:
    """Test MCPServerAdapter tool operations."""

    def test_register_tool(self) -> None:
        """register_tool adds a tool handler."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler()

        adapter.register_tool(handler)

        assert adapter.info.capabilities.tools is True

    async def test_list_tools(self) -> None:
        """list_tools returns registered tools."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")

        adapter.register_tool(handler)
        tools = await adapter.list_tools()

        assert len(tools) == 1
        assert tools[0].name == "my_tool"

    async def test_call_tool_success(self) -> None:
        """call_tool invokes handler and returns result."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler("my_tool")
        adapter.register_tool(handler)

        result = await adapter.call_tool("my_tool", {"input": "test"})

        assert result.is_ok
        assert result.value.text_content == "Success"
        handler.handle_mock.assert_called_once_with({"input": "test"})

    async def test_call_tool_not_found(self) -> None:
        """call_tool returns error for unknown tool."""
        adapter = MCPServerAdapter()

        result = await adapter.call_tool("unknown_tool", {})

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)

    async def test_call_tool_handler_error(self) -> None:
        """call_tool handles handler errors."""
        adapter = MCPServerAdapter()
        handler = MockToolHandler()
        handler.handle_mock.side_effect = RuntimeError("Handler failed")
        adapter.register_tool(handler)

        result = await adapter.call_tool("test_tool", {})

        assert result.is_err
        assert "Handler failed" in str(result.error)


class TestMCPServerAdapterResources:
    """Test MCPServerAdapter resource operations."""

    def test_register_resource(self) -> None:
        """register_resource adds a resource handler."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler()

        adapter.register_resource(handler)

        assert adapter.info.capabilities.resources is True

    async def test_list_resources(self) -> None:
        """list_resources returns registered resources."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://my-resource")

        adapter.register_resource(handler)
        resources = await adapter.list_resources()

        assert len(resources) == 1
        assert resources[0].uri == "test://my-resource"

    async def test_read_resource_success(self) -> None:
        """read_resource invokes handler and returns content."""
        adapter = MCPServerAdapter()
        handler = MockResourceHandler("test://resource")
        adapter.register_resource(handler)

        result = await adapter.read_resource("test://resource")

        assert result.is_ok
        assert result.value.text == "Resource content"

    async def test_read_resource_not_found(self) -> None:
        """read_resource returns error for unknown resource."""
        adapter = MCPServerAdapter()

        result = await adapter.read_resource("unknown://resource")

        assert result.is_err
        assert isinstance(result.error, MCPResourceNotFoundError)


class TestMCPServerAdapterInfo:
    """Test MCPServerAdapter info property."""

    def test_info_updates_with_registrations(self) -> None:
        """Server info reflects registered handlers."""
        adapter = MCPServerAdapter()

        # Initially no capabilities
        assert adapter.info.capabilities.tools is False
        assert adapter.info.capabilities.resources is False

        # After registering tool
        adapter.register_tool(MockToolHandler())
        assert adapter.info.capabilities.tools is True

        # After registering resource
        adapter.register_resource(MockResourceHandler())
        assert adapter.info.capabilities.resources is True

    def test_info_includes_tool_definitions(self) -> None:
        """Server info includes tool definitions."""
        adapter = MCPServerAdapter()
        adapter.register_tool(MockToolHandler("tool1"))
        adapter.register_tool(MockToolHandler("tool2"))

        info = adapter.info

        assert len(info.tools) == 2
        tool_names = {t.name for t in info.tools}
        assert "tool1" in tool_names
        assert "tool2" in tool_names
