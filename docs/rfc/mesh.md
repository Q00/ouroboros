# RFC — MCP Mesh wire format and Coordinator handshake

> Status: **Accepted** (Phase 2 of #476 Agent OS roadmap).
> Closes [#511](https://github.com/Q00/ouroboros/issues/511).
> Related: [#471](https://github.com/Q00/ouroboros/issues/471) (Control vs Capability plane), [#476](https://github.com/Q00/ouroboros/issues/476) Q4 (dynamic MCP invariant), [#492](https://github.com/Q00/ouroboros/pull/492) (`control.directive.emitted`), [#436](https://github.com/Q00/ouroboros/pull/436) (`event_version`), [#517](https://github.com/Q00/ouroboros/issues/517), [#518](https://github.com/Q00/ouroboros/issues/518), [#519](https://github.com/Q00/ouroboros/issues/519).

## Summary

This RFC specifies the **wire layer** of the Mesh — the IPC-style channel that lets every harness (Claude Code, Codex CLI, OpenCode, Gemini CLI, Hermes, LiteLLM) attach to a single in-process Coordinator through one envelope shape, one polling discipline, and one ordering rule. It establishes the contract that Phase 3 implementation issues build against, and it formalizes how the runtime of the Phase-2 Agent OS (#476) actually moves bytes.

The RFC settles the *how* of the Mesh; the *what* (Contract = OS, Mesh = IPC, sub-agents as disposable processes) is settled by #476 itself.

## Scope

This document **does** decide:
- Transport, envelope schema, polling/push discipline, Coordinator topology, runtime registration handshake, ordering guarantees, and timeout/failure semantics.

This document **does not** decide:
- Per-runtime profile mappings (deferred to [#519](https://github.com/Q00/ouroboros/issues/519)).
- Disposable Memory's process model and artifact backend (deferred to [#512](https://github.com/Q00/ouroboros/issues/512); the companion RFC lands separately).
- Contract Ledger schema (deferred to [#513](https://github.com/Q00/ouroboros/issues/513); the companion RFC lands separately).
- The migration of `mcp_manager` plumbing to `AgentRuntimeContext + ControlBus` (tracked in [#474](https://github.com/Q00/ouroboros/issues/474) / [#475](https://github.com/Q00/ouroboros/issues/475)).

## Constraints inherited from #476

- **Local-first cooperative trust.** No daemon, no SaaS, no cross-tenant boundary. A misbehaving runtime *can* forge directives; the RFC does not police identity.
- **4-verb filter.** Every Mesh decision must strengthen exactly one of `replay / explain / steer / compose`. Decisions that fail this filter are rejected.
- **Tier-4 Won't.** The Mesh does not introduce real-time guarantees, multi-tenant isolation, or BFT.
- **Additive-only schemas.** [#436](https://github.com/Q00/ouroboros/pull/436)'s `event_version` rule applies to every envelope field added later.

## Decisions

### D1 — Transport: streamable-http (primary) + stdio multiplex (sub-process fallback)

**Decision.** All Mesh traffic flows over the `streamable-http` transport that ouroboros already uses for MCP ([#339](https://github.com/Q00/ouroboros/pull/339), [#485](https://github.com/Q00/ouroboros/pull/485)). For runtimes that already speak stdin/stdout (Codex CLI, Hermes, OpenCode, Gemini CLI), `stdio multiplex` is the local fallback. The Mesh adapter normalizes both into the same envelope shape so callers do not branch on transport.

**Stdio framing.** Stdio multiplex uses newline-delimited JSON frames with an explicit `frame` discriminator:

- Coordinator → runtime: one `{"frame":"request","body": <request envelope>}`.
- Runtime → Coordinator: zero or more `{"frame":"event","body": <BaseEvent-compatible event>}` frames.
- Runtime → Coordinator: exactly one `{"frame":"result","body": <result envelope>}` frame, then EOF for that contract invocation.

The stdio adapter owns per-invocation process startup and maps these frames to the same logical stream the HTTP adapter exposes. Registration for stdio runtimes is not performed on this one-shot invocation stream; the adapter obtains capabilities from a static descriptor or a separate short-lived `{"frame":"register"}` discovery process before any contract dispatch. There is no `/mesh/contract` URL on stdio; the request envelope is the dispatch boundary.

**Rationale.** Reusing `streamable-http` keeps a single transport story across the codebase; UDS would force a Windows-incompatible split; raw TCP would invite firewall prompts on every workstation; SSE alone cannot carry bidirectional traffic; long-poll would require its own tuning loop. `stdio multiplex` covers exactly the runtimes whose existing pattern already speaks stdio.

**Risks.**
- Coupling to MCP's transport spec. Mitigation: a thin adapter layer quarantines `streamable-http` details so MCP changes only touch one file.
- Operator confusion when both transports are visible. Mitigation: `ouroboros mcp doctor` ([#445](https://github.com/Q00/ouroboros/pull/445)) prints the active transport per runtime in the resolved table.

### D2 — Envelope schema: ULID id + target binding + additive `extra` slot

Two envelopes — request (Coordinator → runtime) and result (runtime → Coordinator) — versioned and additive-only.

**Request envelope.**

```jsonc
{
  "schema_version": 1,
  "contract_id": "01HXAB...",            // ULID, 26 chars, sortable, no extra dependency
  "correlation_id": "01HXAC...",         // attempt id within this contract
  "parent_correlation_id": null,          // set on retry attempts
  "target_type": "lineage",              // open string; known values live in events/control.py
  "target_id": "ralph-...-v3",
  "directive": "evaluate",               // core.directive.Directive.value (lowercase)
  "emitted_by": "orchestrator",
  "deadline_ms": 60000,                  // relative timeout duration in milliseconds
  "extra": {}                             // additive-only growth slot
}
```

**Result envelope.**

```jsonc
{
  "schema_version": 1,
  "contract_id": "01HXAB...",            // mirrored from request
  "correlation_id": "01HXAC...",         // mirrored from request
  "parent_correlation_id": null,          // mirrored from request when present
  "result": {
    "status": "ok",                       // "ok" | "error"
    "next_directive": "converge",          // required: see state-machine table below
    "artifact_ref": "sha256:abc123...",   // content hash; body fetched separately
    "error": null                         // Error object below when status="error"
  },
  "runtime_id": "codex_cli",              // which harness produced the result
  "duration_ms": 12345,
  "runtime_events_emitted_count": 17,     // runtime-reported event frames sent before result
  "extra": {}
}
```

**Error object.** When `result.status="error"`, `result.error` MUST be an object with at least:

```jsonc
{
  "code": "deadline_exceeded",       // stable machine-readable discriminator
  "message": "runtime exceeded deadline",
  "retryable": true,                  // Coordinator policy input
  "details": {}                       // additive-only diagnostic slot
}
```

The Coordinator uses `retryable` plus its own retry budget to decide whether to emit `retry` or accept the runtime's terminal `cancel`; runtimes do not decide budget state.

**Result state machine.** Legal combinations are intentionally small:

| `result.status` | `result.next_directive` | Meaning |
|---|---|---|
| `ok` | `continue` | Successful non-terminal progress; Coordinator journals `continue` |
| `ok` | `converge` | Successful terminal completion; Coordinator journals `converge` |
| `ok` | `wait` | Runtime needs external input; Coordinator journals `wait` |
| `error` | `cancel` | Runtime failed this attempt; Coordinator either journals `cancel` or, when `error.retryable=true` and budget remains, fences this attempt and journals `retry` for a new correlation id |

All other combinations are schema-invalid. In particular, `wait` is a directive, not a third status value, and error results always include the typed `error` object above.

**Why ULID.** Sortable, log-friendly, 26-char ASCII, and synthesizable in 10 lines without a new dependency. Replaces UUIDv4 wherever event chains need to be reconstructed in order. `parent_correlation_id` is nullable on first attempt and set to the previous attempt's `correlation_id` when the Coordinator dispatches a retry, so retry ancestry is represented in-band.

**Why `runtime_events_emitted_count` instead of inline events.** Disposable Memory's promise is that the *main session ledger holds only `contract_id + artifact_ref`*. Streaming the runtime-reported frame count keeps the wire small and gives the Coordinator a sanity check against the events it actually persisted; the authoritative body is fetched from the EventStore by a projector when needed. The Coordinator's post-result lifecycle directive is not included in this runtime-authored count. This preserves the bloat-guard invariant from [#512](https://github.com/Q00/ouroboros/issues/512) at the wire layer.

**Final directive mapping.** The runtime sets `result.next_directive` according to the state-machine table above. Retry-budget decisions remain Coordinator/resilience-layer policy; a retryable runtime failure is reported as `status="error"`, `next_directive="cancel"`, and `error.retryable=true`, and the Coordinator decides whether to emit `retry` and dispatch another attempt instead of accepting the cancellation. `result.status` remains the coarse transport/result class; `next_directive` is the normative journal mapping unless Coordinator retry policy overrides a retryable error.

**`extra` governance.** Adding a key to `extra` requires a one-line justification in the PR body, identical to the narrow-membership commitment for `AgentRuntimeContext` in #476 Q1. This stops slot sprawl over time.

### D3 — Polling vs push: streamable-http hybrid

**Decision.** The Coordinator is the client for contract dispatch. For streamable-http runtimes it opens `POST /mesh/contract` against the selected runtime endpoint. For stdio runtimes it writes the D1 request frame to the child process. In both cases the runtime streams *intermediate events* (`tool.call.*`, `llm.call.*` from [#517](https://github.com/Q00/ouroboros/issues/517)) and the *final result envelope* on the same logical channel, then closes. Runtimes never POST new work back to the Coordinator in this phase.

**Rationale.** The slide framing of *"tool schema, polling, ResultEvent that act like IPC"* maps cleanly onto streamable-http: polling is implicit in the runtime's streaming response to the Coordinator's dispatch request. No long-poll loop to tune, no SSE-only one-way limitation, no command channel separate from the data channel.

**Risks.**
- A single long-running stream ties up one connection per in-flight contract. Mitigation: `deadline_ms` is enforced server-side as a relative timeout from request receipt; abandoned streams are closed when the timeout expires regardless of client behavior.
- Reconnect semantics. Mitigation: the Coordinator reconnects to the same streamable-http runtime endpoint with the same `(contract_id, correlation_id)` after ordinary HTTP transport drops; D6 stream replay and Coordinator-side duplicate suppression make reconnection safe. Stdio EOF/pipe loss is treated as runtime process loss unless the adapter can prove the child is still alive and can accept a fresh request frame.

### D4 — Coordinator topology: embedded with abstract interface

**Decision.** The `Coordinator` lives inside the orchestrator process. A `Coordinator` Protocol/ABC isolates the in-process implementation from the rest of the orchestrator so a future daemon implementation can be substituted without touching call sites.

**Rationale.** Tier-4 Won'ts (no SaaS, no real-time, single-machine) make a separate daemon infrastructure for goals we have explicitly declined. The abstract interface keeps the door open for a Phase 4+ multi-machine extraction at exactly one cost: the day someone wants distributed runtimes, they replace one implementation.

**Risks.**
- Single-process bottleneck. Mitigation: Tier-4 Won't makes this a non-goal; the abstract interface is the planned escape valve.
- Implicit lifetime coupling between Coordinator and orchestrator session. Mitigation: the abstract interface defines lifetime hooks (`start()`, `stop()`) so a future daemon does not need to inherit session boundaries.

### D5 — Runtime registration: startup + #476 Q4 invariant verbatim

**Startup handshake.** At orchestrator start, the Coordinator reads the binding table from [#519](https://github.com/Q00/ouroboros/issues/519) (`runtime_profile.stages`) and resolves capabilities per runtime. Streamable-http runtimes use a persistent registration channel. Stdio runtimes register through the Mesh adapter's static descriptor or a separate short-lived discovery process; the one-shot contract invocation stream from D1 is not held open for registration. Each runtime advertises:

```jsonc
{
  "runtime_id": "codex_cli",
  "version": "0.31.0",
  "accepted_input_directives": ["continue", "evaluate", "evolve", "unstuck", "retry", "compact", "wait", "cancel", "converge"],
  "emitted_result_directives": ["continue", "converge", "wait", "cancel"],
  "supported_target_types": ["lineage", "execution", "agent_process"],
  "capabilities": [/* CapabilityRegistry wire format */]
}
```

`accepted_input_directives` names the `core.directive.Directive.value` inputs this runtime can be asked to handle. `emitted_result_directives` names the smaller set this runtime may place in `result.next_directive`; it is result-side metadata, not an additional dispatch vocabulary.

**Dynamic addition** reuses the contract from #476 Q4 verbatim. This RFC does not invent anything new on this path — it simply requires the Mesh to honor the existing invariant.

```
1. mcp_bridge.add_server(config)               — transport + discovery
2. CapabilityRegistry.sync_from(bridge)        — typed capabilities
3. PolicyEngine.evaluate(role, phase, ...)     — policy decision set
4. Emit policy.capabilities.changed            — diff summary event
5. ControlBus subscribers re-read on next turn
```

**Invariant.** *If step 4 did not emit, step 5 must not see the capability.*

### D6 — Ordering: FIFO per `contract_id` + at-least-once + idempotent handlers

This is the single decision that introduces fresh semantics; all others either inherit or follow naturally.

**Decision.**

- **FIFO per `contract_id`.** Directives within one contract chain are delivered and journaled in emission order. Cross-contract ordering is *not* guaranteed; that is the parallelism unlock.
- **At-least-once delivery with fencing.** Timeout → the Coordinator first fences the timed-out `(contract_id, correlation_id)` so any later frames from that attempt are ignored, emits `retry` while budget remains, and dispatches a fresh attempt under the same `contract_id` with a new `correlation_id` and `parent_correlation_id` pointing at the fenced attempt. No exactly-once machinery.
- **Idempotency obligation on handlers.** A handler that receives a duplicate `(contract_id, correlation_id)` replays the cached stream and final result for that exact attempt instead of re-executing. Cache lifetime equals the contract's lifetime (cleared at completion or cancellation).
- **Stream replay on reconnect.** For streamable-http runtimes, the runtime cache is append-only per `(contract_id, correlation_id)` and contains every emitted intermediate event plus the final envelope once available. A reconnect for the same unfenced attempt receives the cached prefix first, with the original event ids/call ids, then resumes streaming only if the original attempt is still running. The Coordinator appends by stable event identity, treats duplicate streamed events as no-ops, and drops all frames from fenced attempts. For stdio runtimes, replay is only available when the adapter can prove the subprocess is still alive and can accept a duplicate request frame; otherwise EOF/pipe loss is D7 runtime loss, not reconnect.
- **LLM non-determinism resolution.** Duplicate delivery of the same `(contract_id, correlation_id)` returns the cache; retry execution gets a new `correlation_id` so it can run fresh while remaining under the same contract. *Intentional* user re-execution allocates a new `contract_id` (matches `--force-rerun` semantics in [#512](https://github.com/Q00/ouroboros/issues/512) C5).

**Cache backend.** Filesystem-keyed under `.ouroboros/cache/contracts/<contract_id>/` so it aligns with Disposable Memory's content-addressed `artifact_ref` story. SQLite-backed alternatives are deferred until usage evidence demands them.

**Why this matters.** Replay from the journal is the *replay* verb in #476's north star. If FIFO per contract is not honored, replays diverge from the recorded run. If at-least-once is not honored, transient failures produce silent gaps. If idempotency is not enforced, retries produce duplicate work and user-visible non-determinism. All three exist together or not at all.

**Risks.**
- Idempotency cache vs. genuine re-execution intent. Mitigation: the rule "duplicate deliveries reuse the cache; timeout retries get a new `correlation_id`; user-requested reruns allocate a new `contract_id`" is documented in this RFC and enforced by the Disposable Memory replay default.
- Cache eviction during a long contract. Mitigation: cache lifetime = contract lifetime, not a TTL; eviction only happens on contract completion or cancellation.

### D7 — Timeout and failure: per-stage policy + Directive mapping

Configurable per stage via the binding table from [#519](https://github.com/Q00/ouroboros/issues/519):

```toml
[orchestrator.runtime_profile.stages.evaluate]
runtime = "claude_code"
timeout_ms = 90000
retry_budget = 2
on_timeout = "retry"        # "retry" or "cancel"
```

**Failure → Directive mapping.**

| Failure mode | Directive | Notes |
|---|---|---|
| Runtime exceeded `deadline_ms` | Coordinator emits `retry` with a new `correlation_id` while budget remains, then `cancel` | Budget owned by the existing resilience layer; runtime does not decide retry budget |
| Streamable-http connection drop while runtime remains alive | no new directive; reconnect via D6 | Coordinator replays cached prefix and resumes the in-flight stream for the same `(contract_id, correlation_id)` |
| Stdio EOF/pipe loss or runtime process crash / lost runtime identity | `cancel` immediately | Cooperative trust: do not assume retry safety after process death; this is not the D6 reconnect path |
| Schema validation failure on result envelope | `cancel` | Malformed envelope is not retryable |
| Runtime explicitly returned `wait` | propagate `wait` | External input dependency surfaced to operator |

The retry budget reuses the existing resilience layer's accounting — no new budget surface is introduced. Attempt fencing is Coordinator-owned and keyed by `(contract_id, correlation_id)` so FIFO per contract is preserved even if a timed-out runtime later writes to the old stream.

## Sequence diagram — one contract round trip

```mermaid
sequenceDiagram
    participant O as Orchestrator
    participant C as Coordinator (embedded)
    participant R as Runtime (e.g. codex_cli)
    participant E as EventStore

    O->>C: spawn(intent, AgentRuntimeContext)
    C->>R: POST /mesh/contract\nrequest envelope (contract_id, target=lineage/<id>, directive=evaluate)
    R-->>C: stream: llm.call.requested
    C->>E: append (target_type=lineage, target_id=<id>)
    R-->>C: stream: llm.call.returned (prompt_hash, completion_preview)
    C->>E: append
    R-->>C: stream: tool.call.started + tool.call.returned
    C->>E: append (×2)
    R-->>C: result envelope (status=ok, next_directive=converge, artifact_ref="sha256:...", runtime_events_emitted_count=4)
    C->>E: append (control.directive.emitted converge)
    C-->>O: AgentProcessHandle(replay-able by contract_id)
```

## Cross-RFC consistency

| Subject | Source | This RFC's behaviour |
|---|---|---|
| `contract_id = ULID` | this RFC, D2 | Inherited by [#513](https://github.com/Q00/ouroboros/issues/513) |
| `artifact_ref = "sha256:..."` | [#512](https://github.com/Q00/ouroboros/issues/512) C2 | Used in result envelope (D2) |
| Replay does not re-execute LLM calls | #476 M3 + #518 | Honored by D6 idempotency cache |
| `target_type` vocabulary | events/control.py (#492) | Open string with known values documented there; this RFC must not close the set |
| `policy.capabilities.changed` invariant | #476 Q4 | Reused verbatim in D5 |

## Pre-merge checklist

- [ ] All 7 decisions present, each with option, rationale, risks
- [ ] Envelope JSONC blocks lint as valid JSON when comments are stripped
- [ ] Sequence diagram renders (mermaid) and matches the textual description
- [ ] Cross-references resolve to existing issues / lines
- [ ] At least two maintainer approvals on the docs PR
- [ ] D6 sub-thread (idempotency cache backend) resolved; resolution captured here
- [ ] D2 `extra` slot governance rule explicitly stated
- [ ] `contract_id = ULID` matches what `contract-ledger.md` (#513) inherits
- [ ] `artifact_ref = "sha256:..."` matches what `disposable-memory.md` (#512) chose
- [ ] Failure → Directive mapping matches the body of [#518](https://github.com/Q00/ouroboros/issues/518) cancellation discipline

## Post-merge checklist

- [ ] `docs/rfc/mesh.md` reachable from the docs site (or the README index when it lands)
- [ ] Issue [#511](https://github.com/Q00/ouroboros/issues/511) closed with a back-link to this PR
- [ ] At least three Phase 3 implementation issues opened referencing this RFC by section (Coordinator service, ResultEvent envelope, runtime registration handshake)
- [ ] `policy.capabilities.changed` invariant from D5 confirmed against the existing dynamic-MCP code path with one manual smoke run

## Rollback

The deliverable is a docs PR with no runtime impact. Rollback = revert the docs PR. No data, schema, or behavior change to undo. The proposal comment in [#511](https://github.com/Q00/ouroboros/issues/511) remains as the working draft so subsequent attempts can iterate from the same starting point.
