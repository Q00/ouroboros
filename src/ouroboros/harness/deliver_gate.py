"""Read-only EventStore input loader for the #978 evidence deliver gate.

This module is the first P2-safe bridge between the journal normalizer and the
future TraceGuard verdict call. It deliberately does **not** change AC success
semantics: callers receive an :class:`EvidenceManifest` they can pass to an
observe-only or A/B verifier, while legacy completion remains untouched until a
later gate PR explicitly owns behavior changes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ouroboros.events.base import BaseEvent
from ouroboros.harness.journal import EvidenceManifest, normalize_events


class EventStoreEvidenceReader(Protocol):
    """EventStore read subset required by the deliver-gate manifest loader."""

    async def query_execution_related_events(
        self,
        execution_id: str,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        raise NotImplementedError

    async def query_session_related_events(
        self,
        session_id: str,
        execution_id: str | None = None,
        event_type: str | None = None,
        limit: int | None = 50,
        offset: int = 0,
    ) -> list[BaseEvent]:
        raise NotImplementedError


async def load_ac_evidence_manifest(
    event_store: EventStoreEvidenceReader,
    *,
    ac_id: str,
    execution_id: str | None = None,
    session_id: str | None = None,
    limit: int | None = None,
) -> EvidenceManifest:
    """Load and normalize EventStore evidence for one AC deliver-gate check.

    ``execution_id`` is preferred because it avoids accidentally splicing a
    reused session's unrelated runs. ``session_id`` is retained for consumers
    that only know the top-level session while the deliver gate is still in
    observe-only/A-B wiring.

    Args:
        event_store: Read-capable EventStore or test double.
        ac_id: Acceptance-criterion identifier to normalize.
        execution_id: Optional execution aggregate anchor.
        session_id: Optional session aggregate anchor.
        limit: Optional EventStore query cap. The default ``None`` reads the
            full related event set so the manifest is not silently truncated
            before TraceGuard sees it.

    Raises:
        ValueError: If ``ac_id`` is blank or neither execution nor session
            anchor is provided.

    Returns:
        A per-AC :class:`EvidenceManifest` in chronological event order.
    """
    normalized_ac_id = ac_id.strip()
    if not normalized_ac_id:
        msg = "load_ac_evidence_manifest requires a non-blank ac_id"
        raise ValueError(msg)
    if execution_id is None and session_id is None:
        msg = "load_ac_evidence_manifest requires execution_id or session_id"
        raise ValueError(msg)

    if execution_id is not None:
        events = await event_store.query_execution_related_events(
            execution_id,
            limit=limit,
        )
    else:
        assert session_id is not None
        events = await event_store.query_session_related_events(
            session_id,
            execution_id=execution_id,
            limit=limit,
        )

    return normalize_events(_chronological_events(events), ac_id=normalized_ac_id)


def _chronological_events(events: Iterable[BaseEvent]) -> tuple[BaseEvent, ...]:
    """Return events oldest-first regardless of EventStore query ordering."""
    return tuple(sorted(events, key=lambda event: (event.timestamp, event.id)))


__all__ = [
    "EventStoreEvidenceReader",
    "load_ac_evidence_manifest",
]
