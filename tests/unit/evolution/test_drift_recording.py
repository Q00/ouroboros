"""Generation drift recording remains best-effort and durable."""

from unittest.mock import AsyncMock

import pytest

from ouroboros.core.seed import OntologySchema, Seed, SeedMetadata
from ouroboros.evolution.drift_recording import record_generation_drift


def _seed(goal: str = "Preserve the verified outcome") -> Seed:
    return Seed(
        goal=goal,
        acceptance_criteria=("Outcome holds",),
        ontology_schema=OntologySchema(name="outcome", description="Verified outcome"),
        metadata=SeedMetadata(seed_id="seed_drift", ambiguity_score=0.1),
    )


@pytest.mark.asyncio
async def test_missing_execution_output_does_not_emit_drift() -> None:
    event_store = AsyncMock()

    await record_generation_drift(event_store, "lin_1", 2, _seed(), None)

    event_store.append.assert_not_awaited()


@pytest.mark.asyncio
async def test_execution_output_emits_lineage_bound_drift_event() -> None:
    event_store = AsyncMock()
    seed = _seed()

    await record_generation_drift(event_store, "lin_1", 2, seed, "verified output")

    event_store.append.assert_awaited_once()
    event = event_store.append.await_args.args[0]
    assert event.type == "observability.drift.measured"
    assert event.aggregate_id == "lin_1"
    assert event.data["seed_id"] == seed.metadata.seed_id
    assert event.data["iteration"] == 2


@pytest.mark.asyncio
async def test_existing_semantic_alignment_is_reused_for_goal_drift() -> None:
    event_store = AsyncMock()
    seed = _seed("CSV에서 유효 행과 무효 행을 세고 평균 금액을 JSON으로 출력한다")

    await record_generation_drift(
        event_store,
        "lin_1",
        2,
        seed,
        "Count valid and invalid CSV rows, calculate the average amount, and output JSON",
        goal_alignment=0.95,
    )

    event = event_store.append.await_args.args[0]
    assert event.data["goal_drift"] == pytest.approx(0.05)
    assert event.data["constraint_drift"] == 0.0
    assert event.data["ontology_drift"] == 1.0
    assert event.data["combined_drift"] == pytest.approx(0.225)


@pytest.mark.asyncio
async def test_missing_or_invalid_alignment_keeps_lexical_goal_drift() -> None:
    from math import inf, nan

    from ouroboros.observability.drift import calculate_goal_drift

    seed = _seed()
    expected = calculate_goal_drift("verified output", seed)
    for invalid_alignment in (None, nan, inf, -0.1, 1.1):
        event_store = AsyncMock()
        await record_generation_drift(
            event_store,
            "lin_1",
            2,
            seed,
            "verified output",
            goal_alignment=invalid_alignment,
        )

        event = event_store.append.await_args.args[0]
        assert event.data["goal_drift"] == expected


@pytest.mark.asyncio
async def test_drift_failure_does_not_fail_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    event_store = AsyncMock()

    def fail_measurement(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("telemetry unavailable")

    monkeypatch.setattr(
        "ouroboros.evolution.drift_recording.DriftMeasurement.measure",
        fail_measurement,
    )

    await record_generation_drift(event_store, "lin_1", 2, _seed(), "verified output")

    event_store.append.assert_not_awaited()
