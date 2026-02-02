"""Ouroboros tool definitions for MCP server.

This module defines the standard Ouroboros tools that are exposed
via the MCP server:
- execute_seed: Execute a seed (task specification)
- session_status: Get current session status
- query_events: Query event history
"""

from dataclasses import dataclass
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)

log = structlog.get_logger(__name__)


@dataclass
class ExecuteSeedHandler:
    """Handler for the execute_seed tool.

    Executes a seed (task specification) in the Ouroboros system.
    This is the primary entry point for running tasks.
    """

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_execute_seed",
            description=(
                "Execute a seed (task specification) in Ouroboros. "
                "A seed defines a task to be executed with acceptance criteria."
            ),
            parameters=(
                MCPToolParameter(
                    name="seed_content",
                    type=ToolInputType.STRING,
                    description="The seed content describing the task to execute",
                    required=True,
                ),
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Optional session ID to resume. If not provided, a new session is created.",
                    required=False,
                ),
                MCPToolParameter(
                    name="model_tier",
                    type=ToolInputType.STRING,
                    description="Model tier to use (small, medium, large). Default: medium",
                    required=False,
                    default="medium",
                    enum=("small", "medium", "large"),
                ),
                MCPToolParameter(
                    name="max_iterations",
                    type=ToolInputType.INTEGER,
                    description="Maximum number of execution iterations. Default: 10",
                    required=False,
                    default=10,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a seed execution request.

        Args:
            arguments: Tool arguments including seed_content.

        Returns:
            Result containing execution result or error.
        """
        seed_content = arguments.get("seed_content")
        if not seed_content:
            return Result.err(
                MCPToolError(
                    "seed_content is required",
                    tool_name="ouroboros_execute_seed",
                )
            )

        session_id = arguments.get("session_id")
        model_tier = arguments.get("model_tier", "medium")
        max_iterations = arguments.get("max_iterations", 10)

        log.info(
            "mcp.tool.execute_seed",
            session_id=session_id,
            model_tier=model_tier,
            max_iterations=max_iterations,
        )

        # This is a placeholder - actual implementation would integrate
        # with the Ouroboros execution engine
        try:
            # TODO: Integrate with actual execution engine
            result_text = (
                f"Seed execution initiated.\n"
                f"Session ID: {session_id or 'new'}\n"
                f"Model tier: {model_tier}\n"
                f"Max iterations: {max_iterations}\n"
                f"Seed content: {seed_content[:100]}..."
            )

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text=result_text),
                    ),
                    is_error=False,
                    meta={"session_id": session_id or "new-session-id"},
                )
            )
        except Exception as e:
            log.error("mcp.tool.execute_seed.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed execution failed: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )


@dataclass
class SessionStatusHandler:
    """Handler for the session_status tool.

    Returns the current status of an Ouroboros session.
    """

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_session_status",
            description=(
                "Get the status of an Ouroboros session. "
                "Returns information about the current phase, progress, and any errors."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="The session ID to query",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a session status request.

        Args:
            arguments: Tool arguments including session_id.

        Returns:
            Result containing session status or error.
        """
        session_id = arguments.get("session_id")
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_session_status",
                )
            )

        log.info("mcp.tool.session_status", session_id=session_id)

        try:
            # TODO: Integrate with actual session management
            status_text = (
                f"Session: {session_id}\n"
                f"Status: active\n"
                f"Phase: execution\n"
                f"Progress: 60%\n"
                f"Current iteration: 3/10\n"
            )

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text=status_text),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "status": "active",
                        "phase": "execution",
                        "progress": 0.6,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.session_status.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to get session status: {e}",
                    tool_name="ouroboros_session_status",
                )
            )


@dataclass
class QueryEventsHandler:
    """Handler for the query_events tool.

    Queries the event history for a session or across sessions.
    """

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_query_events",
            description=(
                "Query the event history for an Ouroboros session. "
                "Returns a list of events matching the specified criteria."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Filter events by session ID. If not provided, returns events across all sessions.",
                    required=False,
                ),
                MCPToolParameter(
                    name="event_type",
                    type=ToolInputType.STRING,
                    description="Filter by event type (e.g., 'execution', 'evaluation', 'error')",
                    required=False,
                ),
                MCPToolParameter(
                    name="limit",
                    type=ToolInputType.INTEGER,
                    description="Maximum number of events to return. Default: 50",
                    required=False,
                    default=50,
                ),
                MCPToolParameter(
                    name="offset",
                    type=ToolInputType.INTEGER,
                    description="Number of events to skip for pagination. Default: 0",
                    required=False,
                    default=0,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle an event query request.

        Args:
            arguments: Tool arguments for filtering events.

        Returns:
            Result containing matching events or error.
        """
        session_id = arguments.get("session_id")
        event_type = arguments.get("event_type")
        limit = arguments.get("limit", 50)
        offset = arguments.get("offset", 0)

        log.info(
            "mcp.tool.query_events",
            session_id=session_id,
            event_type=event_type,
            limit=limit,
            offset=offset,
        )

        try:
            # TODO: Integrate with actual event store
            events_text = (
                f"Event Query Results\n"
                f"==================\n"
                f"Session: {session_id or 'all'}\n"
                f"Type filter: {event_type or 'all'}\n"
                f"Showing {offset} to {offset + limit}\n\n"
                f"1. [execution] Started seed execution\n"
                f"2. [evaluation] Mechanical check passed\n"
                f"3. [execution] Iteration 2 completed\n"
            )

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text=events_text),
                    ),
                    is_error=False,
                    meta={
                        "total_events": 3,
                        "offset": offset,
                        "limit": limit,
                    },
                )
            )
        except Exception as e:
            log.error("mcp.tool.query_events.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_query_events",
                )
            )


# Convenience functions for handler access
def execute_seed_handler() -> ExecuteSeedHandler:
    """Create an ExecuteSeedHandler instance."""
    return ExecuteSeedHandler()


def session_status_handler() -> SessionStatusHandler:
    """Create a SessionStatusHandler instance."""
    return SessionStatusHandler()


def query_events_handler() -> QueryEventsHandler:
    """Create a QueryEventsHandler instance."""
    return QueryEventsHandler()


# List of all Ouroboros tools for registration
OUROBOROS_TOOLS: tuple[ExecuteSeedHandler | SessionStatusHandler | QueryEventsHandler, ...] = (
    ExecuteSeedHandler(),
    SessionStatusHandler(),
    QueryEventsHandler(),
)
