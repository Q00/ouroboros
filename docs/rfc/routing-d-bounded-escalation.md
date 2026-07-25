# Routing D — live bounded escalation

## Status

Implemented on top of the Routing B Admission Kernel and the Routing C live
compatibility projection. Routing D owns the finite recovery loop for eligible
top-level atomic AC execution in both the parallel executor and the direct
runner. It does not own decomposition trust, cross-run memory, or final
acceptance.

## Authority split

Routing D preserves three non-overlapping authorities:

```text
Advisor                 Admission Kernel                 Final Gate
rank equal-cost hints   authorize each provider effect  declare AC acceptance
          │                         │                           ▲
          └──────────────► cheapest eligible route ─────────────┘
                                      │
                              provisional result
                                      │
                            classified route failure
                                      │
                         one next eligible route or BLOCKED
```

- An Advisor can rank candidates but cannot dispatch or accept work.
- The Admission Kernel selects the cheapest eligible initial route. Every
  escalated route is exactly pinned and revalidated against a freshly rebuilt
  live registry immediately before the provider call.
- A successful route attempt is recorded only as `attempt_succeeded`. It is not
  acceptance. The existing terminal Final Gate remains the only path that can
  durably finalize an AC as accepted.

Routing D activates only when the live provider declares native per-call model
override support. An advised-only runtime cannot prove that the admitted model
will be executed, so it remains on the pre-existing routing contract rather than
opening a bounded episode under a false cheapest-route claim. Once a Routing D
episode is active, loss or drift of native enforcement fails closed before the
next provider effect.

## Finite route algorithm

For one route episode:

1. Build the live compatibility registry and Admission Kernel requirements.
2. Admit the cheapest eligible unattempted route.
3. Rebuild and revalidate the exact admission at the provider boundary.
4. Execute the route once.
5. Persist the provisional attempt judgment.
6. Classify a failed result and persist its `RouteObservation` plus the
   deterministic escalation decision.
7. Only after both durable writes succeed, execute exactly the next eligible
   route.
8. Stop on provisional success, a `BLOCKED` failure, or route exhaustion.

Every classified non-`BLOCKED` failure advances exactly one route in Kernel
order. Routing D never retries the same route, skips an eligible route, loops
back to an attempted route, or lets legacy retry-count, stall, bounce, or
alternate-harness recovery preempt its finite route set.

| Attempt result | Routing D action |
| --- | --- |
| provisional success | persist `attempt_succeeded`; defer acceptance to Final Gate |
| classified non-`BLOCKED` failure with a successor | persist decision; advance one route |
| classified `BLOCKED` failure | explicit `BLOCKED`; human handoff |
| no remaining eligible route | explicit `BLOCKED` with `routes_exhausted`; human handoff |
| missing/stale/malformed admission state | fail closed; no next provider effect |

The maximum number of effects in an episode is the bounded registry size. A
route ID must occur at most once, so infinite retries are structurally
impossible.

## Durable `RouteObservation`

`execution.ac.route_observed` stores a versioned, bounded observation containing:

- episode and attempt identity;
- route ID, model, harness, effort, configured cost, and capabilities;
- provisional verifier outcome and classified failure;
- stable escalation reason;
- a deterministic next-route or terminal decision;
- `final_acceptance_declared: false`.

It deliberately excludes credentials, provider output, arbitrary verifier
prose, and the authority identity. Configured `cost_units` are canonical decimal
contract values and can exceed JSON's safe integer range without losing
precision.

The required parallel AC persistence order is:

```text
provider result
    └─► execution.ac.attempt_judged
            └─► execution.ac.route_observed
                    └─► next provider effect
```

If either required write fails, no successor route may execute. The direct
runner has no root-AC attempt record for its whole-Seed call, so its hard
boundary is the durable route observation itself; that write must complete
before a fresh successor session can start.

## Resume and drift rules

Parallel resume does not trust a persisted `selected_route_id` by itself. It:

1. validates execution, session, root AC, semantic AC, episode, schema, and
   contiguous attempt indices;
2. rebuilds the current compatible registry using the observed effort;
3. requires the observed model, harness, effort, cost, and capabilities to
   equal the live candidate snapshot;
4. strictly parses the persisted decision against that registry;
5. recomputes `advance_route()` from the complete attempted-route prefix; and
6. requires the recomputed and persisted decisions to be identical.

Unknown fields, removed routes, model/cost/config drift, duplicate or gapped
indices, a broken successor chain, or an observation claiming Final Gate
authority all stop replay before another provider effect.

A provisional success observed before interruption is not promoted to
acceptance and is not replayed. It becomes a human handoff because the provider
effect happened but terminal Final Gate acceptance is not durable.

The direct runner uses a fresh provider session whenever the route changes. A
direct route with a durable success, escalation, or `BLOCKED` observation is
sealed against session replay; an old or exhausted route cannot be executed
again through resume.

## Parallel and direct scope

Cheapest-first bounded routing applies to top-level atomic ACs. Decomposed
children remain on legacy child routing until [#1466](https://github.com/Q00/ouroboros/issues/1466)
provides live Verified-MECE trust. Cheapening untrusted children before that
slice would let decomposition output expand dispatch authority prematurely.

After #1466, explicitly trusted children can enter the same Admission Kernel and
bounded escalation loop without changing the authority model.

## Seed/result and spend semantics

Routing determinism does not mean generated text must exactly equal text
predicted in a Seed. A Seed defines executable acceptance contracts; the Final
Gate evaluates the resulting evidence against those contracts. Different model
wording or implementation details can pass when the same contract is proven.

`RouteObservation.cost_units` is configured route cost, not measured token or
currency spend. Routing D makes route choice and attribution identity durable,
but actual per-stage token/spend attribution and guardrails remain the scope of
[#1396](https://github.com/Q00/ouroboros/issues/1396). Cohort and baseline proof
remain the scope of [#1470](https://github.com/Q00/ouroboros/issues/1470).

## Human attention

Route escalation emits proactive attention metadata. Route exhaustion produces
an attention relay and `execution.ac.recovery_exhausted` with
`human_handoff_required: true`. The engine closes its recovery ownership rather
than silently retrying while the host is asked to inspect or intervene.

## Deferred roadmap

- Verified-MECE child trust and live decomposition: #1466
- shared cross-run projection: #1389
- cross-run advisory memory
- actual token/spend attribution and guardrails: #1396
- frugality cohort/baseline proof: #1470

## Non-goals

- exact generated-output equality with the Seed;
- same-route retries or unbounded recovery;
- Advisor dispatch or acceptance authority;
- child-route cheapening before Verified-MECE trust;
- cross-run route learning or spend claims;
- implicit fallback when durable or live contracts disagree.
