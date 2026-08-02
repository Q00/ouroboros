"""End-to-end bloat guard for disposable AgentProcess results."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ouroboros.core.disposable_memory import (
    MAX_DISPOSABLE_ARTIFACT_BYTES,
    MAX_DISPOSABLE_ENVELOPE_BYTES,
    DisposableResultEnvelope,
)
from ouroboros.events.base import BaseEvent
from ouroboros.orchestrator.disposable_memory import DisposableMemory
from ouroboros.persistence.artifact_store import (
    ContentAddressedArtifactStore,
    canonical_artifact_bytes,
)
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore


class _EventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)

    async def replay(self, aggregate_type: str, aggregate_id: str) -> list[BaseEvent]:
        return [
            event
            for event in self.appended
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]


def _service(tmp_path: Path) -> tuple[DisposableMemory, _EventStore]:
    event_store = _EventStore()
    service = DisposableMemory(
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        event_store=event_store,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints"),
    )
    return service, event_store


@pytest.mark.asyncio
async def test_one_mib_child_output_returns_sub_four_kib_parent_envelope(tmp_path: Path) -> None:
    service, event_store = _service(tmp_path)
    empty_size = len(canonical_artifact_bytes({"output": ""}))
    body = {"output": "x" * (MAX_DISPOSABLE_ARTIFACT_BYTES - empty_size)}
    assert len(canonical_artifact_bytes(body)) == MAX_DISPOSABLE_ARTIFACT_BYTES

    async def child_work(_handle):
        return body

    envelope = await service.run(
        intent="qa-judge",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id="01K1DISPOSABLEMEMORY00001",
        events_emitted_count=7,
    )

    serialized_envelope = json.dumps(envelope.model_dump(mode="json"), separators=(",", ":"))
    assert len(serialized_envelope.encode()) < MAX_DISPOSABLE_ENVELOPE_BYTES
    assert "output" not in DisposableResultEnvelope.model_fields
    assert "transcript" not in DisposableResultEnvelope.model_fields

    reference = next(event for event in event_store.appended if event.type == "artifact.referenced")
    serialized_event = json.dumps(reference.data, separators=(",", ":"))
    assert len(serialized_event.encode()) < MAX_DISPOSABLE_ENVELOPE_BYTES
    assert "x" * 100 not in serialized_event
    assert reference.aggregate_type == "contract"
    assert reference.aggregate_id == envelope.contract_id

    fetched = service.fetch(envelope.contract_id)
    assert fetched.body == body
    assert len(canonical_artifact_bytes(fetched.body)) == MAX_DISPOSABLE_ARTIFACT_BYTES


@pytest.mark.asyncio
async def test_replay_reads_artifact_without_rerunning_child(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    calls = 0

    async def child_work(_handle):
        nonlocal calls
        calls += 1
        return {"calls": calls}

    envelope = await service.run(
        intent="contrarian",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id="01K1DISPOSABLEMEMORY00002",
    )
    replayed = service.replay(envelope.contract_id)

    assert replayed.body == {"calls": 1}
    assert calls == 1


@pytest.mark.asyncio
async def test_force_rerun_requires_and_uses_new_contract_id(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    calls = 0

    async def child_work(_handle):
        nonlocal calls
        calls += 1
        return {"calls": calls}

    original = await service.run(
        intent="evaluator",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id="01K1DISPOSABLEMEMORY00003",
    )
    with pytest.raises(ValueError, match="new contract_id"):
        await service.force_rerun(
            original.contract_id,
            intent="evaluator",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            new_contract_id=original.contract_id,
        )

    replacement = await service.force_rerun(
        original.contract_id,
        intent="evaluator",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        new_contract_id="01K1DISPOSABLEMEMORY00004",
    )
    assert replacement.contract_id != original.contract_id
    assert service.fetch(original.contract_id).body == {"calls": 1}
    assert service.fetch(replacement.contract_id).body == {"calls": 2}


@pytest.mark.asyncio
async def test_reference_event_is_idempotent_when_same_contract_is_recovered(
    tmp_path: Path,
) -> None:
    service, event_store = _service(tmp_path)

    async def child_work(_handle):
        return {"stable": True}

    first = await service.run(
        intent="researcher",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id="01K1DISPOSABLEMEMORY00005",
    )
    second = await service.run(
        intent="researcher",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id="01K1DISPOSABLEMEMORY00005",
    )

    assert first.artifact_ref == second.artifact_ref
    references = [event for event in event_store.appended if event.type == "artifact.referenced"]
    assert len(references) == 1


@pytest.mark.asyncio
async def test_bounded_reference_round_trips_through_real_event_store(tmp_path: Path) -> None:
    event_store = EventStore("sqlite+aiosqlite:///:memory:")
    service = DisposableMemory(
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        event_store=event_store,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints"),
    )

    async def child_work(_handle):
        return {"private_body": "never inline"}

    try:
        envelope = await service.run(
            intent="qa-judge",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id="01K1DISPOSABLEMEMORY00006",
        )
        events = await event_store.replay("contract", envelope.contract_id)
    finally:
        await event_store.close()

    assert [event.type for event in events] == ["artifact.referenced"]
    assert events[0].data == envelope.model_dump(mode="json")
    assert "private_body" not in json.dumps(events[0].data)
