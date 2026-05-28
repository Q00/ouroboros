"""Job and execution management tool handlers for MCP server.

Contains handlers for background job operations and execution cancellation:
- CancelExecutionHandler: Cancel a running/paused execution session
- JobStatusHandler: Get status summary for a background job
- JobWaitHandler: Long-poll for job state changes
- JobResultHandler: Fetch terminal output for a completed job
- CancelJobHandler: Cancel a background job
"""

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import (
    JobManager,
    JobSnapshot,
    JobStatus,
    is_terminal_job_expired,
)
from ouroboros.mcp.tools.ac_tree_hud_handler import (
    format_subtask_progress_summary,
    summarize_subtask_events,
)
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

_DEFAULT_JOB_VIEW = "full"
_JOB_EXECUTION_EVENT_LIMIT = 250
_JOB_SUBTASK_EVENT_PAGE_SIZE = 500
_JOB_PROGRESS_EVENT_TYPES = {
    "workflow.progress.updated",
    "execution.node.created",
    "execution.node.updated",
    "execution.subtask.updated",
}
_JOB_VIEW_ALIASES = {
    "compact": "compact",
    "brief": "compact",
    "summary": "summary",
    "default": "full",
    "full": "full",
    "tree": "full",
    "verbose": "full",
}
_JOB_STREAM_ALIASES = {
    "default": "progress",
    "progress": "progress",
    "execution": "progress",
    "linked": "linked",
    "events": "linked",
    "all": "linked",
    "children": "linked",
    "subagents": "linked",
}
_JOB_STREAM_EVENT_LIMIT = 10


def _normalize_job_view(value: object) -> str:
    """Normalize requested job-status verbosity."""
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped:
            return _JOB_VIEW_ALIASES.get(stripped, _DEFAULT_JOB_VIEW)
    return _DEFAULT_JOB_VIEW


def _normalize_job_stream(value: object) -> str:
    """Normalize requested job wait stream scope."""
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped:
            return _JOB_STREAM_ALIASES.get(stripped, "progress")
    return "progress"


def _content_items_from_result_payload(
    payload: dict[str, Any] | None,
) -> tuple[MCPContentItem, ...]:
    """Rebuild MCP content items from a persisted terminal result payload."""
    if not isinstance(payload, dict):
        return ()
    raw_content = payload.get("content")
    if not isinstance(raw_content, list):
        return ()

    items: list[MCPContentItem] = []
    for raw_item in raw_content:
        if not isinstance(raw_item, dict):
            continue
        raw_type = raw_item.get("type")
        try:
            content_type = ContentType(raw_type)
        except ValueError:
            continue
        items.append(
            MCPContentItem(
                type=content_type,
                text=raw_item.get("text") if isinstance(raw_item.get("text"), str) else None,
                data=raw_item.get("data") if isinstance(raw_item.get("data"), str) else None,
                mime_type=(
                    raw_item.get("mime_type")
                    if isinstance(raw_item.get("mime_type"), str)
                    else None
                ),
                uri=raw_item.get("uri") if isinstance(raw_item.get("uri"), str) else None,
            )
        )
    return tuple(items)


def _parse_non_negative_int_argument(
    arguments: dict[str, Any],
    name: str,
    *,
    default: int,
    tool_name: str,
) -> Result[int, MCPServerError]:
    """Parse a non-negative integer tool argument into a stable MCP error."""
    raw = arguments.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return Result.err(
            MCPToolError(f"{name} must be a non-negative integer", tool_name=tool_name)
        )
    if value < 0:
        return Result.err(
            MCPToolError(f"{name} must be a non-negative integer", tool_name=tool_name)
        )
    return Result.ok(value)


async def _query_all_execution_subtask_events(
    event_store: EventStore, execution_id: str
) -> list[Any]:
    """Fetch subtask updates from one stable execution replay snapshot."""
    events, _cursor = await event_store.get_events_after("execution", execution_id, 0)
    return [
        event
        for event in events
        if event.type
        in {
            "execution.node.created",
            "execution.node.updated",
            "execution.subtask.updated",
        }
    ]


async def _query_latest_workflow_event(event_store: EventStore, execution_id: str) -> Any | None:
    """Fetch the latest workflow progress event without relying on a mixed window."""
    events = await event_store.query_events(
        aggregate_id=execution_id,
        event_type="workflow.progress.updated",
        limit=1,
    )
    return events[0] if events else None


