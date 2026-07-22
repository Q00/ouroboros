# AC Runtime Execution Authority Boundary

Foundation A creates one versioned, identity-only executor baseline. It is not
an attempt capsule and it never authorizes result reuse, checkpoint reuse,
trust reuse, dispatch, routing, or final acceptance on its own.

## Finite ownership matrix

| Component | Foundation A treatment | Later owner |
| --- | --- | --- |
| `ParallelACExecutor`, `LeafDispatcher`, `LevelCoordinator`, verification gate, and rate-gate algorithm | Closed, versioned implementation descriptors | Foundation A |
| Runtime adapter | Explicit runtime descriptor supplied by the adapter; values are represented as safe digests | Foundation A |
| Workspace | Resolved path identity represented as a digest | Foundation A |
| Built-in transcript verifier | Closed, versioned verifier descriptor | Foundation A |
| Custom verifier | Exact object identity for this process only; never portable solely because it has Python code, a closure, defaults, globals, or an identity method | Foundation A / process local |
| AC text, prompt, tool catalog, selected model/effort, runtime handle, checkpoint and session state | Excluded | Foundation C attempt capsule |
| Shadow-replay isolated workspace | Ephemeral experiment input; process-local and never an acceptance workspace | Live process |
| Event emitter/store, caches, queues, locks, signal hubs, module monkeypatches, arbitrary Python object graphs | Volatile and excluded | Foundation B or live process |

The contract does **not** recursively inspect functions, descriptor methods,
closures, globals, builtins, classes, or arbitrary object graphs. Those are not
a finite authority surface. A custom verifier remains process-local even if it
provides a descriptor; a descriptor can describe a configuration but cannot
prove arbitrary Python behavior is portable.

## Supported portable forms

Only the closed built-in transcript verifier and an adapter with an explicit,
safe runtime identity descriptor can contribute durable identity. The runtime
descriptor is canonical JSON under a strict size/depth budget and is represented
by a digest, never copied verbatim into authority JSON. A credential-shaped or
malformed value makes that runtime component unobserved/process-local.
A durable runtime also carries an explicit observation that its direct dispatch
root was finite and observable; `stability` alone cannot promote an otherwise
process-local descriptor.

The implementation versions in `execution_authority.py` are reviewed source
constants. Changing the behavior of a closed component requires an explicit
version bump and tests; no source, bytecode, or callable graph hashing is used
as a substitute for that review.

## Live-process guard

Before an atomic dispatch or verifier pass, the executor verifies only the
enumerated live roots:

1. adapter object identity and current finite runtime descriptor;
2. verifier object identity and its captured finite descriptor;
3. captured `LeafDispatcher` instance, direct `stream` callable, and default
   attribute-resolution root;
4. captured `LevelCoordinator.run_review` root, binding, and default
   attribute-resolution root;
5. captured rate-gate `acquire` callable, timing scalars, sleep root, bucket
   identity/configuration/time source, and direct admission roots
   (`enabled`, `acquire`, and `force_reserve`), plus default attribute-resolution
   roots;
6. adapter dispatch and attribute-resolution roots; and
7. workspace and static policy descriptors; and
8. the six original executor entry functions used by internal orchestration:
   single-AC execution, atomic execution, rate admission, decomposition
   dispatch, verifier dispatch, and the shell/filesystem verification gate.

A replacement root fails closed. Mutation inside an arbitrary custom callable
is deliberately not treated as a portable identity claim; it is process-local
and cannot later qualify a capsule, checkpoint, trust, or acceptance reuse.
If a direct component installs a custom attribute-resolution hook before
construction, it remains executable but the baseline is process-local. If a
previously observed default hook changes after construction, the live guard
rejects the effect before dispatch, verification, coordination, or rate
admission.
If a direct runtime-dispatch root cannot be observed, or the adapter installs a
custom attribute-resolution hook, execution remains available but the baseline
is explicitly process-local.

For a portable baseline, internal orchestration calls those six captured
functions directly rather than resolving `self._…` again. A post-construction
class replacement, in-place code replacement, or instance shadow of an entry
root fails closed before the next effect. If such a root is already nonstandard
at construction, the executor remains executable only as process-local; it
cannot advertise portable authority.

Construction captures each of those callable entry roots directly. An opaque
or non-callable entry cannot be captured and rejects construction rather than
falling back to a later dynamic lookup. A `ParallelACExecutor` subclass is
also process-local, including one that inherits all six entries unchanged:
its concrete type is part of the closed portable set. Unhashable or
equality-overriding subclasses remain executable as process-local instances.

Shadow replay is an opt-in, non-authoritative cost experiment. Its isolated
workspace is per-attempt process state rather than the live executor workspace,
and its verdict never accepts the live AC. Before it creates a replay runtime it
uses the same live-process guard; when it needs the transcript verifier it enters
through the captured verifier-dispatch root rather than dynamically resolving
`executor._run_atomic_verifier_pass`.

This is a collaborator-integrity boundary, not a sandbox against arbitrary code
running in the same Python process. Directly calling a monkeypatched private
entry point, or deliberately introspecting and mutating private module closures,
is outside the boundary. The guarantee applies to the executor's own unchanged
orchestration path, where effects use the closed-root invocation path.

## Exit matrix

Tests must show that:

1. workspace, adapter descriptor, static policy, verifier root/code, dispatcher
   root, and rate-gate semantic drift are rejected before dispatch or
   verification, including
   post-construction `__getattribute__` replacement on direct effect owners;
2. custom closures/defaults/globals do not enter the canonical authority JSON
   and make the verifier process-local;
3. reflective lookup and arbitrary object graphs cannot be made portable by
   the implementation;
4. credential-shaped provider data is absent from canonical authority JSON;
5. per-attempt and volatile inputs do not alter the baseline; and
6. post-construction class, code, and instance entry-root drift is rejected
   from the internal execution path, while a pre-construction nonstandard root
   is process-local; and
7. `portable_across_processes` grants no reuse or acceptance semantics.
8. shadow replay rejects entry-root drift before creating its isolated baseline
   runtime and invokes its verifier through the captured entry root.
