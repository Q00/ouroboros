"""#1889: exactly one caller may own a lineage's evolve_step boundary.

Two concurrent ``evolve_step`` calls used to replay the same lineage state,
select the same generation number, run the external executor twice, and
append duplicate completion and terminal events. The lease serializes the
whole replay/selection/execution boundary per lineage: the loser fails
closed before it can observe a stale snapshot or write lineage-creation
state, a released lease always hands the next caller a fresh replay, and a
crashed owner's lease is reclaimable only after it stops heartbeating —
with the fenced owner's in-flight work aborted if it is still alive.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    OntologySchema,
)
from ouroboros.core.seed import Seed, SeedMetadata
from ouroboros.core.types import Result
from ouroboros.evolution.convergence import ConvergenceSignal
from ouroboros.evolution.generation_claims import (
    DurableStepClaims,
    LocalStepClaims,
    current_step_fence,
    owned_lineage_step,
    step_claims_for,
)
from ouroboros.evolution.loop import (
    EvolutionaryLoop,
    EvolutionaryLoopConfig,
    GenerationResult,
)
from ouroboros.evolution.wonder import WonderOutput
from ouroboros.persistence.event_store import EventStore


async def _rewind_claim(
    claims, *, refreshed_at: float | None = None, revoked_at: float | None = None
) -> None:
    """Deterministically age a durable claim row instead of sleeping."""
    from sqlalchemy import update as sa_update

    from ouroboros.persistence.schema import lineage_step_claims_table

    values = {}
    if refreshed_at is not None:
        values["refreshed_at"] = refreshed_at
    if revoked_at is not None:
        values["revoked_at"] = revoked_at
    engine = await claims._engine_once()
    async with engine.begin() as conn:
        await conn.execute(sa_update(lineage_step_claims_table).values(**values))


def _make_seed(seed_id: str = "seed-1") -> Seed:
    ontology = OntologySchema(name="test", description="test ontology", fields=[])
    return Seed(
        goal="test goal",
        ontology_schema=ontology,
        metadata=SeedMetadata(seed_id=seed_id),
    )


def _completed_result(generation_number: int, seed: Seed) -> GenerationResult:
    return GenerationResult(
        generation_number=generation_number,
        seed=seed,
        execution_output="ok",
        evaluation_summary=EvaluationSummary(
            score=0.8,
            final_approved=True,
            highest_stage_passed=1,
        ),
        wonder_output=WonderOutput(),
        phase=GenerationPhase.COMPLETED,
        success=True,
    )


def _build_loop(
    store: EventStore,
    executions: list[int],
    *,
    entered: asyncio.Event | None = None,
    proceed: asyncio.Event | None = None,
) -> EvolutionaryLoop:
    config = EvolutionaryLoopConfig(
        max_generations=10,
        convergence_threshold=0.95,
        min_generations=3,
    )
    loop = EvolutionaryLoop(event_store=store, config=config)
    loop._convergence.evaluate = lambda *_args, **_kwargs: ConvergenceSignal(  # type: ignore[method-assign]
        converged=False,
        reason="continue ownership regression",
        ontology_similarity=0.0,
        generation=1,
    )

    async def _counting_run(**kwargs: Any) -> Result[GenerationResult, Any]:
        executions.append(kwargs["generation_number"])
        if entered is not None:
            entered.set()
        if proceed is not None:
            await proceed.wait()
        return Result.ok(_completed_result(kwargs["generation_number"], kwargs["current_seed"]))

    loop._run_generation_with_watchdog = _counting_run  # type: ignore[method-assign]
    return loop


@pytest.mark.asyncio
async def test_concurrent_evolve_step_executes_exactly_one_generation(tmp_path) -> None:
    """A second caller fails closed while the owner's generation is running."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve.db'}")
    await store.initialize()
    try:
        executions: list[int] = []
        entered = asyncio.Event()
        proceed = asyncio.Event()
        loop_a = _build_loop(store, executions, entered=entered, proceed=proceed)
        loop_b = _build_loop(store, executions)

        winner = asyncio.create_task(
            loop_a.evolve_step("lineage-race", initial_seed=_make_seed("seed-a"))
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        # The owner is mid-generation: the second caller must fail closed
        # before replaying, selecting a generation, or touching the store.
        loser = await loop_b.evolve_step("lineage-race", initial_seed=_make_seed("seed-b"))
        assert loser.is_err, "the concurrent caller must lose the lease"
        loser_message = str(loser.error)
        assert "own" in loser_message or "lease" in loser_message or "claim" in loser_message, (
            f"loser error must state the ownership conflict, got: {loser_message}"
        )
        assert executions == [1], "the loser must not reach the executor"

        proceed.set()
        winner_result = await asyncio.wait_for(winner, timeout=5)
        assert winner_result.is_ok, str(winner_result.error) if winner_result.is_err else ""
        assert executions == [1], (
            f"the external executor ran {len(executions)} times for one generation"
        )

        events = await store.replay_lineage("lineage-race")
        created = [e for e in events if e.type == "lineage.created"]
        assert len(created) == 1, (
            f"the losing caller mutated durable state: {len(created)} lineage.created events"
        )
        completed = [e for e in events if e.type == "lineage.generation.completed"]
        assert len(completed) <= 1, f"duplicate completion events recorded: {len(completed)}"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_released_lease_hands_the_next_caller_a_fresh_replay(tmp_path) -> None:
    """A delayed contender advances the lineage; it never re-runs a done generation."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-seq.db'}")
    await store.initialize()
    try:
        executions: list[int] = []
        first = await _build_loop(store, executions).evolve_step(
            "lineage-seq", initial_seed=_make_seed()
        )
        assert first.is_ok, str(first.error) if first.is_err else ""

        # An explicit seed keeps this test about the lease contract.
        second = await _build_loop(store, executions).evolve_step(
            "lineage-seq", initial_seed=_make_seed()
        )
        assert second.is_ok, str(second.error) if second.is_err else ""
        assert executions == [1, 2], (
            f"the second caller must replay fresh state and advance, got {executions}"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reclaimed_lease_fences_the_stale_owner_before_the_successor_starts(
    tmp_path, monkeypatch
) -> None:
    """The stale owner stops on its own, strictly before a steal is possible.

    A refresh outage means the owner cannot prove it still holds the lease.
    It must fence itself at half the lease — without any reclaimer acting —
    so by the time the lease is stealable the previous attempt has already
    stopped, and the successor never runs alongside it.
    """
    from ouroboros.evolution import loop as loop_module

    class _OutageClaims(LocalStepClaims):
        """Refresh outage: the owner is alive but cannot prove it."""

        refresh_blocked = False

        async def refresh(self, lineage_id: str, claim_token: str) -> bool:
            if self.refresh_blocked:
                raise RuntimeError("simulated refresh outage")
            return await super().refresh(lineage_id, claim_token)

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-fence.db'}")
    await store.initialize()
    try:
        claims = _OutageClaims(lease_seconds=0.6)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)
        monkeypatch.setattr(
            loop_module,
            "owned_lineage_step",
            lambda backend, lineage_id: owned_lineage_step(
                backend, lineage_id, heartbeat_interval=0.01
            ),
        )

        executions: list[int] = []
        entered = asyncio.Event()
        never = asyncio.Event()
        owner_loop = _build_loop(store, executions, entered=entered, proceed=never)

        owner = asyncio.create_task(
            owner_loop.evolve_step("lineage-fence", initial_seed=_make_seed())
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        claims.refresh_blocked = True

        # No reclaimer acts here: the owner must fence itself once half the
        # lease passes without a confirmed refresh.
        owner_result = await asyncio.wait_for(owner, timeout=5)
        assert owner_result.is_err, "a fenced owner must not report success"
        assert executions == [1]

        # Only after the owner has provably stopped does the successor run.
        claims.refresh_blocked = False
        successor = await _build_loop(store, executions).evolve_step(
            "lineage-fence", initial_seed=_make_seed()
        )
        assert successor.is_ok, str(successor.error) if successor.is_err else ""
        assert executions == [1, 1], "the successor re-runs the unfinished generation"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fenced_cancellation_appends_no_lineage_events(tmp_path, monkeypatch) -> None:
    """The stale owner's cancellation writes no lineage history.

    Authority already moved to a reclaimer, so the generic cancellation
    handler must not append lineage.generation.failed for a generation the
    successor may be executing; the successor owns the terminal record.
    """
    from ouroboros.evolution import loop as loop_module

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-fenced-writes.db'}")
    await store.initialize()
    try:
        loss_requested = asyncio.Event()
        loss_observed = asyncio.Event()

        class _ControlledLossClaims(_RecordingClaims):
            """Keep ownership until the executor barrier requests its loss."""

            async def refresh(self, lineage_id: str, claim_token: str) -> bool:
                still_owner = not loss_requested.is_set()
                if not still_owner:
                    loss_observed.set()
                return still_owner

        # Refresh call order is intentionally irrelevant. Under xdist load
        # the heartbeat may run before the lineage-created ownership gate;
        # the old scripted [True, False] sequence therefore fenced before
        # the executor barrier and made the test wait forever for `entered`.
        claims = _ControlledLossClaims(lease_seconds=60.0)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)
        monkeypatch.setattr(
            loop_module,
            "owned_lineage_step",
            lambda backend, lineage_id: owned_lineage_step(
                backend, lineage_id, heartbeat_interval=0.01
            ),
        )

        config = EvolutionaryLoopConfig(
            max_generations=10,
            convergence_threshold=0.95,
            min_generations=1,
        )
        loop = EvolutionaryLoop(event_store=store, config=config)
        entered = asyncio.Event()
        never = asyncio.Event()

        # Stub one layer deeper than the watchdog so the real cancellation
        # handler in _run_generation stays on the path the fence aborts.
        async def _blocked_phases(**kwargs: Any) -> Result[GenerationResult, Any]:
            entered.set()
            await never.wait()
            return Result.ok(_completed_result(kwargs["generation_number"], kwargs["current_seed"]))

        loop._run_generation_phases = _blocked_phases  # type: ignore[method-assign]

        owner = asyncio.create_task(
            loop.evolve_step("lineage-fenced-writes", initial_seed=_make_seed())
        )
        await asyncio.wait_for(entered.wait(), timeout=30)
        loss_requested.set()
        await asyncio.wait_for(loss_observed.wait(), timeout=30)

        owner_result = await asyncio.wait_for(owner, timeout=30)
        assert owner_result.is_err

        events = await store.replay_lineage("lineage-fenced-writes")
        failed = [e for e in events if e.type == "lineage.generation.failed"]
        assert failed == [], "a fenced cancellation appended lineage history it no longer owns"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_lease_lost_before_completion_append_refuses_the_write(tmp_path, monkeypatch) -> None:
    """A stale owner's completion cannot commit after ownership transferred.

    The executor finishes only after a reclaimer has taken the lease; the
    completion append shares one transaction with the token check, so the
    stale write is refused instead of journaling a generation the successor
    may already be executing.
    """
    from sqlalchemy import update as sa_update

    from ouroboros.evolution import loop as loop_module
    from ouroboros.evolution.generation_claims import DurableStepClaims
    from ouroboros.persistence.schema import lineage_step_claims_table

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-inflight.db'}")
    await store.initialize()
    try:
        # Silence the heartbeat so this regression controls both reclaim
        # phases directly. It tests the commit-time token fence, not the
        # separate heartbeat-delivery contract exercised below.
        claims = DurableStepClaims(store.database_url, lease_seconds=600.0)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)
        monkeypatch.setattr(
            loop_module,
            "owned_lineage_step",
            lambda backend, lineage_id: owned_lineage_step(
                backend, lineage_id, heartbeat_interval=3600.0
            ),
        )

        executions: list[int] = []
        entered = asyncio.Event()
        stolen = asyncio.Event()
        owner_loop = _build_loop(store, executions, entered=entered, proceed=stolen)

        owner = asyncio.create_task(
            owner_loop.evolve_step("lineage-inflight", initial_seed=_make_seed())
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        # Transfer ownership while the executor result is still pending:
        # expire the lease row, then reclaim exactly as a successor would —
        # revocation first, takeover after the grace.
        engine = await claims._engine_once()
        async with engine.begin() as conn:
            await conn.execute(sa_update(lineage_step_claims_table).values(refreshed_at=0.0))
        assert await claims.acquire("lineage-inflight", "reclaimer") is False
        await _rewind_claim(claims, refreshed_at=0.0, revoked_at=0.0)
        assert await claims.acquire("lineage-inflight", "reclaimer") is True

        stolen.set()
        owner_result = await asyncio.wait_for(owner, timeout=30)
        assert owner_result.is_err, "a stale owner must not report a persisted completion"

        events = await store.replay_lineage("lineage-inflight")
        completed = [e for e in events if e.type == "lineage.generation.completed"]
        assert completed == [], "a completion event committed after ownership had transferred"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_wall_clock_jump_cannot_steal_a_fresh_lease(tmp_path, monkeypatch) -> None:
    """Lease expiry is decided by database time, not client clocks.

    A contender whose wall clock stepped forward past the lease duration
    must still be denied: otherwise a live, recently refreshed owner could
    be stolen from before its own monotonic half-lease fence can fire.
    """
    from ouroboros.evolution import generation_claims as claims_module
    from ouroboros.evolution.generation_claims import DurableStepClaims

    claims = DurableStepClaims(
        f"sqlite+aiosqlite:///{tmp_path / 'claims-skew.db'}", lease_seconds=300.0
    )
    assert await claims.acquire("lin", "owner") is True

    real_time = claims_module.time.time
    monkeypatch.setattr(claims_module.time, "time", lambda: real_time() + 400.0)
    assert await claims.acquire("lin", "thief") is False, (
        "a stepped client clock stole a fresh lease from a live owner"
    )


@pytest.mark.asyncio
async def test_abandoned_nonterminal_generation_is_resumed_not_skipped(tmp_path) -> None:
    """A crash at a replay-safe phase retries the same generation.

    The resume branch used to advance past any phase other than FAILED or
    INTERRUPTED, so an expired EXECUTING claim skipped its unfinished work.
    """
    from ouroboros.events.lineage import lineage_created, lineage_generation_started

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-abandoned.db'}")
    await store.initialize()
    try:
        seed = _make_seed()
        await store.append(lineage_created("lineage-abandoned", "goal"))
        await store.append(
            lineage_generation_started(
                "lineage-abandoned",
                1,
                "wondering",
                seed.metadata.seed_id,
                json.dumps(seed.to_dict()),
            )
        )

        executions: list[int] = []
        result = await _build_loop(store, executions).evolve_step(
            "lineage-abandoned", initial_seed=seed
        )
        assert result.is_ok, str(result.error) if result.is_err else ""
        assert executions == [1], (
            f"the abandoned generation must be retried, not skipped: ran {executions}"
        )

        seedless = await _build_loop(store, executions).evolve_step(
            "lineage-abandoned-2", initial_seed=None
        )
        assert seedless.is_err, "an empty lineage without a seed reports a clear error"
    finally:
        await store.close()


@pytest.mark.parametrize("_attempt", range(8))
@pytest.mark.asyncio
async def test_revocation_fences_the_owner_before_takeover(
    tmp_path, monkeypatch, _attempt: int
) -> None:
    """Phase one of a reclaim stops the owner inside the grace window.

    A revoked lease refuses the owner's refresh, so a scheduling owner is
    fenced and aborted before the reclaimer's takeover CAS can succeed —
    the successor never runs alongside the stale executor.
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from ouroboros.evolution import loop as loop_module
    from ouroboros.evolution.generation_claims import DurableStepClaims
    from ouroboros.persistence.schema import lineage_step_claims_table

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-revoke.db'}")
    await store.initialize()
    try:
        # Keep expiry far beyond scheduler noise; drive revocation explicitly
        # below while retaining a fast heartbeat for prompt fence delivery.
        class _ObservableClaims(DurableStepClaims):
            """Pause the owner immediately before its durable heartbeat."""

            def __init__(self, database_url: str) -> None:
                super().__init__(database_url, lease_seconds=60.0)
                self.heartbeat_entered = asyncio.Event()
                self.release_heartbeat = asyncio.Event()
                self.revocation_observed = asyncio.Event()

            async def refresh(self, lineage_id: str, claim_token: str) -> bool:
                self.heartbeat_entered.set()
                await self.release_heartbeat.wait()
                still_owner = await super().refresh(lineage_id, claim_token)
                if not still_owner:
                    self.revocation_observed.set()
                return still_owner

        claims = _ObservableClaims(store.database_url)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)
        monkeypatch.setattr(
            loop_module,
            "owned_lineage_step",
            lambda backend, lineage_id: owned_lineage_step(
                backend, lineage_id, heartbeat_interval=0.05
            ),
        )

        executions: list[int] = []
        entered = asyncio.Event()
        never = asyncio.Event()
        owner_loop = _build_loop(store, executions, entered=entered, proceed=never)

        owner = asyncio.create_task(
            owner_loop.evolve_step("lineage-revoke", initial_seed=_make_seed())
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.wait_for(claims.heartbeat_entered.wait(), timeout=5)

        engine = await claims._engine_once()
        async with engine.connect() as conn:
            owner_row = (
                await conn.execute(
                    sa_select(
                        lineage_step_claims_table.c.claim_token,
                        lineage_step_claims_table.c.revoking_token,
                        lineage_step_claims_table.c.revoked_at,
                    ).where(lineage_step_claims_table.c.lineage_id == "lineage-revoke")
                )
            ).one()
        owner_token = owner_row.claim_token
        assert owner_row.revoking_token is None
        assert owner_row.revoked_at is None

        # The owner heartbeat is paused before its durable refresh, so the
        # exact row age below cannot be raced back to fresh before phase one.
        async with engine.begin() as conn:
            await conn.execute(
                sa_update(lineage_step_claims_table)
                .where(lineage_step_claims_table.c.lineage_id == "lineage-revoke")
                .values(refreshed_at=0.0)
            )
        assert await claims.acquire("lineage-revoke", "reclaimer") is False, (
            "reclaim starts with revocation, never immediate takeover"
        )

        # `False` alone is ambiguous: a fresh live owner is also denied. Prove
        # phase one from the durable row before allowing the heartbeat to run.
        async with engine.connect() as conn:
            revoking_row = (
                await conn.execute(
                    sa_select(
                        lineage_step_claims_table.c.claim_token,
                        lineage_step_claims_table.c.revoking_token,
                        lineage_step_claims_table.c.revoked_at,
                    ).where(lineage_step_claims_table.c.lineage_id == "lineage-revoke")
                )
            ).one()
        assert revoking_row.claim_token == owner_token
        assert revoking_row.revoking_token == "reclaimer"
        assert revoking_row.revoked_at is not None

        # The successor stays denied while the owner is still running. The
        # owner's release below is the termination acknowledgement, not the
        # passage of a wall-clock grace period.
        assert await claims.acquire("lineage-revoke", "reclaimer") is False

        claims.release_heartbeat.set()
        await asyncio.wait_for(claims.revocation_observed.wait(), timeout=5)
        owner_result = await asyncio.wait_for(owner, timeout=5)
        assert owner_result.is_err, "a revoked owner must not report success"
        assert executions == [1]

        # Exiting the fenced owner releases its token, which is the durable
        # stop acknowledgement. No wall-clock grace sleep is needed.
        assert await claims.acquire("lineage-revoke", "reclaimer") is True
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_denied_caller_cannot_clear_another_attempts_fence(tmp_path, monkeypatch) -> None:
    """Fence identity is attempt-scoped: unrelated invocations cannot race it.

    While a fenced owner's cancellation is still unwinding, a concurrent
    same-loop caller is denied and finishes first; the stale cancellation
    must still append no lineage history.
    """
    from ouroboros.evolution import loop as loop_module

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-fence-race.db'}")
    await store.initialize()
    try:
        claims = _ControllableFenceClaims(lease_seconds=0.6)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)

        config = EvolutionaryLoopConfig(
            max_generations=10,
            convergence_threshold=0.95,
            min_generations=1,
        )
        loop = EvolutionaryLoop(event_store=store, config=config)
        entered = asyncio.Event()
        cancellation_entered = asyncio.Event()
        denied_done = asyncio.Event()
        executor_entries: list[str] = []
        owner_fence: Any = None

        async def _blocked_phases(**kwargs: Any) -> Result[GenerationResult, Any]:
            nonlocal owner_fence
            fence = current_step_fence()
            assert fence is not None, "the owner's executor must inherit its attempt fence"
            assert not fence.lost.is_set(), "ownership must remain valid through executor entry"
            owner_fence = fence
            executor_entries.append(fence.claim_token)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Hold the cancellation window open until the denied caller
                # has come and gone, reproducing the add/discard race shape.
                assert current_step_fence() is fence
                assert fence.lost.is_set(), "executor cancellation must follow observed lease loss"
                cancellation_entered.set()
                await denied_done.wait()
                raise
            raise AssertionError("unreachable")

        loop._run_generation_phases = _blocked_phases  # type: ignore[method-assign]

        owner = asyncio.create_task(
            loop.evolve_step("lineage-fence-race", initial_seed=_make_seed())
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert owner_fence is not None
        assert executor_entries == [owner_fence.claim_token]
        assert claims.acquire_attempts == [("lineage-fence-race", owner_fence.claim_token)]

        # Ownership remains provably valid through executor entry. Request the
        # loss explicitly, then observe both the rejecting refresh and the
        # cancellation it triggers before introducing the denied caller.
        claims.request_loss()
        await asyncio.wait_for(claims.loss_observed.wait(), timeout=5)
        await asyncio.wait_for(cancellation_entered.wait(), timeout=5)
        assert owner_fence.lost.is_set()
        assert not owner.done(), "the cancellation handler must remain open for the denied caller"

        # The stale attempt is now fenced but deliberately still unwinding. A
        # second caller on the same loop must be denied without disturbing it.
        try:
            denied = await loop.evolve_step(
                "lineage-fence-race", initial_seed=_make_seed("seed-denied")
            )
            assert denied.is_err
            assert not owner.done()
            assert owner_fence.lost.is_set()
            assert executor_entries == [owner_fence.claim_token], (
                "the denied caller must never enter the executor"
            )
            assert claims.acquire_attempts == [("lineage-fence-race", owner_fence.claim_token)], (
                "the outer flight must deny the second caller before lease acquisition"
            )

            events_while_fenced = await store.replay_lineage("lineage-fence-race")
            assert not any(
                event.type == "lineage.generation.failed" for event in events_while_fenced
            ), "the fenced attempt must append no failure while cancellation is still open"
        finally:
            denied_done.set()

        owner_result = await asyncio.wait_for(owner, timeout=10)
        assert owner_result.is_err

        events = await store.replay_lineage("lineage-fence-race")
        failed = [e for e in events if e.type == "lineage.generation.failed"]
        assert failed == [], (
            "a denied caller cleared the stale attempt's fence and a failed "
            "event was appended after authority transferred"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reclaim_completes_through_fresh_public_retries(tmp_path) -> None:
    """Each retry carries a new token; a graced reclaim must still complete.

    Phase two is open to any caller: the revocation carries fencing intent,
    not reclaimer identity, so the realistic evolve_step retry — a fresh
    UUID every invocation — finishes the reclaim a previous call started.
    """
    from ouroboros.evolution.generation_claims import DurableStepClaims

    claims = DurableStepClaims(
        f"sqlite+aiosqlite:///{tmp_path / 'claims-retry.db'}", lease_seconds=600.0
    )
    assert await claims.acquire("lin", "crashed") is True
    await _rewind_claim(claims, refreshed_at=0.0)

    assert await claims.acquire("lin", "retry-a") is False, "first retry marks revocation"
    await _rewind_claim(claims, refreshed_at=0.0, revoked_at=0.0)
    assert await claims.acquire("lin", "retry-b") is True, (
        "a later retry with a different token must complete the reclaim"
    )


@pytest.mark.asyncio
async def test_hanging_refresh_still_self_fences_by_the_deadline() -> None:
    """A refresh that never returns must not suspend the self-fence."""
    from ouroboros.evolution.generation_claims import owned_lineage_step

    class _HangingClaims(_RecordingClaims):
        async def refresh(self, lineage_id: str, claim_token: str) -> bool:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    claims = _HangingClaims(lease_seconds=0.12)
    async with owned_lineage_step(claims, "lin", heartbeat_interval=0.02) as lease:
        await asyncio.wait_for(lease.lost.wait(), timeout=2)
    assert claims.released == ["lin"], "release must survive a hung refresh"


@pytest.mark.asyncio
async def test_takeover_during_preservation_append_refuses_the_write(tmp_path, monkeypatch) -> None:
    """The conductor-preservation failure is token-fenced like every transition."""
    from sqlalchemy import update as sa_update

    from ouroboros.evolution import loop as loop_module
    from ouroboros.evolution.generation_claims import DurableStepClaims
    from ouroboros.persistence.schema import lineage_step_claims_table

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-preserve.db'}")
    await store.initialize()
    try:
        claims = DurableStepClaims(store.database_url, lease_seconds=0.6)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)

        # Silence the heartbeat: this test isolates the append-fence
        # contract, so the owner must NOT be aborted by the lost-lease fence
        # before its executor returns — the write itself has to be refused.
        from ouroboros.evolution.generation_claims import (
            owned_lineage_step as real_owned_lineage_step,
        )

        def _quiet_owned(claims_arg, lineage_id, **_kw):
            return real_owned_lineage_step(claims_arg, lineage_id, heartbeat_interval=1000.0)

        monkeypatch.setattr(loop_module, "owned_lineage_step", _quiet_owned)

        # Force the preservation-failure branch without real directive
        # plumbing: this test is about the append being token-fenced.
        monkeypatch.setattr(
            loop_module,
            "_conductor_preservation_error",
            lambda *_args: "conductor directive dropped by generation",
        )

        config = EvolutionaryLoopConfig(
            max_generations=10,
            convergence_threshold=0.95,
            min_generations=1,
        )
        loop = EvolutionaryLoop(event_store=store, config=config)
        entered = asyncio.Event()
        stolen = asyncio.Event()

        async def _blocked_run(**kwargs: Any) -> Result[GenerationResult, Any]:
            entered.set()
            await stolen.wait()
            return Result.ok(_completed_result(kwargs["generation_number"], kwargs["current_seed"]))

        loop._run_generation_with_watchdog = _blocked_run  # type: ignore[method-assign]

        owner = asyncio.create_task(loop.evolve_step("lineage-preserve", initial_seed=_make_seed()))
        await asyncio.wait_for(entered.wait(), timeout=5)

        engine = await claims._engine_once()
        async with engine.begin() as conn:
            await conn.execute(sa_update(lineage_step_claims_table).values(refreshed_at=0.0))
        assert await claims.acquire("lineage-preserve", "reclaimer") is False
        await asyncio.sleep(0.15)  # grace = lease/6 = 0.1s
        assert await claims.acquire("lineage-preserve", "reclaimer") is True

        stolen.set()
        owner_result = await asyncio.wait_for(owner, timeout=10)
        assert owner_result.is_err

        events = await store.replay_lineage("lineage-preserve")
        failed = [e for e in events if e.type == "lineage.generation.failed"]
        assert failed == [], (
            "a preservation-failure event committed after ownership had transferred"
        )
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_lost_lease_suppresses_a_cancellation_resistant_success(
    tmp_path, monkeypatch
) -> None:
    """An owner that declared its lease lost may never report success.

    A cancellation-resistant executor can swallow the fence's abort and
    return a completed result; the lost state must still fail every
    persistence path closed and surface an error, not a success.
    """
    from ouroboros.evolution import loop as loop_module

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-resistant.db'}")
    await store.initialize()
    try:
        loss_requested = asyncio.Event()

        class _BarrierClaims(_RecordingClaims):
            async def refresh(self, lineage_id: str, claim_token: str) -> bool:
                return not loss_requested.is_set()

        # The heartbeat may start under arbitrary suite load, but cannot
        # report loss until the real executor barrier confirms entry.
        claims = _BarrierClaims(lease_seconds=60.0)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)
        monkeypatch.setattr(
            loop_module,
            "owned_lineage_step",
            lambda backend, lineage_id: owned_lineage_step(
                backend, lineage_id, heartbeat_interval=0.01
            ),
        )

        config = EvolutionaryLoopConfig(
            max_generations=10,
            convergence_threshold=0.95,
            min_generations=1,
        )
        loop = EvolutionaryLoop(event_store=store, config=config)
        entered = asyncio.Event()

        async def _resistant_run(**kwargs: Any) -> Result[GenerationResult, Any]:
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                pass  # swallow the fence's abort and "finish" anyway
            return Result.ok(_completed_result(kwargs["generation_number"], kwargs["current_seed"]))

        loop._run_generation_with_watchdog = _resistant_run  # type: ignore[method-assign]

        owner = asyncio.create_task(
            loop.evolve_step("lineage-resistant", initial_seed=_make_seed())
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        loss_requested.set()

        owner_result = await asyncio.wait_for(owner, timeout=10)
        assert owner_result.is_err, "a lost lease must suppress a cancellation-resistant success"
        events = await store.replay_lineage("lineage-resistant")
        completed = [e for e in events if e.type == "lineage.generation.completed"]
        assert completed == [], "a fenced owner persisted a completion event"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_takeover_waits_for_the_stale_attempt_to_stop(tmp_path, monkeypatch) -> None:
    """Liveness of a fenced-but-running attempt blocks the takeover.

    After the fence fires, the lingering liveness beat keeps the claim row
    fresh, so phase two — which requires an expired lease — stays denied
    until the stale attempt actually exits and releases; the release is the
    termination acknowledgement.
    """
    from sqlalchemy import select as sa_select
    from sqlalchemy import update as sa_update

    from ouroboros.evolution import loop as loop_module
    from ouroboros.evolution.generation_claims import DurableStepClaims
    from ouroboros.persistence.schema import lineage_step_claims_table

    class _ObservableDurableClaims(DurableStepClaims):
        """Expose barriers around the real durable refresh and release operations."""

        def __init__(self, database_url: str) -> None:
            super().__init__(database_url, lease_seconds=60.0)
            self.refresh_entered = asyncio.Event()
            self.refresh_allowed = asyncio.Event()
            self.loss_observed = asyncio.Event()
            self.fenced_beat_observed = asyncio.Event()
            self.hold_fenced_beat = asyncio.Event()
            self.release_entered = asyncio.Event()
            self.release_allowed = asyncio.Event()
            self.fenced_before: Any = None
            self.fenced_after: Any = None

        async def claim_row(self, lineage_id: str) -> Any:
            engine = await self._engine_once()
            async with engine.connect() as conn:
                return (
                    await conn.execute(
                        sa_select(
                            lineage_step_claims_table.c.claim_token,
                            lineage_step_claims_table.c.refreshed_at,
                            lineage_step_claims_table.c.revoking_token,
                            lineage_step_claims_table.c.revoked_at,
                        ).where(lineage_step_claims_table.c.lineage_id == lineage_id)
                    )
                ).one()

        async def refresh(self, lineage_id: str, claim_token: str) -> bool:
            self.refresh_entered.set()
            await self.refresh_allowed.wait()
            still_owner = await super().refresh(lineage_id, claim_token)
            if not still_owner:
                self.loss_observed.set()
            return still_owner

        async def refresh_fenced(self, lineage_id: str, claim_token: str) -> bool:
            self.fenced_before = await self.claim_row(lineage_id)
            refreshed = await super().refresh_fenced(lineage_id, claim_token)
            self.fenced_after = await self.claim_row(lineage_id)
            self.fenced_beat_observed.set()
            # Keep this first real fenced beat in-flight so no later beat can
            # race the row assertions below. The owner finalizer cancels it.
            await self.hold_fenced_beat.wait()
            return refreshed

        async def release(self, lineage_id: str, claim_token: str) -> None:
            self.release_entered.set()
            await self.release_allowed.wait()
            await super().release(lineage_id, claim_token)

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-liveness.db'}")
    await store.initialize()
    try:
        claims = _ObservableDurableClaims(store.database_url)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)
        monkeypatch.setattr(
            loop_module,
            "owned_lineage_step",
            lambda backend, lineage_id: owned_lineage_step(
                backend, lineage_id, heartbeat_interval=0.01
            ),
        )

        config = EvolutionaryLoopConfig(
            max_generations=10,
            convergence_threshold=0.95,
            min_generations=1,
        )
        loop = EvolutionaryLoop(event_store=store, config=config)
        entered = asyncio.Event()
        cancellation_entered = asyncio.Event()
        finish = asyncio.Event()
        owner_fence: Any = None
        executor_entries: list[str] = []

        async def _resistant_run(**kwargs: Any) -> Result[GenerationResult, Any]:
            nonlocal owner_fence
            fence = current_step_fence()
            assert fence is not None
            assert not fence.lost.is_set()
            owner_fence = fence
            executor_entries.append(fence.claim_token)
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                assert fence.lost.is_set()
                cancellation_entered.set()
                await finish.wait()  # stale work keeps running past the abort
            return Result.ok(_completed_result(kwargs["generation_number"], kwargs["current_seed"]))

        loop._run_generation_with_watchdog = _resistant_run  # type: ignore[method-assign]

        owner = asyncio.create_task(loop.evolve_step("lineage-liveness", initial_seed=_make_seed()))
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert owner_fence is not None
        assert executor_entries == [owner_fence.claim_token]
        await asyncio.wait_for(claims.refresh_entered.wait(), timeout=5)

        # Pause the real owner heartbeat before its durable refresh, then
        # expire the exact row and drive production phase-one revocation.
        engine = await claims._engine_once()
        async with engine.begin() as conn:
            await conn.execute(
                sa_update(lineage_step_claims_table)
                .where(
                    lineage_step_claims_table.c.lineage_id == "lineage-liveness",
                    lineage_step_claims_table.c.claim_token == owner_fence.claim_token,
                )
                .values(refreshed_at=0.0)
            )
        assert await claims.acquire("lineage-liveness", "reclaimer") is False
        revoking_row = await claims.claim_row("lineage-liveness")
        assert revoking_row.claim_token == owner_fence.claim_token
        assert revoking_row.revoking_token == "reclaimer"
        assert revoking_row.revoked_at is not None

        # The paused production refresh now observes the durable revocation,
        # fires the fence, and moves into the real fenced-liveness heartbeat.
        claims.refresh_allowed.set()
        await asyncio.wait_for(claims.loss_observed.wait(), timeout=5)
        await asyncio.wait_for(cancellation_entered.wait(), timeout=5)
        await asyncio.wait_for(claims.fenced_beat_observed.wait(), timeout=5)
        assert owner_fence.lost.is_set()
        assert not owner.done(), "stale work must still be running in its cancellation handler"
        assert executor_entries == [owner_fence.claim_token]

        # Production refresh_fenced must update only liveness: the owner and
        # revocation markers survive while refreshed_at becomes current.
        assert claims.fenced_before.claim_token == owner_fence.claim_token
        assert claims.fenced_before.revoking_token == "reclaimer"
        assert claims.fenced_before.revoked_at is not None
        assert claims.fenced_after.claim_token == owner_fence.claim_token
        assert claims.fenced_after.revoking_token == "reclaimer"
        assert claims.fenced_after.revoked_at == claims.fenced_before.revoked_at
        assert claims.fenced_after.refreshed_at > claims.fenced_before.refreshed_at

        # Make revocation old enough for phase two without touching the fresh
        # liveness timestamp. Production acquire must still deny takeover.
        fenced_refreshed_at = claims.fenced_after.refreshed_at
        async with engine.begin() as conn:
            await conn.execute(
                sa_update(lineage_step_claims_table)
                .where(
                    lineage_step_claims_table.c.lineage_id == "lineage-liveness",
                    lineage_step_claims_table.c.claim_token == owner_fence.claim_token,
                )
                .values(revoked_at=0.0)
            )
        aged_revocation_row = await claims.claim_row("lineage-liveness")
        assert aged_revocation_row.refreshed_at == fenced_refreshed_at
        assert aged_revocation_row.revoking_token == "reclaimer"
        assert aged_revocation_row.revoked_at == 0.0
        assert await claims.acquire("lineage-liveness", "reclaimer") is False, (
            "takeover succeeded while the stale attempt was provably alive"
        )
        assert executor_entries == [owner_fence.claim_token]

        # Let stale work stop, but pause the release itself so the test proves
        # that termination acknowledgement — not executor return — opens the
        # takeover gate.
        finish.set()
        await asyncio.wait_for(claims.release_entered.wait(), timeout=5)
        assert not owner.done(), "the owner must remain held until release acknowledges termination"
        assert await claims.acquire("lineage-liveness", "reclaimer") is False
        assert executor_entries == [owner_fence.claim_token]

        claims.release_allowed.set()
        owner_result = await asyncio.wait_for(owner, timeout=10)
        assert owner_result.is_err

        assert await claims.acquire("lineage-liveness", "reclaimer") is True, (
            "the released claim must be acquirable after termination"
        )
        assert executor_entries == [owner_fence.claim_token]
        await claims.release("lineage-liveness", "reclaimer")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_loss_after_admission_fails_the_commit_closed(tmp_path) -> None:
    """A self-fence firing after the append was admitted must refuse commit.

    The outer lost check can be passed before the heartbeat fires; the
    transaction itself consults the lost state again immediately before
    committing, so the admitted write rolls back instead of persisting.
    """
    from ouroboros.events.lineage import lineage_created
    from ouroboros.evolution.generation_claims import DurableStepClaims, StepLease

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'claims-admit.db'}")
    await store.initialize()
    try:
        claims = DurableStepClaims(store.database_url, lease_seconds=600.0)
        assert await claims.acquire("lin", "owner") is True
        lease = StepLease("owner")
        lease.lost.set()

        # Direct inner call: the token is still durably valid, so only the
        # lost-state coupling can refuse this write.
        appended = await claims._append_event_if_owner(
            "lin", "owner", lineage_created("lin", "goal"), lease
        )
        assert appended is False, "a lost attempt committed an admitted write"

        events = await store.replay_lineage("lin")
        assert events == [], "the refused write still persisted an event"
    finally:
        await store.close()


