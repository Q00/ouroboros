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

_SESSION_TERMINAL_EVENT_TYPES = frozenset(
    {
        "orchestrator.session.cancelled",
        "orchestrator.session.completed",
        "orchestrator.session.failed",
        "orchestrator.session.paused",
    }
)


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
                        "Optional safety cap for related events. If the run has "
                        "more events than this cap, the tool fails instead of "
                        "returning a partial projection."
                    ),
                    required=False,
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
        raw_limit = arguments.get("limit")
        limit = _optional_positive_int_argument(raw_limit)

        if session_id is None and execution_id is None:
            return Result.err(
                MCPToolError(
                    "session_id or execution_id is required",
                    tool_name="ouroboros_query_projection",
                )
            )
        if raw_limit is not None and limit is None:
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
            await store.initialize(create_schema=False)

            events = await _load_projection_events(
                store,
                session_id=session_id,
                execution_id=execution_id,
            )
            ordered_events = tuple(
                sorted(
                    (_ensure_aware_timestamp(event) for event in events),
                    key=lambda event: (event.timestamp, event.id),
                )
            )
            if not ordered_events:
                return Result.err(
                    MCPToolError(
                        "No events found for projection query",
                        tool_name="ouroboros_query_projection",
                    )
                )
            if limit is not None and len(ordered_events) > limit:
                return Result.err(
                    MCPToolError(
                        (
                            f"Projection event count {len(ordered_events)} exceeds "
                            f"limit {limit}; rerun without limit for a complete projection."
                        ),
                        tool_name="ouroboros_query_projection",
                    )
                )
            seed_id, seed_id_source = _resolve_seed_id(
                ordered_events,
                session_id,
                execution_id,
                override=seed_id_override,
            )
            goal = _derive_goal(ordered_events)
            projection = build_projection(
                ordered_events,
                seed_id=seed_id,
                goal=goal,
                source_key=_query_projection_source_key(
                    ordered_events,
                    session_id=session_id,
                    execution_id=execution_id,
                ),
            )

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
                        seed_id_source=seed_id_source,
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
) -> list[BaseEvent]:
    if session_id is not None:
        events = await store.query_session_related_events(
            session_id=session_id,
            execution_id=execution_id,
            limit=None,
        )
        if execution_id is None:
            declared_execution_ids = _session_execution_ids(events, session_id)
            if len(declared_execution_ids) > 1:
                msg = (
                    f"session_id {session_id!r} declares multiple executions; "
                    "provide execution_id for an unambiguous projection"
                )
                raise ValueError(msg)
            if len(declared_execution_ids) == 1:
                declared_execution_id = next(iter(declared_execution_ids))
                return [
                    event
                    for event in events
                    if _is_session_metadata_event(
                        event,
                        session_id,
                        execution_id=declared_execution_id,
                    )
                    or _is_session_terminal_event(event, session_id)
                    or _event_links_execution(event, declared_execution_id)
                ]
            payload_execution_ids = _event_payload_execution_ids(events, session_id)
            if len(payload_execution_ids) > 1:
                msg = (
                    f"session_id {session_id!r} references multiple executions; "
                    "provide execution_id for an unambiguous projection"
                )
                raise ValueError(msg)
            if len(payload_execution_ids) == 1:
                payload_execution_id = next(iter(payload_execution_ids))
                scoped_events = await store.query_session_related_events(
                    session_id=session_id,
                    execution_id=payload_execution_id,
                    limit=None,
                )
                return [
                    event
                    for event in scoped_events
                    if _is_session_metadata_event(event, session_id)
                    or _is_session_terminal_event(event, session_id)
                    or _event_links_execution(event, payload_execution_id)
                ]
            return events
        if not _session_declares_execution(
            events,
            session_id,
            execution_id,
        ) and not _session_payload_links_execution(
            events,
            session_id,
            execution_id,
        ):
            msg = f"execution_id {execution_id!r} does not belong to session_id {session_id!r}"
            raise ValueError(msg)
        return [
            event
            for event in events
            if _is_session_metadata_event(event, session_id, execution_id=execution_id)
            or _is_session_terminal_event(event, session_id)
            or _event_links_execution(event, execution_id)
        ]
    if execution_id is not None:
        return await store.query_execution_related_events(
            execution_id=execution_id,
            limit=None,
        )
    return []


