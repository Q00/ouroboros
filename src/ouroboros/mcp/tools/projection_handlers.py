"""Read-only MCP query surface for harness projections."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.harness.projection_builder import ProjectionBuildResult, build_projection
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

_DEFAULT_LIMIT = 1000


@dataclass
class ProjectionQueryHandler:
    """Build a Run/Stage/Step projection from persisted events.

    This is the #946 PR-2 read-only MCP surface. It intentionally computes
    projections on demand from the EventStore rather than caching or mutating
    any rows.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the MCP tool definition."""
        return MCPToolDefinition(
            name="ouroboros_query_projection",
            description=(
                "Build a read-only Run/Stage/Step projection from persisted "
                "Ouroboros events for a session or execution aggregate."
            ),
            parameters=(
                MCPToolParameter(
                    name="session_id",
                    type=ToolInputType.STRING,
                    description="Optional orchestrator session ID to project.",
                    required=False,
                ),
                MCPToolParameter(
                    name="execution_id",
                    type=ToolInputType.STRING,
                    description=(
                        "Optional execution aggregate ID. When session_id is also "
                        "provided, this narrows related session-event lookup."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="seed_id",
                    type=ToolInputType.STRING,
                    description=(
                        "Optional seed ID override. If omitted, the handler derives "
                        "one from events and falls back to the queried ID."
                    ),
                    required=False,
                ),
                MCPToolParameter(
                    name="limit",
                    type=ToolInputType.INTEGER,
                    description=(
                        "Maximum related events to inspect for session queries. "
                        "Default: 1000."
                    ),
                    required=False,
                    default=_DEFAULT_LIMIT,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a projection query request."""
        session_id = _string_argument(arguments, "session_id")
        execution_id = _string_argument(arguments, "execution_id")
        seed_id_override = _string_argument(arguments, "seed_id")
        limit = _positive_int_argument(arguments.get("limit", _DEFAULT_LIMIT))

        if session_id is None and execution_id is None:
            return Result.err(
                MCPToolError(
                    "session_id or execution_id is required",
                    tool_name="ouroboros_query_projection",
                )
            )
        if limit is None:
            return Result.err(
                MCPToolError(
                    "limit must be a positive integer",
                    tool_name="ouroboros_query_projection",
                )
            )

        log.info(
            "mcp.tool.query_projection",
            session_id=session_id,
            execution_id=execution_id,
            limit=limit,
        )

        store = self.event_store
        owns_event_store = False

        try:
            if store is None:
                store = EventStore(read_only=True)
                owns_event_store = True
            await store.initialize(create_schema=False if owns_event_store else None)

            events = await _load_projection_events(
                store,
                session_id=session_id,
                execution_id=execution_id,
                limit=limit,
            )
            ordered_events = tuple(
                sorted(
                    (_ensure_aware_timestamp(event) for event in events),
                    key=lambda event: (event.timestamp, event.id),
                )
            )
            seed_id = seed_id_override or _derive_seed_id(ordered_events, session_id, execution_id)
            goal = _derive_goal(ordered_events)
            projection = build_projection(ordered_events, seed_id=seed_id, goal=goal)

            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=_format_projection_text(projection, len(ordered_events)),
                        ),
                    ),
                    is_error=False,
                    meta=_projection_meta(
                        projection,
                        session_id=session_id,
                        execution_id=execution_id,
                        seed_id=seed_id,
                        event_count=len(ordered_events),
                        limit=limit,
                    ),
                )
            )
        except Exception as exc:
            log.error("mcp.tool.query_projection.error", error=str(exc))
            return Result.err(
                MCPToolError(
                    f"Failed to query projection: {exc}",
                    tool_name="ouroboros_query_projection",
                )
            )
        finally:
            if owns_event_store and store is not None:
                await store.close()


async def _load_projection_events(
    store: EventStore,
    *,
    session_id: str | None,
    execution_id: str | None,
    limit: int,
) -> list[BaseEvent]:
    if session_id is not None:
        return await store.query_session_related_events(
            session_id=session_id,
            execution_id=execution_id,
            limit=limit,
        )
    if execution_id is not None:
        return await store.replay("execution", execution_id)
    return []


def _projection_meta(
    projection: ProjectionBuildResult,
    *,
    session_id: str | None,
    execution_id: str | None,
    seed_id: str,
    event_count: int,
    limit: int,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "execution_id": execution_id,
        "seed_id": seed_id,
        "event_count": event_count,
        "limit": limit,
        "run": projection.run.model_dump(mode="json"),
        "stages": [stage.model_dump(mode="json") for stage in projection.stages],
        "steps": [step.model_dump(mode="json") for step in projection.steps],
        "artifacts": [],
        "verdicts": [],
    }


def _format_projection_text(projection: ProjectionBuildResult, event_count: int) -> str:
    lines = [
        "Run Projection",
        "=" * 60,
        f"Run: {projection.run.run_id}",
        f"Seed: {projection.run.seed_id}",
        f"Events inspected: {event_count}",
        f"Stages: {len(projection.stages)}",
        f"Steps: {len(projection.steps)}",
    ]
    if projection.steps:
        lines.append("")
        lines.append("Steps:")
        for step in projection.steps:
            status = "pending" if step.ok is None else ("ok" if step.ok else "error")
            lines.append(f"- {step.step_id} [{step.kind.value}] {step.name}: {status}")
    return "\n".join(lines)


def _derive_seed_id(
    events: Sequence[BaseEvent],
    session_id: str | None,
    execution_id: str | None,
) -> str:
    for event in events:
        value = event.data.get("seed_id") if isinstance(event.data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    fallback = execution_id or session_id or "projection"
    return fallback.strip() or "projection"


def _ensure_aware_timestamp(event: BaseEvent) -> BaseEvent:
    if event.timestamp.tzinfo is not None:
        return event
    return event.model_copy(update={"timestamp": event.timestamp.replace(tzinfo=UTC)})


def _derive_goal(events: Sequence[BaseEvent]) -> str:
    for event in events:
        if not isinstance(event.data, dict):
            continue
        for key in ("goal", "objective"):
            value = event.data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _string_argument(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _positive_int_argument(value: Any) -> int | None:
    if isinstance(value, int) and value > 0:
        return value
    return None


__all__ = ["ProjectionQueryHandler"]
