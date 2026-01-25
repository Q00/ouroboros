"""Tool registry for managing MCP tool handlers.

This module provides a registry for managing tool handlers, supporting
dynamic registration, discovery, and invocation of tools.
"""

from collections.abc import Sequence
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPResourceNotFoundError, MCPServerError, MCPToolError
from ouroboros.mcp.server.protocol import ToolHandler
from ouroboros.mcp.types import MCPToolDefinition, MCPToolResult

log = structlog.get_logger(__name__)


class ToolRegistry:
    """Registry for managing MCP tool handlers.

    Provides a centralized place to register, discover, and invoke tools.
    Supports grouping tools by category and provides metadata for discovery.

    Example:
        registry = ToolRegistry()

        # Register individual handlers
        registry.register(ExecuteSeedHandler())
        registry.register(SessionStatusHandler())

        # Or register multiple at once
        registry.register_all([
            ExecuteSeedHandler(),
            SessionStatusHandler(),
        ])

        # List tools
        tools = registry.list_tools()

        # Call a tool
        result = await registry.call("execute_seed", {"seed_id": "123"})
    """

    def __init__(self) -> None:
        """Initialize the registry."""
        self._handlers: dict[str, ToolHandler] = {}
        self._categories: dict[str, set[str]] = {}

    @property
    def tool_count(self) -> int:
        """Return the number of registered tools."""
        return len(self._handlers)

    def register(
        self,
        handler: ToolHandler,
        *,
        category: str = "default",
    ) -> None:
        """Register a tool handler.

        Args:
            handler: The tool handler to register.
            category: Optional category for grouping tools.

        Raises:
            ValueError: If a tool with the same name is already registered.
        """
        name = handler.definition.name

        if name in self._handlers:
            msg = f"Tool already registered: {name}"
            raise ValueError(msg)

        self._handlers[name] = handler

        # Track category
        if category not in self._categories:
            self._categories[category] = set()
        self._categories[category].add(name)

        log.info("mcp.registry.tool_registered", tool=name, category=category)

    def register_all(
        self,
        handlers: Sequence[ToolHandler],
        *,
        category: str = "default",
    ) -> None:
        """Register multiple tool handlers.

        Args:
            handlers: The tool handlers to register.
            category: Optional category for grouping tools.
        """
        for handler in handlers:
            self.register(handler, category=category)

    def unregister(self, name: str) -> bool:
        """Unregister a tool handler.

        Args:
            name: Name of the tool to unregister.

        Returns:
            True if the tool was unregistered, False if not found.
        """
        if name not in self._handlers:
            return False

        del self._handlers[name]

        # Remove from categories
        for category_tools in self._categories.values():
            category_tools.discard(name)

        log.info("mcp.registry.tool_unregistered", tool=name)
        return True

    def get(self, name: str) -> ToolHandler | None:
        """Get a tool handler by name.

        Args:
            name: Name of the tool.

        Returns:
            The tool handler or None if not found.
        """
        return self._handlers.get(name)

    def list_tools(self, category: str | None = None) -> Sequence[MCPToolDefinition]:
        """List all registered tools.

        Args:
            category: Optional category to filter by.

        Returns:
            Sequence of tool definitions.
        """
        if category is not None:
            tool_names = self._categories.get(category, set())
            return tuple(
                self._handlers[name].definition
                for name in tool_names
                if name in self._handlers
            )

        return tuple(h.definition for h in self._handlers.values())

    def list_categories(self) -> Sequence[str]:
        """List all tool categories.

        Returns:
            Sequence of category names.
        """
        return tuple(self._categories.keys())

    def tools_in_category(self, category: str) -> Sequence[str]:
        """List tool names in a category.

        Args:
            category: Category name.

        Returns:
            Sequence of tool names in the category.
        """
        return tuple(self._categories.get(category, set()))

    async def call(
        self,
        name: str,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Call a registered tool.

        Args:
            name: Name of the tool to call.
            arguments: Arguments for the tool.

        Returns:
            Result containing the tool result or an error.
        """
        handler = self._handlers.get(name)
        if not handler:
            return Result.err(
                MCPResourceNotFoundError(
                    f"Tool not found: {name}",
                    resource_type="tool",
                    resource_id=name,
                )
            )

        try:
            log.debug("mcp.registry.calling_tool", tool=name)
            result = await handler.handle(arguments)
            return result
        except Exception as e:
            log.error("mcp.registry.tool_error", tool=name, error=str(e))
            return Result.err(
                MCPToolError(
                    f"Tool execution failed: {e}",
                    tool_name=name,
                )
            )

    def get_definition(self, name: str) -> MCPToolDefinition | None:
        """Get the definition for a tool.

        Args:
            name: Name of the tool.

        Returns:
            The tool definition or None if not found.
        """
        handler = self._handlers.get(name)
        return handler.definition if handler else None

    def has_tool(self, name: str) -> bool:
        """Check if a tool is registered.

        Args:
            name: Name of the tool.

        Returns:
            True if the tool is registered.
        """
        return name in self._handlers

    def clear(self) -> None:
        """Clear all registered tools."""
        self._handlers.clear()
        self._categories.clear()
        log.info("mcp.registry.cleared")


# Global registry instance for convenience
_global_registry: ToolRegistry | None = None


def get_global_registry() -> ToolRegistry:
    """Get the global tool registry.

    Returns:
        The global ToolRegistry instance.
    """
    global _global_registry
    if _global_registry is None:
        _global_registry = ToolRegistry()
    return _global_registry


def register_tool(
    handler: ToolHandler,
    *,
    category: str = "default",
) -> None:
    """Register a tool handler with the global registry.

    Args:
        handler: The tool handler to register.
        category: Optional category for grouping tools.
    """
    get_global_registry().register(handler, category=category)
