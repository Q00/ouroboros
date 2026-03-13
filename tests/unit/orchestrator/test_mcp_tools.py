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
    SessionToolCatalog,
    SessionToolCatalogEntry,
    ToolCatalogSourceMetadata,
    ToolConflict,
    assemble_session_tool_catalog,
    enumerate_runtime_builtin_tool_definitions,
    normalize_opencode_session_tool_catalog,
    normalize_opencode_tool_result,
    normalize_runtime_tool_definition,
    normalize_serialized_tool_catalog,
    serialize_tool_catalog,
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


class TestOpenCodeToolResultNormalization:
    """Tests for OpenCode-native tool result normalization."""

    def test_normalize_completed_result_captures_exit_status(self) -> None:
        """Successful OpenCode tool results should preserve normalized metadata."""
        result = normalize_opencode_tool_result(
            {
                "type": "tool.completed",
                "tool": {
                    "name": "command_execution",
                    "description": "Execute a shell command",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "Command to run",
                            }
                        },
                        "required": ["command"],
                    },
                },
                "arguments": {"command": "pytest -q"},
                "result": {
                    "summary": "pytest -q passed",
                    "changed_files": ["src/ouroboros/orchestrator/mcp_tools.py"],
                    "status": "success",
                },
                "output": {
                    "status": "ok",
                    "artifacts": {
                        "updated": ["src/ouroboros/orchestrator/mcp_tools.py"],
                    },
                },
                "exit_code": 0,
                "server": {"name": "workspace"},
                "toolCallId": "call-123",
                "durationMs": 240,
            }
        )

        assert result.is_error is False
        assert "pytest -q passed" in result.text_content
        assert result.meta["runtime_backend"] == "opencode"
        assert result.meta["runtime_event_type"] == "tool.completed"
        assert result.meta["tool_name"] == "Bash"
        assert result.meta["raw_tool_name"] == "command_execution"
        assert result.meta["tool_definition"]["name"] == "Bash"
        assert result.meta["tool_definition"]["server_name"] == "workspace"
        assert result.meta["tool_definition"]["input_schema"] == {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Command to run",
                }
            },
            "required": ["command"],
        }
        assert result.meta["tool_call_id"] == "call-123"
        assert result.meta["duration_ms"] == 240
        assert result.meta["status"] == "completed"
        assert result.meta["exit_status"] == 0
        assert result.meta["success"] is True
        assert result.meta["server_name"] == "workspace"
        assert result.meta["result_payload"] == {
            "summary": "pytest -q passed",
            "changed_files": ["src/ouroboros/orchestrator/mcp_tools.py"],
            "status": "success",
        }
        assert result.meta["output_payload"] == {
            "status": "ok",
            "artifacts": {
                "updated": ["src/ouroboros/orchestrator/mcp_tools.py"],
            },
        }

    def test_normalize_failed_result_marks_error_and_preserves_error_fields(self) -> None:
        """Failed OpenCode tool results should populate normalized error metadata."""
        result = normalize_opencode_tool_result(
            {
                "type": "tool.failed",
                "tool": {
                    "name": "github_search",
                    "description": "Search GitHub repositories",
                    "server": {"name": "github"},
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query",
                            }
                        },
                        "required": ["query"],
                    },
                },
                "arguments": {"query": "opencode adapter"},
                "stderr": "pytest -q failed",
                "output": {
                    "status": "failed",
                    "message": "GitHub API rejected the request",
                },
                "error": {
                    "message": "Command exited with code 2",
                    "type": "CommandFailed",
                    "code": "EEXIT",
                    "details": {
                        "retry_after_seconds": 30,
                        "scope": "search",
                    },
                },
                "exit_status": 2,
                "callId": "call-456",
            }
        )

        assert result.is_error is True
        assert "pytest -q failed" in result.text_content
        assert result.meta["tool_name"] == "github_search"
        assert result.meta["tool_definition"]["name"] == "github_search"
        assert result.meta["tool_definition"]["server_name"] == "github"
        assert result.meta["status"] == "failed"
        assert result.meta["tool_call_id"] == "call-456"
        assert result.meta["output_payload"] == {
            "status": "failed",
            "message": "GitHub API rejected the request",
        }
        assert result.meta["error_payload"] == {
            "message": "Command exited with code 2",
            "type": "CommandFailed",
            "code": "EEXIT",
            "details": {
                "retry_after_seconds": 30,
                "scope": "search",
            },
        }
        assert result.meta["exit_status"] == 2
        assert result.meta["error_message"] == "Command exited with code 2"
        assert result.meta["error_type"] == "CommandFailed"
        assert result.meta["error_code"] == "EEXIT"


