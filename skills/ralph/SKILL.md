---
name: ralph
description: "Persistent self-referential loop until verification passes"
---

# /ouroboros:ralph

Persistent self-referential loop until verification passes. "The boulder never stops."

## Usage

```
ooo ralph "<your request>"
/ouroboros:ralph "<your request>"
```

**Trigger keywords:** "ralph", "don't stop", "must complete", "until it works", "keep going"

## How It Works

Ralph mode includes parallel execution + automatic verification:

1. **Execute** (parallel where possible)
   - Independent tasks run concurrently
   - Dependency-aware scheduling

2. **Verify** (verifier)
   - Check completion
   - Validate tests pass
   - Measure drift

3. **Loop** (if failed)
   - Analyze failure
   - Fix issues
   - Repeat from step 1

## Instructions

When the user invokes this skill:

### Load MCP Tools (Required first)

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded via the configured MCP server. Skip directly to Step 1.

**If `ToolSearch` is available** (Claude Code): MCP tools may be registered as deferred tools that must be explicitly loaded.
1. Use the `ToolSearch` tool to find and load the evolve MCP tools:
   ```
   ToolSearch query: "+ouroboros evolve"
   ```
2. If ToolSearch finds the tools → proceed to Step 1. If not → explain that Ralph mode requires the Ouroboros MCP server.

1. **Parse the request**: Extract what needs to be done

2. **Initialize loop**:
   - Generate a session_id (UUID)
   - Track iteration, verification_history in conversation context
   - No file I/O needed — evolve_step stores all execution data in EventStore

4. **Enter the loop** (synchronous execution per generation):

   ```
   while iteration < max_iterations:
       # Run evolve_step synchronously — blocks until generation completes
       result = await evolve_step(lineage_id, seed_content, execute=true)

       # Parse QA from evolve_step response text
       # (EvolveStepHandler runs QA internally and appends verdict)
       verification.passed = (qa_verdict == "pass")
       verification.score = qa_score

       # Record in conversation context
       verification_history.append({
           "iteration": iteration,
           "passed": verification.passed,
           "score": verification.score,
           "verdict": qa_verdict
       })

       if verification.passed:
           # SUCCESS
           break

       # Failed - analyze and continue
       iteration += 1

       if iteration >= max_iterations:
           # Max iterations reached
           break
   ```

   **Tool mapping:**
   - `evolve_step` = `ouroboros_evolve_step`

4. **On termination**, display a next-step:
   - **Success** (QA passed): `Next: ooo evaluate for formal 3-stage verification`
   - **Max iterations reached**: `Next: ooo interview to re-examine the problem — or ooo unstuck to try a different approach`

6. **Report progress** each iteration:
   ```
   [Ralph Iteration <i>/<max>]
   Execution complete. Running QA...

   QA Verdict: <PASS/REVISE/FAIL> (score: <score>)
   Differences:
     - <difference 1>
     - <difference 2>
   Suggestions:
     - <suggestion 1>
     - <suggestion 2>

   The boulder never stops. Continuing...
   ```

6. **Handle interruption**:
   - If user says "stop": exit gracefully
   - If user says "continue": call `ouroboros_query_events(aggregate_id=<lineage_id>)`
     to reconstruct iteration history from EventStore

### Retry Rule

If `ouroboros_evolve_step` fails:

1. Retry once with the same arguments.
2. If it fails again, fall back to background execution:
   ```
   job = await ouroboros_start_evolve_step(lineage_id, seed_content, execute=true)
   # Poll with ouroboros_job_wait until completed, then ouroboros_job_result
   ```

## The Boulder Never Stops

This is the key phrase. Ralph does not give up:
- Each failure is data for the next attempt
- Verification drives the loop
- Only complete success or max iterations stops it

## Example

```
User: ooo ralph fix all failing tests

[Ralph Iteration 1/10]
Executing generation...

QA Verdict: REVISE (score: 0.65)
Differences:
  - 3 tests still failing
  - Type errors in src/api.py
Suggestions:
  - Fix type annotations in api.py before retrying

The boulder never stops. Continuing...

[Ralph Iteration 2/10]
Executing...

QA Verdict: REVISE (score: 0.85)
Differences:
  - 1 test edge case failing
Suggestions:
  - Add boundary check in parse_input()

The boulder never stops. Continuing...

[Ralph Iteration 3/10]
Executing...

QA Verdict: PASS (score: 1.0)

Ralph COMPLETE
==============
Request: Fix all failing tests
Iterations: 3

QA History:
- Iteration 1: REVISE (0.65)
- Iteration 2: REVISE (0.85)
- Iteration 3: PASS (1.0)

All tests passing. Build successful.

Next: `ooo evaluate` for formal 3-stage verification
```
