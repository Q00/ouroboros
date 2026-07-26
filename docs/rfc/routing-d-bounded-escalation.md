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

The completion stream has one producer slot per admitted root AC. Both the
pre-dispatch detector and replay loader derive their max-plus-one query
sentinel from `len(seed.acceptance_criteria)`, so every valid root can own one
terminal composite even when the Seed contains more than 4,096 criteria. A
population above that Seed-derived limit is necessarily duplicate or foreign
completion authority and fails closed.

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

Pause history has no producer-side event-count ceiling: every recoverable quota
window can append a new direct, parallel, or composite snapshot. Replay therefore
freezes the newest `(timestamp, event_id)` as a high-water boundary and folds the
complete snapshot through a deterministic oldest-first keyset cursor. The fixed
page size bounds memory only; it is never interpreted as a valid-history bound.
Equal timestamps use the event ID tie-breaker, and appends beyond the frozen
high-water key cannot move or extend the replay population. Direct and parallel
repeated pauses on the same unconsumed route replace the provider boundary while
every superseded envelope remains schema- and route-history-validated. Composite
replay applies the same population-total paging before its monotonic frame/path
checks. Thus a 65th route pause or 4,097th composite pause is as replayable as the
first without permitting an unbounded in-memory query.

Quota classification runs immediately after each provider turn and before any
queued SessionSignal follow-up. A quota-ending turn therefore performs no later
provider effect, retains its exact resumable handle, and leaves queued signals to
be rejected at target teardown. Non-finite retry hints are ignored by both direct
and shared pause classifiers rather than being passed into integer rounding.
Classification and pause construction consume one provider-neutral duration
parser for seconds, milliseconds, relative durations, and absolute
`resume_after`/`reset_at` timestamps, so a supported encoding cannot pause one
owner while escalating in another.
Finite but unrepresentably large provider retry hints fall back to the validated
operator pause window before constructing the durable resume timestamp.
The operator fallback is resolved before the first provider effect, bounded to
`1..31,536,000` seconds (one year), and stored in execution contract v9. Direct
and parallel pause construction consume that exact integer after a provider
turn; they never reread environment or YAML at the recovery boundary.

Within one Routing D batch, quota ownership propagates immediately through a
shared pause signal. Semaphore-waiting siblings recheck the signal under their
permit before provider entry, already-running sibling scopes are cancelled, and
the completed result scan recognizes the real quota owner before any decomposed
legacy recovery can dispatch. Only a sibling stopped before execution-authority
entry remains pending and produces no failure judgment, completed stage,
checkpoint, coordinator effect, or successor route. A sibling cancelled after
entry has crossed an uncertain provider-effect boundary: its dispatch is sealed
and `execution.ac.uncertain_handoff_required` makes the root durably `BLOCKED`
for human ownership. It is never relabeled pending or replayed. An interruption
without a matching quota result is an internal inconsistency and fails closed.

An atomic parallel `execution.ac.route_paused` envelope seals every input that
can change its capsule: retry index and prompt, the original sibling population,
route override, the original expected-route value (including `null` on the
first route), runtime scope, dispatch ID, and capsule fingerprint. Replay folds
repeated pauses chronologically despite newest-first storage and reconnects only
through the latest unconsumed provider boundary. Missing, malformed, drifted, or
configuration-only handles fail before provider entry.

The durable parallel resume-owner marker is published only when Routing D is
actually effect-capable for the run. Legacy parallel execution does not have
complete completed-stage replay without a checkpoint, so it cannot advertise the
stronger Routing D owner contract or redirect resume through that state machine.
The owner decision and executor are bound to the same pre-await capability/config
snapshot, preventing cancellation checks from opening a drift window between them.
The owner marker itself is durable Routing D evidence even when a crash occurs
before the first route event. Resume therefore fails before dependency analysis or
provider entry if native model enforcement, routing configuration, or durable-depth
eligibility is no longer available.

Execution contract version 9 also seals the complete scalar executor semantics
used by that owner: verification enablement and timeout, retry and cross-harness
budgets, decomposition enablement/mode/depth, requested and backend-capped effective
worker counts, backend concurrency/rate limits, adapter pacing ownership,
fat-harness acceptance, shadow replay, checkpoint/signal capability presence, and
the resolved context-pack mode that controls provider system prompts. Version 9
retains the bounded usage-limit pause seconds and adds the complete runtime
capability declaration: resume targeting, structured output, parameter support,
the enforceable reasoning-effort vocabulary, model override support, subagent
mode, and session-signal capabilities. This exact-schema population is checked
again at each direct and parallel provider choke point, so capability drift
between resume validation and dispatch also fails closed.
The sub-contract has its own fingerprint. Resume rejects any current-setting
drift before constructing prompts or `ParallelACExecutor`. Prompt construction,
fan-out, rate pacing, and pause publication consume the immutable persisted
snapshot instead of rereading mutable environment or config.

