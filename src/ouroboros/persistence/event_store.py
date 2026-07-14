"""EventStore implementation for event sourcing.

Provides async methods for appending and replaying events using SQLAlchemy Core
with aiosqlite backend.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import logging
from pathlib import Path
import sqlite3
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote
from uuid import uuid4

from sqlalchemy import and_, event, func, or_, select, text
from sqlalchemy.exc import OperationalError

if TYPE_CHECKING:
    from ouroboros.orchestrator.workflow_lifecycle import WorkflowLifecycleEvent
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent
from ouroboros.persistence.schema import events_table, metadata

logger = logging.getLogger(__name__)

_RAW_SUBSCRIBED_EVENT_TYPE_KEYS = frozenset({"type", "event", "kind", "name"})
_RAW_SUBSCRIBED_EVENT_SIGNAL_KEYS = frozenset(
    {
        "args",
        "arguments",
        "command",
        "content",
        "delta",
        "error",
        "input",
        "message",
        "params",
        "path",
        "payload",
        "result",
        "run_id",
        "server_run_id",
        "server_session_id",
        "session",
        "session_id",
        "summary",
        "text",
        "thread_id",
        "tool",
        "tool_name",
    }
)


def _normalized_mapping_keys(value: Mapping[object, object]) -> set[str]:
    """Return normalized string keys for mapping inspection."""
    return {str(key).strip().lower().replace("-", "_") for key in value}


def _looks_like_raw_subscribed_event_payload(value: object) -> bool:
    """Return True when the value resembles a subscribed runtime stream event."""
    if not isinstance(value, Mapping):
        return False

    normalized_keys = _normalized_mapping_keys(value)
    if {"aggregate_type", "aggregate_id", "data"} <= normalized_keys:
        return False

    if not (_RAW_SUBSCRIBED_EVENT_TYPE_KEYS & normalized_keys):
        return False

    return bool(_RAW_SUBSCRIBED_EVENT_SIGNAL_KEYS & normalized_keys)


def _session_related_event_conditions(
    session_id: str,
    execution_id: str | None,
) -> list[Any]:
    """Build aggregate-id predicates for a session and its execution scopes.

    An empty ``session_id`` contributes no session predicate: matching
    ``json_extract(payload,'$.session_id') == ''`` would pull unrelated rows
    that happen to persist a blank session field. Callers with only an
    execution scope (e.g. a TUI poll whose context has no session yet) still
    match worker-scoped events through the ``execution_id`` predicates below.
    """
    conditions: list[Any] = []
    if session_id:
        conditions.append(events_table.c.aggregate_id == session_id)
        conditions.append(func.json_extract(events_table.c.payload, "$.session_id") == session_id)
    if not execution_id:
        return conditions

    conditions.append(events_table.c.aggregate_id == execution_id)
    conditions.append(func.json_extract(events_table.c.payload, "$.execution_id") == execution_id)
    conditions.append(
        func.json_extract(events_table.c.payload, "$.parent_execution_id") == execution_id
    )

    return conditions


class EventStore:
    """Event store for persisting and replaying events.

    Uses SQLAlchemy Core with aiosqlite for async database operations.
    All operations are transactional for atomicity.

    Usage:
        store = EventStore("sqlite+aiosqlite:///ouroboros.db")
        await store.initialize()

        # Append event
        await store.append(event)

        # Replay events for an aggregate
        events = await store.replay("seed", "seed-123")

        # Close when done
        await store.close()
    """

    def __init__(
        self,
        database_url: str | None = None,
        *,
        read_only: bool = False,
    ) -> None:
        """Initialize EventStore with database URL.

        Args:
            database_url: SQLAlchemy database URL.
                         For async SQLite: "sqlite+aiosqlite:///path/to/db.sqlite"
                         If not provided, defaults to ~/.ouroboros/ouroboros.db
            read_only: When True, open the underlying SQLite database in true
                read-only mode by rewriting the URL into the ``file:<path>?mode=ro&uri=true``
                form and passing ``connect_args={"uri": True}`` to aiosqlite.
                This enforces the read-only contract at the connection layer
                so *any* accidental write path (including library/future code
                paths we don't control) fails fast with
                ``sqlite3.OperationalError: attempt to write a readonly database``.
                Callers that opt in should also skip schema creation by calling
                ``initialize(create_schema=False)`` — this is the default when
                ``read_only=True``. ``read_only`` is a no-op for non-SQLite URLs.
        """
        if database_url is None:
            db_path = Path.home() / ".ouroboros" / "ouroboros.db"
            if not read_only:
                db_path.parent.mkdir(parents=True, exist_ok=True)
            database_url = f"sqlite+aiosqlite:///{db_path}"

        self._read_only = read_only
        if read_only:
            database_url = self._coerce_to_readonly_url(database_url)
        self._database_url = database_url
        self._engine: AsyncEngine | None = None
        # Anchor connection for process-shared in-memory databases (memdb VFS):
        # the database lives as long as at least one connection holds it open.
        self._memory_keepalive: sqlite3.Connection | None = None

    @staticmethod
    def _coerce_to_readonly_url(database_url: str) -> str:
        """Rewrite a plain aiosqlite URL into a ``mode=ro`` URI form.

        Leaves non-SQLite URLs untouched. Already-URI forms (starting with
        ``file:``) are returned as-is so explicit callers keep full control.
        """
        prefix = "sqlite+aiosqlite:///"
        if not database_url.startswith(prefix):
            return database_url

        path_part = database_url[len(prefix) :]
        if path_part.startswith("file:"):
            # Caller already provided a URI form — respect it verbatim.
            return database_url

        # ``:memory:`` has no filesystem and cannot be opened read-only
        # meaningfully; leave it alone.
        if path_part in (":memory:", ""):
            return database_url

        return f"{prefix}file:{path_part}?mode=ro&uri=true"

    @staticmethod
    def _sqlite_path_from_url(database_url: str) -> str | None:
        """Filesystem path of the SQLite file this URL points at, else ``None``.

        Returns ``None`` for in-memory or non-SQLite backends (they have no local
        file). Understands both the plain ``sqlite+aiosqlite:///<path>`` form and
        the read-only ``…///file:<path>?mode=ro&uri=true`` URI form.
        """
        prefix = "sqlite+aiosqlite:///"
        if not database_url.startswith(prefix):
            return None
        path_part = database_url[len(prefix) :]
        if path_part.startswith("file:"):
            # URI form — drop the ``file:`` scheme and any ``?query``/``#fragment``,
            # then percent-decode the path back to its filesystem form.
            rest = path_part[len("file:") :]
            rest = rest.split("?", 1)[0].split("#", 1)[0]
            path_part = unquote(rest)
        if path_part in (":memory:", ""):
            return None
        return path_part

    def sqlite_path(self) -> str | None:
        """Filesystem path of the backing SQLite file, or ``None``.

        The dashboard daemon is DB-scoped: it must tail the *same* file this store
        writes to. Custom-path stores (``ooo mcp --db-path``) otherwise get a
        dashboard for the home-directory default. Returns ``None`` for in-memory /
        non-SQLite backends, where there is no local file to point the daemon at.
        """
        return self._sqlite_path_from_url(self._database_url)

    @property
    def database_url(self) -> str:
        """Canonical database URL used by this store.

        Detached job workers must open the exact same event stream as the MCP
        process that accepted the request.  Exposing the already-normalized URL
        avoids reaching into private engine state and also preserves custom
        ``--db-path`` deployments.
        """
        return self._database_url

    @property
    def supports_cross_process_workers(self) -> bool:
        """Whether another process can observe this store's event stream."""
        if not self._database_url.startswith("sqlite+aiosqlite:///"):
            return True
        return self.sqlite_path() is not None

    def _raise_invalid_append_input(
        self,
        event: object,
        *,
        operation: str,
        index: int | None = None,
    ) -> None:
        """Raise a persistence error for invalid append inputs."""
        details = {"received_type": type(event).__name__}
        if index is not None:
            details["event_index"] = index

        if isinstance(event, Mapping):
            details["received_keys"] = sorted(_normalized_mapping_keys(event))[:12]
            if _looks_like_raw_subscribed_event_payload(event):
                raise PersistenceError(
                    "EventStore rejects raw subscribed event stream payloads. "
                    "Normalize them into BaseEvent records before persistence.",
                    operation=operation,
                    details=details,
                )

        raise PersistenceError(
            "EventStore only persists BaseEvent instances.",
            operation=operation,
            details=details,
        )

    async def initialize(self, *, create_schema: bool | None = None) -> None:
        """Initialize the database connection and create tables if needed.

        This method is idempotent - calling it multiple times is safe.

        Args:
            create_schema: When True run ``metadata.create_all`` so missing
                tables are created. Read-only consumers (for example diagnostic
                CLI commands that must not mutate the store) can pass ``False``
                to skip schema creation entirely. When ``None`` (default), the
                value follows ``read_only``: stores constructed with
                ``read_only=True`` skip schema creation and all others create
                it, preserving the prior default behaviour.

        For aiosqlite ``:memory:`` databases, backs the store with SQLite's
        process-shared in-memory VFS (``memdb``): pooled connections join one
        shared database with normal connection-scoped transactions, and a
        keepalive connection anchors the database's lifetime.
        """
        if create_schema is None:
            create_schema = not self._read_only

        if self._read_only and create_schema:
            raise PersistenceError(
                "Cannot create schema on a read-only EventStore.",
                operation="initialize",
                details={"read_only": True},
            )

        if self._engine is None:
            connect_args: dict[str, object] = {"timeout": 30}
            if self._read_only:
                # aiosqlite forwards unknown kwargs to sqlite3.connect — the
                # ``uri=True`` flag is what turns the ``file:...?mode=ro`` form
                # into a real read-only connection.
                connect_args["uri"] = True

            engine_kwargs: dict[str, Any] = {
                "echo": False,
                "connect_args": connect_args,
            }
            engine_url = self._database_url
            if self._database_url.endswith("/:memory:"):
                # A plain ``:memory:`` SQLite database is scoped to ONE DB-API
                # connection, which is fundamentally unsafe under concurrent
                # async use:
                #
                # - StaticPool (used previously) hands the same connection to
                #   CONCURRENT ``engine.begin()`` blocks with no mutual
                #   exclusion. SQLite transactions are connection-scoped, so a
                #   concurrent block exiting via exception (e.g. a monitor task
                #   cancelled mid-SELECT) issues a ROLLBACK that silently
                #   discards another task's uncommitted INSERT — whose own
                #   COMMIT then no-ops (sqlite3 commit with no active
                #   transaction) and append() reports success while the event
                #   vanishes. That silent append-void is the mechanism behind
                #   the #1566 / PR #1576 CI zombie jobs.
                # - ANY single-connection pool additionally loses the entire
                #   database when asyncio cancellation invalidates the pooled
                #   connection: the replacement connection is a fresh empty DB
                #   ("no such table: events").
                #
                # Fix: back ``:memory:`` stores with SQLite's process-shared
                # in-memory VFS (``memdb``, SQLite >= 3.36). Every pooled
                # connection then joins the SAME in-memory database with normal
                # connection-scoped transactions (no interleaving) and the
                # database survives connection invalidation; a keepalive
                # connection anchors its lifetime. Each store instance gets a
                # unique name, preserving the "one ``:memory:`` store == one
                # private database" semantics.
                if sqlite3.sqlite_version_info >= (3, 36):
                    shared_name = f"/ouroboros-mem-{uuid4().hex}"
                    engine_url = f"sqlite+aiosqlite:///file:{shared_name}?vfs=memdb&uri=true"
                    self._memory_keepalive = sqlite3.connect(
                        f"file:{shared_name}?vfs=memdb",
                        uri=True,
                        check_same_thread=False,
                    )
                else:  # pragma: no cover - ancient SQLite fallback
                    # Serialized one-connection pool: keeps the shared
                    # connection AND makes checkouts mutually exclusive so
                    # transactions cannot interleave (closes the append-void);
                    # cancellation-invalidation remains a residual risk here.
                    engine_kwargs["poolclass"] = AsyncAdaptedQueuePool
                    engine_kwargs["pool_size"] = 1
                    engine_kwargs["max_overflow"] = 0

            self._engine = create_async_engine(
                engine_url,
                **engine_kwargs,
            )

            # Enable WAL mode and set busy timeout on every new connection.
            # Skipped for read-only consumers: ``PRAGMA journal_mode=WAL`` is
            # itself a write and would trip SQLite's read-only guard.
            if not self._read_only:

                @event.listens_for(self._engine.sync_engine, "connect")
                def _set_sqlite_pragmas(dbapi_conn, _connection_record):
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA synchronous=NORMAL")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.close()

        # Create all tables defined in metadata (skipped for read-only consumers)
        if create_schema:
            async with self._engine.begin() as conn:
                await conn.run_sync(metadata.create_all)

    async def append(
        self,
        event: BaseEvent,
        *,
        _skip_workflow_ir_guard: bool = False,
    ) -> None:
        """Append an event to the store.

        The operation is wrapped in a transaction for atomicity.
        If the insert fails, the transaction is rolled back.

        Args:
            event: The event to append.

        Raises:
            PersistenceError: If the append operation fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="append",
            )
        if not isinstance(event, BaseEvent):
            self._raise_invalid_append_input(event, operation="append")

        # Guard the workflow IR lifecycle family from direct raw appends:
        # ``WorkflowLifecycleEvent`` enforces the replay-unsafe key blocklist
        # at the Pydantic model boundary, so a caller that constructs a raw
        # ``BaseEvent`` with ``aggregate_type="workflow_ir"`` would bypass
        # that redaction. Route lifecycle persistence exclusively through
        # :meth:`append_workflow_lifecycle_event`. The internal-only
        # ``_skip_workflow_ir_guard`` flag is used by that helper after it has
        # already validated the event via ``WorkflowLifecycleEvent``.
        if event.aggregate_type == "workflow_ir" and not _skip_workflow_ir_guard:
            raise PersistenceError(
                "Workflow IR lifecycle events must be persisted via "
                "append_workflow_lifecycle_event() to preserve the "
                "WorkflowLifecycleEvent redaction guard.",
                operation="append",
                details={
                    "aggregate_type": event.aggregate_type,
                    "event_type": event.type,
                },
            )

        for attempt in range(3):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(events_table.insert().values(**event.to_db_dict()))
                return
            except Exception as e:
                if "database is locked" in str(e) and attempt < 2:
                    logger.warning(
                        "event_store.append.retry",
                        extra={"attempt": attempt + 1, "event_id": event.id},
                    )
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise PersistenceError(
                    f"Failed to append event: {e}",
                    operation="insert",
                    table="events",
                    details={"event_id": event.id, "event_type": event.type},
                ) from e

    async def append_batch(self, events: list[BaseEvent]) -> None:
        """Append multiple events atomically in a single transaction.

        All events are inserted in a single transaction. If any insert fails,
        the entire batch is rolled back, ensuring atomicity.

        This is more efficient than calling append() multiple times and
        guarantees that either all events are persisted or none are.

        Args:
            events: List of events to append.

        Raises:
            PersistenceError: If the batch operation fails. No events
                             will be persisted if this is raised.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="append_batch",
            )

        if not events:
            return  # Nothing to do
        invalid_events = [
            (index, event) for index, event in enumerate(events) if not isinstance(event, BaseEvent)
        ]
        if invalid_events:
            invalid_index, invalid_event = invalid_events[0]
            self._raise_invalid_append_input(
                invalid_event,
                operation="append_batch",
                index=invalid_index,
            )

        # Mirror the ``append()`` workflow_ir guard so callers cannot bypass
        # the ``WorkflowLifecycleEvent`` redaction blocklist by batching raw
        # ``BaseEvent`` instances. Lifecycle persistence must go through
        # :meth:`append_workflow_lifecycle_event`, which validates payloads
        # at the Pydantic boundary before delegating to :meth:`append`.
        # This check runs BEFORE any DB insert so a single bad row in the
        # batch refuses the entire transaction.
        from ouroboros.orchestrator.workflow_lifecycle import (
            WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
        )

        workflow_ir_events = [
            e for e in events if e.aggregate_type == WORKFLOW_LIFECYCLE_AGGREGATE_TYPE
        ]
        if workflow_ir_events:
            raise PersistenceError(
                "Workflow IR lifecycle events must be persisted via "
                "append_workflow_lifecycle_event() and cannot be batched.",
                operation="append_batch",
                details={"count": len(workflow_ir_events)},
            )

        for attempt in range(3):
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        events_table.insert(),
                        [event.to_db_dict() for event in events],
                    )
                return
            except Exception as e:
                if "database is locked" in str(e) and attempt < 2:
                    logger.warning(
                        "event_store.append_batch.retry",
                        extra={"attempt": attempt + 1, "batch_size": len(events)},
                    )
                    await asyncio.sleep(0.1 * (2**attempt))
                    continue
                raise PersistenceError(
                    f"Failed to append event batch: {e}",
                    operation="insert_batch",
                    table="events",
                    details={
                        "batch_size": len(events),
                        "event_ids": [e.id for e in events[:5]],
                    },
                ) from e

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        """Replay all events for a specific aggregate.

        The operation uses a transaction for read consistency.

        Args:
            aggregate_type: The type of aggregate (e.g., "seed", "execution").
            aggregate_id: The unique identifier of the aggregate.

        Returns:
            List of events for the aggregate, ordered by timestamp.

        Raises:
            PersistenceError: If the replay operation fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="replay",
            )

        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    select(events_table)
                    .where(events_table.c.aggregate_type == aggregate_type)
                    .where(events_table.c.aggregate_id == aggregate_id)
                    # Order by timestamp + id for deterministic replay when
                    # multiple events share the same timestamp resolution.
                    .order_by(events_table.c.timestamp, events_table.c.id)
                )
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to replay events: {e}",
                operation="select",
                table="events",
                details={
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                },
            ) from e

    async def get_events_after(
        self,
        aggregate_type: str,
        aggregate_id: str,
        last_row_id: int = 0,
        *,
        limit: int | None = None,
        max_row_id: int | None = None,
    ) -> tuple[list[BaseEvent], int]:
        """Get events for an aggregate after a given row ID.

        Incremental fetch that only returns new events since the last poll,
        avoiding the O(n) cost of replaying the full event history.

        Args:
            aggregate_type: The type of aggregate (e.g., "execution").
            aggregate_id: The unique identifier of the aggregate.
            last_row_id: The SQLite rowid of the last event processed.
                         Pass 0 to get all events from the beginning.
            limit: Optional maximum number of events to materialize. When set,
                   the page is ordered by ``rowid`` (the cursor dimension) and
                   capped, so the returned ``max(rowid)`` is a true boundary and
                   a follow-up poll resumes the remainder without skipping rows
                   appended out of timestamp order. ``None`` keeps the unbounded
                   timestamp-ordered fetch other callers rely on.
            max_row_id: Optional inclusive upper bound on rowid. Lets a caller
                   that has already chosen a global page boundary re-read exactly
                   the rows at or before that boundary (``rowid <= max_row_id``).

        Returns:
            Tuple of (list of new events, max rowid seen).
            The max rowid should be passed back as last_row_id on the next call.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_events_after",
            )

        try:
            async with self._engine.begin() as conn:
                # Use SQLite's implicit rowid for efficient cursor-based pagination.
                # This avoids deserializing all prior events just to slice the tail.
                rowid_col = text("rowid")
                query = (
                    select(events_table, rowid_col)
                    .where(events_table.c.aggregate_type == aggregate_type)
                    .where(events_table.c.aggregate_id == aggregate_id)
                    .where(text("rowid > :last_id").bindparams(last_id=last_row_id))
                )
                if max_row_id is not None:
                    query = query.where(text("rowid <= :max_id").bindparams(max_id=max_row_id))
                if limit is not None:
                    # Page by rowid so max(rowid) is a contiguous boundary; ordering
                    # a limited page by timestamp could advance the cursor past a
                    # lower-rowid row appended out of order, skipping it forever.
                    query = query.order_by(rowid_col).limit(limit)
                else:
                    query = query.order_by(events_table.c.timestamp, events_table.c.id)
                result = await conn.execute(query)
                rows = result.mappings().all()
                if not rows:
                    return [], last_row_id
                events = [BaseEvent.from_db_row(dict(row)) for row in rows]
                max_rowid = max(row["rowid"] for row in rows)
                return events, max_rowid
        except Exception as e:
            raise PersistenceError(
                f"Failed to get events after rowid {last_row_id}: {e}",
                operation="select",
                table="events",
                details={
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "last_row_id": last_row_id,
                },
            ) from e

    async def get_current_rowid(self) -> int:
        """Return the current maximum event-store rowid.

        Callers can use this as a global cursor baseline before starting work,
        then query ``rowid > baseline`` across any aggregate discovered later.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_current_rowid",
            )

        try:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    select(func.coalesce(func.max(text("rowid")), 0)).select_from(events_table)
                )
                return int(result.scalar_one() or 0)
        except Exception as e:
            raise PersistenceError(
                f"Failed to get current rowid: {e}",
                operation="select",
                table="events",
            ) from e

    async def get_recent_aggregate_events(
        self,
        aggregate_type: str,
        aggregate_id: str,
        *,
        event_types: set[str] | None = None,
        max_row_id: int | None = None,
        limit: int = 500,
    ) -> tuple[list[BaseEvent], int]:
        """Return the latest bounded rowid page for one aggregate.

        ``get_events_after(..., limit=...)`` pages forward from an old cursor,
        which is correct for replay but wrong for status rendering that needs
        the latest visible progress from a long execution. This helper reads
        the newest matching rowids first, then returns them in ascending cursor
        order so callers can summarize a bounded recent window without
        fabricating cursors.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_recent_aggregate_events",
            )

        try:
            async with self._engine.begin() as conn:
                rowid_col = text("rowid")
                query = (
                    select(events_table, rowid_col)
                    .where(events_table.c.aggregate_type == aggregate_type)
                    .where(events_table.c.aggregate_id == aggregate_id)
                )
                if event_types:
                    query = query.where(events_table.c.event_type.in_(sorted(event_types)))
                if max_row_id is not None:
                    query = query.where(text("rowid <= :max_id").bindparams(max_id=max_row_id))
                query = query.order_by(text("rowid DESC")).limit(limit)
                result = await conn.execute(query)
                rows = result.mappings().all()
                if not rows:
                    return [], max_row_id or 0
                rows = list(reversed(rows))
                events = [BaseEvent.from_db_row(dict(row)) for row in rows]
                max_seen = max(row["rowid"] for row in rows)
                return events, max_seen
        except Exception as e:
            raise PersistenceError(
                f"Failed to get recent aggregate events: {e}",
                operation="select",
                table="events",
                details={
                    "aggregate_type": aggregate_type,
                    "aggregate_id": aggregate_id,
                    "event_types": sorted(event_types) if event_types else None,
                    "max_row_id": max_row_id,
                    "limit": limit,
                },
            ) from e

    async def get_recent_events(
        self, event_type: str | None = None, limit: int = 100
    ) -> list[BaseEvent]:
        """Get recent events, optionally filtered by type.

        Args:
            event_type: Optional event type to filter by.
            limit: Maximum number of events to return.

        Returns:
            List of recent events, ordered by timestamp descending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_recent_events",
            )

        try:
            async with self._engine.begin() as conn:
                query = select(events_table).order_by(events_table.c.timestamp.desc()).limit(limit)

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get recent events: {e}",
                operation="select",
                table="events",
            ) from e

    async def get_all_sessions(self) -> list[BaseEvent]:
        """Get all session lifecycle events.

        Returns all ``orchestrator.session.*`` events ordered by timestamp
        ascending so callers can replay them to reconstruct current status.

        Returns:
            List of session events, ordered by timestamp ascending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_all_sessions",
            )

        # Pin the selective ix_events_event_type index. SQLite's planner
        # otherwise satisfies ORDER BY timestamp by scanning *every* event row
        # via ix_events_timestamp and filtering inline — a 30s+ query on large
        # stores, even though session events are a tiny subset. SQLAlchemy/SQLite
        # ignores with_hint(), so the hint is pinned via raw SQL.
        # ``.columns(*events_table.c)`` re-attaches the ORM column types so the
        # raw result gets the same processing as ``select(events_table)`` (notably
        # JSON payload deserialization); without it from_db_row rejects the
        # payload string.
        indexed_query = (
            text(
                "SELECT * FROM events INDEXED BY ix_events_event_type "
                "WHERE event_type LIKE :pattern "
                "ORDER BY timestamp ASC"
            )
            .bindparams(pattern="orchestrator.session.%")
            .columns(*events_table.c)
        )
        fallback_query = (
            select(events_table)
            .where(events_table.c.event_type.like("orchestrator.session.%"))
            .order_by(events_table.c.timestamp.asc())
        )
        try:
            try:
                async with self._engine.begin() as conn:
                    result = await conn.execute(indexed_query)
                    rows = result.mappings().all()
                    return [BaseEvent.from_db_row(dict(row)) for row in rows]
            except OperationalError:
                # ``INDEXED BY`` makes ix_events_event_type mandatory; SQLite
                # errors if it is absent (e.g. a store created outside the
                # bundled schema/migrations). Fall back to the planner's choice
                # on a fresh transaction so correctness never depends on a
                # specific index name.
                async with self._engine.begin() as conn:
                    result = await conn.execute(fallback_query)
                    rows = result.mappings().all()
                    return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get all sessions: {e}",
                operation="select",
                table="events",
                details={"event_type": "orchestrator.session.%"},
            ) from e

    async def get_session_activity_snapshots(self) -> list[SessionActivitySnapshot]:
        """Return one session snapshot row per session aggregate.

        The snapshot includes session identity from the start event, the most
        recent session activity timestamp, and the latest status-bearing event
        or runtime_status payload when present. This avoids replaying every
        event for every session just to detect stale active sessions.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_session_activity_snapshots",
            )

        status_expr = func.coalesce(
            func.json_extract(events_table.c.payload, "$.progress.runtime_status"),
            func.json_extract(events_table.c.payload, "$.runtime_status"),
        )

        first_session_event_ranked = (
            select(
                events_table.c.aggregate_id.label("session_id"),
                func.json_extract(events_table.c.payload, "$.execution_id").label("execution_id"),
                func.json_extract(events_table.c.payload, "$.seed_id").label("seed_id"),
                func.coalesce(
                    func.json_extract(events_table.c.payload, "$.start_time"),
                    events_table.c.timestamp,
                ).label("start_time"),
                func.row_number()
                .over(
                    partition_by=events_table.c.aggregate_id,
                    order_by=(events_table.c.timestamp.asc(), events_table.c.id.asc()),
                )
                .label("rn"),
            )
            .where(events_table.c.aggregate_type == "session")
            .subquery()
        )

        latest_activity_ranked = (
            select(
                events_table.c.aggregate_id.label("session_id"),
                events_table.c.timestamp.label("last_activity"),
                func.row_number()
                .over(
                    partition_by=events_table.c.aggregate_id,
                    order_by=(events_table.c.timestamp.desc(), events_table.c.id.desc()),
                )
                .label("rn"),
            )
            .where(events_table.c.aggregate_type == "session")
            .subquery()
        )

        latest_status_ranked = (
            select(
                events_table.c.aggregate_id.label("session_id"),
                events_table.c.event_type.label("status_event_type"),
                status_expr.label("runtime_status"),
                func.row_number()
                .over(
                    partition_by=events_table.c.aggregate_id,
                    order_by=(events_table.c.timestamp.desc(), events_table.c.id.desc()),
                )
                .label("rn"),
            )
            .where(events_table.c.aggregate_type == "session")
            .where(
                or_(
                    events_table.c.event_type.in_(
                        (
                            "orchestrator.session.completed",
                            "orchestrator.session.failed",
                            "orchestrator.session.paused",
                            "orchestrator.session.cancelled",
                        )
                    ),
                    and_(
                        events_table.c.event_type.in_(
                            (
                                "orchestrator.progress.updated",
                                "workflow.progress.updated",
                            )
                        ),
                        status_expr.is_not(None),
                    ),
                )
            )
            .subquery()
        )

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(
                        first_session_event_ranked.c.session_id,
                        first_session_event_ranked.c.execution_id,
                        first_session_event_ranked.c.seed_id,
                        first_session_event_ranked.c.start_time,
                        latest_activity_ranked.c.last_activity,
                        latest_status_ranked.c.status_event_type,
                        latest_status_ranked.c.runtime_status,
                    )
                    .select_from(first_session_event_ranked)
                    .join(
                        latest_activity_ranked,
                        and_(
                            latest_activity_ranked.c.session_id
                            == first_session_event_ranked.c.session_id,
                            latest_activity_ranked.c.rn == 1,
                        ),
                    )
                    .outerjoin(
                        latest_status_ranked,
                        and_(
                            latest_status_ranked.c.session_id
                            == first_session_event_ranked.c.session_id,
                            latest_status_ranked.c.rn == 1,
                        ),
                    )
                    .where(first_session_event_ranked.c.rn == 1)
                    .order_by(first_session_event_ranked.c.session_id.asc())
                )

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [
                    SessionActivitySnapshot(
                        session_id=row["session_id"],
                        execution_id=row.get("execution_id"),
                        seed_id=row.get("seed_id"),
                        start_time=row.get("start_time"),
                        last_activity=row.get("last_activity"),
                        status_event_type=row.get("status_event_type"),
                        runtime_status=row.get("runtime_status"),
                    )
                    for row in rows
                ]
        except Exception as e:
            raise PersistenceError(
                f"Failed to fetch session activity snapshots: {e}",
                operation="select",
                table="events",
            ) from e

    async def query_events(
        self,
        aggregate_id: str | None = None,
        event_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        """Query events with optional filters.

        Args:
            aggregate_id: Optional aggregate ID to filter by (e.g., session_id).
            event_type: Optional event type to filter by.
            limit: Maximum number of events to return.
            offset: Number of events to skip for pagination.

        Returns:
            List of events matching the criteria, ordered by timestamp descending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_events",
            )

        try:
            async with self._engine.begin() as conn:
                query = select(events_table).order_by(events_table.c.timestamp.desc())

                if aggregate_id:
                    query = query.where(events_table.c.aggregate_id == aggregate_id)

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                query = query.limit(limit).offset(offset)

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to query events: {e}",
                operation="select",
                table="events",
                details={
                    "aggregate_id": aggregate_id,
                    "event_type": event_type,
                    "limit": limit,
                    "offset": offset,
                },
            ) from e

    async def query_session_related_events(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        """Query events across the session aggregate and related parallel scopes.

        Parallel execution stores activity in several aggregate families:
        - ``session/<session_id>`` for top-level session state
        - ``execution/<execution_id>`` for workflow progress
        - ``execution/*`` runtime scopes whose payload references the session,
          exact execution ID, or exact parent execution ID

        Related execution scopes are matched through persisted ``session_id`` /
        ``execution_id`` / ``parent_execution_id`` payload fields instead of
        normalized aggregate-name prefixes. Runtime aggregate names intentionally
        normalize dynamic IDs for filesystem/path safety, so using those lossy
        names as join keys can collide across distinct executions.

        Args:
            session_id: Orchestrator session ID.
            execution_id: Optional execution ID. If omitted, it is resolved from
                the session's start event when possible.
            event_type: Optional event-type filter.
            limit: Maximum number of events to return. ``None`` returns all.
            offset: Number of events to skip for pagination.

        Returns:
            Matching events ordered by timestamp descending.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_session_related_events",
            )

        resolved_execution_id = execution_id or await self._resolve_execution_id_for_session(
            session_id,
        )
        session_started_at = await self._resolve_session_started_at(session_id)

        conditions = _session_related_event_conditions(session_id, resolved_execution_id)
        if not conditions:
            # Fail closed: a blank session with no resolvable execution produces zero
            # scope predicates, and ``or_()`` over an empty list emits NO WHERE clause
            # — SQLAlchemy would then return the ENTIRE store. Select nothing instead.
            return []

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(events_table)
                    .where(or_(*conditions))
                    .order_by(events_table.c.timestamp.desc())
                )
                if session_started_at is not None:
                    query = query.where(events_table.c.timestamp >= session_started_at)

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                if limit is not None:
                    query = query.limit(limit).offset(offset)
                elif offset:
                    query = query.offset(offset)

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to query session-related events: {e}",
                operation="select",
                table="events",
                details={
                    "session_id": session_id,
                    "execution_id": resolved_execution_id,
                    "event_type": event_type,
                    "limit": limit,
                    "offset": offset,
                },
            ) from e

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        """Query events for an execution and its child/runtime scopes.

        This is the execution-only counterpart to
        :meth:`query_session_related_events`: it includes the root execution
        aggregate plus events whose payload links back through ``execution_id``
        or ``parent_execution_id``.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_execution_related_events",
            )

        conditions = [
            events_table.c.aggregate_id == execution_id,
            func.json_extract(events_table.c.payload, "$.execution_id") == execution_id,
            func.json_extract(events_table.c.payload, "$.parent_execution_id") == execution_id,
        ]

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(events_table)
                    .where(events_table.c.aggregate_type == "execution")
                    .where(or_(*conditions))
                    .order_by(events_table.c.timestamp.desc())
                )

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                if limit is not None:
                    query = query.limit(limit).offset(offset)
                elif offset:
                    query = query.offset(offset)

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to query execution-related events: {e}",
                operation="select",
                table="events",
                details={
                    "execution_id": execution_id,
                    "event_type": event_type,
                    "limit": limit,
                    "offset": offset,
                },
            ) from e

    async def get_latest_execution_job_status(self, execution_id: str) -> str | None:
        """Return the latest background-job status linked to an execution.

        AC runtime lifecycle rows can remain non-terminal when an stdio MCP host
        shuts down and interrupts the owning job. Synapse discovery uses this
        status as a fail-closed execution-generation guard so a later MCP process
        never advertises those abandoned attempts as live targets.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_latest_execution_job_status",
            )

        try:
            async with self._engine.begin() as conn:
                created_query = (
                    select(events_table.c.aggregate_id)
                    .where(events_table.c.aggregate_type == "job")
                    .where(events_table.c.event_type == "mcp.job.created")
                    .where(
                        func.json_extract(events_table.c.payload, "$.links.execution_id")
                        == execution_id
                    )
                    .order_by(events_table.c.timestamp.desc())
                    .limit(1)
                )
                created_result = await conn.execute(created_query)
                row = created_result.first()
                if row is None:
                    return None

                latest_query = (
                    select(events_table.c.payload)
                    .where(events_table.c.aggregate_type == "job")
                    .where(events_table.c.aggregate_id == row[0])
                    .order_by(events_table.c.timestamp.desc())
                    .limit(1)
                )
                latest_result = await conn.execute(latest_query)
                latest = latest_result.first()
                if latest is None or not isinstance(latest[0], Mapping):
                    return None
                status = latest[0].get("status")
                return status.strip() if isinstance(status, str) and status.strip() else None
        except Exception as e:
            raise PersistenceError(
                f"Failed to resolve latest execution job status: {e}",
                operation="select",
                table="events",
                details={"execution_id": execution_id},
            ) from e

    async def query_session_signal_events(
        self,
        *,
        execution_id: str,
        session_scope_id: str,
        session_attempt_id: str,
    ) -> list[BaseEvent]:
        """Return one exact target's SessionSignal history in replay order."""
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_session_signal_events",
            )
        try:
            async with self._engine.begin() as conn:
                query = (
                    select(events_table)
                    .where(events_table.c.aggregate_type == "session_signal")
                    .where(
                        func.json_extract(events_table.c.payload, "$.expected_execution_id")
                        == execution_id
                    )
                    .where(
                        func.json_extract(
                            events_table.c.payload,
                            "$.target_session_scope_id",
                        )
                        == session_scope_id
                    )
                    .where(
                        func.json_extract(
                            events_table.c.payload,
                            "$.target_session_attempt_id",
                        )
                        == session_attempt_id
                    )
                    .order_by(events_table.c.timestamp.asc())
                )
                result = await conn.execute(query)
                return [BaseEvent.from_db_row(dict(row)) for row in result.mappings().all()]
        except Exception as e:
            raise PersistenceError(
                f"Failed to query SessionSignal events: {e}",
                operation="select",
                table="events",
                details={
                    "execution_id": execution_id,
                    "session_scope_id": session_scope_id,
                    "session_attempt_id": session_attempt_id,
                },
            ) from e

    async def query_session_related_events_after(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        last_row_id: int = 0,
        *,
        limit: int | None = None,
        max_row_id: int | None = None,
    ) -> tuple[list[BaseEvent], int]:
        """Incrementally query events across a session and related execution scopes.

        This is the multi-aggregate equivalent of ``get_events_after``. It uses
        the same exact session/execution payload predicates as
        ``query_session_related_events`` while advancing a single rowid cursor.

        When ``limit`` is set the page is ordered by ``rowid`` and capped, so a
        stale or first-time poll over a long-running session cannot load an
        unbounded result set and the returned ``max(rowid)`` is a true boundary
        a follow-up poll resumes from without skipping rows appended out of
        timestamp order. ``max_row_id`` bounds the read to ``rowid <= max_row_id``
        for callers that have already chosen a global page boundary. ``None``
        for both keeps the unbounded timestamp-ordered fetch.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="query_session_related_events_after",
            )

        resolved_execution_id = execution_id or await self._resolve_execution_id_for_session(
            session_id,
        )
        session_started_at = await self._resolve_session_started_at(session_id)
        conditions = _session_related_event_conditions(session_id, resolved_execution_id)
        if not conditions:
            # Fail closed (see query_session_related_events): ``or_()`` over an empty
            # list emits NO WHERE clause and would scan the whole store. A blank scope
            # must yield no rows and leave the cursor unchanged so the poll is idempotent.
            return [], last_row_id

        try:
            async with self._engine.begin() as conn:
                rowid_col = text("rowid")
                query = (
                    select(events_table, rowid_col)
                    .where(or_(*conditions))
                    .where(text("rowid > :last_id").bindparams(last_id=last_row_id))
                )
                if max_row_id is not None:
                    query = query.where(text("rowid <= :max_id").bindparams(max_id=max_row_id))
                if session_started_at is not None:
                    query = query.where(events_table.c.timestamp >= session_started_at)

                if event_type:
                    query = query.where(events_table.c.event_type == event_type)

                if limit is not None:
                    # Page by rowid (see get_events_after) so the cursor boundary
                    # is skip-safe even when timestamp order diverges from rowid.
                    query = query.order_by(rowid_col).limit(limit)
                else:
                    query = query.order_by(events_table.c.timestamp, events_table.c.id)

                result = await conn.execute(query)
                rows = result.mappings().all()
                if not rows:
                    return [], last_row_id

                events = [BaseEvent.from_db_row(dict(row)) for row in rows]
                max_rowid = max(row["rowid"] for row in rows)
                return events, max_rowid
        except Exception as e:
            raise PersistenceError(
                f"Failed to query session-related events after rowid {last_row_id}: {e}",
                operation="select",
                table="events",
                details={
                    "session_id": session_id,
                    "execution_id": resolved_execution_id,
                    "event_type": event_type,
                    "last_row_id": last_row_id,
                },
            ) from e

    async def _resolve_execution_id_for_session(self, session_id: str) -> str | None:
        """Return the execution ID referenced by a session start event, if present."""
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="resolve_execution_id_for_session",
            )

        async with self._engine.begin() as conn:
            query = (
                select(events_table)
                .where(events_table.c.aggregate_type == "session")
                .where(events_table.c.aggregate_id == session_id)
                .where(events_table.c.event_type == "orchestrator.session.started")
                .order_by(events_table.c.timestamp.asc())
                .limit(1)
            )
            result = await conn.execute(query)
            row = result.mappings().first()
            if row is None:
                return None
            payload = row.get("payload")
            if isinstance(payload, Mapping):
                execution_id = payload.get("execution_id")
                if isinstance(execution_id, str) and execution_id:
                    return execution_id
            return None

    async def _resolve_session_started_at(self, session_id: str) -> Any | None:
        """Return the persisted start timestamp for a session, if available."""
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="resolve_session_started_at",
            )

        async with self._engine.begin() as conn:
            query = (
                select(events_table.c.timestamp)
                .where(events_table.c.aggregate_type == "session")
                .where(events_table.c.aggregate_id == session_id)
                .where(events_table.c.event_type == "orchestrator.session.started")
                .order_by(events_table.c.timestamp.asc())
                .limit(1)
            )
            result = await conn.execute(query)
            row = result.first()
            return row[0] if row is not None else None

    async def get_all_lineages(self) -> list[BaseEvent]:
        """Get all lineage creation events.

        Retrieves all events of type 'lineage.created' to identify every
        evolutionary lineage recorded in the event store.

        Returns:
            List of lineage creation events, ordered by timestamp descending.

        Raises:
            PersistenceError: If the query fails.
        """
        if self._engine is None:
            raise PersistenceError(
                "EventStore not initialized. Call initialize() first.",
                operation="get_all_lineages",
            )

        try:
            async with self._engine.begin() as conn:
                query = (
                    select(events_table)
                    .where(events_table.c.event_type == "lineage.created")
                    .order_by(events_table.c.timestamp.desc())
                )

                result = await conn.execute(query)
                rows = result.mappings().all()
                return [BaseEvent.from_db_row(dict(row)) for row in rows]
        except Exception as e:
            raise PersistenceError(
                f"Failed to get all lineages: {e}",
                operation="select",
                table="events",
                details={"event_type": "lineage.created"},
            ) from e

    async def append_workflow_lifecycle_event(
        self,
        lifecycle_event: WorkflowLifecycleEvent,
    ) -> None:
        """Append a workflow IR lifecycle event to the durable event family.

        This is an additive registration of the #956 workflow lifecycle
        event family. The helper accepts a
        ``WorkflowLifecycleEvent`` and persists it through the existing
        :meth:`append` path so no new database column or table is
        introduced. The event is routed under the
        ``WORKFLOW_LIFECYCLE_AGGREGATE_TYPE`` aggregate, leaving all other
        event families untouched.

        Args:
            lifecycle_event: A
                :class:`ouroboros.orchestrator.workflow_lifecycle.WorkflowLifecycleEvent`.
                Imported lazily to avoid an import cycle between
                ``persistence`` and ``orchestrator``.

        Raises:
            PersistenceError: If the append operation fails.
        """
        from ouroboros.orchestrator.workflow_lifecycle import (
            WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
            WORKFLOW_LIFECYCLE_EVENT_TYPES,
            WorkflowLifecycleEvent,
        )

        if not isinstance(lifecycle_event, WorkflowLifecycleEvent):
            raise PersistenceError(
                "append_workflow_lifecycle_event requires a WorkflowLifecycleEvent.",
                operation="append_workflow_lifecycle_event",
                details={"received_type": type(lifecycle_event).__name__},
            )

        base_event = lifecycle_event.to_base_event()
        # Defensive registration check: every persisted lifecycle row
        # belongs to the workflow IR family and uses a registered event
        # type. Foreign or unknown event types must be rejected before
        # they reach the existing event-store sanitization layer.
        if base_event.aggregate_type != WORKFLOW_LIFECYCLE_AGGREGATE_TYPE:
            raise PersistenceError(
                "Workflow lifecycle event has an unexpected aggregate_type.",
                operation="append_workflow_lifecycle_event",
                details={
                    "expected": WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
                    "received": base_event.aggregate_type,
                },
            )
        if base_event.type not in WORKFLOW_LIFECYCLE_EVENT_TYPES:
            raise PersistenceError(
                "Workflow lifecycle event_type is not registered.",
                operation="append_workflow_lifecycle_event",
                details={"event_type": base_event.type},
            )
        # Bypass the ``workflow_ir`` guard in :meth:`append` because the
        # caller-side ``WorkflowLifecycleEvent`` validation above is the
        # authoritative redaction boundary. The guard exists to refuse
        # *direct* raw appends; this helper has already enforced the
        # equivalent invariants.
        await self.append(base_event, _skip_workflow_ir_guard=True)

    async def replay_workflow_lifecycle(
        self,
        workflow_id: str,
    ) -> list[WorkflowLifecycleEvent]:
        """Replay durable workflow lifecycle events for a workflow id.

        Returns a list of
        :class:`ouroboros.orchestrator.workflow_lifecycle.WorkflowLifecycleEvent`
        instances rehydrated from persisted ``BaseEvent`` rows. Other
        event families are not consulted.
        """
        from pydantic import ValidationError

        from ouroboros.orchestrator.workflow_lifecycle import (
            WORKFLOW_LIFECYCLE_AGGREGATE_TYPE,
            WorkflowLifecycleEvent,
        )

        base_events = await self.replay(WORKFLOW_LIFECYCLE_AGGREGATE_TYPE, workflow_id)
        rehydrated: list[WorkflowLifecycleEvent] = []
        for base in base_events:
            try:
                rehydrated.append(WorkflowLifecycleEvent.from_base_event(base))
            except (ValueError, ValidationError) as exc:
                # A malformed row must not crash the entire replay. The
                # :meth:`append` guard prevents new bypasses, but historical
                # rows from before that guard (or rows inserted via direct
                # SQL during recovery) can still fail strict rehydration —
                # skip and log so the rest of the slice remains usable.
                # The raw exception text can echo back replay-unsafe payload
                # values when the malformed row was populated from a poisoned
                # source, so emit only safe metadata and the exception class.
                logger.warning(
                    "event_store.replay_workflow_lifecycle.skip_malformed",
                    extra={
                        "event_id": getattr(base, "id", None),
                        "event_type": getattr(base, "type", None),
                        "aggregate_id": getattr(base, "aggregate_id", None),
                        "error": type(exc).__name__,
                    },
                )
                continue
        return rehydrated

    async def replay_lineage(self, lineage_id: str) -> list[BaseEvent]:
        """Replay all events for a lineage aggregate.

        Convenience method for evolutionary loop lineage reconstruction.

        Args:
            lineage_id: The unique identifier of the lineage.

        Returns:
            List of lineage events, ordered by timestamp.

        Raises:
            PersistenceError: If the replay operation fails.
        """
        return await self.replay("lineage", lineage_id)

    async def checkpoint_wal(self) -> bool:
        """Best-effort WAL TRUNCATE checkpoint without closing the store.

        Collapses the WAL back into the main DB. Without an explicit TRUNCATE
        checkpoint, long-lived multi-connection setups (many concurrent
        MCP/TUI sessions) let the -wal file grow unbounded: passive
        autocheckpoints cannot truncate while any other reader is active, so
        the file only ever appends. Long-lived *idle* servers can call this
        periodically so they stop pinning a growing WAL between requests.

        Uses a short per-connection busy_timeout instead of the 30s default
        (set in initialize()): the checkpoint needs the write lock, and a
        peer process mid-write could otherwise stall the caller for up to
        30s. A missed checkpoint is harmless — a later attempt retries — so
        the wait is capped at ~2s. ``connect()`` (not ``begin()``) avoids an
        upfront BEGIN IMMEDIATE; the checkpoint acquires its own lock under
        the bounded timeout.

        Returns:
            True when the checkpoint ran, False when skipped or it failed
            (read-only store, no engine, lock contention).
        """
        if self._engine is None or self._read_only:
            return False
        try:
            async with self._engine.connect() as conn:
                await conn.exec_driver_sql("PRAGMA busy_timeout=2000")
                await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            return False
        return True

    async def close(self) -> None:
        """Close the database connection."""
        if self._engine is not None:
            # Collapse the WAL before disposing so the -wal file does not
            # survive shutdown. Best effort — see checkpoint_wal().
            await self.checkpoint_wal()
            await self._engine.dispose()
            self._engine = None
        if self._memory_keepalive is not None:
            # Release the shared in-memory database anchor last so pooled
            # connections never observe the database disappearing mid-dispose.
            try:
                self._memory_keepalive.close()
            finally:
                self._memory_keepalive = None


@dataclass(frozen=True, slots=True)
class SessionActivitySnapshot:
    """Session start/activity/status summary used by orphan detection."""

    session_id: str
    execution_id: str | None
    seed_id: str | None
    start_time: str | None
    last_activity: object
    status_event_type: str | None
    runtime_status: str | None
