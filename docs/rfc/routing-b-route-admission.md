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
  dimensions, never inferred from a provider name. Authority identities use a
  small allowlisted stable-descriptor grammar (for example `runtime:claude` or
  `session-a`). A descriptor has one typed namespace and at most four
  explicitly registered non-secret label segments (or a bounded numeric
  ordinal). Unknown labels, opaque values, and credential-shaped values are
  rejected before serialization;

The route contract uses the shared `core.security.is_stable_authority_identity`
boundary. The process-local execution-authority module retains its separate
recursive sanitizer for nested authority payloads; it is broader by design and
does not make free-form route identities admissible.
- `capabilities`: bounded unique capability tokens;
- `enabled`: configuration kill switch;
- `ordinal`: stable configuration order for the final deterministic tie-break.

Cost and ordinal integers have finite bounds so the contract remains safe for
ordinary JSON serialization and future fingerprints, including hostile numeric
inputs.

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
malformed, raises while iterating, or exceeds its bound, the complete ranking is
discarded after consuming at most `MAX_ADVISOR_ORDER + 1` values and the Kernel
uses its non-Advisor deterministic order; advisory input can therefore never
veto admission. An Advisor cannot make an expensive route win over a cheaper
eligible route, and cannot dispatch a route absent from the registry.
Repeating the same registry, requirements, and Advisor order produces
byte-equivalent contract data.

Registry candidates and capability lists are bounded before nested parsing, and
streaming ordered inputs stop at the first item beyond their bound. Unordered
collections are rejected rather than serialized in process-dependent order.

The returned `RouteAdmission` is a deterministic result value, not a
self-authenticating capability. It validates disposition, selected-route
membership, eligible/rejected-set coherence, and bounded ordered collections,
but Python object/closure introspection can still manufacture an object-shaped
value. Every effect boundary must call
`validate_admission(registry, requirements, admission, advisor_order=original_order)`
against the live registry, exact requirements, and the same normalized Advisor
order used for the original admission;
only that revalidated `selected` route may enter dispatch. A serialized route
contract is configuration evidence only and must likewise be rebuilt and
compared before side effects. Revalidation compares the complete selected
candidate semantics, not only `route_id`, so a registry replacement that keeps
an ID but changes model, harness, effort, cost, persona, tools, authority, or
capabilities cannot reuse a stale admission. The module deliberately has no
provider calls, retry/escalation policy, or Final Gate behavior.

## Next slices

1. Wire this contract into the existing live model/harness routing path while
   preserving current behavior behind the explicit compatibility adapter in
   the next stacked routing layer.
2. Add bounded observations and escalation in the subsequent stacked routing
   layer. Escalation may choose the next configured route only after a
   classified failure and a finite budget.
3. Emit the route fingerprint into the frugality proof and shared projection.
