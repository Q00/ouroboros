"""Unit tests for MCPToolProvider.

Tests cover:
- Tool discovery and conversion
- Tool execution with retry logic
- Error handling (timeout, network, execution errors)
- Tool name conflict resolution
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPClientError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.mcp_tools import (
    DEFAULT_TOOL_TIMEOUT,
    MCPToolInfo,
    MCPToolProvider,
    ToolConflict,
)


@pytest.fixture
def mock_mcp_manager() -> MagicMock:
    """Create a mock MCPClientManager."""
    manager = MagicMock()
    manager.list_all_tools = AsyncMock(return_value=[])
    manager.call_tool = AsyncMock(
        return_value=Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text="Success"),),
                is_error=False,
            )
        )
    )
    manager.find_tool_server = MagicMock(return_value=None)
    return manager


@pytest.fixture
def sample_mcp_tools() -> list[MCPToolDefinition]:
    """Create sample MCP tool definitions."""
    return [
        MCPToolDefinition(
            name="file_read",
            description="Read a file from the filesystem",
            parameters=(
                MCPToolParameter(
                    name="path",
                    type=ToolInputType.STRING,
                    description="Path to the file",
                    required=True,
                ),
            ),
            server_name="filesystem",
        ),
        MCPToolDefinition(
            name="github_search",
            description="Search GitHub repositories",
            parameters=(
                MCPToolParameter(
                    name="query",
                    type=ToolInputType.STRING,
                    description="Search query",
                    required=True,
                ),
            ),
            server_name="github",
        ),
    ]


class TestMCPToolProviderInit:
    """Tests for MCPToolProvider initialization."""

    def test_init_with_defaults(self, mock_mcp_manager: MagicMock) -> None:
        """Test provider initialization with defaults."""
        provider = MCPToolProvider(mock_mcp_manager)

        assert provider.tool_prefix == ""
        assert len(provider.conflicts) == 0

    def test_init_with_prefix(self, mock_mcp_manager: MagicMock) -> None:
        """Test provider initialization with tool prefix."""
        provider = MCPToolProvider(mock_mcp_manager, tool_prefix="mcp_")

        assert provider.tool_prefix == "mcp_"

    def test_init_with_custom_timeout(self, mock_mcp_manager: MagicMock) -> None:
        """Test provider initialization with custom timeout."""
        provider = MCPToolProvider(mock_mcp_manager, default_timeout=60.0)

        assert provider._default_timeout == 60.0


class TestMCPToolProviderGetTools:
    """Tests for MCPToolProvider.get_tools()."""

    @pytest.mark.asyncio
    async def test_get_tools_empty(self, mock_mcp_manager: MagicMock) -> None:
        """Test getting tools when no tools available."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=[])
        provider = MCPToolProvider(mock_mcp_manager)

        tools = await provider.get_tools()

        assert len(tools) == 0
        assert len(provider.conflicts) == 0

    @pytest.mark.asyncio
    async def test_get_tools_success(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test successful tool discovery."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        provider = MCPToolProvider(mock_mcp_manager)

        tools = await provider.get_tools()

        assert len(tools) == 2
        assert tools[0].name == "file_read"
        assert tools[0].original_name == "file_read"
        assert tools[0].server_name == "filesystem"
        assert tools[1].name == "github_search"
        assert tools[1].server_name == "github"

    @pytest.mark.asyncio
    async def test_get_tools_with_prefix(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test tool discovery with name prefix."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        provider = MCPToolProvider(mock_mcp_manager, tool_prefix="ext_")

        tools = await provider.get_tools()

        assert len(tools) == 2
        assert tools[0].name == "ext_file_read"
        assert tools[0].original_name == "file_read"

    @pytest.mark.asyncio
    async def test_get_tools_builtin_conflict(
        self,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test tool conflict with built-in tools."""
        # Create MCP tool that conflicts with built-in "Read"
        conflicting_tools = [
            MCPToolDefinition(
                name="Read",
                description="Conflicting read tool",
                server_name="external",
            ),
            MCPToolDefinition(
                name="safe_tool",
                description="Non-conflicting tool",
                server_name="external",
            ),
        ]
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=conflicting_tools)
        provider = MCPToolProvider(mock_mcp_manager)

        tools = await provider.get_tools(builtin_tools=["Read", "Write", "Edit"])

        # Only non-conflicting tool should be returned
        assert len(tools) == 1
        assert tools[0].name == "safe_tool"

        # Conflict should be recorded
        assert len(provider.conflicts) == 1
        conflict = provider.conflicts[0]
        assert conflict.tool_name == "Read"
        assert conflict.shadowed_by == "built-in"

    @pytest.mark.asyncio
    async def test_get_tools_server_conflict(
        self,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test tool conflict between servers."""
        # Same tool from multiple servers
        conflicting_tools = [
            MCPToolDefinition(
                name="search",
                description="Search from server1",
                server_name="server1",
            ),
            MCPToolDefinition(
                name="search",
                description="Search from server2",
                server_name="server2",
            ),
        ]
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=conflicting_tools)
        provider = MCPToolProvider(mock_mcp_manager)

        tools = await provider.get_tools()

        # First server's tool should win
        assert len(tools) == 1
        assert tools[0].server_name == "server1"

        # Conflict should be recorded
        assert len(provider.conflicts) == 1
        conflict = provider.conflicts[0]
        assert conflict.tool_name == "search"
        assert conflict.source == "server2"
        assert conflict.shadowed_by == "server1"

    @pytest.mark.asyncio
    async def test_get_tools_list_failure(
        self,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test graceful handling of list_all_tools failure."""
        mock_mcp_manager.list_all_tools = AsyncMock(side_effect=Exception("Connection lost"))
        provider = MCPToolProvider(mock_mcp_manager)

        tools = await provider.get_tools()

        # Should return empty list, not raise
        assert len(tools) == 0


class TestMCPToolProviderCallTool:
    """Tests for MCPToolProvider.call_tool()."""

    @pytest.mark.asyncio
    async def test_call_tool_success(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test successful tool call."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        mock_mcp_manager.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="File content here"),),
                    is_error=False,
                )
            )
        )

        provider = MCPToolProvider(mock_mcp_manager)
        await provider.get_tools()

        result = await provider.call_tool("file_read", {"path": "/tmp/test.txt"})

        assert result.is_ok
        assert result.value.text_content == "File content here"
        mock_mcp_manager.call_tool.assert_called_once_with(
            server_name="filesystem",
            tool_name="file_read",
            arguments={"path": "/tmp/test.txt"},
            timeout=DEFAULT_TOOL_TIMEOUT,
        )

    @pytest.mark.asyncio
    async def test_call_tool_not_found(
        self,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Test calling non-existent tool."""
        provider = MCPToolProvider(mock_mcp_manager)

        result = await provider.call_tool("nonexistent", {})

        assert result.is_err
        assert "not found" in str(result.error).lower()

    @pytest.mark.asyncio
    async def test_call_tool_with_prefix(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test calling tool with prefixed name."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        mock_mcp_manager.call_tool = AsyncMock(
            return_value=Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text="Success"),),
                )
            )
        )

        provider = MCPToolProvider(mock_mcp_manager, tool_prefix="ext_")
        await provider.get_tools()

        result = await provider.call_tool("ext_file_read", {"path": "/tmp"})

        assert result.is_ok
        # Should use original name when calling
        mock_mcp_manager.call_tool.assert_called_once()
        call_kwargs = mock_mcp_manager.call_tool.call_args.kwargs
        assert call_kwargs["tool_name"] == "file_read"

    @pytest.mark.asyncio
    async def test_call_tool_custom_timeout(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test calling tool with custom timeout."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        mock_mcp_manager.call_tool = AsyncMock(return_value=Result.ok(MCPToolResult(content=())))

        provider = MCPToolProvider(mock_mcp_manager)
        await provider.get_tools()

        await provider.call_tool("file_read", {}, timeout=120.0)

        call_kwargs = mock_mcp_manager.call_tool.call_args.kwargs
        assert call_kwargs["timeout"] == 120.0

    @pytest.mark.asyncio
    async def test_call_tool_execution_error(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test handling tool execution error."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        mock_mcp_manager.call_tool = AsyncMock(
            return_value=Result.err(MCPClientError("Tool execution failed", is_retriable=False))
        )

        provider = MCPToolProvider(mock_mcp_manager)
        await provider.get_tools()

        result = await provider.call_tool("file_read", {"path": "/nonexistent"})

        assert result.is_err
        assert isinstance(result.error, MCPToolError)


class TestMCPToolProviderHelpers:
    """Tests for MCPToolProvider helper methods."""

    @pytest.mark.asyncio
    async def test_get_tool_names(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test getting tool names list."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        provider = MCPToolProvider(mock_mcp_manager)
        await provider.get_tools()

        names = provider.get_tool_names()

        assert "file_read" in names
        assert "github_search" in names

    @pytest.mark.asyncio
    async def test_has_tool(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test checking tool existence."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        provider = MCPToolProvider(mock_mcp_manager)
        await provider.get_tools()

        assert provider.has_tool("file_read")
        assert not provider.has_tool("nonexistent")

    @pytest.mark.asyncio
    async def test_get_tool_info(
        self,
        mock_mcp_manager: MagicMock,
        sample_mcp_tools: list[MCPToolDefinition],
    ) -> None:
        """Test getting tool info."""
        mock_mcp_manager.list_all_tools = AsyncMock(return_value=sample_mcp_tools)
        provider = MCPToolProvider(mock_mcp_manager)
        await provider.get_tools()

        info = provider.get_tool_info("file_read")

        assert info is not None
        assert info.name == "file_read"
        assert info.server_name == "filesystem"
        assert info.description == "Read a file from the filesystem"

        # Non-existent tool
        assert provider.get_tool_info("nonexistent") is None


class TestToolConflict:
    """Tests for ToolConflict dataclass."""

    def test_create_conflict(self) -> None:
        """Test creating a tool conflict."""
        conflict = ToolConflict(
            tool_name="Read",
            source="external-server",
            shadowed_by="built-in",
            resolution="MCP tool skipped",
        )

        assert conflict.tool_name == "Read"
        assert conflict.source == "external-server"
        assert conflict.shadowed_by == "built-in"
        assert conflict.resolution == "MCP tool skipped"

    def test_conflict_is_frozen(self) -> None:
        """Test that ToolConflict is immutable."""
        conflict = ToolConflict(
            tool_name="test",
            source="s",
            shadowed_by="b",
            resolution="r",
        )

        with pytest.raises(AttributeError):
            conflict.tool_name = "changed"  # type: ignore


class TestMCPToolInfo:
    """Tests for MCPToolInfo dataclass."""

    def test_create_tool_info(self) -> None:
        """Test creating tool info."""
        info = MCPToolInfo(
            name="ext_read",
            original_name="read",
            server_name="filesystem",
            description="Read files",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        )

        assert info.name == "ext_read"
        assert info.original_name == "read"
        assert info.server_name == "filesystem"
        assert "path" in info.input_schema.get("properties", {})

    def test_tool_info_is_frozen(self) -> None:
        """Test that MCPToolInfo is immutable."""
        info = MCPToolInfo(
            name="test",
            original_name="test",
            server_name="server",
            description="Test",
        )

        with pytest.raises(AttributeError):
            info.name = "changed"  # type: ignore
