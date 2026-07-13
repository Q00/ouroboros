# RFC — Active conductor: judgment on the event-driven main session

> Status: Proposed
> Scope: `ooo run`, `ooo auto`, `ooo ralph` host skills; MCP attention classification
> Builds on: [delegated-job-observer](delegated-job-observer.md), [frugality-proof-producers](frugality-proof-producers.md)

## Problem

#1599 (delegated-job-observer) made the main session event-driven. An observer
child owns the `job_wait` cursor exclusively and relays only four categories to
the parent — `phase_changed`, `progress_advanced`, `attention_required`,
`terminal` (`mcp/tools/job_observer.py:58-61`). By design the parent's declared
`main_session_policy` is `start_and_on_demand_only`
(`mcp/tools/job_observer.py:53`): it is a passive spectator.

That is the right split for *observation*, but it left *judgment* unowned. When
an AC fails after retry exhaustion, a TraceGuard deliver verdict rejects the same
lineage repeatedly, or the frugality proof returns a FAIL status, nothing with
semantic judgment reacts. The engine's deterministic recovery is the ceiling:
reasoning-effort raise at `EFFORT_RAISE_RETRY_THRESHOLD = 2`
(`orchestrator/effort_routing.py:38`), and progressive model-tier raises at or
after the configured escalation retry threshold (`orchestrator/model_routing.py`,
`raise_one_notch`). Top-level and untrusted decomposed work start at `standard`;
only an explicitly trusted child may start at `frugal`. Once the configured retry
budget, including stronger-model attempts where available, is exhausted, the run
simply reports failure. The proven pattern for lifting that ceiling is a conductor
main loop: woken by events, it re-verifies claims against ground truth and issues
corrective, context-rich directives to workers.

## Decision

Make the main session an **active conductor** in three layers, preserving the
observer split — the observer owns the cursor; the conductor owns judgment. The
conductor never re-drives what the engine already retries.

### L1 — server: attention classification with action menus

The MCP job event summarizer classifies events into `attention_required` for a
fixed set of judgment-demanding conditions, and embeds a
`recommended_host_actions` menu on each such event. This generalizes the
existing singular `recommended_host_action="spawn_observer_session"`
(`mcp/tools/job_observer.py:32`) into an ordered list of concrete MCP tool-call
templates — structured meta the host executes verbatim, never prose the host
must reconstruct. This is the same SOL-compatibility rationale the observer RFC
gives: tool names and arguments live in MCP meta so the orchestrating model does
not rebuild them from surrounding skill prose, and one explicit act replaces a
multi-turn reconstruction.

Attention triggers (all sensed from events already emitted on this branch):

| Trigger | Source event | Sensed field |
|---|---|---|
| AC terminal failure after retry exhaustion | latest failed `execution.ac.outcome_finalized` plus terminal execution state | `success=false`, `retry_attempt`, and no further engine-owned retry/redispatch |
| One-notch model escalation attempted and still failing | `execution.ac.model_routed` + matching `execution.ac.outcome_finalized` | `retry_attempt` at/above the resolved escalation threshold, raised `model_tier`, `success=false` |
| deliver_verdict rejected streak (≥2 for one AC lineage) | `execution.ac.deliver_verdict` | `rejected_reasons`, `traceguard_verdict` (`execution_event_emitter.py:434-467`) |
| Frugality proof FAIL | `execution.frugality_proof.evaluated` | `status` ∈ `fail_grounding_regression`, `fail_no_frugality` (`runner.py:871`, `frugality_proof.py:80-81`) |
| Seed-QA blocked | seed-QA gate block event | gate status |
| Stagnation | progress-stall signal | unchanged progress across bounded window |

Each `recommended_host_actions` entry is a template of `{tool, arguments,
rationale}` drawn from the conductor control surface (below). The menu is
*ordered by minimal intervention* — verify-only first, spec-changing last.
Non-attention events carry no menu.

### L2 — skill: triage playbook

`run`, `auto`, and `ralph` SKILL.md gain a conductor section executed on each
relayed `attention_required` event. Four steps:

1. **VERIFY** — never act on a worker's claim alone. Spawn one cheap, read-only
   host subagent (the host-native Agent tool, the only surface with inline
   visibility) to check actual state: the files on disk, the `deliver_verdict`
   `rejected_reasons`, the `frugality_proof.evaluated` `reason`. The worker
   reports what it believes; the conductor confirms against ground truth.
