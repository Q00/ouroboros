# Execution Deep Dive: Recursive AC Decomposition

## Overview

The Ouroboros execution engine implements a recursive Double Diamond methodology to decompose complex Acceptance Criteria (ACs) into atomic, executable subtasks. This approach combines structured problem-solving (Discover-Define-Design-Deliver phases) with intelligent parallelization based on task dependencies.

The core innovation is the dynamic decision-making at each recursion level: the system uses LLM-based atomicity detection to determine whether an AC can be executed directly or needs further decomposition. This creates a self-balancing execution tree that adapts to task complexity.

## The Recursive Flow

The primary entry point is `run_cycle_with_decomposition(ac, depth=0)`, which implements the following algorithm:

```
run_cycle_with_decomposition(ac, depth=0)
  1. Discover phase → explore problem space, gather insights
  2. Define phase → narrow to core problem definition
  3. check_atomicity(ac) → LLM decides if AC is atomic
  4a. IF ATOMIC:
      → Design phase → create solution plan
      → Deliver phase → execute implementation
      → return AtomicACResult
  4b. IF NON-ATOMIC AND depth < max_depth-1:
      → decompose_ac() → generate 2-5 child ACs
      → topological_sort(children) → group by dependency levels
      → for each dependency level:
          → asyncio.gather(run_cycle_with_decomposition(child, depth+1)
                           for child in level)
      → aggregate results → return DecomposedACResult
```

### Key Characteristics

- **Adaptive recursion**: Each AC independently determines its execution strategy based on complexity assessment
- **Bounded depth**: Maximum recursion depth of 5 prevents infinite decomposition
- **Parallel within levels**: Independent children execute concurrently; dependent children execute sequentially
- **Context compression**: Deep subtasks receive truncated context to manage prompt size

## Atomicity Detection

The `check_atomicity()` function in `execution/atomicity.py` determines whether an AC can be executed as a single unit or requires decomposition.

### LLM-Based Primary Detection

The primary method uses a language model to evaluate:

1. **Single-focused**: Does the AC address one clear objective?
2. **File scope**: Can it be completed in 1-2 files?
3. **Self-contained**: Are all dependencies available or trivial?

The LLM returns a structured `AtomicityResult` with:
- `is_atomic`: Boolean decision
- `reasoning`: Explanation of the decision
- `method`: "llm" for LLM-based detection

### Heuristic Fallback

When the LLM is unavailable, the system falls back to rule-based heuristics:

```python
# Default thresholds
max_complexity = 0.7
max_tool_count = 3
max_duration_seconds = 300
```

Heuristic criteria:
- Complexity score below threshold (0.7)
- Expected tool count within limit (3)
- Estimated duration under 5 minutes (300s)

The fallback returns `AtomicityResult` with `method="heuristic"`.

### Design Rationale

Two-tier detection provides robustness: LLM offers semantic understanding of task complexity, while heuristics ensure system availability even when LLM calls fail. The `method` field enables observability into which detection strategy was used.

## AC Decomposition

The `decompose_ac()` function in `execution/decomposition.py` breaks non-atomic ACs into child subtasks.

### Decomposition Constraints

```python
MIN_CHILDREN = 2
MAX_CHILDREN = 5
MAX_DEPTH = 5
COMPRESSION_DEPTH = 3
```

- **Child count**: Always generates 2-5 children for balanced trees
- **Maximum depth**: Prevents unbounded recursion at 5 levels
- **Compression depth**: Context truncation begins at depth 3

### Decomposition Process

1. **LLM invocation**: Prompt includes parent AC, discover insights, and MECE principle instructions
2. **Child generation**: LLM produces 2-5 child ACs with:
   - `id`: Unique identifier
   - `description`: Clear, actionable acceptance criteria
   - `depends_on`: List of sibling AC IDs this child depends on
3. **Validation**:
   - Cyclic prevention: No child can depend on its parent
   - Count validation: Enforce MIN_CHILDREN and MAX_CHILDREN bounds
   - Empty check: All children must have non-empty descriptions

### MECE Principle

