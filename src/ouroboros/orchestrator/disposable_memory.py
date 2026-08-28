"""Disposable AgentProcess execution backed by explicit artifact references."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import threading
import time
from typing import Any

from ouroboros.core.disposable_memory import DisposableResultEnvelope
from ouroboros.events.artifact import create_artifact_referenced_event
from ouroboros.events.io import new_call_id
from ouroboros.orchestrator.agent_process import AgentProcessHandle, run_with_agent_process
from ouroboros.persistence.artifact_store import (
    ArtifactStore,
    FetchedArtifact,
)
from ouroboros.persistence.checkpoint import CheckpointStore


@dataclass(frozen=True, slots=True)
class DisposableMemory:
    """Run child work without returning its large body to the parent caller."""

    artifact_store: ArtifactStore
    event_store: Any | None = None
    checkpoint_store: CheckpointStore | None = None
    ensure_ready: Callable[[], Awaitable[None]] | None = None

    async def run(
        self,
        *,
        intent: str,
        runtime_id: str,
        work_fn: Callable[[AgentProcessHandle], Awaitable[Any]],
        contract_id: str | None = None,
        events_emitted_count: int = 0,
        timeout: float | None = None,
    ) -> DisposableResultEnvelope:
        """Execute child work and return only a bounded result envelope."""
        if self.ensure_ready is not None:
            await self.ensure_ready()
        resolved_contract_id = contract_id or new_call_id()
        async with asyncio.timeout(timeout):
            return await self._run_within_timeout(
                intent=intent,
                runtime_id=runtime_id,
                work_fn=work_fn,
                contract_id=resolved_contract_id,
                events_emitted_count=events_emitted_count,
            )

    async def _run_within_timeout(
        self,
        *,
        intent: str,
        runtime_id: str,
        work_fn: Callable[[AgentProcessHandle], Awaitable[Any]],
        contract_id: str,
        events_emitted_count: int,
    ) -> DisposableResultEnvelope:
        """Run while the caller-owned timeout covers persistence."""
        # Nothing serializes two callers that hold the same contract id: both run
        # the child, and both try to publish.  That is deliberate.  Publication is
        # an insert, not an update, so the second writer to reach
        # ``put_for_contract`` loses on the contract key and the comparison
        # already living there decides what happens: an identical body collapses
        # into the publication that won, and a differing body is refused as a
        # contract conflict exactly as it is today.  No update can be lost, so a
        # race spends work twice, not correctness.  The only work run under a
        # contract today is a pure synthesis with no awaits, so the duplicate
        # costs microseconds and yields the same bytes.
        #
        # That trade rests on two conventions the store does not enforce: that one
        # contract id means one piece of work, and that the work is deterministic.
        # Work that is expensive, or that has effects beyond the artifact it
        # returns, breaks the trade rather than the store, and owes itself its own
        # idempotency.
        existing = await asyncio.to_thread(
            self.artifact_store.envelope_if_exists,
            contract_id,
        )
        if existing is not None:
            await self._append_reference_event(existing)
            return existing

        started = time.monotonic()
        committed: list[DisposableResultEnvelope] = []
        cancelled = threading.Event()
        commit_started = threading.Event()
        commit_state = threading.Lock()
        publication: asyncio.Task[DisposableResultEnvelope] | None = None

        async def persist_before_completion(
            handle: AgentProcessHandle,
        ) -> DisposableResultEnvelope:
            nonlocal publication
            if handle.should_cancel():
                raise asyncio.CancelledError("disposable work cancelled before execution")
            body = await work_fn(handle)
            if handle.should_cancel():
                raise asyncio.CancelledError("disposable work cancelled before publication")
            duration_ms = max(0, round((time.monotonic() - started) * 1000))

            def ensure_active() -> None:
                if cancelled.is_set() or handle.should_cancel():
                    raise asyncio.CancelledError(
                        "disposable work cancelled while awaiting artifact persistence"
                    )

            def begin_commit() -> None:
                with commit_state:
                    ensure_active()
                    commit_started.set()

            publication = asyncio.create_task(
                asyncio.to_thread(
                    self.artifact_store.put_for_contract,
                    contract_id=contract_id,
                    body=body,
                    runtime_id=runtime_id,
                    duration_ms=duration_ms,
                    events_emitted_count=events_emitted_count,
                    precommit_check=ensure_active,
                    commit_check=begin_commit,
                )
            )
            try:
                envelope = await asyncio.shield(publication)
            except asyncio.CancelledError:
                with commit_state:
                    cancelled.set()
                    must_finish = commit_started.is_set()
                if not must_finish:
                    publication.add_done_callback(_consume_background_result)
                    raise
                envelope = await _settle_committed_publication(publication)
            committed.append(envelope)
            handle.complete_on_return_after_cancel()
            await self._append_reference_event(envelope)
            return envelope

        try:
            return await run_with_agent_process(
                event_store=self.event_store,
                intent=intent,
                work_fn=persist_before_completion,
                timeout=None,
                checkpoint_store=self.checkpoint_store,
                process_id=contract_id,
                cancel_key=contract_id,
            )
        except asyncio.CancelledError:
            if committed:
                return committed[0]
            if commit_started.is_set() and publication is not None:
                envelope = await _settle_committed_publication(publication)
                committed.append(envelope)
                return envelope
            raise

    def fetch(self, contract_id: str) -> FetchedArtifact:
        """Explicitly fetch a disposable body by contract id."""
        return self.artifact_store.fetch(contract_id)

    def fetch_lane(self, contract_id: str, lane_id: str) -> FetchedArtifact:
        """Explicitly fetch one lane's output from a fan-out body."""
        return self.artifact_store.fetch_lane(contract_id, lane_id)

    def replay(self, contract_id: str) -> FetchedArtifact:
        """Read the original body deterministically; never re-execute."""
        return self.artifact_store.replay(contract_id)

    async def force_rerun(
        self,
        original_contract_id: str,
        *,
        intent: str,
        runtime_id: str,
        work_fn: Callable[[AgentProcessHandle], Awaitable[Any]],
        new_contract_id: str | None = None,
        events_emitted_count: int = 0,
        timeout: float | None = None,
    ) -> DisposableResultEnvelope:
        """Explicitly re-execute under a fresh contract identity."""
        replacement = new_contract_id or new_call_id()
        if replacement == original_contract_id:
            raise ValueError("force rerun must allocate a new contract_id")
        return await self.run(
            intent=intent,
            runtime_id=runtime_id,
            work_fn=work_fn,
            contract_id=replacement,
            events_emitted_count=events_emitted_count,
            timeout=timeout,
        )

    async def _append_reference_event(self, envelope: DisposableResultEnvelope) -> None:
        store = self.event_store
        if store is None:
            return
        initialize = getattr(store, "initialize", None)
        if callable(initialize):
            await initialize()
        event = create_artifact_referenced_event(envelope)
        if await _event_already_persisted(store, event):
            return
        try:
            await store.append(event)
        except Exception:
            if await _event_already_persisted(store, event):
                return
            raise


async def _event_already_persisted(store: Any, event: Any) -> bool:
    """Ask whether this exact reference row is already in the ledger."""
    replay = getattr(store, "replay", None)
    if not callable(replay):
        return False
    events = await replay("contract", event.aggregate_id)
    # The id, not the type.  A row written under the older formula names a body
    # in the filesystem store this change deletes, so it is not this
    # publication and must not stand in for it -- the ledger would then hold a
    # reference to a body that is gone while the one that exists went
    # unrecorded.  Two rows for one contract on an upgraded machine is what
    # actually happened: it published twice, into two different stores.
    return any(getattr(candidate, "id", None) == event.id for candidate in events)


def _consume_background_result(task: asyncio.Task[Any]) -> None:
    """Retrieve an abandoned pre-commit worker result to avoid task warnings."""
    if not task.cancelled():
        task.exception()


async def _settle_committed_publication(
    publication: asyncio.Task[DisposableResultEnvelope],
) -> DisposableResultEnvelope:
    """Let an irreversible publication win despite repeated caller cancellation."""
    while not publication.done():
        try:
            await asyncio.shield(publication)
        except asyncio.CancelledError:
            continue
    return publication.result()


__all__ = ["DisposableMemory"]
