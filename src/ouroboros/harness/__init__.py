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
    strict_deterministic_claim_term_guard,
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
    RunSnapshotRecord,
    RunSnapshotStatus,
    StageKind,
    StageRecord,
    StepKind,
    StepRecord,
    VerdictOutcome,
    VerdictRecord,
)
from ouroboros.harness.run_snapshot import build_run_snapshot
from ouroboros.harness.traceguard_validator import (
    TraceGuardClaimRef,
    TraceGuardRejection,
    TraceGuardValidationResult,
    validate_evidence_claims,
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
    "RunSnapshotRecord",
    "RunSnapshotStatus",
    "ClaimTermGuard",
    "ClaimTermGuardFact",
    "ClaimTermGuardVerdict",
    "StageKind",
    "StageRecord",
    "StepKind",
    "StepRecord",
    "TraceGuardClaimRef",
    "TraceGuardEvidenceInput",
    "TraceGuardRejection",
    "TraceGuardResultLike",
    "TraceGuardValidationResult",
    "TraceGuardValidator",
    "VerdictOutcome",
    "VerdictRecord",
    "build_run_snapshot",
    "deterministic_claim_term_guard",
    "strict_deterministic_claim_term_guard",
    "evaluate_deliver_claim",
    "filter_events_for_ac",
    "load_ac_evidence_manifest",
    "normalize_events",
    "route_deliver_gate_verdict",
    "validate_evidence_claims",
]