Decomposition follows the Mutually Exclusive, Collectively Exhaustive principle:

- **Mutually Exclusive**: Children do not overlap in scope
- **Collectively Exhaustive**: Children completely cover the parent's requirements

This ensures clean decomposition without gaps or redundancies.

### Example Decomposition

```
Parent AC: "Implement user authentication system"
  → Child 1: "Create user registration endpoint with validation"
  → Child 2: "Implement password hashing and storage" (depends_on: [1])
  → Child 3: "Build login endpoint with JWT token generation" (depends_on: [2])
  → Child 4: "Add password reset workflow via email"
```

## Dependency-Based Parallel Execution

The execution engine optimizes throughput by running independent ACs concurrently while respecting dependencies.

### Topological Sorting

Children are grouped into dependency levels using Kahn's algorithm:

```python
def topological_sort(children: List[AC]) -> List[List[AC]]:
    # Returns [[level_0_acs], [level_1_acs], ...]
    # where level_0 has no dependencies,
    # level_1 depends only on level_0, etc.
```

### Execution Strategy

```python
for level in dependency_levels:
    # All ACs in this level are independent of each other
    results = await asyncio.gather(
        *[run_cycle_with_decomposition(child, depth+1)
          for child in level]
    )
    # Next level waits until all current level ACs complete
```

**Within-level parallelism**: Independent ACs execute concurrently via `asyncio.gather()`, maximizing resource utilization.

**Between-level sequencing**: Level N+1 only starts after all ACs in level N complete, ensuring dependency constraints are satisfied.

### Circular Dependency Handling

If circular dependencies are detected:

1. System logs a warning
2. Falls back to sequential execution
3. Continues with degraded parallelism rather than failing

This defensive approach prioritizes correctness over performance when decomposition produces invalid dependency graphs.

### Parallelism Example

```
Level 0 (run in parallel):
  - AC1: "Set up database schema"
  - AC2: "Configure logging framework"

Level 1 (run in parallel after Level 0):
  - AC3: "Create data models" (depends_on: [AC1])
  - AC4: "Implement API endpoints" (depends_on: [AC1, AC2])

Level 2 (run after Level 1):
  - AC5: "Add integration tests" (depends_on: [AC3, AC4])
```

## Context Compression

As decomposition depth increases, context size grows exponentially. Context compression prevents prompt size from exceeding model limits.

### Compression Trigger

```python
COMPRESSION_DEPTH = 3

if depth >= COMPRESSION_DEPTH:
    discover_insights = discover_insights[:500]  # Truncate to 500 chars
```

At depth 3 and beyond, Discover phase insights are truncated to 500 characters before being passed to child ACs.

### Rationale

- **Depth 0-2**: Full context available for top-level problem understanding
- **Depth 3+**: Subtasks are sufficiently scoped that abbreviated context is adequate
- **500 characters**: Enough for key insights, small enough to prevent prompt bloat

This graduated compression strategy balances context richness at high levels with scalability at deep levels.

## Safety Mechanisms

### Recursion Depth Limit

```python
MAX_DEPTH = 5

# Two boundary conditions:
if depth >= max_depth:
    # Force execution without decomposition (hard stop)

if not is_atomic and depth < max_depth - 1:
    # Allow decomposition only below penultimate level
```

The hard limit at `MAX_DEPTH` prevents infinite recursion. Decomposition stops one level before the max (`depth < max_depth - 1`), ensuring leaf nodes always have room to execute their 4-phase cycle.

### Cyclic Decomposition Prevention

The `is_cyclic()` check in `ACTree` validates that:

- No child AC has its parent in the `depends_on` list
- No circular dependency chains exist among siblings

Invalid trees are rejected before execution begins.

### Subagent Result Validation

Different validation rules apply based on execution path:

**Atomic ACs**: Must produce all four phases (Discover, Define, Design, Deliver)

```python
if result.is_atomic:
    assert result.discover is not None
    assert result.define is not None
    assert result.design is not None
    assert result.deliver is not None
```

**Decomposed ACs**: Only Discover and Define phases required (Design and Deliver happen at leaf nodes)

