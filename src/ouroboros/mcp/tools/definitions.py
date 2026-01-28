"""Ouroboros tool definitions for MCP server.

This module defines the standard Ouroboros tools that are exposed
via the MCP server:
- execute_seed: Execute a seed (task specification)
- session_status: Get current session status
- query_events: Query event history
"""

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import structlog
import yaml
from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from ouroboros.core.seed import Seed
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
from ouroboros.orchestrator.adapter import ClaudeAgentAdapter
from ouroboros.orchestrator.runner import OrchestratorRunner
from ouroboros.orchestrator.session import SessionRepository
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)


# =============================================================================
# Progress Tracking
# =============================================================================


@dataclass
class ProgressTracker:
    """Tracks progress by polling events from EventStore.

    Attributes:
        event_store: EventStore to poll for events.
        session_id: Session to track.
        ac_statuses: Dict mapping AC index to status symbol.
        phase: Current execution phase.
        duration: Execution duration in seconds.
        cost: Estimated cost in dollars.
        tokens: Total tokens used.
        poll_interval: Seconds between polls.
    """

    event_store: EventStore
    session_id: str
    ac_count: int
    ac_statuses: dict[int, str] = field(default_factory=dict)
    phase: str = "initializing"
    duration: float = 0.0
    cost: float = 0.0
    tokens: int = 0
    poll_interval: float = 0.5
    start_time: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Initialize AC statuses."""
        for i in range(self.ac_count):
            self.ac_statuses[i] = "⏳"  # pending

    async def poll_events(self) -> None:
        """Poll for new events and update progress."""
        try:
            async for event in self.event_store.poll_events(
                aggregate_type="session",
                aggregate_id=self.session_id,
                poll_interval=self.poll_interval,
            ):
                # Update based on event type
                if event.type == "orchestrator.session.started":
                    self.phase = "executing"
                elif event.type == "orchestrator.progress.updated":
                    self.phase = "in_progress"
                elif event.type == "orchestrator.task.started":
                    # Mark AC as in progress
                    ac_text = event.data.get("acceptance_criterion", "")
                    for idx in range(self.ac_count):
                        if str(idx) in ac_text or f"AC{idx}" in ac_text:
                            self.ac_statuses[idx] = "🔄"
                            break
                elif event.type == "orchestrator.task.completed":
                    # Mark AC as completed or failed
                    ac_text = event.data.get("acceptance_criterion", "")
                    success = event.data.get("success", False)
                    for idx in range(self.ac_count):
                        if str(idx) in ac_text or f"AC{idx}" in ac_text:
                            self.ac_statuses[idx] = "✅" if success else "❌"
                            break
                elif event.type == "orchestrator.session.completed":
                    self.phase = "completed"
                    # Mark remaining as completed
                    for idx in range(self.ac_count):
                        if self.ac_statuses[idx] == "⏳":
                            self.ac_statuses[idx] = "✅"
                    break
                elif event.type == "orchestrator.session.failed":
                    self.phase = "failed"
                    # Mark remaining as failed
                    for idx in range(self.ac_count):
                        if self.ac_statuses[idx] in ("⏳", "🔄"):
                            self.ac_statuses[idx] = "❌"
                    break

                # Update duration
                self.duration = (datetime.now(UTC) - self.start_time).total_seconds()

        except Exception as e:
            log.warning("progress_tracker.poll_failed", error=str(e))

    def build_progress_table(self, ac_list: tuple[str, ...]) -> Table:
        """Build Rich table showing AC progress.

        Args:
            ac_list: List of acceptance criteria.

        Returns:
            Rich Table with progress.
        """
        table = Table(title="Acceptance Criteria Progress", show_header=True)
        table.add_column("Status", width=8)
        table.add_column("Acceptance Criterion", ratio=1)

        for idx, ac in enumerate(ac_list):
            status = self.ac_statuses.get(idx, "⏳")
            table.add_row(status, ac[:80])

        return table

    def get_completion_percentage(self) -> float:
        """Calculate overall completion percentage.

        Returns:
            Percentage (0.0 to 100.0).
        """
        completed = sum(1 for s in self.ac_statuses.values() if s == "✅")
        return (completed / max(self.ac_count, 1)) * 100.0


# =============================================================================
# Execute Seed Handler
# =============================================================================


@dataclass
class ExecuteSeedHandler:
    """Handler for the execute_seed tool.

    Executes a seed (task specification) in the Ouroboros system.
    This is the primary entry point for running tasks with real-time
    progress visualization.
    """

    event_store: EventStore | None = None
    console: Console | None = None

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

        try:
            # Parse YAML seed content
            try:
                seed_dict = yaml.safe_load(seed_content)
                seed = Seed.from_dict(seed_dict)
            except Exception as e:
                log.error("mcp.tool.execute_seed.parse_error", error=str(e))
                return Result.err(
                    MCPToolError(
                        f"Failed to parse seed YAML: {e}",
                        tool_name="ouroboros_execute_seed",
                    )
                )

            # Execute with live progress
            result = await self.execute_with_live_progress(
                seed=seed,
                session_id=session_id,
                model_tier=model_tier,
                max_iterations=max_iterations,
            )

            if result.is_ok:
                orch_result = result.value
                summary_text = (
                    f"Execution {'completed' if orch_result.success else 'failed'}.\n"
                    f"Session ID: {orch_result.session_id}\n"
                    f"Messages processed: {orch_result.messages_processed}\n"
                    f"Duration: {orch_result.duration_seconds:.1f}s\n"
                    f"Goal: {seed.goal}\n"
                    f"Acceptance criteria: {len(seed.acceptance_criteria)}\n"
                )

                return Result.ok(
                    MCPToolResult(
                        content=(
                            MCPContentItem(type=ContentType.TEXT, text=summary_text),
                        ),
                        is_error=not orch_result.success,
                        meta={
                            "session_id": orch_result.session_id,
                            "success": orch_result.success,
                            "duration_seconds": orch_result.duration_seconds,
                            "messages_processed": orch_result.messages_processed,
                        },
                    )
                )
            else:
                error_msg = f"Execution failed: {result.error}"
                log.error("mcp.tool.execute_seed.execution_error", error=error_msg)
                return Result.err(
                    MCPToolError(
                        error_msg,
                        tool_name="ouroboros_execute_seed",
                    )
                )

        except Exception as e:
            log.exception("mcp.tool.execute_seed.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Seed execution failed: {e}",
                    tool_name="ouroboros_execute_seed",
                )
            )

    async def execute_with_live_progress(
        self,
        seed: Seed,
        session_id: str | None = None,
        model_tier: str = "medium",
        max_iterations: int = 10,
    ) -> Result[Any, Any]:
        """Execute seed with live Rich progress display.

        Args:
            seed: Seed to execute.
            session_id: Optional session ID to resume.
            model_tier: Model tier (unused for now).
            max_iterations: Max iterations (unused for now).

        Returns:
            Result containing OrchestratorResult.
        """
        # Initialize components
        event_store = self.event_store
        if event_store is None:
            # Create default event store
            import os

            db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
            await event_store.initialize()

        console = self.console or Console()
        adapter = ClaudeAgentAdapter()
        runner = OrchestratorRunner(adapter, event_store, console)

        # Execute seed (resume or new)
        if session_id:
            result = await runner.resume_session(session_id, seed)
        else:
            result = await runner.execute_seed(seed)

        return result


# =============================================================================
# Session Status Handler
# =============================================================================


@dataclass
class SessionStatusHandler:
    """Handler for the session_status tool.

    Returns the current status of an Ouroboros session by reconstructing
    it from events in the EventStore.
    """

    event_store: EventStore | None = None

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
            # Initialize event store if needed
            event_store = self.event_store
            if event_store is None:
                import os

                db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
                event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
                await event_store.initialize()

            # Reconstruct session from events
            repo = SessionRepository(event_store)
            session_result = await repo.reconstruct_session(session_id)

            if session_result.is_err:
                return Result.err(
                    MCPToolError(
                        f"Session not found or could not be reconstructed: {session_result.error}",
                        tool_name="ouroboros_session_status",
                    )
                )

            tracker = session_result.value

            # Build status text
            status_text = (
                f"Session: {session_id}\n"
                f"Status: {tracker.status.value}\n"
                f"Execution ID: {tracker.execution_id}\n"
                f"Seed ID: {tracker.seed_id}\n"
                f"Messages processed: {tracker.messages_processed}\n"
                f"Started at: {tracker.start_time.isoformat()}\n"
            )

            if tracker.completed_at:
                status_text += f"Completed at: {tracker.completed_at.isoformat()}\n"
            if tracker.error_message:
                status_text += f"Error: {tracker.error_message}\n"

            # Calculate progress percentage if we have progress data
            progress_pct = tracker.progress.get("completion_percentage", 0.0)

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text=status_text),
                    ),
                    is_error=False,
                    meta={
                        "session_id": session_id,
                        "status": tracker.status.value,
                        "messages_processed": tracker.messages_processed,
                        "progress": progress_pct,
                    },
                )
            )
        except Exception as e:
            log.exception("mcp.tool.session_status.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to get session status: {e}",
                    tool_name="ouroboros_session_status",
                )
            )


# =============================================================================
# Query Events Handler
# =============================================================================


@dataclass
class QueryEventsHandler:
    """Handler for the query_events tool.

    Queries the event history for a session or across sessions using
    EventStore.replay().
    """

    event_store: EventStore | None = None

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
                    description="Filter events by session ID (required).",
                    required=True,
                ),
                MCPToolParameter(
                    name="event_type",
                    type=ToolInputType.STRING,
                    description="Filter by event type (e.g., 'orchestrator.session.started', 'orchestrator.tool.called')",
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
        if not session_id:
            return Result.err(
                MCPToolError(
                    "session_id is required",
                    tool_name="ouroboros_query_events",
                )
            )

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
            # Initialize event store if needed
            event_store = self.event_store
            if event_store is None:
                import os

                db_path = os.path.expanduser("~/.ouroboros/ouroboros.db")
                event_store = EventStore(f"sqlite+aiosqlite:///{db_path}")
                await event_store.initialize()

            # Replay events for the session
            events = await event_store.replay("session", session_id)

            # Filter by event type if specified
            if event_type:
                events = [e for e in events if e.type == event_type]

            # Apply pagination
            total_events = len(events)
            events = events[offset : offset + limit]

            # Build events text
            events_text = (
                f"Event Query Results\n"
                f"==================\n"
                f"Session: {session_id}\n"
                f"Type filter: {event_type or 'all'}\n"
                f"Showing {offset + 1} to {offset + len(events)} of {total_events}\n\n"
            )

            for i, event in enumerate(events, start=offset + 1):
                timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                events_text += f"{i}. [{event.type}] at {timestamp}\n"
                # Add key data fields
                if "seed_goal" in event.data:
                    events_text += f"   Goal: {event.data['seed_goal'][:60]}\n"
                if "tool_name" in event.data:
                    events_text += f"   Tool: {event.data['tool_name']}\n"
                if "error" in event.data:
                    events_text += f"   Error: {event.data['error'][:60]}\n"
                if "message_type" in event.data:
                    events_text += f"   Message type: {event.data['message_type']}\n"

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(type=ContentType.TEXT, text=events_text),
                    ),
                    is_error=False,
                    meta={
                        "total_events": total_events,
                        "offset": offset,
                        "limit": limit,
                        "session_id": session_id,
                    },
                )
            )
        except Exception as e:
            log.exception("mcp.tool.query_events.error", error=str(e))
            return Result.err(
                MCPToolError(
                    f"Failed to query events: {e}",
                    tool_name="ouroboros_query_events",
                )
            )


# =============================================================================
# Convenience Functions
# =============================================================================


def execute_seed_handler(
    event_store: EventStore | None = None,
    console: Console | None = None,
) -> ExecuteSeedHandler:
    """Create an ExecuteSeedHandler instance.

    Args:
        event_store: Optional EventStore instance.
        console: Optional Rich Console instance.

    Returns:
        ExecuteSeedHandler instance.
    """
    return ExecuteSeedHandler(event_store=event_store, console=console)


def session_status_handler(event_store: EventStore | None = None) -> SessionStatusHandler:
    """Create a SessionStatusHandler instance.

    Args:
        event_store: Optional EventStore instance.

    Returns:
        SessionStatusHandler instance.
    """
    return SessionStatusHandler(event_store=event_store)


def query_events_handler(event_store: EventStore | None = None) -> QueryEventsHandler:
    """Create a QueryEventsHandler instance.

    Args:
        event_store: Optional EventStore instance.

    Returns:
        QueryEventsHandler instance.
    """
    return QueryEventsHandler(event_store=event_store)


# List of all Ouroboros tools for registration
OUROBOROS_TOOLS: tuple[ExecuteSeedHandler | SessionStatusHandler | QueryEventsHandler, ...] = (
    ExecuteSeedHandler(),
    SessionStatusHandler(),
    QueryEventsHandler(),
)


__all__ = [
    "ExecuteSeedHandler",
    "SessionStatusHandler",
    "QueryEventsHandler",
    "ProgressTracker",
    "execute_seed_handler",
    "session_status_handler",
    "query_events_handler",
    "OUROBOROS_TOOLS",
]
