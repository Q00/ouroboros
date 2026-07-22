# AC Runtime Execution Authority Boundary

## Status

Proposed Foundation A replacement for the closed implementation PRs #1682,
#1704, and #1705. This document deliberately selects the conservative first
boundary: every currently supported CLI runtime is executable **only within
its creating process**. No existing CLI runtime can claim portable execution
identity until it is rebuilt around the sealed kernel described below.

Foundation A is identity-only. It does not authorize result reuse, checkpoint
reuse, trust reuse, dispatch, routing, acceptance, or cross-run learning.

## Problem

The prior designs tried to promote `CodexCliRuntime` to portable identity by
binding its public `execute_task` method and an expanding set of configuration
fields. That is not a finite effect boundary: the unchanged public method
dynamically resolves `_execute_task_impl`, command builders, event handlers,
local skill/MCP handlers, process hooks, and caches. Binding each newly found
helper would turn Foundation A back into an unbounded inspection of a Python
object graph.

Consequently, an unchanged public `execute_task` could dispatch a changed
post-construction helper while the contract was still marked portable. A
portable claim made under those conditions is false and must not be an input to
later reuse or acceptance decisions.

## Foundation A contract

`ExecutionAuthorityContract` is a canonical, digest-only snapshot with these
components:

| Component | Portable treatment | Process-local treatment |
| --- | --- | --- |
| Executor policy | Closed versioned declarative values owned by the executor | Invalid/unknown values add a live-instance nonce |
| Workspace | Canonical workspace identity; a future portable kernel must also bind an immutable workspace generation | Missing/unsafe identity or a missing future generation adds a nonce |
| Built-in verifier | Closed implementation version and declarative configuration | Custom, dynamic, or unsafe verifier adds a nonce |
| Runtime | **No legacy CLI runtime is portable in Foundation A** | Runtime type and all runtime execution behavior are scoped to one live instance |
| Prompt, AC, tool list, model override, effort, session/handle, checkpoint | Excluded; attempt input | Excluded; Foundation C owns it |
| Event store, queues, locks, signal hubs, local handler caches, globals, closures, monkeypatches | Excluded | Live process state |

The canonical JSON contains only safe, finite declarative values or their
digests. It never recursively hashes callables, closures, descriptors,
globals, modules, environment maps, handler maps, cache contents, or opaque
objects. Credential-shaped values are never serialized; they force the owning
component to process-local.

Every dynamically or unsafely identified process-local component receives an
opaque random nonce once per **authority generation**.
`ExecutionAuthorityLiveBinding` retains that nonce for its same-instance
integrity rechecks; a new executor/runtime capture gets a new nonce. Two
instances may have similar visible configuration, but they cannot compare as a
portable authority or accidentally authorize cross-process reuse.

More precisely, a nonce is attached to each component whose own identity is
dynamic, unsafe, or otherwise not canonically observable. A valid declarative
workspace or policy descriptor need not receive a separate nonce merely because
another component makes the *whole* authority process-local. In Foundation A
every current runtime is such a component, so its live nonce makes the complete
authority generation process-local even when the workspace and policy retain
their finite declarative descriptors.

An authority generation additionally has a non-serializable live capability
held in the process-local registry by session id. The persisted event contract
may record `scope: "process_local"` and a correlation id, but never contains
that capability. Deserializing the correlation id in another process is
evidence only: it cannot recreate the capability or authorize an effect.

The registry alone mints generations and accepts registrations; it exposes no
public "register this correlation id" or caller-constructed capability path.
It records the exact minted object and its creating PID. After `fork`, the
child replaces the registry state and lock, so inherited memory cannot act as
a parent capability and cannot deadlock on a lock held by a vanished parent
thread. A child must create a fresh authority generation for a fresh attempt.

`portable_across_processes` is an identity-stability predicate only. It is
false for every current CLI runtime. There is no
`reusable_across_processes` alias: reuse is a later, separately authorized
decision in Foundation C and the Final Gate.

