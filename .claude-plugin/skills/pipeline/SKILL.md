# /ouroboros:pipeline

Sequential agent chaining with data passing between stages.

## Usage

```
ooo pipeline "<your request>"
/ouroboros:pipeline "<your request>"
```

**Trigger keywords:** "pipeline", "chain agents", "sequential", "step by step"

## How It Works

Pipeline mode chains agents sequentially, each building on the previous:

1. **Stage 1: Analyst** - Understand requirements
2. **Stage 2: Planner** - Create execution plan
3. **Stage 3: Executor** - Implement the plan
4. **Stage 4: Reviewer** - Review the implementation
5. **Stage 5: Verifier** - Verify completion

Each stage receives the output of the previous stage.

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what needs to be done

2. **Initialize state**: Create `.omc/state/pipeline-state.json`:
   ```json
   {
     "mode": "pipeline",
     "session_id": "<uuid>",
     "request": "<user request>",
     "status": "stage_1",
     "current_stage": "analyst",
     "stages": ["analyst", "planner", "executor", "reviewer", "verifier"],
     "stage_outputs": {},
     "failed_at": null
   }
   ```

3. **Execute stages sequentially**:

   **Stage 1 - Analyst**:
   ```
   Delegate to analyst (model="sonnet"):
   "Analyze this request: '<user request>'
   Identify requirements, constraints, and acceptance criteria."

   Save output to state.stage_outputs.analyst
   ```

   **Stage 2 - Planner**:
   ```
   Delegate to planner (model="opus"):
   "Create execution plan based on analysis: {state.stage_outputs.analyst}

   Request: '<user request>'

   Produce step-by-step plan."

   Save output to state.stage_outputs.planner
   ```

   **Stage 3 - Executor**:
   ```
   Delegate to executor (model="sonnet"):
   "Execute this plan: {state.stage_outputs.planner}

   Analysis: {state.stage_outputs.analyst}
   Request: '<user request>'

   Implement all changes."

   Save output to state.stage_outputs.executor
   ```

   **Stage 4 - Reviewer**:
   ```
   Delegate to quality-reviewer (model="sonnet"):
   "Review this implementation: {state.stage_outputs.executor}

   Plan: {state.stage_outputs.planner}

   Check for quality issues, maintainability, and completeness."

   Save output to state.stage_outputs.reviewer
   ```

   **Stage 5 - Verifier**:
   ```
   Delegate to verifier (model="haiku"):
   "Verify completion: {state.stage_outputs.executor}

   Review findings: {state.stage_outputs.reviewer}
   Original request: '<user request>'

   Check build, tests, and acceptance criteria."

   Save output to state.stage_outputs.verifier
   ```

4. **Report progress** after each stage:
   ```
   [Pipeline Stage <n>/<total>: <stage_name>]
   <brief status>

   Output: <summary of stage output>

   Next stage: <next_stage>
   ```

5. **Handle failures**:
   - If any stage fails: stop pipeline, report failure
   - Save state at failure point for debugging
   - Offer to retry from failed stage

## Stage Data Passing

Each stage receives:
- `previous_output`: The output of the previous stage
- `all_outputs`: All stage outputs so far
- `original_request`: The user's original request

## Custom Pipelines

Users can specify custom stage order:

```
ooo pipeline "build and test" --stages planner,executor,verifier
```

## Example

```
User: ooo pipeline add authentication to API

[Pipeline Stage 1/5: analyst]
Analyzing requirements for authentication...

Output:
- Requirements: JWT auth, user registration, login
- Constraints: Use existing user model, no external deps
- Acceptance: 3 endpoints, middleware, tests

Next stage: planner

[Pipeline Stage 2/5: planner]
Creating execution plan...

Output:
1. Add JWT dependency
2. Create auth service
3. Add /register, /login endpoints
4. Create auth middleware
5. Write tests

Next stage: executor

[Pipeline Stage 3/5: executor]
Implementing authentication...

Output:
- Created: src/auth/jwt.py
- Created: src/auth/service.py
- Modified: src/api/routes.py (added 3 endpoints)
- Created: src/auth/middleware.py
- Created: tests/test_auth.py

Next stage: reviewer

[Pipeline Stage 4/5: reviewer]
Reviewing implementation...

Output:
- Quality: GOOD
- Security: Minor concern - token expiration configurable
- Maintainability: EXCELLENT
- Completeness: All acceptance criteria met

Next stage: verifier

[Pipeline Stage 5/5: verifier]
Verifying completion...

Output:
- Build: PASSED
- Tests: 8/8 PASSED
- Acceptance criteria: ALL MET

Pipeline COMPLETE
=================
Request: Add authentication to API
Duration: 6m 45s
Stages: 5

Stage Outputs:
- analyst: Requirements identified
- planner: 5-step plan created
- executor: 5 files created/modified
- reviewer: Quality approved
- verifier: All checks passed

Result: SUCCESS
```

## Pipeline Comparison

| Mode | Parallelism | Use Case |
|------|-------------|----------|
| Pipeline | Sequential (stages in order) | Clear handoff, audit trail |
| Swarm | Parallel (team coordination) | Complex, multi-domain work |
| Autopilot | Sequential (planner decides) | Autonomous, minimal visibility |
| Ultrapilot | Parallel (file partitioning) | Multi-file implementation |

## State Persistence

Track:
- Current stage
- Output of each completed stage
- Stage transition history
- Failure point (if any)

Resume: "continue pipeline" restarts from last stage

## Cancellation

Cancel with `/ouroboros:cancel`.

Current stage will complete before stopping.
State preserved for "continue pipeline".
