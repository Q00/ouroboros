---
name: evolve
description: "Start or monitor an evolutionary development loop"
---

# ooo evolve - Evolutionary Loop

Start, monitor, or rewind an evolutionary loop that refines the ontology and acceptance criteria across generations.

```
Gen 1: Seed → Execute(all ACs) → Judge → Gate
Gen 2+: Wonder(failed ACs) → Reflect(active ACs only) → Execute(active) → Gate(all)
...until the gate passes, the loop stagnates, or 30 generations are reached
```

## Usage

```
ooo evolve "build a task management CLI"              # start
ooo evolve "build a task management CLI" --no-execute # ontology-only (fast)
ooo evolve --status <lineage_id>                      # lineage status
ooo evolve --rewind <lineage_id> <generation_number>  # rewind
```

## Instructions

### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros evolve"` for `ouroboros_evolve_step`, `ouroboros_lineage_status`, and `ouroboros_evolve_rewind`; `"+ouroboros interview"` for `ouroboros_interview`; `"+ouroboros seed"` for `ouroboros_generate_seed`; `"+ouroboros lateral"` for `ouroboros_lateral_think`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. Use the fallback only when the tool is genuinely absent.

### Start a new loop

1. Run the interview: `ouroboros_interview` with the user's input as `initial_context`, until ambiguity ≤ 0.2.
2. Generate the Seed: `ouroboros_generate_seed` with the interview `session_id`.
3. Call `ouroboros_evolve_step` with a new `lineage_id` (e.g. `lin_<seed_id>`), `seed_content` (the Seed YAML), and `execute` (`true` by default for Execute→Evaluate each generation; `false` for `--no-execute`). Generation 2+ calls pass only `lineage_id`. Each call runs exactly one generation; state is rebuilt from events between calls.
4. Act on `action`:
   - `continue`: inspect `active_ac_indices`, then call again with `lineage_id`. Only failed or reopened ACs evolve; frozen PASS ACs are reverified, not regenerated.
   - `ontology_stable`: not success. Call again for the same lineage with `execute: true` and process that response; never report convergence before a `converged` action.
   - `converged`: the independently evaluated outcome passed. Show the final ontology.
   - `stagnated`: the ontology stayed unchanged, or rejected scores moved less than 0.01, across a 3-generation window. Hand off to `ooo unstuck` (`ouroboros_lateral_think`).
   - `exhausted`: the 30-generation cap was hit. Show the best generation.
   - `failed`: report the error. If it names an expired lineage owner, first confirm the prior owner process is dead, then make one call with `recover_expired_claim: true`; never set it for a slow or still-running owner.
5. Repeat while `continue`. With `execute: true` each generation already ran Execute→Evaluate, so never offer `ooo evaluate` with the `lineage_id`: formal evaluation takes an execution `session_id`, which the lineage id is not.

Executed generations include a QA verdict unless `skip_qa: true`; use its score to track quality across generations. A passing Gen 1 may converge immediately.

### Status

Call `ouroboros_lineage_status` with `lineage_id`; show generation count, ontology evolution, and convergence progress. This only reads status; it does not resume the loop.

### Rewind

Call `ouroboros_evolve_rewind` with `lineage_id` and `to_generation` (kept, inclusive; later generations are truncated). Continue by calling `ouroboros_evolve_step` with the same `lineage_id`.

### Benchmark control (explicit request only)

Never launch a full-graph control arm on your own: it would consume the savings being measured. Only when the user asks, call `ouroboros_evolve_step` for an existing Gen 2+ lineage with `benchmark_control: true`, `execute: true`, and an explicit clean Git `project_dir` in a distinct worktree at the treatment commit. A smaller active set is a working-set observation, not a savings claim; the frugality verdict comes from the server's receipts.

### Fallback (no Ouroboros MCP server)

Explain the evolutionary loop and suggest installing the MCP server (see [Getting Started](https://github.com/Q00/ouroboros/blob/main/docs/getting-started.md)), then running `ouroboros mcp serve --runtime claude-cli` and adding it to the runtime's MCP configuration.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
