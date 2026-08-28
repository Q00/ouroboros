# Workflow Failure Reasons Architecture

> Generated: 2026-08-13
> Approach: one pure closed-vocabulary classifier at the durable terminal boundary

## Overview

```text
runner / recovery path
        │ terminal status + structured result_meta
        ▼
JobManager._append_event ──► classify_failure()
        │                         │
        │                         ├─ result_meta: reason + action + next_step
        │                         └─ JobTelemetryBoundary
        │                                      │
        │                                      ▼
        │                           workflow_outcome allowlist
        │                           reason + action only
        ▼
job_status / job_result render the fixed next step
```

Direct, non-job evaluation calls use the same classifier at
`record_direct_evaluation_outcome()`. They emit the two telemetry enums but do
not have a durable job result in which to render `next_step`.

## Components

| Component | Responsibility | Location |
|---|---|---|
| `classify_failure` | Map terminal status and safe metadata to a fixed reason/action/text tuple | `src/ouroboros/mcp/failure_taxonomy.py` |
| Job terminal boundary | Enrich persisted terminal metadata before telemetry observation | `src/ouroboros/mcp/job_manager.py` |
| Durable telemetry | Emit only enum values on `workflow_outcome` | `src/ouroboros/telemetry.py` |
| Direct evaluation telemetry | Keep the non-job producer on the same contract | `src/ouroboros/mcp/telemetry_boundary.py` |
| User recovery surface | Render reason, action, and next step in terminal job summaries | `src/ouroboros/mcp/tools/job_handlers.py` |
| Public contract | Document the exact event fields and privacy boundary | `TELEMETRY.md` |

## Classification Rules

1. `cancelled` or `interrupted` status maps to `cancelled` and `retry`.
2. Shutdown, dead-owner, stranded-task, and user-cancellation flags map to the
   same cancellation route.
3. Progress-accounting stalls and explicit timeout metadata map to `timeout`
   and `retry`.
4. An exact machine-readable reason code may select `config`, `auth`, `model`,
   `tool`, or `validation` and its fixed action.
5. Anything else maps to `unknown` and `inspect_logs`.

No rule scans exception or result strings. This prevents privacy leakage and
avoids making telemetry semantics depend on provider wording.

## Data Flow and Invariants

- `JobManager._append_event()` enriches a copy of terminal event data; caller
  dictionaries are not mutated.
- `JobTelemetryBoundary.observe()` forwards only the structured `result_meta`
  dictionary to `capture_job_outcome()`.
- `capture_job_outcome()` derives `verified` exactly as before and preserves
  the same one-way `$insert_id` digest.
- `capture()` drops every property outside `_WORKFLOW_OUTCOME_KEYS`, which
  contains only the two new enum fields in addition to the existing contract.
- User-facing `next_step` is never included in telemetry.

## Recovery Measurement

After release, group failed `workflow_outcome` events by
`failure_reason_code`/`recovery_action`, then identify a later `command_run` or
terminal outcome for the same anonymous user within the chosen recovery window.
The first seven-day report must state the cohort size and unknown share before
being used as a product KPI.
