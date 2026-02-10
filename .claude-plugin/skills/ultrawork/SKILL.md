# /ouroboros:ultrawork

Maximum parallelism execution mode for independent tasks.

## Usage

```
ooo ultrawork "<your request>"
/ouroboros:ultrawork "<your request>"
```

**Trigger keywords:** "ultrawork", "ulw", "maximum parallelism", "parallel everything"

## How It Works

Ultrawork mode identifies independent tasks and executes them in parallel:

1. **Decompose** (planner)
   - Break request into atomic tasks
   - Build dependency graph
   - Identify parallelizable work

2. **Parallel Execute** (multiple executors)
   - Execute each dependency level in parallel
   - Spawn multiple agent instances
   - Aggregate results as levels complete

3. **Aggregate** (planner)
   - Combine results from parallel tasks
   - Handle any dependencies
   - Move to next level

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what needs to be done

2. **Initialize state**: Create `.omc/state/ultrawork-state.json`:
   ```json
   {
     "mode": "ultrawork",
     "session_id": "<uuid>",
     "request": "<user request>",
     "status": "decomposing",
     "dependency_levels": [],
     "current_level": 0,
     "tasks": {}
   }
   ```

3. **Delegate to planner** to decompose:
   ```
   Decompose this request into parallelizable tasks: "<user request>"

   Produce:
   1. A list of atomic tasks
   2. Dependency graph (which tasks depend on others)
   3. Execution levels (tasks that can run in parallel)
   ```

4. **Execute each level in parallel**:

   For each level `i` in the dependency levels:
   - Collect all tasks in this level
   - Delegate each task to a separate `executor` instance
   - Use `Task(subagent_type="oh-my-claudecode:executor", ...)` in parallel
   - Wait for all tasks in this level to complete
   - Update state with results

5. **Handle failures**:
   - If any task fails: note the failure, continue with other parallel tasks
   - After level completion: if any failures, offer retry or continue

6. **Report progress** after each level:
   ```
   [Ultrawork Level <i>/<total>]
   Executing <n> tasks in parallel...
   Completed: <completed>/<n>
   ```

## Parallel Execution Pattern

Use this pattern for parallel execution:

```
# Spawn multiple tasks in parallel
tasks = []
for task in level_tasks:
    tasks.append(Task(
        subagent_type="oh-my-claudecode:executor",
        model="sonnet",
        prompt=f"Execute: {task.description}",
        context={"files": task.files}
    ))

# Wait for all to complete (they run in parallel)
results = await asyncio.gather(*tasks)
```

## State Persistence

Track:
- Dependency levels
- Current execution level
- Task completion status per level
- Parallel worker count
- Timestamp of level start/complete

## Example

```
User: ooo ultrawork implement CRUD for 5 models

[Ultrawork Decomposing]
Identified 5 independent tasks
Dependency Levels: 1
Level 0: 5 tasks (all independent)

[Ultrawork Level 1/1]
Spawning 5 parallel executors...

[Worker 1] Creating User model...
[Worker 2] Creating Task model...
[Worker 3] Creating Project model...
[Worker 4] Creating Comment model...
[Worker 5] Creating Tag model...

[Ultrawork Level 1/1]
All tasks completed!

Ultrawork COMPLETE
==================
Request: CRUD for 5 models
Duration: 2m 15s
Parallel Workers: 5

Tasks Completed:
- User model: PASSED
- Task model: PASSED
- Project model: PASSED
- Comment model: PASSED
- Tag model: PASSED
```

## Composition

Ultrawork is included in:
- **ralph** mode (persistence wrapper)
- Can be used standalone for one-time parallel execution

## Cancellation

Cancel with `/ouroboros:cancel`.

Current level tasks will complete before stopping.