class TestMCPToolProviderGetTools:
    """Tests for MCPToolProvider.get_tools()."""

    def test_assemble_session_tool_catalog_merges_tools_deterministically(self) -> None:
        """Built-in and attached tools should produce a stable merged catalog."""
        catalog = assemble_session_tool_catalog(
            builtin_tools=["Write", "Read"],
            attached_tools=[
                MCPToolDefinition(
                    name="zeta",
                    description="Zeta tool",
                    server_name="server-z",
                ),
                MCPToolDefinition(
                    name="Read",
                    description="Conflicting read tool",
                    server_name="server-shadow",
                ),
                MCPToolDefinition(
                    name="search",
                    description="Search from server-b",
                    server_name="server-b",
                ),
                MCPToolDefinition(
                    name="alpha",
                    description="Alpha tool",
                    server_name="server-a",
                ),
                MCPToolDefinition(
                    name="search",
                    description="Search from server-a",
                    server_name="server-a",
                ),
            ],
        )

        assert isinstance(catalog, SessionToolCatalog)
        assert [tool.name for tool in catalog.tools] == [
            "Write",
            "Read",
            "alpha",
            "search",
            "zeta",
        ]
        assert [tool.server_name for tool in catalog.attached_tools] == [
            "server-a",
            "server-a",
            "server-z",
        ]
        assert len(catalog.conflicts) == 2
        assert catalog.conflicts[0] == ToolConflict(
            tool_name="Read",
            source="server-shadow",
            shadowed_by="built-in",
            resolution="MCP tool skipped",
        )
        assert catalog.conflicts[1] == ToolConflict(
            tool_name="search",
            source="server-b",
            shadowed_by="server-a",
            resolution="Later server's tool skipped",
        )

    def test_assemble_session_tool_catalog_tracks_stable_ids_and_source_metadata(self) -> None:
        """Merged catalog entries should keep stable identifiers and provenance."""
        catalog = assemble_session_tool_catalog(
            builtin_tools=["write"],
            attached_tools=[
                MCPToolDefinition(
                    name="search_repo",
                    description="Search repositories",
                    server_name="github",
                ),
            ],
            tool_prefix="ext_",
        )

        assert catalog.entries == (
            SessionToolCatalogEntry(
                stable_id="builtin:Write",
                tool=catalog.tools[0],
                source=ToolCatalogSourceMetadata(
                    kind="builtin",
                    name="built-in",
                    original_name="write",
                    server_name=None,
                ),
            ),
            SessionToolCatalogEntry(
                stable_id="mcp:github:ext_search_repo",
                tool=catalog.tools[1],
                source=ToolCatalogSourceMetadata(
                    kind="attached_mcp",
                    name="github",
                    original_name="search_repo",
                    server_name="github",
                ),
            ),
        )
        assert catalog.attached_entries == (catalog.entries[1],)

    def test_serialize_tool_catalog_includes_stable_ids_and_source_metadata(self) -> None:
        """Serialized catalogs should retain deterministic IDs and source metadata."""
        catalog = assemble_session_tool_catalog(
            builtin_tools=["write"],
            attached_tools=[
                MCPToolDefinition(
                    name="search_repo",
                    description="Search repositories",
                    server_name="github",
                ),
            ],
            tool_prefix="ext_",
        )

        serialized = serialize_tool_catalog(catalog)

        assert [tool["id"] for tool in serialized] == [
            "builtin:Write",
            "mcp:github:ext_search_repo",
        ]
        assert serialized[0]["source_kind"] == "builtin"
        assert serialized[0]["source_name"] == "built-in"
        assert serialized[0]["original_name"] == "write"
        assert serialized[1]["source"] == {
            "kind": "attached_mcp",
            "name": "github",
            "original_name": "search_repo",
            "server_name": "github",
        }

    def test_normalize_serialized_tool_catalog_preserves_original_names_and_ids(self) -> None:
        """Serialized tool catalogs should round-trip back to the same merged metadata."""
        original_catalog = assemble_session_tool_catalog(
            builtin_tools=["write"],
            attached_tools=[
                MCPToolDefinition(
                    name="search_repo",
                    description="Search repositories",
                    server_name="github",
                ),
            ],
            tool_prefix="ext_",
        )

        rehydrated_catalog = normalize_serialized_tool_catalog(
            serialize_tool_catalog(original_catalog),
            tool_prefix="ext_",
        )

        assert isinstance(rehydrated_catalog, SessionToolCatalog)
        assert [entry.stable_id for entry in rehydrated_catalog.entries] == [
            "builtin:Write",
            "mcp:github:ext_search_repo",
        ]
        assert [entry.source.original_name for entry in rehydrated_catalog.entries] == [
            "write",
            "search_repo",
        ]

    def test_assemble_session_tool_catalog_canonicalizes_builtin_tool_definitions(self) -> None:
        """Built-in MCP tool definitions should normalize to shared runtime metadata."""
        catalog = assemble_session_tool_catalog(
            builtin_tools=[
                MCPToolDefinition(
                    name="web_search",
                    description="Search reference docs",
                    parameters=(
                        MCPToolParameter(
                            name="query",
                            type=ToolInputType.STRING,
                            description="Search query",
                            required=True,
                        ),
                    ),
                )
            ]
        )

        assert [tool.name for tool in catalog.tools] == ["WebSearch"]
        assert catalog.tools[0].description == "Search the web for supporting information."
        assert catalog.tools[0].parameters == (
            MCPToolParameter(
                name="query",
                type=ToolInputType.STRING,
                description="Search query",
                required=True,
            ),
        )

    def test_normalize_opencode_session_tool_catalog_merges_builtin_and_attached_tools(
        self,
    ) -> None:
        """OpenCode session payloads should collapse into the shared deterministic catalog."""
        catalog = normalize_opencode_session_tool_catalog(
            {
                "session": {
                    "builtin_tools": [
                        {
                            "name": "write",
                            "description": "OpenCode write tool",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "file_path": {"type": "string"},
                                },
                                "required": ["file_path"],
                            },
                        },
                        "Read",
                    ],
                    "attached_tools": [
                        {
                            "name": "alpha_lookup",
                            "description": "Lookup alpha records",
                            "server": {"name": "alpha"},
                        }
                    ],
                    "mcp_servers": [
                        {
                            "name": "github",
                            "tools": [
                                {
                                    "name": "github_search",
                                    "description": "Search GitHub repositories",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {
                                            "query": {"type": "string"},
                                        },
                                        "required": ["query"],
                                    },
                                }
                            ],
                        },
                        {
                            "name": "filesystem",
                            "tools": [
                                {
                                    "name": "Read",
                                    "description": "Conflicting external read tool",
                                }
                            ],
                        },
                    ],
                }
            }
        )

        assert isinstance(catalog, SessionToolCatalog)
        assert [tool.name for tool in catalog.tools] == [
            "Write",
            "Read",
            "alpha_lookup",
            "github_search",
        ]
        assert [tool.server_name for tool in catalog.attached_tools] == [
            "alpha",
            "github",
        ]
        assert catalog.tools[0].to_input_schema() == {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "",
                }
            },
            "required": ["file_path"],
        }
        assert catalog.tools[1].to_input_schema() == {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "",
                }
            },
            "required": ["file_path"],
        }
        assert catalog.conflicts == (
            ToolConflict(
                tool_name="Read",
                source="filesystem",
                shadowed_by="built-in",
                resolution="MCP tool skipped",
            ),
        )

    def test_normalize_opencode_session_tool_catalog_discovers_keyed_mcp_server_tools(
        self,
    ) -> None:
        """Session-start server maps should normalize attached MCP tools into shared metadata."""
        catalog = normalize_opencode_session_tool_catalog(
            {
                "session": {
                    "builtin_tools": ["Read"],
                    "mcp": {
                        "servers": {
                            "github": {
                                "toolDefinitions": {
                                    "github_search": {
                                        "description": "Search GitHub repositories",
                                        "inputSchema": {
                                            "type": "object",
                                            "properties": {
                                                "query": {
                                                    "type": "string",
                                                    "description": "Search query",
                                                },
                                                "limit": {
                                                    "type": "integer",
                                                    "default": 10,
                                                },
                                            },
                                            "required": ["query"],
                                        },
                                    }
                                }
                            },
                            "filesystem": {
                                "tools": {
                                    "Read": {
                                        "description": "Conflicting external read tool",
                                    }
                                }
                            },
                        }
                    },
                }
            }
        )

        assert isinstance(catalog, SessionToolCatalog)
        assert [tool.name for tool in catalog.tools] == [
            "Read",
            "github_search",
        ]
        assert [tool.server_name for tool in catalog.attached_tools] == ["github"]
        assert catalog.attached_tools[0].parameters == (
            MCPToolParameter(
                name="query",
                type=ToolInputType.STRING,
                description="Search query",
                required=True,
            ),
            MCPToolParameter(
                name="limit",
                type=ToolInputType.INTEGER,
                required=False,
                default=10,
            ),
        )
        assert catalog.conflicts == (
            ToolConflict(
                tool_name="Read",
                source="filesystem",
                shadowed_by="built-in",
                resolution="MCP tool skipped",
            ),
        )

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
    async def test_get_tools_exposes_session_catalog_and_preserves_original_names(
        self,
        mock_mcp_manager: MagicMock,
    ) -> None:
        """Normalized session names should not overwrite raw MCP dispatch names."""
        mock_mcp_manager.list_all_tools = AsyncMock(
            return_value=[
                MCPToolDefinition(
                    name="search_repo",
                    description="Search repositories",
                    server_name="github",
                ),
            ]
        )
        provider = MCPToolProvider(mock_mcp_manager, tool_prefix="ext_")

        tools = await provider.get_tools(builtin_tools=["Read"])

        assert [tool.name for tool in provider.session_catalog.tools] == ["Read", "ext_search_repo"]
        assert len(tools) == 1
        assert tools[0].name == "ext_search_repo"
        assert tools[0].original_name == "search_repo"

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


