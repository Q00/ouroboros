# Parallel AC Execution Architecture

> Version: 1.0 | Updated: 2026-02-07

Ouroboros executes Acceptance Criteria (ACs) in parallel when they have no
dependencies on each other. This document covers the three pillars of parallel
execution: **Dependency Analysis**, **AC Decomposition**, and the
**Coordinator Agent**.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Dependency Analysis](#2-dependency-analysis)
3. [AC Decomposition (Sub-ACs)](#3-ac-decomposition)
4. [Coordinator Agent](#4-coordinator-agent)
5. [Data Flow (End-to-End)](#5-data-flow)
6. [Configuration](#6-configuration)

---

## 1. Overview

```
Seed (4 ACs)
    |
    v
DependencyAnalyzer (Claude LLM)
    |
    v
DependencyGraph { execution_levels: ((0,), (1, 2), (3,)) }
    |
    v
ParallelACExecutor
    |
    +--[ Level 1 ]-- AC 0 (atomic or decomposed)
    |       |
    |       v
    |   extract_level_context()
    |   detect_file_conflicts()   <-- Coordinator (if conflicts)
    |       |
    +--[ Level 2 ]-- AC 1 || AC 2 (parallel)
    |       |
    |       v
    |   extract_level_context()
    |   detect_file_conflicts()   <-- Coordinator (if conflicts)
    |       |
    +--[ Level 3 ]-- AC 3 (with context from Levels 1+2)
```

Key principles:
- **Zero overhead when no conflicts**: Coordinator Claude session is only
  invoked when file conflicts are detected (Approach A: Pragmatic).
- **Context flows forward**: Each level's results are summarized and injected
  into subsequent level prompts.
- **Graceful degradation**: If dependency analysis fails, all ACs run in a
  single parallel level (all-independent fallback).

---

## 2. Dependency Analysis

**Module**: `src/ouroboros/orchestrator/dependency_analyzer.py`

### How It Works

1. All AC texts are sent to Claude in a single prompt.
2. Claude returns a JSON dependency map: `{ "0": [], "1": [0], "2": [0], "3": [1, 2] }`.
3. The analyzer performs **topological sort** to group ACs into execution levels.

### Data Model

```python
@dataclass(frozen=True)
class ACNode:
    index: int                          # 0-based AC index
    content: str                        # AC description text
    depends_on: tuple[int, ...]         # Indices this AC depends on

@dataclass(frozen=True)
class DependencyGraph:
    nodes: tuple[ACNode, ...]
    execution_levels: tuple[tuple[int, ...], ...]
    # Example: ((0,), (1, 2), (3,))
    #   Level 1: AC 0 alone
    #   Level 2: AC 1 and AC 2 in parallel
    #   Level 3: AC 3 alone (depends on 1 and 2)
```

### Example

Given a seed with 4 ACs:
```
AC 1: Create config.py and models.py (foundation)
AC 2: Add auth feature (needs config.py from AC 1)
AC 3: Add logging feature (needs config.py from AC 1)
AC 4: Create app.py integrating auth + logging (needs AC 2 + AC 3)
```

Claude produces:
```json
{ "0": [], "1": [0], "2": [0], "3": [1, 2] }
```

Topological sort yields:
```
Level 1: [0]        (AC 1 — no dependencies)
Level 2: [1, 2]     (AC 2 + AC 3 — both depend only on AC 1)
Level 3: [3]         (AC 4 — depends on AC 2 and AC 3)
```

### Fallback

If the LLM call fails or returns unparseable JSON, all ACs are placed in a
single level `((0, 1, 2, 3),)` — treating everything as independent.

---

## 3. AC Decomposition

**Module**: `src/ouroboros/orchestrator/parallel_executor.py`
**Method**: `ParallelACExecutor._try_decompose_ac()`

### How It Works

Before executing an AC, the executor asks Claude whether the AC is simple
(atomic) or complex (decomposable):

```
Prompt → Claude:
  "Analyze this AC. If complex, decompose into 2-5 Sub-ACs.
   If simple, respond with: ATOMIC"

Response options:
  A) "ATOMIC"  → execute as-is in a single Claude session
  B) '["Sub-AC 1: ...", "Sub-AC 2: ..."]'  → parallel Sub-AC execution
```

### Decision Criteria

Claude evaluates:
- **Multiple distinct steps**: Does the AC require creating multiple files or
  performing logically separate operations?
- **Independent sub-tasks**: Can parts of the work run concurrently without
  depending on each other?
- **Scope**: Is the AC too large for a single focused Claude session?

### Sub-AC Execution

```
AC 2 (complex)
    |
    v
_try_decompose_ac() → ["Sub-AC 1: Add auth config", "Sub-AC 2: Create auth.py", "Sub-AC 3: Write tests"]
    |
    v
_execute_sub_acs_parallel()
    |
    +-- Sub-AC 1 → Claude session → Edit config.py
    +-- Sub-AC 2 → Claude session → Write auth.py      (parallel)
    +-- Sub-AC 3 → Claude session → Write test_auth.py
    |
    v
All Sub-AC results merged → ACExecutionResult(sub_results=[...])
```

### Constraints

| Parameter | Value | Description |
|-----------|-------|-------------|
| `MIN_SUB_ACS` | 2 | Minimum Sub-ACs for decomposition |
| `MAX_SUB_ACS` | 5 | Maximum Sub-ACs per AC |
| `MAX_DECOMPOSITION_DEPTH` | 2 | No recursive decomposition |

### Why Not Always Decompose?

- Simple ACs (e.g., "create one file") have no benefit from decomposition.
- Each Sub-AC is a separate Claude session (API cost).
- Sub-ACs within the same AC can have their own file conflicts.

---

## 4. Coordinator Agent

**Module**: `src/ouroboros/orchestrator/coordinator.py`

### Purpose

The Coordinator acts as an **intelligent review gate** between execution
levels. After all ACs in a level complete, the Coordinator:

1. **Detects** file conflicts (pure Python, zero API cost)
2. **Resolves** conflicts via a Claude session (only when needed)
3. **Warns** the next level about potential issues

### 4.1 Conflict Detection

```python
LevelCoordinator.detect_file_conflicts(level_results) → list[FileConflict]
```

Scans `ACExecutionResult.messages` for `Write` and `Edit` tool calls.
If two or more ACs (or Sub-ACs) modified the same `file_path`, a
`FileConflict` is created.

```python
@dataclass(frozen=True)
class FileConflict:
    file_path: str                  # e.g., "src/config.py"
    ac_indices: tuple[int, ...]     # e.g., (1, 2) — AC 2 and AC 3
    resolved: bool
    resolution_description: str
```

**Key**: Sub-AC modifications are attributed to their parent AC index for
conflict tracking. This prevents false positives within the same AC's Sub-ACs.

### 4.2 Conflict Resolution (Claude Session)

When conflicts are detected, a Claude session is started with:

- **Tools**: `Read`, `Bash`, `Edit`, `Grep`, `Glob`
- **Prompt**: Conflict details + file paths + instructions to review and fix

The Coordinator Claude agent:
1. Reads the conflicting files
2. Runs `git diff` if needed
3. Applies `Edit` fixes to merge conflicting changes
4. Returns a structured JSON review

### 4.3 CoordinatorReview

```python
@dataclass(frozen=True)
class CoordinatorReview:
    level_number: int
    conflicts_detected: tuple[FileConflict, ...]
    review_summary: str                         # What happened
    fixes_applied: tuple[str, ...]              # What was fixed
    warnings_for_next_level: tuple[str, ...]    # Injected into next prompt
    duration_seconds: float
    session_id: str | None
```

### 4.4 Warning Injection

The `CoordinatorReview` is attached to `LevelContext` and automatically
injected into the next level's AC prompts via `build_context_prompt()`:

```markdown
## Previous Work Context
- AC 2: Added authentication (auth.py, config.py modified)
- AC 3: Added logging (logger.py, config.py modified)

## Coordinator Review (Level 2)
**Review**: Both ACs modified config.py with additive changes (no conflict).
**Fixes applied**: None needed
- WARNING: Verify that Config class has both AUTH_SECRET and LOG_LEVEL fields
- WARNING: Ensure imports in app.py reference the merged config
- WARNING: Run all tests after integration to verify no regressions
```

This means Level 3's AC 4 ("Create app.py") receives explicit guidance about
what to watch out for — without having to rediscover the state from scratch.

### 4.5 Cost Model

| Scenario | Claude Sessions | Cost |
|----------|----------------|------|
| No file conflicts in level | 0 | Free |
| File conflicts detected | 1 per level | ~$0.02-0.10 |
| 3 levels, 1 with conflicts | 1 total | Minimal |

---

## 5. Data Flow

### End-to-End Sequence

```
[1] CLI: ouroboros run workflow --orchestrator seed.yaml
         |
[2] Runner.execute_seed(seed, parallel=True)
         |
[3] DependencyAnalyzer.analyze(seed.acceptance_criteria)
         |  → Claude LLM call → DependencyGraph
         |
[4] ParallelACExecutor.execute_parallel(seed, dependency_graph)
         |
    FOR each level in dependency_graph.execution_levels:
         |
[5]     FOR each AC in level (concurrent via asyncio.gather):
         |      |
[6]     |   _try_decompose_ac(ac)
         |      |  → Claude call → ATOMIC or Sub-AC list
         |      |
[7]     |   IF atomic:
         |      |   _execute_atomic_ac(ac) → Claude session with tools
         |      ELSE:
         |      |   _execute_sub_acs_parallel(sub_acs) → N Claude sessions
         |      |
[8]     |   → ACExecutionResult(messages, sub_results)
         |
[9]     extract_level_context(level_results) → LevelContext
         |
[10]    detect_file_conflicts(level_results) → list[FileConflict]
         |
[11]    IF conflicts:
         |      run_review(conflicts) → CoordinatorReview
         |      attach review to LevelContext
         |
[12]    level_contexts.append(level_ctx)
         |      → build_context_prompt(level_contexts) for next level
         |
    END FOR levels
         |
[13] ParallelExecutionResult(all_results, success/failure counts)
```

### Event Flow (TUI)

```
ParallelACExecutor
    |-- emit "workflow.progress.updated"     → TUI: AC tree + progress bar
    |-- emit "execution.subtask.updated"     → TUI: Sub-AC nodes in tree
    |-- emit "execution.tool.started"        → TUI: inline tool activity
    |-- emit "execution.tool.completed"      → TUI: tool history
    |-- emit "execution.agent.thinking"      → TUI: thinking panel
    |
EventStore (SQLite) → app._subscribe_to_events() (0.5s poll)
    |
app.py → _merge_ac_progress() → DashboardScreenV3 (AC tree + detail panel)
```

---

## 6. Configuration

### CLI Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--orchestrator` / `-o` | off | Enable Claude Agent SDK execution |
| `--sequential` / `-s` | off | Disable parallel execution (run ACs one by one) |
| `--debug` / `-d` | off | Show structlog output, agent thinking, raw events |

### Seed Fields

```yaml
task_type: code          # "code", "research", or "analysis"
                         # Determines tool set and prompt strategy

acceptance_criteria:
  - "AC 1: ..."          # Each AC is analyzed for dependencies
  - "AC 2: ..."          # and optionally decomposed into Sub-ACs
```

### Internal Constants

| Constant | Value | Location |
|----------|-------|----------|
| `MIN_SUB_ACS` | 2 | parallel_executor.py |
| `MAX_SUB_ACS` | 5 | parallel_executor.py |
| `MAX_DECOMPOSITION_DEPTH` | 2 | parallel_executor.py |
| `COORDINATOR_TOOLS` | Read, Bash, Edit, Grep, Glob | coordinator.py |
| Poll interval | 0.5s | app.py |
