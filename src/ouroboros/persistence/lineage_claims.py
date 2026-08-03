"""Durable same-lineage advancement claims shared by EventStore instances."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ouroboros.core.errors import PersistenceError
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import lineage_advancement_claims_table

DEFAULT_LEASE_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class ClaimObservation:
    generation_number: int
    owner_id: str
    request_key: str
    acquired: bool
    lease_expires_at_ms: int
    waiter_registered: bool = False
    completed: bool = False
    result_payload: dict[str, Any] | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _lease_ms(seconds: float | None = None) -> int:
    duration = DEFAULT_LEASE_SECONDS if seconds is None else seconds
    return _now_ms() + max(1, int(duration * 1000))


def _engine(event_store: EventStore) -> AsyncEngine | None:
    engine = getattr(event_store, "_engine", None)
    return engine if isinstance(engine, AsyncEngine) else None


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
    engine = _engine(event_store)
    if engine is None:
        raise PersistenceError(
            "EventStore must be initialized before lineage claim acquisition",
            operation="claim_lineage_advancement",
        )
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
                now_ms = _now_ms()
                waiter_count = int(row["waiter_count"]) if row is not None else 0
                receipt_drained = row is not None and (
                    waiter_count == 0 or int(row["lease_expires_at_ms"]) <= now_ms
                )
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
                        update(lineage_advancement_claims_table)
                        .where(
                            lineage_advancement_claims_table.c.scope == scope,
                            lineage_advancement_claims_table.c.lineage_id == lineage_id,
                            lineage_advancement_claims_table.c.owner_id == row["owner_id"],
                        )
                        .values(waiter_count=lineage_advancement_claims_table.c.waiter_count + 1)
                    )
                    await _commit(connection, transaction)
                    return ClaimObservation(
                        int(row["generation_number"]),
                        str(row["owner_id"]),
                        str(row["request_key"]),
                        acquired=False,
                        lease_expires_at_ms=int(row["lease_expires_at_ms"]),
                        waiter_registered=True,
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


async def renew(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
) -> bool:
    """Renew an active claim lease owned by this caller."""
    engine = _engine(event_store)
    if engine is None:
        return False
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


async def observe(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
) -> ClaimObservation | None:
    """Read the current owner and optional completed result."""
    engine = _engine(event_store)
    if engine is None:
        return None
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
    engine = _engine(event_store)
    if engine is None:
        return False
    async with engine.connect() as connection:
        transaction = await _begin_write(connection)
        row = (
            await connection.execute(
                select(lineage_advancement_claims_table.c.waiter_count).where(
                    lineage_advancement_claims_table.c.scope == scope,
                    lineage_advancement_claims_table.c.lineage_id == lineage_id,
                    lineage_advancement_claims_table.c.owner_id == owner_id,
                )
            )
        ).first()
        if row is None:
            await _commit(connection, transaction)
            return False
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
    engine = _engine(event_store)
    if engine is None:
        return
    async with engine.begin() as connection:
        await connection.execute(
            delete(lineage_advancement_claims_table).where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.owner_id == owner_id,
                lineage_advancement_claims_table.c.completed.is_(False),
            )
        )


async def recover_expired(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
) -> bool:
    """Explicitly clear an expired unfinished claim after operator confirmation."""
    engine = _engine(event_store)
    if engine is None:
        return False
    async with engine.begin() as connection:
        result = await connection.execute(
            delete(lineage_advancement_claims_table).where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.completed.is_(False),
                lineage_advancement_claims_table.c.lease_expires_at_ms <= _now_ms(),
            )
        )
    return bool(result.rowcount)


async def acknowledge_waiter(
    event_store: EventStore,
    *,
    scope: str,
    lineage_id: str,
    owner_id: str,
) -> None:
    """Atomically consume one registered waiter without a lost update."""
    engine = _engine(event_store)
    if engine is None:
        return
    async with engine.begin() as connection:
        await connection.execute(
            update(lineage_advancement_claims_table)
            .where(
                lineage_advancement_claims_table.c.scope == scope,
                lineage_advancement_claims_table.c.lineage_id == lineage_id,
                lineage_advancement_claims_table.c.owner_id == owner_id,
                lineage_advancement_claims_table.c.waiter_count > 0,
            )
            .values(waiter_count=lineage_advancement_claims_table.c.waiter_count - 1)
        )