## Runtime rule

`CodexCliRuntime`, `CopilotCliRuntime`, `ZcodeCLIRuntime`, and every custom or
subclassed runtime remain fully executable. Their dynamic helpers, local skill
interception, MCP-handler caches, profile loaders, environment reads, launcher
chains, and resume recovery hooks are normal live-process behavior, not a
portable authority declaration.

Foundation A must not call a runtime's dynamic identity provider while
constructing a portable witness. A runtime may expose a descriptor for logging
or same-process diagnostics, but that descriptor cannot upgrade its stability.

The supported legacy runtime catalog is the factory's complete set: Codex,
OpenCode, Hermes, Gemini, Antigravity, Grok, Kiro, Copilot, Goose, Pi, GJC,
Zcode, and all custom/subclassed adapters. Tests enumerate that catalog rather
than relying on a prose list.

The executor may still capture its own fixed entry functions to prevent an
accidental internal dispatch through a replaced executor method. That is a
local integrity check, not a proof that an arbitrary runtime's whole Python
implementation is portable.

## Future portable runtime: sealed execution kernel

A future, separate proposal may admit one portable runtime only by introducing
an explicit `SealedExecutionKernel`. It must not wrap or call the legacy
runtime's dynamically resolved `self._...` helpers on the portable path.

Its finite data and collaborators must be:

1. an immutable `LaunchSpec` containing the canonical executable chain,
   working directory, allowed child-environment snapshot, permissions, fixed
   timeouts, and bounded process policy;
2. a fixed parser/normalizer implementation table whose functions are captured
   at kernel construction and invoked directly;
3. a direct subprocess launcher collaborator captured at construction;
4. no local skill interception, mutable MCP handler cache, session-signal hub,
   arbitrary callback, or dynamic profile/config lookup on the portable path;
5. a versioned closed implementation identifier, with reviewed source changes
   requiring a version bump; and
6. an immutable settled-workspace generation/snapshot, rather than a path-only
   workspace identity; and
7. a live guard that validates the kernel object and its exact finite
   collaborators before it produces an executor-owned effect.

Local interception and any unsupported configuration must route to the legacy
process-local runtime instead. The sealed kernel is deliberately deferred; it
is not simulated by a long allowlist of legacy helper methods.

## Consumer rules

Foundation B may attach an authority fingerprint to attempt and final-acceptance
events as diagnostic attribution, but a process-local fingerprint or correlation
id must never be used as an event-deduplication key, replay/idempotency key,
trust key, reusable-result key, or final-acceptance key. Foundation B's
replay-safe final event will have its own authority-generation semantics.

The runner records the authority scope when a new run starts. On resume it must
check the live generation capability **before** looking up
`execution_identity_contract`, a resume-selector provider, or any other
runtime-owned dynamic collaborator. If a `process_local` session has no live
capability, resume terminates with a typed `process_local_resume_unavailable`
outcome and an operator must start a new attempt; it must never silently fall
back to a stale session, a cached pass, or a newly computed runtime descriptor.

This is also a deployment boundary: a one-shot detached worker may retain an
owner while it remains alive, but its handler-local runner and capability leave
with that worker. A later request handled by a different worker therefore
returns `process_local_resume_unavailable`; it is not a durable-resume path and
must not attempt to recreate the prior owner from its stored correlation data.

For a new process-local session, the runner validates/allocates the session id,
registers the registry-minted capability, and acquires its PID-and-boot-time
liveness lease **before** it persists the durable `RUNNING` tracker. If either
registration or lease acquisition fails, no `RUNNING` tracker is published.
The lease is liveness evidence only; it never transfers or reconstructs the
opaque authority capability.

Effectful execution atomically claims the live session capability. A second
same-process caller receives `process_local_execution_in_progress` and must
not terminalize or retire the original caller. `PAUSED` releases **only** that
exclusive claim: its registry registration/capability and liveness lease remain
held by the original owner. Terminal, cancellation, and setup-abort paths retire
the registration, issuance, claim, and owned lease together.