def test_claim_read_locks_the_row_on_non_sqlite_backends() -> None:
    """The ownership read compiles with FOR UPDATE on lock-based backends."""
    from sqlalchemy import select as sa_select
    from sqlalchemy.dialects import postgresql

    from ouroboros.persistence.schema import lineage_step_claims_table

    statement = (
        sa_select(lineage_step_claims_table.c.claim_token)
        .where(lineage_step_claims_table.c.lineage_id == "lin")
        .with_for_update()
    )
    compiled = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in compiled


@pytest.mark.asyncio
async def test_fencing_aware_executor_stops_before_takeover_under_partition() -> None:
    """The context fence reaches effectful work no database check can stop.

    With the owner's database path failed entirely, the lingering beat
    cannot testify liveness — but a fencing-aware executor observes the
    self-fence at half the lease and stops its effects strictly before a
    reclaimer, who needs the full lease plus a grace, can take over.
    """
    from ouroboros.evolution.generation_claims import (
        current_step_fence,
        owned_lineage_step,
    )

    class _PartitionedClaims(LocalStepClaims):
        """Every database interaction fails after acquisition."""

        partitioned = False

        async def refresh(self, lineage_id: str, claim_token: str) -> bool:
            if self.partitioned:
                raise RuntimeError("database unreachable")
            return await super().refresh(lineage_id, claim_token)

        async def refresh_fenced(self, lineage_id: str, claim_token: str) -> bool:
            if self.partitioned:
                raise RuntimeError("database unreachable")
            return await super().refresh_fenced(lineage_id, claim_token)

    claims = _PartitionedClaims(lease_seconds=0.3)
    effects: list[float] = []
    stopped = asyncio.Event()

    async def _fencing_aware_work() -> None:
        fence = current_step_fence()
        assert fence is not None, "the executor must see the attempt's fence"
        while not fence.lost.is_set():
            effects.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.01)
        stopped.set()

    async def _owner() -> None:
        async with owned_lineage_step(claims, "lin"):
            claims.partitioned = True
            work = asyncio.create_task(_fencing_aware_work())
            await asyncio.wait_for(stopped.wait(), timeout=5)
            await work

    owner = asyncio.create_task(_owner())
    # The reclaimer cannot acquire while the work is running: it needs the
    # full lease (0.3s) plus the grace, while the fencing-aware work stops
    # at the half-lease self-fence (0.15s).
    await asyncio.wait_for(stopped.wait(), timeout=5)
    assert await claims.acquire("lin", "reclaimer") is False, (
        "the reclaimer acquired before the lease could have expired"
    )
    await asyncio.wait_for(owner, timeout=5)
    # After the owner exits (releasing), the reclaimer proceeds normally.
    assert await claims.acquire("lin", "reclaimer") is True


