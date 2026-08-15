"""Cancellation-settled persistence writes and bounded SQLite appends.

Transactions that survive caller cancellation must finish before the caller
or EventStore lifecycle can move on.  This module owns that settlement
primitive and the SQLite-specific deadline/interrupt transaction used by
durable lifecycle publication.  EventStore retains admission, validation,
write-registry, and engine lifecycle ownership and passes the live engine into
the bounded operation only after those gates succeed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import logging
import time
from typing import Any

from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine

from ouroboros.core.errors import PersistenceError
from ouroboros.events.base import BaseEvent

logger = logging.getLogger(__name__)

InsertEvent = Callable[[Any, BaseEvent, bool], Awaitable[None]]


async def run_to_settlement[T](
    coro: Coroutine[Any, Any, T],
    *,
    registry: set[asyncio.Future[Any]] | None = None,
    refuse_when: Callable[[], bool] | None = None,
    operation: str = "append",
) -> T:
    """Run a transactional coroutine, settling it before cancellation surfaces.

    A write, once begun, must commit or roll back inside the caller's
    lifetime: shield-only semantics let a cancelled caller (and even
    ``EventStore.close()``) return while the transaction was still pending,
    so durable history could change after shutdown. Cancellation is re-raised
    only after the inner task completes, which both preserves the
    cancellation-atomicity contract and keeps every write within the store
    lifecycle (#1794 review rounds one and two).
    """
    if refuse_when is not None and refuse_when():
        # Admission is synchronized with close(): this check and the registry
        # add below run in one synchronous block on the event loop, so a
        # write either registers before close() snapshots the registry or is
        # refused outright — it can never slip past the drain (round five).
        coro.close()
        raise PersistenceError(
            "EventStore is closing; write refused.",
            operation=operation,
        )
    inner: asyncio.Task[T] = asyncio.ensure_future(coro)
    if registry is not None:
        # close() drains this registry so no settling write can escape the
        # store lifecycle (review round four).
        registry.add(inner)
        inner.add_done_callback(registry.discard)
    try:
        return await asyncio.shield(inner)
    except asyncio.CancelledError as caller_cancellation:
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                # A further caller cancellation — keep settling.
                continue
            except Exception:
                # The transaction settled by failing; the loop exits below.
                break
        settlement_error: BaseException | None = None
        if not inner.cancelled():
            settlement_error = inner.exception()
        if settlement_error is not None:
            # Cancellation outranks the write's own failure: lifecycle code
            # awaiting a cancelled task must still observe CancelledError
            # (e.g. the watchdog's decision batch depends on it). The
            # transaction failure is preserved as the cause.
            logger.debug(
                "event_store.append.settled_with_error",
                exc_info=settlement_error,
            )
            raise caller_cancellation from settlement_error
        raise


async def await_sqlite_write_atomically[T](awaitable: Coroutine[Any, Any, T]) -> T:
    """Compatibility entry point for callers needing transaction settlement."""
    return await run_to_settlement(awaitable)


async def append_with_sqlite_deadline(
    engine: AsyncEngine,
    event: BaseEvent,
    *,
    overall_deadline: float,
    picker_projection_ready: bool,
    insert_event: InsertEvent,
) -> None:
    """Run one deadline-aware SQLite insert transaction to final settlement.

    The connection, progress handler, interrupt, transaction, and cleanup all
    stay in one task so aiosqlite thread ownership cannot cross cancellation
    boundaries.  EventStore supplies its initialized engine and canonical
    event insertion callback after registering this task with its lifecycle.
    """
    loop = asyncio.get_running_loop()
    remaining = overall_deadline - loop.time()
    if remaining <= 0:
        raise TimeoutError("durable append deadline expired before transaction admission")

    # Reserve up to 250ms (half of very small test budgets) for rollback,
    # handler removal, and pool return. The SQL interrupt fires at the earlier
    # operation deadline; the public deadline includes cleanup.
    settlement_reserve = min(0.25, remaining / 2)
    operation_deadline = time.monotonic() + (remaining - settlement_reserve)
    deadline_reached = False
    committed = False
    post_commit_cleanup_error: BaseException | None = None

    def interrupt_long_sql() -> int:
        nonlocal deadline_reached
        if time.monotonic() < operation_deadline:
            return 0
        deadline_reached = True
        return 1

    try:
        async with asyncio.timeout_at(overall_deadline):
            async with engine.connect() as conn:
                raw = await conn.get_raw_connection()
                driver = raw.driver_connection
                await driver.set_progress_handler(interrupt_long_sql, 1_000)
                busy_ms = max(1, int((operation_deadline - time.monotonic()) * 1_000))
                busy_cursor = await driver.execute(f"PRAGMA busy_timeout={busy_ms}")
                await busy_cursor.close()

                interrupt_handle = loop.call_later(
                    max(0.0, operation_deadline - time.monotonic()),
                    lambda: asyncio.create_task(driver.interrupt()),
                )
                operation_error: BaseException | None = None
                try:
                    async with conn.begin():
                        await insert_event(conn, event, picker_projection_ready)
                    committed = True
                except BaseException as exc:
                    operation_error = exc
                    raise
                finally:
                    interrupt_handle.cancel()
                    cleanup_error: BaseException | None = None
                    try:
                        await driver.set_progress_handler(None, 0)
                    except asyncio.CancelledError as exc:
                        # The overall deadline can expire after COMMIT while
                        # driver cleanup is in flight.  Keep that cancellation
                        # as a cleanup failure so the still-configured progress
                        # handler can never return to the pool.
                        cleanup_error = exc
                    except Exception as exc:
                        cleanup_error = exc
                    try:
                        restore_cursor = await driver.execute("PRAGMA busy_timeout=30000")
                        await restore_cursor.close()
                    except asyncio.CancelledError as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                        else:
                            logger.warning(
                                "event_store.append.secondary_cleanup_cancelled",
                                exc_info=exc,
                            )
                    except Exception as exc:
                        if cleanup_error is None:
                            cleanup_error = exc
                        else:
                            logger.warning(
                                "event_store.append.secondary_cleanup_failed",
                                exc_info=exc,
                            )
                    if cleanup_error is not None:
                        try:
                            await conn.invalidate(cleanup_error)
                        except asyncio.CancelledError as exc:
                            logger.warning(
                                "event_store.append.connection_invalidation_cancelled",
                                exc_info=exc,
                            )
                        except Exception as exc:
                            logger.warning(
                                "event_store.append.connection_invalidation_failed",
                                exc_info=exc,
                            )
                        if committed:
                            post_commit_cleanup_error = cleanup_error
                        elif operation_error is None:
                            raise cleanup_error
                        else:
                            logger.warning(
                                "event_store.append.rollback_cleanup_failed",
                                exc_info=cleanup_error,
                            )
        if post_commit_cleanup_error is not None:
            logger.warning(
                "event_store.append.post_commit_cleanup_failed",
                exc_info=post_commit_cleanup_error,
            )
    except TimeoutError:
        if committed:
            logger.warning("event_store.append.post_commit_cleanup_timed_out")
            return
        raise
    except OperationalError as exc:
        if committed:
            logger.warning(
                "event_store.append.post_commit_cleanup_failed",
                exc_info=exc,
            )
            return
        if deadline_reached or loop.time() >= overall_deadline - settlement_reserve:
            raise TimeoutError("durable append exceeded its persistence deadline") from exc
        raise PersistenceError(
            f"Failed to append event: {exc}",
            operation="append_durable",
            table="events",
            details={"event_id": event.id, "event_type": event.type},
        ) from exc
    except Exception as exc:
        if committed:
            logger.warning(
                "event_store.append.post_commit_cleanup_failed",
                exc_info=exc,
            )
            return
        if deadline_reached:
            raise TimeoutError("durable append exceeded its persistence deadline") from exc
        if isinstance(exc, PersistenceError):
            raise
        raise PersistenceError(
            f"Failed to append event: {exc}",
            operation="append_durable",
            table="events",
            details={"event_id": event.id, "event_type": event.type},
        ) from exc
