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
- The Admission Kernel selects the cheapest eligible initial route at or above
  the run's configured `base_model_tier`. Routing D may save cost within that
  public starting-tier contract; it cannot silently weaken the contract itself.
  Every escalated route is exactly pinned and revalidated against a freshly
  rebuilt live registry immediately before the provider call.
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
2. Disable candidates below the configured starting-tier floor, then admit the
   cheapest eligible unattempted route.
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
- a deterministic next-route or terminal decision, including the complete
  effect-relevant successor candidate snapshot;
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

Parallel resume does not trust a persisted route ID by itself. The durable
decision carries the complete successor candidate and replay:

1. validates execution, session, root AC, semantic AC, episode, schema, and
   contiguous attempt indices;
2. rebuilds the current compatible registry using the observed effort;
3. requires the observed model, harness, effort, cost, and capabilities to
   equal the live candidate snapshot;
4. strictly parses the persisted successor snapshot and requires every model,
   harness, effort, cost, persona, tool-policy, authority, capability, enabled,
   and ordinal field to equal the same-ID live candidate;
5. recomputes `advance_route()` from the complete attempted-route prefix; and
6. requires the recomputed and persisted decisions to be identical.

Unknown fields, removed routes, model/cost/config drift, duplicate or gapped
indices, a broken successor chain, or an observation claiming Final Gate
authority all stop replay before another provider effect.

The route-aware `execution.ac.attempt_judged` row carries the same episode ID,
attempt index, route ID, root AC, execution, session, and parallel call-site
identity. Replay reconciles it with `execution.ac.route_observed`; a judgment
left unmatched by a crash is a completed-effect ambiguity and fails closed.
Legacy judgments without the Routing D marker remain unrelated telemetry.

A provisional success observed before interruption is not promoted to
acceptance and its provider effect is not replayed. The durable observation
also seals the canonical bounded `ACContextSummary`, recursive file-conflict
projection, and structured verify-gate outcome required to re-enter the
interrupted stage. Resume consumes that projection directly rather than
reconstructing synthetic provider messages, so file ordering, total file count,
public-API context, and coordinator conflict inputs remain identical. The
restored provisional result still enters the normal Final Gate; malformed or
missing cached evidence fails closed before another provider effect.

A terminal legacy composite that shares an interrupted stage is sealed in an
exact-schema `execution.ac.composite_completed` event before the stage can
return paused. The event binds execution/session/semantic AC identity, the
canonical context and conflict projections, verify evidence, terminal outcome,
the bounded child-result tree used by reporting, and the canonical decomposition
decision plus its fingerprint. Resume restores the completed composite and
excludes it from dispatch. Duplicate, conflicting, drifted, oversized, or
non-canonical composite evidence fails closed instead of repeating decomposition,
child provider calls, or tool effects.

A quota pause inside a legacy composite is sealed separately as an exact-schema
`execution.ac.composite_paused` event. Its versioned frame list records every
composite on the root-to-leaf path, each completed sibling prefix, and each
immutable decomposition decision/fingerprint; one leaf record binds the final
node, retry index, runtime scope, dispatch ID, and capsule fingerprint. Replay
folds these events chronologically even though the store returns newest-first,
restores every frame, and resumes only the newest exact leaf boundary. Advancing
or repeated pauses therefore preserve already completed effects, while regressed,
conflicting, oversized, or malformed frame histories fail before provider entry.
A newer pause may shorten an established descendant path only when an ancestor
has advanced to a later child and thereby consumed that subtree; simply dropping
the nested frame is rejected as replay regression.

Quota classification runs immediately after each provider turn and before any
queued SessionSignal follow-up. A quota-ending turn therefore performs no later
provider effect, retains its exact resumable handle, and leaves queued signals to
be rejected at target teardown. Non-finite retry hints are ignored by both direct
and shared pause classifiers rather than being passed into integer rounding.
Finite but unrepresentably large provider retry hints fall back to the validated
operator pause window before constructing the durable resume timestamp.

The durable parallel resume-owner marker is published only when Routing D is
actually effect-capable for the run. Legacy parallel execution does not have
complete completed-stage replay without a checkpoint, so it cannot advertise the
stronger Routing D owner contract or redirect resume through that state machine.
The owner decision and executor are bound to the same pre-await capability/config
snapshot, preventing cancellation checks from opening a drift window between them.

Live decomposition depth is admitted only in the inclusive range 0-2. At the
maximum five-way branching factor this yields at most 30 persisted child nodes
(`5 + 25`), within the fixed 64-node completion/pause replay envelope. Larger or
non-integer depths are rejected during runner/executor construction, before any
provider effect; restored split decisions cannot cross the same depth boundary.

The direct runner uses a fresh provider session whenever the route changes. A
direct route with a durable success, escalation, or `BLOCKED` observation is
sealed against session replay; an old or exhausted route cannot be executed
again through resume.

Cancellation is not a route failure and exits before observation or successor
selection. A recoverable usage/quota limit is also detected before route
classification: the parallel path preserves the raw failed result so the runner
can mark the session `PAUSED`, and emits neither a route judgment nor a terminal
route observation that could authorize escalation. The direct path additionally
retains the current route and provider handle. Its
`execution.ac.route_paused` event durably binds the full current candidate,
attempt index, and prior route prefix. Resume validates that snapshot against
the live registry and either the cheapest initial admission or the exact last
escalation decision, then resumes the same provider handle with that exact
route. Any same-session route evidence with a missing or non-runner call site
blocks direct replay.

Both owners compare a paused candidate with the complete predecessor
`selected_route` snapshot, or with the exact live initial admission when no
observation exists. A same-ID change to effort or any other candidate semantic
is configuration drift, not a resumable pause. Pre-dispatch detection and the
full replay loaders use finite max-plus-one stream sentinels; no Routing D
history scan uses an unbounded query.

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
