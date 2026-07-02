---
name: evaluate
description: "Evaluate execution with three-stage verification pipeline"
aliases: [eval]
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

1. Use the active runtime's tool-discovery capability to find and load the evaluate MCP tool:
   ```
   tool discovery query: "+ouroboros evaluate"
   ```
2. The tool will typically be named `mcp__plugin_ouroboros_ouroboros__ouroboros_evaluate` (with a plugin prefix). After runtime tool discovery returns, the tool becomes callable.
3. If the tool is callable — already exposed, or loaded by discovery — proceed with the MCP-based evaluation below. An empty discovery result for an already-exposed tool is expected, not a failure. Skip to the **Fallback** section only if the tool is genuinely absent (no Ouroboros MCP server).

**IMPORTANT**: Do NOT skip this step. Do NOT assume MCP tools are unavailable just because they don't appear in your immediate tool list. They are almost always available as deferred tools that need to be loaded first.

**CRITICAL — deferred-schema guard (prevents "Invalid tool parameters"):**
This skill can call `ouroboros_evaluate` after a fresh turn. A deferred tool's
schema loaded on one turn is NOT guaranteed to still be loaded on the next. If
you call it while its schema is not loaded in the **current** turn, the runtime
rejects the call with **"Invalid tool parameters"** before it reaches the server.
Therefore: **immediately before EVERY `ouroboros_evaluate` call in this skill,
re-run `tool discovery query: "+ouroboros evaluate"`** (idempotent — a no-op when
already loaded). If the load returns no matching tool (and the tool is not already callable — an empty load for an already-exposed tool is an expected no-op, not absence), switch to the documented
fallback instead of retrying the failing call.

### Evaluation Steps

1. Determine what to evaluate:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for recent execution session IDs

2. Gather the artifact to evaluate:
   - If user specifies a file: Read it with Read tool
   - If recent execution output exists in conversation: Use that
   - Ask user if unclear what to evaluate

2.5. Acting verification — reproduce and OBSERVE (do not skip for behaviour-bearing work):
   Stage 1 already runs mechanical checks (build/test). Go further when the runtime
   exposes acting tools — **computer-use / browser, `Bash`/shell, file reads**: don't
   just reason over the diff, **run the result and observe the real effect** (the
   command's output, the endpoint's response, the rendered UI via a screenshot). Do
   it via a dedicated verification sub-agent to keep the main session lean — or
   inline in the main session where the runtime restricts sub-agent spawning (the
   observation is what matters; the delegation is only an optimization). Probe the
   acceptance criteria against the ACTUAL observable behaviour and the adversarial
   classes (`misleading_output`, `hung_command`, `stale_state`, `dirty_worktree`, …).
   Feed the captured evidence (commands, outputs, artifact paths) into the evaluate
   call as part of the `artifact`. If acting tools are unavailable, note that
   behaviour was not observed and evaluate on the text alone.

3. Call the `ouroboros_evaluate` MCP tool:
   ```
   Tool: ouroboros_evaluate
   Arguments:
     session_id: <session ID>
     artifact: <the code/output to evaluate, plus observed-behaviour evidence from 2.5>
     seed_content: <original seed YAML, if available>
     acceptance_criterion: <specific AC to check, optional>
     artifact_type: "code"  (or "docs", "config")
     working_dir: <absolute project root, recommended>
     trigger_consensus: false  (true if user requests Stage 3)
   ```

   `working_dir` controls both Stage 1 command execution and Stage 2 source-file visibility. Pass the absolute project root whenever available; if omitted, the MCP handler falls back to the registered brownfield default, seed project metadata, then the MCP server cwd.

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
