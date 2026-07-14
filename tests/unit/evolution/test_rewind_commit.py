"""Tests for the canonical committed-rewind boundary."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from ouroboros.core.lineage import GenerationRecord, OntologyLineage
from ouroboros.core.seed import OntologyField, OntologySchema
from ouroboros.events.base import BaseEvent
from ouroboros.evolution.loop import EvolutionaryLoop
from ouroboros.evolution.rewind import RewindObservationSnapshot


def _lineage() -> OntologyLineage:
    ontology = OntologySchema(
        name="Rewind",
        description="Rewind test ontology",
        fields=(OntologyField(name="id", field_type="string", description="ID"),),
    )
    return OntologyLineage(
        lineage_id="lin_rewind_commit",
        goal="Test committed rewind",
        generations=tuple(
            GenerationRecord(
                generation_number=number,
                seed_id=f"seed-{number}",
                ontology_snapshot=ontology,
            )
            for number in (1, 2, 3)
        ),
    )


class _Store:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[BaseEvent] = []

    async def append(self, event) -> None:
        if self.fail:
            raise RuntimeError("append unavailable")
        self.events.append(event)


class _Observer:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.snapshots: list[RewindObservationSnapshot] = []

    async def observe(self, snapshot: RewindObservationSnapshot) -> None:
        self.snapshots.append(snapshot)
        if self.fail:
            raise RuntimeError("observer unavailable")


@pytest.mark.asyncio
async def test_rewind_captures_exact_committed_event_identity() -> None:
    store = _Store()
    observer = _Observer()
    loop = EvolutionaryLoop(store, rewind_observer=observer)  # type: ignore[arg-type]

    result = await loop.rewind_to(_lineage(), 1)

    assert result.is_ok
    committed = result.value
    assert len(store.events) == 1
    persisted = store.events[0]
    assert persisted.type == "lineage.rewound"
    assert committed.rewind_event_id == persisted.id
    assert committed.rewind_occurred_at == persisted.timestamp
    assert committed.lineage.current_generation == 1
    assert committed.from_generation == 3
    assert committed.to_generation == 1
    assert observer.snapshots == [committed.observation_snapshot()]
    assert not hasattr(observer.snapshots[0], "lineage")


@pytest.mark.asyncio
async def test_observer_failure_cannot_change_committed_result() -> None:
    store = _Store()
    observer = _Observer(fail=True)
    loop = EvolutionaryLoop(store, rewind_observer=observer)  # type: ignore[arg-type]

    result = await loop.rewind_to(_lineage(), 2)

    assert result.is_ok
    assert result.value.rewind_event_id == store.events[0].id
    assert result.value.lineage.current_generation == 2
    assert len(observer.snapshots) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [0, 4])
async def test_invalid_target_appends_nothing_and_skips_observer(target: int) -> None:
    store = _Store()
    observer = _Observer()
    loop = EvolutionaryLoop(store, rewind_observer=observer)  # type: ignore[arg-type]

    result = await loop.rewind_to(_lineage(), target)

    assert result.is_err
    assert store.events == []
    assert observer.snapshots == []


@pytest.mark.asyncio
async def test_current_generation_appends_nothing_and_skips_observer() -> None:
    store = _Store()
    observer = _Observer()
    loop = EvolutionaryLoop(store, rewind_observer=observer)  # type: ignore[arg-type]

    result = await loop.rewind_to(_lineage(), 3)

    assert result.is_err
    assert "Already at generation 3" in str(result.error)
    assert store.events == []
    assert observer.snapshots == []


@pytest.mark.asyncio
async def test_append_failure_skips_observer() -> None:
    store = _Store(fail=True)
    observer = _Observer()
    loop = EvolutionaryLoop(store, rewind_observer=observer)  # type: ignore[arg-type]

    result = await loop.rewind_to(_lineage(), 1)

    assert result.is_err
    assert "Failed to append rewind event" in str(result.error)
    assert observer.snapshots == []


def test_observation_snapshot_is_immutable() -> None:
    snapshot = RewindObservationSnapshot(
        lineage_id="lin-1",
        from_generation=3,
        to_generation=1,
        rewind_event_id="event-1",
        rewind_occurred_at=_lineage().created_at,
    )

    with pytest.raises(FrozenInstanceError):
        snapshot.lineage_id = "mutated"  # type: ignore[misc]