def _event_scope(event: BaseEvent) -> str:
    return f"{event.aggregate_type}:{event.aggregate_id}"


def _compact_event_detail(event: BaseEvent) -> str:
    data = event.data
    for key in (
        "message",
        "activity_detail",
        "activity",
        "title",
        "content",
        "phase",
        "status",
        "reason",
        "tool_name",
    ):
        value = data.get(key)
        if value not in (None, ""):
            return str(value).replace("\n", " ")[:240]
    if data:
        pairs = []
        for key, value in list(data.items())[:4]:
            if value in (None, "", [], {}):
                continue
            pairs.append(f"{key}={str(value).replace(chr(10), ' ')[:80]}")
        if pairs:
            return ", ".join(pairs)[:240]
    return ""


def _event_stream_item(event: BaseEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "type": event.type,
        "timestamp": event.timestamp.isoformat(),
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "scope": _event_scope(event),
        "detail": _compact_event_detail(event),
    }


async def _query_linked_stream_events(
    event_store: EventStore,
    snapshot: JobSnapshot,
    cursor: int,
) -> tuple[list[BaseEvent], int]:
    """Fetch new events from the job's linked execution/session/lineage streams."""
    collected: dict[str, BaseEvent] = {}
    max_cursor = cursor

    async def collect(events: list[BaseEvent], new_cursor: int) -> None:
        nonlocal max_cursor
        max_cursor = max(max_cursor, new_cursor)
        for event in events:
            collected[event.id] = event

    job_events, job_cursor = await event_store.get_events_after("job", snapshot.job_id, cursor)
    await collect(job_events, job_cursor)

    if snapshot.links.execution_id:
        execution_events, execution_cursor = await event_store.get_events_after(
            "execution",
            snapshot.links.execution_id,
            cursor,
        )
        await collect(execution_events, execution_cursor)

    if snapshot.links.session_id:
        session_events, session_cursor = await event_store.query_session_related_events_after(
            session_id=snapshot.links.session_id,
            execution_id=snapshot.links.execution_id,
            last_row_id=cursor,
        )
        await collect(session_events, session_cursor)

    if snapshot.links.lineage_id:
        lineage_events, lineage_cursor = await event_store.get_events_after(
            "lineage",
            snapshot.links.lineage_id,
            cursor,
        )
        await collect(lineage_events, lineage_cursor)

    events = sorted(collected.values(), key=lambda event: (event.timestamp, event.id))
    return events, max_cursor