On a valid process-local resume, a foreign runner or observer must treat either
a live registry registration or a live liveness lease as an active owner and
return the non-terminal typed block
`process_local_authority_held_elsewhere`. It produces the terminal
`process_local_resume_unavailable` result only when **both** the matching
registry registration and liveness lease are absent. This distinguishes an
active process from a crashed/exited owner without treating a lock as portable
authority.

The MCP handler retains a paused process-local owner strongly. A same-handler
resume selects the exact retained runner, adapter, and handler-owned
`EventStore`; it does not reconstruct a fresh runtime or capability from the
persisted correlation id. Because pausing releases the task-worktree lock, the
handler restores that exact workspace and reacquires its lock before the
retained runner resumes. The retained event store stays open while paused and
is closed only after that runner reaches a terminal state.

New process-local session ids must pass the canonical safe-id validation before
registration or lease acquisition. For an old/corrupt persisted id that is not
safe, heartbeat observers derive a containment-safe hashed lookup filename;
they do not use the raw value as a path and cannot register it as a new
authority. Heartbeat observers never delete stale or malformed lease records:
their read is non-atomic, and removal could race with a new holder. Such a
record simply cannot prove liveness.

Contracts created before this schema have no Foundation A authority scope. They
are not migrated into the new authority model: a runner that cannot prove a
same-process generation rejects their resume explicitly. This is a deliberate
fail-closed migration rule, not a claim that old persisted runtime descriptors
were portable.

Foundation C may support same-process capsule continuation for a process-local
generation. Cross-process fresh-session continuation is locked behind the
sealed-kernel prerequisite: until that kernel is approved, a process restart
produces the explicit terminal/new-attempt path above. A later C sub-slice may
enable cross-process continuation only when every component is portable and its
capsule contract is independently valid.

## Process-local lifecycle ownership

The process-local registry is the sole in-process owner of the authority
generation's lifecycle.  It has a deliberately small state machine; runner,
MCP execution, and public cancellation surfaces may request a transition, but
they must not each infer a separate terminal lifecycle.

```mermaid
stateDiagram-v2
    [*] --> registered: mint + register + acquire lease
    registered --> claimed: atomically claim effects
    claimed --> registered: pause or retryable cancellation failure\nrelease claim only
    registered --> terminalizing: public terminalization reservation
    terminalizing --> registered: durable write fails or caller is cancelled
    terminalizing --> retired: durable terminal record + registry finalizer
    claimed --> retired: owner persists terminal record + owner teardown
    registered --> retired: setup abort or proven lost authority
    retired --> [*]
```

`registered` holds the opaque generation, registration, heartbeat lease, and
registered terminal finalizers. `claimed` adds the one exclusive right to
perform effects. `terminalizing` is not a second effect owner: it is a
reservation held only while a public terminal surface writes its durable record
for an otherwise unclaimed paused owner. `retired` contains none of those
resources. A session must never be both durably terminal and locally
`registered` after a successful terminalization path completes.

### Terminalization API

The registry exposes four lifecycle operations, all keyed by the exact
session/execution/correlation tuple:

| Operation | Preconditions | Result |
| --- | --- | --- |
| `claim` | `registered`, no effect claim or reservation | moves to `claimed`; a competing caller receives the non-terminal in-progress block |
| `release` | exact current effect owner | returns `claimed` to `registered`, preserving the registration and lease for a paused/retryable owner |
| `begin_terminalization` | `registered` and unclaimed | reserves `terminalizing`; an active claim or another reservation is signalled instead of being terminalized underneath |
| `retire_after_terminal_persistence` | a durable terminal record has already succeeded | atomically removes registration, claim/reservation, issuance, and finalizer set, then drains the captured finalizers |

