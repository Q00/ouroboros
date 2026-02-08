# Parallel Execution Comparison: Claude Code Teammates vs Ouroboros Sub-AC

> Generated: 2026-02-06
> Context: P0-P3 Parallel DX Implementation via Claude Code teammate team

---

## Summary

Claude Code's teammate-based parallel execution and Ouroboros's Sub-AC parallel execution both use **identical Claude SDK sessions**, but differ fundamentally in **where orchestration intelligence is placed**.

> **Important clarification:** In the Claude Code teammate model, the "Team Lead" is itself a Claude session (the main CLI session), **not a human**. Both systems are fully AI-driven — the difference is architectural, not human-vs-AI.

### Claude Code Teammate Architecture

Each teammate is an **independent Claude Code session** that receives:

| Received | Details |
|----------|---------|
| `CLAUDE.md` | Project-level instructions (persona, rules, workflow) |
| Project memory | `~/.claude/projects/.../memory/MEMORY.md` |
| MCP servers | All connected MCP tools (Tavily, context7, etc.) |
| Skills | All installed skills |
| Spawn prompt | Task description from the lead session |
| Team tools | SendMessage, TaskList, TaskUpdate for coordination |

What teammates do **NOT** receive:
- The lead session's conversation history
- The lead session's system prompt (they load their own from CLAUDE.md)
- Other teammates' context or conversation state
- Custom system prompt (not configurable at spawn time)

---

## 1. Ouroboros Architecture (Actual)

```
Seed Goal
  |
  +-- DependencyAnalyzer (Claude, temp=0.0)
  |   "Analyze AC dependencies"
  |   -> DAG: ((0,2), (1,), (3))   <- Kahn's algorithm
  |
  +-- Level 0: AC-0, AC-2 execute in parallel
  |   |
  |   +-- AC-0 -> _try_decompose_ac() (Claude)
  |   |         "Is this atomic or should we split?"
  |   |         -> "ATOMIC" -> execute directly
  |   |
  |   +-- AC-2 -> _try_decompose_ac() (Claude)
  |             -> ["Sub1: login endpoint", "Sub2: signup endpoint"]
  |             -> Sub-ACs execute in parallel (anyio task group)
  |
  +-- Level 1: AC-1 executes (after AC-0 completes)
  |
  +-- Level 2: AC-3 executes (after AC-1, AC-2 complete)
```

### 3 AI Calls per AC (All Claude SDK)

The parallel executor pipeline (`runner.py` → `ParallelACExecutor`) makes **3 sequential AI calls**, all through `ClaudeCodeAdapter` (Claude SDK):

| AI Call | LLM | Tools | Role |
|---------|-----|-------|------|
| 1. Dependency Analysis | ClaudeCodeAdapter (Sonnet, temp=0.0) | None (analysis only) | Build inter-AC dependency DAG via `DependencyAnalyzer` |
| 2. Per-AC Decomposition | ClaudeCodeAdapter (adapter default) | None (tools=[]) | Each AC: "ATOMIC" or split into 2-5 Sub-ACs via `_try_decompose_ac()` |
| 3. AC/Sub-AC Execution | ClaudeCodeAdapter | Read, Edit, Bash, Glob, Grep | Actually execute the task — explore code, modify files |

```
AI Call 1 (once):     DependencyAnalyzer  → DAG with execution levels
AI Call 2 (per AC):   _try_decompose_ac() → "ATOMIC" or [Sub-AC list]
AI Call 3 (per AC):   _execute_atomic_ac() → actual code work
```

> **Note:** There is a separate `double_diamond.py` pipeline (used for sequential execution) that replaces Call 2 with a formal **Atomicity Check** (Gemini Flash, temp=0.3) + **Decomposition** (Claude Sonnet, temp=0.5) — two separate calls instead of one. The parallel executor combines these into a single prompt.

---

## 2. What's Identical

| | Claude Code Teammate | Ouroboros Sub-AC |
|---|---|---|
| Execution unit | Claude SDK session | Claude SDK session |
| Tools | Read, Edit, Bash, Glob, Grep | Read, Edit, Bash, Glob, Grep |
| Code understanding | Reads and modifies directly | Reads and modifies directly |
| Filesystem | Shared (same cwd) | Shared (same cwd) |
| Parallelism | Task tool (background) | anyio task group |

---

## 3. Core Difference: Intelligence Placement in Orchestration

```
                    Claude Code (teammate)      Ouroboros
                    ---------------------       ---------

  Planning          Lead Claude session          3 AI calls (all Claude SDK):
                    reads code, analyzes          1. DependencyAnalyzer (DAG)
                    -> creates 4 tasks            2. Per-AC decomposition
                    -> declares dependencies      3. Per-AC execution

  Instruction       "Remove the break on         "Improve CLI console
  Precision         line 350 of this file        to show tool details"
                    and replace with this code"

  Agent Autonomy    Low (typing-level)           High (explore->decide->implement)

  Inter-agent       Lead session relays          Completely isolated
  Awareness         "P0a done, start P0b"        Agents don't know each other exist

  Failure           Manual (Lead session         Automatic (Cascade Failure)
  Propagation       decides)

  Lead identity     Claude session (main CLI)    ParallelACExecutor (code)
```