@dataclass
class CancelExecutionHandler:
    """Handler for the cancel_execution tool.

    Cancels a running or paused Ouroboros execution session.
    Validates that the execution exists and is not already in a terminal state
    (completed, failed, or cancelled) before performing cancellation.
    """

    event_store: EventStore | None = field(default=None, repr=False)

    # Terminal statuses that cannot be cancelled
    TERMINAL_STATUSES: tuple[SessionStatus, ...] = (
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.CANCELLED,
    )

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

    async def _resolve_session_id(self, execution_id: str) -> str | None:
        """Resolve an execution_id to its session_id via event store lookup."""
        events = await self._event_store.get_all_sessions()
        for event in events:
            if event.data.get("execution_id") == execution_id:
                return event.aggregate_id
        return None

    @property
    def definition(self) -> MCPToolDefinition:
        """Return the tool definition."""
        return MCPToolDefinition(
            name="ouroboros_cancel_execution",
            description=(
                "Cancel a running or paused Ouroboros execution. "
                "Validates that the execution exists and is not already in a "
                "terminal state (completed, failed, cancelled) before cancelling."
            ),
            parameters=(
                MCPToolParameter(
                    name="execution_id",
                    type=ToolInputType.STRING,
                    description="The execution/session ID to cancel",
                    required=True,
                ),
                MCPToolParameter(
                    name="reason",
                    type=ToolInputType.STRING,
                    description="Reason for cancellation",
                    required=False,
                    default="Cancelled by user",
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        """Handle a cancel execution request.

        Validates the execution exists and is not in a terminal state,
        then marks it as cancelled.

        Args:
            arguments: Tool arguments including execution_id and optional reason.

        Returns:
            Result containing cancellation confirmation or error.
        """
        execution_id = arguments.get("execution_id")
        if not execution_id:
            return Result.err(
                MCPToolError(
                    "execution_id is required",
                    tool_name="ouroboros_cancel_execution",
                )
            )

        reason = arguments.get("reason", "Cancelled by user")

        log.info(
            "mcp.tool.cancel_execution",
            execution_id=execution_id,
            reason=reason,
        )

        try:
            await self._ensure_initialized()

            # Try direct lookup first (user may have passed session_id)
            result = await self._session_repo.reconstruct_session(execution_id)

            if result.is_err:
                # Try resolving as execution_id
                session_id = await self._resolve_session_id(execution_id)
                if session_id is None:
                    return Result.err(
                        MCPToolError(
                            f"Execution not found: {execution_id}",
                            tool_name="ouroboros_cancel_execution",
                        )
                    )
                result = await self._session_repo.reconstruct_session(session_id)
                if result.is_err:
                    return Result.err(
                        MCPToolError(
                            f"Execution not found: {result.error.message}",
                            tool_name="ouroboros_cancel_execution",
                        )
                    )

            tracker = result.value

            # Check if already in a terminal state
            if tracker.status in self.TERMINAL_STATUSES:
                return Result.err(
                    MCPToolError(
                        f"Execution {execution_id} is already in terminal state: "
                        f"{tracker.status.value}. Cannot cancel.",
                        tool_name="ouroboros_cancel_execution",
                    )
                )

            # Perform cancellation
            cancel_result = await self._session_repo.mark_cancelled(
                session_id=tracker.session_id,
                reason=reason,
                cancelled_by="mcp_tool",
            )

            if cancel_result.is_err:
                cancel_error = cancel_result.error
                return Result.err(
                    MCPToolError(
                        f"Failed to cancel execution: {cancel_error.message}",
                        tool_name="ouroboros_cancel_execution",
                    )
                )

            status_text = (
                f"Execution {execution_id} has been cancelled.\n"
                f"Previous status: {tracker.status.value}\n"
                f"Reason: {reason}\n"
            )

            return Result.ok(
                MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=status_text),),
                    is_error=False,
                    meta={
                        "execution_id": execution_id,
                        "previous_status": tracker.status.value,
                        "new_status": SessionStatus.CANCELLED.value,
                        "reason": reason,
                        "cancelled_by": "mcp_tool",
                    },
                )
            )
        except Exception as e:
            log.error(
                "mcp.tool.cancel_execution.error",
                execution_id=execution_id,
                error=str(e),
            )
            return Result.err(
                MCPToolError(
                    f"Failed to cancel execution: {e}",
                    tool_name="ouroboros_cancel_execution",
                )
            )


_EMPTY_PROGRESS: dict[str, Any] = {
    "ac_completed": None,
    "ac_total": None,
    "current_phase": None,
    "activity": None,
}

_render_cache: dict[tuple[str, int], tuple[str, dict[str, Any]]] = {}
_RENDER_CACHE_MAX = 64


