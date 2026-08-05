---
name: evolve
description: "Start or monitor an evolutionary development loop"
---

# ooo evolve - Evolutionary Loop

## Description
Start, monitor, or rewind an evolutionary development loop. The loop iteratively
refines the ontology and acceptance criteria across generations until convergence.

## Flow
```
Gen 1: Seed(O₁) → Execute(all nodes) → Judge → Gate
Gen 2: Wonder(failed nodes) → Reflect(active nodes only) → Execute(active) → Gate(all)
Gen 3: Repeat with a smaller active set; frozen PASS nodes are reverified, not regenerated
...until the outcome gate passes, the working set stagnates, ontology converges,
or max 30 generations is reached
```

## Usage

### Start a new evolutionary loop
```
ooo evolve "build a task management CLI"
```

### Fast mode (ontology-only, no execution)
```
ooo evolve "build a task management CLI" --no-execute
```

### Check lineage status
```
ooo evolve --status <lineage_id>
```

### Record an isolated full-graph benchmark control
Call `ouroboros_evolve_step` for an existing Gen 2+ lineage with
`benchmark_control: true`, `execute: true`, and an explicit clean Git
`project_dir`. Use a distinct clean worktree at the treatment commit. Normal
evolve calls keep `benchmark_control` false and never launch a control arm.

### Rewind to a previous generation
```
ooo evolve --rewind <lineage_id> <generation_number>
```

## Instructions

### Load MCP Tools (Required before Path A/B decision)

The Ouroboros MCP tools are often registered as **deferred tools** that must be explicitly loaded before use. **You MUST perform this step before deciding between Path A and Path B.**

1. Use the active runtime's tool-discovery capability to find and load the evolve MCP tools:
   ```
   tool discovery query: "+ouroboros evolve"
   ```
2. The tools will typically be named with prefix `mcp__plugin_ouroboros_ouroboros__` (e.g., `ouroboros_evolve_step`, `ouroboros_interview`, `ouroboros_generate_seed`). After runtime tool discovery returns, the tools become callable.
3. If the tools are callable — already exposed, or loaded by discovery — proceed to **Path A**. An empty discovery result for already-exposed tools is expected, not a failure. Proceed to **Path B** only if they are genuinely absent (no Ouroboros MCP server).

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

**CRITICAL — deferred-schema guard (prevents "Invalid tool parameters"):**
This skill makes `ouroboros_*` MCP calls across multiple turns, and each turn runs
in a fresh tool context. A deferred tool's schema loaded on one turn is NOT
guaranteed to still be loaded on the next. If you call any `ouroboros_*` MCP tool
while its schema is not loaded in the **current** turn, the runtime rejects the
call with **"Invalid tool parameters"** before it ever reaches the server.
Therefore: **immediately before EVERY `ouroboros_*` MCP call in this skill, re-run
the tool-discovery load query for the specific MCP tool or documented tool family
you are about to call**. Use `"+ouroboros evolve"` for `ouroboros_evolve_step`,
`ouroboros_lineage_status`, and the evolve flow's documented tool family;
use `"+ouroboros interview"` before `ouroboros_interview`, `"+ouroboros seed"`
before `ouroboros_generate_seed`, and `"+ouroboros lateral"` before
`ouroboros_lateral_think`. If a load returns no matching tool (and the tool is not already callable — an empty load for an already-exposed tool is an expected no-op, not absence), switch to the
documented fallback / Path B instead of retrying the failing call.

### Path A: MCP Available (loaded via runtime tool discovery above)

**Starting a new evolutionary loop:**
1. Parse the user's input as `initial_context`
2. Run the interview: call `ouroboros_interview` with `initial_context`
3. Complete the interview (3+ rounds until ambiguity ≤ 0.2)
4. Generate seed: call `ouroboros_generate_seed` with the `session_id`
5. Call `ouroboros_evolve_step` with:
   - `lineage_id`: new unique ID (e.g., `lin_<seed_id>`)
   - `seed_content`: the generated seed YAML
   - `execute`: `true` (default) for full Execute→Evaluate pipeline,
     `false` for fast ontology-only evolution (no seed execution)
   - `benchmark_control`: `false` (default). Set `true` only for a deliberate
     Gen 2+ full-graph control in an explicit clean Git project/worktree.
6. Check the `action` in the response:
   - `continue` → Inspect `active_ac_indices`, then call `ouroboros_evolve_step`
     again with just `lineage_id`. Only active failed/reopened nodes evolve;
     frozen PASS nodes stay immutable and are boundary-reverified.
   - `ontology_stable` → This is not success. Call `ouroboros_evolve_step`
     again for the same `lineage_id` with `execute: true` so the stable Seed
     goes through Execute→Evaluate. Do not call standalone evaluate or report
     convergence before that step returns `converged`.
   - `converged` → Evolution complete! Display final ontology
   - `stagnated` → Ontology unchanged for 3+ gens. Consider `ouroboros_lateral_think`
   - `exhausted` → Max 30 generations reached. Display best result
   - `failed` → Check error, possibly retry. If the error reports an expired
     lineage owner, first confirm that the prior owner process is dead, then
     make one explicit recovery call with `recover_expired_claim: true`.
     Never set this flag for a merely slow or still-running owner.
7. **Repeat step 6** while action is `continue`. Treat `ontology_stable` as the
   explicit transition above, then process the `execute: true` response using
   step 6. Stop only on `converged`, `stagnated`, `exhausted`, or `failed`.
