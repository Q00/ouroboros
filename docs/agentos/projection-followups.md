# AgentOS Projection Follow-up Sequence

This document records the remaining #946 follow-up lanes after the v1 projection
records, EventStore builder, MCP query surface, identity hardening, boundary doc,
and RunSnapshot read model have landed.

## Current baseline

The #946 baseline is a read-only projection stack over persisted events:

1. `RunRecord`, `StageRecord`, `StepRecord`, `ArtifactRecord`, and
   `VerdictRecord` schemas exist.
2. `ProjectionBuilder` derives stable Run/Stage/Step records from known
   tool/LLM event pairs.
3. `ouroboros_query_projection` exposes machine-readable projection data from
   EventStore rows without writing rows or creating schema.
4. `build_run_snapshot` derives a conservative safe-resume read model from
   projection records.

## Next safe PR slots

| Slot | Purpose | Boundary |
| --- | --- | --- |
| Artifact/Verdict projection | Populate existing `ArtifactRecord` and `VerdictRecord` outputs from persisted evidence/evaluation-like events. | Read-model only; no new evidence schema or verifier policy. |
| Status JSON CLI | Expose the existing projection query through a thin `ouroboros status run ... --json` surface. | Reuse projection semantics; no cache or writes. |
| Mechanical-evaluation fixture | Prove a small execution/evaluation history projects to run, step, artifact, verdict, and source event IDs. | Offline/local fixture only. |
| StepSnapshot/session/runtime views | Add bounded derived views for post-step state, session health, runtime handle, and resume-token metadata. | Later views; do not block artifact/verdict or CLI JSON work. |
| Context/checkpoint anchors | Surface context pack and checkpoint references as projection metadata. | Read-only anchors; no second context state model. |
| Optional exporter sinks | Feed OTEL or other exporters from projections. | Optional/lazy, disabled by default, never source of truth. |

## Explicit anti-actions

- Do not make projection records authoritative state. EventStore/journal remains
  the source of truth.
- Do not add projection caching without a cache invalidation and migration owner.
- Do not define a new typed AC evidence schema in #946; #830/#978 own evidence
  semantics.
- Do not add plugin permissions/audit behavior in #946; #939 owns that surface.
- Do not add HITL resume authority in #946; #960 owns WAIT/RESUME behavior.
- Do not wire Workflow IR live dispatch from #946; #956 owns planning IR and
  #920/#978 govern execution/default gates.

## Review checklist

A #946 follow-up is safe when it can answer yes to all of these:

- Is the change rebuildable from the same source event slice?
- Does it preserve existing source event IDs or mark legacy/inferred gaps
  explicitly?
- Does it expose bounded/redacted metadata rather than raw prompts, raw stdout,
  secrets, or unbounded provider payloads?
- Does it improve projection inspection without changing runtime behavior?
- Does the PR body name which follow-up slot above it implements?
