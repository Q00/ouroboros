"""MCP Tool Provider for OrchestratorRunner.

This module provides the MCPToolProvider class that wraps external MCP tools
as agent-callable tools during workflow execution.

Features:
    - Converts MCPClientManager tools to agent tool format
    - Handles tool execution with configurable timeouts
    - Implements retry policy for transient failures
    - Provides graceful error handling (no crashes on MCP failures)

Usage:
    provider = MCPToolProvider(mcp_manager)
    tools = await provider.get_tools()
    result = await provider.call_tool("tool_name", {"arg": "value"})
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import stamina

from ouroboros.core.types import Result
from ouroboros.mcp.errors import (
    MCPClientError,
    MCPConnectionError,
    MCPToolError,
)
from ouroboros.mcp.types import MCPToolResult
from ouroboros.observability.logging import get_logger

if TYPE_CHECKING:
    from ouroboros.mcp.client.manager import MCPClientManager

log = get_logger(__name__)


# Default timeout for tool execution (30 seconds)
DEFAULT_TOOL_TIMEOUT = 30.0

# Maximum retries for transient failures
MAX_RETRIES = 3

# Retry wait range (exponential backoff)
RETRY_WAIT_MIN = 0.5
RETRY_WAIT_MAX = 5.0


@dataclass(frozen=True, slots=True)
class ToolConflict:
    """Information about a tool name conflict.

    Attributes:
        tool_name: Name of the conflicting tool.
        source: Where the conflict originated (built-in, server name).
        shadowed_by: What is shadowing this tool.
        resolution: How the conflict was resolved.
    """

    tool_name: str
    source: str
    shadowed_by: str
    resolution: str


@dataclass(frozen=True, slots=True)
class MCPToolInfo:
    """Information about an available MCP tool.

    Attributes:
        name: Tool name (possibly prefixed).
        original_name: Original tool name from MCP server.
        server_name: Name of the MCP server providing this tool.
        description: Tool description.
        input_schema: JSON Schema for tool parameters.
    """

    name: str
    original_name: str
    server_name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


class MCPToolProvider:
    """Provider for MCP tools to integrate with OrchestratorRunner.

    This class wraps an MCPClientManager and provides:
    - Tool discovery and conversion to agent format
    - Tool execution with timeout handling
    - Retry policy for transient failures
    - Graceful error handling

    All errors are wrapped and returned as results, not raised as exceptions,
    to ensure MCP failures don't crash the orchestrator.

    Example:
        manager = MCPClientManager()
        await manager.add_server(config)
        await manager.connect_all()

        provider = MCPToolProvider(manager)
        tools = await provider.get_tools()

        result = await provider.call_tool("file_read", {"path": "/tmp/test"})
        if result.is_ok:
            print(result.value.text_content)
    """

    def __init__(
        self,
        mcp_manager: MCPClientManager,
        *,
        default_timeout: float = DEFAULT_TOOL_TIMEOUT,
        tool_prefix: str = "",
    ) -> None:
        """Initialize the MCP tool provider.

        Args:
            mcp_manager: MCPClientManager with connected servers.
            default_timeout: Default timeout for tool execution in seconds.
            tool_prefix: Optional prefix to add to all MCP tool names
                        (e.g., "mcp_" to namespace tools).
        """
        self._manager = mcp_manager
        self._default_timeout = default_timeout
        self._tool_prefix = tool_prefix
        self._tool_map: dict[str, MCPToolInfo] = {}
        self._conflicts: list[ToolConflict] = []

    @property
    def tool_prefix(self) -> str:
        """Return the tool name prefix."""
        return self._tool_prefix

    @property
    def conflicts(self) -> Sequence[ToolConflict]:
        """Return any tool conflicts detected during tool loading."""
        return tuple(self._conflicts)

    async def get_tools(
        self,
        builtin_tools: Sequence[str] | None = None,
    ) -> Sequence[MCPToolInfo]:
        """Get all available MCP tools.

        Discovers tools from all connected MCP servers and converts them
        to the agent tool format. Handles tool name conflicts by:
        - Skipping tools that conflict with built-in tools
        - Using first server's tool when multiple servers provide same name

        Args:
            builtin_tools: List of built-in tool names to avoid conflicts with.

        Returns:
            Sequence of MCPToolInfo for available tools.
        """
        builtin_set = set(builtin_tools or [])
        self._tool_map.clear()
        self._conflicts.clear()

        try:
            mcp_tools = await self._manager.list_all_tools()
        except Exception as e:
            log.error(
                "orchestrator.mcp_tools.list_failed",
                error=str(e),
            )
            return ()

        # Track which tools we've seen (for server conflict detection)
        seen_tools: dict[str, str] = {}  # tool_name -> first_server_name

        for tool in mcp_tools:
            prefixed_name = f"{self._tool_prefix}{tool.name}"

            # Check for built-in tool conflict
            if prefixed_name in builtin_set or tool.name in builtin_set:
                self._conflicts.append(
                    ToolConflict(
                        tool_name=tool.name,
                        source=tool.server_name or "unknown",
                        shadowed_by="built-in",
                        resolution="MCP tool skipped",
                    )
                )
                log.warning(
                    "orchestrator.mcp_tools.shadowed_by_builtin",
                    tool_name=tool.name,
                    server=tool.server_name,
                )
                continue

            # Check for server conflict (same tool from multiple servers)
            if prefixed_name in seen_tools:
                first_server = seen_tools[prefixed_name]
                self._conflicts.append(
                    ToolConflict(
                        tool_name=tool.name,
                        source=tool.server_name or "unknown",
                        shadowed_by=first_server,
                        resolution="Later server's tool skipped",
                    )
                )
                log.warning(
                    "orchestrator.mcp_tools.shadowed_by_server",
                    tool_name=tool.name,
                    server=tool.server_name,
                    shadowed_by=first_server,
                )
                continue

            # Register the tool
            seen_tools[prefixed_name] = tool.server_name or "unknown"
            tool_info = MCPToolInfo(
                name=prefixed_name,
                original_name=tool.name,
                server_name=tool.server_name or "unknown",
                description=tool.description,
                input_schema=tool.to_input_schema(),
            )
            self._tool_map[prefixed_name] = tool_info

        log.info(
            "orchestrator.mcp_tools.loaded",
            tool_count=len(self._tool_map),
            conflict_count=len(self._conflicts),
            servers=list({t.server_name for t in self._tool_map.values()}),
        )

        return tuple(self._tool_map.values())

    def get_tool_names(self) -> Sequence[str]:
        """Get list of available tool names.

        Returns:
            Sequence of tool names (with prefix if configured).
        """
        return tuple(self._tool_map.keys())

    def has_tool(self, name: str) -> bool:
        """Check if a tool is available.

        Args:
            name: Tool name to check (with prefix if applicable).

        Returns:
            True if tool is available.
        """
        return name in self._tool_map

    def get_tool_info(self, name: str) -> MCPToolInfo | None:
        """Get info for a specific tool.

        Args:
            name: Tool name (with prefix if applicable).

        Returns:
            MCPToolInfo or None if not found.
        """
        return self._tool_map.get(name)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> Result[MCPToolResult, MCPToolError]:
        """Call an MCP tool with the given arguments.

        Handles:
        - Timeout with configurable duration
        - Retry for transient failures (network errors, connection issues)
        - Graceful error handling (returns error result, doesn't raise)

        Args:
            name: Tool name (with prefix if applicable).
            arguments: Tool arguments as a dict.
            timeout: Optional timeout override in seconds.

        Returns:
            Result containing MCPToolResult on success or MCPToolError on failure.
        """
        tool_info = self._tool_map.get(name)
        if not tool_info:
            return Result.err(
                MCPToolError(
                    f"Tool not found: {name}",
                    tool_name=name,
                    is_retriable=False,
                )
            )

        effective_timeout = timeout or self._default_timeout

        log.debug(
            "orchestrator.mcp_tools.call_start",
            tool_name=name,
            server=tool_info.server_name,
            timeout=effective_timeout,
        )

        try:
            # Use stamina for retries on transient failures
            result = await self._call_with_retry(
                tool_info=tool_info,
                arguments=arguments or {},
                timeout=effective_timeout,
            )
            return result
        except Exception as e:
            # Catch any unexpected errors and wrap them
            log.exception(
                "orchestrator.mcp_tools.unexpected_error",
                tool_name=name,
                error=str(e),
            )
            return Result.err(
                MCPToolError(
                    f"Unexpected error calling tool {name}: {e}",
                    tool_name=name,
                    server_name=tool_info.server_name,
                    is_retriable=False,
                    details={"exception_type": type(e).__name__},
                )
            )

    async def _call_with_retry(
        self,
        tool_info: MCPToolInfo,
        arguments: dict[str, Any],
        timeout: float,
    ) -> Result[MCPToolResult, MCPToolError]:
        """Call tool with retry logic for transient failures.

        Uses stamina for exponential backoff retries on:
        - Connection errors
        - Timeout errors (if marked retriable)
        - Other transient MCPClientErrors

        Args:
            tool_info: Information about the tool to call.
            arguments: Tool arguments.
            timeout: Timeout in seconds.

        Returns:
            Result containing MCPToolResult or MCPToolError.
        """

        @stamina.retry(
            on=(MCPConnectionError, asyncio.TimeoutError),
            attempts=MAX_RETRIES,
            wait_initial=RETRY_WAIT_MIN,
            wait_max=RETRY_WAIT_MAX,
            wait_jitter=0.5,
        )
        async def _do_call() -> Result[MCPToolResult, MCPClientError]:
            # Use call_tool with server name for explicit routing
            return await self._manager.call_tool(
                server_name=tool_info.server_name,
                tool_name=tool_info.original_name,
                arguments=arguments,
                timeout=timeout,
            )

        try:
            result = await _do_call()
        except TimeoutError:
            log.warning(
                "orchestrator.mcp_tools.timeout_after_retries",
                tool_name=tool_info.name,
                timeout=timeout,
            )
            return Result.err(
                MCPToolError(
                    f"Tool call timed out after {MAX_RETRIES} retries: {tool_info.name}",
                    tool_name=tool_info.name,
                    server_name=tool_info.server_name,
                    is_retriable=False,
                    details={"timeout_seconds": timeout, "retries": MAX_RETRIES},
                )
            )
        except MCPConnectionError as e:
            log.warning(
                "orchestrator.mcp_tools.connection_failed_after_retries",
                tool_name=tool_info.name,
                error=str(e),
            )
            return Result.err(
                MCPToolError(
                    f"Connection failed after {MAX_RETRIES} retries: {e}",
                    tool_name=tool_info.name,
                    server_name=tool_info.server_name,
                    is_retriable=False,
                    details={"retries": MAX_RETRIES},
                )
            )

        # Convert MCPClientError to MCPToolError for consistency
        if result.is_err:
            error = result.error
            log.warning(
                "orchestrator.mcp_tools.call_failed",
                tool_name=tool_info.name,
                error=str(error),
            )
            return Result.err(
                MCPToolError(
                    f"Tool execution failed: {error}",
                    tool_name=tool_info.name,
                    server_name=tool_info.server_name,
                    is_retriable=error.is_retriable if isinstance(error, MCPClientError) else False,
                    details={"original_error": str(error)},
                )
            )

        log.debug(
            "orchestrator.mcp_tools.call_success",
            tool_name=tool_info.name,
            is_error=result.value.is_error,
        )

        return Result.ok(result.value)


@dataclass(frozen=True, slots=True)
class MCPToolsLoadedEvent:
    """Event data when MCP tools are loaded.

    Attributes:
        tool_count: Number of tools loaded.
        server_names: Names of servers providing tools.
        conflict_count: Number of tool conflicts detected.
        conflicts: Details of any conflicts.
    """

    tool_count: int
    server_names: tuple[str, ...]
    conflict_count: int
    conflicts: tuple[ToolConflict, ...] = field(default_factory=tuple)


def create_mcp_tools_loaded_event(
    session_id: str,
    provider: MCPToolProvider,
) -> dict[str, Any]:
    """Create event data for MCP tools loaded.

    Args:
        session_id: Current session ID.
        provider: MCPToolProvider with loaded tools.

    Returns:
        Event data dict for inclusion in BaseEvent.
    """
    tools = list(provider._tool_map.values())
    server_names = tuple({t.server_name for t in tools})

    return {
        "session_id": session_id,
        "tool_count": len(tools),
        "server_names": server_names,
        "conflict_count": len(provider.conflicts),
        "tool_names": [t.name for t in tools],
    }


__all__ = [
    "DEFAULT_TOOL_TIMEOUT",
    "MAX_RETRIES",
    "MCPToolError",
    "MCPToolInfo",
    "MCPToolProvider",
    "MCPToolsLoadedEvent",
    "ToolConflict",
    "create_mcp_tools_loaded_event",
]
