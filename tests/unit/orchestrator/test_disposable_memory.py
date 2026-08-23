"""End-to-end bloat guard for disposable AgentProcess results."""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any
from uuid import NAMESPACE_URL, uuid5

import pytest

from ouroboros.core.disposable_memory import (
    MAX_DISPOSABLE_ARTIFACT_BYTES,
    MAX_DISPOSABLE_ENVELOPE_BYTES,
    DisposableResultEnvelope,
)
from ouroboros.events.artifact import create_artifact_referenced_event
from ouroboros.events.base import BaseEvent
import ouroboros.orchestrator.agent_process as agent_process_module
from ouroboros.orchestrator.agent_process import AgentProcessHandle
import ouroboros.orchestrator.disposable_memory as disposable_memory_module
from ouroboros.orchestrator.disposable_memory import DisposableMemory
from ouroboros.persistence.artifact_store import (
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactTombstonedError,
    canonical_artifact_bytes,
)
from ouroboros.persistence.checkpoint import CheckpointStore
from ouroboros.persistence.event_store import EventStore

# Shared by the spawned overlap children and the parent that inspects the store
# afterwards; a spawned process re-imports this module, so a constant travels.
_OVERLAP_CONTRACT_ID = "01K1DISPOSABLEMEMORY00012"


class _EventStore:
    def __init__(self) -> None:
        self.appended: list[BaseEvent] = []

    async def append(self, event: BaseEvent) -> None:
        self.appended.append(event)

    async def append_durable(self, event: BaseEvent, *, timeout: float) -> None:
        async with asyncio.timeout(timeout):
            await self.append(event)

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
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
        event_store=event_store,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints"),
    )
    return service, event_store


class _ControlledDeadline:
    def __init__(self) -> None:
        self._task: asyncio.Task[Any] | None = None
        self._expired = False

    async def __aenter__(self) -> None:
        self._task = asyncio.current_task()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: Any,
    ) -> None:
        if self._expired and exc_type is asyncio.CancelledError:
            raise TimeoutError from None

    def expire(self) -> None:
        assert self._task is not None
        self._expired = True
        self._task.cancel()


def _install_controlled_deadline(
    monkeypatch: pytest.MonkeyPatch,
    delay: float,
) -> _ControlledDeadline:
    deadline = _ControlledDeadline()
    original_timeout = asyncio.timeout

    def controlled_timeout(requested_delay: float | None) -> Any:
        if requested_delay == delay:
            return deadline
        return original_timeout(requested_delay)

    monkeypatch.setattr(disposable_memory_module.asyncio, "timeout", controlled_timeout)
    return deadline


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
            artifact_store=ArtifactStore(Path(artifact_root)),
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
            contract_id=_OVERLAP_CONTRACT_ID,
        )
        # The whole envelope crosses back, not just its ref: convergence means
        # both processes were handed the same published identity, not merely the
        # same content address.
        return json.dumps(envelope.model_dump(mode="json"), sort_keys=True)

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

    assert first == second
    references = [event for event in event_store.appended if event.type == "artifact.referenced"]
    assert len(references) == 1


def test_the_envelope_that_dropped_a_required_field_says_so_in_its_version(
    tmp_path: Path,
) -> None:
    """Dropping `artifact_ref` is not additive, so it cannot stay version 1.

    This envelope is the payload of `artifact.referenced`, and that store is
    append-only: rows written before this change carry `artifact_ref` and say
    version 1. If the new shape also said 1, the store would hold two shapes
    under one number and nothing could tell which it was reading — which is the
    single thing the number is for.
    """
    service, _ = _service(tmp_path)

    envelope = service.artifact_store.put_for_contract(
        contract_id="01K1DISPOSABLEMEMORY00016",
        body={"stable": True},
        runtime_id="fixture-runtime",
        duration_ms=1,
        events_emitted_count=0,
    )
    payload = create_artifact_referenced_event(envelope).data

    assert envelope.schema_version == 2
    assert payload["schema_version"] == 2
    assert "artifact_ref" not in payload