class _RecordingClaims:
    """Fake claims whose refresh behavior is scripted per test.

    ``refresh_results`` is consumed call by call (last value repeats), so a
    test can let the ownership gate on the creation append pass and then
    report the loss on the first heartbeat.
    """

    def __init__(
        self,
        *,
        refresh_result: bool = True,
        refresh_raises: bool = False,
        lease_seconds: float = 0.09,
        refresh_results: list[bool] | None = None,
    ) -> None:
        self.lease_seconds = lease_seconds
        self.released: list[str] = []
        self._refresh_result = refresh_result
        self._refresh_raises = refresh_raises
        self._refresh_results = list(refresh_results) if refresh_results else None

    async def acquire(self, lineage_id: str, claim_token: str) -> bool:
        return True

    async def refresh(self, lineage_id: str, claim_token: str) -> bool:
        if self._refresh_raises:
            raise RuntimeError("simulated refresh outage")
        if self._refresh_results is not None:
            if len(self._refresh_results) > 1:
                return self._refresh_results.pop(0)
            return self._refresh_results[0]
        return self._refresh_result

    async def refresh_fenced(self, lineage_id: str, claim_token: str) -> bool:
        return True

    async def release(self, lineage_id: str, claim_token: str) -> None:
        self.released.append(lineage_id)


