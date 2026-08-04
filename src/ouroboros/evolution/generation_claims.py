"""Durable single-owner lease over one lineage's evolve_step boundary (#1889).

Two concurrent ``evolve_step`` calls used to replay the same lineage state,
select the same generation number, run the external executor twice, and
append duplicate completion and terminal events. The lease here serializes
the whole replay/selection/execution boundary per lineage, and it is decided
at the database boundary so it holds across loop instances and processes
sharing one store:

- The lease is acquired *before* the caller replays lineage state, so a
  loser never observes a stale snapshot, never writes ``lineage.created``
  for an attempt it cannot run, and can never select a generation number
  while another generation is still executing.
- ``acquire`` inserts the lease inside a single write transaction; the
  primary key makes a concurrent second insert lose deterministically.
- The owner heartbeats the lease while working; ``release`` deletes it on
  exit, including on interruption, so a resuming caller re-replays fresh
  state under its own lease — a released lease is an invitation to replay,
  never to reuse a previously selected generation.
- A lease that stops being refreshed for ``lease_seconds`` is presumed
  crashed and may be reclaimed. The steal is a compare-and-set on the
  observed token, so two reclaimers cannot both win. An owner that loses
  its lease is *fenced*: the loss is exposed on the yielded lease handle so
  the caller can abort its in-flight work, and heartbeating stops.

Stores that expose no database URL (unit-test fakes) or an in-memory URL
(private to one process by construction) fall back to a per-store claim
table with identical semantics.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import time
from typing import Any, Protocol
from uuid import uuid4
import weakref

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
import structlog

from ouroboros.persistence.schema import lineage_step_claims_table, metadata

log = structlog.get_logger(__name__)

DEFAULT_LEASE_SECONDS = 300.0


class LineageStepClaimDenied(Exception):
    """Another caller currently owns this lineage's evolve_step boundary."""

    def __init__(self, lineage_id: str) -> None:
        super().__init__(
            f"lineage {lineage_id} is owned by a concurrent evolve_step; it becomes "
            "reclaimable once the owner completes or its lease expires without a heartbeat"
        )
        self.lineage_id = lineage_id


class StepLease:
    """Live handle to an owned lease; ``lost`` fires if a reclaimer takes it."""

    def __init__(self) -> None:
        self.lost = asyncio.Event()


class StepClaims(Protocol):
    """One-owner-at-a-time lease over a lineage's evolve_step boundary."""

    lease_seconds: float

    async def acquire(self, lineage_id: str, claim_token: str) -> bool: ...

    async def refresh(self, lineage_id: str, claim_token: str) -> bool: ...

    async def release(self, lineage_id: str, claim_token: str) -> None: ...


