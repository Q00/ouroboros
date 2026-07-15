# Ouroboros Synapse Implementation

> Started: 2026-07-12
> Branch: `feat/active-conductor-synapse`
> Worktree: `/Users/jaegyu.lee/Project/ouroboros-synapse`
> Status: implementation and final regression complete

## Summary

The clean-room Synapse implementation establishes a transport-neutral, durable
contract for directing bounded user intent to one exact AC runtime attempt. It
does not copy external framework code or terminology, and it does not claim that
existing runtimes can redirect an active turn.

Implemented:

- detached per-job ownership for every non-plugin Start* surface, so run, auto,
  evaluate, evolve, and Ralph survive the accepting stdio MCP turn;
- one-shot forced job identity when a detached worker re-enters the original
  Start* handler, while nested handoffs receive independent durable owners;
- cross-process cancellation delivery through persisted `cancel_requested`
  observation at the owning JobManager monitor;
- `SessionSignal` immutable contract and original Synapse vocabulary;
- independent runtime capability matrix with all fields defaulting to `False`;
- deterministic `redirect -> after_turn` fallback resolution;
- exact execution/scope/attempt identity and stable signal ID derivation;
- secret-shaped input rejection, UTF-8 byte bounds, expiry, and approval guards;
- eight durable lifecycle event factories, including provider-boundary
  `delivering`;
- legal-transition replay projection and source authority ordering;
- EventStore-based active-attempt resolver;
- durable requested/accepted/queued or rejected mailbox admission;
- user > conductor > worker pending-signal priority and supersession;
- consumption-time expiry rejection;
- restart replay for still-queued signals and terminal `delivery_uncertain` for
  signals claimed before process loss;
- shared in-process exact-attempt hub;
- same-session `inform` delivery that explicitly requests `tools=[]`, with
  bounded, secret-filtered replies and runtime-parameter degradation surfaced
  when a CLI cannot enforce an empty catalog natively;
- leader-driven resumable runtime `after_turn` delivery;
- tested `inform`/`after_turn` delivery for persisted `codex_cli`, Claude Agent SDK,
  persisted Claude MCP workers, OpenCode, Goose, and Pi;
- active-target discovery through `ouroboros_session_signal_targets`, including
  AC content and exact logical guards without provider-native session IDs;
- MCP handler registration that truthfully distinguishes queued from applied;
- runtime-message acknowledgement producing `applied` and `completed`;
- linked job-observer wakeups for queued, delivering, applied, rejected,
  delivery-uncertain, and completed signals;
- run/auto/ralph host guidance that reports Synapse state in the user's
  conversation language without claiming queued work was applied.

The MCP handler is registered in the server composition root. Unsupported
runtimes fail capability resolution; a resumable leader-driven runtime advertises
only capability-proven `inform_delivery`, `background_reply`, and
`after_turn_delivery`, never checkpoint redirect or hard replacement.

## Files created

| File | Purpose |
|---|---|
| `src/ouroboros/mcp/detached_jobs.py` | Private request handoff, process launch, and persisted acceptance boundary |
| `src/ouroboros/mcp/detached_worker.py` | Re-enters Start* under the accepted job ID and owns it through terminal state |
| `src/ouroboros/core/session_signal.py` | Signal, modes, sources, capabilities, bounds, digest, and mode resolution |
| `src/ouroboros/core/session_signal_projection.py` | Replay-safe lifecycle and authority projection |
| `src/ouroboros/events/session_signal.py` | Durable requested through completed event factories |
| `src/ouroboros/orchestrator/synapse.py` | Exact-attempt resolver and durable mailbox admission |
| `src/ouroboros/mcp/tools/synapse_handler.py` | Registered live-target discovery and signal-delivery MCP schemas |
| `docs/active-conductor/synapse-clean-room-spec.md` | Source-independent implementation input |
| `scripts/manual-synapse-smoke.py` | Reusable real-provider same-session delivery harness |
| `scripts/manual-synapse-state-smoke.py` | Deterministic expiry, priority, and durable-restart harness |

## Files modified

