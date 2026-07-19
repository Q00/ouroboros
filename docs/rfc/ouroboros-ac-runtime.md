# RFC — Ouroboros Runtime: an Acceptance-Criterion VM for AI Engineering

> Status: proposed architecture
> Tagline: **Compile intent. Page context. Prove work. Reuse trust.**

## Thesis

AI coding harnesses should be provider drivers, not the owners of a software
workflow. Claude, Codex, Gemini, OpenCode, and future models may execute work,
but Ouroboros owns the durable semantics:

- what the user meant;
- which independently verifiable outcome is being attempted;
- what context that attempt may see;
- which workspace and authority it may mutate;
- what evidence counts as success;
- when a cheaper model is safe;
- how a crash resumes without inventing state.

Ouroboros Runtime is therefore an acceptance-criterion virtual machine. It
compiles a clarified intent into an AC graph, executes every AC in a disposable
provider context, pages external context on demand, and accepts work only through
the existing deterministic gate/evidence system.

## The Product Loop

```text
GRILL -> COMPILE -> PAGE -> RUN -> PROVE -> LEARN
  ^                                         |
  +-----------------------------------------+
```

### 1. Grill — preserve reasoning coherence

The discovery phase follows the strongest lesson from `grill-me`: do not split
or compact a line of reasoning while the problem is still ambiguous. The model
and user stay in one coherent decision thread until the important branches,
terms, constraints, and fears are explicit.

The output is not a transcript summary. It is a durable **Intent Ledger**:

- domain vocabulary and resolved ambiguities;
- accepted constraints and rejected alternatives;
- open questions and their owners;
- success gates and evidence requirements;
- ADR/spec/issue references for settled detail.

If the discovery thread approaches its healthy reasoning window, Ouroboros
freezes the ledger and opens a fresh discovery session from references. It never
drags the full conversation forward merely because it exists.

### 2. Compile — turn intent into executable AC IR

The AC compiler transforms the Intent Ledger into a dependency graph of
tracer-bullet vertical slices. An AC is atomic because it has one independently
verifiable outcome and bounded blast radius, not because it has an arbitrary
token count.

Operationally, each leaf should be expected to fit one fresh model context. A
rough 100k-token smart-zone target is a planning heuristic. If a unit cannot fit
but still has only one acceptance gate, it remains one AC and may use a physical
continuation segment; if it contains multiple independently verifiable outcomes,
the compiler splits it before dispatch.

The compiled graph is the runtime's IR. Recovery restores this exact graph; it
does not ask another model to derive a potentially different plan and then apply
old outcomes to it.

### 3. Page — use context like virtual memory

RLM's most valuable idea is not “use more tokens” or “always call a small model.”
It is to keep the large environment outside the main prompt and let the runtime
retrieve or recursively process only the pieces needed for the current step.

Every AC receives a compact **AC Capsule**:

- semantic AC identity and acceptance contract;
- canonical workspace and dispatch authority;
- dependency/gate/artifact references;
- a small deterministic project map and verify commands;
- model, effort, retry, and isolation policy;
- a context-page budget.

The capsule contains references, not copies. Large sources remain in the
workspace, event ledger, specs, diffs, docs, and test output. The model can ask
for bounded **Context Pages** such as:

- `symbol:PaymentService.create`;
- `diff:dependency-AC-2`;
- `gate:AC-3/latest-failure`;
- `decision:auth-session-model`;
- `tests:affected-by/src/payment.py`.

Each page is bounded, provenance-carrying, content-hashed, and disposable. A
page may be produced deterministically or by a read-only **Probe**. Probe output
is advisory context, never acceptance evidence by itself.

### 4. Run — shed context at AC boundaries

Every AC attempt starts in a fresh provider session. This is the universal
cross-harness guarantee: it does not depend on Claude, Codex, or another driver
reporting token usage correctly.

The model shares no prior chat by default. It receives the capsule, works against
the authoritative workspace, pages additional context as needed, and emits
normalized tool/output events. Native provider resume is allowed only within the
same AC attempt after a crash; a handle can never cross an AC, retry, workspace,
or authority boundary.

When one genuinely atomic AC outgrows a physical session, Ouroboros **sheds** the
provider context:

1. persist a reference-first continuation capsule;
2. close and invalidate the native session;
3. start a fresh segment for the same AC and retry attempt;
4. preserve model/effort/persona/gate semantics;
5. continue from workspace and ledger state, not transcript replay.

Token telemetry may trigger shedding where available, but it is a fallback
signal. Billed work tokens are not claimed to equal resident context occupancy.

### 5. Prove — gates, not prose, create trust

Provider self-report is not acceptance. The runtime normalizes tool facts,
artifacts, command results, and typed evidence, then applies the existing AC gate.

Decomposition trust is gate-anchored:

- every child passes its child-local gate;
- the parent gate is re-run over the union of promoted child effects;
- missing evidence is indeterminate, never trustworthy;
- real negative evidence wins over missing evidence;
- the verdict is durable and authority-bound.

This is the central lesson from the #1648 experiment: proposal shape, model prose,
and apparent MECE structure cannot authorize a cheaper route. Only executed and
verified outcomes can.

### 6. Learn — make frugality an evidence feedback loop

Frugality is split into four independent levers:

1. **Attention frugality** — fresh contexts and bounded pages remove irrelevant
   history from the model's active reasoning surface.
2. **Retrieval frugality** — cheap read-only Probes summarize or index large
   material with citations; their output cannot mutate state or satisfy a gate.
3. **Model frugality** — trusted decomposed ACs may run one model tier cheaper
   while retaining reasoning depth; failures progressively regain stronger
   models and eventually lateral personas.
4. **Execution frugality** — durable workspace/gate/event state prevents repeated
   discovery and lets sessions be disposable.