---

## 4. Where Ouroboros Excels

### 4.1 Independence Principle in Decomposition

The parallel executor's `_try_decompose_ac()` prompt instructs:

```
"Each Sub-AC should be:
 - Independently executable
 - Specific and focused
 - Part of achieving the parent AC"
```

At decomposition time, the AI is instructed to create **independently executable** Sub-ACs -> no file-level locking needed.

> **Note:** The separate `double_diamond.py` pipeline uses an even stronger **MECE principle** ("Mutually Exclusive, Collectively Exhaustive — children should not overlap and should cover the full scope"). The parallel executor's prompt is simpler but achieves similar separation through "independently executable."

### 4.2 Automatic Atomicity Detection

In the parallel executor, Claude decides atomicity inline:

```
"If the AC is simple/atomic (can be done in one focused task), respond with: ATOMIC
 If this AC is complex (requires multiple distinct steps that could run independently),
 decompose it into 2-5 smaller Sub-ACs."
```

In Claude Code, the lead session decided ("dashboard_v3.py is complex, split into two agents") based on its own code analysis. Ouroboros automates this — each AC is individually assessed by Claude before execution.

### 4.3 Dependency Cascade Failure

```
AC-0 fails -> AC-1 (depends on 0) auto-skipped -> AC-3 (depends on 1) auto-skipped
```

The teammate model has no such automatic propagation. The lead session must manually decide "cancel P0b since P0a failed."

### 4.4 Repeatability & Scalability

- User provides only the Seed Goal; the entire pipeline runs automatically
- No lead-session bottleneck (Ouroboros orchestrator is deterministic code, not an LLM session)
- Same seed produces consistent execution plans (deterministic dependency analysis at temp=0.0)

---

## 5. Where Claude Code Teammates Excel

### 5.1 Real-time Inter-agent Communication

Ouroboros Sub-ACs **don't even know each other exist**. The system prompt never mentions "other agents are running in parallel." In Claude Code, the lead session can send DMs to teammates, relay intermediate results, and dynamically adjust execution order. Teammates can also message each other directly.

### 5.2 Same-file Region Splitting

p2-tree and p2-detail simultaneously modified the same `dashboard_v3.py` because the lead session explicitly bounded each agent in the spawn prompt: "you ONLY touch the NodeDetailPanel class." Ouroboros cannot do this fine-grained intra-file partitioning.

### 5.3 Reduced Exploration Cost via Precise Instructions

```
Claude Code Teammate: exact code provided -> immediate edit (minimal tokens)
Ouroboros Sub-AC:     abstract goal -> explore code -> decide -> implement (token-heavy)
```

---

## 6. Conclusion

| Dimension | Claude Code Teammate | Ouroboros |
|-----------|---------------------|-----------|
| Single-run accuracy | High (precise instructions) | Medium (AI judgment dependent) |
| Scalability | Low (lead session bottleneck) | High (fully automated) |
| Repeatability | Low (lead must re-analyze each time) | High (only Seed needed) |
| File conflict risk | Low (manual avoidance) | Low (MECE decomposition) |
| Cost efficiency | High (minimal exploration) | Medium (exploration overhead) |
| Agent autonomy | Low | High |

**Ouroboros's automated pipeline is the repeatable, scalable answer.** The Claude Code teammate approach is ideal for one-shot precision implementations, but the lead Claude session becomes the bottleneck — it must read all code, design all tasks, and write precise instructions for each teammate. Both systems are fully AI-driven; the difference is that Ouroboros distributes the planning intelligence across 3 specialized AI calls (dependency analysis → decomposition → execution), while Claude Code concentrates planning in a single lead session that must read all code and craft precise instructions before delegating execution to teammates.

---

## 7. Potential Improvements for Ouroboros

### Short-term (Prompt Engineering)

- **Sub-AC awareness**: Add parallel execution context to sub-agent system prompts ("Other agents are working on sibling ACs concurrently")
- **File boundary hints**: Enhance decomposition prompt with "each child should target distinct files or distinct sections within shared files"
- **Context injection**: Pass completed AC results as context to next-level ACs

### Medium-term (Architecture)

- **Coordinator agent**: Lightweight agent between levels that reviews Level N results before dispatching Level N+1
- **File impact prediction**: Pre-analyze which files each AC will likely touch, serialize if overlap detected
- **Inter-AC messaging**: Allow Sub-ACs to broadcast discoveries (e.g., "I created auth/models.py") to sibling agents

### Long-term (Convergence)

The two approaches could converge: Ouroboros could adopt **adaptive instruction precision** — using lightweight pre-analysis to generate more specific instructions when the codebase is well-understood, while falling back to autonomous exploration for novel domains.
