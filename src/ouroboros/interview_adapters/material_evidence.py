"""Revision-bound, non-authoritative local material references."""

import hashlib
import json
from typing import Literal

from ouroboros.core.material_evidence import MaterialEvidenceRef
from ouroboros.interview_adapters.inline_materials import InlineMaterial, MaterialEvidenceDecision

MaterialKind = Literal["source_quote", "model_assumption"]
MaterialAction = Literal["include", "remove"]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_digest(source: InlineMaterial) -> str:
    return _sha(source.text)


def draft_digest(source: InlineMaterial) -> str:
    return _sha(source.draft.model_dump_json() if source.draft else "")


def revision(source: InlineMaterial, count: int | None = None) -> str:
    events = source.evidence_decisions if count is None else source.evidence_decisions[:count]
    body = {
        "source_digest": source_digest(source),
        "draft_digest": draft_digest(source),
        "events": [event.model_dump(mode="json") for event in events],
    }
    return _sha(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True))


def item_text(source: InlineMaterial, kind: MaterialKind, index: int) -> str | None:
    if source.draft is None or type(index) is not int or index < 0:
        return None
    values = (
        source.draft.source_quotes if kind == "source_quote" else source.draft.inferred_assumptions
    )
    return values[index] if index < len(values) else None


def confirmation_prompt(
    source: InlineMaterial,
    kind: MaterialKind,
    index: int,
    action: MaterialAction,
    *,
    session_id: str,
    revision_token: str | None = None,
) -> str:
    item = item_text(source, kind, index)
    if item is None:
        raise ValueError("Unknown material item")
    label = "source quotation" if kind == "source_quote" else "unverified model assumption"
    verb = (
        "include a local reference to"
        if action == "include"
        else "remove the active local reference to"
    )
    return (
        f"Session: {session_id}. Item: {kind} {index}. Revision: {revision_token or revision(source)}\n"
        f"The {label} for this item is shown separately in the material view.\n"
        f"{action.upper()}: {verb} this item in future Seeds? "
        "The Seed contains random local lookup IDs, not the text, and cannot resolve them without "
        "this local interview state. A person or host resolving the reference sees the text and "
        "may transmit it elsewhere. This is not confirmation of truth or a requirement. "
        "A prior exported Seed is not erased by removal. "
        f"Reply exactly {action.upper()} to proceed."
    )


def verified_active(
    source: InlineMaterial,
    session_id: str,
) -> dict[tuple[str, int], MaterialEvidenceDecision] | None:
    """Validate the local chain before resolving or copying opaque handles."""
    active: dict[tuple[str, int], MaterialEvidenceDecision] = {}
    first_source_id: str | None = None
    seen_events: set[str] = set()
    seen_evidence: set[str] = set()
    for n, event in enumerate(source.evidence_decisions):
        if first_source_id is None:
            first_source_id = event.source_id
        item = item_text(source, event.kind, event.index)
        if (
            item is None
            or event.source_id != first_source_id
            or event.source_digest != source_digest(source)
            or event.draft_digest != draft_digest(source)
            or event.previous_revision != revision(source, n)
            or event.event_id in seen_events
            or event.presented_prompt
            != confirmation_prompt(
                source,
                event.kind,
                event.index,
                event.action,
                session_id=session_id,
                revision_token=event.previous_revision,
            )
            or (event.kind == "source_quote" and item not in source.text)
        ):
            return None
        seen_events.add(event.event_id)
        key = (event.kind, event.index)
        previous = active.get(key)
        if event.action == "include":
            if previous is not None or event.evidence_id in seen_evidence:
                return None
            seen_evidence.add(event.evidence_id)
            active[key] = event
        elif previous is None or previous.evidence_id != event.evidence_id:
            return None
        else:
            del active[key]
    return active


def evidence_refs(
    source: InlineMaterial | None, session_id: str
) -> tuple[MaterialEvidenceRef, ...]:
    if source is None:
        return ()
    active = verified_active(source, session_id)
    if active is None:
        raise ValueError("Invalid local material evidence chain")
    return tuple(
        MaterialEvidenceRef(source_id=e.source_id, evidence_id=e.evidence_id, kind=e.kind)
        for e in active.values()
    )
