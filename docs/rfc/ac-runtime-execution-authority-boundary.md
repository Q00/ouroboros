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

Every process-local component receives an opaque random nonce once per
**authority generation**. `ExecutionAuthorityLiveBinding` retains that nonce
for its same-instance integrity rechecks; a new executor/runtime capture gets a
new nonce. Two instances may have similar visible configuration, but they
cannot compare as a portable authority or accidentally authorize cross-process
reuse.

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

This exit matrix is intentionally narrower than an arbitrary-code sandbox and
broader than a cosmetic fingerprint: it makes the only cross-process claim
that Foundation A can currently prove, which is that it makes no such claim
for legacy dynamic runtimes.
