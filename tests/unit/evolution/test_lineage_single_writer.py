"""Durable lineage-writer regressions for evolve retries."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import multiprocessing
from pathlib import Path
import time
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from ouroboros.core.errors import PersistenceError
from ouroboros.core.lineage import GenerationPhase
from ouroboros.core.seed import (
    EvaluationPrinciple,
    ExitCondition,
    OntologyField,
    OntologySchema,
    Seed,
    SeedMetadata,
)
from ouroboros.core.types import Result
from ouroboros.events.lineage import (
    lineage_created,
    lineage_generation_completed,
    lineage_generation_failed,
    lineage_generation_interrupted,
    lineage_generation_started,
)
from ouroboros.evolution import loop_support
from ouroboros.evolution.convergence import ConvergenceSignal
from ouroboros.evolution.loop import GenerationResult, StepAction, StepResult
from ouroboros.evolution.loop_support import run_durable_lineage_single_flight
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.reflect import ACPatch, ReflectOutput
from ouroboros.evolution.step_receipt import (
    MAX_EXECUTION_TEXT,
    decode_step_result,
    encode_step_result,
)
from ouroboros.evolution.wonder import GroundedQuestion, WonderOutput
from ouroboros.persistence import lineage_claims
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import (
    lineage_advancement_claims_table,
    lineage_advancement_waiters_table,
)


def _seed() -> Seed:
    return Seed(
        goal="Prove one durable evolve writer",
        task_type="code",
        constraints=("Keep retries idempotent",),
        acceptance_criteria=("One generation has one writer",),
        ontology_schema=OntologySchema(
            name="LineageWriter",
            description="Durable generation authority",
            fields=(
                OntologyField(
                    name="owner",
                    field_type="string",
                    description="The generation writer",
                ),
            ),
        ),
        evaluation_principles=(
            EvaluationPrinciple(
                name="single_writer",
                description="Provider work executes once",
                weight=1.0,
            ),
        ),
        exit_conditions=(
            ExitCondition(
                name="one_receipt",
                description="Every waiter observes the winner",
                evaluation_criteria="Exactly one operation call",
            ),
        ),
        metadata=SeedMetadata(seed_id="seed_single_writer", ambiguity_score=0.0),
    )


async def _stores(db_path: Path) -> tuple[EventStore, EventStore]:
    database_url = f"sqlite+aiosqlite:///{db_path}"
    first = EventStore(database_url)
    second = EventStore(database_url)
    await first.initialize()
    await second.initialize()
    return first, second


def _run_process_claim(
    database_url: str,
    label: str,
    marker_path: str,
    owner_ready_path: str,
    release_path: str,
    process_started_path: str,
    result_queue: multiprocessing.Queue,
) -> None:
    """Run one standalone process against the shared lineage claim table."""

    async def run() -> None:
        store = EventStore(database_url)
        await store.initialize()
        Path(process_started_path).write_text(label, encoding="utf-8")

        async def operation() -> str:
            Path(owner_ready_path).write_text(label, encoding="utf-8")
            while not Path(release_path).exists():
                await asyncio.sleep(0.01)
            with Path(marker_path).open("a", encoding="utf-8") as marker:
                marker.write(f"{label}\n")
            return label

        try:
            value = await run_durable_lineage_single_flight(
                store,
                "cross-process-lineage",
                "same-request",
                operation,
                generation_number=3,
                encode=lambda result: {"value": result},
                decode=lambda payload: str(payload["value"]),
            )
            result_queue.put(("ok", value))
        except BaseException as exc:
            result_queue.put(("error", f"{type(exc).__name__}: {exc}"))
        finally:
            await store.close()

    asyncio.run(run())


def _wait_for_path(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise AssertionError(f"Timed out waiting for {path.name}")
        time.sleep(0.01)


@pytest.mark.asyncio
async def test_durable_claim_coalesces_distinct_event_store_instances(tmp_path: Path) -> None:
    """The database claim, not the process registry, owns one generation."""
    first_store, second_store = await _stores(tmp_path / "lineage-writer.db")
    entered = asyncio.Event()
    release = asyncio.Event()
    calls: list[str] = []

    async def winner() -> str:
        calls.append("generation-3")
        entered.set()
        await release.wait()
        return "generation-3-winner"

    async def forbidden_duplicate() -> str:
        calls.append("duplicate")
        return "duplicate"

    encode = lambda value: {"value": value}  # noqa: E731
    decode = lambda payload: str(payload["value"])  # noqa: E731
    try:
        owner = asyncio.create_task(
            run_durable_lineage_single_flight(
                first_store,
                "lineage",
                "request-3",
                winner,
                generation_number=3,
                encode=encode,
                decode=decode,
            )
        )
        await entered.wait()
        waiter = asyncio.create_task(
            run_durable_lineage_single_flight(
                second_store,
                "lineage",
                "request-3",
                forbidden_duplicate,
                generation_number=3,
                encode=encode,
                decode=decode,
            )
        )
        await asyncio.sleep(0)
        assert not waiter.done()
        release.set()

        assert await asyncio.gather(owner, waiter) == [
            "generation-3-winner",
            "generation-3-winner",
        ]
        assert calls == ["generation-3"]

        late_retry = await run_durable_lineage_single_flight(
            second_store,
            "lineage",
            "request-3",
            forbidden_duplicate,
            generation_number=3,
            encode=encode,
            decode=decode,
        )
        assert late_retry == "generation-3-winner"

        async def next_generation() -> str:
            calls.append("generation-4")
            return "generation-4-winner"

        advanced = await run_durable_lineage_single_flight(
            first_store,
            "lineage",
            "request-4",
            next_generation,
            generation_number=4,
            encode=encode,
            decode=decode,
        )
        assert advanced == "generation-4-winner"
        assert calls == ["generation-3", "generation-4"]
    finally:
        await first_store.close()
        await second_store.close()


@pytest.mark.asyncio
async def test_completed_receipt_waits_for_delayed_waiter_acknowledgement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short receipt lease cannot move a registered waiter into the next generation."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'delayed-waiter.db'}"
    owner_store = EventStore(database_url)
    waiter_store = EventStore(database_url)
    next_store = EventStore(database_url)
    for store in (owner_store, waiter_store, next_store):
        await store.initialize()

    clock_ms = 1_000
    monkeypatch.setattr(lineage_claims, "_now_ms", lambda: clock_ms)
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    waiter_observing = asyncio.Event()
    release_waiter = asyncio.Event()
    next_attempted = asyncio.Event()
    calls: list[str] = []
    original_observe = lineage_claims.observe
    original_try_acquire = lineage_claims.try_acquire

    async def delay_waiter_observation(*args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] is waiter_store and not release_waiter.is_set():
            waiter_observing.set()
            await release_waiter.wait()
        return await original_observe(*args, **kwargs)

    monkeypatch.setattr(lineage_claims, "observe", delay_waiter_observation)

    async def track_next_acquisition(*args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] is next_store:
            next_attempted.set()
        return await original_try_acquire(*args, **kwargs)

    monkeypatch.setattr(lineage_claims, "try_acquire", track_next_acquisition)

    async def generation_one() -> str:
        calls.append("generation-1")
        owner_entered.set()
        await release_owner.wait()
        return "generation-1-winner"

    async def forbidden_duplicate() -> str:
        calls.append("duplicate")
        return "duplicate"

    async def generation_two() -> str:
        calls.append("generation-2")
        return "generation-2-winner"

    encode = lambda value: {"value": value}  # noqa: E731
    decode = lambda payload: str(payload["value"])  # noqa: E731
    try:
        owner = asyncio.create_task(
            run_durable_lineage_single_flight(
                owner_store,
                "lineage",
                "request-1",
                generation_one,
                generation_number=1,
                encode=encode,
                decode=decode,
            )
        )
        await owner_entered.wait()
        waiter = asyncio.create_task(
            run_durable_lineage_single_flight(
                waiter_store,
                "lineage",
                "request-1",
                forbidden_duplicate,
                generation_number=1,
                encode=encode,
                decode=decode,
            )
        )
        await waiter_observing.wait()
        release_owner.set()
        assert await owner == "generation-1-winner"

        # The completed receipt has a five-second cleanup lease.  Advancing
        # beyond it must not revoke the registered waiter's winner authority.
        clock_ms = 7_000
        next_generation = asyncio.create_task(
            run_durable_lineage_single_flight(
                next_store,
                "lineage",
                "request-2",
                generation_two,
                generation_number=2,
                encode=encode,
                decode=decode,
            )
        )
        await next_attempted.wait()
        await asyncio.sleep(0)
        assert not next_generation.done()
        assert calls == ["generation-1"]
        retained = await lineage_claims.observe(
            next_store,
            scope="evolve-core",
            lineage_id="lineage",
        )
        assert retained is not None
        assert retained.generation_number == 1
        assert retained.completed

        release_waiter.set()
        assert await waiter == "generation-1-winner"
        assert await next_generation == "generation-2-winner"
        assert calls == ["generation-1", "generation-2"]
    finally:
        for store in (owner_store, waiter_store, next_store):
            await store.close()


@pytest.mark.asyncio
async def test_waiter_heartbeat_extends_lease_and_retains_winner_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live waiter's heartbeat prevents a later generation from displacing it."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'waiter-heartbeat.db'}"
    owner_store = EventStore(database_url)
    waiter_store = EventStore(database_url)
    next_store = EventStore(database_url)
    for store in (owner_store, waiter_store, next_store):
        await store.initialize()

    clock_ms = 1_000
    monkeypatch.setattr(lineage_claims, "_now_ms", lambda: clock_ms)
    monkeypatch.setattr(lineage_claims, "WAITER_LEASE_SECONDS", 0.03)
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    waiter_observing = asyncio.Event()
    release_waiter = asyncio.Event()
    waiter_renewed = asyncio.Event()
    next_attempted = asyncio.Event()
    calls: list[str] = []
    original_observe = lineage_claims.observe
    original_renew_waiter = lineage_claims.renew_waiter
    original_try_acquire = lineage_claims.try_acquire

    async def delay_waiter_observation(*args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] is waiter_store and not release_waiter.is_set():
            waiter_observing.set()
            await release_waiter.wait()
        return await original_observe(*args, **kwargs)

    async def track_waiter_renewal(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal clock_ms
        if args and args[0] is waiter_store and not waiter_renewed.is_set():
            clock_ms = 1_020
        renewed = await original_renew_waiter(*args, **kwargs)
        if args and args[0] is waiter_store and renewed:
            waiter_renewed.set()
        return renewed

    async def track_next_acquisition(*args, **kwargs):  # type: ignore[no-untyped-def]
        if args and args[0] is next_store:
            next_attempted.set()
        return await original_try_acquire(*args, **kwargs)

    monkeypatch.setattr(lineage_claims, "observe", delay_waiter_observation)
    monkeypatch.setattr(lineage_claims, "renew_waiter", track_waiter_renewal)
    monkeypatch.setattr(lineage_claims, "try_acquire", track_next_acquisition)

    async def generation_one() -> str:
        calls.append("generation-1")
        owner_entered.set()
        await release_owner.wait()
        return "generation-1-winner"

    async def forbidden_duplicate() -> str:
        calls.append("duplicate")
        return "duplicate"

    async def generation_two() -> str:
        calls.append("generation-2")
        return "generation-2-winner"

    encode = lambda value: {"value": value}  # noqa: E731
    decode = lambda payload: str(payload["value"])  # noqa: E731
    try:
        owner = asyncio.create_task(
            run_durable_lineage_single_flight(
                owner_store,
                "lineage",
                "request-1",
                generation_one,
                generation_number=1,
                encode=encode,
                decode=decode,
            )
        )
        await owner_entered.wait()
        waiter = asyncio.create_task(
            run_durable_lineage_single_flight(
                waiter_store,
                "lineage",
                "request-1",
                forbidden_duplicate,
                generation_number=1,
                encode=encode,
                decode=decode,
            )
        )
        await waiter_observing.wait()
        release_owner.set()
        assert await owner == "generation-1-winner"
        await asyncio.wait_for(waiter_renewed.wait(), timeout=1.0)

        # Move past the registration's original t=1,030 lease, but remain
        # inside the heartbeat-renewed t=1,050 lease.
        clock_ms = 1_040
        next_generation = asyncio.create_task(
            run_durable_lineage_single_flight(
                next_store,
                "lineage",
                "request-2",
                generation_two,
                generation_number=2,
                encode=encode,
                decode=decode,
            )
        )
        await next_attempted.wait()
        await asyncio.sleep(0)
        assert not next_generation.done()
        assert calls == ["generation-1"]

        release_waiter.set()
        assert await waiter == "generation-1-winner"
        assert await next_generation == "generation-2-winner"
        assert calls == ["generation-1", "generation-2"]
    finally:
        release_owner.set()
        release_waiter.set()
        for store in (owner_store, waiter_store, next_store):
            await store.close()


@pytest.mark.asyncio
async def test_hard_crashed_waiter_expires_without_pinning_next_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An orphaned waiter lease expires so a later generation can acquire."""
    owner_store, waiter_store = await _stores(tmp_path / "crashed-waiter.db")
    next_store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'crashed-waiter.db'}")
    await next_store.initialize()
    clock_ms = 1_000
    monkeypatch.setattr(lineage_claims, "_now_ms", lambda: clock_ms)
    monkeypatch.setattr(lineage_claims, "WAITER_LEASE_SECONDS", 1.0)
    calls: list[str] = []
    try:
        owner = await lineage_claims.try_acquire(
            owner_store,
            scope="evolve-core",
            lineage_id="lineage",
            generation_number=1,
            owner_id="generation-1-owner",
            request_key="request-1",
        )
        assert owner is not None and owner.acquired
        crashed_waiter = await lineage_claims.try_acquire(
            waiter_store,
            scope="evolve-core",
            lineage_id="lineage",
            generation_number=1,
            owner_id="crashed-waiter",
            request_key="request-1",
        )
        assert crashed_waiter is not None and crashed_waiter.waiter_registered
        assert crashed_waiter.waiter_id == "crashed-waiter"
        assert await lineage_claims.complete(
            owner_store,
            scope="evolve-core",
            lineage_id="lineage",
            owner_id="generation-1-owner",
            result_payload={"value": "generation-1-winner"},
        )

        # Simulate the waiter process disappearing without its heartbeat or
        # finally acknowledgement, then move beyond both bounded leases.
        clock_ms = 7_000

        async def generation_two() -> str:
            calls.append("generation-2")
            return "generation-2-winner"

        advanced = await asyncio.wait_for(
            run_durable_lineage_single_flight(
                next_store,
                "lineage",
                "request-2",
                generation_two,
                generation_number=2,
                encode=lambda value: {"value": value},
                decode=lambda payload: str(payload["value"]),
            ),
            timeout=1.0,
        )
        assert advanced == "generation-2-winner"
        assert calls == ["generation-2"]
        assert not await lineage_claims.renew_waiter(
            waiter_store,
            scope="evolve-core",
            lineage_id="lineage",
            owner_id="generation-1-owner",
            waiter_id="crashed-waiter",
        )

        current = await lineage_claims.observe(
            next_store,
            scope="evolve-core",
            lineage_id="lineage",
        )
        assert current is not None
        assert current.generation_number == 2
        assert current.completed
    finally:
        await owner_store.close()
        await waiter_store.close()
        await next_store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable_action", ["failed", "interrupted"])