@pytest.mark.asyncio
async def test_a_row_from_the_old_store_does_not_stand_in_for_a_new_publication(
    tmp_path: Path,
) -> None:
    """The ledger records the publication that exists, not the one that is gone.

    A machine upgraded into this change keeps its EventStore and loses its
    filesystem artifacts, so a contract re-run afterwards publishes a new body
    while an old row still names the deleted one. Letting the old row count as
    this publication would leave the ledger pointing at a body nobody can fetch
    and no record of the body that exists. Two rows is what happened: the
    contract published twice, into two different stores.
    """
    service, event_store = _service(tmp_path)
    contract_id = "01K1DISPOSABLEMEMORY00015"

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        return {"stable": True}

    # The row a pre-cutover ledger holds, under the formula that folded in the
    # content address the filesystem store gave it.
    legacy_artifact_ref = (
        "sha256:" + hashlib.sha256(canonical_artifact_bytes({"stable": True})).hexdigest()
    )
    legacy_id = str(
        uuid5(
            NAMESPACE_URL,
            f"ouroboros:artifact:{contract_id}:{legacy_artifact_ref}:referenced",
        )
    )
    event_store.appended.append(
        BaseEvent(
            id=legacy_id,
            type="artifact.referenced",
            aggregate_type="contract",
            aggregate_id=contract_id,
            data={
                "schema_version": 1,
                "contract_id": contract_id,
                "artifact_ref": legacy_artifact_ref,
                "result": {"status": "completed"},
                "runtime_id": "runtime-before-the-cutover",
                "duration_ms": 1,
                "events_emitted_count": 0,
            },
        )
    )

    envelope = await service.run(
        intent="rerun after the cutover",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )

    assert service.artifact_store.fetch(contract_id).body == {"stable": True}
    references = [event for event in event_store.appended if event.type == "artifact.referenced"]
    assert [event.id for event in references] == [
        legacy_id,
        create_artifact_referenced_event(envelope).id,
    ]
    assert references[-1].data["runtime_id"] == "fixture-runtime"


@pytest.mark.asyncio
async def test_a_recovered_contract_appends_its_reference_only_once(tmp_path: Path) -> None:
    """Exactly-once still holds for the publication this version writes."""
    service, event_store = _service(tmp_path)
    contract_id = "01K1DISPOSABLEMEMORY00017"

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        return {"stable": True}

    first = await service.run(
        intent="publish",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )
    again = await service.run(
        intent="recover",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )

    assert again == first
    references = [event for event in event_store.appended if event.type == "artifact.referenced"]
    assert [event.id for event in references] == [create_artifact_referenced_event(first).id]


@pytest.mark.asyncio
async def test_overlapping_tasks_converge_on_one_published_contract(tmp_path: Path) -> None:
    service, event_store = _service(tmp_path)
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    contract_id = "01K1DISPOSABLEMEMORY00010"

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
            contract_id=contract_id,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    second_task = asyncio.create_task(
        service.run(
            intent="task-overlap",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )
    )
    try:
        await asyncio.sleep(0.1)
        # Both callers are inside the child at once: nothing holds the second
        # one back, and that is the permitted outcome, not the failure.
        assert calls == 2
    finally:
        release.set()

    first, second = await asyncio.gather(first_task, second_task)

    # The duplicated work is spent; the published result is still single.
    assert first == second
    assert service.fetch(contract_id).body == {"stable": True}
    references = [event for event in event_store.appended if event.type == "artifact.referenced"]
    assert len(references) == 1


def test_overlapping_processes_converge_on_one_published_contract(tmp_path: Path) -> None:
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
    # Nothing stops both processes from executing the child, and duplicate
    # execution is allowed outright.  What must hold is that they converge: one
    # published body, and the same envelope handed to both callers.
    assert records[0][1] == records[1][1]
    executions = len(counter_path.read_text(encoding="utf-8").splitlines())
    assert 1 <= executions <= 2

    # Convergence at the store: whichever process won the contract key, one
    # row holds the one body, and a fresh reader is handed exactly it.
    store = ArtifactStore(artifact_root)
    assert store.fetch(_OVERLAP_CONTRACT_ID).body == {"stable": True}


@pytest.mark.asyncio
async def test_store_contention_preserves_event_loop_timeout_and_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _ = _service(tmp_path)
    release_persistence = threading.Event()
    persistence_entered = threading.Event()
    persistence_settled = threading.Event()
    original_put = service.artifact_store.put_for_contract
    child_calls = 0
    deadline = _install_controlled_deadline(monkeypatch, 0.1)

    def observe_persistence(**kwargs: Any) -> DisposableResultEnvelope:
        persistence_entered.set()
        try:
            release_persistence.wait()
            return original_put(**kwargs)
        finally:
            persistence_settled.set()

    monkeypatch.setattr(service.artifact_store, "put_for_contract", observe_persistence)

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal child_calls
        child_calls += 1
        return {"must_not_publish_after_timeout": True}

    run_task = asyncio.create_task(
        service.run(
            intent="contended-persistence",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id="01K1DISPOSABLEMEMORY00012",
            timeout=0.1,
        )
    )
    try:
        assert await asyncio.to_thread(persistence_entered.wait, 2.0)
        deadline.expire()
        with pytest.raises(TimeoutError):
            await run_task
    finally:
        release_persistence.set()
        if not run_task.done():
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task

    assert await asyncio.to_thread(persistence_settled.wait, 2.0)

    # Persistence can only be released by the finally block after ``run`` returns.
    # Reaching the TimeoutError while the worker thread is still contended is a
    # synchronization proof that persistence did not block the event loop. A
    # heartbeat-gap wall-clock budget measures xdist scheduling instead.
    assert persistence_entered.is_set()
    assert child_calls == 1
    with pytest.raises(ArtifactNotFoundError):
        service.fetch("01K1DISPOSABLEMEMORY00012")