Version 9 retains the v8 complete provider-input population and freezes the
resolved execution strategy: its system-prompt
fragment, task suffix, base tools, and activity map. After session-scoped MCP
discovery, the complete canonical tool catalog and the policy-allowed tool list
are fingerprinted and persisted before the first provider effect. Resume rebuilds
prompts from that frozen strategy and requires the current handler catalog to be
byte-equivalent before re-entering either the direct or parallel owner; it never
falls back to the task-type registry or overwrites a persisted runtime catalog
with broader current authority. A resumed direct route that fails also builds its
fresh successor prompt from this same persisted strategy. The exact rendered
context-pack fragment, complete
canonical `ExecutionProfile`, and persisted inherited `RuntimeHandle` are frozen in
the same input fingerprint before the session is published. New and resumed direct
or parallel execution consume those snapshots without rescanning a changed
workspace, reloading profile YAML, or adopting a different parent conversation.
Contractless sessions and versions 2 through 8 cannot reconstruct the complete v9
effect population and therefore fail closed on resume; every new Routing D owner
is born with version 9. Version 9 itself has one exact top-level schema:
`version`, `foundation_a_authority`, `execution_preferences`,
`execution_semantics`, `execution_inputs`, `model_routing`, `frugality_proof`,
`guidance`, and `resume`. Resume rejects every missing or unknown top-level member
before restoring guidance or preferences and before analyzer or provider effects;
no current-format field is synthesized from runtime defaults.
The prepare-to-execute boundary also stops trusting the caller-owned tracker
copy. Only after initial progress is durably published, the process-local
authority seals that exact prepared contract as canonical JSON. Execution must
claim the same opaque authority and equal the seal before the trusted snapshot
is released, then applies the same complete contract restoration with unbound
tool-catalog state admitted only during this pre-provider phase. Seed, routing,
semantics, preferences, guidance, workspace, and every nested input fingerprint
are authenticated before prompt construction or provider entry.
Live-only runtimes may preserve the builder's explicit unobservable runtime or
workspace states at this first boundary because the opaque generation and seal
still bind the exact process; durable resume continues to reject those states.
The prepared tracker is only a contract receipt, never lifecycle authority.
Every precreated dispatch reconstructs event-sourced session status before the
process-local claim and again immediately after a successful claim. Only
`RUNNING` at both observations may reach contract authentication or tool setup.
`PAUSED` is rejected without retiring the retained owner and can continue only
through `resume_session`; terminal status retires local authority, and an
unreadable observation is retryable with zero provider effects. The second read
closes the race where another execution publishes `PAUSED` between the first
observation and claim release. A retained persistence-pending lifecycle intent
also outranks a durable `RUNNING` snapshot at this same ingress: it is replayed
before the normal prepared claim, so a failed pause or terminal publication
cannot repeat the provider effect that produced it.

The historical decomposition input contract remains any non-negative integer
across CLI, environment, Seed, runner, and executor boundaries. Routing D adds a
separate durable subset at depths `0..4`. At the maximum five-way branching
factor this subset contains exactly 780 child nodes
(`5 + 25 + 125 + 625`), and its completion/pause envelope is derived from that
same boundary. Values above `4` are not rejected or silently clamped: they run
through the established legacy parallel path, with no Routing D route switching
or parallel resume-owner claim. This preserves existing execution behavior while
making the stronger crash-replay guarantee explicit and version-local.

The durable conflict representation is also population-safe at depth four.
Each result node seals its exact finite local file projection; the coordinator
walks the full result tree for both live and replayed execution. Coordinator
started/completed producers serialize the complete conflict population admitted
from those results, and replay uses the exact current stage population as its
row, path, and per-row AC-index bound. It does not impose a smaller fixed
post-effect cap on any of those provider-derived fields. A complete five-way
tree therefore does not flatten 625 leaf paths into a 512-entry parent field,
conflict sets or writer populations above 4,096 remain serializable, and nested
writes have identical conflict semantics before and after resume.

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
route. The event and session `PAUSED` transition are published only after the
provider exposes an exact nonterminal resume ID and that complete handle is
durably stored in session progress. This rule also applies to a paused successor
during resume. Once `PAUSED` wins, or its durable publication is explicitly
pending, the execution coroutine transfers cleanup ownership and must not invoke
the handle terminator; only a terminal lifecycle winner may destroy that provider
session. Parallel and direct paths share the same resumable predicate, so a
terminal lifecycle event cannot be persisted as a recoverable pause even when it
still carries a session ID. A handle-less, terminal, or unpersisted quota boundary emits no
route-pause event, makes no second fresh provider call, and reaches the Final
Gate as `outcome=blocked`, `disposition=blocked` with human handoff. Any
same-session route evidence with a missing or non-runner call site blocks direct
replay.

Hard provider preconditions share one direct/parallel classifier. Typed error
labels are canonicalized across prose, CamelCase, snake_case, kebab-case, and
dotted machine identifiers; numeric HTTP authorization statuses `401` and `403`
are admitted only from status/code fields. Missing access, tools, credentials,
configuration, or authentication produces one `BLOCKED` observation and immediate
human handoff, never a costlier successor.
Provider metadata traversal is bounded at both classification boundaries. Quota
metadata is projected through a closed key vocabulary without iterating provider
mappings; any mapping-protocol failure or population overflow pauses rather than
authorizing a successor. Hard-precondition traversal uses per-mapping and total
mapping sentinels, and converts iterator failures, oversized keys/text, or excess
population into conservative `BLOCKED` handoff.

Both owners compare a paused candidate with the complete predecessor
`selected_route` snapshot, or with the exact live initial admission when no
observation exists. A same-ID change to effort or any other candidate semantic
is configuration drift, not a resumable pause. Pre-dispatch detection and the
full replay loaders use population-matched bounds. Terminal and attempt streams
use finite max-plus-one sentinels derived from their producer domains. Repeating
pause streams use one-row presence probes plus the stable high-water/keyset
replay above, so no Routing D history scan uses an unbounded query or a fixed
total cap smaller than its producer domain.

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
