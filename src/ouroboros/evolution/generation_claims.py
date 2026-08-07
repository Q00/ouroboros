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
  crashed and may be reclaimed — in two phases: the reclaimer first marks
  the lease revoked, then takes ownership only after a grace period of one
  heartbeat interval. A revoked lease refuses the owner's refresh, so a
  scheduling owner provably observes the transfer and stops inside the
  grace window, before the successor can begin. Phase two is open to any
  caller — the revocation carries no reclaimer identity, so a fresh retry
  completes a graced reclaim — and each phase is a compare-and-set on the
  observed state, so two reclaimers cannot both win. Fencing is two-sided:
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
from contextvars import ContextVar
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

# The lease of the evolve_step attempt running in the current task tree.
# Effectful executors read it through current_step_fence() and must stop
# producing effects once its ``lost`` event fires: this is the fencing
# token that reaches work no database check can interrupt. The owner
# self-fences at half the lease, and a reclaimer needs the full lease plus
# a grace period, so fencing-aware work provably stops before a successor
# can begin even when the owner's database path has failed entirely.
_CURRENT_STEP_LEASE: ContextVar[StepLease | None] = ContextVar("evolve_step_lease", default=None)


def current_step_fence() -> StepLease | None:
    """Return the running evolve_step attempt's lease handle, if any."""
    return _CURRENT_STEP_LEASE.get()


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
                        select(
                            table.c.claim_token,
                            table.c.refreshed_at,
                            table.c.revoking_token,
                            table.c.revoked_at,
                        ).where(table.c.lineage_id == lineage_id)
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
                observed_token, refreshed_at, revoking_token, revoked_at = row
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
                    # Two-phase reclaim: mark the expired lease revoked, wait
                    # one grace period (a heartbeat interval, so a scheduling
                    # owner provably observes the revocation and stops), then
                    # take ownership by CAS on the observed state. Two
                    # concurrent reclaimers cannot both win either phase.
                    grace = self.lease_seconds / 6
                    if revoked_at is not None and now - float(revoked_at) > grace:
                        # Phase two is open to any caller: the revocation's
                        # job is fencing notification plus the grace window,
                        # not reclaimer identity — a fresh retry with a new
                        # token must be able to complete a graced reclaim.
                        # The CAS on the full observed state still leaves
                        # exactly one winner.
                        stolen = await conn.execute(
                            update(table)
                            .where(
                                table.c.lineage_id == lineage_id,
                                table.c.claim_token == observed_token,
                                table.c.revoking_token == revoking_token,
                            )
                            .values(
                                claim_token=claim_token,
                                refreshed_at=now,
                                revoking_token=None,
                                revoked_at=None,
                            )
                        )
                        await conn.commit()
                        return stolen.rowcount == 1
                    if revoking_token is None or (
                        revoked_at is not None and now - float(revoked_at) > self.lease_seconds
                    ):
                        # Start revocation, or restart it if the previous
                        # reclaimer itself went silent for a whole lease.
                        await conn.execute(
                            update(table)
                            .where(
                                table.c.lineage_id == lineage_id,
                                table.c.claim_token == observed_token,
                                table.c.revoking_token.is_(revoking_token),
                            )
                            .values(revoking_token=claim_token, revoked_at=now)
                        )
                        await conn.commit()
                        return False
                    await conn.rollback()
                    return False
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
                    # A revoked lease no longer refreshes: this is how a
                    # scheduling owner observes the transfer within one
                    # heartbeat and fences itself before the grace elapses.
                    table.c.revoking_token.is_(None),
                )
                .values(refreshed_at=await self._db_now(conn))
            )
            return refreshed.rowcount == 1

    async def refresh_fenced(self, lineage_id: str, claim_token: str) -> bool:
        """Liveness beat for a fenced owner whose work has not stopped yet.

        Keeps ``refreshed_at`` fresh without touching the revocation marker,
        so phase-two takeover — which requires the lease to be expired —
        stays blocked while the stale attempt is still provably alive. The
        row's deletion on exit is the termination acknowledgement that
        finally lets a reclaimer proceed.
        """
        engine = await self._engine_once()
        table = lineage_step_claims_table
        async with engine.begin() as conn:
            beat = await conn.execute(
                update(table)
                .where(
                    table.c.lineage_id == lineage_id,
                    table.c.claim_token == claim_token,
                )
                .values(refreshed_at=await self._db_now(conn))
            )
            return beat.rowcount == 1

    async def _append_event_if_owner(
        self, lineage_id: str, claim_token: str, event: Any, lease: StepLease
    ) -> bool:
        """Token check and event insert in one transaction (see module doc).

        The attempt's process-local lost state is consulted both on entry
        and immediately before the commit, so a self-fence that fires while
        the append is already admitted still fails the write closed. The
        claim row is read with a row lock so a reclaimer on a non-SQLite
        backend cannot update the token between this read and the insert
        (SQLite's immediate write transaction covers the same window).
        """
        if lease.lost.is_set():
            return False
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
                    select(table.c.claim_token)
                    .where(table.c.lineage_id == lineage_id)
                    .with_for_update()
                )
                if held != claim_token:
                    await conn.rollback()
                    return False
                await conn.execute(events_table.insert().values(**event.to_db_dict()))
                if lease.lost.is_set():
                    # The self-fence fired after admission: authority is no
                    # longer provable, so the admitted write must not commit.
                    await conn.rollback()
                    return False
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
        # lineage_id -> [claim_token, refreshed_at, revoking_token, revoked_at]
        self._claims: dict[str, list[Any]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, lineage_id: str, claim_token: str) -> bool:
        now = time.monotonic()
        grace = self.lease_seconds / 6
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is None or held[0] == claim_token:
                self._claims[lineage_id] = [claim_token, now, None, None]
                return True
            token, refreshed_at, revoking_token, revoked_at = held
            if now - refreshed_at <= self.lease_seconds:
                return False
            if revoked_at is not None and now - revoked_at > grace:
                self._claims[lineage_id] = [claim_token, now, None, None]
                return True
            if revoking_token is None or (
                revoked_at is not None and now - revoked_at > self.lease_seconds
            ):
                held[2] = claim_token
                held[3] = now
            return False

    async def refresh(self, lineage_id: str, claim_token: str) -> bool:
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is None or held[0] != claim_token or held[2] is not None:
                return False
            held[1] = time.monotonic()
            return True

    async def release(self, lineage_id: str, claim_token: str) -> None:
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is not None and held[0] == claim_token:
                del self._claims[lineage_id]

    async def refresh_fenced(self, lineage_id: str, claim_token: str) -> bool:
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is None or held[0] != claim_token:
                return False
            held[1] = time.monotonic()
            return True

    async def append_event_if_owner(
        self, lineage_id: str, claim_token: str, event_store: Any, event: Any
    ) -> bool:
        """Ownership check and append in one critical section.

        The lock is held across the store append, so an expiry-and-reclaim
        cannot interleave between validation and persistence — acquire()
        serializes behind an in-flight owner append, mirroring the durable
        backend's single-transaction ordering.
        """
        async with self._lock:
            held = self._claims.get(lineage_id)
            if held is None or held[0] != claim_token or held[2] is not None:
                return False
            await event_store.append(event)
            return True


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
    lease: StepLease,
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
    if lease.lost.is_set():
        # Self-fencing is process-local: the stored token may still be
        # valid, but an owner that has declared its ownership unprovable
        # must fail every persistence path closed (#1889 round seven).
        return False
    claim_token = lease.claim_token
    if isinstance(claims, DurableStepClaims):
        registry = getattr(event_store, "_settling_writes", None)
        refuse_when = None
        if hasattr(event_store, "_closing"):
            refuse_when = lambda: event_store._closing  # noqa: E731
        return await _run_to_settlement(
            claims._append_event_if_owner(lineage_id, claim_token, event, lease),
            registry=registry,
            refuse_when=refuse_when,
            operation="append_lineage_event_if_owner",
        )
    if isinstance(claims, LocalStepClaims):
        return await claims.append_event_if_owner(lineage_id, claim_token, event_store, event)
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
            # Each refresh attempt is bounded by the self-fence deadline: a
            # refresh that hangs must not suspend the fence, or a reclaimer
            # could take over while this owner still runs unaware.
            fence_budget = last_confirmed + claims.lease_seconds / 2 - time.monotonic()
            if fence_budget <= 0:
                log.warning("evolve.step_lease.self_fenced", lineage_id=lineage_id)
                lease.lost.set()
                break
            try:
                still_owner = await asyncio.wait_for(
                    claims.refresh(lineage_id, claim_token), timeout=fence_budget
                )
            except TimeoutError:
                log.warning("evolve.step_lease.self_fenced", lineage_id=lineage_id)
                lease.lost.set()
                break
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
                    break
                continue
            if not still_owner:
                log.warning("evolve.step_lease.lost", lineage_id=lineage_id)
                lease.lost.set()
                break
            last_confirmed = time.monotonic()
        # Fenced-lingering mode: authority is gone but the attempt may not
        # have physically stopped (a cancellation-resistant executor). Keep
        # a liveness beat on the still-held token so a reclaimer cannot
        # take over while the stale work is provably alive; the release on
        # exit is the termination acknowledgement that unblocks takeover.
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                if not await claims.refresh_fenced(lineage_id, claim_token):
                    return
            except Exception:
                log.warning(
                    "evolve.step_lease.fenced_beat_failed",
                    lineage_id=lineage_id,
                    exc_info=True,
                )

    heartbeat = asyncio.create_task(_heartbeat())
    fence_token = _CURRENT_STEP_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_STEP_LEASE.reset(fence_token)
        stop.set()
        heartbeat.cancel()
        try:
            await heartbeat
        except (asyncio.CancelledError, Exception):  # noqa: B014
            # Heartbeat outcomes were already logged; nothing here may
            # bypass the release below.
            pass
        await claims.release(lineage_id, claim_token)
