"""#1889: exactly one caller may own a lineage generation at a time.

Two concurrent ``evolve_step`` calls used to replay the same lineage state,
select the same generation number, run the external executor twice, and
append duplicate completion and terminal events. Ownership must be a durable
claim at the store boundary: the loser fails closed before any external
work, and a crashed owner's claim becomes reclaimable only after its lease
expires without a heartbeat.
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


def _build_loop(store: EventStore, executions: list[int]) -> EvolutionaryLoop:
    config = EvolutionaryLoopConfig(
        max_generations=10,
        convergence_threshold=0.95,
        min_generations=1,
    )
    loop = EvolutionaryLoop(event_store=store, config=config)

    async def _counting_run(**kwargs: Any) -> Result[GenerationResult, Any]:
        executions.append(kwargs["generation_number"])
        return Result.ok(_completed_result(kwargs["generation_number"], kwargs["current_seed"]))

    loop._run_generation_with_watchdog = _counting_run  # type: ignore[method-assign]
    return loop


@pytest.mark.asyncio
async def test_concurrent_evolve_step_executes_exactly_one_generation(tmp_path) -> None:
    """The issue's barrier reproduction: one executor run, one completion."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve.db'}")
    await store.initialize()
    try:
        executions: list[int] = []
        loop_a = _build_loop(store, executions)
        loop_b = _build_loop(store, executions)

        # Both callers must hold their replay snapshot before either appends:
        # this is the deterministic shape of the reported race.
        barrier_calls = 0
        both_replayed = asyncio.Event()
        real_replay = store.replay_lineage

        async def gated_replay(lineage_id: str):
            nonlocal barrier_calls
            events = await real_replay(lineage_id)
            barrier_calls += 1
            if barrier_calls >= 2:
                both_replayed.set()
            await both_replayed.wait()
            return events

        store.replay_lineage = gated_replay  # type: ignore[method-assign]

        results = await asyncio.gather(
            loop_a.evolve_step("lineage-race", initial_seed=_make_seed("seed-a")),
            loop_b.evolve_step("lineage-race", initial_seed=_make_seed("seed-b")),
        )

        ok_results = [r for r in results if r.is_ok]
        err_results = [r for r in results if r.is_err]
        assert len(ok_results) == 1, (
            f"exactly one caller may win the generation, got {len(ok_results)} winners"
        )
        assert len(executions) == 1, (
            f"the external executor ran {len(executions)} times for one generation"
        )
        assert len(err_results) == 1
        loser_message = str(err_results[0].error)
        assert "own" in loser_message or "claim" in loser_message, (
            f"loser error must state the ownership conflict, got: {loser_message}"
        )

        events = await real_replay("lineage-race")
        completed = [e for e in events if e.type == "lineage.generation.completed"]
        assert len(completed) <= 1, f"duplicate completion events recorded: {len(completed)}"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sequential_evolve_steps_reacquire_released_claims(tmp_path) -> None:
    """A completed generation releases its claim; the next call proceeds."""
    store = EventStore(f"sqlite+aiosqlite:///{tmp_path / 'evolve-seq.db'}")
    await store.initialize()
    try:
        executions: list[int] = []
        loop = _build_loop(store, executions)

        first = await loop.evolve_step("lineage-seq", initial_seed=_make_seed())
        assert first.is_ok, str(first.error) if first.is_err else ""
        # An explicit seed keeps this test about claim release, not about
        # reconstructing a MagicMock seed from persisted JSON.
        second = await loop.evolve_step("lineage-seq", initial_seed=_make_seed())
        assert second.is_ok, str(second.error) if second.is_err else ""
        assert executions == [1, 2]
    finally:
        await store.close()


class TestDurableClaimLease:
    """The explicit lease/CAS contract for crash recovery."""

    def _claims(self, tmp_path, lease_seconds: float):
        from ouroboros.evolution.generation_claims import DurableGenerationClaims

        return DurableGenerationClaims(
            f"sqlite+aiosqlite:///{tmp_path / 'claims.db'}", lease_seconds=lease_seconds
        )

    @pytest.mark.asyncio
    async def test_fresh_claim_blocks_and_expired_claim_is_reclaimable(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=0.2)
        assert await claims.acquire("lin", 1, "owner-a") is True
        assert await claims.acquire("lin", 1, "owner-b") is False, (
            "a fresh claim must not be stealable"
        )
        await asyncio.sleep(0.25)
        assert await claims.acquire("lin", 1, "owner-b") is True, (
            "an expired claim must be reclaimable"
        )
        assert await claims.refresh("lin", 1, "owner-a") is False, (
            "the presumed-crashed owner must observe the loss"
        )
        assert await claims.refresh("lin", 1, "owner-b") is True

    @pytest.mark.asyncio
    async def test_refresh_keeps_the_lease_from_expiring(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=0.3)
        assert await claims.acquire("lin", 1, "owner-a") is True
        for _ in range(3):
            await asyncio.sleep(0.15)
            assert await claims.refresh("lin", 1, "owner-a") is True
        assert await claims.acquire("lin", 1, "owner-b") is False, (
            "a heartbeating owner must not be stolen from"
        )

    @pytest.mark.asyncio
    async def test_release_hands_ownership_to_the_next_caller(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=60.0)
        assert await claims.acquire("lin", 1, "owner-a") is True
        await claims.release("lin", 1, "owner-a")
        assert await claims.acquire("lin", 1, "owner-b") is True

    @pytest.mark.asyncio
    async def test_release_with_foreign_token_is_a_no_op(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=60.0)
        assert await claims.acquire("lin", 1, "owner-a") is True
        await claims.release("lin", 1, "owner-b")
        assert await claims.acquire("lin", 1, "owner-c") is False, (
            "a stale caller's release must not drop the live owner's claim"
        )

    @pytest.mark.asyncio
    async def test_concurrent_reclaim_of_expired_lease_has_one_winner(self, tmp_path) -> None:
        claims = self._claims(tmp_path, lease_seconds=0.1)
        assert await claims.acquire("lin", 1, "crashed") is True
        await asyncio.sleep(0.15)
        outcomes = await asyncio.gather(
            claims.acquire("lin", 1, "reclaim-a"),
            claims.acquire("lin", 1, "reclaim-b"),
        )
        assert sorted(outcomes) == [False, True], (
            f"exactly one reclaimer may win the CAS steal, got {outcomes}"
        )
