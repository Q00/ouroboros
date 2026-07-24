# Routing B — provider-neutral route admission

## Status

Proposed implementation slice for Stack 2. This document defines the route
contract and the pure Admission Kernel. Provider dispatch, bounded escalation,
and acceptance remain later slices.

## Why this boundary exists

Routing must choose a complete execution route, not just a model tier. A route
is the provider-neutral tuple:

```text
model × harness × effort × persona × tool policy × authority identity
```

The configured cost is an explicit integer from route configuration. It is not
inferred from a provider name, a model label, or a hard-coded rule such as
"Haiku for easy work".

The authority split is deliberately narrow:

1. **Advisor** returns an ordered preference of route IDs. It may rank, but it
   cannot create a route, bypass a constraint, or authorize dispatch.
2. **Admission Kernel** validates the configured registry, applies hard
   constraints, and authorizes at most one eligible route.
3. **Final Gate** remains the only authority that can accept an AC. Admission
   does not imply execution success or acceptance.

## Contract

`RouteRegistry` is versioned and contains immutable `RouteCandidate` values.
Each candidate has:

- `route_id`: stable, bounded identifier;
- `model`, `harness`, and optional `effort`;
- `cost_units`: non-negative configured relative cost;
- `persona`, `tool_policy`, and `authority_identity`: explicit route identity
  dimensions, never inferred from a provider name;
- `capabilities`: bounded unique capability tokens;
- `enabled`: configuration kill switch;
- `ordinal`: stable configuration order for the final deterministic tie-break.

The serialized contract is intentionally strict: unknown fields, unsupported
versions, duplicate route IDs, malformed tokens, and an empty registry fail
closed before any provider boundary is entered.

`RouteRequirements` carries hard constraints:

- required capabilities;
- allowed harnesses;
- required effort;
- optional pinned route, model, harness, persona, tool policy, or authority
  identity.

Pins and capabilities are constraints, not suggestions. If no configured route
satisfies them, the result is `blocked` and contains no selected route.

## Deterministic admission

The Kernel evaluates candidates in registry order and records stable rejection
codes. Eligible routes are sorted by:

```text
cost_units → Advisor rank (equal-cost ties only) → ordinal → route_id
```

An unknown or repeated Advisor ID is ignored. If the ranking itself is
malformed or exceeds its bound, the complete ranking is discarded and the
Kernel uses its non-Advisor deterministic order; advisory input can therefore
never veto admission. An Advisor cannot make an expensive route win over a
cheaper eligible route, and cannot dispatch a route absent from the registry.
Repeating the same registry, requirements, and Advisor order produces
byte-equivalent contract data.

Registry candidates and capability lists are bounded before nested parsing, and
streaming ordered inputs stop at the first item beyond their bound. Unordered
collections are rejected rather than serialized in process-dependent order.

The returned `RouteAdmission` is an authorization boundary for a later
executor: only `selected` on an `admitted` result may be passed to dispatch.
The module deliberately has no provider calls, retry/escalation policy, or
Final Gate behavior.

## Next slices

1. Wire this contract into the existing live model/harness routing path while
   preserving current behavior behind an explicit compatibility adapter.
2. Add Routing C observations and bounded escalation. Escalation may choose the
   next configured route only after a classified failure and a finite budget.
3. Emit the route fingerprint into the frugality proof and shared projection.
