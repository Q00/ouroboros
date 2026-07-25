# Routing D — bounded escalation and route observations

## Status

Implemented as a pure policy slice on top of the Route B admission contract and
the Route C compatibility projection. It is intentionally not a provider
dispatch implementation.

## Authority boundary

The slice adds one deterministic transition after a classified attempt failure:

```text
attempt result + RouteObservation
                 │
                 ▼
        bounded escalation policy
          ├─ retry same route
          ├─ next eligible route
          ├─ redispatch for decomposition
          └─ explicit BLOCKED / human handoff
```

The Advisor can still affect only equal-cost ordering. `advance_route()` calls
the Admission Kernel against the live registry and filters already-attempted
route IDs. It does not return an authorization capability: the effect owner
must re-admit the selected route immediately before a provider call. Final Gate
acceptance remains outside this module.

## RouteObservation contract

`RouteObservation` is raw, authority-bound telemetry. It records the episode,
attempt ordinal, selected route dimensions, configured cost, required and
available capabilities, capability-match result, verifier outcome, failure
taxonomy, and a stable escalation reason. It deliberately excludes
`authority_identity`, credentials, provider responses, and arbitrary verifier
prose. The versioned parser rejects unknown fields, unordered collections,
duplicates, invalid taxonomy values, and inconsistent capability claims.

An accepted observation cannot carry failure metadata. A failed observation must
carry a known `FailureClass`; a blocked verifier outcome must use the
`BLOCKED` class. These invariants keep replay and projections deterministic.

## Bounded episode semantics

The ordered `attempted_route_ids` history is bounded by the registry limit and
must contain unique, known routes. A resumed episode whose current route is not
the last history entry is rejected rather than silently repeating an effect.
When the failure policy permits model escalation, the next route is selected
from the remaining eligible routes in Kernel order. Once that set is exhausted,
the decision is `BLOCKED` with `routes_exhausted`; no infinite retry loop is
possible.

Failure classes retain their existing recovery meanings:

| Failure class | Route-D action |
| --- | --- |
| `EVIDENCE_MISSING`, `EVIDENCE_FORM_MISMATCH` | Retry the same route |
| `FABRICATION_SUSPECTED` | Escalate to the next eligible route |
| `SCOPE_CREEP`, `STALL` | Redispatch for decomposition |
| `BLOCKED` | Human handoff / explicit `BLOCKED` |

The module does not persist events itself. A later event-writer integration may
persist the versioned observation and decision after the owning authority has
committed the attempt outcome.

## Non-goals

- no provider call or retry execution;
- no mutation of `ModelRouter` or the economics catalog;
- no cross-run memory or trust reuse;
- no Final Gate acceptance;
- no implicit fallback when a route or episode contract is malformed.