2. **DECIDE** — the minimal intervention that unblocks. Prefer the earliest
   entry in `recommended_host_actions` that the verification supports.
3. **ACT** via the MCP control surface: `ouroboros_lateral_think` injection, a
   spec amendment + redispatch, a model-tier pin, `ouroboros_cancel_execution`,
   or explicit user escalation.
4. **LOG** the decision as an event so the intervention trail is auditable
   alongside the run's own events.

Non-attention events: no action, no tokens. S2 is inert without S1 — with no
`recommended_host_actions` on the relayed event, the playbook has nothing to
triage.

### L3 — autonomy loop (auto/ralph only)

In `auto` and `ralph` the playbook may ACT without user confirmation on
deterministic triggers. Example: a repeated deliver-verdict rejection — the
conductor reads the event's `rejected_reasons`, composes a corrective
instruction naming the unsupported claims, and injects it into the next
`ouroboros_evolve_step` / redispatch. `ooo run` is interactive: it stops at
user escalation for anything spec-changing, and only auto-acts on
non-spec-changing unblocks (e.g. a tier pin).

## Division of labor (hard rule)

Deterministic recovery belongs to the engine and stays there — retries,
reasoning-effort raise (`effort_routing.py`), model-tier escalation
(`model_routing.py`). The conductor owns only what the engine cannot:

- semantic interpretation of **why** something keeps failing;
- spec-level amendments;
- goal-drift judgment;
- user escalation.

The conductor never re-drives an AC the engine is still retrying. Double-driving
burns tokens and races the engine's own escalation ladder. The conductor engages
only *after* the configured retry budget is closed and no engine-owned
same-runtime or alternate-harness redispatch remains. Reaching `frontier` is
neither required nor sufficient: an untrusted decomposed child starts at the base
tier, while an explicitly trusted child may start one tier lower and need an
additional retry to reach `frontier`. Current live decomposition supplies no trust
issuer. A top-level AC may reach `frontier` before its retry budget is closed.

## Frugality coupling

The conductor is the most expensive context in the system, so the design spends
it only at attention moments — never on `phase_changed` / `progress_advanced`
heartbeats. Verification is delegated to cheap read-only subagents, not done in
the conductor's own context. The event vocabulary shipped on this branch is
precisely the conductor's sensor suite:

- `execution.ac.model_routed` (`model_tier`, `retry_attempt`)
- `execution.ac.token_attribution.reported`
  (`frugality_proof.py:66`, `EVENT_TOKEN_ATTRIBUTION`)
- `execution.ac.deliver_verdict`
  (`frugality_proof.py:67`, `EVENT_DELIVER_VERDICT`; `rejected_reasons`)
- `execution.frugality_proof.evaluated` (`status`, `reason`)

No new sensor is invented — the conductor reads what the frugality machine
already produces.

## Non-goals

- No MCP server push. The observer still long-polls (`job_wait`); the conductor
  reacts to relayed events, not to a pushed stream.
- No new daemon. The conductor is the existing main session, not a process.
- No change to OpenCode plugin mode — execution there belongs to a plugin child
  and returns no pollable job ID, so there is no observer/conductor split to add.
- No conductor writes into the active run workspace without the overlap check
  the observer RFC already defines. Verification subagents are read-only.

## Implementation slices

- **S1** — attention classification + `recommended_host_actions` in the job
  event summarizer (server). Independently shippable and testable against the
  fixed event contract above.
- **S2** — the conductor playbook sections in `run`/`auto`/`ralph` SKILL.md.
  Inert without S1.
- **S3** — `auto`/`ralph` autonomous triggers + the decision-log event.

Each slice ships independently.

## Acceptance criteria

1. The summarizer emits `attention_required` with a non-empty
   `recommended_host_actions` menu for each L1 trigger, and no menu on
   `phase_changed` / `progress_advanced`.
2. `recommended_host_actions` entries are structured `{tool, arguments,
   rationale}` templates using real `ouroboros_*` tool names — no prose.
3. The playbook VERIFYs via a read-only host subagent before any ACT, and never
   acts on a worker claim alone.
4. The conductor never redispatches an AC while the engine still owns a configured
   retry, the one-notch effort/model raise, or an alternate-harness redispatch.
5. In `auto`/`ralph` a repeated `deliver_verdict` rejection produces a corrective
   injection composed from `rejected_reasons`; in `ooo run` a spec-changing
   intervention stops at user escalation.
6. Every conductor decision is logged as an event.
