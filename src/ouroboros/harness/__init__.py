"""Harness projection vocabulary for Ouroboros.

This module hosts the public Run / Stage / Step / Artifact / Verdict
projection vocabulary derived from the canonical event store. The records
are read-models, not a replacement for the underlying ``EventStore``.

See ``ouroboros.harness.projection`` for the schema.
"""

from ouroboros.harness.projection import (
    ArtifactRecord,
    RunRecord,
    StageKind,
    StageRecord,
    StepKind,
    StepRecord,
    VerdictOutcome,
    VerdictRecord,
)

__all__ = [
    "ArtifactRecord",
    "RunRecord",
    "StageKind",
    "StageRecord",
    "StepKind",
    "StepRecord",
    "VerdictOutcome",
    "VerdictRecord",
]