| File | Change |
|---|---|
| `src/ouroboros/orchestrator/adapter.py` | Adds independent `session_signals` capabilities and enables tested Claude Agent SDK delivery |
| `src/ouroboros/orchestrator/worker_runtime.py` | Enables `after_turn` only for targeted-resume leader workers |
| `src/ouroboros/orchestrator/codex_cli_runtime.py` | Enables tested persisted-thread `after_turn` delivery for Codex CLI |
| `src/ouroboros/orchestrator/opencode_runtime.py` | Enables tested OpenCode exact-session `after_turn` delivery |
| `src/ouroboros/orchestrator/goose_runtime.py` | Persists a stable AC-scoped Goose session name and enables tested resume delivery |
| `src/ouroboros/orchestrator/pi_runtime.py` | Enables tested Pi exact-session `after_turn` delivery |
| `src/ouroboros/orchestrator/hermes_runtime.py` | Reads Hermes 0.11 quiet-mode session markers from stderr without claiming resume support before a full live proof |
| `src/ouroboros/orchestrator/parallel_executor.py` | Registers active attempts and resumes queued signal turns |
| `src/ouroboros/orchestrator/runner.py` | Threads the shared Synapse hub into AC execution |
| `src/ouroboros/orchestrator/agent_runtime_context.py` | Shares one hub between MCP admission and runtime dispatch |
| `src/ouroboros/mcp/server/adapter.py` | Constructs and registers the Synapse vertical slice |
| `src/ouroboros/mcp/job_manager.py` | Records process ownership, observes external cancellation, and protects live external holders |
| `src/ouroboros/mcp/tools/background.py` | Transfers every non-plugin Start* request to its detached owner and preserves nested handoffs |
| `src/ouroboros/cli/commands/mcp.py` | Restores every whitelisted provider environment needed by mixed-backend detached workers |
| `src/ouroboros/mcp/tools/job_handlers.py` | Streams linked Synapse lifecycle events and wakes attention-aware waits |
| `src/ouroboros/mcp/tools/job_observer.py` | Makes the delegated observer own linked attention-or-progress polling |
| `src/ouroboros/core/__init__.py` | Lazily re-exports the Synapse core contract |
| `src/ouroboros/mcp/tools/__init__.py` | Exposes the handler class without registering the tool |
| Run/auto/ralph skills | Discovers and semantically selects the relevant live AC, then relays truthful Synapse state |
| Active Conductor RFC/docs | Renames the subsystem and records clean-room boundaries and implementation status |

## Current flow

```text
human intent in the main session
  -> Start* accepts a stable job ID
  -> detached worker records itself as the job owner
  -> the accepting MCP turn may end without interrupting execution
  -> discover live targets for the observed execution
  -> main session semantically selects the relevant AC
  -> MCP arguments with the selected exact logical guards
  -> SessionSignal validation
  -> deterministic signal ID
  -> requested event
  -> exact active-attempt resolution
  -> capability + explicit fallback resolution
  -> accepted + queued events, or terminal rejected event
  -> current runtime turn completes
  -> delivering records the provider handoff boundary
  -> same provider session resumes with a tools=[] inform or additive intent turn
  -> runtime messages prove applied + completed
  -> linked job observer wakes and relays the exact lifecycle state
```

`queued` still means only that Ouroboros durably owns pending delivery;
`delivering` means the runtime claimed it but application is not yet proven. A
leader-driven resumable runtime emits `applied` only after the resumed signal
turn produces runtime messages, then emits `completed`. A failure across that
boundary emits `delivery_uncertain` and is not automatically resent.

`tools=[]` does not remove tools from implementation AC workers. The existing
server composition uses an empty tool envelope for pure LLM stages (interview,
semantic evaluation, and reflection), while only the interviewer also receives
the special prompt variant that suppresses tool-use cues. Normal AC execution
still receives its implementation tools. Synapse `inform` uses a separate
intentional `tools=[]` follow-up so a bounded read-only acknowledgement cannot
edit artifacts; the live Codex proof observed normal Edit/Bash use in the AC
turn and zero tool messages in the Synapse reply turn.

## Validation

- The complete post-worker suite passed with `12,273 passed, 5 skipped` in
  227.45 seconds. Focused coverage includes parent-process exit, cross-process
  cancellation, nested durable ownership, target discovery, the full
  `inform`/`after_turn` resume path, priority, admission and consumption expiry,
  restart recovery, error-envelope uncertainty, backend capability guards, and
  linked observer wakeup.
- Ruff lint passed, and Ruff confirmed all 1,076 checked Python files were
  formatted. Mypy passed across all 446 `src/ouroboros` source files.
- A real `codex_cli` persisted thread passed the manual discovery → queued
  signal → same-thread resume flow, including one native session ID across both
  turns, the full requested → completed lifecycle, a bounded reply marker, and
  zero observed tool messages.
