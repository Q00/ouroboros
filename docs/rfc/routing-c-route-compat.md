# RFC — Routing C compatibility projection

## Status

Implemented as the next Routing slice after the provider-neutral Admission
Kernel. This slice is a compatibility adapter: it makes the existing model and
effort decisions consume the Route B contract without introducing a second
dispatch authority.

## Boundary

The existing `ModelRouter` remains responsible for selecting a model tier, and
the effort router remains responsible for selecting reasoning effort. The
adapter snapshots those decisions into a complete `RouteRegistry` and sends
the current decision through the Admission Kernel:

```text
economics + ModelRouter snapshot
        │
        ▼
RouteCompatProjection (immutable contract evidence)
        │  pins model, harness, effort, persona, tools, authority
        ▼
Admission Kernel
        │
        ├─ blocked → no provider call
        └─ admitted → effect-boundary revalidation → provider dispatch
```

The projection is configuration evidence, not a new authority. `RouteAdmission`
is still the only dispatch authorization, and `Final Gate` remains the only
acceptance authority.

## Invariants

1. The configured economics catalog is the source of route model and cost. A
   mutable `ModelRouter.tier_models` map must exactly match the normalized
   catalog for the active runtime backend.
2. A model/effort decision is pinned to the complete route tuple. Retaining a
   tier name while changing its model, backend, effort, cost, or authority
   cannot pass admission.
3. Missing projections, unknown tiers, malformed capabilities, backend drift,
   catalog drift, and unsupported route metadata produce a Kernel `blocked`
   result or an empty model override. They never fall back to provider defaults.
4. Persisted projections are parsed with bounded, deterministic containers and
   compared with a freshly rebuilt projection whose effort, persona, tool
   policy, authority, and capabilities come from current defaults rather than
   the persisted payload. An enabled restored router requires an enabled
   projection; a dormant projection can never authorize it.
5. The provider call receives a model override only after the admission has
   been revalidated against the same projection and exact requirements. This
   applies to parallel ACs, single-AC direct execution, and direct resume.

## Persistence and resume

The resolved model-routing contract carries a versioned `route_compat` payload.
An explicit dormant contract remains distinguishable from malformed data. A
syntactically valid payload is not authorization: resume reconstructs the
registry and rejects it when current economics, backend, or route metadata no
longer match. Enabled-router/missing-projection, enabled-router/dormant-
projection, and dormant-router/enabled-projection combinations all fail closed.

## Scope and non-goals

This slice wires the compatibility boundary into the parallel AC executor and
both direct runner provider boundaries, as well as the runner's persisted
execution/resume contract. It does not add provider calls,
retry/escalation policy, route-failure classification, persistence of new
execution outcomes, or Final Gate acceptance. Those remain the next routing
slice and must consume only revalidated Kernel output.

## Verification

Focused coverage proves catalog and cost drift, unordered/oversized inputs,
tampered persisted projections, supported backend authority identities,
dormant-resume rejection, blocked-before-provider behavior, and effect-boundary
model-override revalidation. The adapter remains opt-in at the low-level
executor constructor so existing embedders without economics retain their
current behavior.