8. When the loop terminates, display a result summary with next step:
   - `converged`: `◆ Current state → next: Ontology converged! Run ooo evaluate for formal verification`
   - `ontology_stable`: `◆ Current state → next: Run the same lineage with execute=true for Execute→Evaluate; this is not verified convergence yet`
   - `stagnated`: `◆ Current state → next: ooo unstuck to break through, then ooo evolve --status <lineage_id> to resume`
   - `exhausted`: `◆ Current state → next: ooo evaluate to check best result — or ooo unstuck to try a new approach`
   - `failed`: `◆ Current state → next: Check the error above. ooo status to inspect session, or ooo unstuck if blocked`

**Checking status:**
1. Call `ouroboros_lineage_status` with the `lineage_id`
2. Display: generation count, ontology evolution, convergence progress

**Rewinding:**
1. Call `ouroboros_evolve_step` with:
   - `lineage_id`: the lineage to continue from a rewind point
   - `seed_content`: the seed YAML from the target generation
   (Future: dedicated `ouroboros_evolve_rewind` tool)

### Path B: Plugin-only (no MCP tools available)

If MCP tools are not available, explain the evolutionary loop concept and
suggest installing the Ouroboros MCP server. See [Getting Started](docs/getting-started.md) for install options, then run:

```
ouroboros mcp serve
```

Then add to your runtime's MCP configuration (e.g., `~/.claude/mcp.json` for Claude Code).

## Key Concepts

- **Wonder**: "What do we still not know?" - examines evaluation results
  to identify ontological gaps and hidden assumptions
- **Reflect**: "How should the ontology evolve?" - proposes specific
  mutations to fields, acceptance criteria, and constraints
- **Convergence**: Verified success requires the independently evaluated
  outcome to pass. In ontology-only mode, similarity ≥ 0.95 returns the
  non-success `ontology_stable` handoff and the same lineage must run once with
  `execute: true`. Judge-score plateau and the 30-generation cap are also
  non-success stops.
- **Focused evolution**: Gen 1 establishes the baseline. Gen 2+ derives an
  active node set from failed/regressed verifier results. Only those nodes are
  open to Wonder, Reflect, and execution; PASS nodes are frozen. The active
  output should shrink toward zero rather than feeding the full graph back.
- **Frugality proof**: A smaller active-node count is a working-set observation,
  not a savings claim. Gen 2+ records measured total-generation runtime tokens
  (Wonder, all Reflect attempts, validator/evaluator providers, executor AC
  attempts, dependency analysis, decomposition policy/attestation/repair,
  coordinator review, and shadow replay when enabled), wall time, calls, retries,
  and quality evidence. A PASS requires a paired full-graph control and
  focused treatment from distinct clean worktrees at the same Git commit, at
  least 10% fewer total-generation runtime tokens, and no final-gate,
  evaluation score/stage, drift, reward-hacking, per-AC verdict/score, lineage
  regression, or TraceGuard degradation. The comparison key covers the complete semantic Seed
  and previous evaluation. Every expected primary attempt needs exactly one
  runtime-token receipt and TraceGuard verdict bound to the exact session,
  primary dispatch, and root identity; every active root must be dispatched;
  every other generation provider call needs runtime usage; normalized
  backend/model/tier/mode/effort/permission and provider request configurations
  must be complete and match
  the same root AC or auxiliary phase role and preserve per-unit call multiplicity
  across the paired arms. Shared-unit sequences must be exactly equal; only whole
  control-only units may be removed. Blank/unknown phase roles are incomplete
  evidence. Full-graph controls must execute every Seed root; treatment
  active/frozen sets must exactly partition the Seed. Auxiliary tools,
  system-prompt identity, request kwargs, and fresh/scoped session mode are
  configuration-bound. Completion profiles are resolved once into a sealed
  dispatch config; only registered exact-key, single-attempt, secret-safe adapter
  attestations whose in-memory endpoint/credential authority reaches the actual
  call boundary unchanged are proof-eligible. Global or post-attestation mutable
  routing is ineligible. Malformed attestations invalidate the proof,
  not evolution. True resumed contexts without semantic identity and
  unknown/conflicting effective models or unreconciled usage counters are
  incomplete. Every current
  Seed AC needs exactly one final verdict against the same final semantic Seed
  contract. Missing, duplicate, malformed, partial, or opaque evidence is
  `insufficient_data`. Evidence reads and provider-call capture are capped;
  oversized or non-finite individual/subtotal/combined usage, cap overflow, and
  Wonder-only early stops also produce non-PASS durable receipts.
  Evolve never launches the control automatically because doing so would consume
  the production savings being measured.
- **Judge/Gate split**: Evaluation records score and evidence; deterministic
  convergence logic decides accept/continue/stagnate. A passing Gen 1 may end
  immediately—minimum-generation churn is not required after the gate passes.
- **No-drift stop**: If rejected evaluation scores move by less than 0.01 for
  the 3-generation window, stop as `stagnated` and hand off to `ooo unstuck`
  instead of spending the 30-generation cap.
- **Rewind**: Each generation is a snapshot. You can rewind to any
  generation and branch evolution from there
- **evolve_step**: Runs exactly ONE generation per call. Designed for
  Ralph integration — state is fully reconstructed from events between calls
- **execute flag**: `true` (default) runs full Execute→Evaluate each generation.
  `false` skips execution for fast ontology exploration. Previous generation's
  execution output is fed into Wonder/Reflect for informed evolution. An
  `ontology_stable` response from `execute: false` must be followed by the same
  lineage with `execute: true`; ontology stability alone is never convergence.
- **QA verdict**: Each generation's response includes a QA Verdict section
  (when `execute=true` and `skip_qa` is not set). Use the QA score to track
  quality progression across generations. Pass `skip_qa: true` to disable

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