async def test_retryable_receipt_replays_waiters_but_allows_later_attempt(
    tmp_path: Path,
    retryable_action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure receipts deduplicate overlap without pinning later recovery forever."""
    writer, retry_store = await _stores(tmp_path / f"retry-{retryable_action}.db")
    calls: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    waiter_registered = asyncio.Event()
    original_try_acquire = lineage_claims.try_acquire

    async def observe_waiter(*args, **kwargs):  # type: ignore[no-untyped-def]
        claim = await original_try_acquire(*args, **kwargs)
        if claim is not None and claim.waiter_registered:
            waiter_registered.set()
        return claim

    monkeypatch.setattr(lineage_claims, "try_acquire", observe_waiter)

    async def failed_attempt() -> str:
        calls.append(retryable_action)
        entered.set()
        await release.wait()
        return retryable_action

    async def forbidden_duplicate() -> str:
        calls.append("duplicate")
        return "duplicate"

    async def recovered_attempt() -> str:
        calls.append("continue")
        return "continue"

    def encode(action: str) -> dict[str, object]:
        return {"ok": True, "action": action}

    try:
        owner = asyncio.create_task(
            run_durable_lineage_single_flight(
                writer,
                "lineage",
                "same-request",
                failed_attempt,
                generation_number=2,
                encode=encode,
                decode=lambda payload: str(payload["action"]),
            )
        )
        await entered.wait()
        waiter = asyncio.create_task(
            run_durable_lineage_single_flight(
                retry_store,
                "lineage",
                "same-request",
                forbidden_duplicate,
                generation_number=2,
                encode=encode,
                decode=lambda payload: str(payload["action"]),
            )
        )
        await waiter_registered.wait()
        release.set()
        assert await asyncio.gather(owner, waiter) == [
            retryable_action,
            retryable_action,
        ]

        recovered = await run_durable_lineage_single_flight(
            retry_store,
            "lineage",
            "same-request",
            recovered_attempt,
            generation_number=2,
            encode=encode,
            decode=lambda payload: str(payload["action"]),
        )

        assert recovered == "continue"
        assert calls == [retryable_action, "continue"]
    finally:
        await writer.close()
        await retry_store.close()


def test_durable_claim_coalesces_distinct_processes(tmp_path: Path) -> None:
    """Separate interpreters still execute the generation operation exactly once."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'cross-process.db'}"
    initializer = EventStore(database_url)
    asyncio.run(initializer.initialize())
    asyncio.run(initializer.close())

    context = multiprocessing.get_context("spawn")
    result_queue = context.Queue()
    marker = tmp_path / "operation-calls.txt"
    owner_ready = tmp_path / "owner-ready"
    release = tmp_path / "release-owner"
    first_started = tmp_path / "first-started"
    second_started = tmp_path / "second-started"
    common = (database_url, "process-a", str(marker), str(owner_ready), str(release))
    first = context.Process(
        target=_run_process_claim,
        args=(*common, str(first_started), result_queue),
    )
    second = context.Process(
        target=_run_process_claim,
        args=(
            database_url,
            "process-b",
            str(marker),
            str(owner_ready),
            str(release),
            str(second_started),
            result_queue,
        ),
    )
    first.start()
    try:
        _wait_for_path(first_started)
        _wait_for_path(owner_ready)
        second.start()
        _wait_for_path(second_started)
        time.sleep(0.1)
        release.write_text("release", encoding="utf-8")
        first.join(timeout=10)
        second.join(timeout=10)
        assert first.exitcode == 0
        assert second.exitcode == 0
        results = sorted([result_queue.get(timeout=1), result_queue.get(timeout=1)])
        assert results == [("ok", "process-a"), ("ok", "process-a")]
        assert marker.read_text(encoding="utf-8").splitlines() == ["process-a"]
    finally:
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        result_queue.close()


@pytest.mark.asyncio
async def test_expired_owner_fails_closed_without_implicit_takeover(
    tmp_path: Path,
) -> None:
    """A crashed writer requires recovery instead of silently duplicating work."""
    from sqlalchemy import update as sa_update

    from ouroboros.persistence.schema import lineage_advancement_claims_table

    first_store, second_store = await _stores(tmp_path / "expired-writer.db")
    executed = False

    async def forbidden_operation() -> str:
        nonlocal executed
        executed = True
        return "must-not-run"

    try:
        claim = await lineage_claims.try_acquire(
            first_store,
            scope="evolve-core",
            lineage_id="lineage",
            generation_number=2,
            owner_id="crashed-owner",
            request_key="request-2",
        )
        assert claim is not None and claim.acquired

        # Model a crashed owner's expired row directly. A 30 ms global lease
        # made both the stale claim *and the recovery attempt* depend on host
        # scheduling; under xdist the recovered owner could expire before it
        # published. Explicit aging isolates the fail-closed observation while
        # preserving the production lease for the post-recovery operation.
        assert first_store._engine is not None
        async with first_store._engine.begin() as connection:
            await connection.execute(
                sa_update(lineage_advancement_claims_table)
                .where(
                    lineage_advancement_claims_table.c.scope == "evolve-core",
                    lineage_advancement_claims_table.c.lineage_id == "lineage",
                    lineage_advancement_claims_table.c.owner_id == "crashed-owner",
                )
                .values(lease_expires_at_ms=0)
            )

        with pytest.raises(RuntimeError, match="recover_expired_claim=true"):
            await asyncio.wait_for(
                run_durable_lineage_single_flight(
                    second_store,
                    "lineage",
                    "request-2",
                    forbidden_operation,
                    generation_number=2,
                    encode=lambda value: {"value": value},
                    decode=lambda payload: str(payload["value"]),
                ),
                timeout=1.0,
            )
        assert not executed
        assert await lineage_claims.recover_expired(
            second_store,
            scope="evolve-core",
            lineage_id="lineage",
        )
        recovered = await run_durable_lineage_single_flight(
            second_store,
            "lineage",
            "request-2",
            forbidden_operation,
            generation_number=2,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
        )
        assert recovered == "must-not-run"
        assert executed
    finally:
        await lineage_claims.release(
            first_store,
            scope="evolve-core",
            lineage_id="lineage",
            owner_id="crashed-owner",
        )
        await first_store.close()
        await second_store.close()


@pytest.mark.asyncio
async def test_heartbeat_failure_before_completion_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost renewal cannot be hidden behind a successful provider result."""
    store, reader = await _stores(tmp_path / "heartbeat-loss.db")

    async def failed_heartbeat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("database renewal failed")

    async def operation() -> str:
        await asyncio.sleep(0)
        return "unowned-result"

    monkeypatch.setattr(loop_support, "_renew_claim_until_cancelled", failed_heartbeat)
    try:
        with pytest.raises(RuntimeError, match="heartbeat failed before completion"):
            await run_durable_lineage_single_flight(
                store,
                "lineage",
                "request",
                operation,
                generation_number=1,
                encode=lambda value: {"value": value},
                decode=lambda payload: str(payload["value"]),
            )
        assert (
            await lineage_claims.observe(
                reader,
                scope="evolve-core",
                lineage_id="lineage",
            )
            is None
        )
    finally:
        await store.close()
        await reader.close()


@pytest.mark.asyncio
async def test_post_commit_heartbeat_error_does_not_override_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once the receipt commits, a racing heartbeat exception is no longer authoritative."""
    writer, reader = await _stores(tmp_path / "heartbeat-after-commit.db")
    committed = asyncio.Event()
    original_complete = lineage_claims.complete

    async def heartbeat(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        await committed.wait()
        raise RuntimeError("late heartbeat failure")

    async def complete_then_fail_heartbeat(*args, **kwargs):  # type: ignore[no-untyped-def]
        published = await original_complete(*args, **kwargs)
        committed.set()
        await asyncio.sleep(0)
        return published

    monkeypatch.setattr(loop_support, "_renew_claim_until_cancelled", heartbeat)
    monkeypatch.setattr(lineage_claims, "complete", complete_then_fail_heartbeat)
    try:
        result = await run_durable_lineage_single_flight(
            writer,
            "lineage",
            "request",
            lambda: asyncio.sleep(0, result="committed-result"),
            generation_number=1,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
        )
        assert result == "committed-result"

        replayed = await run_durable_lineage_single_flight(
            reader,
            "lineage",
            "request",
            lambda: asyncio.sleep(0, result="duplicate"),
            generation_number=1,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
        )
        assert replayed == "committed-result"
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_generation_claim_callback_holds_operation_until_released(tmp_path: Path) -> None:
    """The exact durable generation is published before generation effects begin."""
    writer, reader = await _stores(tmp_path / "claim-handoff.db")
    claimed = asyncio.Event()
    release = asyncio.Event()
    operation_started = asyncio.Event()
    claimed_generations: list[int] = []

    async def on_claimed(generation_number: int) -> None:
        claimed_generations.append(generation_number)
        claimed.set()
        await release.wait()

    async def operation() -> str:
        operation_started.set()
        return "generation-result"

    task = asyncio.create_task(
        run_durable_lineage_single_flight(
            writer,
            "claim-handoff-lineage",
            "claim-handoff-request",
            operation,
            generation_number=2,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
            on_claimed=on_claimed,
        )
    )
    try:
        await asyncio.wait_for(claimed.wait(), timeout=1.0)
        assert claimed_generations == [2]
        assert not operation_started.is_set()

        release.set()
        assert await asyncio.wait_for(task, timeout=1.0) == "generation-result"
        assert operation_started.is_set()

        replayed_claims: list[int] = []
        replayed = await run_durable_lineage_single_flight(
            reader,
            "claim-handoff-lineage",
            "claim-handoff-request",
            lambda: asyncio.sleep(0, result="must-not-run"),
            generation_number=2,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
            on_claimed=lambda generation: asyncio.sleep(
                0, result=replayed_claims.append(generation)
            ),
        )
        assert replayed == "generation-result"
        assert replayed_claims == [2]
    finally:
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_outer_claim_rebinds_before_nested_generation_effects(tmp_path: Path) -> None:
    """A nested core claim makes its generation durable on the outer receipt first."""
    writer, reader = await _stores(tmp_path / "claim-rebind.db")
    observed_generations: list[int] = []

    async def operation_with_claim(
        projected_generation: int,
        rebind_generation: Callable[[int], Awaitable[None]],
    ) -> str:
        assert projected_generation == 1
        await rebind_generation(2)
        rebound = await lineage_claims.observe(
            reader,
            scope="evolve-handler",
            lineage_id="claim-rebind-lineage",
        )
        assert rebound is not None
        observed_generations.append(rebound.generation_number)
        return "generation-2-result"

    try:
        result = await run_durable_lineage_single_flight(
            writer,
            "claim-rebind-lineage",
            "claim-rebind-request",
            lambda: asyncio.sleep(0, result="must-not-run"),
            generation_number=1,
            encode=lambda value: {"value": value},
            decode=lambda payload: str(payload["value"]),
            scope="evolve-handler",
            operation_with_claim=operation_with_claim,
        )

        receipt = await lineage_claims.observe(
            reader,
            scope="evolve-handler",
            lineage_id="claim-rebind-lineage",
        )
        assert result == "generation-2-result"
        assert observed_generations == [2]
        assert receipt is not None and receipt.completed
        assert receipt.generation_number == 2
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_owner", [False, True])
@pytest.mark.parametrize("close_during_cancel", [False, True])
async def test_cancelled_claim_acquisition_settles_and_removes_participation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    existing_owner: bool,
    close_during_cancel: bool,
) -> None:
    """Cancellation after commit releases owner/waiter state before close returns."""
    writer, reader = await _stores(
        tmp_path / f"cancelled-claim-{existing_owner}-{close_during_cancel}.db"
    )
    scope = "evolve-handler"
    lineage_id = f"cancelled-claim-{existing_owner}"
    winner_id = "existing-owner"
    if existing_owner:
        winner = await lineage_claims.try_acquire(
            writer,
            scope=scope,
            lineage_id=lineage_id,
            generation_number=1,
            owner_id=winner_id,
            request_key="existing-request",
        )
        assert winner is not None and winner.acquired

    committed = asyncio.Event()
    release_commit = asyncio.Event()
    original_commit = lineage_claims._commit
    gate_first_commit = True

    async def gated_commit(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal gate_first_commit
        await original_commit(*args, **kwargs)
        if gate_first_commit:
            gate_first_commit = False
            committed.set()
            await release_commit.wait()

    monkeypatch.setattr(lineage_claims, "_commit", gated_commit)
    participant_id = "cancelled-participant"
    acquisition = asyncio.create_task(
        lineage_claims.try_acquire(
            writer,
            scope=scope,
            lineage_id=lineage_id,
            generation_number=1,
            owner_id=participant_id,
            request_key="cancelled-request",
        )
    )
    close_task: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(committed.wait(), timeout=5)
        acquisition.cancel()
        if close_during_cancel:
            close_task = asyncio.create_task(writer.close())
            await asyncio.sleep(0)
            assert not close_task.done()
        release_commit.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(acquisition, timeout=5)
        if close_task is not None:
            await asyncio.wait_for(close_task, timeout=10)

        observed = await lineage_claims.observe(reader, scope=scope, lineage_id=lineage_id)
        engine = reader._engine
        assert engine is not None
        async with engine.connect() as connection:
            waiter_count = await connection.scalar(
                select(func.count())
                .select_from(lineage_advancement_waiters_table)
                .where(
                    lineage_advancement_waiters_table.c.scope == scope,
                    lineage_advancement_waiters_table.c.lineage_id == lineage_id,
                )
            )
        assert waiter_count == 0
        if existing_owner:
            assert observed is not None and observed.owner_id == winner_id
            await asyncio.wait_for(
                lineage_claims.release(
                    reader if close_during_cancel else writer,
                    scope=scope,
                    lineage_id=lineage_id,
                    owner_id=winner_id,
                ),
                timeout=1,
            )
        else:
            assert observed is None
    finally:
        release_commit.set()
        if not acquisition.done():
            acquisition.cancel()
            with pytest.raises(asyncio.CancelledError):
                await acquisition
        if close_task is not None and not close_task.done():
            await close_task
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_claim_acquisition_refuses_close_admission_without_hanging(tmp_path: Path) -> None:
    """A close winner reaches the acquisition caller without orphaned task errors."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'claim-close-refusal.db'}")
    await store.initialize()
    store._closing = True
    try:
        with pytest.raises(PersistenceError, match="closing"):
            await asyncio.wait_for(
                lineage_claims.try_acquire(
                    store,
                    scope="evolve-handler",
                    lineage_id="claim-close-refusal",
                    generation_number=1,
                    owner_id="refused-owner",
                    request_key="refused-request",
                ),
                timeout=1,
            )
    finally:
        store._closing = False
        await store.close()


@pytest.mark.asyncio
async def test_claim_operations_ignore_synthetic_mock_settlement_capability() -> None:
    """Lightweight mocks retain the no-durable-authority fallback."""
    event_store = AsyncMock()
    synthetic_settle = event_store.settle_transactional_write

    with pytest.raises(PersistenceError, match="initialized"):
        await lineage_claims.try_acquire(
            event_store,
            scope="evolve-handler",
            lineage_id="mock-lineage",
            generation_number=1,
            owner_id="mock-owner",
            request_key="mock-request",
        )
    assert not await lineage_claims.renew(
        event_store,
        scope="evolve-handler",
        lineage_id="mock-lineage",
        owner_id="mock-owner",
    )
    assert not await lineage_claims.rebind_generation(
        event_store,
        scope="evolve-handler",
        lineage_id="mock-lineage",
        owner_id="mock-owner",
        generation_number=2,
    )
    assert (
        await lineage_claims.observe(
            event_store,
            scope="evolve-handler",
            lineage_id="mock-lineage",
        )
        is None
    )
    assert not await lineage_claims.complete(
        event_store,
        scope="evolve-handler",
        lineage_id="mock-lineage",
        owner_id="mock-owner",
        result_payload={"ok": True},
    )
    await lineage_claims.release(
        event_store,
        scope="evolve-handler",
        lineage_id="mock-lineage",
        owner_id="mock-owner",
    )
    assert not await lineage_claims.recover_expired(
        event_store,
        scope="evolve-handler",
        lineage_id="mock-lineage",
    )
    assert not await lineage_claims.renew_waiter(
        event_store,
        scope="evolve-handler",
        lineage_id="mock-lineage",
        owner_id="mock-owner",
        waiter_id="mock-waiter",
    )
    await lineage_claims.acknowledge_waiter(
        event_store,
        scope="evolve-handler",
        lineage_id="mock-lineage",
        owner_id="mock-owner",
        waiter_id="mock-waiter",
    )
    synthetic_settle.assert_not_awaited()


@pytest.mark.asyncio
async def test_closed_store_refuses_every_claim_operation_without_durable_change(
    tmp_path: Path,
) -> None:
    """Closed production stores cannot silently bypass their lifecycle fence."""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'closed-claim-operations.db'}"
    store = EventStore(database_url)
    await store.initialize()
    owner = await lineage_claims.try_acquire(
        store,
        scope="evolve-handler",
        lineage_id="closed-claim-operations",
        generation_number=1,
        owner_id="closed-owner",
        request_key="closed-request",
    )
    waiter = await lineage_claims.try_acquire(
        store,
        scope="evolve-handler",
        lineage_id="closed-claim-operations",
        generation_number=1,
        owner_id="closed-waiter",
        request_key="closed-request",
    )
    assert owner is not None and owner.acquired
    assert waiter is not None and waiter.waiter_registered
    engine = store._engine
    assert engine is not None
    async with engine.connect() as connection:
        claims_before = [
            dict(row)
            for row in (await connection.execute(select(lineage_advancement_claims_table)))
            .mappings()
            .all()
        ]
        waiters_before = [
            dict(row)
            for row in (await connection.execute(select(lineage_advancement_waiters_table)))
            .mappings()
            .all()
        ]
    await store.close()

    operations: tuple[Callable[[], Awaitable[object]], ...] = (
        lambda: lineage_claims.try_acquire(
            store,
            scope="evolve-handler",
            lineage_id="post-close-new-lineage",
            generation_number=1,
            owner_id="post-close-new-owner",
            request_key="post-close-new-request",
        ),
        lambda: lineage_claims.renew(
            store,
            scope="evolve-handler",
            lineage_id="closed-claim-operations",
            owner_id="closed-owner",
        ),
        lambda: lineage_claims.observe(
            store,
            scope="evolve-handler",
            lineage_id="closed-claim-operations",
        ),
        lambda: lineage_claims.complete(
            store,
            scope="evolve-handler",
            lineage_id="closed-claim-operations",
            owner_id="closed-owner",
            result_payload={"ok": True},
        ),
        lambda: lineage_claims.release(
            store,
            scope="evolve-handler",
            lineage_id="closed-claim-operations",
            owner_id="closed-owner",
        ),
        lambda: lineage_claims.recover_expired(
            store,
            scope="evolve-handler",
            lineage_id="closed-claim-operations",
        ),
        lambda: lineage_claims.renew_waiter(
            store,
            scope="evolve-handler",
            lineage_id="closed-claim-operations",
            owner_id="closed-owner",
            waiter_id="closed-waiter",
        ),
        lambda: lineage_claims.acknowledge_waiter(
            store,
            scope="evolve-handler",
            lineage_id="closed-claim-operations",
            owner_id="closed-owner",
            waiter_id="closed-waiter",
        ),
    )
    for operation in operations:
        with pytest.raises(PersistenceError, match="closing"):
            await operation()

    reopened = EventStore(database_url)
    await reopened.initialize()
    try:
        reopened_engine = reopened._engine
        assert reopened_engine is not None
        async with reopened_engine.connect() as connection:
            claims_after = [
                dict(row)
                for row in (await connection.execute(select(lineage_advancement_claims_table)))
                .mappings()
                .all()
            ]
            waiters_after = [
                dict(row)
                for row in (await connection.execute(select(lineage_advancement_waiters_table)))
                .mappings()
                .all()
            ]
        assert claims_after == claims_before
        assert waiters_after == waiters_before
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_close_drains_generation_rebind_and_refuses_post_close_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebind transaction cannot outlive its EventStore lifecycle."""
    writer, reader = await _stores(tmp_path / "claim-rebind-close.db")
    claim = await lineage_claims.try_acquire(
        writer,
        scope="evolve-handler",
        lineage_id="claim-rebind-close",
        generation_number=1,
        owner_id="rebind-owner",
        request_key="rebind-request",
    )
    assert claim is not None and claim.acquired
    entered, release = asyncio.Event(), asyncio.Event()
    original_update = lineage_claims._update_generation

    async def gated_update(*args, **kwargs):  # type: ignore[no-untyped-def]
        entered.set()
        await release.wait()
        return await original_update(*args, **kwargs)

    monkeypatch.setattr(lineage_claims, "_update_generation", gated_update)
    rebind_task = asyncio.create_task(
        lineage_claims.rebind_generation(
            writer,
            scope="evolve-handler",
            lineage_id="claim-rebind-close",
            owner_id="rebind-owner",
            generation_number=2,
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    close_task = asyncio.create_task(writer.close())
    await asyncio.sleep(0)
    assert not close_task.done()

    release.set()
    assert await asyncio.wait_for(rebind_task, timeout=5)
    await asyncio.wait_for(close_task, timeout=10)

    with pytest.raises(PersistenceError, match="closing"):
        await lineage_claims.rebind_generation(
            writer,
            scope="evolve-handler",
            lineage_id="claim-rebind-close",
            owner_id="rebind-owner",
            generation_number=3,
        )

    try:
        observed = await lineage_claims.observe(
            reader,
            scope="evolve-handler",
            lineage_id="claim-rebind-close",
        )
        assert observed is not None
        assert observed.generation_number == 2
    finally:
        await reader.close()


@pytest.mark.asyncio
async def test_failed_step_receipt_replays_seed_from_started_event(tmp_path: Path) -> None:
    """Cross-process waiters reproduce a failed winner without inventing a Seed."""
    writer, reader = await _stores(tmp_path / "failed-receipt.db")
    seed = _seed()
    lineage_id = "failed-lineage"
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_started(
                lineage_id,
                1,
                GenerationPhase.EXECUTING.value,
                seed.metadata.seed_id,
                json.dumps(seed.to_dict()),
            )
        )
        await writer.append(
            lineage_generation_failed(
                lineage_id,
                1,
                GenerationPhase.EXECUTING.value,
                "provider failed",
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        result = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=1,
                    seed=seed,
                    phase=GenerationPhase.FAILED,
                    success=False,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="provider failed",
                    ontology_similarity=0.0,
                    generation=1,
                ),
                lineage=lineage,
                action=StepAction.FAILED,
                next_generation=1,
            )
        )

        replayed = await decode_step_result(reader, encode_step_result(result))

        assert replayed.is_ok
        assert replayed.value.action is StepAction.FAILED
        assert replayed.value.generation_result.seed == seed
        assert replayed.value.generation_result.phase is GenerationPhase.FAILED
        assert not replayed.value.generation_result.success
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_interrupted_step_receipt_replays_partial_generation(tmp_path: Path) -> None:
    """A waiter recovers interrupted Wonder/Reflect evidence from the durable checkpoint."""
    writer, reader = await _stores(tmp_path / "interrupted-receipt.db")
    seed = _seed()
    lineage_id = "interrupted-lineage"
    questions = ("[AC 1] What writer evidence remains unresolved?",)
    wonder = WonderOutput(
        questions=questions,
        grounded_questions=(
            GroundedQuestion(question=questions[0], kind="challenge", ac_indices=(0,)),
        ),
        ontology_tensions=("owner and waiter observe one contract",),
        should_continue=True,
        reasoning="challenge",
    )
    reflect = ReflectOutput(
        refined_goal=seed.goal,
        refined_constraints=seed.constraints,
        refined_acs=("One generation has one writer",),
        ac_patches=(ACPatch(op="keep", index=0, reason="preserve identity"),),
        reasoning="preserve the partial reflection",
    )
    reflect.restore_durable_patch_identity(seed)
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_started(
                lineage_id,
                2,
                GenerationPhase.WONDERING.value,
                seed.metadata.seed_id,
                json.dumps(seed.to_dict()),
            )
        )
        await writer.append(
            lineage_generation_interrupted(
                lineage_id,
                2,
                last_completed_phase=GenerationPhase.EXECUTING.value,
                partial_state={
                    "wonder_questions": list(questions),
                    "reflect_output": reflect.model_dump(mode="json"),
                    "execution_output": "partial execution",
                },
                seed_json=json.dumps(seed.to_dict()),
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        result = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=2,
                    seed=seed,
                    wonder_output=wonder,
                    reflect_output=reflect,
                    execution_output="partial execution",
                    phase=GenerationPhase.INTERRUPTED,
                    success=False,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="Generation interrupted by SIGINT",
                    ontology_similarity=0.0,
                    generation=2,
                ),
                lineage=lineage,
                action=StepAction.INTERRUPTED,
                next_generation=2,
            )
        )

        replayed = await decode_step_result(reader, encode_step_result(result))

        assert replayed.is_ok
        generation = replayed.value.generation_result
        assert replayed.value.action is StepAction.INTERRUPTED
        assert generation.phase is GenerationPhase.INTERRUPTED
        assert generation.wonder_output == wonder
        assert generation.reflect_output == reflect
        assert generation.reflect_output is not None
        assert generation.reflect_output.ac_patch_identity_explicit is True
        assert generation.execution_output == "partial execution"
        assert not generation.success
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_completed_step_receipt_preserves_structured_phase_outputs(tmp_path: Path) -> None:
    """Completed projection cannot erase the winner result observed by a waiter."""
    writer, reader = await _stores(tmp_path / "completed-structured-receipt.db")
    seed = _seed()
    lineage_id = "completed-structured-lineage"
    wonder = WonderOutput(
        questions=(),
        grounded_questions=(),
        ontology_tensions=("empty questions still carry a tension",),
        should_continue=True,
        reasoning="authoritative empty-question result",
    )
    reflect = ReflectOutput(
        refined_goal=seed.goal,
        refined_constraints=seed.constraints,
        refined_acs=("One generation has one writer",),
        ac_patches=(ACPatch(op="keep", index=0, reason="stable contract"),),
        settled_ac_indices=(0,),
        reasoning="parser-issued keep",
    )
    reflect.restore_durable_patch_identity(seed)
    execution_output = "execution-start\n" + ("x" * 12_000) + "\nTRAILING FAILURE MARKER"
    validation_output = "validation-start\n" + ("v" * 4_000) + "\nVALIDATION TAIL"
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_completed(
                lineage_id,
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                wonder_questions=[],
                seed_json=json.dumps(seed.to_dict()),
                execution_output=execution_output,
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        assert lineage.generations[-1].partial_state is None
        winner = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=1,
                    seed=seed,
                    wonder_output=wonder,
                    reflect_output=reflect,
                    execution_output=execution_output,
                    validation_output=validation_output,
                    phase=GenerationPhase.COMPLETED,
                    success=True,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="continue",
                    ontology_similarity=0.0,
                    generation=1,
                ),
                lineage=lineage,
                action=StepAction.CONTINUE,
                next_generation=2,
            )
        )

        replayed = await decode_step_result(reader, encode_step_result(winner))

        assert replayed.is_ok
        waiter_generation = replayed.value.generation_result
        assert waiter_generation.wonder_output == wonder
        assert waiter_generation.reflect_output == reflect
        assert waiter_generation.reflect_output is not None
        assert waiter_generation.reflect_output.ac_patch_identity_explicit is True
        assert waiter_generation.execution_output == execution_output
        assert waiter_generation.execution_output.endswith("TRAILING FAILURE MARKER")
        assert waiter_generation.validation_output == validation_output
        assert waiter_generation.validation_output.endswith("VALIDATION TAIL")
    finally:
        await writer.close()
        await reader.close()


