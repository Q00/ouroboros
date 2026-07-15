"""Typed boundary for committed lineage rewinds and post-commit observers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ouroboros.core.errors import OuroborosError
from ouroboros.core.lineage import OntologyLineage
from ouroboros.core.types import Result


@dataclass(frozen=True, slots=True)
class RewindObservationSnapshot:
    """Scalar-only view of a committed rewind exposed to observers."""

    lineage_id: str
    from_generation: int
    to_generation: int
    rewind_event_id: str
    rewind_occurred_at: datetime


@dataclass(frozen=True, slots=True)
class CommittedRewindResult:
    """Immutable caller-facing result captured before observer dispatch."""

    lineage: OntologyLineage
    lineage_id: str
    from_generation: int
    to_generation: int
    rewind_event_id: str
    rewind_occurred_at: datetime

    def observation_snapshot(self) -> RewindObservationSnapshot:
        """Return the least-privilege observer input for this commit."""
        return RewindObservationSnapshot(
            lineage_id=self.lineage_id,
            from_generation=self.from_generation,
            to_generation=self.to_generation,
            rewind_event_id=self.rewind_event_id,
            rewind_occurred_at=self.rewind_occurred_at,
        )


class RewindObserver(Protocol):
    """Optional post-commit observer with no authority over rewind outcome."""

    async def observe(self, snapshot: RewindObservationSnapshot) -> None:
        """Observe an already committed rewind."""
        ...


class RewindCommitter(Protocol):
    """Caller boundary for the canonical rewind commit primitive."""

    async def rewind_to(
        self,
        lineage: OntologyLineage,
        generation_number: int,
    ) -> Result[CommittedRewindResult, OuroborosError]:
        """Commit one lineage rewind through the canonical primitive."""
        ...


class NoOpRewindObserver:
    """Default observer preserving rewind behavior when plugins are absent."""

    async def observe(self, snapshot: RewindObservationSnapshot) -> None:  # noqa: ARG002
        return None


__all__ = [
    "CommittedRewindResult",
    "NoOpRewindObserver",
    "RewindCommitter",
    "RewindObservationSnapshot",
    "RewindObserver",
]