class TestNormalizeRuntimeToolDefinition:
    """Tests for runtime tool normalization helpers."""

    def test_normalizes_opencode_builtin_metadata_schema(self) -> None:
        """OpenCode built-ins should map into the shared MCP tool model."""
        definition = normalize_runtime_tool_definition(
            "web_search",
            description=None,
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                    "scope": {
                        "type": ["string", "null"],
                        "default": "docs",
                        "enum": ["docs", "web"],
                    },
                },
                "required": ["query"],
            },
        )

        assert definition.name == "WebSearch"
        assert definition.description == "Search the web for supporting information."
        assert definition.parameters == (
            MCPToolParameter(
                name="query",
                type=ToolInputType.STRING,
                description="Search query",
                required=True,
            ),
            MCPToolParameter(
                name="scope",
                type=ToolInputType.STRING,
                required=False,
                default="docs",
                enum=("docs", "web"),
            ),
        )

    def test_enumerates_runtime_builtin_definitions_with_primary_input_schema(self) -> None:
        """Shared runtime built-ins should enumerate as canonical MCP tool definitions."""
        definitions = enumerate_runtime_builtin_tool_definitions()

        assert [definition.name for definition in definitions] == [
            "Read",
            "Write",
            "Edit",
            "Bash",
            "Glob",
            "Grep",
            "WebFetch",
            "WebSearch",
            "NotebookEdit",
        ]
        assert definitions[0].to_input_schema() == {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "",
                }
            },
            "required": ["file_path"],
        }
        assert definitions[6].to_input_schema() == {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "",
                }
            },
            "required": ["url"],
        }

    def test_preserves_external_tool_names_when_server_scoped(self) -> None:
        """Server-provided tool names should not be rewritten as built-ins."""
        definition = normalize_runtime_tool_definition(
            "read",
            server_name="external-mcp",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        )

        assert definition.name == "read"
        assert definition.server_name == "external-mcp"

    @pytest.mark.parametrize(
        ("raw_name", "canonical_name", "canonical_description", "schema_property"),
        [
            ("command_execution", "Bash", "Run a shell command in the workspace.", "command"),
            ("file_change", "Edit", "Edit an existing file in the workspace.", "file_path"),
            ("file_read", "Read", "Read a file from the workspace.", "file_path"),
            ("file_write", "Write", "Write a file in the workspace.", "file_path"),
        ],
    )
    def test_normalizes_opencode_native_builtin_aliases(
        self,
        raw_name: str,
        canonical_name: str,
        canonical_description: str,
        schema_property: str,
    ) -> None:
        """OpenCode-native builtin aliases should map to the shared canonical tool names."""
        definition = normalize_runtime_tool_definition(
            raw_name,
            input_schema={
                "type": "object",
                "properties": {
                    schema_property: {
                        "type": "string",
                        "description": "Primary input",
                    }
                },
                "required": [schema_property],
            },
        )

        assert definition.name == canonical_name
        assert definition.description == canonical_description
        assert definition.parameters == (
            MCPToolParameter(
                name=schema_property,
                type=ToolInputType.STRING,
                description="Primary input",
                required=True,
            ),
        )

    @pytest.mark.parametrize(
        ("raw_name", "canonical_name", "schema_property"),
        [
            ("Read", "Read", "file_path"),
            ("write", "Write", "file_path"),
            ("command_execution", "Bash", "command"),
            ("web_search", "WebSearch", "query"),
        ],
    )
    def test_populates_default_primary_parameter_for_bare_builtins(
        self,
        raw_name: str,
        canonical_name: str,
        schema_property: str,
    ) -> None:
        """Bare built-in names should still project into the shared tool shape."""
        definition = normalize_runtime_tool_definition(raw_name)

        assert definition.name == canonical_name
        assert definition.parameters == (
            MCPToolParameter(
                name=schema_property,
                type=ToolInputType.STRING,
                required=True,
            ),
        )

    def test_reads_attached_mcp_metadata_from_nested_tool_definition(self) -> None:
        """Attached MCP metadata should normalize into the same shared tool shape."""
        definition = normalize_runtime_tool_definition(
            "github_search",
            {"query": "ouroboros"},
            tool_metadata={
                "tool_definition": {
                    "description": "Search GitHub repositories",
                    "server": {"name": "github"},
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query",
                            },
                            "limit": {
                                "type": "integer",
                                "default": 10,
                            },
                        },
                        "required": ["query"],
                    },
                }
            },
        )

        assert definition.name == "github_search"
        assert definition.description == "Search GitHub repositories"
        assert definition.server_name == "github"
        assert definition.parameters == (
            MCPToolParameter(
                name="query",
                type=ToolInputType.STRING,
                description="Search query",
                required=True,
            ),
            MCPToolParameter(
                name="limit",
                type=ToolInputType.INTEGER,
                required=False,
                default=10,
            ),
        )

    def test_falls_back_to_runtime_argument_inference_without_schema(self) -> None:
        """Observed tool arguments still infer parameter types when no schema exists."""
        definition = normalize_runtime_tool_definition(
            "Bash",
            {"command": "pytest", "timeout": 30, "dry_run": False},
        )

        assert definition.parameters == (
            MCPToolParameter(name="command", type=ToolInputType.STRING, required=True),
            MCPToolParameter(name="timeout", type=ToolInputType.INTEGER, required=True),
            MCPToolParameter(name="dry_run", type=ToolInputType.BOOLEAN, required=True),
        )


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