async def _render_job_snapshot(
    snapshot: JobSnapshot, event_store: EventStore
) -> tuple[str, dict[str, Any]]:
    """Format a user-facing job summary with linked execution context.

    Returns (text, progress_dict).  The progress dict contains structured
    AC progress (ac_completed, ac_total, current_phase, activity) extracted
    from the same query used for rendering — no duplicate event-store hit.

    Results are cached by (job_id, cursor) only when the render does not read
    directly from execution events. Execution-linked snapshots can change even
    when the job aggregate cursor does not, so those renders must stay live.
    """
    cache_key = (snapshot.job_id, snapshot.cursor)
    cacheable = not snapshot.is_terminal and snapshot.links.execution_id is None
    if cacheable and cache_key in _render_cache:
        return _render_cache[cache_key]

    text, progress = await _render_job_snapshot_inner(snapshot, event_store)

    if cacheable:
        if len(_render_cache) >= _RENDER_CACHE_MAX:
            to_remove = list(_render_cache.keys())[: _RENDER_CACHE_MAX // 2]
            for key in to_remove:
                _render_cache.pop(key, None)
        _render_cache[cache_key] = (text, progress)

    return text, progress


def _render_compact_job_snapshot(
    snapshot: JobSnapshot,
    progress: dict[str, Any],
    *,
    include_message: bool,
) -> str:
    """Render a low-token job monitor summary."""
    parts = [snapshot.job_id, snapshot.status.value]

    phase = progress.get("current_phase")
    if phase:
        parts.append(str(phase))

    ac_completed = progress.get("ac_completed")
    ac_total = progress.get("ac_total")
    if ac_completed is not None or ac_total is not None:
        parts.append(f"AC {ac_completed if ac_completed is not None else '?'}/{ac_total or '?'}")

    sub_ac_completed = progress.get("sub_ac_completed")
    sub_ac_total = progress.get("sub_ac_total")
    if sub_ac_completed is not None and sub_ac_total:
        parts.append(f"Sub-AC {sub_ac_completed}/{sub_ac_total}")

    parts.append(f"cursor {snapshot.cursor}")
    lines = [" | ".join(parts)]
    if include_message and snapshot.message:
        lines.append(snapshot.message)
    return "\n".join(lines)


async def _render_job_snapshot_inner(
    snapshot: JobSnapshot, event_store: EventStore
) -> tuple[str, dict[str, Any]]:
    """Inner render without caching.  Returns (text, progress_dict)."""
    progress = dict(_EMPTY_PROGRESS)
    status_category = _job_status_category(snapshot)
    lines = [
        f"## Job: {snapshot.job_id}",
        "",
        f"**Type**: {snapshot.job_type}",
        f"**Status**: {snapshot.status.value}",
        f"**Terminal**: {str(snapshot.is_terminal).lower()}",
        f"**Status Category**: {status_category}",
        f"**Message**: {snapshot.message}",
        f"**Cursor**: {snapshot.cursor}",
    ]
    if snapshot.job_type != "auto":
        lines[7:7] = [
            f"**Created**: {snapshot.created_at.isoformat()}",
            f"**Updated**: {snapshot.updated_at.isoformat()}",
        ]
    if snapshot.job_type == "auto" and not snapshot.is_terminal:
        lines.insert(6, "**Tracking**: detached auto tracked background work")

    link_lines = _render_job_link_lines(snapshot)
    if link_lines:
        lines.extend(["", "### Links", *link_lines])

    if snapshot.links.execution_id:
        workflow_event = await _query_latest_workflow_event(
            event_store, snapshot.links.execution_id
        )

        subtask_events = await _query_all_execution_subtask_events(
            event_store, snapshot.links.execution_id
        )
        subtask_summary = summarize_subtask_events(subtask_events)
        subtask_progress = format_subtask_progress_summary(subtask_summary)
        if subtask_summary:
            progress.update(
                {
                    "current_phase": "Sub-AC work",
                    "activity": subtask_progress or "running",
                    "sub_ac_completed": subtask_summary.get("completed_count"),
                    "sub_ac_total": subtask_summary.get("total_count"),
                    "sub_ac_executing": subtask_summary.get("executing_count"),
                    "sub_ac_pending": subtask_summary.get("pending_count"),
                    "sub_ac_failed": subtask_summary.get("failed_count"),
                }
            )
        if workflow_event is not None:
            data = workflow_event.data
            progress.update(
                {
                    "ac_completed": data.get("completed_count"),
                    "ac_total": data.get("total_count"),
                    "current_phase": data.get("current_phase") or "Working",
                    "activity": data.get("activity_detail") or data.get("activity") or "running",
                }
            )
        if workflow_event is not None or subtask_summary:
            lines.extend(
                [
                    "",
                    "### Execution",
                    f"**Execution ID**: {snapshot.links.execution_id}",
                    f"**Phase**: {progress['current_phase']}",
                    f"**Activity**: {progress['activity']}",
                ]
            )
            if workflow_event is not None:
                lines.append(
                    f"**AC Progress**: {progress['ac_completed']}/{progress['ac_total'] or '?'}"
                )
            if subtask_progress:
                lines.append(f"**Sub-AC Progress**: {subtask_progress}")

        subtasks: dict[str, tuple[str, str]] = {}
        for event in reversed(subtask_events[-_JOB_EXECUTION_EVENT_LIMIT:]):
            sub_task_id = event.data.get("sub_task_id")
            if sub_task_id and sub_task_id not in subtasks:
                subtasks[sub_task_id] = (
                    event.data.get("content", ""),
                    event.data.get("status", "unknown"),
                )
            if len(subtasks) >= 5:
                break

        if subtasks:
            lines.append("")
            lines.append("### Recent Subtasks")
            for sub_task_id, (content, status) in list(subtasks.items())[:3]:
                lines.append(f"- `{sub_task_id}`: {status} -- {content}")

    elif snapshot.links.session_id:
        repo = SessionRepository(event_store)
        session_result = await repo.reconstruct_session(snapshot.links.session_id)
        if session_result.is_ok:
            tracker = session_result.value
            lines.extend(
                [
                    "",
                    "### Session",
                    f"**Session ID**: {tracker.session_id}",
                    f"**Session Status**: {tracker.status.value}",
                    f"**Messages Processed**: {tracker.messages_processed}",
                ]
            )

    if snapshot.links.lineage_id:
        events = await event_store.query_events(
            aggregate_id=snapshot.links.lineage_id,
            limit=10,
        )
        latest = next((e for e in events if e.type.startswith("lineage.")), None)
        if latest is not None:
            lines.extend(
                [
                    "",
                    "### Lineage",
                    f"**Lineage ID**: {snapshot.links.lineage_id}",
                ]
            )
            if latest.type == "lineage.generation.started":
                lines.append(
                    f"**Current Step**: Gen {latest.data.get('generation_number')} {latest.data.get('phase')}"
                )
            elif latest.type == "lineage.generation.completed":
                lines.append(
                    f"**Current Step**: Gen {latest.data.get('generation_number')} completed"
                )
            elif latest.type == "lineage.generation.failed":
                lines.append(
                    f"**Current Step**: Gen {latest.data.get('generation_number')} failed at {latest.data.get('phase')}"
                )
            elif latest.type == "lineage.generation.watchdog_decision":
                lines.append(
                    f"**Current Step**: Gen {latest.data.get('generation_number')} watchdog {latest.data.get('action', 'decision')}"
                )
                if latest.data.get("reason"):
                    lines.append(f"**Reason**: {latest.data.get('reason')}")
            elif latest.type in {"lineage.converged", "lineage.stagnated", "lineage.exhausted"}:
                lines.append(f"**Current Step**: {latest.type.split('.', 1)[1]}")
                if latest.data.get("reason"):
                    lines.append(f"**Reason**: {latest.data.get('reason')}")

    if snapshot.result_text and snapshot.is_terminal:
        lines.extend(
            [
                "",
                "### Result",
                "Use `ouroboros_job_result` to fetch the full terminal output.",
            ]
        )

    if snapshot.error:
        lines.extend(["", f"**Error**: {snapshot.error}"])

    return "\n".join(lines), progress


def _render_job_link_lines(snapshot: JobSnapshot) -> list[str]:
    """Return stable job cross-reference lines for every linked job surface.

    Some long-running flows, notably ``ouroboros_start_auto``, use
    ``links.session_id`` as their durable resume handle without necessarily
    having a matching orchestrator session row. Render the raw links before
    optional rich session/execution/lineage sections so callers always see the
    identifiers they need to poll, resume, or diagnose the job.
    """

    lines: list[str] = []
    if snapshot.links.session_id:
        lines.append(f"**Session ID**: {snapshot.links.session_id}")
    if snapshot.links.execution_id:
        lines.append(f"**Execution ID**: {snapshot.links.execution_id}")
    if snapshot.links.lineage_id:
        lines.append(f"**Lineage ID**: {snapshot.links.lineage_id}")
    return lines


def _render_non_terminal_result_unavailable(snapshot: JobSnapshot) -> str:
    """Return stable guidance when result retrieval is requested too early."""
    lines = [
        f"Job result not ready: {snapshot.job_id}",
        f"status={snapshot.status.value}",
        f"terminal={str(snapshot.is_terminal).lower()}",
    ]
    if snapshot.job_type == "auto":
        lines.append("detached auto job is still tracked background work")
    lines.extend(
        [
            f"wait: ouroboros job wait {snapshot.job_id}",
            f"retrieve after terminal status: ouroboros job result {snapshot.job_id}",
            f'mcp_wait: ouroboros_job_wait(job_id="{snapshot.job_id}")',
            f'mcp_result: ouroboros_job_result(job_id="{snapshot.job_id}")',
        ]
    )
    return "\n".join(lines)


def _job_status_category(snapshot: JobSnapshot) -> str:
    """Return the stable category for status consumers."""
    return "terminal" if snapshot.is_terminal else "non_terminal"


# Terminal job statuses whose outcome must surface as an error. INTERRUPTED is
# terminal (see ``JobSnapshot.is_terminal``) and is persisted when a child tool
# returns ``meta.status="interrupted"`` with ``is_error=True``, so result/status
# surfaces must treat it as a failed outcome rather than a successful result.
_TERMINAL_ERROR_STATUSES = frozenset({JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.INTERRUPTED})


def _job_is_error(snapshot: JobSnapshot) -> bool:
    """Return true when a job's terminal outcome must be presented as an error."""
    if snapshot.status in _TERMINAL_ERROR_STATUSES:
        return True
    payload = snapshot.result_payload
    return bool(payload.get("is_error")) if isinstance(payload, dict) else False


def _job_snapshot_meta(snapshot: JobSnapshot) -> dict[str, Any]:
    """Return stable structured metadata shared by job status APIs."""
    links = {
        "session_id": snapshot.links.session_id,
        "execution_id": snapshot.links.execution_id,
        "lineage_id": snapshot.links.lineage_id,
    }
    return {
        "job_id": snapshot.job_id,
        "job_type": snapshot.job_type,
        "status": snapshot.status.value,
        "lifecycle_status": snapshot.status.value,
        "status_category": _job_status_category(snapshot),
        "is_terminal": snapshot.is_terminal,
        "result_available": snapshot.is_terminal,
        "error": snapshot.error,
        "cursor": snapshot.cursor,
        "links": links,
        "session_id": snapshot.links.session_id,
        "execution_id": snapshot.links.execution_id,
        "lineage_id": snapshot.links.lineage_id,
    }


@dataclass
class JobStatusHandler:
    """Return a human-readable status summary for a background job."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_job_status",
            description="Get the latest summary for a background Ouroboros job.",
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
                MCPToolParameter(
                    name="view",
                    type=ToolInputType.STRING,
                    description="'full' (default), 'summary', or 'compact'.",
                    required=False,
                    default=_DEFAULT_JOB_VIEW,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_job_status",
                )
            )
        view = _normalize_job_view(arguments.get("view"))

        try:
            snapshot = await self._job_manager.get_snapshot(job_id)
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_job_status"))

        text, progress = await _render_job_snapshot(snapshot, self._event_store)
        if view == "compact":
            text = _render_compact_job_snapshot(snapshot, progress, include_message=False)
        elif view == "summary":
            text = _render_compact_job_snapshot(snapshot, progress, include_message=True)

        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=_job_is_error(snapshot),
                meta={
                    **_job_snapshot_meta(snapshot),
                    "view": view,
                    **progress,
                },
            )
        )


@dataclass
class JobWaitHandler:
    """Long-poll for the next background job update."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_job_wait",
            description=(
                "Wait briefly for a background job to change state. "
                "Useful for conversational polling after a start command."
            ),
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
                MCPToolParameter(
                    name="cursor",
                    type=ToolInputType.INTEGER,
                    description="Previous cursor from job_status or job_wait",
                    required=False,
                    default=0,
                ),
                MCPToolParameter(
                    name="timeout_seconds",
                    type=ToolInputType.INTEGER,
                    description=(
                        "Maximum seconds to wait for a change. Defaults to 0 so the "
                        "tool returns an immediate snapshot and never holds the MCP "
                        "client open unless the caller explicitly asks for long-polling."
                    ),
                    required=False,
                    default=0,
                ),
                MCPToolParameter(
                    name="view",
                    type=ToolInputType.STRING,
                    description="'full' (default), 'summary', or 'compact'.",
                    required=False,
                    default=_DEFAULT_JOB_VIEW,
                ),
                MCPToolParameter(
                    name="stream",
                    type=ToolInputType.STRING,
                    description=(
                        "'progress' (default) watches job/execution progress; "
                        "'linked' also streams linked session, lineage, and subagent events."
                    ),
                    required=False,
                    default="progress",
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_job_wait",
                )
            )

        cursor_result = _parse_non_negative_int_argument(
            arguments,
            "cursor",
            default=0,
            tool_name="ouroboros_job_wait",
        )
        if cursor_result.is_err:
            return Result.err(cursor_result.error)
        timeout_result = _parse_non_negative_int_argument(
            arguments,
            "timeout_seconds",
            default=0,
            tool_name="ouroboros_job_wait",
        )
        if timeout_result.is_err:
            return Result.err(timeout_result.error)
        cursor = cursor_result.value
        timeout_seconds = timeout_result.value
        view = _normalize_job_view(arguments.get("view"))
        stream = _normalize_job_stream(arguments.get("stream"))

        try:
            if stream == "linked":
                (
                    snapshot,
                    changed,
                    stream_events,
                    stream_cursor,
                ) = await self._wait_for_linked_change(
                    job_id,
                    cursor=cursor,
                    timeout_seconds=timeout_seconds,
                )
            else:
                snapshot, changed = await self._job_manager.wait_for_change(
                    job_id,
                    cursor=cursor,
                    timeout_seconds=timeout_seconds,
                )
                stream_events = []
                stream_cursor = cursor
        except ValueError as exc:
            return Result.err(
                MCPToolError(
                    (
                        f"Job not found: {job_id}. Wait unavailable. "
                        "Check the job handle returned by detached auto start, "
                        f"then retry `ouroboros job wait {job_id}`."
                    ),
                    tool_name="ouroboros_job_wait",
                    error_code="job_handle_not_found",
                    details={
                        "job_id": job_id,
                        "lifecycle_status": "invalid",
                        "is_terminal": False,
                        "result_available": False,
                        "reason": "not_found",
                        "source_error": str(exc),
                    },
                )
            )

        execution_progress_changed = False
        response_cursor = max(snapshot.cursor, stream_cursor, cursor)
        if snapshot.links.execution_id:
            execution_events, execution_cursor = await self._event_store.get_events_after(
                "execution",
                snapshot.links.execution_id,
                cursor,
            )
            execution_progress_changed = any(
                event.type in _JOB_PROGRESS_EVENT_TYPES for event in execution_events
            )
            response_cursor = max(response_cursor, execution_cursor)
            if response_cursor != snapshot.cursor:
                snapshot = replace(snapshot, cursor=response_cursor)

        text, progress = await _render_job_snapshot(snapshot, self._event_store)
        stream_items = [_event_stream_item(event) for event in stream_events]
        if stream_items:
            text += "\n\n### Stream Events"
            for item in stream_items[-_JOB_STREAM_EVENT_LIMIT:]:
                detail = f" -- {item['detail']}" if item["detail"] else ""
                text += f"\n- `{item['type']}` [{item['scope']}]{detail}"
        has_live_execution_progress = snapshot.links.execution_id is not None and any(
            progress.get(key) is not None
            for key in (
                "ac_completed",
                "ac_total",
                "current_phase",
                "sub_ac_completed",
                "sub_ac_total",
            )
        )
        stream_changed = bool(stream_items)
        response_changed = changed or execution_progress_changed or stream_changed
        if not changed:
            if (
                view in {"compact", "summary"}
                and has_live_execution_progress
                and (execution_progress_changed or stream_changed)
            ):
                text = _render_compact_job_snapshot(
                    snapshot,
                    progress,
                    include_message=view == "summary",
                )
                if stream_items:
                    text += f"\nstream_events {len(stream_items)} cursor={snapshot.cursor}"
            elif view in {"compact", "summary"}:
                if stream_items:
                    summaries = ", ".join(item["type"] for item in stream_items[-3:])
                    text = (
                        f"stream_events {len(stream_items)} cursor={snapshot.cursor}: {summaries}"
                    )
                else:
                    text = f"unchanged cursor={snapshot.cursor}"
            elif execution_progress_changed:
                text += "\n\nExecution progress updated during this wait window."
            elif stream_changed:
                text += "\n\nLinked stream events updated during this wait window."
            else:
                text += "\n\nNo new job-level events during this wait window."
        elif view == "compact":
            text = _render_compact_job_snapshot(snapshot, progress, include_message=False)
            if stream_items:
                text += f"\nstream_events {len(stream_items)} cursor={snapshot.cursor}"
        elif view == "summary":
            text = _render_compact_job_snapshot(snapshot, progress, include_message=True)
            if stream_items:
                text += f"\nstream_events {len(stream_items)} cursor={snapshot.cursor}"
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=_job_is_error(snapshot),
                meta={
                    **_job_snapshot_meta(snapshot),
                    "changed": response_changed,
                    "view": view,
                    "stream": stream,
                    "stream_events": stream_items,
                    **progress,
                },
            )
        )

    async def _wait_for_linked_change(
        self,
        job_id: str,
        *,
        cursor: int,
        timeout_seconds: int,
    ) -> tuple[JobSnapshot, bool, list[BaseEvent], int]:
        """Long-poll job plus linked child/session event streams."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while True:
            snapshot, changed = await self._job_manager.wait_for_change(
                job_id,
                cursor=cursor,
                timeout_seconds=0,
            )
            events, stream_cursor = await _query_linked_stream_events(
                self._event_store,
                snapshot,
                cursor,
            )
            if (
                changed
                or events
                or snapshot.is_terminal
                or asyncio.get_running_loop().time() >= deadline
            ):
                if stream_cursor != snapshot.cursor:
                    snapshot = replace(snapshot, cursor=max(snapshot.cursor, stream_cursor))
                return snapshot, changed, events, stream_cursor
            await asyncio.sleep(0.5)


@dataclass
class JobResultHandler:
    """Fetch the terminal output for a background job."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_job_result",
            description="Get the final output for a completed background job.",
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_job_result",
                )
            )

        try:
            snapshot = await self._job_manager.get_snapshot(job_id)
        except ValueError as exc:
            return Result.err(
                MCPToolError(
                    f"Job handle not found: {job_id}. Result unavailable.",
                    tool_name="ouroboros_job_result",
                    error_code="job_handle_not_found",
                    details={
                        "job_id": job_id,
                        "lifecycle_status": "invalid",
                        "is_terminal": True,
                        "result_available": False,
                        "reason": "not_found",
                        "source_error": str(exc),
                    },
                )
            )

        if is_terminal_job_expired(snapshot):
            return Result.err(
                MCPToolError(
                    f"Job handle expired: {snapshot.job_id}. Result unavailable.",
                    tool_name="ouroboros_job_result",
                    error_code="job_handle_expired",
                    details={
                        "job_id": snapshot.job_id,
                        "lifecycle_status": "expired",
                        "is_terminal": snapshot.is_terminal,
                        "result_available": False,
                        "reason": "expired",
                    },
                )
            )

        if not snapshot.is_terminal:
            return Result.ok(
                MCPToolResult(
                    content=(
                        MCPContentItem(
                            type=ContentType.TEXT,
                            text=_render_non_terminal_result_unavailable(snapshot),
                        ),
                    ),
                    is_error=False,
                    meta={
                        **_job_snapshot_meta(snapshot),
                        "result_available": False,
                    },
                )
            )

        stored_content = _content_items_from_result_payload(snapshot.result_payload)
        result_text = snapshot.result_text or snapshot.error or snapshot.message
        content = stored_content or (MCPContentItem(type=ContentType.TEXT, text=result_text),)
        result_payload_meta = (
            {"result_payload": snapshot.result_payload}
            if snapshot.result_payload is not None
            else {}
        )
        return Result.ok(
            MCPToolResult(
                content=content,
                is_error=_job_is_error(snapshot),
                meta={
                    **_job_snapshot_meta(snapshot),
                    **result_payload_meta,
                    **snapshot.result_meta,
                },
            )
        )


