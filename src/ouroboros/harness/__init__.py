"""Harness projection and evidence-manifest vocabulary for Ouroboros.

This package hosts read-only projections over the canonical ``EventStore``:
Run / Stage / Step / Artifact / Verdict records for #946 plus the
journal-to-evidence-manifest normalizer for #978.
"""

from ouroboros.harness.journal import (
    EvidenceEntry,
    EvidenceKind,
    EvidenceManifest,
    filter_events_for_ac,
    normalize_events,
)
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
    "EvidenceEntry",
    "EvidenceKind",
    "EvidenceManifest",
    "RunRecord",
    "StageKind",
    "StageRecord",
    "StepKind",
    "StepRecord",
    "VerdictOutcome",
    "VerdictRecord",
    "filter_events_for_ac",
    "normalize_events",
]
