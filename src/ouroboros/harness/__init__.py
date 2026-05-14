"""Harness projection and evidence-manifest vocabulary for Ouroboros.

This package hosts read-only projections over the canonical ``EventStore``:
Run / Stage / Step / Artifact / Verdict records for #946 plus the
journal-to-evidence-manifest normalizer for #978.
"""

from ouroboros.harness.claim_term_guard import (
    ClaimTermGuard,
    ClaimTermGuardFact,
    ClaimTermGuardVerdict,
    deterministic_claim_term_guard,
)
from ouroboros.harness.deliver_gate import (
    DeliverEvidenceClaim,
    DeliverEvidenceFact,
    DeliverGateVerdict,
    EventStoreEvidenceReader,
    TraceGuardEvidenceInput,
    TraceGuardResultLike,
    TraceGuardValidator,
    evaluate_deliver_claim,
    load_ac_evidence_manifest,
)
from ouroboros.harness.deliver_routing import DeliverGateRoute, route_deliver_gate_verdict
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
    "DeliverEvidenceClaim",
    "DeliverEvidenceFact",
    "DeliverGateVerdict",
    "DeliverGateRoute",
    "EvidenceEntry",
    "EvidenceKind",
    "EventStoreEvidenceReader",
    "EvidenceManifest",
    "RunRecord",
    "ClaimTermGuard",
    "ClaimTermGuardFact",
    "ClaimTermGuardVerdict",
    "StageKind",
    "StageRecord",
    "StepKind",
    "StepRecord",
    "TraceGuardEvidenceInput",
    "TraceGuardResultLike",
    "TraceGuardValidator",
    "VerdictOutcome",
    "VerdictRecord",
    "deterministic_claim_term_guard",
    "evaluate_deliver_claim",
    "filter_events_for_ac",
    "load_ac_evidence_manifest",
    "normalize_events",
    "route_deliver_gate_verdict",
]
