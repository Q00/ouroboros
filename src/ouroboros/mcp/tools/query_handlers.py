"""Query and status tool handlers for MCP server.

This module contains handlers for querying session state and events:
- SessionStatusHandler: Get current session status
- QueryEventsHandler: Query event history
- ACDashboardHandler: Per-AC pass/fail compliance dashboard
"""

from dataclasses import dataclass, field
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
from ouroboros.orchestrator.session import SessionRepository, SessionStatus
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)


@dataclass
class SessionStatusHandler:
    """Handler for the session_status tool.

    Returns the current status of an Ouroboros session.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize the session repository after dataclass creation."""
        self._event_store = self.event_store or EventStore()
        self._session_repo = SessionRepository(self._event_store)
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

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
            # Ensure event store is initialized
            await self._ensure_initialized()

            # Query session state from repository
            result = await self._session_repo.reconstruct_session(session_id)

            if result.is_err:
                error = result.error
                return Result.err(
                    MCPToolError(
                        f"Session not found: {error.message}",
                        tool_name="ouroboros_session_status",
                    )
                )

            tracker = result.value

            # Build status response from SessionTracker.
            # The "Terminal:" line is a machine-parseable summary so callers
            # can reliably detect end-of-session without substring-matching
            # "completed" against the entire text body (which may contain the
            # word in AC descriptions, progress dicts, etc.).
            is_terminal = tracker.status in {
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.CANCELLED,
            }
            status_text = (
                f"Session: {tracker.session_id}\n"
                f"Status: {tracker.status.value}\n"
                f"Terminal: {is_terminal}\n"
                f"Execution ID: {tracker.execution_id}\n"
                f"Seed ID: {tracker.seed_id}\n"
                f"Messages Processed: {tracker.messages_processed}\n"
                f"Start Time: {tracker.start_time.isoformat()}\n"
            )

            if tracker.last_message_time:
                status_text += f"Last Message: {tracker.last_message_time.isoformat()}\n"

            if tracker.progress:
                status_text += "\nProgress:\n"
                for key, value in tracker.progress.items():
                    status_text += f"  {key}: {value}\n"

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=status_text),),
                    is_error=False,
                    meta={
                        "session_id": tracker.session_id,
                        "status": tracker.status.value,
                        "execution_id": tracker.execution_id,
                        "seed_id": tracker.seed_id,
                        "is_active": tracker.is_active,
                        "is_completed": tracker.is_completed,
                        "is_failed": tracker.is_failed,
                        "messages_processed": tracker.messages_processed,
                        "progress": tracker.progress,
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

    event_store: EventStore | None = field(default=None, repr=False)

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
            # Use injected or create event store
            store = self.event_store or EventStore()
            await store.initialize()

            # Query events from the store
            if session_id:
                events = await store.query_session_related_events(
                    session_id=session_id,
                    event_type=event_type,
                    limit=limit,
                    offset=offset,
                )
            else:
                events = await store.query_events(
                    aggregate_id=None,
                    event_type=event_type,
                    limit=limit,
                    offset=offset,
                )

            # Only close if we created the store ourselves
            if self.event_store is None:
                await store.close()

            # Format events for response
            events_text = self._format_events(events, session_id, event_type, offset, limit)

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=events_text),),
                    is_error=False,
                    meta={
                        "total_events": len(events),
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

    def _format_events(
        self,
        events: list,
        session_id: str | None,
        event_type: str | None,
        offset: int,
        limit: int,
    ) -> str:
        """Format events as human-readable text.

        Args:
            events: List of BaseEvent objects.
            session_id: Optional session ID filter.
            event_type: Optional event type filter.
            offset: Pagination offset.
            limit: Pagination limit.

        Returns:
            Formatted text representation.
        """
        lines = [
            "Event Query Results",
            "=" * 60,
            f"Session: {session_id or 'all'}",
            f"Type filter: {event_type or 'all'}",
            f"Showing {offset} to {offset + len(events)} (found {len(events)} events)",
            "",
        ]

        if not events:
            lines.append("No events found matching the criteria.")
        else:
            for i, event in enumerate(events, start=offset + 1):
                lines.extend(
                    [
                        f"{i}. [{event.type}]",
                        f"   ID: {event.id}",
                        f"   Timestamp: {event.timestamp.isoformat()}",
                        f"   Aggregate: {event.aggregate_type}/{event.aggregate_id}",
                        f"   Data: {str(event.data)[:100]}..."
                        if len(str(event.data)) > 100
                        else f"   Data: {event.data}",
                        "",
                    ]
                )

        return "\n".join(lines)


@dataclass
class ACDashboardHandler:
    """Handler for the ouroboros_ac_dashboard tool.

    Displays per-AC pass/fail visibility across generations
    with three display modes: summary, full, ac.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        """Initialize event store."""
        self._event_store = self.event_store or EventStore()
        self._initialized = False

    async def _ensure_initialized(self) -> None:
        """Ensure the event store is initialized."""
        if not self._initialized:
            await self._event_store.initialize()
            self._initialized = True

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_ac_dashboard",
            description=(
                "Display per-AC pass/fail compliance dashboard across generations. "
                "Shows which acceptance criteria passed, failed, or are flaky. "
                "Modes: 'summary' (default), 'full' (AC x Gen matrix), 'ac' (single AC history)."
            ),
            parameters=(
                MCPToolParameter(
                    name="lineage_id",
                    type=ToolInputType.STRING,
                    description="ID of the lineage to display",
                    required=True,
                ),
                MCPToolParameter(
                    name="mode",
                    type=ToolInputType.STRING,
                    description="Display mode: 'summary' (default), 'full', or 'ac'",
                    required=False,
                ),
                MCPToolParameter(
                    name="ac_index",
                    type=ToolInputType.INTEGER,
                    description="AC index (1-based) for 'ac' mode. Required when mode='ac'.",
                    required=False,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a dashboard request."""
        lineage_id = arguments.get("lineage_id")
        if not lineage_id:
            return Result.err(
                MCPToolError(
                    "lineage_id is required",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        mode = arguments.get("mode", "summary")
        ac_index = arguments.get("ac_index")

        await self._ensure_initialized()

        try:
            events = await self._event_store.replay_lineage(lineage_id)
        except Exception as e:
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        if not events:
            return Result.err(
                MCPToolError(
                    f"No lineage found with ID: {lineage_id}",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        from ouroboros.evolution.projector import LineageProjector
        from ouroboros.mcp.tools.dashboard import (
            format_full,
            format_single_ac,
            format_summary,
        )

        projector = LineageProjector()
        lineage = projector.project(events)

        if lineage is None:
            return Result.err(
                MCPToolError(
                    f"Failed to project lineage: {lineage_id}",
                    tool_name="ouroboros_ac_dashboard",
                )
            )

        if mode == "full":
            text = format_full(lineage)
        elif mode == "ac":
            if ac_index is None:
                return Result.err(
                    MCPToolError(
                        "ac_index is required for mode='ac'",
                        tool_name="ouroboros_ac_dashboard",
                    )
                )
            text = format_single_ac(lineage, int(ac_index) - 1)  # Convert to 0-based
        else:
            text = format_summary(lineage)

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "lineage_id": lineage.lineage_id,
                    "mode": mode,
                    "generations": lineage.current_generation,
                },
            )
        )