- A two-candidate manual routing check selected the checkout-copy AC from Korean
  human intent and queued only that exact attempt; the unrelated database AC
  received nothing.
- A fresh Codex main-session run proved the complete cross-turn path on
  2026-07-14: the initiating Codex turn ended; a detached worker remained live;
  a resumed Codex turn reconstructed two active ACs from EventStore and selected
  only the checkout AC from Korean intent; the signal completed through
  requested → accepted → queued → delivering → applied → completed with a
  bounded reply; the database AC received no signal; both AC artifacts and the
  job completed without any `mcp.job.interrupted` event. A later parent turn
  caught up the completion and rendered it in Korean.
- A live Codex `start_auto` resume also outlived its accepting turn under a
  detached owner, completed the interview/Seed/Seed-QA path, and terminated as
  a truthful QA `blocked` result without `mcp.job.interrupted`.
- Codex's automatic per-cwd `trust_level` bookkeeping is excluded from runtime
  drift fingerprints; project model/profile overrides remain guarded.
- Claude Agent SDK and the persisted Claude worker were re-run on 2026-07-14 and
  each passed live target discovery → queued signal → same-native-session
  follow-up → `applied` → `completed`, with zero tool messages. OpenCode 1.14.19,
  Goose 1.38.0, and Pi 0.78.0 retain their earlier live proof of the same path;
  the latest local refresh could not produce provider output for OpenCode/Pi,
  while Goose explicitly reported that no provider was configured. Those are
  deployment-readiness failures, not transport capability upgrades or silent
  success. Runtime contract tests for all three still pass, and the earlier
  live proofs observed the second-turn marker with zero tool messages.
- Pi also passed an explicit `redirect` request that truthfully resolved to the
  declared `after_turn` fallback. No tested runtime advertises checkpoint
  redirect, owned-turn abort, or replacement resume.
- Goose's first live proof exposed a false resume claim: the CLI generated a new
  name on the signal turn because current stream output does not echo the `-n`
  value. The runtime now derives one stable name from the exact AC attempt and
  the repeated live proof used the same name with `--resume`.
- Hermes 0.11 was corrected to read its quiet-mode `session_id` marker from
  stderr. The installed Hermes build produced a marker but did not index that
  session in its resume store, so its Synapse capability remains disabled. The
  manual harness confirmed requested → rejected with every SessionSignal
  capability left `false`.
- The deterministic manual state harness passed against the production
  EventStore, mailbox, hub, replay projection, and AC delivery boundary:
  consumption-time expiry produced `expired_before_delivery` without a second
  provider turn; a user signal superseded a pending conductor signal and blocked
  a worker signal; closing and reopening SQLite replayed a still-queued signal;
  and a claimed-before-restart signal became terminal `delivery_uncertain` with
  automatic retry disabled.

## RFC acceptance audit