@pytest.mark.asyncio
async def test_timeout_after_commit_gate_waits_for_durable_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_process_module, "_CANCELLED_WORK_DRAIN_GRACE_SECONDS", 0.05)
    deadline = _install_controlled_deadline(monkeypatch, 0.1)
    service, _ = _service(tmp_path)
    commit_entered = threading.Event()
    release_commit = threading.Event()
    original_put = service.artifact_store.put_for_contract
    original_settle = disposable_memory_module._settle_committed_publication
    timeout_settlement_entered = asyncio.Event()
    child_calls = 0

    async def observe_timeout_settlement(
        publication: asyncio.Task[DisposableResultEnvelope],
    ) -> DisposableResultEnvelope:
        timeout_settlement_entered.set()
        return await original_settle(publication)

    monkeypatch.setattr(
        disposable_memory_module,
        "_settle_committed_publication",
        observe_timeout_settlement,
    )

    def pause_after_commit_gate(**kwargs: Any) -> DisposableResultEnvelope:
        # The caller's commit gate is wrapped rather than replaced, so the
        # pause begins only after that gate has irrevocably opened.  That is
        # the window this test is about: commitment declared, durability not
        # yet reached, and the caller's timeout landing in between.
        inner_commit_check = kwargs.get("commit_check")

        def gated_commit_check() -> None:
            if inner_commit_check is not None:
                inner_commit_check()
            commit_entered.set()
            release_commit.wait()

        return original_put(**{**kwargs, "commit_check": gated_commit_check})

    monkeypatch.setattr(service.artifact_store, "put_for_contract", pause_after_commit_gate)

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal child_calls
        child_calls += 1
        return {"race": True}

    # The concurrent retry gets its own runner checkpoint store while sharing the
    # artifact store, which is how a real retry arrives: from another runner.
    # A cancel signal is persisted under the contract id, so a retry sharing this
    # runner's checkpoint store would inherit the first run's timeout cancellation
    # and never reach the publication race this test is about.
    retry_service = DisposableMemory(
        artifact_store=service.artifact_store,
        event_store=service.event_store,
        checkpoint_store=CheckpointStore(tmp_path / "checkpoints-retry"),
    )

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
            retry_service.run(
                intent="commit-wins-timeout",
                runtime_id="fixture-runtime",
                work_fn=child_work,
                contract_id=contract_id,
            )
        )
        deadline.expire()
        # Observe the product timeout entering the irreversible-publication
        # settlement path. This replaces a sleep race: the assertion is about
        # state ordering, not how quickly a loaded xdist worker is scheduled.
        await asyncio.wait_for(timeout_settlement_entered.wait(), timeout=10.0)
        first_was_pending = not first_task.done()
        retry_was_pending = not retry_task.done()
    finally:
        release_commit.set()

    assert retry_task is not None
    # The release above is the synchronization boundary.  This timeout is only
    # a deadlock guard: completion still depends on the persistence worker being
    # scheduled, which can legitimately take more than two seconds under xdist.
    first, retry = await asyncio.wait_for(
        asyncio.gather(first_task, retry_task),
        timeout=30.0,
    )

    assert first_was_pending
    assert retry_was_pending
    # The retry is free to run the child again; what it may not do is publish a
    # second time or return anything other than the envelope that committed.
    assert retry == first
    assert service.fetch(contract_id).body == {"race": True}


