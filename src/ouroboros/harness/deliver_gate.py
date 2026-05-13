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
    scope_id: str | None = None,
    limit: int | None = None,
) -> EvidenceManifest:
    """Load and normalize EventStore evidence for one AC deliver-gate check.

    When both ``session_id`` and ``execution_id`` are available the loader
    uses the session-related query with the execution correlation filter. That
    includes session-aggregate I/O events as well as execution/runtime-scope
    rows. ``execution_id``-only reads remain supported for callers that do not
    know the session anchor.

    Args:
        event_store: Read-capable EventStore or test double.
        ac_id: Acceptance-criterion identifier to normalize.
        execution_id: Optional execution aggregate anchor.
        session_id: Optional session aggregate anchor.
        scope_id: Optional event-scope token to filter by when the public AC
            id differs from the runtime aggregate/phase token used by the
            recorder. Defaults to ``ac_id``.
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
    normalized_execution_id = _normalize_optional_anchor("execution_id", execution_id)
    normalized_session_id = _normalize_optional_anchor("session_id", session_id)
    normalized_scope_id = _normalize_optional_anchor("scope_id", scope_id) or normalized_ac_id
    if normalized_execution_id is None and normalized_session_id is None:
        msg = "load_ac_evidence_manifest requires execution_id or session_id"
        raise ValueError(msg)

    if normalized_session_id is not None:
        events = await event_store.query_session_related_events(
            normalized_session_id,
            execution_id=normalized_execution_id,
            limit=limit,
        )
    else:
        assert normalized_execution_id is not None
        events = await event_store.query_execution_related_events(
            normalized_execution_id,
            limit=limit,
        )

    manifest = normalize_events(_chronological_events(events), ac_id=normalized_scope_id)
    if normalized_scope_id == normalized_ac_id:
        return manifest
    return manifest.model_copy(update={"ac_id": normalized_ac_id})


def _normalize_optional_anchor(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        msg = f"load_ac_evidence_manifest received blank {name}"
        raise ValueError(msg)
    return stripped


def _chronological_events(events: Iterable[BaseEvent]) -> tuple[BaseEvent, ...]:
    """Return events oldest-first regardless of EventStore query ordering.

    Timestamp ties must preserve causal start-before-return ordering for
    journal pairs. ``BaseEvent.id`` is a UUID-like string, not a monotonic
    sequence, so it must never be used as a causality tie-breaker.
    """
    return tuple(sorted(events, key=_event_chronology_key))


def _event_chronology_key(event: BaseEvent) -> tuple[object, int]:
    return (event.timestamp, _event_phase_order(event.type))


def _event_phase_order(event_type: str) -> int:
    if event_type in {"tool.call.started", "llm.call.requested"}:
        return 0
    if event_type in {"tool.call.returned", "llm.call.returned"}:
        return 1
    return 2


__all__ = [
    "EventStoreEvidenceReader",
    "load_ac_evidence_manifest",
]