class _ControllableFenceClaims(_RecordingClaims):
    """Keep ownership valid until a test explicitly asks the heartbeat to lose it."""

    def __init__(self, *, lease_seconds: float) -> None:
        super().__init__(lease_seconds=lease_seconds)
        self.loss_requested = asyncio.Event()
        self.loss_observed = asyncio.Event()
        self.acquire_attempts: list[tuple[str, str]] = []
        self._owner_token: str | None = None

    async def acquire(self, lineage_id: str, claim_token: str) -> bool:
        self.acquire_attempts.append((lineage_id, claim_token))
        if self._owner_token is not None:
            return False
        self._owner_token = claim_token
        return True

    async def refresh(self, lineage_id: str, claim_token: str) -> bool:
        assert claim_token == self._owner_token
        if self.loss_requested.is_set():
            self.loss_observed.set()
            return False
        return True

    async def release(self, lineage_id: str, claim_token: str) -> None:
        assert claim_token == self._owner_token
        await super().release(lineage_id, claim_token)

    def request_loss(self) -> None:
        self.loss_requested.set()


class TestOwnedLineageStep:
    @pytest.mark.asyncio
    async def test_lost_refresh_fires_the_lease_handle(self) -> None:
        claims = _RecordingClaims(refresh_result=False)
        async with owned_lineage_step(claims, "lin", heartbeat_interval=0.02) as lease:
            await asyncio.wait_for(lease.lost.wait(), timeout=2)
        assert claims.released == ["lin"], "release must run even after a lost lease"

    @pytest.mark.asyncio
    async def test_refresh_exception_never_bypasses_release(self) -> None:
        claims = _RecordingClaims(refresh_raises=True, lease_seconds=60.0)
        async with owned_lineage_step(claims, "lin", heartbeat_interval=0.02) as lease:
            await asyncio.sleep(0.08)
            assert not lease.lost.is_set(), "a transient refresh failure is not a loss of ownership"
        assert claims.released == ["lin"], "release must survive heartbeat exceptions"

    @pytest.mark.asyncio
    async def test_unprovable_ownership_self_fences_at_half_the_lease(self) -> None:
        """A refresh outage must fence the owner before a steal is possible."""
        claims = _RecordingClaims(refresh_raises=True, lease_seconds=0.08)
        async with owned_lineage_step(claims, "lin", heartbeat_interval=0.02) as lease:
            await asyncio.wait_for(lease.lost.wait(), timeout=2)
        assert claims.released == ["lin"]