@dataclass
class CancelJobHandler:
    """Cancel a background job."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._event_store = self.event_store or EventStore()
        self._job_manager = self.job_manager or JobManager(self._event_store)

    @property
    def definition(self) -> MCPToolDefinition:
        return MCPToolDefinition(
            name="ouroboros_cancel_job",
            description="Request cancellation for a background job.",
            parameters=(
                MCPToolParameter(
                    name="job_id",
                    type=ToolInputType.STRING,
                    description="Job ID returned by a start tool",
                    required=True,
                ),
            ),
        )

    async def handle(
        self,
        arguments: dict[str, Any],
    ) -> Result[MCPToolResult, MCPServerError]:
        job_id = arguments.get("job_id")
        if not job_id:
            return Result.err(
                MCPToolError(
                    "job_id is required",
                    tool_name="ouroboros_cancel_job",
                )
            )

        try:
            snapshot = await self._job_manager.cancel_job(job_id)
        except ValueError as exc:
            return Result.err(MCPToolError(str(exc), tool_name="ouroboros_cancel_job"))

        text, _progress = await _render_job_snapshot(snapshot, self._event_store)
        return Result.ok(
            MCPToolResult(
                content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                is_error=False,
                meta={
                    "job_id": snapshot.job_id,
                    "status": snapshot.status.value,
                    "cursor": snapshot.cursor,
                },
            )
        )
