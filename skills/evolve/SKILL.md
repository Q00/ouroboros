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
Gen 1: Interview → Seed(O₁) → Execute → Evaluate
Gen 2: Wonder → Reflect → Seed(O₂) → Execute → Evaluate
Gen 3: Wonder → Reflect → Seed(O₃) → Execute → Evaluate
...until ontology converges (similarity ≥ 0.95) or max 30 generations
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

### Rewind to a previous generation
```
ooo evolve --rewind <lineage_id> <generation_number>
```

## Instructions

### Load MCP Tools (Required before Path A/B decision)

The Ouroboros MCP tools are often registered as **deferred tools** that must be explicitly loaded before use. **You MUST perform this step before deciding between Path A and Path B.**

1. Use the `ToolSearch` tool to find and load the evolve MCP tools:
   ```
   ToolSearch query: "+ouroboros evolve"
   ```
2. The tools will typically be named with prefix `mcp__plugin_ouroboros_ouroboros__` (e.g., `ouroboros_evolve_step`, `ouroboros_interview`, `ouroboros_generate_seed`). After ToolSearch returns, the tools become callable.
3. If ToolSearch finds the tools → proceed to **Path A**. If not → proceed to **Path B**.

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

### Path A: MCP Available (loaded via ToolSearch above)

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
6. Check the `action` in the response:
   - `continue` → Call `ouroboros_evolve_step` again with just `lineage_id`
   - `converged` → Evolution complete! Display final ontology
   - `stagnated` → Ontology unchanged for 3+ gens. Consider `ouroboros_lateral_think`
   - `exhausted` → Max 30 generations reached. Display best result
   - `failed` → Check error, possibly retry
7. **Repeat step 6** until action ≠ `continue`
8. When the loop terminates, display a result summary with next step:
   - `converged`: `📍 Next: Ontology converged! Run ooo evaluate for formal verification`
   - `stagnated`: `📍 Next: ooo unstuck to break through, then ooo evolve --status <lineage_id> to resume`
   - `exhausted`: `📍 Next: ooo evaluate to check best result — or ooo unstuck to try a new approach`
   - `failed`: `📍 Next: Check the error above. ooo status to inspect session, or ooo unstuck if blocked`

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
suggest installing the Ouroboros MCP server:

```
pip install ouroboros-ai
ouroboros mcp serve
```

Then add to Claude Code's MCP configuration.

## Key Concepts

- **Wonder**: "What do we still not know?" - examines evaluation results
  to identify ontological gaps and hidden assumptions
- **Reflect**: "How should the ontology evolve?" - proposes specific
  mutations to fields, acceptance criteria, and constraints
- **Convergence**: Loop stops when ontology similarity ≥ 0.95 between
  consecutive generations, or after 30 generations max
- **Rewind**: Each generation is a snapshot. You can rewind to any
  generation and branch evolution from there
- **evolve_step**: Runs exactly ONE generation per call. Designed for
  Ralph integration — state is fully reconstructed from events between calls
- **execute flag**: `true` (default) runs full Execute→Evaluate each generation.
  `false` skips execution for fast ontology exploration. Previous generation's
  execution output is fed into Wonder/Reflect for informed evolution
- **QA verdict**: Each generation's response includes a QA Verdict section
  (when `execute=true` and `skip_qa` is not set). Use the QA score to track
  quality progression across generations. Pass `skip_qa: true` to disable
