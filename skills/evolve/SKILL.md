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

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded via the configured MCP server. Skip directly to **Path A**.

**If `ToolSearch` is available** (Claude Code): MCP tools may be registered as deferred tools that must be explicitly loaded.
1. Use the `ToolSearch` tool to find and load the evolve MCP tools:
   ```
   ToolSearch query: "+ouroboros evolve"
   ```
2. If ToolSearch finds the tools → proceed to **Path A**. If not → proceed to **Path B**.

### Path A: Native Execution

**Architecture**:

```
ouroboros_evolve_step (synchronous, one generation at a time)
  ←→ You (main — thin orchestrator, loop until converged)
      ←→ @socratic-interviewer   # bootstrap only
      ←→ @seed-architect         # bootstrap only
```

**Starting a new evolutionary loop:**
1. Parse the user's input as `initial_context`
2. Start an interview session with `ouroboros_interview(action="start", initial_context=<initial_context>)`.
3. Run the interview using the same native pattern as `skills/interview`: spawn `ouroboros:socratic-interviewer` with `session_id`; the subagent only reads `action="state"` and persists `action="score"`; the main session routes the question to the user and records the full turn with `action="record_turn"` until `seed_ready: true`.
4. Generate seed via `ouroboros:seed-architect` with `session_id` and record the returned `seed_id`.
5. Run Gen 1 with `ouroboros_evolve_step` (synchronous — no job polling needed):
   ```
   Tool: ouroboros_evolve_step
   Arguments:
     lineage_id: lin_<seed_id>   # new unique lineage ID
     seed_id: <seed_id>
     execute: true               # false for --no-execute / fast mode
   ```
   Returns the generation result directly. No job_wait or job_result needed.
6. Check the `action` in the result:
   - `continue` → first call `ouroboros_lineage_status(lineage_id=<lineage_id>)`, then call `ouroboros_evolve_step(lineage_id=<lineage_id>)` for the next generation
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
2. Treat `ouroboros_lineage_status` as the source of truth for current generation count, ontology evolution, and convergence progress

**Rewinding:**
1. Call `ouroboros_evolve_rewind(lineage_id=<lineage_id>, to_generation=<n>)`
2. After rewind, confirm the new state with `ouroboros_lineage_status`

**Context minimization** (CRITICAL):
- Do NOT inline full seed YAML into the main session if you already have `seed_id`
- Prefer `ouroboros_evolve_step(seed_id=...)` for Gen 1 over copying large `seed_content`
- Treat `ouroboros_lineage_status` as the stage-local source of truth
- Do NOT manually replay lineage events in the main session

### Retry Rule

If `ouroboros_evolve_step` fails:

1. Retry once with the same arguments.
2. If it fails again, fall back to **Path A Fallback** (background job).

### Path A Fallback: Background Job Execution

If `ouroboros_evolve_step` fails twice, fall back to background execution:

1. **Start background generation**:
   ```
   Tool: ouroboros_start_evolve_step
   Arguments:
     lineage_id: <lineage_id>
     seed_id: <seed_id>          # Gen 1 only
     execute: <bool>
   ```
   Returns `job_id`.

2. **Poll for completion**:
   ```
   loop:
     Tool: ouroboros_job_wait
     Arguments:
       job_id: <job_id>
       cursor: <cursor from previous response, starts at 0>
       timeout_seconds: 60
     # Continue until status is "completed", "failed", or "cancelled"
   ```

3. **Fetch result**:
   ```
   Tool: ouroboros_job_result
   Arguments:
     job_id: <job_id>
   ```

4. Process result action the same as Path A step 6, using `ouroboros_start_evolve_step` for subsequent generations.

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