@pytest.mark.asyncio
async def test_step_receipt_fails_closed_when_execution_output_exceeds_cap(
    tmp_path: Path,
) -> None:
    """Incomplete bounded evidence never becomes a handler verification artifact."""
    writer, reader = await _stores(tmp_path / "oversized-execution-receipt.db")
    seed = _seed()
    lineage_id = "oversized-execution-lineage"
    execution_output = "x" * (MAX_EXECUTION_TEXT + 1)
    try:
        await writer.append(lineage_created(lineage_id, seed.goal))
        await writer.append(
            lineage_generation_completed(
                lineage_id,
                generation_number=1,
                seed_id=seed.metadata.seed_id,
                ontology_snapshot=seed.ontology_schema.model_dump(mode="json"),
                seed_json=json.dumps(seed.to_dict()),
                execution_output=execution_output,
            )
        )
        lineage = LineageProjector().project(await writer.replay_lineage(lineage_id))
        assert lineage is not None
        winner = Result.ok(
            StepResult(
                generation_result=GenerationResult(
                    generation_number=1,
                    seed=seed,
                    execution_output=execution_output,
                ),
                convergence_signal=ConvergenceSignal(
                    converged=False,
                    reason="continue",
                    ontology_similarity=0.0,
                    generation=1,
                ),
                lineage=lineage,
                action=StepAction.CONTINUE,
                next_generation=2,
            )
        )
        payload = encode_step_result(winner)
        assert payload["execution_output_complete"] is False

        with pytest.raises(ValueError, match="execution output is incomplete"):
            await decode_step_result(reader, payload)
    finally:
        await writer.close()
        await reader.close()