Only the final operation is allowed to run the retained-owner cleanup callbacks.
The callbacks close retained `EventStore` instances, evict handler-held runners,
release heartbeat/worktree state through their exact owner, and are idempotent.
They are registered against the opaque live binding rather than reconstructed
from persisted diagnostic data. If cancellation reaches the caller while this
finalizer set is draining, the drain still completes every callback before that
caller cancellation is re-raised. A cancelled or failing individual callback is
logged and cannot prevent subsequent cleanup callbacks from running.

Normal runner-owned completion persists its terminal tracker before performing
its own teardown. A public terminal surface follows the same order, but first
takes `begin_terminalization`; it has no authority to mint/reconstruct a
runner. This gives the paused retained owner and the public MCP surface one
shared transition instead of two competing cleanup paths.

Authority-loss failure and public cancellation use an additional durable
transition guard: `append_session_terminal_if_active`. It checks for an
explicit session terminal event and appends the new terminal event in the same
database transaction. For SQLite it takes the immediate write transaction;
the per-session unique terminal-transition guard provides the same one-winner
property on other supported stores. The outcome is either one durable winner or
a successful no-op because another terminal event already won. A stale
`PAUSED`/`RUNNING` snapshot therefore cannot append `FAILED` over a concurrent
`CANCELLED` record after the registry has retired its local generation.

### Cancellation and resume race matrix

| Situation | Durable action | Local authority result | Caller-visible outcome |
| --- | --- | --- | --- |
| A local worker is `claimed`; public cancel arrives | Do not write a terminal record underneath it; publish the cooperative request | claim remains with worker until its orderly cancellation path | successful `cancellation_requested` response; worker performs persistence and teardown |
| A local paused owner is `registered`; public cancel arrives | reserve first, publish request, then write `CANCELLED` | reservation blocks a concurrent resume claim; successful write retires and drains all finalizers | terminal cancellation response |
| The paused-owner cancellation write fails or its caller is cancelled | no terminal record is reported | release only the reservation; preserve registration, lease, and published cancellation request | retryable error/caller cancellation; retained owner retries before any next effect |
| Worker cancellation persistence fails | no false `CANCELLED`/`FAILED` record | release exiting worker's effect claim, active route, and worktree lock; retain registration, heartbeat, and cancellation request | typed `cancellation_persistence_pending`; next same-process resume retries cancellation before executing effects |
| MCP execution wrapper receives `cancellation_persistence_pending` | must not call `mark_failed` | retains the exact owner and its liveness until a successful terminal write | retryable nonterminal error |
| A stale paused/running snapshot observes authority retirement after a public terminal write | conditional terminal append finds the existing terminal event and does not append `FAILED` | no generation is recreated or retired a second time | fail-closed resume error while the durable terminal status remains unchanged |
| A concurrent same-owner resume occurs during a claim or terminalization reservation | no competing terminal write | `claim` fails without releasing the live owner or its worktree state | `process_local_execution_in_progress` |
| A foreign live process holds the heartbeat lease | must not terminalize a process it cannot signal | no local registration is manufactured or retired | retryable `process_local_authority_held_elsewhere` |
| A terminal record is written through a public paused-owner cancellation | call `retire_after_terminal_persistence` only after the write | registry atomically detaches the opaque binding, then finalizers evict runner/cache/store and lease resources | later resume sees terminal state and cannot leak a retained owner |
| A raw caller cancellation interrupts pre-effect contract/tool setup after a claim | first drain any published cooperative cancellation in a shielded task | successful cancellation retires normally; failed persistence releases only the claim/route/lock and preserves retryable authority | cancellation result or `cancellation_persistence_pending`, never `PAUSED → FAILED` through lost authority |

The `claim` and terminalization reservation are intentionally the same mutual
exclusion boundary. Thus a resume cannot acquire effects between public-cancel
preflight and persistence. The implementation still treats an observed
post-write active claim defensively: it republishes cancellation for that owner
to complete orderly teardown rather than silently deleting its live state.

## Exit matrix

The Foundation A implementation must demonstrate all of the following:

