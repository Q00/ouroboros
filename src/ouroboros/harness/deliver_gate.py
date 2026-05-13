"""Read-only helpers for the #978 evidence deliver gate.

This module is the first P2-safe bridge between the journal normalizer and the
TraceGuard verdict call. It deliberately does **not** change AC success
semantics: callers receive an :class:`EvidenceManifest` and can evaluate an
explicit deliver claim through an injected TraceGuard-compatible validator while
legacy completion remains untouched until a later gate PR explicitly owns
behavior changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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


class TraceGuardResultLike(Protocol):
    """Subset returned by ``rlm_forge.traceguard.validate_parent_synthesis``."""

    accepted: bool
    accepted_claims: object
    rejected_claims: object
    allowed_fact_ids: object
    allowed_chunk_ids: object

    @property
    def unsupported_claim_rate(self) -> float:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class TraceGuardEvidenceInput:
    """Duck-typed input compatible with ``TraceGuardEvidence``."""

    fact_id: str
    chunk_id: str
    text: str
    child_call_id: str | None = None


class TraceGuardValidator(Protocol):
    """Callable shape for the injected deterministic TraceGuard validator."""

    def __call__(
        self,
        *,
        evidence_manifest: tuple[TraceGuardEvidenceInput, ...],
        parent_synthesis: dict[str, Any],
    ) -> TraceGuardResultLike:
        raise NotImplementedError


class DeliverEvidenceFact(BaseModel, frozen=True):
    """One leaf-delivery fact the agent claims is backed by evidence."""

    model_config = ConfigDict(extra="forbid")

    fact_id: str = Field(..., min_length=1)
    evidence_handle: str = Field(..., min_length=1)
    statement: str = Field(default="")

    @field_validator("fact_id", "evidence_handle")
    @classmethod
    def _identifier_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "deliver evidence fact identifiers must be non-blank"
            raise ValueError(msg)
        return stripped


class DeliverEvidenceClaim(BaseModel, frozen=True):
    """Structured AC completion claim passed to the deliver gate."""

    model_config = ConfigDict(extra="forbid")

    ac_id: str = Field(..., min_length=1)
    facts: tuple[DeliverEvidenceFact, ...] = Field(default_factory=tuple)

    @field_validator("ac_id")
    @classmethod
    def _ac_id_not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "DeliverEvidenceClaim.ac_id must be non-blank"
            raise ValueError(msg)
        return stripped

    @field_validator("facts")
    @classmethod
    def _facts_not_empty(
        cls, value: tuple[DeliverEvidenceFact, ...]
    ) -> tuple[DeliverEvidenceFact, ...]:
        if not value:
            msg = "DeliverEvidenceClaim requires at least one fact"
            raise ValueError(msg)
        seen: set[str] = set()
        for fact in value:
            if fact.fact_id in seen:
                msg = f"DeliverEvidenceClaim fact_id {fact.fact_id!r} is duplicated"
                raise ValueError(msg)
            seen.add(fact.fact_id)
        return value


class DeliverGateVerdict(BaseModel, frozen=True):
    """TraceGuard-derived verdict for one AC deliver claim.

    The verdict is intentionally a read-model value. It does not mark the AC
    complete by itself; later #920/#978 PRs can A/B record it or use it to drive
    retry / redispatch / escalation routing.
    """

    model_config = ConfigDict(extra="forbid")

    ac_id: str = Field(..., min_length=1)
    accepted: bool
    unsupported_claim_rate: float = Field(..., ge=0.0, le=1.0)
    accepted_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    rejected_fact_ids: tuple[str, ...] = Field(default_factory=tuple)
    rejected_reasons: tuple[str, ...] = Field(default_factory=tuple)
    evidence_event_ids: tuple[str, ...] = Field(default_factory=tuple)

    @model_validator(mode="after")
    def _accepted_verdict_has_no_rejections(self) -> DeliverGateVerdict:
        if self.accepted and (self.rejected_fact_ids or self.rejected_reasons):
            msg = "accepted DeliverGateVerdict cannot carry rejected claims"
            raise ValueError(msg)
        if not self.accepted and not self.rejected_reasons:
            msg = "rejected DeliverGateVerdict must include rejection reasons"
            raise ValueError(msg)
        return self


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

    ``execution_id`` is required so the deliver-gate input is bounded to one
    execution. When ``session_id`` is also available the loader uses the
    session-related query with the execution correlation filter; otherwise it
    uses the execution-only query. Session-only reads are rejected because a
    session can contain multiple executions/retries that must not be spliced
    into one verifier input.

    Args:
        event_store: Read-capable EventStore or test double.
        ac_id: Acceptance-criterion identifier to normalize.
        execution_id: Required execution aggregate anchor.
        session_id: Optional session aggregate anchor used as an additional
            ownership filter.
        scope_id: Optional event-scope token to filter by when the public AC
            id differs from the runtime aggregate/phase token used by the
            recorder. Defaults to ``ac_id``.
        limit: Optional EventStore query cap. The default ``None`` reads the
            full related event set so the manifest is not silently truncated
            before TraceGuard sees it.

    Raises:
        ValueError: If ``ac_id`` is blank, if ``execution_id`` is missing
            or blank, or if optional anchors are whitespace-only.

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
    if normalized_execution_id is None:
        msg = "load_ac_evidence_manifest requires execution_id"
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

    filtered_events = _filter_events_by_anchors(
        events,
        execution_id=normalized_execution_id,
        session_id=normalized_session_id,
    )
    manifest = normalize_events(_chronological_events(filtered_events), ac_id=normalized_scope_id)
    if normalized_scope_id == normalized_ac_id:
        return manifest
    return manifest.model_copy(update={"ac_id": normalized_ac_id})


def evaluate_deliver_claim(
    manifest: EvidenceManifest,
    claim: DeliverEvidenceClaim,
    *,
    traceguard_validator: TraceGuardValidator,
) -> DeliverGateVerdict:
    """Evaluate a typed AC deliver claim with a TraceGuard-compatible validator.

    This is the narrow verdict-adapter slice for #978 P2. It converts
    Ouroboros' journal-derived :class:`EvidenceManifest` into TraceGuard's
    canonical ``fact_id`` / ``chunk_id`` manifest shape and converts the leaf
    claim into TraceGuard's ``parent_synthesis`` claim surface. The deterministic
    validator is injected so this PR does not add a hard runtime dependency or
    alter live AC success semantics.
    """
    if manifest.ac_id != claim.ac_id:
        msg = (
            "DeliverEvidenceClaim.ac_id must match EvidenceManifest.ac_id "
            f"({claim.ac_id!r} != {manifest.ac_id!r})"
        )
        raise ValueError(msg)

    traceguard_manifest, source_events_by_handle = _traceguard_manifest(manifest, claim)
    parent_synthesis = _parent_synthesis_from_claim(claim)
    raw_result = traceguard_validator(
        evidence_manifest=traceguard_manifest,
        parent_synthesis=parent_synthesis,
    )
    rejected = _rejected_claim_summaries(raw_result)
    accepted_fact_ids = _claim_fact_ids(getattr(raw_result, "accepted_claims", ()))
    rejected_fact_ids = tuple(fact_id for fact_id, _, _ in rejected if fact_id is not None)
    accepted_handles = _claim_chunk_ids(getattr(raw_result, "accepted_claims", ()))

    return DeliverGateVerdict(
        ac_id=manifest.ac_id,
        accepted=bool(raw_result.accepted),
        unsupported_claim_rate=float(raw_result.unsupported_claim_rate),
        accepted_fact_ids=accepted_fact_ids,
        rejected_fact_ids=rejected_fact_ids,
        rejected_reasons=tuple(reason for _, _, reason in rejected),
        evidence_event_ids=_evidence_event_ids_for_handles(
            accepted_handles,
            source_events_by_handle=source_events_by_handle,
        ),
    )


def _filter_events_by_anchors(
    events: Iterable[BaseEvent],
    *,
    execution_id: str | None,
    session_id: str | None,
) -> tuple[BaseEvent, ...]:
    return tuple(
        event
        for event in events
        if _event_matches_required_anchors(
            event,
            execution_id=execution_id,
            session_id=session_id,
        )
    )


def _event_matches_required_anchors(
    event: BaseEvent,
    *,
    execution_id: str | None,
    session_id: str | None,
) -> bool:
    if execution_id is not None and not _event_matches_anchor(
        event,
        execution_id,
        keys=("execution_id", "parent_execution_id"),
    ):
        return False
    return not (
        session_id is not None
        and not _event_matches_anchor(
            event,
            session_id,
            keys=("session_id",),
        )
    )


def _event_matches_anchor(event: BaseEvent, anchor: str, *, keys: tuple[str, ...]) -> bool:
    if event.aggregate_id == anchor:
        return True
    if isinstance(event.data, dict):
        for key in keys:
            value = event.data.get(key)
            if isinstance(value, str) and value.strip() == anchor:
                return True
    return False


def _normalize_optional_anchor(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        msg = f"load_ac_evidence_manifest received blank {name}"
        raise ValueError(msg)
    return stripped


def _traceguard_manifest(
    manifest: EvidenceManifest,
    claim: DeliverEvidenceClaim,
) -> tuple[tuple[TraceGuardEvidenceInput, ...], dict[str, tuple[str, ...]]]:
    entries_by_handle = {entry.handle: entry for entry in manifest.entries if entry.ok is True}
    entries: list[TraceGuardEvidenceInput] = []
    source_events_by_handle: dict[str, tuple[str, ...]] = {}
    for fact in claim.facts:
        entry = entries_by_handle.get(fact.evidence_handle)
        if entry is None:
            continue
        text = _evidence_text(entry.payload)
        entries.append(
            TraceGuardEvidenceInput(
                fact_id=fact.fact_id,
                chunk_id=fact.evidence_handle,
                text=text,
                child_call_id=",".join(entry.source_event_ids),
            )
        )
        source_events_by_handle[entry.handle] = entry.source_event_ids
    return tuple(entries), source_events_by_handle


def _evidence_text(payload: object) -> str:
    if not isinstance(payload, Mapping):
        return str(payload)
    for key in ("result_preview", "args_preview", "tool_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return str(dict(payload))


def _parent_synthesis_from_claim(claim: DeliverEvidenceClaim) -> dict[str, Any]:
    return {
        "result": {
            "observed_facts": [
                {
                    "fact_id": fact.fact_id,
                    "chunk_id": fact.evidence_handle,
                    "statement": fact.statement,
                }
                for fact in claim.facts
            ]
        }
    }


def _claim_fact_ids(claims: object) -> tuple[str, ...]:
    return tuple(
        fact_id
        for fact_id in (_claim_attr(claim, "fact_id") for claim in _iter_result_items(claims))
        if fact_id is not None
    )


def _claim_chunk_ids(claims: object) -> tuple[str, ...]:
    return tuple(
        chunk_id
        for chunk_id in (_claim_attr(claim, "chunk_id") for claim in _iter_result_items(claims))
        if chunk_id is not None
    )


def _rejected_claim_summaries(
    result: TraceGuardResultLike,
) -> tuple[tuple[str | None, str | None, str], ...]:
    summaries: list[tuple[str | None, str | None, str]] = []
    for rejection in _iter_result_items(getattr(result, "rejected_claims", ())):
        claim = _object_value(rejection, "claim")
        reason = _object_value(rejection, "reason")
        detail = _object_value(rejection, "detail")
        summaries.append(
            (
                _claim_attr(claim, "fact_id"),
                _claim_attr(claim, "chunk_id"),
                _join_reason(reason, detail),
            )
        )
    return tuple(summaries)


def _evidence_event_ids_for_handles(
    handles: tuple[str, ...],
    *,
    source_events_by_handle: dict[str, tuple[str, ...]],
) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for handle in handles:
        for event_id in source_events_by_handle.get(handle, ()):
            if event_id not in seen:
                ordered.append(event_id)
                seen.add(event_id)
    return tuple(ordered)


def _iter_result_items(value: object) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    return ()


def _claim_attr(claim: object, name: str) -> str | None:
    value = _object_value(claim, name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _object_value(item: object, name: str) -> object:
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _join_reason(reason: object, detail: object) -> str:
    reason_text = reason if isinstance(reason, str) and reason.strip() else "traceguard_rejected"
    detail_text = detail if isinstance(detail, str) and detail.strip() else ""
    if detail_text:
        return f"{reason_text}: {detail_text}"
    return reason_text


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
    "DeliverEvidenceClaim",
    "DeliverEvidenceFact",
    "DeliverGateVerdict",
    "EventStoreEvidenceReader",
    "TraceGuardResultLike",
    "TraceGuardEvidenceInput",
    "TraceGuardValidator",
    "evaluate_deliver_claim",
    "load_ac_evidence_manifest",
]