class DurableStepClaims:
    """Database-backed claims shared by every process using one store URL."""

    def __init__(self, database_url: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self._database_url = database_url
        self._engine: AsyncEngine | None = None
        self._engine_lock = asyncio.Lock()
        self.lease_seconds = lease_seconds

    async def _engine_once(self) -> AsyncEngine:
        async with self._engine_lock:
            if self._engine is None:
                # NullPool: claims are touched a handful of times per step,
                # and holding no pooled connections means this side table
                # needs no lifecycle hook of its own.
                engine = create_async_engine(self._database_url, poolclass=NullPool)
                async with engine.begin() as conn:
                    await conn.run_sync(metadata.create_all)
                self._engine = engine
            return self._engine

    async def acquire(self, lineage_id: str, claim_token: str) -> bool:
        engine = await self._engine_once()
        table = lineage_step_claims_table
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
                            table.c.lineage_id == lineage_id
                        )
                    )
                ).first()
                if row is None:
                    try:
                        async with conn.begin_nested():
                            await conn.execute(
                                table.insert().values(
                                    lineage_id=lineage_id,
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

    async def refresh(self, lineage_id: str, claim_token: str) -> bool:
        engine = await self._engine_once()
        table = lineage_step_claims_table
        async with engine.begin() as conn:
            refreshed = await conn.execute(
                update(table)
                .where(
                    table.c.lineage_id == lineage_id,
                    table.c.claim_token == claim_token,
                )
                .values(refreshed_at=time.time())
            )
            return refreshed.rowcount == 1

    async def release(self, lineage_id: str, claim_token: str) -> None:
        engine = await self._engine_once()
        table = lineage_step_claims_table
        async with engine.begin() as conn:
            await conn.execute(
                delete(table).where(
                    table.c.lineage_id == lineage_id,
                    table.c.claim_token == claim_token,
                )
            )


class LocalStepClaims:
    """Per-store claims for stores that cannot host the durable table."""

    def __init__(self, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> None:
        self.lease_seconds = lease_seconds
        self._claims: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, lineage_id: str, claim_token: str) -> bool:
        now = time.monotonic()
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is None or held[0] == claim_token or now - held[1] > self.lease_seconds:
                self._claims[lineage_id] = (claim_token, now)
                return True
            return False

    async def refresh(self, lineage_id: str, claim_token: str) -> bool:
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is None or held[0] != claim_token:
                return False
            self._claims[lineage_id] = (claim_token, time.monotonic())
            return True

    async def release(self, lineage_id: str, claim_token: str) -> None:
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is not None and held[0] == claim_token:
                del self._claims[lineage_id]


_CLAIMS_BY_URL: dict[str, DurableStepClaims] = {}
# Fallback claims are namespaced by store object: two unrelated fake or
# private in-memory stores have independent event streams, so sharing one
# process-global table would let them deny each other over a lineage ID
# they do not actually share.
_LOCAL_CLAIMS_BY_STORE: weakref.WeakKeyDictionary[Any, LocalStepClaims] = (
    weakref.WeakKeyDictionary()
)


def step_claims_for(event_store: Any) -> StepClaims:
    """Resolve the claims backend for a store.

    Only a store with a real database URL can share claims across processes;
    in-memory URLs are private to one process by construction and unit-test
    fakes expose no URL at all, so both use a per-store table.
    """
    url = getattr(event_store, "database_url", None)
    if not isinstance(url, str) or "://" not in url or ":memory:" in url:
        claims = _LOCAL_CLAIMS_BY_STORE.get(event_store)
        if claims is None:
            claims = LocalStepClaims()
            _LOCAL_CLAIMS_BY_STORE[event_store] = claims
        return claims
    durable = _CLAIMS_BY_URL.get(url)
    if durable is None:
        durable = DurableStepClaims(url)
        _CLAIMS_BY_URL[url] = durable
    return durable


@asynccontextmanager
async def owned_lineage_step(
    claims: StepClaims,
    lineage_id: str,
    *,
    heartbeat_interval: float | None = None,
) -> AsyncIterator[StepLease]:
    """Own one lineage's evolve_step boundary for the duration of the block.

    Raises :class:`LineageStepClaimDenied` before yielding when another
    caller holds the lease. While the block runs, the lease is refreshed on
    a heartbeat. If the refresh reports the lease was reclaimed (this owner
    was presumed crashed), the yielded handle's ``lost`` event fires so the
    caller can fence its in-flight work; the reclaimer is authoritative from
    that point. Heartbeat failures never bypass cleanup: the lease is always
    released on exit, so interruption hands ownership straight to the
    resuming call.
    """
    claim_token = uuid4().hex
    if not await claims.acquire(lineage_id, claim_token):
        raise LineageStepClaimDenied(lineage_id)
    lease = StepLease()
    stop = asyncio.Event()
    interval = heartbeat_interval if heartbeat_interval is not None else claims.lease_seconds / 3

    async def _heartbeat() -> None:
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                still_owner = await claims.refresh(lineage_id, claim_token)
            except Exception:
                # A transient refresh failure is not a loss of ownership;
                # the next beat retries. Persistent failure ends in lease
                # expiry, which the reclaim path already handles.
                log.warning(
                    "evolve.step_lease.refresh_failed",
                    lineage_id=lineage_id,
                    exc_info=True,
                )
                continue
            if not still_owner:
                log.warning("evolve.step_lease.lost", lineage_id=lineage_id)
                lease.lost.set()
                return

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        yield lease
    finally:
        stop.set()
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):  # noqa: B014
            # Heartbeat outcomes were already logged; nothing here may
            # bypass the release below.
            pass
        await claims.release(lineage_id, claim_token)
