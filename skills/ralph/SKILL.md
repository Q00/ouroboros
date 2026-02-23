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

Ralph mode includes parallel execution + automatic verification + persistence:

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

4. **Persist** (checkpoint)
   - Save state after each iteration
   - Resume capability if interrupted
   - Full audit trail

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what needs to be done

2. **Initialize state**: Create `.omc/state/ralph-state.json`:
   ```json
   {
     "mode": "ralph",
     "session_id": "<uuid>",
     "request": "<user request>",
     "status": "running",
     "iteration": 0,
     "max_iterations": 10,
     "last_checkpoint": null,
     "verification_history": []
   }
   ```

3. **Enter the loop**:

   ```
   while iteration < max_iterations:
       # Execute with parallel agents
       result = await execute_parallel(request, context)

       # Verify the result
       verification = await verify_result(result, acceptance_criteria)

       # Record in history
       state.verification_history.append({
           "iteration": iteration,
           "passed": verification.passed,
           "score": verification.score,
           "timestamp": <now>
       })

       if verification.passed:
           # SUCCESS - persist final checkpoint
           await save_checkpoint("complete")
           break

       # Failed - analyze and continue
       iteration += 1
       await save_checkpoint("iteration_{iteration}")

       if iteration >= max_iterations:
           # Max iterations reached
           break
   ```

4. **Report progress** each iteration:
   ```
   [Ralph Iteration <i>/<max>]
   Execution complete. Verifying...

   Verification: <FAILED/PASSED>
   Score: <score>
   Issues: <list of issues>

   The boulder never stops. Continuing...
   ```

5. **Handle interruption**:
   - If user says "stop": save checkpoint, exit gracefully
   - If user says "continue": reload from last checkpoint
   - State persists across session resets

## Persistence

State includes:
- Current iteration number
- Verification history for all iterations
- Last successful checkpoint
- Issues found in each iteration
- Execution context for resume

Resume command: "continue ralph" or "ralph continue"

## The Boulder Never Stops

This is the key phrase. Ralph does not give up:
- Each failure is data for the next attempt
- Verification drives the loop
- Only complete success or max iterations stops it

## Example

```
User: ooo ralph fix all failing tests

[Ralph Iteration 1/10]
Executing in parallel...
Fixing test failures...

Verification: FAILED
Score: 0.65
Issues:
- 3 tests still failing
- Type errors in src/api.py

The boulder never stops. Continuing...

[Ralph Iteration 2/10]
Executing in parallel...
Fixing remaining issues...

Verification: FAILED
Score: 0.85
Issues:
- 1 test edge case failing

The boulder never stops. Continuing...

[Ralph Iteration 3/10]
Executing in parallel...
Fixing edge case...

Verification: PASSED
Score: 1.0

Ralph COMPLETE
==============
Request: Fix all failing tests
Duration: 8m 32s
Iterations: 3

Verification History:
- Iteration 1: FAILED (0.65)
- Iteration 2: FAILED (0.85)
- Iteration 3: PASSED (1.0)

All tests passing. Build successful.
```

## Cancellation

Cancel with `/ouroboros:cancel --force` to clear state.

Standard `/ouroboros:cancel` saves checkpoint for resume.
