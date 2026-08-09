"""Durable same-lineage advancement claims shared by EventStore instances."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
import time
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ouroboros.core.errors import PersistenceError
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import (
    lineage_advancement_claims_table,
    lineage_advancement_waiters_table,
)

DEFAULT_LEASE_SECONDS = 120.0
WAITER_LEASE_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ClaimObservation:
    generation_number: int
    owner_id: str
    request_key: str
    acquired: bool
    lease_expires_at_ms: int
    waiter_registered: bool = False
    waiter_id: str | None = None
    completed: bool = False
    result_payload: dict[str, Any] | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _lease_ms(seconds: float | None = None) -> int:
    duration = DEFAULT_LEASE_SECONDS if seconds is None else seconds
    return _now_ms() + max(1, int(duration * 1000))


def _waiter_lease_ms() -> int:
    return _now_ms() + max(1, int(WAITER_LEASE_SECONDS * 1000))


def _engine(event_store: EventStore) -> AsyncEngine | None:
    engine = getattr(event_store, "_engine", None)
    return engine if isinstance(engine, AsyncEngine) else None


def _has_declared_settlement(event_store: object) -> bool:
    """Return whether the store type declares the transactional lifecycle fence."""
    settle = getattr(event_store, "settle_transactional_write", None)
    declared_settle = getattr(type(event_store), "settle_transactional_write", None)
    return callable(declared_settle) and callable(settle)


def _can_settle_claim_transaction(event_store: EventStore) -> bool:
    """Keep lightweight fallbacks while routing real stores through their fence."""
    return _has_declared_settlement(event_store) or _engine(event_store) is not None


async def _settle_claim_transaction[T](
    event_store: EventStore,
    transaction: Callable[[AsyncEngine], Coroutine[Any, Any, T]],
    *,
    operation: str,
) -> T:
    """Run one claim transaction inside the EventStore lifecycle fence."""
    settle = getattr(event_store, "settle_transactional_write", None)
    if _has_declared_settlement(event_store) and callable(settle):
        return await settle(transaction, operation=operation)
    engine = _engine(event_store)
    if engine is None:
        raise PersistenceError(
            "EventStore must be initialized before lineage claim mutation",
            operation=operation,
        )
    return await transaction(engine)


def supports_durable_claims(event_store: object) -> bool:
    """Return whether this is the production EventStore authority."""
    return isinstance(event_store, EventStore)


def _receipt_allows_retry(payload: object) -> bool:
    """Return whether a completed receipt represents resumable work."""
    if not isinstance(payload, dict):
        return False
    if payload.get("ok") is False:
        return True
    return payload.get("action") in {"failed", "interrupted"}


async def _begin_write(connection: AsyncConnection) -> Any:
    if connection.dialect.name == "sqlite":
        await connection.exec_driver_sql("BEGIN IMMEDIATE")
        return None
    return await connection.begin()


async def _commit(connection: AsyncConnection, transaction: Any) -> None:
    if connection.dialect.name == "sqlite":
        await connection.commit()
    elif transaction is not None:
        await transaction.commit()


async def _prune_and_count_waiters(
    connection: AsyncConnection,
    *,
    scope: str,
    lineage_id: str,
    claim_owner_id: str,
) -> int:
    """Remove expired registrations and return the live waiter cardinality."""
    await connection.execute(
        delete(lineage_advancement_waiters_table).where(
            lineage_advancement_waiters_table.c.scope == scope,
            lineage_advancement_waiters_table.c.lineage_id == lineage_id,
            lineage_advancement_waiters_table.c.claim_owner_id == claim_owner_id,
            lineage_advancement_waiters_table.c.lease_expires_at_ms <= _now_ms(),
        )
    )
    count = await connection.scalar(
        select(func.count())
        .select_from(lineage_advancement_waiters_table)
        .where(
            lineage_advancement_waiters_table.c.scope == scope,
            lineage_advancement_waiters_table.c.lineage_id == lineage_id,
            lineage_advancement_waiters_table.c.claim_owner_id == claim_owner_id,
        )
    )
    return int(count or 0)


async def _delete_claim_waiters(
    connection: AsyncConnection,
    *,
    scope: str,
    lineage_id: str,
) -> None:
    await connection.execute(
        delete(lineage_advancement_waiters_table).where(
            lineage_advancement_waiters_table.c.scope == scope,
            lineage_advancement_waiters_table.c.lineage_id == lineage_id,
        )
    )


async def try_acquire(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    generation_number: int,
    owner_id: str,
    request_key: str,
) -> ClaimObservation | None:
    """Acquire a lineage claim or register as a waiter on its active owner."""
    if not _can_settle_claim_transaction(event_store):
        raise PersistenceError(
            "EventStore must be initialized before lineage claim acquisition",
            operation="claim_lineage_advancement",
        )
    loop = asyncio.get_running_loop()
    result: asyncio.Future[ClaimObservation] = loop.create_future()
    decision: asyncio.Future[bool] = loop.create_future()

    def consume_result_failure(future: asyncio.Future[ClaimObservation]) -> None:
        if not future.cancelled():
            future.exception()

    result.add_done_callback(consume_result_failure)

    async def settled_acquisition(settled_engine: AsyncEngine) -> ClaimObservation:
        try:
            observation = await _try_acquire(
                settled_engine,
                scope=scope,
                lineage_id=lineage_id,
                generation_number=generation_number,
                owner_id=owner_id,
                request_key=request_key,
            )
        except BaseException as exc:
            if not result.done():
                result.set_exception(exc)
            raise
        if not result.done():
            result.set_result(observation)
        if not await decision:
            await _discard_claim_participation(
                settled_engine,
                scope=scope,
                lineage_id=lineage_id,
                participant_id=owner_id,
            )
        return observation

    settlement = asyncio.create_task(
        _settle_claim_transaction(
            event_store,
            settled_acquisition,
            operation="claim_lineage_advancement",
        )
    )

    def publish_settlement_failure(task: asyncio.Task[ClaimObservation]) -> None:
        if result.done():
            if not task.cancelled():
                task.exception()
            return
        if task.cancelled():
            result.cancel()
            return
        error = task.exception()
        if error is not None:
            result.set_exception(error)

    settlement.add_done_callback(publish_settlement_failure)
    try:
        observation = await asyncio.shield(result)
    except asyncio.CancelledError as cancellation:
        if not decision.done():
            decision.set_result(False)
        settlement_error: BaseException | None = None
        while not settlement.done():
            try:
                await asyncio.shield(settlement)
            except asyncio.CancelledError:
                continue
            except Exception as exc:
                settlement_error = exc
                break
        if settlement_error is None and not settlement.cancelled():
            settlement_error = settlement.exception()
        if settlement_error is not None:
            raise cancellation from settlement_error
        raise cancellation
    decision.set_result(True)
    return observation


async def _try_acquire(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    generation_number: int,
    owner_id: str,
    request_key: str,
) -> ClaimObservation:
    """Execute the complete retry loop as one cancellation-settled write."""
    for _attempt in range(3):
        try:
            async with engine.connect() as connection:
                transaction = await _begin_write(connection)
                row = (
                    (
                        await connection.execute(
                            select(lineage_advancement_claims_table)
                            .where(
                                lineage_advancement_claims_table.c.scope == scope,
                                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                            )
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .first()
                )
                waiter_count = 0
                if row is not None:
                    waiter_count = await _prune_and_count_waiters(
                        connection,
                        scope=scope,
                        lineage_id=lineage_id,
                        claim_owner_id=str(row["owner_id"]),
                    )
                    if waiter_count != int(row["waiter_count"]):
                        await connection.execute(
                            update(lineage_advancement_claims_table)
                            .where(
                                lineage_advancement_claims_table.c.scope == scope,
                                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                                lineage_advancement_claims_table.c.owner_id == row["owner_id"],
                            )
                            .values(waiter_count=waiter_count)
                        )
                # A completed receipt is exact winner authority for every live
                # waiter registered on its owner.  Short receipt-lease expiry
                # alone cannot revoke it; individually leased waiter rows
                # distinguish a delayed live process from a hard-crashed one.
                receipt_drained = row is not None and waiter_count == 0
                replaceable = row is None or (
                    bool(row["completed"])
                    and receipt_drained
                    and (
                        _receipt_allows_retry(row["result_payload"])
                        or int(row["generation_number"]) != generation_number
                        or str(row["request_key"]) != request_key
                    )
                )
                lease_expires_at_ms = _lease_ms()
                values = {
                    "owner_id": owner_id,
                    "generation_number": generation_number,
                    "request_key": request_key,
                    "lease_expires_at_ms": lease_expires_at_ms,
                    "waiter_count": 0,
                    "completed": False,
                    "result_payload": None,
                }
                if replaceable:
                    await _delete_claim_waiters(
                        connection,
                        scope=scope,
                        lineage_id=lineage_id,
                    )
                    if row is None:
                        await connection.execute(
                            insert(lineage_advancement_claims_table).values(
                                scope=scope,
                                lineage_id=lineage_id,
                                **values,
                            )
                        )
                    else:
                        await connection.execute(
                            update(lineage_advancement_claims_table)
                            .where(
                                lineage_advancement_claims_table.c.scope == scope,
                                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                            )
                            .values(**values)
                        )
                    await _commit(connection, transaction)
                    return ClaimObservation(
                        generation_number,
                        owner_id,
                        request_key,
                        acquired=True,
                        lease_expires_at_ms=lease_expires_at_ms,
                    )

                assert row is not None
                if not bool(row["completed"]):
                    await connection.execute(
                        insert(lineage_advancement_waiters_table).values(
                            scope=scope,
                            lineage_id=lineage_id,
                            claim_owner_id=row["owner_id"],
                            waiter_id=owner_id,
                            lease_expires_at_ms=_waiter_lease_ms(),
                        )
                    )
                    await connection.execute(
                        update(lineage_advancement_claims_table)
                        .where(
                            lineage_advancement_claims_table.c.scope == scope,
                            lineage_advancement_claims_table.c.lineage_id == lineage_id,
                            lineage_advancement_claims_table.c.owner_id == row["owner_id"],
                        )
                        .values(waiter_count=waiter_count + 1)
                    )
                    await _commit(connection, transaction)
                    return ClaimObservation(
                        int(row["generation_number"]),
                        str(row["owner_id"]),
                        str(row["request_key"]),
                        acquired=False,
                        lease_expires_at_ms=int(row["lease_expires_at_ms"]),
                        waiter_registered=True,
                        waiter_id=owner_id,
                    )

                await _commit(connection, transaction)
                return ClaimObservation(
                    int(row["generation_number"]),
                    str(row["owner_id"]),
                    str(row["request_key"]),
                    acquired=False,
                    lease_expires_at_ms=int(row["lease_expires_at_ms"]),
                    completed=True,
                    result_payload=(
                        dict(row["result_payload"])
                        if isinstance(row["result_payload"], dict)
                        else None
                    ),
                )
        except IntegrityError:
            continue
    raise PersistenceError(
        "Could not acquire durable lineage advancement claim",
        operation="claim_lineage_advancement",
        details={"scope": scope, "lineage_id": lineage_id},
    )


async def _discard_claim_participation(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    participant_id: str,
) -> None:
    """Delete one cancelled participant and repair the owner's waiter count."""
    async with engine.connect() as connection:
        transaction = await _begin_write(connection)
        row = (
            (
                await connection.execute(
                    select(lineage_advancement_claims_table).where(
                        lineage_advancement_claims_table.c.scope == scope,
                        lineage_advancement_claims_table.c.lineage_id == lineage_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            await _commit(connection, transaction)
            return

        claim_owner_id = str(row["owner_id"])
        if claim_owner_id == participant_id and not bool(row["completed"]):
            await connection.execute(
                delete(lineage_advancement_claims_table).where(
                    lineage_advancement_claims_table.c.scope == scope,
                    lineage_advancement_claims_table.c.lineage_id == lineage_id,
                    lineage_advancement_claims_table.c.owner_id == participant_id,
                    lineage_advancement_claims_table.c.completed.is_(False),
                )
            )
            await _delete_claim_waiters(connection, scope=scope, lineage_id=lineage_id)
            await _commit(connection, transaction)
            return

        removed = await connection.execute(
            delete(lineage_advancement_waiters_table).where(
                lineage_advancement_waiters_table.c.scope == scope,
                lineage_advancement_waiters_table.c.lineage_id == lineage_id,
                lineage_advancement_waiters_table.c.claim_owner_id == claim_owner_id,
                lineage_advancement_waiters_table.c.waiter_id == participant_id,
            )
        )
        if removed.rowcount:
            waiter_count = await _prune_and_count_waiters(
                connection,
                scope=scope,
                lineage_id=lineage_id,
                claim_owner_id=claim_owner_id,
            )
            await connection.execute(
                update(lineage_advancement_claims_table)
                .where(
                    lineage_advancement_claims_table.c.scope == scope,
                    lineage_advancement_claims_table.c.lineage_id == lineage_id,
                    lineage_advancement_claims_table.c.owner_id == claim_owner_id,
                )
                .values(waiter_count=waiter_count)
            )
        await _commit(connection, transaction)


async def renew(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
) -> bool:
    """Renew an active claim lease owned by this caller."""
    if not _can_settle_claim_transaction(event_store):
        return False
    return await _settle_claim_transaction(
        event_store,
        lambda settled_engine: _renew(
            settled_engine,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
        ),
        operation="renew_lineage_claim",
    )


async def _renew(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
) -> bool:
    async with engine.begin() as connection:
        result = await connection.execute(
            update(lineage_advancement_claims_table)
            .where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.owner_id == owner_id,
                lineage_advancement_claims_table.c.completed.is_(False),
            )
            .values(lease_expires_at_ms=_lease_ms())
        )
    return bool(result.rowcount)


async def rebind_generation(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    generation_number: int,
) -> bool:
    """Bind an unfinished outer receipt to the generation its core claim owns."""
    if not _has_declared_settlement(event_store):
        return False
    return await _settle_claim_transaction(
        event_store,
        lambda engine: _rebind_generation(
            engine,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
            generation_number=generation_number,
        ),
        operation="rebind_lineage_generation",
    )


async def _rebind_generation(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    generation_number: int,
) -> bool:
    async with engine.begin() as connection:
        result = await _update_generation(
            connection,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
            generation_number=generation_number,
        )
    return bool(result.rowcount)


async def _update_generation(
    connection: AsyncConnection,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    generation_number: int,
) -> Any:
    return await connection.execute(
        update(lineage_advancement_claims_table)
        .where(
            lineage_advancement_claims_table.c.scope == scope,
            lineage_advancement_claims_table.c.lineage_id == lineage_id,
            lineage_advancement_claims_table.c.owner_id == owner_id,
            lineage_advancement_claims_table.c.completed.is_(False),
            lineage_advancement_claims_table.c.lease_expires_at_ms > _now_ms(),
        )
        .values(generation_number=generation_number)
    )


async def observe(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
) -> ClaimObservation | None:
    """Read the current owner and optional completed result."""
    if not _can_settle_claim_transaction(event_store):
        return None
    return await _settle_claim_transaction(
        event_store,
        lambda settled_engine: _observe(
            settled_engine,
            scope=scope,
            lineage_id=lineage_id,
        ),
        operation="observe_lineage_claim",
    )


async def _observe(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
) -> ClaimObservation | None:
    async with engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    select(lineage_advancement_claims_table).where(
                        lineage_advancement_claims_table.c.scope == scope,
                        lineage_advancement_claims_table.c.lineage_id == lineage_id,
                    )
                )
            )
            .mappings()
            .first()
        )
    if row is None:
        return None
    payload = row["result_payload"]
    return ClaimObservation(
        int(row["generation_number"]),
        str(row["owner_id"]),
        str(row["request_key"]),
        acquired=False,
        lease_expires_at_ms=int(row["lease_expires_at_ms"]),
        completed=bool(row["completed"]),
        result_payload=dict(payload) if isinstance(payload, dict) else None,
    )


