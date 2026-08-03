"""Disposable AgentProcess execution backed by explicit artifact references."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import time
from typing import Any

from ouroboros.core.disposable_memory import DisposableResultEnvelope
from ouroboros.events.artifact import create_artifact_referenced_event
from ouroboros.events.io import new_call_id
from ouroboros.orchestrator.agent_process import AgentProcessHandle, run_with_agent_process
from ouroboros.persistence.artifact_store import (
    ContentAddressedArtifactStore,
    FetchedArtifact,
)
from ouroboros.persistence.checkpoint import CheckpointStore


@dataclass(frozen=True, slots=True)
class DisposableMemory:
    """Run child work without returning its large body to the parent caller."""

    artifact_store: ContentAddressedArtifactStore
    event_store: Any | None = None
    checkpoint_store: CheckpointStore | None = None

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
        resolved_contract_id = contract_id or new_call_id()
        existing = self.artifact_store.fetch_if_exists(resolved_contract_id)
        if existing is not None:
            await self._append_reference_event(existing.envelope)
            return existing.envelope

        started = time.monotonic()

        async def persist_before_completion(handle: AgentProcessHandle) -> DisposableResultEnvelope:
            if handle.should_cancel():
                raise asyncio.CancelledError("disposable work cancelled before execution")
            body = await work_fn(handle)
            if handle.should_cancel():
                raise asyncio.CancelledError("disposable work cancelled before publication")
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            envelope = self.artifact_store.put_for_contract(
                contract_id=resolved_contract_id,
                body=body,
                runtime_id=runtime_id,
                duration_ms=duration_ms,
                events_emitted_count=events_emitted_count,
            )
            handle.complete_on_return_after_cancel()
            await self._append_reference_event(envelope)
            return envelope

        return await run_with_agent_process(
            event_store=self.event_store,
            intent=intent,
            work_fn=persist_before_completion,
            timeout=timeout,
            checkpoint_store=self.checkpoint_store,
            process_id=resolved_contract_id,
            cancel_key=resolved_contract_id,
        )

    def fetch(self, contract_id: str) -> FetchedArtifact:
        """Explicitly fetch a disposable body by contract id."""
        return self.artifact_store.fetch(contract_id)

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
        if await _event_already_persisted(store, event.id, envelope.contract_id):
            return
        try:
            await store.append(event)
        except Exception:
            if await _event_already_persisted(store, event.id, envelope.contract_id):
                return
            raise


async def _event_already_persisted(store: Any, event_id: str, contract_id: str) -> bool:
    replay = getattr(store, "replay", None)
    if not callable(replay):
        return False
    events = await replay("contract", contract_id)
    return any(getattr(event, "id", None) == event_id for event in events)


__all__ = ["DisposableMemory"]
