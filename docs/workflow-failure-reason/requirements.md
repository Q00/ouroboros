# Workflow Failure Reasons Requirements

> Generated: 2026-08-13
> Status: Implemented locally; release and recovery-rate measurement pending

## Original Requirement

Add privacy-safe failure reasons and a user recovery path to failed `run` and
`ralph` workflows, then measure recovery by reason in PostHog.

## Clarified Specification

The durable MCP job terminal boundary must classify failed, cancelled, and
interrupted outcomes into a closed `failure_reason_code` enum and a closed
`recovery_action` enum. The same fields must be present on direct evaluation
terminal telemetry so the event contract does not diverge between producers.

The classifier may use only terminal status and existing structured metadata.
Raw exception messages, result text, prompts, paths, job IDs, and identifiers
must never be used as telemetry values or classification inputs.

Every failed terminal job result must include a fixed user-visible `next_step`
alongside the reason and action. Existing completion semantics, `verified`, and
job deduplication must remain unchanged.

## Enum Contract

`failure_reason_code`:

`config | auth | timeout | model | tool | validation | cancelled | unknown`

`recovery_action`:

`retry | setup | login | update | inspect_logs | none`

The current implementation emits `unknown` plus `inspect_logs` when no safe
machine-readable signal exists. It emits `cancelled` for cancellation,
shutdown interruption, dead-owner interruption, and stranded-task interruption;
progress-accounting stalls and explicit timeout metadata emit `timeout`.

## Success Criteria

- All non-success durable job terminal outcomes have a reason/action pair.
- A normal exception with no structured code falls back to `unknown` without
  forwarding its raw text.
- Job status/result surfaces show the recommended next step.
- `workflow_outcome` allowlisting and `TELEMETRY.md` describe the exact same
  fields.
- Existing `$insert_id`, `verified`, and final approval behavior are unchanged.
- After release, a complete seven-day window can calculate recovery by reason
  from the terminal event followed by a later command or terminal outcome.

## Out of Scope

- Sending exception text, prompt content, paths, or provider identifiers.
- Inferring a reason from arbitrary human-readable error messages.
- Claiming the seven-day recovery KPI before this change is released and
  observed in a complete cohort.
