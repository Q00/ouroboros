# /ouroboros:ultrapilot

Parallel autopilot with file ownership partitioning for maximum throughput.

## Usage

```
ooo ultrapilot "<your request>"
/ouroboros:ultrapilot "<your request>"
```

**Trigger keywords:** "ultrapilot", "parallel build", "parallel autopilot"

## How It Works

Ultrapilot combines autopilot with parallel execution by partitioning files:

1. **Partition** (planner)
   - Identify files to modify/create
   - Group by ownership (different agents own different files)
   - Avoid conflicts through clear ownership

2. **Parallel Execute** (multiple executors)
   - Each executor owns a subset of files
   - All executors work in parallel
   - No write conflicts

3. **Merge** (planner)
   - Combine all changes
   - Resolve any integration issues
   - Run final verification

## Instructions

When the user invokes this skill:

1. **Parse the request**: Extract what needs to be built

2. **Initialize state**: Create `.omc/state/ultrapilot-state.json`:
   ```json
   {
     "mode": "ultrapilot",
     "session_id": "<uuid>",
     "request": "<user request>",
     "status": "partitioning",
     "file_ownership": {},
     "workers": [],
     "merge_status": "pending"
   }
   ```

3. **Delegate to planner** for partitioning:
   ```
   Partition this work for parallel execution: "<user request>"

   Identify:
   1. All files that need to be created/modified
   2. Logical groupings (by module, feature, layer)
   3. Ownership assignment (which files each worker owns)

   Output:
   - Worker 1 owns: [file list]
   - Worker 2 owns: [file list]
   - ...
   ```

4. **Execute in parallel**:

   For each worker's file set:
   ```
   Delegate to executor with:
   - model: "sonnet"
   - prompt: "Build your assigned files for: <user request>"
   - context: {"owned_files": [file list], "request": <request>}
   ```

   All workers run in parallel.

5. **Monitor progress**:
   ```
   [Ultrapilot Progress]
   Worker 1: <file list> - IN_PROGRESS
   Worker 2: <file list> - COMPLETED
   Worker 3: <file list> - IN_PROGRESS
   ```

6. **Merge and verify**:
   - After all workers complete, delegate to verifier
   - Check for integration issues
   - Run full build and test
   - If merge conflicts: delegate to executor to resolve

## Ownership Rules

To avoid conflicts:
- Each file is owned by exactly one worker
- Shared interfaces are owned by the first worker
- Integration points are documented by planner

## Example

```
User: ooo ultrapilot build a full-stack CRUD app

[Ultrapilot Partitioning]
Identified 12 files across 3 modules

File Ownership:
- Worker 1 (Backend): [models.py, api.py, schema.py]
- Worker 2 (Frontend): [App.tsx, components.tsx, api.ts]
- Worker 3 (Tests): [test_api.py, test_models.py, e2e.spec.ts]

[Ultrapilot Progress]
Worker 1: Building backend... IN_PROGRESS
Worker 2: Building frontend... IN_PROGRESS
Worker 3: Writing tests... IN_PROGRESS

[Ultrapilot Progress]
Worker 1: COMPLETED
Worker 2: COMPLETED
Worker 3: COMPLETED

[Ultrapilot Merge]
Merging all changes...
Running integration tests...

Ultrapilot COMPLETE
===================
Request: Full-stack CRUD app
Duration: 4m 22s
Workers: 3

Files Created:
- Backend: 3 files
- Frontend: 3 files
- Tests: 3 files

Verification: PASSED
- Build: PASSED
- Tests: 15/15 PASSED
- Integration: PASSED
```

## vs Autopilot

| Aspect | Autopilot | Ultrapilot |
|--------|-----------|------------|
| Parallelism | Sequential execution | Parallel workers |
| File handling | Single executor | Partitioned ownership |
| Speed | Baseline | 2-3x faster for multi-file |
| Complexity | Simple | Needs planning |

## vs Ultrawork

| Aspect | Ultrawork | Ultrapilot |
|---------|-----------|------------|
| Task type | Independent tasks | Related files |
| Coordination | Dependency graph | File ownership |
| Use case | AC-level parallelism | Feature-level parallelism |

## Cancellation

Cancel with `/ouroboros:cancel`.

Current workers will finish their current file before stopping.
