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
  observed token, so two reclaimers cannot both win. Fencing is two-sided:
  an owner that observes a replacement token stops immediately, and an
  owner that cannot *prove* ownership (refresh outage) fences itself once
  half the lease has elapsed since its last confirmed refresh — strictly
  before a reclaimer is allowed to steal at the full lease — so a stale
  attempt can never continue effects alongside its successor. The loss is
  exposed on the yielded lease handle so the caller aborts in-flight work.

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

from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool
import structlog

from ouroboros.persistence.event_store import _run_to_settlement
from ouroboros.persistence.schema import events_table, lineage_step_claims_table, metadata

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

    def __init__(self, claim_token: str) -> None:
        self.claim_token = claim_token
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
                now = await self._db_now(conn)
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

    @staticmethod
    async def _db_now(conn: Any) -> float:
        """Epoch seconds from the database itself.

        Lease expiry must never compare wall-clock values written by
        different owners: a skewed or stepped client clock could steal a
        fresh lease from a live owner. The database connection is the one
        shared time authority every contender already agrees on.
        """
        if conn.dialect.name == "sqlite":
            query = "SELECT (julianday('now') - 2440587.5) * 86400.0"
        else:
            query = "SELECT EXTRACT(EPOCH FROM NOW())"
        return float(await conn.scalar(text(query)))

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
                .values(refreshed_at=await self._db_now(conn))
            )
            return refreshed.rowcount == 1

    async def _append_event_if_owner(self, lineage_id: str, claim_token: str, event: Any) -> bool:
        """Token check and event insert in one transaction (see module doc)."""
        engine = await self._engine_once()
        table = lineage_step_claims_table
        async with engine.connect() as conn:
            sqlite = conn.dialect.name == "sqlite"
            if sqlite:
                await conn.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                await conn.begin()
            try:
                held = await conn.scalar(
                    select(table.c.claim_token).where(table.c.lineage_id == lineage_id)
                )
                if held != claim_token:
                    await conn.rollback()
                    return False
                await conn.execute(events_table.insert().values(**event.to_db_dict()))
                await conn.commit()
                return True
            except BaseException:
                if conn.in_transaction():
                    await conn.rollback()
                raise

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


def _is_shared_database_url(url: str) -> bool:
    """True when the URL names a database another process could open too."""
    try:
        parsed = make_url(url)
    except Exception:
        return False
    if parsed.get_backend_name() == "sqlite":
        # Every pathless or explicit-:memory: SQLite form is in-memory and
        # therefore private to this process by construction.
        return bool(parsed.database) and parsed.database != ":memory:"
    return True


def step_claims_for(event_store: Any) -> StepClaims:
    """Resolve the claims backend for a store.

    Only a store with a real database URL can share claims across processes;
    in-memory URLs are private to one process by construction and unit-test
    fakes expose no URL at all, so both use a per-store table.
    """
    url = getattr(event_store, "database_url", None)
    if not isinstance(url, str) or not _is_shared_database_url(url):
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


async def append_lineage_event_if_owner(
    event_store: Any,
    claims: StepClaims,
    lineage_id: str,
    claim_token: str,
    event: Any,
) -> bool:
    """Append one lineage event only while ``claim_token`` still owns the step.

    On the durable backend the token check and the event insert share one
    database transaction, so the single-writer database serializes them
    against any steal: a write that commits necessarily precedes the
    successor's post-steal replay, and a steal that commits first refuses
    the stale write. The write still runs to settlement inside the store's
    registry, so close() drains it and cancellation cannot abandon it
    mid-transaction. Fallback backends are private to one process and check
    ownership immediately before appending.
    """
    if isinstance(claims, DurableStepClaims):
        registry = getattr(event_store, "_settling_writes", None)
        refuse_when = None
        if hasattr(event_store, "_closing"):
            refuse_when = lambda: event_store._closing  # noqa: E731
        return await _run_to_settlement(
            claims._append_event_if_owner(lineage_id, claim_token, event),
            registry=registry,
            refuse_when=refuse_when,
            operation="append_lineage_event_if_owner",
        )
    if not await claims.refresh(lineage_id, claim_token):
        return False
    await event_store.append(event)
    return True


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
    that point. If refreshes cannot be confirmed at all, the owner fences
    itself at half the lease — strictly before a reclaimer may steal — so
    the ``lost`` event always fires before a successor can begin. Heartbeat failures never bypass cleanup: the lease is always
    released on exit, so interruption hands ownership straight to the
    resuming call.
    """
    claim_token = uuid4().hex
    if not await claims.acquire(lineage_id, claim_token):
        raise LineageStepClaimDenied(lineage_id)
    lease = StepLease(claim_token)
    stop = asyncio.Event()
    interval = heartbeat_interval if heartbeat_interval is not None else claims.lease_seconds / 6

    async def _heartbeat() -> None:
        last_confirmed = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                still_owner = await claims.refresh(lineage_id, claim_token)
            except Exception:
                # A transient refresh failure is retried — but only while
                # ownership is still provable. Past half the lease without a
                # confirmed refresh this owner must fence itself, strictly
                # before a reclaimer may steal at the full lease, so a stale
                # attempt can never keep working alongside its successor.
                log.warning(
                    "evolve.step_lease.refresh_failed",
                    lineage_id=lineage_id,
                    exc_info=True,
                )
                if time.monotonic() - last_confirmed > claims.lease_seconds / 2:
                    log.warning("evolve.step_lease.self_fenced", lineage_id=lineage_id)
                    lease.lost.set()
                    return
                continue
            if not still_owner:
                log.warning("evolve.step_lease.lost", lineage_id=lineage_id)
                lease.lost.set()
                return
            last_confirmed = time.monotonic()

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
