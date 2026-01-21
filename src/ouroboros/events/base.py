"""Base event definition for event sourcing.

All events in Ouroboros inherit from BaseEvent. Events are immutable
(frozen Pydantic models) and follow the dot.notation.past_tense naming convention.
"""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


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

    def to_db_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for database insertion.

        Returns:
            Dictionary with keys matching the events table columns.
        """
        return {
            "id": self.id,
            "event_type": self.type,
            "timestamp": self.timestamp,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "payload": self.data,
            "consensus_id": self.consensus_id,
        }

    @classmethod
    def from_db_row(cls, row: dict[str, Any]) -> BaseEvent:
        """Create event from database row.

        Args:
            row: Dictionary from database query result.

        Returns:
            BaseEvent instance.
        """
        return cls(
            id=row["id"],
            type=row["event_type"],
            timestamp=row["timestamp"],
            aggregate_type=row["aggregate_type"],
            aggregate_id=row["aggregate_id"],
            data=row["payload"],
        )