class TestDurableClaimLease:
    """The explicit lease/CAS contract for crash recovery."""

    def _claims(self, tmp_path, lease_seconds: float) -> DurableStepClaims:
        return DurableStepClaims(
            f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}", lease_seconds=lease_seconds
        )

    @pytest.mark.asyncio
    async def test_fresh_claim_blocks_and_expired_claim_is_reclaimable(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=600.0)
        assert await claims.acquire("lin", "owner-a") is True
        assert await claims.acquire("lin", "owner-b") is False, (
            "a fresh claim must not be stealable"
        )
        await _rewind_claim(claims, refreshed_at=0.0)
        assert await claims.acquire("lin", "owner-b") is False, (
            "reclaim is two-phase: the first call only marks revocation"
        )
        assert await claims.refresh("lin", "owner-a") is False, (
            "the presumed-crashed owner must observe the revocation"
        )
        await _rewind_claim(claims, refreshed_at=0.0, revoked_at=0.0)
        assert await claims.acquire("lin", "owner-b") is True, (
            "an expired claim must be reclaimable after the grace"
        )
        assert await claims.refresh("lin", "owner-b") is True

    @pytest.mark.asyncio
    async def test_refresh_keeps_the_lease_from_expiring(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=0.3)
        assert await claims.acquire("lin", "owner-a") is True
        for _ in range(3):
            await asyncio.sleep(0.15)
            assert await claims.refresh("lin", "owner-a") is True
        assert await claims.acquire("lin", "owner-b") is False, (
            "a heartbeating owner must not be stolen from"
        )

    @pytest.mark.asyncio
    async def test_release_hands_ownership_to_the_next_caller(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=60.0)
        assert await claims.acquire("lin", "owner-a") is True
        await claims.release("lin", "owner-a")
        assert await claims.acquire("lin", "owner-b") is True

    @pytest.mark.asyncio
    async def test_release_with_foreign_token_is_a_no_op(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=60.0)
        assert await claims.acquire("lin", "owner-a") is True
        await claims.release("lin", "owner-b")
        assert await claims.acquire("lin", "owner-c") is False, (
            "a stale caller's release must not drop the live owner's claim"
        )

    @pytest.mark.asyncio
    async def test_concurrent_reclaim_of_expired_lease_has_one_winner(self, tmp_path) -> None:
        # A huge lease makes the grace impossible to cross by scheduler
        # delay; both phase transitions are driven by explicit row aging.
        claims = self._claims(tmp_path, lease_seconds=600.0)
        assert await claims.acquire("lin", "crashed") is True
        await _rewind_claim(claims, refreshed_at=0.0)
        first_round = await asyncio.gather(
            claims.acquire("lin", "reclaim-a"),
            claims.acquire("lin", "reclaim-b"),
        )
        assert first_round == [False, False], (
            "the revocation phase never grants ownership immediately"
        )
        await _rewind_claim(claims, refreshed_at=0.0, revoked_at=0.0)
        outcomes = await asyncio.gather(
            claims.acquire("lin", "reclaim-a"),
            claims.acquire("lin", "reclaim-b"),
        )
        assert sorted(outcomes) == [False, True], (
            f"exactly one reclaimer may win the takeover CAS, got {outcomes}"
        )


