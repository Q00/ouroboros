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
import os
from typing import Any

import structlog

from ouroboros.core.types import Result
from ouroboros.events.base import BaseEvent
from ouroboros.mcp.errors import MCPServerError, MCPToolError
from ouroboros.mcp.job_manager import (
    JobManager,
    JobSnapshot,
    JobStatus,
)
from ouroboros.mcp.tools.ac_tree_hud_handler import (
    format_subtask_progress_summary,
    summarize_subtask_events,
)
from ouroboros.mcp.tools.attention_relay import (
    RELAY_SOURCE_EVENT_TYPES,
    classify_relay_events,
)
from ouroboros.mcp.types import (
    ContentType,
    MCPContentItem,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    ToolInputType,
)
from ouroboros.orchestrator.execution_authority import (
    _abort_process_local_authority_terminalization,
    _begin_process_local_authority_terminalization,
    _retire_process_local_authority_after_terminal_persistence,
    valid_process_local_authority_contract,
)
from ouroboros.orchestrator.heartbeat import is_holder_alive
from ouroboros.orchestrator.runner import clear_cancellation, request_cancellation
from ouroboros.orchestrator.session import SessionRepository, SessionStatus
from ouroboros.persistence.event_store import EventStore

log = structlog.get_logger(__name__)

_DEFAULT_JOB_VIEW = "full"
_MAX_JOB_WAIT_TIMEOUT_SECONDS = 5
_JOB_WAIT_GUARD_VERSION = "detached-branch-timeout-v2"
_JOB_WAIT_RESPONSE_GRACE_SECONDS = 1.0
_JOB_WAIT_EXECUTION_SCAN_TIMEOUT_SECONDS = 1.0
_JOB_WAIT_RENDER_TIMEOUT_SECONDS = 2.0
_JOB_EXECUTION_EVENT_LIMIT = 250
_JOB_SUBTASK_EVENT_PAGE_SIZE = 500
_JOB_PROGRESS_EVENT_TYPES = {
    "workflow.progress.updated",
    "execution.node.created",
    "execution.node.updated",
    "execution.subtask.updated",
}
_JOB_ATTENTION_EVENT_TYPES = RELAY_SOURCE_EVENT_TYPES
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
_JOB_WAIT_FOR_ALIASES = {
    "default": "raw",
    "raw": "raw",
    "any": "raw",
    "change": "raw",
    "event": "raw",
    "events": "raw",
    "ac": "ac_change",
    "ac_change": "ac_change",
    "progress": "ac_change",
    "meaningful": "ac_change",
    "attention": "attention_or_ac_change",
    "attention_or_ac_change": "attention_or_ac_change",
    "phase": "phase_change",
    "phase_change": "phase_change",
    "terminal": "terminal",
    "done": "terminal",
    "completion": "terminal",
}


def _get_cached_job_snapshot(job_manager: Any, job_id: str) -> JobSnapshot | None:
    getter = getattr(job_manager, "get_cached_snapshot", None)
    if getter is None:
        return None
    snapshot = getter(job_id)
    return snapshot if isinstance(snapshot, JobSnapshot) else None


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


def _normalize_job_wait_for(value: object) -> str:
    """Normalize requested job wait wakeup significance."""
    if isinstance(value, str):
        stripped = value.strip().lower()
        if stripped:
            return _JOB_WAIT_FOR_ALIASES.get(stripped, "raw")
    return "raw"


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


def _job_wait_internal_timeout_result(
    *,
    job_id: str,
    cursor: int,
    timeout_seconds: int,
    requested_timeout_seconds: int,
    view: str,
    stream: str,
    wait_for: str,
) -> MCPToolResult:
    text = (
        f"job_wait timed out before producing a snapshot for `{job_id}`. "
        "Poll `ouroboros_job_status` or call `ouroboros_job_wait` again."
    )
    return MCPToolResult(
        content=(MCPContentItem(type=ContentType.TEXT, text=text),),
        is_error=False,
        meta={
            "job_id": job_id,
            "cursor": cursor,
            "changed": False,
            "view": view,
            "stream": stream,
            "wait_for": wait_for,
            "timeout_seconds": timeout_seconds,
            "timeout_seconds_requested": requested_timeout_seconds,
            "timeout_seconds_capped": timeout_seconds != requested_timeout_seconds,
            "wait_timed_out": True,
            "result_available": False,
        },
    )


