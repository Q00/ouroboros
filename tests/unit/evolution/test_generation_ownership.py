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
from typing import Any
from unittest.mock import MagicMock

import pytest

from ouroboros.core.lineage import (
    EvaluationSummary,
    GenerationPhase,
    OntologySchema,
)
from ouroboros.core.seed import Seed
from ouroboros.core.types import Result
from ouroboros.evolution.generation_claims import (
    DurableStepClaims,
    LocalStepClaims,
    owned_lineage_step,
    step_claims_for,
)
from ouroboros.evolution.loop import (
    EvolutionaryLoop,
    EvolutionaryLoopConfig,
    GenerationResult,
)
from ouroboros.persistence.event_store import EventStore


def _make_seed(seed_id: str = "seed-1") -> Seed:
    ontology = OntologySchema(name="test", description="test ontology", fields=[])
    seed = MagicMock(spec=Seed)
    seed.goal = "test goal"
    seed.metadata = MagicMock()
    seed.metadata.seed_id = seed_id
    seed.metadata.parent_seed_id = None
    seed.ontology_schema = ontology
    seed.to_dict.return_value = {"seed_id": seed_id}
    return seed


def _completed_result(generation_number: int, seed: Seed) -> GenerationResult:
    wonder_output = MagicMock()
    wonder_output.questions = ()
    return GenerationResult(
        generation_number=generation_number,
        seed=seed,
        execution_output="ok",
        evaluation_summary=EvaluationSummary(
            score=0.8,
            final_approved=True,
            highest_stage_passed=1,
        ),
        wonder_output=wonder_output,
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
        min_generations=1,
    )
    loop = EvolutionaryLoop(event_store=store, config=config)

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

        # An explicit seed keeps this test about the lease contract, not
        # about reconstructing a MagicMock seed from persisted JSON.
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
async def test_lost_lease_fences_the_running_generation(tmp_path, monkeypatch) -> None:
    """A reclaimed lease aborts the presumed-crashed owner's in-flight work."""
    from ouroboros.evolution import loop as loop_module

    class _OutageClaims(LocalStepClaims):
        """Refresh outage: the owner is alive but cannot prove it."""

        refresh_blocked = True

        async def refresh(self, lineage_id: str, claim_token: str) -> bool:
            if self.refresh_blocked:
                raise RuntimeError("simulated refresh outage")
            return await super().refresh(lineage_id, claim_token)

    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-fence.db'}")
    await store.initialize()
    try:
        claims = _OutageClaims(lease_seconds=0.15)
        monkeypatch.setattr(loop_module, "step_claims_for", lambda _store: claims)

        executions: list[int] = []
        entered = asyncio.Event()
        never = asyncio.Event()
        owner_loop = _build_loop(store, executions, entered=entered, proceed=never)

        owner = asyncio.create_task(
            owner_loop.evolve_step("lineage-fence", initial_seed=_make_seed())
        )
        await asyncio.wait_for(entered.wait(), timeout=5)

        # The refresh outage keeps the lease unrefreshed past expiry; a
        # reclaimer then steals it while the owner is still mid-generation.
        await asyncio.sleep(0.2)
        assert await LocalStepClaims.acquire(claims, "lineage-fence", "reclaimer") is True

        # Once refresh works again it reports the loss; the fence aborts the
        # blocked generation instead of letting it run alongside the thief.
        claims.refresh_blocked = False
        owner_result = await asyncio.wait_for(owner, timeout=5)
        assert owner_result.is_err, "a fenced owner must not report success"
    finally:
        await store.close()


class _RecordingClaims:
    """Fake claims whose refresh behavior is scripted per test."""

    def __init__(self, *, refresh_result: bool = True, refresh_raises: bool = False) -> None:
        self.lease_seconds = 0.09
        self.released: list[str] = []
        self._refresh_result = refresh_result
        self._refresh_raises = refresh_raises

    async def acquire(self, lineage_id: str, claim_token: str) -> bool:
        return True

    async def refresh(self, lineage_id: str, claim_token: str) -> bool:
        if self._refresh_raises:
            raise RuntimeError("simulated refresh outage")
        return self._refresh_result

    async def release(self, lineage_id: str, claim_token: str) -> None:
        self.released.append(lineage_id)


class TestOwnedLineageStep:
    @pytest.mark.asyncio
    async def test_lost_refresh_fires_the_lease_handle(self) -> None:
        claims = _RecordingClaims(refresh_result=False)
        async with owned_lineage_step(claims, "lin", heartbeat_interval=0.02) as lease:
            await asyncio.wait_for(lease.lost.wait(), timeout=2)
        assert claims.released == ["lin"], "release must run even after a lost lease"

    @pytest.mark.asyncio
    async def test_refresh_exception_never_bypasses_release(self) -> None:
        claims = _RecordingClaims(refresh_raises=True)
        async with owned_lineage_step(claims, "lin", heartbeat_interval=0.02) as lease:
            await asyncio.sleep(0.08)
            assert not lease.lost.is_set(), "a transient refresh failure is not a loss of ownership"
        assert claims.released == ["lin"], "release must survive heartbeat exceptions"


class TestDurableClaimLease:
    """The explicit lease/CAS contract for crash recovery."""

    def _claims(self, tmp_path, lease_seconds: float) -> DurableStepClaims:
        return DurableStepClaims(
            f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}", lease_seconds=lease_seconds
        )

    @pytest.mark.asyncio
    async def test_fresh_claim_blocks_and_expired_claim_is_reclaimable(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=0.2)
        assert await claims.acquire("lin", "owner-a") is True
        assert await claims.acquire("lin", "owner-b") is False, (
            "a fresh claim must not be stealable"
        )
        await asyncio.sleep(0.25)
        assert await claims.acquire("lin", "owner-b") is True, (
            "an expired claim must be reclaimable"
        )
        assert await claims.refresh("lin", "owner-a") is False, (
            "the presumed-crashed owner must observe the loss"
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
        claims = self._claims(tmp_path, lease_seconds=0.1)
        assert await claims.acquire("lin", "crashed") is True
        await asyncio.sleep(0.15)
        outcomes = await asyncio.gather(
            claims.acquire("lin", "reclaim-a"),
            claims.acquire("lin", "reclaim-b"),
        )
        assert sorted(outcomes) == [False, True], (
            f"exactly one reclaimer may win the CAS steal, got {outcomes}"
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