| # | Completion evidence | Result |
|---:|---|---|
| 1 | Recovery, verdict, Seed-QA, frugality, stagnation, and Synapse producers emit typed durable events; classifier tests cover every trigger. | Passed |
| 2 | `attention_or_ac_change` and linked-stream JobManager tests prove wake, timeout, paging, and unrelated-event suppression. | Passed |
| 3 | `attention_relay.py` emits bounded discriminated envelopes; malformed evidence and action-menu tests fail closed. | Passed |
| 4 | Action menus are generated only from registered tool availability and declare dynamic host inputs. | Passed |
| 5 | Root and packaged run/auto/ralph guidance requires one short-lived read-only verifier and forbids ACT when unavailable. | Passed |
| 6 | Conductor mutation validates durable `engine_ownership_state="closed"`; Synapse uses its separate exact-attempt capability contract. | Passed |
| 7 | Intermediate progress and unchanged polling windows never produce mutating menus in classifier tests. | Passed |
| 8 | Stable `semantic_ac_key` propagation and rejected-verdict grouping carry bounded reasons into corrective directives. | Passed |
| 9 | Run requires approval for specification-changing successors; Auto/Ralph accept only bounded non-relaxing autonomous directives. | Passed |
| 10 | Selected, completed, failed, and declined conductor decisions are idempotently persisted and tested. | Passed |
| 11 | `job_observer` owns the only cursor; artifact and dispatch tests preserve OpenCode plugin behavior. | Passed |
| 12 | Root skills, `.claude-plugin` copies, Codex guidance, runtime guides, and artifact tests are coherent. | Passed |
| 13 | Execute and Auto contracts persist, forward, and restore `efficiency_mode` and `frugality_assurance`. | Passed |
| 14 | English canonical guidance presents user outcomes and is phrased by the host in the active conversation language; no locale catalog exists. | Passed |
| 15 | Strict assurance and shadow replay require separate explicit authorization and safety attestations. | Passed |
| 16 | Start, configuration, Discover, plan, routing, level, verification, attention, and terminal relays are implemented and host-guided. | Passed |
| 17 | `execution.plan.created` contains total levels and first ACs and is persisted before level 1 starts. | Passed |
| 18 | Current model/tier and harness transitions come from #1601/#1602-compatible events and are materially deduplicated. | Passed |
| 19 | Discover targets/purpose are UTF-8 bounded, materially deduplicated, and exclude commands, tool output, and reasoning. | Passed |
| 20 | On-demand Synapse target discovery and `inform` do not call or advance the observer wait cursor. | Passed |
| 21 | Execution, logical scope, unique attempt, terminal, stale, and contract-generation guards fail closed. | Passed |
| 22 | `inform`, `after_turn`, `redirect`, and `replace` resolve against independent capabilities; resume alone grants none. | Passed |
| 23 | MCP text/meta and observer relays distinguish queued/delivering from runtime-acknowledged applied/completed. | Passed |
| 24 | Pi live proof confirmed explicit `redirect -> after_turn`; unsupported redirect without fallback is rejected. | Passed |
| 25 | User > conductor > worker priority is durable; one-AC specification changes are rejected. | Passed |
| 26 | Bounds, secret filtering, digest audit, idempotency, legal replay, expiry, and no-transcript persistence are tested. | Passed |
| 27 | SessionSignal lifecycle events use the existing linked observer stream and direct commands create no polling owner. | Passed |
| 28 | Runtime contract tests plus live Codex, Claude SDK/worker, OpenCode, Goose, Pi, and Hermes proofs establish truthful support/degradation. | Passed |
| 29 | Provider-boundary failures and claimed-before-restart state become terminal `delivery_uncertain` with automatic retry disabled. | Passed |
| 30 | Live specification change is rejected and the conductor successor path requires matching user approval for a shared contract change. | Passed |
| 31 | Detached-worker integration tests prove parent exit survival, cross-process cancellation, and independent nested ownership; the live Codex run contains no shutdown interruption. | Passed |
| 32 | Observer contracts and skills distinguish confirmed live relay from durable catch-up; a resumed Codex parent turn rendered the missed completion in Korean. | Passed |

## Clean-room external review

Read-only review of `code-yeongyu/oh-my-openagent` and
`code-yeongyu/lazycodex` informed behavioral requirements only. No source,
external API vocabulary, event names, or wire formats were copied.

Useful independent conclusions:

- OpenCode's server/SDK session surface confirms that session creation, prompt
  dispatch, status observation, and event observation should remain separate
  concerns. The current CLI transport is now proven; a future long-lived server
  transport may reduce process overhead without changing Synapse contracts.
- Parent wakeups should be deferred while the parent is active, and duplicate
  delivery should be guarded by durable turn identity. Synapse already expresses
  those requirements through exact attempt IDs, idempotency keys, and after-turn
  queueing.
- Mailbox ownership should distinguish unread work, claimed work awaiting
  acknowledgement, and terminal processed work. Synapse's
  `queued`/`applied`/`completed`/`delivery_uncertain` lifecycle keeps that
  distinction explicit.
- LazyCodex's strongest relevant pattern is plugin-scoped, on-demand MCP plus
  thin lifecycle hooks with health diagnostics. It does not provide a session
  transport replacement; Synapse remains runtime-neutral and MCP remains the
  main-session control surface.

## Known limitations

- No checkpoint redirect or owned replacement is enabled.
- Non-resumable runtimes advertise no Synapse support.
- Process-bound `codex_mcp` workers remain unsupported; persisted `codex_cli`
  threads support real `after_turn` delivery.
- Hermes remains disabled until its emitted session ID can be resumed in the
  same installed environment; an advertised CLI flag alone is not sufficient.
- Exact active model disclosure begins with configuration/routing events rather
  than a start-time guess.

## Deferred capability extensions

Provider-specific checkpoint `redirect` or hard `replace` can be added later
only where a transport exposes a tested interruption or owned-abort boundary.
Their absence does not weaken the completed `inform`/`after_turn` slice because
unsupported modes are capability-disabled and never reported as applied.
