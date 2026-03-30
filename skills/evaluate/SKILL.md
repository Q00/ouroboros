---
name: evaluate
description: "Evaluate execution with three-stage verification pipeline"
---

# /ouroboros:evaluate

Evaluate an execution session using the three-stage verification pipeline.

## Usage

```
ooo evaluate <session_id> [artifact_path]
/ouroboros:evaluate <session_id> [artifact_path]
```

**Trigger keywords:** "evaluate this", "3-stage check"

## Instructions

When the user invokes this skill:

### Load MCP Tools

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded. Skip directly to Evaluation Steps.

**If `ToolSearch` is available** (Claude Code):
1. Use ToolSearch to load the evaluate MCP tool:
   ```
   ToolSearch query: "+ouroboros evaluate"
   ```
2. If tools not found → skip to **Fallback** section.

### Evaluation Steps

The original Python core still keeps `Stage 1 -> Stage 2 -> Stage 3 consensus` behavior. The native path here only separates orchestration into `state + record`.

**Architecture**:

```
ouroboros_evaluate (state + record)
  ←→ You (main — thin orchestrator)
      ←→ @evaluator
      ←→ @consensus-reviewer × 3
```

1. **Resolve evaluation state** via MCP:
   ```
   Tool: ouroboros_evaluate
   Arguments:
     action: state
     session_id: <session_id>
     # optional artifact_path: <user supplied path>
   ```
   Returns: goal, acceptance_criteria, constraints, cwd, artifact_path.

2. **Stage 1+2 — Spawn @evaluator**:
   ```
   Tool: Agent
     subagent_type: ouroboros:evaluator
     description: "Stage 1+2 evaluation"
     prompt: |
       session_id: <session_id>
       artifact_path: <artifact_path from step 1>
   ```
   Returns JSON: `{stage1, stage2, needs_consensus}`

3. **If `stage1.passed == false`**:
   ```
   Tool: ouroboros_evaluate
   Arguments:
     action: record
     session_id: <session_id>
     agent_verdict: <JSON from evaluator>
   ```
   Present REJECTED at Stage 1 and stop.

4. **If `needs_consensus == false` AND `stage2.score >= 0.8`**:
   ```
   Tool: ouroboros_evaluate
   Arguments:
     action: record
     session_id: <session_id>
     agent_verdict: <JSON from evaluator>
   ```
   Present APPROVED and stop.

5. **Stage 3 — Simple 3-vote consensus**:

   Spawn `@consensus-reviewer` **three times in ONE message** (parallel, independent votes):

   ```
   [Single message — 3 parallel Agent calls]

   Tool: Agent  (reviewer 1)
     subagent_type: ouroboros:consensus-reviewer
     description: "Consensus vote 1"
     prompt: |
       session_id: <session_id>
       artifact_path: <artifact_path from step 1>
       reviewer_id: reviewer_1
       stage2_summary: <stage2 JSON from evaluator>

   Tool: Agent  (reviewer 2)
     subagent_type: ouroboros:consensus-reviewer
     description: "Consensus vote 2"
     prompt: |
       session_id: <session_id>
       artifact_path: <artifact_path from step 1>
       reviewer_id: reviewer_2
       stage2_summary: <stage2 JSON from evaluator>

   Tool: Agent  (reviewer 3)
     subagent_type: ouroboros:consensus-reviewer
     description: "Consensus vote 3"
     prompt: |
       session_id: <session_id>
       artifact_path: <artifact_path from step 1>
       reviewer_id: reviewer_3
       stage2_summary: <stage2 JSON from evaluator>
   ```

   If the runtime supports per-agent model pinning, assign three different frontier models here. If it does not, still keep the shape as three isolated independent votes so the majority-vote contract stays aligned with the original Stage 3.

6. **Aggregate the votes inline**:

   Build a compact JSON bundle:
   ```json
   {
     "stage2": <stage2 JSON from evaluator>,
     "stage3": {
       "approved": true,
       "majority_ratio": 0.67,
       "total_votes": 3,
       "approving_votes": 2,
       "votes": [<vote1>, <vote2>, <vote3>]
     }
   }
   ```

   Use a simple majority rule matching the original consensus flow:
   - `approved = approving_votes >= 2`
   - `majority_ratio = approving_votes / 3`

7. **Record final verdict** via MCP:
   ```
   Tool: ouroboros_evaluate
   Arguments:
     session_id: <session_id>
     action: record
     agent_verdict: <aggregated stage2 + stage3 JSON bundle>
   ```

8. **Present result**:
   ```
   APPROVED | Stage <N> passed ✅
   Score: <score> | AC: <n>/<total> | Drift: <drift>
   Stage 1: build ✅  tests <n>/<total> ✅
   Stage 2: score <x> (sonnet)
   Stage 3: 2/3 reviewers approved ✅
   ```
   - **APPROVED**: `📍 Done! Optional: ooo evolve to iteratively refine`
   - **REJECTED at Stage 1**: `📍 Next: Fix failures → ooo evaluate — or ooo ralph`
   - **REJECTED at Stage 2/3**: `📍 Next: ooo run to re-execute — or ooo evolve`

**Context minimization** (CRITICAL):
- The **main session** does NOT read artifact files or run tests — subagents (@evaluator, @consensus-reviewer) do this in their own context.
- Do NOT call `ouroboros_execute_seed(action="state")` from main to gather evaluation context.
- Let `ouroboros_evaluate(action="state")` be the single source of truth for evaluation context.
- For Stage 3, pass only `session_id`, `artifact_path`, `reviewer_id`, and `stage2_summary` to each reviewer.

### Retry Rule

If the native agent path fails:

1. Retry once.
2. If it still fails, fall back to the **original direct MCP flow**:
   - Read the artifact file from `artifact_path` (or ask the user what to evaluate)
   - Call `ouroboros_evaluate` directly without `action` parameter:
     ```
     Tool: ouroboros_evaluate
     Arguments:
       session_id: <session_id>
       artifact: <inline artifact content>
       seed_content: <seed YAML if available>
       artifact_type: "code"
     ```
   - Let MCP run the internal 3-stage evaluation pipeline.
   - Present the result from the MCP response.

## Fallback (No MCP Server)

If MCP is not available, use a best-effort direct agent evaluation:

1. Provide `artifact_path`
2. Provide `goal`, `acceptance_criteria`, and `constraints` if known from the conversation or seed
3. Spawn `ouroboros:evaluator`

If you only have `session_id` and no MCP, explain that full evaluation context cannot be reconstructed and suggest enabling the MCP server first.

## Example

```
User: ooo evaluate orch_x1y2z3

Stage 1: build ✅  tests 43/43 ✅
Stage 2: score 0.72 (needs consensus)
Stage 3: reviewer_1 ✅ | reviewer_2 ✅ | reviewer_3 ❌

APPROVED | Stage 3 consensus ✅
Score: 0.78 | AC: 6/6 | Drift: low

📍 Done! Optional: ooo evolve to iteratively refine
```
