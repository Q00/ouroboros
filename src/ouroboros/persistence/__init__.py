"""Ouroboros persistence module - event sourcing infrastructure."""

from ouroboros.persistence.brownfield import BrownfieldRepo, BrownfieldStore
from ouroboros.persistence.checkpoint import (
    CheckpointData,
    CheckpointStore,
    PeriodicCheckpointer,
    RecoveryManager,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import brownfield_repos_table, events_table, metadata
from ouroboros.persistence.uow import PhaseTransaction, UnitOfWork

__all__ = [
    "BrownfieldRepo",
    "BrownfieldStore",
    "CheckpointData",
    "CheckpointStore",
    "EventStore",
    "PeriodicCheckpointer",
    "PhaseTransaction",
    "RecoveryManager",
    "UnitOfWork",
    "brownfield_repos_table",
    "events_table",
    "metadata",
]