async def complete(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    result_payload: dict[str, Any],
) -> bool:
    """Publish a result only when registered waiters need to replay it."""
    if not _can_settle_claim_transaction(event_store):
        return False
    return await _settle_claim_transaction(
        event_store,
        lambda settled_engine: _complete(
            settled_engine,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
            result_payload=result_payload,
        ),
        operation="complete_lineage_claim",
    )


async def _complete(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    result_payload: dict[str, Any],
) -> bool:
    async with engine.connect() as connection:
        transaction = await _begin_write(connection)
        row = (
            (
                await connection.execute(
                    select(lineage_advancement_claims_table)
                    .where(
                        lineage_advancement_claims_table.c.scope == scope,
                        lineage_advancement_claims_table.c.lineage_id == lineage_id,
                        lineage_advancement_claims_table.c.owner_id == owner_id,
                    )
                    .with_for_update()
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            await _commit(connection, transaction)
            return False
        waiter_count = await _prune_and_count_waiters(
            connection,
            scope=scope,
            lineage_id=lineage_id,
            claim_owner_id=owner_id,
        )
        result = await connection.execute(
            update(lineage_advancement_claims_table)
            .where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.owner_id == owner_id,
                lineage_advancement_claims_table.c.completed.is_(False),
                lineage_advancement_claims_table.c.lease_expires_at_ms > _now_ms(),
            )
            .values(
                completed=True,
                result_payload=result_payload,
                lease_expires_at_ms=_lease_ms(5.0),
                waiter_count=waiter_count,
            )
        )
        await _commit(connection, transaction)
    return bool(result.rowcount)


async def release(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
) -> None:
    """Release an unfinished owner claim after failure or cancellation."""
    if not _can_settle_claim_transaction(event_store):
        return
    await _settle_claim_transaction(
        event_store,
        lambda settled_engine: _release(
            settled_engine,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
        ),
        operation="release_lineage_claim",
    )


async def _release(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
) -> None:
    async with engine.begin() as connection:
        removed = await connection.execute(
            delete(lineage_advancement_claims_table).where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.owner_id == owner_id,
                lineage_advancement_claims_table.c.completed.is_(False),
            )
        )
        if removed.rowcount:
            await connection.execute(
                delete(lineage_advancement_waiters_table).where(
                    lineage_advancement_waiters_table.c.scope == scope,
                    lineage_advancement_waiters_table.c.lineage_id == lineage_id,
                    lineage_advancement_waiters_table.c.claim_owner_id == owner_id,
                )
            )


async def recover_expired(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
) -> bool:
    """Explicitly clear an expired unfinished claim after operator confirmation."""
    if not _can_settle_claim_transaction(event_store):
        return False
    return await _settle_claim_transaction(
        event_store,
        lambda settled_engine: _recover_expired(
            settled_engine,
            scope=scope,
            lineage_id=lineage_id,
        ),
        operation="recover_expired_lineage_claim",
    )


async def _recover_expired(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
) -> bool:
    async with engine.begin() as connection:
        result = await connection.execute(
            delete(lineage_advancement_claims_table).where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.completed.is_(False),
                lineage_advancement_claims_table.c.lease_expires_at_ms <= _now_ms(),
            )
        )
        if result.rowcount:
            await _delete_claim_waiters(
                connection,
                scope=scope,
                lineage_id=lineage_id,
            )
    return bool(result.rowcount)


