"""LineageGuard — validates state transitions before event persistence.

Wraps EventStore.append() with transition matrix validation. Only lineage
events (aggregate_type == "lineage") are validated; all other events pass
through unconditionally.

Note: append_batch() is not covered by this guard. Lineage events currently
do not use batch append.
"""

from __future__ import annotations

import logging

from ouroboros.core.errors import TransitionError
from ouroboros.core.lineage import LineageStatus
from ouroboros.events.base import BaseEvent
from ouroboros.evolution.projector import LineageProjector
from ouroboros.evolution.transitions import is_transition_allowed
from ouroboros.persistence.event_store import EventStore

logger = logging.getLogger(__name__)


class LineageGuard:
    """Validates lineage event transitions before persisting to EventStore.

    Usage:
        guard = LineageGuard(event_store)
        await guard.gated_append(event)  # validates, then appends
    """

    def __init__(self, event_store: EventStore) -> None:
        self._event_store = event_store

    async def gated_append(self, event: BaseEvent) -> None:
        """Validate and append an event to the EventStore.

        Non-lineage events (aggregate_type != "lineage") pass through
        without validation. lineage.created always passes (initial event).
        All other lineage events are checked against the transition matrix.

        Args:
            event: The event to validate and append.

        Raises:
            TransitionError: If the event is not allowed in the current
                lineage status.
            PersistenceError: If the underlying append fails.
        """
        if event.aggregate_type != "lineage":
            await self._event_store.append(event)
            return

        if event.type == "lineage.created":
            await self._event_store.append(event)
            return

        # Determine current status from event stream (single source of truth)
        events = await self._event_store.replay_lineage(event.aggregate_id)
        current_status = LineageProjector.resolve_status(events)

        if not is_transition_allowed(current_status, event.type):
            raise TransitionError(
                f"Event '{event.type}' not allowed in status '{current_status.value}'",
                current_status=current_status.value,
                event_type=event.type,
            )

        await self._event_store.append(event)
