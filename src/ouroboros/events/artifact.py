"""Bounded EventStore projections for Disposable Memory artifacts."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from ouroboros.core.disposable_memory import DisposableResultEnvelope
from ouroboros.events.base import BaseEvent

ARTIFACT_REFERENCED_EVENT = "artifact.referenced"
ARTIFACT_TOMBSTONED_EVENT = "artifact.tombstoned"


def create_artifact_referenced_event(envelope: DisposableResultEnvelope) -> BaseEvent:
    """Create the small main-ledger row; artifact content is never accepted."""
    event_id = uuid5(
        NAMESPACE_URL,
        f"ouroboros:artifact:{envelope.contract_id}:{envelope.artifact_ref}:referenced",
    )
    return BaseEvent(
        id=str(event_id),
        type=ARTIFACT_REFERENCED_EVENT,
        aggregate_type="contract",
        aggregate_id=envelope.contract_id,
        data=envelope.model_dump(mode="json"),
    )


def create_artifact_tombstoned_event(
    *,
    contract_id: str,
    artifact_ref: str,
    reason: str,
) -> BaseEvent:
    """Create an optional EventStore tombstone projection for manifest GC."""
    event_id = uuid5(
        NAMESPACE_URL,
        f"ouroboros:artifact:{contract_id}:{artifact_ref}:tombstoned",
    )
    return BaseEvent(
        id=str(event_id),
        type=ARTIFACT_TOMBSTONED_EVENT,
        aggregate_type="contract",
        aggregate_id=contract_id,
        data={
            "contract_id": contract_id,
            "artifact_ref": artifact_ref,
            "reason": reason,
        },
    )


__all__ = [
    "ARTIFACT_REFERENCED_EVENT",
    "ARTIFACT_TOMBSTONED_EVENT",
    "create_artifact_referenced_event",
    "create_artifact_tombstoned_event",
]