1. exact current CLI runtimes, their subclasses, custom runtimes, custom
   verifiers, local skill dispatch, local handler caches, and signal hubs are
   process-local;
2. changing `_execute_task_impl`, `_build_command`, a local handler cache, or
   `execution_identity_contract` after capture never leaves a runtime marked
   portable;
3. no runtime descriptor, credential-shaped value, callable closure/global, or
   cache contents appear in canonical authority JSON;
4. two process-local runtime captures receive distinct fingerprints even when
   configured alike, while a live binding retains its captured nonce for a
   same-instance recheck;
5. workspace, built-in verifier, and closed executor-policy divergence change
   the identity; malformed or unsafe values fail closed to process-local;
6. executor-owned fixed entry-root drift is rejected before its next owned
   effect, while runtime helper drift is explicitly outside the portable
   guarantee; and
7. no public Foundation A API says or implies that a portable identity grants
   reuse, dispatch, result, checkpoint, trust, routing, or final acceptance.
8. a new run registers its non-serializable generation capability; same-process
   resume retains it, while a reconstructed process-local contract without that
   capability rejects before any dynamic runtime identity or resume-selector
   provider is invoked;
9. a correlation id, forged or deserialized generation, or forked child cannot
   register a live authority; issuance and registration are registry-private
   and PID-bound;
10. only one caller may hold an effectful claim for a session, while a second
    caller fails non-terminally and a live `RUNNING` holder is never
    terminalized by an observer;
11. a `RUNNING` tracker whose registry capability and liveness lease have both
    disappeared takes the explicit terminal/new-attempt path;
12. process-local authority data is attribution only and cannot be used by an
    attempt/final-event reducer as a dedupe, replay, trust, or acceptance key;
    and
13. every runtime-factory backend and a subclass/custom adapter are classified
    process-local, the boundary matrix lists `legacy_runtime_descriptor` only
    under `process_local`, and legacy persisted contracts without the new scope
    marker take the explicit fail-closed migration path.
14. registry registration and the held liveness lease precede durable `RUNNING`
    persistence; a failed lease acquisition rolls the registration back without
    publishing a recoverable-looking session;
15. `PAUSED` releases only the effect claim, and the same handler's retained
    runner/adapter/EventStore resumes only after it has reacquired the paused
    worktree lock; a concurrent or foreign resume returns a typed non-terminal
    result without calling `mark_failed`; and
16. heartbeat lookup is contained for unsafe legacy ids, while observers retain
    stale/malformed lease files rather than deleting liveness evidence they did
    not own.
17. a public terminalization reserves an unclaimed paused generation before its
    durable write, blocks concurrent effect claims, and releases that reservation
    without retiring the owner when the write or its caller fails;
18. a failed worker cancellation write releases the departed effect claim,
    active route, and worktree lock while preserving the live registration,
    heartbeat, and cancellation request for an exact-owner retry;
19. the MCP wrapper preserves that retryable cancellation state rather than
    manufacturing `FAILED`, and a foreign live heartbeat receives a retryable
    ownership block rather than an unsafe public terminal write;
20. an externally persisted terminal record drains the exact retained runner,
    handler cache, heartbeat, worktree ownership, and owned EventStore through
    the registry's one finalizer path; and
21. caller cancellation and a cancelled/failing finalizer cannot skip the
    remaining registered finalizers or leave the registry reservation behind.
22. competing `FAILED`/`CANCELLED` session lifecycle writes have exactly one
    durable winner, and a stale pre-terminal tracker cannot overwrite that
    terminal status after authority retirement; and
23. a raw task cancellation in every claimed-but-pre-effect setup window drains
    a published cooperative cancellation before generic cleanup, or preserves
    the explicit retryable cancellation state for the next exact-owner resume.

This exit matrix is intentionally narrower than an arbitrary-code sandbox and
broader than a cosmetic fingerprint: it makes the only cross-process claim
that Foundation A can currently prove, which is that it makes no such claim
for legacy dynamic runtimes.
