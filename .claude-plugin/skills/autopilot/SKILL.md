# /ouroboros:autopilot

Full autonomous execution from idea to working code with verification.

## Usage

```
ooo autopilot "<your request>"
/ouroboros:autopilot "<your request>"
```

**Trigger keywords:** "autopilot", "build me", "I want a", "make this", "create this for me"

## How It Works

Autopilot mode executes the full development cycle autonomously:

1. **Plan** (analyst + planner)
   - Analyze requirements
   - Create execution plan
   - Identify dependencies

2. **Execute** (executor)
   - Implement the changes
   - Write/edit files
   - Run builds and tests

3. **Verify** (verifier)
   - Check completion evidence
   - Validate tests pass
   - Confirm zero errors

4. **Iterate** (if verification fails)
   - Analyze failures
   - Fix issues
   - Re-verify

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what the user wants to build

2. **Initialize state**: Create `.omc/state/autopilot-state.json`:
   ```json
   {
     "mode": "autopilot",
     "session_id": "<uuid>",
     "request": "<user request>",
     "status": "planning",
     "iterations": 0,
     "max_iterations": 5,
     "current_phase": "analyze"
   }
   ```

3. **Execute the agent chain**:

   **Phase 1: Planning**
   - Delegate to `analyst` with model="sonnet":
     ```
     Analyze the request: "<user request>"
     Identify:
     - What files need to change
     - What patterns to follow
     - Potential edge cases
     - Acceptance criteria
     ```

   - Delegate to `planner` with model="opus":
     ```
     Create an execution plan for: "<user request>"
     Based on analyst findings, produce:
     1. Step-by-step execution plan
     2. File-by-file change list
     3. Verification criteria
     ```

   **Phase 2: Execution**
   - Delegate to `executor` with model="sonnet":
     ```
     Execute this plan: <plan from planner>
     Request: "<user request>"
     Context: <analyst findings>

     Implement all changes. Run builds and tests after each major change.
     ```

   **Phase 3: Verification**
   - Delegate to `verifier` with model="haiku":
     ```
     Verify the claim: "<executor's completion claim>"
     Check:
     - Build passes (fresh output)
     - Tests pass (fresh output)
     - All files modified correctly
     - Zero errors in implementation
     ```

4. **Handle verification results**:
   - If PASSED: Update state to "completed", report success
   - If FAILED: Update state to "retry", increment iterations, return to Phase 2

5. **Max iterations**: If `iterations >= max_iterations`, stop and report partial completion

6. **Report progress** after each phase:
   ```
   [Autopilot Phase: <phase>]
   <brief status update>
   ```

## State Persistence

The state file tracks:
- Current session ID
- User's original request
- Execution phase (planning/executing/verifying/completed)
- Iteration count
- Verification history
- Timestamp of last update

Resume is supported if the user says "continue" after a stop.

## Transitions

Autopilot can automatically transition to:
- **ralph** mode if user says "don't stop" or "must complete"
- **ultraqa** mode for intensive quality assurance

## Example

```
User: ooo autopilot build a REST API for task management

[Autopilot Phase: Planning]
Analyzing requirements...

[Autopilot Phase: Planning]
Creating execution plan...

[Autopilot Phase: Execution]
Implementing API endpoints...
Created: src/api/tasks.py
Created: src/models/task.py
Build: PASSED
Tests: 8/10 passing

[Autopilot Phase: Verification]
Verifying completion...

Result: 2 tests failed, fixing...

[Autopilot Phase: Execution]
Fixing test failures...

[Autopilot Phase: Verification]
Verifying completion...

Autopilot COMPLETE
==================
Request: REST API for task management
Duration: 3m 45s
Iterations: 2

Files Created:
- src/api/tasks.py
- src/models/task.py
- tests/test_tasks.py

Verification: PASSED
- Build: PASSED
- Tests: 10/10 PASSED
```

## Cancellation

Cancel with `/ouroboros:cancel` or "stop autopilot".

State is preserved for resume with "continue autopilot".