def _projection_meta(
    projection: ProjectionBuildResult,
    *,
    session_id: str | None,
    execution_id: str | None,
    seed_id: str,
    seed_id_source: str,
    event_count: int,
    limit: int | None,
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "execution_id": execution_id,
        "seed_id": seed_id,
        "seed_id_source": seed_id_source,
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


def _resolve_seed_id(
    events: Sequence[BaseEvent],
    session_id: str | None,
    execution_id: str | None,
    *,
    override: str | None,
) -> tuple[str, str]:
    if override is not None:
        return override, "argument"
    seed_id = _derive_seed_id(events)
    if seed_id is not None:
        return seed_id, "event"
    fallback = execution_id or session_id or "projection"
    return fallback.strip() or "projection", "fallback"


def _derive_seed_id(
    events: Sequence[BaseEvent],
) -> str | None:
    for event in events:
        value = event.data.get("seed_id") if isinstance(event.data, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _ensure_aware_timestamp(event: BaseEvent) -> BaseEvent:
    if event.timestamp.tzinfo is not None:
        return event
    return event.model_copy(update={"timestamp": event.timestamp.replace(tzinfo=UTC)})


def _session_declares_execution(
    events: Sequence[BaseEvent],
    session_id: str,
    execution_id: str,
) -> bool:
    for event in events:
        if _is_session_metadata_event(event, session_id, execution_id=execution_id):
            return True
    return False


def _session_execution_ids(events: Sequence[BaseEvent], session_id: str) -> frozenset[str]:
    execution_ids: set[str] = set()
    for event in events:
        if not _is_session_metadata_event(event, session_id):
            continue
        value = event.data.get("execution_id") if isinstance(event.data, dict) else None
        if isinstance(value, str) and value.strip():
            execution_ids.add(value.strip())
    return frozenset(execution_ids)


def _session_payload_links_execution(
    events: Sequence[BaseEvent],
    session_id: str,
    execution_id: str,
) -> bool:
    return any(
        _event_payload_links_session_execution(event, session_id, execution_id) for event in events
    )


def _event_payload_execution_ids(
    events: Sequence[BaseEvent],
    session_id: str,
) -> frozenset[str]:
    execution_ids: set[str] = set()
    for event in events:
        if not isinstance(event.data, dict):
            continue
        for key in ("execution_id", "parent_execution_id"):
            value = event.data.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            execution_id = value.strip()
            if _event_payload_links_session_execution(event, session_id, execution_id):
                execution_ids.add(execution_id)
    return frozenset(execution_ids)


def _event_payload_links_session_execution(
    event: BaseEvent,
    session_id: str,
    execution_id: str,
) -> bool:
    if not isinstance(event.data, dict):
        return False
    payload_session_id = event.data.get("session_id")
    if not isinstance(payload_session_id, str) or payload_session_id.strip() != session_id:
        return False
    return _event_links_execution(event, execution_id)


def _is_session_metadata_event(
    event: BaseEvent,
    session_id: str,
    *,
    execution_id: str | None = None,
) -> bool:
    if event.aggregate_type != "session" or event.aggregate_id != session_id:
        return False
    if execution_id is None:
        return True
    if not isinstance(event.data, dict):
        return False
    value = event.data.get("execution_id")
    return isinstance(value, str) and value.strip() == execution_id


def _is_session_terminal_event(event: BaseEvent, session_id: str) -> bool:
    return (
        event.aggregate_type == "session"
        and event.aggregate_id == session_id
        and event.type in _SESSION_TERMINAL_EVENT_TYPES
    )


def _event_links_execution(event: BaseEvent, execution_id: str) -> bool:
    if event.aggregate_type != "execution":
        return False
    if event.aggregate_id == execution_id:
        return True
    if not isinstance(event.data, dict):
        return False
    for key in ("execution_id", "parent_execution_id"):
        value = event.data.get(key)
        if isinstance(value, str) and value.strip() == execution_id:
            return True
    return False


def _derive_goal(events: Sequence[BaseEvent]) -> str:
    for event in events:
        if not isinstance(event.data, dict):
            continue
        for key in ("seed_goal", "goal", "objective"):
            value = event.data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _query_projection_source_key(
    events: Sequence[BaseEvent],
    *,
    session_id: str | None,
    execution_id: str | None,
) -> str | None:
    if execution_id is not None:
        return f"execution:{execution_id}"
    if session_id is not None:
        execution_ids = _session_execution_ids(events, session_id)
        if len(execution_ids) == 1:
            return f"execution:{next(iter(execution_ids))}"
        payload_execution_ids = _event_payload_execution_ids(events, session_id)
        if len(payload_execution_ids) == 1:
            return f"execution:{next(iter(payload_execution_ids))}"
    if session_id is not None:
        return f"session:{session_id}"
    return None


def _string_argument(arguments: dict[str, Any], name: str) -> str | None:
    value = arguments.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_positive_int_argument(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


__all__ = ["ProjectionQueryHandler"]