async def renew_waiter(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    waiter_id: str,
) -> bool:
    """Renew one live waiter's independent receipt-registration lease."""
    if not _can_settle_claim_transaction(event_store):
        return False
    return await _settle_claim_transaction(
        event_store,
        lambda settled_engine: _renew_waiter(
            settled_engine,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
            waiter_id=waiter_id,
        ),
        operation="renew_lineage_waiter",
    )


async def _renew_waiter(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    waiter_id: str,
) -> bool:
    async with engine.begin() as connection:
        result = await connection.execute(
            update(lineage_advancement_waiters_table)
            .where(
                lineage_advancement_waiters_table.c.scope == scope,
                lineage_advancement_waiters_table.c.lineage_id == lineage_id,
                lineage_advancement_waiters_table.c.claim_owner_id == owner_id,
                lineage_advancement_waiters_table.c.waiter_id == waiter_id,
                lineage_advancement_waiters_table.c.lease_expires_at_ms > _now_ms(),
            )
            .values(lease_expires_at_ms=_waiter_lease_ms())
        )
    return bool(result.rowcount)


async def acknowledge_waiter(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    waiter_id: str,
) -> None:
    """Atomically consume one exact waiter registration without a lost update."""
    if not _can_settle_claim_transaction(event_store):
        return
    await _settle_claim_transaction(
        event_store,
        lambda settled_engine: _acknowledge_waiter(
            settled_engine,
            scope=scope,
            lineage_id=lineage_id,
            owner_id=owner_id,
            waiter_id=waiter_id,
        ),
        operation="acknowledge_lineage_waiter",
    )


async def _acknowledge_waiter(
    engine: AsyncEngine,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
    waiter_id: str,
) -> None:
    async with engine.connect() as connection:
        transaction = await _begin_write(connection)
        await connection.execute(
            select(lineage_advancement_claims_table.c.owner_id)
            .where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.owner_id == owner_id,
            )
            .with_for_update()
        )
        await connection.execute(
            delete(lineage_advancement_waiters_table).where(
                lineage_advancement_waiters_table.c.scope == scope,
                lineage_advancement_waiters_table.c.lineage_id == lineage_id,
                lineage_advancement_waiters_table.c.claim_owner_id == owner_id,
                lineage_advancement_waiters_table.c.waiter_id == waiter_id,
            )
        )
        waiter_count = await _prune_and_count_waiters(
            connection,
            scope=scope,
            lineage_id=lineage_id,
            claim_owner_id=owner_id,
        )
        await connection.execute(
            update(lineage_advancement_claims_table)
            .where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.owner_id == owner_id,
            )
            .values(waiter_count=waiter_count)
        )
        await _commit(connection, transaction)
