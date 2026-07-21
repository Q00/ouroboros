# AC Runtime Execution Authority Boundary

Foundation A defines a portable executor *baseline*. It is deliberately not a
complete identity for an active execution attempt. The baseline may become one
input to a later capsule, but it never authorizes dispatch, checkpoint reuse,
trust reuse, or final acceptance by itself.

## Inclusion matrix

| Category | Foundation A treatment | Later owner |
| --- | --- | --- |
| Executor implementation, static `LeafDispatcher`, and token estimator | Versioned, implementation-bound portable baseline | Foundation A |
| Runtime/backend configuration, executable generation, watchdog policy, static handle-selector implementation | Versioned, redacted/digested portable baseline | Foundation A |
| Workspace generation, verifier implementation, and exact execution policy | Versioned portable baseline | Foundation A |
| AC semantics, prompt, tool catalog, selected runtime handle, `ACRuntimeHandleManager`, checkpoint and resume state | Explicitly excluded from the baseline | Foundation C attempt capsule |
| `LevelCoordinator` implementation, conflict-reconciliation policy, reasoning effort, session cache, and review session state | Explicitly excluded from the baseline | Foundation C attempt capsule |
| `ExecutionEventEmitter`, event-store handles, `SessionSignalHub`, queues, locks, and live pools | Explicitly volatile; never upgraded by a declared identity | Foundation B event semantics / runtime process |

The machine-readable form of this matrix is
`execution_authority_boundary_contract()`, embedded in every
`ExecutionAuthorityContract` canonical payload.

## Security and portability rules

- Provider-controlled identity values are never copied into canonical authority
  JSON. Foundation A retains a deterministic digest only where a durable
  baseline needs equality; known credential-shaped values make that input
  unobserved and process-local.
- An unobserved runtime or LLM backend label may bind only the initial live
  adapter through a process-local guard. It cannot resume durably or join a
  frugality-proof cohort, because two hidden labels cannot prove equality
  without serializing credential material.
- A declared identity on a live collaborator cannot make it portable. Only the
  explicit `leaf_dispatcher` static-type allowlist is an executor subcomponent
  of this layer.
- A selector implementation can be baseline identity; a selected
  `RuntimeHandle` cannot. The handle's metadata is bound by the attempt capsule
  that actually resumes or dispatches it.
- If a provider getter, implementation, gate-factory dependency, or directly
  constructed gate-class member cannot be observed safely, the resulting
  baseline is process-local rather than exposing an error message or claiming
  cross-process portability.
- A rate-gate class member is portable only when it is the exact original
  import-time declaration. A dynamically installed wrapper is process-local
  even if `functools.wraps` gives it the same module, name, signature, or source
  shape; its live closure and module state cannot be promoted into the baseline.
- The only portable rate-gate factory is the exact original import-time
  `build_rate_limit_gate` declaration. Any replacement factory is process-local,
  even when its source, metadata, closures, or dependency graph appear
  equivalent. Mutating that original function's code, defaults, keyword
  defaults, or closure state in place also makes it process-local.
- The original gate classes' directly read runtime-module members are likewise
  import-time-bound. Replacing `time.monotonic`, `asyncio.Lock`, or
  `asyncio.sleep` makes the factory process-local rather than treating an
  in-memory scheduling change as portable behavior.
- For a directly read Python function or class such as `asyncio.sleep` or
  `asyncio.Lock`, import-time binding includes its executable implementation:
  code, defaults, closure state, raw class members, and inherited class-member
  implementations, along with directly resolved globals, builtins, and module
  members. An in-place stdlib monkeypatch is therefore process-local even when
  the exported module member object and its source text are unchanged.
- The direct gate-class member manifest must also be complete. Replacing a
  method with a custom descriptor cannot make that member disappear from the
  portable implementation graph, and a descriptor that reuses the original
  `__func__` still fails closed when its own binding behavior changes.
- Each original gate member's directly resolved globals and builtins are
  import-time-bound too. Replacing `deque` or an internal result type therefore
  fails closed even when a replacement copies the original display metadata.
- The complete raw class dictionary is import-time-bound as well. Adding a
  special descriptor such as `__getattribute__` therefore cannot reroute
  `gate.acquire` while retaining a portable factory identity.
- An original gate member's object identity is not sufficient by itself: its
  code, defaults, keyword defaults, and closure state are import-time-bound.
  In-place implementation mutation therefore fails closed instead of creating a
  new portable algorithm digest.
- Gate result types directly constructed by those members receive the same raw
  class/member integrity check. Mutating `RateLimitSnapshot` or
  `RateLimitBackoff` in place cannot alter rate-gate behavior under a portable
  baseline.

## Foundation A exit matrix

Before a Foundation A PR is reviewed, tests must demonstrate all of the
following:

1. Provider credentials and property-getter exceptions never appear in
   canonical authority JSON.
2. Workspace, runtime implementation, verifier graph, policy, static dispatcher,
   and the actual rate-gate factory (including direct class-member implementations)
   change the baseline fingerprint.
3. A live signal hub cannot become portable merely by declaring an identity.
4. Changing an AC runtime handle, handle manager, checkpoint state, coordinator
   session, or event emitter does not purport to change the portable baseline;
   the encoded boundary assigns each to its later owner.
5. `portable_across_processes` is only an identity-stability property. It is
   insufficient for any dispatch, recovery, trust, result, or acceptance reuse.