@pytest.mark.asyncio
async def test_retry_repairs_reference_without_reexecuting_durable_contract(
    tmp_path: Path,
) -> None:
    event_store = _FailReferenceOnceEventStore()
    service = DisposableMemory(
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
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
async def test_external_body_corruption_is_refused_before_any_reader_meets_it(
    tmp_path: Path,
) -> None:
    """Tampered storage must not become silent re-execution or a served body.

    The filesystem store held a manifest that could be pointed at another
    contract's blob, and refused the substituted binding on retry.  The SQLite
    row has no binding left to redirect -- the contract id is the body's only
    address -- and a rewrite of the stored bytes into non-JSON is refused by
    the database itself, because the generated ``kind`` column cannot be
    derived from a body that is not JSON.  The corrupt state the old test
    planted and detected is now unrepresentable, so the retry and fetch that
    follow still see only the original publication.
    """
    service, _ = _service(tmp_path)
    calls = 0

    async def child_work(_handle: AgentProcessHandle) -> dict[str, str]:
        nonlocal calls
        calls += 1
        return {"owner": "a"}

    contract_id = "01K1DISPOSABLEMEMORY00021"
    first = await service.run(
        intent="victim",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )
    connection = sqlite3.connect(tmp_path / "artifacts" / "artifacts.db")
    with closing(connection), connection:
        with pytest.raises(sqlite3.OperationalError, match="malformed JSON"):
            connection.execute(
                "UPDATE artifacts SET body = ? WHERE contract_id = ?",
                ("{corrupted, not json", contract_id),
            )

    recovered = await service.run(
        intent="victim-retry",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )

    assert recovered == first
    assert calls == 1
    assert service.fetch(contract_id).body == {"owner": "a"}


@pytest.mark.asyncio
async def test_run_refuses_tombstoned_contract_without_reexecuting(tmp_path: Path) -> None:
    """An applied prune is terminal: rerunning the contract stops at the stone.

    Half-destroyed store state stopped being representable with the manifest
    that used to dangle; the one deliberate way a contract loses its body now
    is pruning, and that must keep refusing silent re-execution the way a
    missing manifest used to.
    """
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
    report = service.artifact_store.prune(
        ttl=timedelta(0),
        apply=True,
        allow_replay_tombstone=True,
    )
    assert report.removed_contract_ids == (contract_id,)

    with pytest.raises(ArtifactTombstonedError, match="force-rerun"):
        await service.run(
            intent="crash-window-retry",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )
    assert calls == 1
    with pytest.raises(ArtifactTombstonedError):
        service.replay(contract_id)


@pytest.mark.asyncio
async def test_retry_publishes_after_failed_publication_leaves_nothing_behind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publication failure surfaces, strands nothing, and the retry succeeds.

    The filesystem store could fail between its blob and its manifest, leaving
    a partial publication for the retry to recover without re-executing.  A
    publication is one row insert now, so a failure leaves no partial state at
    all -- and the retry therefore legitimately runs the child again before
    publishing, instead of finding half a publication to finish.
    """
    service, _ = _service(tmp_path)
    calls = 0
    attempts = 0
    original_put = service.artifact_store.put_for_contract

    async def child_work(_handle: AgentProcessHandle) -> dict[str, bool]:
        nonlocal calls
        calls += 1
        return {"recoverable": True}

    def fail_first_publication(**kwargs: Any) -> DisposableResultEnvelope:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ArtifactStoreError(
                "simulated artifact publication failure",
                operation="write",
            )
        return original_put(**kwargs)

    monkeypatch.setattr(service.artifact_store, "put_for_contract", fail_first_publication)
    contract_id = "01K1DISPOSABLEMEMORY00024"
    with pytest.raises(ArtifactStoreError, match="publication failure"):
        await service.run(
            intent="failed-publication",
            runtime_id="fixture-runtime",
            work_fn=child_work,
            contract_id=contract_id,
        )

    with pytest.raises(ArtifactNotFoundError):
        service.fetch(contract_id)

    recovered = await service.run(
        intent="failed-publication-retry",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )
    assert recovered.contract_id == contract_id
    assert calls == 2
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

    # The observation seam is SQLite's own authorizer rather than a store
    # private: every column the retry reads off the artifacts table is
    # recorded, and the megabyte body must never be among them.  A metadata
    # read that stays metadata-only is the retry's whole cost model.
    columns_read: set[str] = set()
    original_connect = sqlite3.connect

    def observing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = original_connect(*args, **kwargs)

        def record_artifact_reads(
            action: int,
            table: str | None,
            column: str | None,
            _database: str | None,
            _trigger: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_READ and table == "artifacts" and column:
                columns_read.add(column)
            return sqlite3.SQLITE_OK

        connection.set_authorizer(record_artifact_reads)
        return connection

    monkeypatch.setattr(sqlite3, "connect", observing_connect)
    recovered = await service.run(
        intent="large-retry",
        runtime_id="fixture-runtime",
        work_fn=child_work,
        contract_id=contract_id,
    )

    assert recovered == first
    assert calls == 1
    assert columns_read, "the retry never consulted the contract row"
    assert "body" not in columns_read


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
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
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
    await event_store.initialize()
    service = DisposableMemory(
        artifact_store=ArtifactStore(tmp_path / "artifacts"),
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
