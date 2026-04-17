"""Base event definition for event sourcing.

All events in Ouroboros inherit from BaseEvent. Events are immutable
(frozen Pydantic models) and follow the dot.notation.past_tense naming convention.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

_EXCLUDED_PERSISTENCE_KEYS = frozenset(
    {
        "event_payload",
        "event_payloads",
        "raw_event",
        "raw_events",
        "raw_payload",
        "raw_payloads",
        "raw_subscribed_event",
        "raw_subscribed_events",
        "subscribed_event",
        "subscribed_event_payload",
        "subscribed_event_payloads",
        "subscribed_events",
        "subscribed_payload",
        "subscribed_payloads",
    }
)


def _should_exclude_from_persistence(key: str) -> bool:
    """Return True when a nested payload key should not be persisted."""
    normalized = key.strip().lower().replace("-", "_")
    if normalized in _EXCLUDED_PERSISTENCE_KEYS:
        return True
    if normalized.startswith("raw_"):
        return True
    return normalized.startswith("subscribed_") and (
        "event" in normalized or "payload" in normalized
    )


def sanitize_event_data_for_persistence(value: Any) -> Any:
    """Recursively strip raw subscribed payloads from persisted event data."""
    if isinstance(value, dict):
        return {
            key: sanitize_event_data_for_persistence(item)
            for key, item in value.items()
            if not _should_exclude_from_persistence(str(key))
        }
    if isinstance(value, list):
        return [sanitize_event_data_for_persistence(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_event_data_for_persistence(item) for item in value]
    return value


class BaseEvent(BaseModel, frozen=True):
    """Base class for all Ouroboros events.

    Events are immutable records of state changes. They are persisted in the
    event store and can be replayed to reconstruct aggregate state.

    Attributes:
        id: Unique event identifier (UUID).
        type: Event type following dot.notation.past_tense convention.
              Examples: "ontology.concept.added", "execution.ac.completed"
        timestamp: When the event occurred (UTC).
        aggregate_type: Type of aggregate this event belongs to.
        aggregate_id: Unique identifier of the aggregate.
        data: Event-specific payload data.
        consensus_id: Optional consensus identifier for grouped events.
        event_version: Schema version for the persisted payload.
            Version 1 is the baseline. Legacy rows without this field
            are deserialized as version 0.

    Example:
        event = BaseEvent(
            type="ontology.concept.added",
            aggregate_type="ontology",
            aggregate_id="ont-123",
            data={"concept_name": "authentication", "weight": 1.0}
        )
    """

    id: str = Field(default_factory=lambda: str(uuid4()))
    type: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    aggregate_type: str
    aggregate_id: str
    data: dict[str, Any] = Field(default_factory=dict)
    consensus_id: str | None = Field(default=None)
    event_version: int = Field(default=1)

    def to_db_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for database insertion.

        The ``event_version`` is injected into the persisted payload so
        consumers can detect schema changes without a database migration.

        Returns:
            Dictionary with keys matching the events table columns.
        """
        payload = sanitize_event_data_for_persistence(self.data)
        payload["event_version"] = self.event_version
        return {
            "id": self.id,
            "event_type": self.type,
            "timestamp": self.timestamp,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "payload": payload,
            "consensus_id": self.consensus_id,
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> BaseEvent:
        """Create event from database row.

        Legacy rows that predate the ``event_version`` field are loaded
        with ``event_version=0`` so callers can distinguish old data.

        Args:
            row: Dictionary from database query result.

        Returns:
            BaseEvent instance.
        """
        raw_payload = row["payload"]
        event_version = 0
        if isinstance(raw_payload, dict):
            raw_version = raw_payload.get("event_version", 0)
            event_version = raw_version if isinstance(raw_version, int) else 0
            payload = {k: v for k, v in raw_payload.items() if k != "event_version"}
        else:
            payload = raw_payload
        return cls(
            id=row["id"],
            type=row["event_type"],
            timestamp=row["timestamp"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            data=payload,
            event_version=event_version,
        )