```python
if not result.is_atomic:
    assert result.discover is not None
    assert result.define is not None
    # Design and Deliver may be None
```

This ensures structural integrity of the execution tree.

## Parallel Executor vs Double Diamond Differences

Ouroboros includes two decomposition systems with different trade-offs:

### DoubleDiamond (execution/double_diamond.py)

```python
MAX_DEPTH = 5
```

- **Full recursive decomposition**: Deep hierarchies allowed
- **Use case**: Complex, multi-faceted problems requiring extensive breakdown
- **Concurrency**: Native asyncio.gather()

### ParallelACExecutor (execution/parallel_executor.py)

```python
MAX_DECOMPOSITION_DEPTH = 2
```

- **Conservative decomposition**: Shallow hierarchies (2 levels max)
- **Use case**: Well-scoped problems where deep decomposition is overkill
- **Concurrency**: anyio task groups (preserves Anthropic SDK cancel scopes)

### Selection Criteria

- **Use DoubleDiamond**: When AC is vague, complex, or spans multiple domains
- **Use ParallelACExecutor**: When AC is clear, bounded, and benefits from simple parallelization

The ParallelACExecutor's use of anyio ensures proper cancellation propagation when using Anthropic's async SDK, which is critical for interrupt handling.

## Configuration Reference

| Constant | Value | Location | Purpose |
|----------|-------|----------|---------|
| MAX_DEPTH | 5 | decomposition.py, ac_tree.py | Maximum recursion depth |
| MIN_CHILDREN | 2 | decomposition.py | Minimum child ACs per decomposition |
| MAX_CHILDREN | 5 | decomposition.py | Maximum child ACs per decomposition |
| COMPRESSION_DEPTH | 3 | decomposition.py | Depth at which context truncation begins |
| MAX_DECOMPOSITION_DEPTH | 2 | parallel_executor.py | Depth limit for ParallelACExecutor |

## Multi-Level Decomposition Example

```
Depth 0: "Build full-stack blog platform"
├─ Depth 1: "Implement backend API"
│  ├─ Depth 2: "Set up database and ORM"
│  │  ├─ Depth 3: "Configure PostgreSQL connection" (ATOMIC)
│  │  └─ Depth 3: "Define SQLAlchemy models" (ATOMIC)
│  └─ Depth 2: "Create REST endpoints"
│     ├─ Depth 3: "Implement POST /articles endpoint" (ATOMIC)
│     ├─ Depth 3: "Implement GET /articles endpoint" (ATOMIC)
│     └─ Depth 3: "Implement DELETE /articles/:id endpoint" (ATOMIC)
└─ Depth 1: "Build frontend UI"
   ├─ Depth 2: "Set up React project structure" (ATOMIC)
   └─ Depth 2: "Implement blog post list view"
      ├─ Depth 3: "Create ArticleList component" (ATOMIC)
      └─ Depth 3: "Add pagination controls" (ATOMIC)

Execution flow:
1. Depth 1 children run in parallel (backend + frontend)
2. Within backend:
   - Depth 2 "Set up database" runs first
   - Depth 2 "Create REST endpoints" runs after (depends on database)
3. Within "Create REST endpoints":
   - All 3 endpoint ACs run in parallel (independent)
4. Within frontend:
   - Depth 2 "Set up project" runs first
   - Depth 2 "Implement list view" runs after
5. Leaf nodes (ATOMIC) execute Discover-Define-Design-Deliver
```

## Summary

The recursive AC decomposition system provides:

- **Adaptive complexity handling**: LLM-driven atomicity detection tailors execution to task needs
- **Intelligent parallelization**: Dependency-aware scheduling maximizes throughput while maintaining correctness
- **Scalable context management**: Graduated compression prevents prompt size explosion in deep trees
- **Robust safety**: Multiple validation layers prevent degenerate cases
- **Flexible configuration**: Two executor variants support different problem classes

This architecture enables Ouroboros to tackle problems ranging from simple atomic tasks to complex multi-domain workflows requiring extensive decomposition.
