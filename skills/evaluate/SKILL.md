---
name: evaluate
description: "Evaluate execution with three-stage verification pipeline"
---

# /ouroboros:evaluate

Evaluate an execution session using the three-stage verification pipeline.

## Usage

```
/ouroboros:evaluate <session_id> [artifact]
```

**Trigger keywords:** "evaluate this", "3-stage check"

## How It Works

The evaluation pipeline runs three progressive stages:

1. **Stage 1: Mechanical Verification** ($0 cost)
   - Lint checks, build validation, test execution
   - Static analysis, coverage measurement
   - Fails fast if mechanical checks don't pass

2. **Stage 2: Semantic Evaluation** (Standard tier)
   - AC compliance assessment
   - Goal alignment scoring
   - Drift measurement
   - Reasoning explanation

3. **Stage 3: Multi-Model Consensus** (Frontier tier, optional)
   - Multiple models vote on approval
   - Only triggered by uncertainty or manual request
   - Majority ratio determines outcome

## Instructions

When the user invokes this skill:

### Load MCP Tools (Required first)

The Ouroboros MCP tools are often registered as **deferred tools** that must be explicitly loaded before use. **You MUST perform this step before proceeding.**

1. Use the `ToolSearch` tool to find and load the evaluate MCP tool:
   ```
   ToolSearch query: "+ouroboros evaluate"
   ```
2. The tool will typically be named `mcp__plugin_ouroboros_ouroboros__ouroboros_evaluate` (with a plugin prefix). After ToolSearch returns, the tool becomes callable.
3. If ToolSearch finds the tool → proceed with the MCP-based evaluation below. If not → skip to **Fallback** section.

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

### Evaluation Steps

1. Determine what to evaluate:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for recent execution session IDs

2. Gather the artifact to evaluate:
   - If user specifies a file: Read it with Read tool
   - If recent execution output exists in conversation: Use that
   - Ask user if unclear what to evaluate

3. Call the `ouroboros_evaluate` MCP tool:
   ```
   Tool: ouroboros_evaluate
   Arguments:
     session_id: <session ID>
     artifact: <the code/output to evaluate>
     seed_content: <original seed YAML, if available>
     acceptance_criterion: <specific AC to check, optional>
     artifact_type: "code"  (or "docs", "config")
     trigger_consensus: false  (true if user requests Stage 3)
   ```

4. Present results clearly:
   - Show each stage's pass/fail status
   - Highlight the final approval decision
   - If rejected, explain the failure reason
   - Suggest fixes if evaluation fails
   - Always end with a state breadcrumb based on the outcome:
     - **APPROVED**: `◆ Evaluation approved → next: accept, or ooo evolve to iteratively refine`
     - **REJECTED at Stage 1** (mechanical, `code_changes_detected: true`): `◆ Current state → next: Fix the build/test failures above, then ooo evaluate — or ooo ralph for automated fix loop`
     - **REJECTED at Stage 1** (mechanical, `code_changes_detected: false`): `◆ Current state → next: Run ooo run first to produce code, then ooo evaluate`
     - **REJECTED at Stage 2** (semantic): `◆ Current state → next: ooo run to re-execute with fixes — or ooo evolve for iterative refinement`
     - **REJECTED at Stage 3** (consensus): `◆ Current state → next: ooo interview to re-examine requirements — or ooo unstuck to challenge assumptions`

## Fallback (No MCP Server)

If the MCP server is not available, use the `ouroboros:evaluator` agent to perform a prompt-based evaluation:

1. Delegate to `ouroboros:evaluator` agent
2. The agent performs qualitative evaluation based on the seed spec
3. Results are advisory (no numerical scoring without Python core)

## Example

```
User: /ouroboros:evaluate sess-abc-123

Evaluation Results
============================================================
Final Approval: APPROVED
Highest Stage Completed: 2

Stage 1: Mechanical Verification
  [PASS] lint: No issues found
  [PASS] build: Build successful
  [PASS] test: 12/12 tests passing

Stage 2: Semantic Evaluation
  Score: 0.85
  AC Compliance: YES
  Goal Alignment: 0.90
  Drift Score: 0.08

◆ Evaluation approved → next: accept, or `ooo evolve` to iteratively refine
```

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