def _consume_detached_wait_task(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.warning("mcp.tool.job_wait.detached_wait_failed", exc_info=True)


async def _await_job_wait_branch(
    awaitable: Any,
    *,
    timeout: float,
    job_id: str,
    branch: str,
) -> Any:
    """Await a job wait branch without waiting for slow cancellation cleanup."""
    log.debug(
        "mcp.tool.job_wait.guard.enter",
        job_id=job_id,
        branch=branch,
        timeout_seconds=timeout,
        guard_version=_JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    task = asyncio.create_task(awaitable)
    log.debug(
        "mcp.tool.job_wait.guard.task_created",
        job_id=job_id,
        branch=branch,
        guard_version=_JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    try:
        done, _pending = await asyncio.wait({task}, timeout=timeout)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_detached_wait_task)
        raise
    log.debug(
        "mcp.tool.job_wait.guard.wait_returned",
        job_id=job_id,
        branch=branch,
        done=task in done,
        guard_version=_JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    if task in done:
        return task.result()

    task.cancel()
    task.add_done_callback(_consume_detached_wait_task)
    log.debug(
        "mcp.tool.job_wait.wait.branch_timeout",
        job_id=job_id,
        branch=branch,
        timeout_seconds=timeout,
        guard_version=_JOB_WAIT_GUARD_VERSION,
        pid=os.getpid(),
    )
    raise TimeoutError


async def _query_all_execution_subtask_events(
    event_store: EventStore, execution_id: str
) -> list[Any]:
    """Fetch a bounded recent subtask snapshot for live job rendering.

    ``job_wait`` is called from MCP clients with relatively short tool-call
    ceilings. Rendering progress must not materialize an execution aggregate's
    full event history on every poll; the authoritative terminal result remains
    available through ``job_result``.
    """
    events = await event_store.query_events(
        aggregate_id=execution_id,
        limit=_JOB_SUBTASK_EVENT_PAGE_SIZE,
    )
    events = sorted(events, key=lambda event: (event.timestamp, event.id))
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


async def _query_recent_execution_progress_events(
    event_store: EventStore,
    execution_id: str,
    *,
    cursor: int | None = None,
) -> tuple[list[BaseEvent], int]:
    """Fetch a bounded latest progress window with durable rowid cursor semantics."""
    getter = getattr(event_store, "get_recent_aggregate_events", None)
    if getter is not None:
        return await getter(
            "execution",
            execution_id,
            event_types=_JOB_PROGRESS_EVENT_TYPES,
            max_row_id=cursor,
            limit=_JOB_SUBTASK_EVENT_PAGE_SIZE,
        )
    return await event_store.get_events_after(
        "execution",
        execution_id,
        0,
        max_row_id=cursor,
        limit=_JOB_SUBTASK_EVENT_PAGE_SIZE,
    )


async def _query_execution_progress_at_cursor(
    event_store: EventStore,
    execution_id: str,
    *,
    cursor: int | None = None,
) -> tuple[dict[str, Any], int]:
    """Return execution progress visible at ``cursor`` and the latest rowid read."""
    events, event_cursor = await _query_recent_execution_progress_events(
        event_store,
        execution_id,
        cursor=cursor,
    )
    progress = dict(_EMPTY_PROGRESS)
    workflow_event = next(
        (event for event in reversed(events) if event.type == "workflow.progress.updated"),
        None,
    )
    subtask_events = [
        event
        for event in events
        if event.type
        in {
            "execution.node.created",
            "execution.node.updated",
            "execution.subtask.updated",
        }
    ]
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
    return progress, event_cursor


def _job_wait_progress_changed(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    wait_for: str,
) -> bool:
    """Return true when progress satisfies a filtered job_wait wakeup."""
    if wait_for == "phase_change":
        return before.get("current_phase") != after.get("current_phase")
    if wait_for in {"ac_change", "attention_or_ac_change"}:
        return any(
            before.get(key) != after.get(key)
            for key in ("ac_completed", "sub_ac_completed", "current_phase")
        )
    return False


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
        "detail",
        "state",
        "effective_mode",
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


def _compact_stream_suffix(
    stream_items: list[dict[str, Any]],
    cursor: int,
    *,
    has_more: bool = False,
) -> str:
    """One-line linked-stream summary appended to compact/summary job views.

    Empty when no linked events were streamed, so callers can unconditionally
    concatenate the result without re-checking ``stream_items``. ``has_more``
    appends a ``more=1`` marker signalling the caller should poll again with the
    returned cursor to drain the rest of a bounded linked-stream page.
    """
    if not stream_items:
        return ""
    suffix = f"\nstream_events {len(stream_items)} cursor={cursor}"
    if has_more:
        suffix += " more=1"
    return suffix


async def _query_linked_stream_events(
    event_store: EventStore,
    snapshot: JobSnapshot,
    cursor: int,
    *,
    limit: int = _JOB_STREAM_EVENT_LIMIT,
) -> tuple[list[BaseEvent], int, bool]:
    """Fetch a bounded page of new events from the job's linked streams.

    Linked streams (job/execution/session/lineage) share one rowid cursor, so
    the page is delivered as a single global rowid window ``(cursor, boundary]``:

    1. Peek each stream with a rowid-ordered ``limit`` (bounded reads, so a stale
       or first ``stream="linked"`` poll over a long-running auto/session cannot
       materialize an unbounded query/response). A stream that returns a full
       page may have more rows past it.
    2. Clamp ``boundary`` to the lowest saturated stream cursor — the highest
       rowid below which *every* stream's knowledge is complete — then re-read
       each stream only up to ``boundary``.

    Because rowid order is the cursor dimension, ``boundary`` is skip-safe even
    when timestamp order diverges from insertion order, and no event past
    ``boundary`` is returned, so the follow-up poll (cursor=boundary) never
    re-delivers or skips an event. ``has_more`` is True when a stream still has
    rows beyond ``boundary``. The page is bounded by ``links × limit`` events.
    """

    async def gather(
        page_limit: int | None, max_row_id: int | None
    ) -> list[tuple[list[BaseEvent], int]]:
        """Read every linked stream with a shared (limit, max_row_id) contract."""
        results: list[tuple[list[BaseEvent], int]] = [
            await event_store.get_events_after(
                "job", snapshot.job_id, cursor, limit=page_limit, max_row_id=max_row_id
            )
        ]
        if snapshot.links.execution_id:
            results.append(
                await event_store.get_events_after(
                    "execution",
                    snapshot.links.execution_id,
                    cursor,
                    limit=page_limit,
                    max_row_id=max_row_id,
                )
            )
        if snapshot.links.session_id:
            results.append(
                await event_store.query_session_related_events_after(
                    session_id=snapshot.links.session_id,
                    execution_id=snapshot.links.execution_id,
                    last_row_id=cursor,
                    limit=page_limit,
                    max_row_id=max_row_id,
                )
            )
        if snapshot.links.lineage_id:
            results.append(
                await event_store.get_events_after(
                    "lineage",
                    snapshot.links.lineage_id,
                    cursor,
                    limit=page_limit,
                    max_row_id=max_row_id,
                )
            )
        return results

    def merge(results: list[tuple[list[BaseEvent], int]]) -> list[BaseEvent]:
        collected: dict[str, BaseEvent] = {}
        for events, _new_cursor in results:
            for event in events:
                collected[event.id] = event
        return sorted(collected.values(), key=lambda event: (event.timestamp, event.id))

    # Pass 1: bounded rowid-ordered peek to discover the global page boundary.
    peeked = await gather(limit, None)
    saturated = [new_cursor for events, new_cursor in peeked if len(events) >= limit]

    if not saturated:
        # Every stream is fully drained to the global head; the peek is complete.
        next_cursor = max([cursor, *(new_cursor for _events, new_cursor in peeked)])
        return merge(peeked), next_cursor, False

    # Pass 2: clamp to the lowest saturated boundary and re-read each stream only
    # up to it, yielding a contiguous rowid window with no skips and no rows past
    # the cursor handed back. boundary > cursor (page rowids are all > cursor), so
    # the next poll strictly advances.
    boundary = min(saturated)
    return merge(await gather(None, boundary)), boundary, True


async def _query_linked_history_events(
    event_store: EventStore,
    snapshot: JobSnapshot,
    *,
    after_cursor: int = 0,
    max_row_id: int | None = None,
) -> list[BaseEvent]:
    """Read complete linked history for classification, never for raw relay text."""
    streams: list[tuple[list[BaseEvent], int]] = [
        await event_store.get_events_after(
            "job",
            snapshot.job_id,
            after_cursor,
            limit=None,
            max_row_id=max_row_id,
        )
    ]
    if snapshot.links.execution_id:
        streams.append(
            await event_store.get_events_after(
                "execution",
                snapshot.links.execution_id,
                after_cursor,
                limit=None,
                max_row_id=max_row_id,
            )
        )
    if snapshot.links.session_id:
        streams.append(
            await event_store.query_session_related_events_after(
                session_id=snapshot.links.session_id,
                execution_id=snapshot.links.execution_id,
                last_row_id=after_cursor,
                limit=None,
                max_row_id=max_row_id,
            )
        )
    if snapshot.links.lineage_id:
        streams.append(
            await event_store.get_events_after(
                "lineage",
                snapshot.links.lineage_id,
                after_cursor,
                limit=None,
                max_row_id=max_row_id,
            )
        )
    unique: dict[str, BaseEvent] = {}
    for events, _cursor in streams:
        for event in events:
            unique[event.id] = event
    return sorted(unique.values(), key=lambda event: (event.timestamp, event.id))


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

            raw_contract = tracker.progress.get("execution_contract")
            authority = (
                raw_contract.get("foundation_a_authority")
                if isinstance(raw_contract, dict)
                else None
            )
            terminalization_started, authority_claimed = (
                _begin_process_local_authority_terminalization(
                    tracker.session_id,
                    tracker.execution_id,
                    authority,
                )
            )
            if authority_claimed:
                # Do not write a terminal record underneath a live worker. It
                # will observe this request, persist cancellation, and retire
                # its exact authority in one lifecycle transition.
                await request_cancellation(tracker.session_id)
                status_text = (
                    f"Execution {execution_id} cancellation has been requested.\n"
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
                            "new_status": "cancellation_requested",
                            "reason": reason,
                            "cancelled_by": "mcp_tool",
                            "in_flight": True,
                        },
                    )
                )

            if (
                not terminalization_started
                and valid_process_local_authority_contract(authority)
                and is_holder_alive(tracker.session_id)
            ):
                # The registry and cancellation signal are process-local. A
                # different live process cannot receive this request, so
                # terminalizing its durable session here would split terminal
                # state from active effects. Fail retryably instead.
                return Result.err(
                    MCPToolError(
                        "This process-local execution is held by another live owner; "
                        "cancel it through that owner.",
                        tool_name="ouroboros_cancel_execution",
                        is_retriable=True,
                        details={
                            "session_id": tracker.session_id,
                            "execution_id": tracker.execution_id,
                            "resume_blocked": "process_local_authority_held_elsewhere",
                        },
                    )
                )

            # Reserve first, then publish the cooperative request.  A paused
            # runner cannot claim effects while the durable terminal write is
            # in flight; if that write fails, it will observe this request on
            # its next resume and retry cancellation before executing effects.
            cancellation_request_published = False
            try:
                await request_cancellation(tracker.session_id)
                cancellation_request_published = True
                cancel_result = await self._session_repo.mark_cancelled_if_active(
                    session_id=tracker.session_id,
                    reason=reason,
                    cancelled_by="mcp_tool",
                )
            except BaseException:
                if terminalization_started:
                    _abort_process_local_authority_terminalization(
                        tracker.session_id,
                        tracker.execution_id,
                        authority,
                    )
                elif cancellation_request_published:
                    await clear_cancellation(tracker.session_id)
                raise

            if cancel_result.is_err:
                if terminalization_started:
                    _abort_process_local_authority_terminalization(
                        tracker.session_id,
                        tracker.execution_id,
                        authority,
                    )
                else:
                    await clear_cancellation(tracker.session_id)
                cancel_error = cancel_result.error
                return Result.err(
                    MCPToolError(
                        f"Failed to cancel execution: {cancel_error.message}",
                        tool_name="ouroboros_cancel_execution",
                    )
                )

            if not cancel_result.value:
                # A competing terminal writer won after this handler's initial
                # snapshot.  Preserve that durable result and still drain this
                # process's exact paused owner when our reservation proves it
                # belongs to the same lifecycle.
                (
                    retired,
                    claim_became_active,
                ) = await _retire_process_local_authority_after_terminal_persistence(
                    tracker.session_id,
                    tracker.execution_id,
                    authority,
                )
                if claim_became_active:
                    await request_cancellation(tracker.session_id)
                else:
                    await clear_cancellation(tracker.session_id)
                if retired:
                    log.info(
                        "mcp.tool.cancel_execution.preexisting_terminal_owner_retired",
                        execution_id=tracker.execution_id,
                        session_id=tracker.session_id,
                    )
                return Result.err(
                    MCPToolError(
                        f"Execution {execution_id} became terminal before cancellation completed.",
                        tool_name="ouroboros_cancel_execution",
                    )
                )

            (
                retired,
                claim_became_active,
            ) = await _retire_process_local_authority_after_terminal_persistence(
                tracker.session_id,
                tracker.execution_id,
                authority,
            )
            if claim_became_active:
                # A worker raced from paused/unclaimed to active after the
                # preflight.  The terminal record is already durable; signal
                # it so it performs normal orderly cleanup rather than leaving
                # an owner live indefinitely.
                await request_cancellation(tracker.session_id)
            else:
                await clear_cancellation(tracker.session_id)
            if retired:
                log.info(
                    "mcp.tool.cancel_execution.process_local_owner_retired",
                    execution_id=tracker.execution_id,
                    session_id=tracker.session_id,
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

        cached_snapshot = _get_cached_job_snapshot(self._job_manager, job_id)
        if cached_snapshot is not None:
            snapshot = cached_snapshot
        else:
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
    available_conductor_tools: frozenset[str] = field(default_factory=frozenset, repr=False)

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
                        "client open unless the caller explicitly asks for long-polling. "
                        f"Values above {_MAX_JOB_WAIT_TIMEOUT_SECONDS} are capped so "
                        "clients with MCP tool-call timeouts can poll safely."
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
                MCPToolParameter(
                    name="wait_for",
                    type=ToolInputType.STRING,
                    description=(
                        "'raw' (default) returns on any job event, preserving existing "
                        "behavior; 'ac_change' waits for AC/Sub-AC/phase progress or "
                        "terminal status; 'attention_or_ac_change' additionally wakes "
                        "for Synapse delivery status; 'phase_change' waits for phase transitions "
                        "or terminal status; 'terminal' waits only for terminal status."
                    ),
                    required=False,
                    default="raw",
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
        requested_timeout_seconds = timeout_result.value
        timeout_seconds = min(requested_timeout_seconds, _MAX_JOB_WAIT_TIMEOUT_SECONDS)
        view = _normalize_job_view(arguments.get("view"))
        stream = _normalize_job_stream(arguments.get("stream"))
        wait_for = _normalize_job_wait_for(arguments.get("wait_for"))
        wait_response_timeout = max(
            _JOB_WAIT_RESPONSE_GRACE_SECONDS,
            timeout_seconds + _JOB_WAIT_RESPONSE_GRACE_SECONDS,
        )
        started_at = asyncio.get_running_loop().time()
        log.debug(
            "mcp.tool.job_wait.start",
            job_id=job_id,
            cursor=cursor,
            argument_keys=sorted(arguments),
            timeout_seconds=timeout_seconds,
            timeout_seconds_requested=requested_timeout_seconds,
            timeout_seconds_capped=timeout_seconds != requested_timeout_seconds,
            view=view,
            stream=stream,
            wait_for=wait_for,
            wait_response_timeout=wait_response_timeout,
            guard_version=_JOB_WAIT_GUARD_VERSION,
            pid=os.getpid(),
        )

        if stream == "progress" and timeout_seconds > 0:
            cached_snapshot = _get_cached_job_snapshot(self._job_manager, job_id)
            if cached_snapshot is not None and (
                cached_snapshot.cursor > cursor or cached_snapshot.is_terminal
            ):
                progress = dict(_EMPTY_PROGRESS)
                text = _render_compact_job_snapshot(
                    cached_snapshot,
                    progress,
                    include_message=view == "summary",
                )
                if view == "full":
                    text += (
                        "\n\nLive snapshot returned without long-polling; "
                        f"poll again with cursor={cached_snapshot.cursor}."
                    )
                result = MCPToolResult(
                    content=(MCPContentItem(type=ContentType.TEXT, text=text),),
                    is_error=_job_is_error(cached_snapshot),
                    meta={
                        **_job_snapshot_meta(cached_snapshot),
                        "changed": cached_snapshot.cursor > cursor,
                        "view": view,
                        "stream": stream,
                        "wait_for": wait_for,
                        "timeout_seconds": timeout_seconds,
                        "timeout_seconds_requested": requested_timeout_seconds,
                        "timeout_seconds_capped": timeout_seconds != requested_timeout_seconds,
                        "execution_scan_timed_out": False,
                        "render_timed_out": False,
                        "live_snapshot": True,
                        "stream_events": [],
                        "stream_has_more": False,
                        **progress,
                    },
                )
                log.debug(
                    "mcp.tool.job_wait.live_snapshot.return",
                    job_id=job_id,
                    status=cached_snapshot.status.value,
                    changed=cached_snapshot.cursor > cursor,
                    cursor=cached_snapshot.cursor,
                    requested_cursor=cursor,
                    elapsed_seconds=round(
                        asyncio.get_running_loop().time() - started_at,
                        3,
                    ),
                    guard_version=_JOB_WAIT_GUARD_VERSION,
                    pid=os.getpid(),
                )
                return Result.ok(result)

        try:
            if stream == "linked":
                log.debug(
                    "mcp.tool.job_wait.wait.enter",
                    job_id=job_id,
                    branch="linked",
                    cursor=cursor,
                    timeout_seconds=timeout_seconds,
                    wait_for=wait_for,
                    wait_response_timeout=wait_response_timeout,
                )
                (
                    snapshot,
                    changed,
                    stream_events,
                    stream_cursor,
                    stream_has_more,
                ) = await _await_job_wait_branch(
                    self._wait_for_linked_change(
                        job_id,
                        cursor=cursor,
                        timeout_seconds=timeout_seconds,
                        wait_for=wait_for,
                    ),
                    timeout=wait_response_timeout,
                    job_id=job_id,
                    branch="linked",
                )
                meaningful_execution_changed = False
                wait_branch = "linked"
            elif wait_for != "raw":
                log.debug(
                    "mcp.tool.job_wait.wait.enter",
                    job_id=job_id,
                    branch="meaningful",
                    cursor=cursor,
                    timeout_seconds=timeout_seconds,
                    wait_for=wait_for,
                    wait_response_timeout=wait_response_timeout,
                )
                (
                    snapshot,
                    changed,
                    stream_cursor,
                    meaningful_execution_changed,
                ) = await _await_job_wait_branch(
                    self._wait_for_meaningful_change(
                        job_id,
                        cursor=cursor,
                        timeout_seconds=timeout_seconds,
                        wait_for=wait_for,
                    ),
                    timeout=wait_response_timeout,
                    job_id=job_id,
                    branch="meaningful",
                )
                stream_events = []
                stream_has_more = False
                wait_branch = "meaningful"
            else:
                log.debug(
                    "mcp.tool.job_wait.wait.enter",
                    job_id=job_id,
                    branch="raw",
                    cursor=cursor,
                    timeout_seconds=timeout_seconds,
                    wait_for=wait_for,
                    wait_response_timeout=wait_response_timeout,
                )
                snapshot, changed = await _await_job_wait_branch(
                    self._job_manager.wait_for_change(
                        job_id,
                        cursor=cursor,
                        timeout_seconds=timeout_seconds,
                    ),
                    timeout=wait_response_timeout,
                    job_id=job_id,
                    branch="raw",
                )
                stream_events = []
                stream_cursor = cursor
                stream_has_more = False
                meaningful_execution_changed = False
                wait_branch = "raw"
        except TimeoutError:
            log.debug(
                "mcp.tool.job_wait.wait.timeout",
                job_id=job_id,
                cursor=cursor,
                timeout_seconds=timeout_seconds,
                timeout_seconds_requested=requested_timeout_seconds,
                view=view,
                stream=stream,
                wait_for=wait_for,
                elapsed_seconds=round(asyncio.get_running_loop().time() - started_at, 3),
            )
            return Result.ok(
                _job_wait_internal_timeout_result(
                    job_id=job_id,
                    cursor=cursor,
                    timeout_seconds=timeout_seconds,
                    requested_timeout_seconds=requested_timeout_seconds,
                    view=view,
                    stream=stream,
                    wait_for=wait_for,
                )
            )
        except ValueError as exc:
            log.debug(
                "mcp.tool.job_wait.not_found",
                job_id=job_id,
                cursor=cursor,
                source_error=str(exc),
            )
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

        log.debug(
            "mcp.tool.job_wait.wait.exit",
            job_id=job_id,
            branch=wait_branch,
            changed=changed,
            snapshot_status=snapshot.status.value,
            snapshot_cursor=snapshot.cursor,
            is_terminal=snapshot.is_terminal,
            execution_id=snapshot.links.execution_id,
            elapsed_seconds=round(asyncio.get_running_loop().time() - started_at, 3),
        )

        execution_progress_changed = False
        execution_scan_timed_out = False
        if stream == "linked":
            # Execution events are already part of the bounded linked page, and
            # `_wait_for_linked_change` pinned the cursor to that page boundary.
            # Derive the progress signal from the page itself and never advance
            # the public cursor past the boundary here: a separate execution scan
            # could otherwise publish a rowid above a held-back lower-rowid stream
            # event, skipping it on the next poll.
            execution_progress_changed = any(
                event.type in _JOB_PROGRESS_EVENT_TYPES for event in stream_events
            )
        elif snapshot.links.execution_id:
            response_cursor = max(snapshot.cursor, stream_cursor, cursor)
            try:
                log.debug(
                    "mcp.tool.job_wait.execution_scan.enter",
                    job_id=job_id,
                    execution_id=snapshot.links.execution_id,
                    cursor=cursor,
                    limit=_JOB_EXECUTION_EVENT_LIMIT,
                    timeout_seconds=_JOB_WAIT_EXECUTION_SCAN_TIMEOUT_SECONDS,
                )
                execution_events, execution_cursor = await asyncio.wait_for(
                    self._event_store.get_events_after(
                        "execution",
                        snapshot.links.execution_id,
                        cursor,
                        limit=_JOB_EXECUTION_EVENT_LIMIT,
                    ),
                    timeout=_JOB_WAIT_EXECUTION_SCAN_TIMEOUT_SECONDS,
                )
                if wait_for == "raw":
                    execution_progress_changed = any(
                        event.type in _JOB_PROGRESS_EVENT_TYPES for event in execution_events
                    )
                else:
                    execution_progress_changed = meaningful_execution_changed
                response_cursor = max(response_cursor, execution_cursor)
                if response_cursor != snapshot.cursor:
                    snapshot = replace(snapshot, cursor=response_cursor)
                log.debug(
                    "mcp.tool.job_wait.execution_scan.exit",
                    job_id=job_id,
                    execution_id=snapshot.links.execution_id,
                    event_count=len(execution_events),
                    execution_cursor=execution_cursor,
                    execution_progress_changed=execution_progress_changed,
                    response_cursor=snapshot.cursor,
                    elapsed_seconds=round(asyncio.get_running_loop().time() - started_at, 3),
                )
            except TimeoutError:
                execution_scan_timed_out = True
                log.debug(
                    "mcp.tool.job_wait.execution_scan.timeout",
                    job_id=job_id,
                    execution_id=snapshot.links.execution_id,
                    cursor=cursor,
                    timeout_seconds=_JOB_WAIT_EXECUTION_SCAN_TIMEOUT_SECONDS,
                    elapsed_seconds=round(asyncio.get_running_loop().time() - started_at, 3),
                )

        render_timed_out = False
        try:
            log.debug(
                "mcp.tool.job_wait.render.enter",
                job_id=job_id,
                view=view,
                snapshot_status=snapshot.status.value,
                snapshot_cursor=snapshot.cursor,
                timeout_seconds=_JOB_WAIT_RENDER_TIMEOUT_SECONDS,
            )
            text, progress = await asyncio.wait_for(
                _render_job_snapshot(snapshot, self._event_store),
                timeout=_JOB_WAIT_RENDER_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            render_timed_out = True
            progress = dict(_EMPTY_PROGRESS)
            text = _render_compact_job_snapshot(
                snapshot,
                progress,
                include_message=view == "summary",
            )
            if view == "full":
                text += "\n\nFull job render timed out; poll again with compact view."
            log.debug(
                "mcp.tool.job_wait.render.timeout",
                job_id=job_id,
                view=view,
                timeout_seconds=_JOB_WAIT_RENDER_TIMEOUT_SECONDS,
                elapsed_seconds=round(asyncio.get_running_loop().time() - started_at, 3),
            )
        else:
            log.debug(
                "mcp.tool.job_wait.render.exit",
                job_id=job_id,
                view=view,
                render_timed_out=False,
                elapsed_seconds=round(asyncio.get_running_loop().time() - started_at, 3),
            )
        stream_items = [_event_stream_item(event) for event in stream_events]
        relay_events: list[dict[str, object]] = []
        if stream == "linked" and (stream_events or snapshot.is_terminal):
            history_max_row_id = None if snapshot.is_terminal else snapshot.cursor
            history_events = await _query_linked_history_events(
                self._event_store,
                snapshot,
                max_row_id=history_max_row_id,
            )
            if snapshot.is_terminal:
                unseen_events = await _query_linked_history_events(
                    self._event_store,
                    snapshot,
                    after_cursor=cursor,
                )
                new_event_ids = {event.id for event in unseen_events}
            else:
                new_event_ids = {event.id for event in stream_events}
            relay_events = classify_relay_events(
                history_events,
                new_event_ids=new_event_ids,
                job_id=snapshot.job_id,
                available_tools=self.available_conductor_tools,
            )
            if snapshot.is_terminal:
                relay_events.append(
                    {
                        "id": f"terminal_{snapshot.job_id}_{snapshot.status.value}",
                        "kind": "terminal",
                        "subtype": "job_terminal",
                        "scope": {
                            "job_id": snapshot.job_id,
                            "execution_id": snapshot.links.execution_id,
                            "session_id": snapshot.links.session_id,
                            "lineage_id": snapshot.links.lineage_id,
                            "semantic_ac_key": None,
                        },
                        "evidence": {
                            "status": snapshot.status.value,
                            "message": snapshot.message[:320],
                            "is_error": _job_is_error(snapshot),
                        },
                    }
                )
        if stream_items:
            text += "\n\n### Stream Events"
            for item in stream_items[-_JOB_STREAM_EVENT_LIMIT:]:
                detail = f" -- {item['detail']}" if item["detail"] else ""
                text += f"\n- `{item['type']}` [{item['scope']}]{detail}"
            if stream_has_more:
                text += f"\n\nMore linked events pending; poll again with cursor={snapshot.cursor}."
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
        response_changed = (
            changed or execution_progress_changed or stream_changed or bool(relay_events)
        )
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
                text += _compact_stream_suffix(
                    stream_items, snapshot.cursor, has_more=stream_has_more
                )
            elif view in {"compact", "summary"}:
                if stream_items:
                    summaries = ", ".join(item["type"] for item in stream_items[-3:])
                    more = " more=1" if stream_has_more else ""
                    text = (
                        f"stream_events {len(stream_items)} "
                        f"cursor={snapshot.cursor}{more}: {summaries}"
                    )
                elif snapshot.is_terminal:
                    text = _render_compact_job_snapshot(
                        snapshot,
                        progress,
                        include_message=view == "summary",
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
            text += _compact_stream_suffix(stream_items, snapshot.cursor, has_more=stream_has_more)
        elif view == "summary":
            text = _render_compact_job_snapshot(snapshot, progress, include_message=True)
            text += _compact_stream_suffix(stream_items, snapshot.cursor, has_more=stream_has_more)
        result = MCPToolResult(
            content=(MCPContentItem(type=ContentType.TEXT, text=text),),
            is_error=_job_is_error(snapshot),
            meta={
                **_job_snapshot_meta(snapshot),
                "changed": response_changed,
                "view": view,
                "stream": stream,
                "wait_for": wait_for,
                "timeout_seconds": timeout_seconds,
                "timeout_seconds_requested": requested_timeout_seconds,
                "timeout_seconds_capped": timeout_seconds != requested_timeout_seconds,
                "execution_scan_timed_out": execution_scan_timed_out,
                "render_timed_out": render_timed_out,
                "stream_events": stream_items,
                "relay_events": relay_events,
                "stream_has_more": stream_has_more,
                **progress,
            },
        )
        log.debug(
            "mcp.tool.job_wait.return",
            job_id=job_id,
            status=snapshot.status.value,
            changed=response_changed,
            is_error=result.is_error,
            cursor=snapshot.cursor,
            execution_scan_timed_out=execution_scan_timed_out,
            render_timed_out=render_timed_out,
            elapsed_seconds=round(asyncio.get_running_loop().time() - started_at, 3),
        )
        return Result.ok(result)

    async def _wait_for_meaningful_change(
        self,
        job_id: str,
        *,
        cursor: int,
        timeout_seconds: int,
        wait_for: str,
    ) -> tuple[JobSnapshot, bool, int, bool]:
        """Long-poll until a filtered progress signal or terminal job status."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        baseline_progress: dict[str, Any] | None = None
        latest_execution_cursor = cursor

        while True:
            snapshot, job_changed = await self._job_manager.wait_for_change(
                job_id,
                cursor=cursor,
                timeout_seconds=0,
            )
            if snapshot.is_terminal:
                return snapshot, True, latest_execution_cursor, False

            execution_changed = False
            if wait_for != "terminal" and snapshot.links.execution_id:
                if baseline_progress is None:
                    baseline_progress, _baseline_cursor = await _query_execution_progress_at_cursor(
                        self._event_store,
                        snapshot.links.execution_id,
                        cursor=cursor,
                    )
                (
                    latest_progress,
                    latest_execution_cursor,
                ) = await _query_execution_progress_at_cursor(
                    self._event_store,
                    snapshot.links.execution_id,
                )
                execution_changed = _job_wait_progress_changed(
                    baseline_progress,
                    latest_progress,
                    wait_for=wait_for,
                )
                if execution_changed:
                    response_cursor = max(snapshot.cursor, latest_execution_cursor, cursor)
                    if response_cursor != snapshot.cursor:
                        snapshot = replace(snapshot, cursor=response_cursor)
                    return snapshot, job_changed, latest_execution_cursor, True

            if asyncio.get_running_loop().time() >= deadline:
                response_cursor = max(snapshot.cursor, latest_execution_cursor, cursor)
                if response_cursor != snapshot.cursor:
                    snapshot = replace(snapshot, cursor=response_cursor)
                return snapshot, False, latest_execution_cursor, False

            await asyncio.sleep(0.5)

    async def _wait_for_linked_change(
        self,
        job_id: str,
        *,
        cursor: int,
        timeout_seconds: int,
        wait_for: str,
    ) -> tuple[JobSnapshot, bool, list[BaseEvent], int, bool]:
        """Long-poll job plus linked child/session event streams."""
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        baseline_progress: dict[str, Any] | None = None
        while True:
            snapshot, changed = await self._job_manager.wait_for_change(
                job_id,
                cursor=cursor,
                timeout_seconds=0,
            )
            events, stream_cursor, has_more = await _query_linked_stream_events(
                self._event_store,
                snapshot,
                cursor,
            )
            progress_changed = False
            attention_changed = any(event.type in _JOB_ATTENTION_EVENT_TYPES for event in events)
            if wait_for != "raw" and wait_for != "terminal" and snapshot.links.execution_id:
                if baseline_progress is None:
                    baseline_progress, _baseline_cursor = await _query_execution_progress_at_cursor(
                        self._event_store,
                        snapshot.links.execution_id,
                        cursor=cursor,
                    )
                (
                    latest_progress,
                    _latest_execution_cursor,
                ) = await _query_execution_progress_at_cursor(
                    self._event_store,
                    snapshot.links.execution_id,
                )
                progress_changed = _job_wait_progress_changed(
                    baseline_progress,
                    latest_progress,
                    wait_for=wait_for,
                )
            if (
                (wait_for == "raw" and (changed or events))
                or (
                    wait_for != "raw"
                    and (
                        snapshot.is_terminal
                        or progress_changed
                        or (wait_for == "attention_or_ac_change" and attention_changed)
                    )
                )
                or snapshot.is_terminal
                or asyncio.get_running_loop().time() >= deadline
            ):
                # Pin the public cursor to the linked page boundary (not max with
                # the job-manager cursor): the boundary uniformly bounds every
                # linked stream including job, so a higher job cursor would skip
                # held-back lower-rowid events from a clamped sibling stream.
                if snapshot.cursor != stream_cursor:
                    snapshot = replace(snapshot, cursor=stream_cursor)
                return snapshot, changed, events, stream_cursor, has_more
            await asyncio.sleep(0.5)


@dataclass
class JobResultHandler:
    """Fetch the terminal output for a background job."""

    event_store: EventStore | None = field(default=None, repr=False)
    job_manager: JobManager | None = field(default=None, repr=False)
    available_conductor_tools: frozenset[str] = field(default_factory=frozenset, repr=False)

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

        cached_snapshot = _get_cached_job_snapshot(self._job_manager, job_id)
        if cached_snapshot is not None and cached_snapshot.is_terminal:
            snapshot = cached_snapshot
        else:
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
        relay_events: list[dict[str, object]] = []
        if snapshot.is_terminal:
            history_events = await _query_linked_history_events(
                self._event_store,
                snapshot,
            )
            relay_events = [
                relay
                for relay in classify_relay_events(
                    history_events,
                    job_id=snapshot.job_id,
                    available_tools=self.available_conductor_tools,
                )
                if relay.get("kind") == "attention_required"
            ]
        return Result.ok(
            MCPToolResult(
                content=content,
                is_error=_job_is_error(snapshot),
                meta={
                    **_job_snapshot_meta(snapshot),
                    **result_payload_meta,
                    **snapshot.result_meta,
                    "relay_events": relay_events,
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
