"""Bounded EventStore projections for Disposable Memory artifacts."""

from __future__ import annotations

from uuid import NAMESPACE_URL, uuid5

from ouroboros.core.disposable_memory import DisposableResultEnvelope
from ouroboros.events.base import BaseEvent

ARTIFACT_REFERENCED_EVENT = "artifact.referenced"


def create_artifact_referenced_event(envelope: DisposableResultEnvelope) -> BaseEvent:
    """Create the small main-ledger row; artifact content is never accepted."""
    # The id is derived from the contract id alone.  A contract publishes at most
    # one artifact into one store, so the contract already names the row; folding
    # the content address in would tie the identity to a storage detail that is
    # on its way out.  This id is also the exactly-once key, and a row written
    # under the older contract-plus-ref formula deliberately does not match it:
    # that row names a body in the filesystem store, which this change deletes.
    event_id = uuid5(
        NAMESPACE_URL,
        f"ouroboros:artifact:{envelope.contract_id}:referenced",
    )
    return BaseEvent(
        id=str(event_id),
        type=ARTIFACT_REFERENCED_EVENT,
        aggregate_type="contract",
        aggregate_id=envelope.contract_id,
        data=envelope.model_dump(mode="json"),
    )


__all__ = [
    "ARTIFACT_REFERENCED_EVENT",
    "create_artifact_referenced_event",
]
