"""Durable single-owner leases for evolve_step generations (#1889).

Two concurrent ``evolve_step`` calls can replay the same lineage state and
select the same generation number; without a durable claim both would run
the external executor and append duplicate completion events. The claim
here is decided at the database boundary so it holds across loop instances
and processes sharing one store:

- ``acquire`` inserts the (lineage, generation) lease inside a single write
  transaction; the composite primary key makes a concurrent second insert
  lose deterministically.
- The owner refreshes the lease while working; ``release`` deletes it on
  exit, including on interruption, so resume follows immediately.
- A lease that stops being refreshed for ``lease_seconds`` is presumed
  crashed and may be reclaimed — the steal is a compare-and-set on the
  observed token, so two reclaimers cannot both win. A live owner whose
  refresh then fails observes the loss and stops claiming ownership.

Stores that expose no database URL (unit-test fakes) or an in-memory URL
(private to one process by construction) fall back to a per-process claim
table with identical semantics.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
import time
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
import structlog

from ouroboros.persistence.schema import lineage_generation_claims_table, metadata

log = structlog.get_logger(__name__)

DEFAULT_LEASE_SECONDS = 300.0


class GenerationClaimDenied(Exception):
    """Another caller currently owns this lineage generation."""

    def __init__(self, lineage_id: str, generation_number: int) -> None:
        super().__init__(
            f"generation {generation_number} of lineage {lineage_id} is owned by a "
            "concurrent evolve_step; it becomes reclaimable once the owner completes "
            "or its lease expires without a heartbeat"
        )
        self.lineage_id = lineage_id
        self.generation_number = generation_number


class GenerationClaims(Protocol):
    """One-owner-at-a-time lease over (lineage_id, generation_number)."""

    lease_seconds: float

    async def acquire(self, lineage_id: str, generation_number: int, claim_token: str) -> bool: ...

    async def refresh(self, lineage_id: str, generation_number: int, claim_token: str) -> bool: ...

    async def release(self, lineage_id: str, generation_number: int, claim_token: str) -> None: ...


class DurableGenerationClaims:
    """Database-backed claims shared by every process using one store URL."""

    def __init__(self, database_url: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine | None = None
        self._engine_lock = asyncio.Lock()
        self.lease_seconds = lease_seconds

    async def _engine_once(self) -> AsyncEngine:
        async with self._engine_lock:
            if self._engine is None:
                # NullPool: claims are touched a handful of times per
                # generation, and holding no pooled connections means this
                # side table needs no lifecycle hook of its own.
                engine = create_async_engine(self._database_url, poolclass=NullPool)
                async with engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
                self._engine = engine
            return self._engine

    async def acquire(self, lineage_id: str, generation_number: int, claim_token: str) -> bool:
        engine = await self._engine_once()
        table = lineage_generation_claims_table
        now = time.time()
        async with engine.connect() as conn:
            # SQLite needs an immediate write transaction: a deferred SELECT
            # followed by INSERT leaves a gap in which a second caller can
            # commit the competing claim. The primary-key guard below closes
            # the same absent-row race on every backend.
            sqlite = conn.dialect.name == "sqlite"
            if sqlite:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                await conn.begin()
            try:
                row = (
                    await conn.execute(
                        select(table.c.claim_token, table.c.refreshed_at).where(
                            table.c.lineage_id == lineage_id,
                            table.c.generation_number == generation_number,
                        )
                    )
                ).first()
                if row is None:
                    try:
                        async with conn.begin_nested():
                            await conn.execute(
                                table.insert().values(
                                    lineage_id=lineage_id,
                                    generation_number=generation_number,
                                    claim_token=claim_token,
                                    refreshed_at=now,
                                )
                            )
                    except IntegrityError:
                        # A concurrent caller won the primary-key guard.
                        if conn.in_transaction():
                            await conn.rollback()
                        return False
                    await conn.commit()
                    return True
                observed_token, refreshed_at = row
                if observed_token == claim_token:
                    # Re-entrant acquire by the current owner refreshes.
                    await conn.execute(
                        update(table)
                        .where(
                            table.c.lineage_id == lineage_id,
                            table.c.generation_number == generation_number,
                            table.c.claim_token == claim_token,
                        )
                        .values(refreshed_at=now)
                    )
                    await conn.commit()
                    return True
                if now - float(refreshed_at) > self.lease_seconds:
                    # Presumed-crashed owner: steal by CAS on the token we
                    # observed, so two concurrent reclaimers cannot both win.
                    stolen = await conn.execute(
                        update(table)
                        .where(
                            table.c.lineage_id == lineage_id,
                            table.c.generation_number == generation_number,
                            table.c.claim_token == observed_token,
                        )
                        .values(claim_token=claim_token, refreshed_at=now)
                    )
                    await conn.commit()
                    return stolen.rowcount == 1
                await conn.rollback()
                return False
            except BaseException:
                if conn.in_transaction():
                    await conn.rollback()
                raise

    async def refresh(self, lineage_id: str, generation_number: int, claim_token: str) -> bool:
        engine = await self._engine_once()
        table = lineage_generation_claims_table
        async with engine.begin() as conn:
            refreshed = await conn.execute(
                update(table)
                .where(
                    table.c.lineage_id == lineage_id,
                    table.c.generation_number == generation_number,
                    table.c.claim_token == claim_token,
                )
                .values(refreshed_at=time.time())
            )
            return refreshed.rowcount == 1

    async def release(self, lineage_id: str, generation_number: int, claim_token: str) -> None:
        engine = await self._engine_once()
        table = lineage_generation_claims_table
        async with engine.begin() as conn:
            await conn.execute(
                delete(table).where(
                    table.c.lineage_id == lineage_id,
                    table.c.generation_number == generation_number,
                    table.c.claim_token == claim_token,
                )
            )


class LocalGenerationClaims:
    """Per-process claims for stores that cannot host the durable table."""

    def __init__(self, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self.lease_seconds = lease_seconds
        self._claims: dict[tuple[str, int], tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, lineage_id: str, generation_number: int, claim_token: str) -> bool:
        key = (lineage_id, generation_number)
        now = time.monotonic()
        async with self._lock:
            held = self._claims.get(key)
            if held is None or held[0] == claim_token or now - held[1] > self.lease_seconds:
                self._claims[key] = (claim_token, now)
                return True
            return False

    async def refresh(self, lineage_id: str, generation_number: int, claim_token: str) -> bool:
        key = (lineage_id, generation_number)
        async with self._lock:
            held = self._claims.get(key)
            if held is None or held[0] != claim_token:
                return False
            self._claims[key] = (claim_token, time.monotonic())
            return True

    async def release(self, lineage_id: str, generation_number: int, claim_token: str) -> None:
        key = (lineage_id, generation_number)
        async with self._lock:
            held = self._claims.get(key)
            if held is not None and held[0] == claim_token:
                del self._claims[key]


_CLAIMS_BY_URL: dict[str, DurableGenerationClaims] = {}
_LOCAL_CLAIMS = LocalGenerationClaims()


def generation_claims_for(event_store: Any) -> GenerationClaims:
    """Resolve the claims backend for a store, cached per database URL.

    Only a store with a real database URL can share claims across processes;
    in-memory URLs are private to one process by construction and unit-test
    fakes expose no URL at all, so both use the per-process table.
    """
    url = getattr(event_store, "database_url", None)
    if not isinstance(url, str) or "://" not in url or ":memory:" in url:
        return _LOCAL_CLAIMS
    claims = _CLAIMS_BY_URL.get(url)
    if claims is None:
        claims = DurableGenerationClaims(url)
        _CLAIMS_BY_URL[url] = claims
    return claims


@asynccontextmanager
async def owned_generation(
    claims: GenerationClaims,
    lineage_id: str,
    generation_number: int,
    *,
    heartbeat_interval: float | None = None,
) -> AsyncIterator[None]:
    """Own one generation for the duration of the block.

    Raises :class:`GenerationClaimDenied` before yielding when another
    caller holds the claim. While the block runs, the lease is refreshed on
    a heartbeat; if the refresh reports the claim was reclaimed (this owner
    was presumed crashed), the loss is logged and heartbeating stops — the
    reclaimer is authoritative from that point. The claim is always released
    on exit, so interruption hands ownership straight to the resuming call.
    """
    claim_token = uuid4().hex
    if not await claims.acquire(lineage_id, generation_number, claim_token):
        raise GenerationClaimDenied(lineage_id, generation_number)
    stop = asyncio.Event()
    interval = heartbeat_interval if heartbeat_interval is not None else claims.lease_seconds / 3

    async def _heartbeat() -> None:
        while True:
            with suppress(asyncio.TimeoutError, TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            if not await claims.refresh(lineage_id, generation_number, claim_token):
                log.warning(
                    "evolve.generation_claim.lost",
                    lineage_id=lineage_id,
                    generation_number=generation_number,
                )
                return

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        yield
    finally:
        stop.set()
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
        await claims.release(lineage_id, generation_number, claim_token)