The runtime can also support **speculative frugal execution** for workspace-pure
ACs:

1. run a cheaper model in an isolated overlay/worktree;
2. execute the child gate inside that isolation boundary;
3. promote the patch only when the gate passes and side-effect policy permits;
4. otherwise discard the overlay and rerun at the base tier;
5. after all promotions, re-run the parent gate before registering trust.

This creates first-run savings without letting an untrusted cheap model corrupt
the live workspace. It is disabled unless filesystem, verification, network, and
external side effects are all isolation-attested.

## Execution Units

| Unit | Purpose | Mutation | Trust semantics | Typical model |
|---|---|---:|---|---|
| Decision Thread | Grill ambiguity into an Intent Ledger | Docs/ledger only | User-confirmed decisions | strong conversational model |
| Context Page | Bounded deterministic retrieval | No | content hash + provenance | none |
| Probe | Recursive RLM-style inspection/summarization | No | advisory, cited, never gate evidence alone | frugal model |
| AC Capsule | Independently verifiable implementation slice | Yes | AC gate | base or trusted-frugal model |
| Continuation Capsule | Fresh physical session for the same AC | Yes | same gate/retry identity | unchanged |
| Verifier | Deterministic or isolated semantic judgment | No live mutation | authoritative gate outcome | code/process, optional strong model |

The Probe/AC distinction prevents a common decomposition failure: not every
question deserves a durable child AC. A Probe retrieves knowledge. An AC changes
the product and must own a gate.

## Runtime Planes

### Control plane

- Grill state machine and Intent Ledger
- AC compiler and dependency graph
- scheduler, retries, lateral escalation, parking
- trust registry and economics policy
- checkpoint/recovery reducer

### Memory plane

- shared or isolated workspace
- Seed/spec/ADR/issue references
- event and evidence ledger
- context-page cache keyed by source digest
- patch/overlay artifacts

### Execution plane

- AC Capsule materializer
- Context Page resolver and Probe runner
- provider drivers (`AgentRuntime`)
- native session lifecycle and capability negotiation

### Verification plane

- child-local success gates
- parent-gate revalidation
- typed evidence and TraceGuard
- decomposition attestation
- frugality proof and outcome finalization

## State Machine

```text
PLANNED
  -> CAPSULED
  -> DISPATCHED
  -> OBSERVED
  -> GATED
      -> ACCEPTED
      -> RETRY_SCHEDULED
      -> DECOMPOSED
      -> SHED_AND_CONTINUE
      -> ALT_DRIVER
      -> LATERAL_ESCALATION
      -> PARKED
      -> INFRA_FATAL
```

Every transition emits an event before the next authority-bearing effect. Crash
recovery folds events into state and resumes from the last complete transition.
It never infers progress from a provider transcript or re-derives an execution
plan against old outcomes.

## Provider Contract

All providers implement the same driver boundary. Capabilities affect
optimizations, not AC semantics:

| Capability | Effect |
|---|---|
| native model override | Can enforce trusted-frugal and escalation routing |
| live token usage | Can request an in-session shed at a safe boundary |
| terminal token usage | Can audit cost and shed incomplete work after a turn |
| targeted resume | May restore the same AC attempt after a crash |
| structured output | Improves evidence normalization |
| isolation attestation | May participate in speculative frugal execution |

A telemetry-free driver still receives a fresh session per AC and bounded
Ouroboros-injected context. It is merely unmeasured for token-triggered shedding.

## Lessons Incorporated

### From the Grill workflow

- Keep discovery coherent until questions are resolved.
- Convert conversation into durable decisions before splitting execution.
- Make implementation slices vertical, independently verifiable, and fresh.
- Handoffs reference settled artifacts instead of duplicating them.

### From #1648 and the review loop

- Trust false positives are unacceptable; ambiguity fails closed.
- First-round decomposition prose cannot authorize a discount.
- Reusable trust must bind workspace, Seed, tools, prompt, permissions, runtime,
  model routing, and execution profile.
- Retry, continuation, and decomposition are different state axes.
- Recovery contracts must be exact and durable; plan drift is a correctness bug.
- Provider capabilities must be truthful rather than assumed from a backend name.

### From the token-envelope experiment

- Token count is not semantic atomicity.
- Provider usage is a work/cost signal, not exact context occupancy.
- Universal behavior must come from Ouroboros-owned session boundaries.
- Live/terminal token rollover remains useful as an exceptional safety net.

### From RLM

- Treat large context as an external environment.
- Retrieve/process recursively instead of eagerly stuffing the main prompt.
- Keep sub-results bounded and provenance-carrying.
- Spend strong-model attention on planning, synthesis, and hard exceptions;
  delegate safe read-only work and proven slices economically.

## Implementation Strategy

1. **AC Capsule contract** — versioned capsule, typed references, fingerprint,
   prompt materialization, and durable compiled event.
2. **Session ownership** — assert fresh provider session per AC attempt and bind
   any same-attempt resume handle to the capsule fingerprint.
3. **Context paging** — promote `context_governor`, `context_pack`, and
   `LevelContext` into typed resolvers; stop eager prose injection.
4. **Probe runtime** — read-only recursive queries with citations and no
   acceptance authority.
5. **Speculative frugal lane** — isolated cheap execution, gate, patch promotion,
   and parent revalidation for side-effect-safe ACs.
6. **Shed fallback** — capability-driven live/terminal measurement and durable
   same-AC continuation.
7. **Kernel extraction** — move transition/recovery logic out of the monolithic
   executor into a pure event-folded state machine with effect handlers at the
   edges.

The first PR should land only steps 1 and 2 as a complete vertical slice. The
remaining steps form a stack; none should be hidden inside a single giant runtime
rewrite.