class TestLocalClaimNamespacing:
    def test_unrelated_stores_do_not_contend(self) -> None:
        class _FakeStore:
            pass

        store_a = _FakeStore()
        store_b = _FakeStore()
        assert step_claims_for(store_a) is not step_claims_for(store_b), (
            "independent stores must not share a claim namespace"
        )
        assert step_claims_for(store_a) is step_claims_for(store_a), (
            "one store must keep one claim table"
        )


class TestClaimBackendClassification:
    def test_every_sqlite_in_memory_form_is_local(self, tmp_path) -> None:
        from ouroboros.evolution.generation_claims import _is_shared_database_url

        assert _is_shared_database_url("sqlite+aiosqlite://") is False
        assert _is_shared_database_url("sqlite+aiosqlite:///:memory:") is False
        assert _is_shared_database_url(f"sqlite+aiosqlite:///{tmp_path / 'a.db'}") is True

    @pytest.mark.asyncio
    async def test_ownership_holds_on_a_pathless_in_memory_store(self) -> None:
        """The supported pathless SQLite URL must not crash the claim path."""
        store = EventStore("sqlite+aiosqlite://")
        await store.initialize()
        try:
            assert isinstance(step_claims_for(store), LocalStepClaims)
            executions: list[int] = []
            result = await _build_loop(store, executions).evolve_step(
                "lineage-memory", initial_seed=_make_seed()
            )
            assert result.is_ok, str(result.error) if result.is_err else ""
            assert executions == [1]
        finally:
            await store.close()
