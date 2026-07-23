# Foundation B — Attempt versus final acceptance

## Scope

Foundation B separates observations made during an AC attempt from the one
final acceptance decision for that AC authority generation. It does not add
dispatch, routing, capsules, cross-run trust, or provider-specific policy.

## Event contract

### `execution.ac.attempt_judged`

This is provisional telemetry emitted by the outer verify/retry layer after an
individual runtime attempt has been judged. A root AC may have several of
these events, one for each `retry_attempt`. It never grants acceptance and
must not trigger a successor, route change, or final cleanup.

Required fields:

```json
{
  "execution_id": "exec_...",
  "session_id": "sess_...",
  "root_ac_index": 0,
  "retry_attempt": 1,
  "attempt_number": 2,
  "success": false,
  "outcome": "failed",
  "is_decomposed": true
}
```

The existing `execution.ac.outcome_finalized` name is retained only as a
legacy read alias for historical rows. New producers emit
`execution.ac.attempt_judged`; consumers must not treat either event as final
acceptance.

### `execution.ac.acceptance_finalized`

This is the Final Gate's durable decision after the complete same-authority
attempt episode has closed and the session terminal state is known. It is the
only event that can declare `accepted: true`. A final rejected/blocked/cancelled
disposition may be recorded with `accepted: false`, but it never authorizes a
new attempt.

Required fields:

```json
{
  "execution_id": "exec_...",
  "session_id": "sess_...",
  "authority_correlation_id": "...",
  "root_ac_index": 0,
  "final_retry_attempt": 1,
  "accepted": true,
  "disposition": "accepted",
  "outcome": "succeeded",
  "terminal_status": "completed"
}
```

The durable uniqueness key is
`(authority_correlation_id, root_ac_index)`. The EventStore inserts the event
and its guard row in one transaction. A duplicate append is an idempotent
no-op only when the existing payload is byte-for-byte equivalent; a conflicting
payload is a persistence error and never becomes a second final decision.

No final acceptance event is emitted for a recoverable `PAUSED` transition.
The retained owner must resume the same authority generation and publish the
decision only after a later terminal outcome. A terminal cancellation or
failure records `accepted: false` with its explicit disposition so replay can
distinguish a rejected final gate from an unfinished attempt.

## Consumer rules

- Frugality proof pairs attempt-axis telemetry with the latest
  `execution.ac.acceptance_finalized` record, never with an intermediate
  `outcome_finalized` row.
- MCP recovery uses attempt judgments for diagnostic failure evidence and the
  final acceptance event for terminal disposition.
- Attention relay reports attempt verification from `attempt_judged` and final
  acceptance from `acceptance_finalized` as distinct subtypes.
- Checkpoint/replay and escalation correlation use `retry_attempt` from
  `attempt_judged`, and use the authority-keyed final event only to close an
  episode.
- Historical `execution.ac.outcome_finalized` rows remain readable as
  provisional attempt telemetry, so old runs cannot be reinterpreted as final
  acceptance.

## Invariants

1. Only the Final Gate emits `accepted: true`.
2. One authority generation and root AC have at most one durable final event.
3. A final event cannot be overwritten by a later attempt judgment.
4. A paused episode has no final event until it resumes and reaches a terminal
   disposition.
5. Duplicate or contradictory final payloads fail closed.

