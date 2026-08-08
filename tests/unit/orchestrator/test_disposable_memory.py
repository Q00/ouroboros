"""End-to-end bloat guard for disposable AgentProcess results."""

from __future__ import annotations

import asyncio
from itertools import pairwise
import json
import multiprocessing
import os
from pathlib import Path
import threading
from typing import Any

import pytest

from ouroboros.core.disposable_memory import (
    MAX_DISPOSABLE_ARTIFACT_BYTES,
    MAX_DISPOSABLE_ENVELOPE_BYTES,
    DisposableResultEnvelope,
)
from ouroboros.events.base import BaseEvent
import ouroboros.orchestrator.agent_process as agent_process_module
from ouroboros.orchestrator.agent_process import AgentProcessHandle
from ouroboros.orchestrator.disposable_memory import DisposableMemory
from ouroboros.persistence.artifact_store import (
    ArtifactManifestError,
    ArtifactNotFoundError,
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


class _FailReferenceOnceEventStore(_EventStore):
    def __init__(self) -> None:
        super().__init__()
        self.reference_attempts = 0

    async def append(self, event: BaseEvent) -> None:
        if event.type == "artifact.referenced":
            self.reference_attempts += 1
            if self.reference_attempts == 1:
                raise RuntimeError("simulated reference append failure")
        await super().append(event)


def _service(tmp_path: Path) -> tuple[DisposableMemory, _EventStore]:
    event_store = _EventStore()
    service = DisposableMemory(
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        event_store=event_store,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints"),
    )
    return service, event_store


def _run_disposable_process(
    artifact_root: str,
    counter_path: str,
    ready: Any,
    start: Any,
    results: Any,
) -> None:
    ready.put(os.getpid())
    if not start.wait(20):
        results.put(("error", "start timeout"))
        return

    async def invoke() -> str:
        service = DisposableMemory(
            artifact_store=ContentAddressedArtifactStore(Path(artifact_root)),
            checkpoint_store=CheckpointStore(
                Path(artifact_root).parent / f"checkpoints-{os.getpid()}"
            ),
        )

        async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
            with Path(counter_path).open("a", encoding="utf-8") as handle:
                handle.write(f"{os.getpid()}\n")
            await asyncio.sleep(0.25)
            return {"stable": True}

        envelope = await service.run(
            intent="process-overlap",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id="01K1DISPOSABLEMEMORY00012",
        )
        return envelope.artifact_ref

    try:
        results.put(("ok", asyncio.run(invoke())))
    except BaseException as exc:  # noqa: BLE001 - report child-process failures to pytest
        results.put(("error", f"{type(exc).__name__}: {exc}"))


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
async def test_overlapping_tasks_execute_same_contract_child_once(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"stable": True}

    first_task = asyncio.create_task(
        service.run(
            intent="task-overlap",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id="01K1DISPOSABLEMEMORY00010",
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    second_task = asyncio.create_task(
        service.run(
            intent="task-overlap",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id="01K1DISPOSABLEMEMORY00010",
        )
    )
    try:
        await asyncio.sleep(0.1)
        assert calls == 1
    finally:
        release.set()

    first, second = await asyncio.gather(first_task, second_task)
    assert calls == 1
    assert first == second


@pytest.mark.asyncio
async def test_cancelled_lock_waiter_leaves_no_claim_or_child_effect(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return {"stable": True}

    contract_id = "01K1DISPOSABLEMEMORY00013"
    first_task = asyncio.create_task(
        service.run(
            intent="cancelled-waiter",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    waiting_task = asyncio.create_task(
        service.run(
            intent="cancelled-waiter",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )
    )
    await asyncio.sleep(0.1)
    waiting_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting_task

    release.set()
    first = await first_task
    recovered = await asyncio.wait_for(
        service.run(
            intent="cancelled-waiter",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        ),
        2,
    )

    assert calls == 1
    assert recovered == first


def test_overlapping_processes_execute_same_contract_child_once(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    results = context.Queue()
    artifact_root = tmp_path / "artifacts"
    counter_path = tmp_path / "child-calls.txt"
    processes = [
        context.Process(
            target=_run_disposable_process,
            args=(str(artifact_root), str(counter_path), ready, start, results),
        )
        for _ in range(2)
    ]

    try:
        for process in processes:
            process.start()
        assert len({ready.get(timeout=20) for _ in processes}) == 2
        start.set()
        for process in processes:
            process.join(30)
        records = [results.get(timeout=5) for _ in processes]
    finally:
        start.set()
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(5)

    assert [process.exitcode for process in processes] == [0, 0]
    errors = [record[1] for record in records if record[0] == "error"]
    assert not errors, "\n".join(errors)
    assert records[0][1] == records[1][1]
    assert len(counter_path.read_text(encoding="utf-8").splitlines()) == 1


@pytest.mark.asyncio
async def test_store_contention_preserves_event_loop_timeout_and_cancellation(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    lock_entered = threading.Event()
    release_lock = threading.Event()
    holder: threading.Thread | None = None
    heartbeat: list[float] = []
    stop_heartbeat = asyncio.Event()
    child_calls = 0

    async def pulse() -> None:
        while not stop_heartbeat.is_set():
            heartbeat.append(asyncio.get_running_loop().time())
            await asyncio.sleep(0.01)

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal child_calls, holder
        child_calls += 1

        def hold_global_store_lock() -> None:
            with service.artifact_store._store_lock(exclusive=True):
                lock_entered.set()
                release_lock.wait(1.0)

        holder = threading.Thread(target=hold_global_store_lock, daemon=True)
        holder.start()
        assert await asyncio.to_thread(lock_entered.wait, 1.0)
        return {"must_not_publish_after_timeout": True}

    pulse_task = asyncio.create_task(pulse())
    started = asyncio.get_running_loop().time()
    try:
        with pytest.raises(TimeoutError):
            await service.run(
                intent="contended-persistence",
                runtime_id="fixture-runtime",
                work_fn=child_work,
                contract_id="01K1DISPOSABLEMEMORY00012",
                timeout=0.1,
            )
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        release_lock.set()
        if holder is not None:
            await asyncio.to_thread(holder.join, 2.0)
        await asyncio.sleep(0.1)
        stop_heartbeat.set()
        await pulse_task

    assert elapsed < 0.5
    assert child_calls == 1
    assert len(heartbeat) >= 5
    assert max(b - a for a, b in pairwise(heartbeat)) < 0.2
    with pytest.raises(ArtifactNotFoundError):
        service.fetch("01K1DISPOSABLEMEMORY00012")


@pytest.mark.asyncio
async def test_timeout_after_commit_gate_waits_for_durable_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_process_module, "_CANCELLED_WORK_DRAIN_GRACE_SECONDS", 0.05)
    service, _ = _service(tmp_path)
    commit_entered = threading.Event()
    release_commit = threading.Event()
    original_write = service.artifact_store._write_blob_locked
    child_calls = 0

    def pause_after_commit_gate(
        digest: str,
        payload: bytes,
        *,
        authority_check: Any = None,
    ) -> None:
        commit_entered.set()
        if not release_commit.wait(2.0):
            raise AssertionError("test did not release the durable commit")
        original_write(digest, payload, authority_check=authority_check)

    monkeypatch.setattr(service.artifact_store, "_write_blob_locked", pause_after_commit_gate)

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal child_calls
        child_calls += 1
        return {"race": True}

    contract_id = "01K1DISPOSABLEMEMORY00014"
    first_task = asyncio.create_task(
        service.run(
            intent="commit-wins-timeout",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
            timeout=0.1,
        )
    )
    retry_task: asyncio.Task[DisposableResultEnvelope] | None = None
    try:
        assert await asyncio.to_thread(commit_entered.wait, 1.0)
        retry_task = asyncio.create_task(
            service.run(
                intent="commit-wins-timeout",
                runtime_id="fixture-runtime",
                work_fn=child_work,
                contract_id=contract_id,
            )
        )
        await asyncio.sleep(0.15)
        first_was_pending = not first_task.done()
        retry_was_pending = not retry_task.done()
        calls_before_release = child_calls
    finally:
        release_commit.set()

    assert retry_task is not None
    first, retry = await asyncio.wait_for(
        asyncio.gather(first_task, retry_task),
        timeout=2.0,
    )

    assert first_was_pending
    assert retry_was_pending
    assert calls_before_release == 1
    assert child_calls == 1
    assert retry == first
    assert service.fetch(contract_id).body == {"race": True}


@pytest.mark.asyncio
async def test_retry_repairs_reference_without_reexecuting_durable_contract(
    tmp_path: Path,
) -> None:
    event_store = _FailReferenceOnceEventStore()
    service = DisposableMemory(
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        event_store=event_store,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints"),
    )
    calls = 0

    async def child_work(_handle):
        nonlocal calls
        calls += 1
        return {"stable": True}

    contract_id = "01K1DISPOSABLEMEMORY00007"
    with pytest.raises(RuntimeError, match="reference append failure"):
        await service.run(
            intent="researcher",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )

    assert calls == 1
    assert service.fetch(contract_id).body == {"stable": True}

    recovered = await service.run(
        intent="researcher",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )

    assert recovered.contract_id == contract_id
    assert calls == 1
    references = [event for event in event_store.appended if event.type == "artifact.referenced"]
    assert len(references) == 1


@pytest.mark.asyncio
async def test_retry_rejects_cross_contract_manifest_substitution_without_execution(
    tmp_path: Path,
) -> None:
    service, _ = _service(tmp_path)
    calls = 0

    async def child_work(_handle: AgentProcessHandle) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"owner": "a"}

    victim = "01K1DISPOSABLEMEMORY00021"
    source = "01K1DISPOSABLEMEMORY00022"
    await service.run(
        intent="victim",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=victim,
    )
    source_envelope = service.artifact_store.put_for_contract(
        contract_id=source,
        body={"owner": "b", "different": "payload"},
        runtime_id="fixture-runtime",
        duration_ms=1,
        events_emitted_count=0,
    )
    manifest_path = service.artifact_store._manifest_path(victim)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    source_path = service.artifact_store._manifest_path(source)
    source_event = json.loads(source_path.read_text(encoding="utf-8"))["events"][0]
    manifest["events"][0]["artifact_ref"] = source_envelope.artifact_ref
    manifest["events"][0]["size_bytes"] = source_event["size_bytes"]
    manifest["events"][0]["envelope"]["artifact_ref"] = source_envelope.artifact_ref
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ArtifactManifestError, match="binding"):
        await service.run(
            intent="victim-retry",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=victim,
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_rejects_binding_without_manifest_without_execution(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    calls = 0

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"durable": True}

    contract_id = "01K1DISPOSABLEMEMORY00023"
    await service.run(
        intent="crash-window",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )
    manifest_path = service.artifact_store._manifest_path(contract_id)
    manifest_path.unlink()

    with pytest.raises(ArtifactManifestError, match="binding"):
        await service.run(
            intent="crash-window-retry",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )
    assert calls == 1


@pytest.mark.asyncio
async def test_retry_recovers_binding_first_manifest_failure_without_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _service(tmp_path)
    calls = 0
    writes = 0
    original_write = service.artifact_store._write_manifest_locked

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"recoverable": True}

    def fail_first_manifest(*args: Any, **kwargs: Any) -> None:
        nonlocal writes
        writes += 1
        if writes == 1:
            raise OSError("simulated manifest publication failure")
        original_write(*args, **kwargs)

    monkeypatch.setattr(service.artifact_store, "_write_manifest_locked", fail_first_manifest)
    contract_id = "01K1DISPOSABLEMEMORY00024"
    with pytest.raises(OSError, match="manifest publication failure"):
        await service.run(
            intent="partial-publication",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )

    recovered = await service.run(
        intent="partial-publication-retry",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )
    assert recovered.contract_id == contract_id
    assert calls == 1
    assert service.fetch(contract_id).body == {"recoverable": True}


@pytest.mark.asyncio
async def test_large_retry_recovers_envelope_without_reading_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _service(tmp_path)
    empty_size = len(canonical_artifact_bytes({"output": ""}))
    body = {"output": "x" * (MAX_DISPOSABLE_ARTIFACT_BYTES - empty_size)}
    calls = 0

    async def child_work(_handle: AgentProcessHandle) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return body

    contract_id = "01K1DISPOSABLEMEMORY00011"
    first = await service.run(
        intent="large-retry",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )

    def fail_body_read(_artifact_ref: str) -> bytes:
        raise AssertionError("ordinary retry must not materialize the artifact body")

    monkeypatch.setattr(service.artifact_store, "_read_blob_locked", fail_body_read)
    recovered = await service.run(
        intent="large-retry",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )

    assert recovered == first
    assert calls == 1


@pytest.mark.asyncio
async def test_persisted_cancel_prevents_child_execution_and_publication(tmp_path: Path) -> None:
    contract_id = "01K1DISPOSABLEMEMORY00008"
    checkpoint_store = CheckpointStore(tmp_path / "checkpoints")
    checkpoint_store.initialize()
    AgentProcessHandle.persist_cancel_signal(
        contract_id,
        store=checkpoint_store,
        reason="cancelled before restart",
    )
    service = DisposableMemory(
        artifact_store=ContentAddressedArtifactStore(tmp_path / "artifacts"),
        event_store=_EventStore(),
        checkpoint_store=checkpoint_store,
    )
    calls = 0

    async def child_work(_handle):
        nonlocal calls
        calls += 1
        return {"must_not_publish": True}

    with pytest.raises(asyncio.CancelledError):
        await service.run(
            intent="qa-judge",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )

    assert calls == 0
    with pytest.raises(ArtifactNotFoundError):
        service.fetch(contract_id)


@pytest.mark.asyncio
async def test_cancel_requested_by_child_prevents_completed_publication(tmp_path: Path) -> None:
    contract_id = "01K1DISPOSABLEMEMORY00009"
    service, event_store = _service(tmp_path)

    async def child_work(handle: AgentProcessHandle):
        await handle.cancel("cancel after child effect")
        return {"must_not_publish": True}

    with pytest.raises(asyncio.CancelledError):
        await service.run(
            intent="qa-judge",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )

    with pytest.raises(ArtifactNotFoundError):
        service.fetch(contract_id)
    assert not [event for event in event_store.appended if event.type == "artifact.referenced"]


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
