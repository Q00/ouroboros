"""Ouroboros persistence module - event sourcing infrastructure."""

from ouroboros.persistence.checkpoint import (
    CheckpointData,
    CheckpointStore,
    PeriodicCheckpointer,
    RecoveryManager,
)
from ouroboros.persistence.event_store import EventStore
from ouroboros.persistence.schema import events_table, metadata
from ouroboros.persistence.uow import PhaseTransaction, UnitOfWork

__all__ = [
    "CheckpointData",
    "CheckpointStore",
    "EventStore",
    "PeriodicCheckpointer",
    "PhaseTransaction",
    "RecoveryManager",
    "UnitOfWork",
    "events_table",
    "metadata",
]
