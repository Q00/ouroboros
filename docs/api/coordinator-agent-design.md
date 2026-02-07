# Coordinator Agent Architecture Design

> Generated: 2026-02-06
> Status: Phase 4 (Architecture Design) — Pending user decision
> Context: Section 7 Medium-term #4 + #6 absorption

---

## 1. Problem Statement

After parallel AC execution completes a dependency level, the system transitions to the next level with only mechanical context extraction (`extract_level_context()`). This misses:

1. **File conflicts**: Multiple ACs editing the same file concurrently
2. **Implementation quality issues**: ACs reporting "success" but producing incomplete work
3. **Integration gaps**: Parallel work that needs manual stitching

The Coordinator Agent acts as an **intelligent review gate** between levels.

---

## 2. Design Decisions (Confirmed)

| Question | Decision | Rationale |
|----------|----------|-----------|
| Conflict handling | Auto-resolve via Claude | Coordinator gets Edit/Bash tools to fix conflicts directly |
| Agent capability | Full agent (Read, Bash, Edit, Grep, Glob) | Can inspect files, run git diff, resolve conflicts |
| Relationship to extract_level_context() | Enhance (not replace) | Existing mechanical extraction stays; Coordinator adds intelligent review on top |
| #6 Inter-AC Messaging | Absorbed into #4 | Coordinator queries in-memory ACExecutionResult data (already contains all tool events with file paths) instead of building separate messaging |

---

## 3. Key Insight: No EventStore Query Needed

Explorer agents discovered that `ACExecutionResult.messages` already contains **all tool calls with full inputs** (file_path, content, etc.). The `extract_level_context()` function already extracts `files_modified` from Write/Edit tool inputs.

Therefore, **conflict detection can be done entirely in Python** from the in-memory level results — no EventStore schema changes needed.

EventStore events (`execution.tool.started`) are still emitted for TUI/observability but are NOT the data source for the Coordinator.

---

## 4. Insertion Point

**File**: `src/ouroboros/orchestrator/parallel_executor.py`
**Location**: Lines 416-423 in `execute_parallel()` — between `level_completed` event emission and next level start.

```
Current flow:
  Level N executes → process results → emit level_completed → extract_level_context → Level N+1

New flow:
  Level N executes → process results → emit level_completed
    → extract_level_context (mechanical)
    → detect_file_conflicts (Python code)
    → IF conflicts OR quality concerns:
        → run Coordinator Claude session (auto-resolve)
    → attach CoordinatorReview to LevelContext
    → Level N+1 (with enriched context)
```

---

## 5. Architecture Approaches

### Approach A: Pragmatic (Recommended)

**Principle**: Python-based conflict detection + Claude session only when needed.

```
Level N complete
  ↓
1. extract_level_context() — existing (mechanical)
  ↓
2. _detect_file_conflicts() — NEW Python function
   Analyzes ACExecutionResult.messages for Write/Edit to same file_path
   Returns: list of (file_path, [ac_indices]) conflicts
  ↓
3. IF conflicts exist:
     → Start Coordinator Claude session
       tools: [Read, Bash, Edit, Grep, Glob]
       prompt: conflict details + "review and resolve"
       Claude runs git diff, reads files, applies fixes
   ELSE:
     → Skip (zero cost)
  ↓
4. Create CoordinatorReview → attach to LevelContext
  ↓
Level N+1 starts with enriched context
```

**Cost**: 0 Claude sessions when no conflicts. 1 session per level when conflicts detected.

### Approach B: Full Agent (Every Level)

**Principle**: Always run Coordinator, regardless of conflict detection.

```
Level N complete
  ↓
1. extract_level_context()
  ↓
2. Always start Coordinator Claude session
   - Review all results (not just conflicts)
   - Check implementation quality
   - Verify integration between parallel work
   tools: [Read, Bash, Edit, Grep, Glob]
  ↓
3. Create CoordinatorReview → attach to LevelContext
  ↓
Level N+1 starts with enriched context
```

**Cost**: 1 Claude session per level, always.

### Comparison

| Dimension | A: Pragmatic | B: Full Agent |
|-----------|-------------|---------------|
| Claude calls | Conflicts only | Every level |
| Cost | Low (usually 0) | 1 per level |
| Conflict detection | Python (exact) | Python + Claude |
| Conflict resolution | Claude (when needed) | Claude (always) |
| Quality review | No | Yes |
| Implementation complexity | Low | Medium |
| EventStore changes | None | None |

---

## 6. Data Model

### New: `CoordinatorReview`

```python
@dataclass(frozen=True, slots=True)
class CoordinatorReview:
    level_number: int
    conflicts_detected: tuple[FileConflict, ...]
    review_summary: str          # Coordinator's analysis text
    fixes_applied: tuple[str, ...]  # Descriptions of fixes made
    warnings_for_next_level: tuple[str, ...]  # Injected into next level prompt
    duration_seconds: float
    session_id: str | None = None

@dataclass(frozen=True, slots=True)
class FileConflict:
    file_path: str
    ac_indices: tuple[int, ...]  # Which ACs touched this file
    resolved: bool
    resolution_description: str = ""
```

### Modified: `LevelContext`

Add optional `coordinator_review` field:

```python
@dataclass(frozen=True, slots=True)
class LevelContext:
    level_number: int
    completed_acs: tuple[ACContextSummary, ...] = field(default_factory=tuple)
    coordinator_review: CoordinatorReview | None = None  # NEW
```

### Modified: `build_context_prompt()`

When `coordinator_review` is present, append review section:

```
## Previous Work Context
- AC 1: Created user model (src/models.py)
- AC 3: Created API routes (src/routes.py)

## Coordinator Review (Level 1)
Conflicts resolved: src/app.py (AC 1 and AC 3 both modified)
Warnings: Ensure you import the new routes in src/main.py
```

---

## 7. New Files

| File | Purpose |
|------|---------|
| `src/ouroboros/orchestrator/coordinator.py` | CoordinatorReview dataclass + LevelCoordinator class |
| `tests/unit/orchestrator/test_coordinator.py` | Unit tests |

## 8. Modified Files

| File | Changes |
|------|---------|
| `src/ouroboros/orchestrator/parallel_executor.py` | Insert coordinator call in level loop (lines 416-423) |
| `src/ouroboros/orchestrator/level_context.py` | Add `coordinator_review` field to LevelContext + prompt injection |
| `src/ouroboros/orchestrator/__init__.py` | Export new symbols |

---

## 9. Coordinator Prompt Template

```
You are a Level Coordinator reviewing parallel AC execution results.

## Level {N} Results
{level_context.to_prompt_text()}

## File Conflicts Detected
{conflict_details}

## Your Tasks
1. Read the conflicting files using the Read tool
2. Run `git diff` to understand the actual changes
3. If conflicts exist, resolve them using Edit tool
4. Provide a summary of:
   - What each AC accomplished
   - Any conflicts found and how you resolved them
   - Warnings or recommendations for the next level's ACs

Respond with a structured review.
```

---

## 10. Open Question

**Which approach?** A (Pragmatic, Claude only on conflicts) or B (Full agent every level)?

Recommendation: **Start with A**, add B as opt-in flag (`--coordinator-mode=always`) later.
