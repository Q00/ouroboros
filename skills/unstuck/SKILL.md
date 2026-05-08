---
name: unstuck
description: "Break through stagnation with lateral thinking personas — single or multi-persona debate"
---

# /ouroboros:unstuck

Break through stagnation with lateral thinking personas. Two modes:
- **Solo** — one persona reframes the problem (fast, cheap).
- **Debate** — multiple personas run in parallel as sub-agents and the user picks the verdict (visual, thorough).

## Usage

```
ooo lateral                       # debate (default) — all 5 lateral personas
ooo lateral <persona>             # solo — single persona
ooo lateral debate <p1> <p2> ...  # debate with explicit members
ooo lateral @<preset>             # debate with preset (Phase 1: only @all = 5 personas)
```

Trigger keywords: "I'm stuck", "think sideways", "ooo lateral", "/ouroboros:unstuck".

## Personas (Lateral Pool)

The lateral pool is **stateless mindset personas only** — five reframing lenses. Stateful roles (evaluator, qa-judge, ontologist, socratic-interviewer, etc.) are NOT mixed into this pool; they have their own SKILLs.

| Persona | Style | When to Use |
|---------|-------|-------------|
| **hacker** | "Make it work first, elegance later" | When overthinking blocks progress |
| **researcher** | "What information are we missing?" | When the problem is unclear |
| **simplifier** | "Cut scope, return to MVP" | When complexity is overwhelming |
| **architect** | "Restructure the approach entirely" | When the current design is wrong |
| **contrarian** | "What if we're solving the wrong problem?" | When assumptions need challenging |

## When to Call

**Direct user invocation** — `ooo lateral …` from the prompt.

**Autonomous chain from another SKILL** — when you (the main session) are operating in another SKILL's persona (e.g., `socratic-interviewer` during `ooo interview`, or any agent role) and judge that the current question requires multi-perspective deliberation, you MAY invoke this SKILL on your own. No forced trigger; this is your self-assessment. After the debate, summarize the options for the user, return to the original SKILL's flow, and let the user decide. The user will see the sub-agent fan-out as it happens — that visibility is the point.

## Instructions

### Step 1 — Parse args → decide mode and path

Parse the user's argument string (or your autonomous-chain intent) into a mode (solo / debate):

| Input | Mode | Members |
|---|---|---|
| no args | `debate` | all 5 lateral personas |
| `debate` (keyword alone) | `debate` | all 5 lateral personas |
| `<persona>` (e.g., `hacker`) | `solo` | that one persona |
| `debate <p1> <p2> ...` | `debate` | the listed personas |
| `@all` | `debate` | all 5 lateral personas |
| `@<unknown-preset>` | error | reject + list known presets |
| `<unknown-persona>` | error | reject + list the 5 lateral personas |
| `<persona1> <persona2> ...` (no `debate` keyword) | error | reject + suggest `ooo lateral debate <p1> <p2>` |

Validate every persona name against the lateral pool above. If invalid, emit a brief error message naming the valid personas — do NOT silently coerce. Multiple persona tokens without the explicit `debate` keyword are rejected to keep the syntax unambiguous.

For **debate mode**, pick the dispatch path now — the chosen path determines whether Step 2 needs to run.

**Path selection rule:**
- Sub-agent dispatch available (Claude Code, Codex CLI, etc.) → **Path A** (preferred — no MCP needed for debate).
- OpenCode plugin mode → **Path B**.
- Neither (constrained subprocess, etc.) → **Path C** (inline).

The MCP path is **not** the debate-mode default for Claude Code or Codex.

### Step 2 — Load the MCP tool (only when required)

You need `ouroboros_lateral_think` for: **Solo mode**, and **Debate mode Path B** or **Path C**. **Debate mode Path A** does NOT call the MCP — skip this step.

When you do need the MCP:

1. Call `ToolSearch` with query `"+ouroboros lateral"` to load `ouroboros_lateral_think` (often prefixed, e.g., `mcp__plugin_ouroboros_ouroboros__ouroboros_lateral_think`).
2. If the tool loads → call it as instructed in Step 3. If not → for solo, read the persona file directly (see "When MCP is unavailable" below); for debate, just use Path A.

Deferred tools won't appear in your immediate tool list until ToolSearch runs.

### Step 3 — Dispatch

> **Important runtime fact**: `ouroboros_lateral_think` only emits a `_subagents` dispatch envelope when `should_dispatch_via_plugin(...)` is true (`src/ouroboros/mcp/tools/subagent.py:186-218`) — i.e., **OpenCode plugin mode only**. In Claude Code, Codex CLI, and every other runtime, the handler's inline path runs all personas internally and returns one markdown text blob (`src/ouroboros/mcp/tools/evaluation_handlers.py:1414+`). Do **not** call MCP and then wait for an envelope outside OpenCode plugin mode — it will not arrive.

#### Solo mode (any runtime)

1. Determine the context: what is the user stuck on, what has been tried, why this persona.
2. Call `ouroboros_lateral_think`:
   - `problem_context`: description of the stuck situation
   - `current_approach`: what has been tried
   - `persona`: the chosen persona
   - `failed_attempts`: list of previous failures (if any)
3. Receive inline text. Present the persona's approach summary, reframing prompt, questions to consider, and a `📍 Next:` suggestion routing back to the workflow.

#### Debate mode

Run the path you picked in Step 1.

##### Path A — Direct sub-agent fan-out (Claude Code, Codex CLI, any sub-agent-capable runtime)

This is the path for **most users**. The MCP is **not** called.

1. Read each member's persona definition from `src/ouroboros/agents/<persona>.md`. (Pool: `hacker`, `researcher`, `simplifier`, `architect`, `contrarian`.)
2. In a **single message**, emit N parallel `Task` calls (general-purpose subagent), one per persona. Each Task receives:
   - The full persona file content.
   - The problem context (`problem_context`, `current_approach`, `failed_attempts`).
   - Strict isolation: no other persona's output. The user sees "Running N agents…".
3. Wait for all N to return.
4. (Optional) **Round 2 cross-attack** — only if Round 1 answers diverge meaningfully. Dispatch a second N-fan-out where each persona receives short summaries of the other answers and is asked: "Identify one weakness in each. ≤200 words." Skip if Round 1 already converges.
5. Synthesize per the **Synthesize** block below.

##### Path B — MCP plugin dispatch (OpenCode plugin mode only)

When you are running inside OpenCode in plugin mode, the plugin can fan out for you.

1. Call `ouroboros_lateral_think` with `personas=[...]`. Optional: pass `problem_context`, `current_approach`, `failed_attempts`.
2. If `should_dispatch_via_plugin(...)` is true, the response includes a `_subagents` array — `[{tool_name, title, prompt, agent, model, context}, ...]`. The plugin spawns Task panes automatically; await the per-persona results.
3. If for any reason the response is **inline text** (not an envelope) — you are not actually in plugin mode. Treat it as Path C below; do not wait for an envelope.
4. (Optional) Round 2 cross-attack — same trigger and shape as Path A.
5. Synthesize.

##### Path C — Inline (any runtime, when sub-agent dispatch is unavailable)

If you cannot dispatch sub-agents (e.g., constrained subprocess) and you call `ouroboros_lateral_think(personas=[...])`, the handler returns a single markdown text containing all N persona answers concatenated. Present it directly — there is no per-persona visualization, and Round 2 cross-attack is not available without sub-agents.

##### Synthesize (all paths)

Do **not** auto-emit a verdict. Present:

```
## Debate result — N personas

### Options
- **Option A** (from <persona>): <one-liner>
- **Option B** (from <persona>): <one-liner>
- …

### Disagreements
- <persona X> vs <persona Y>: <what they disagree about>

### Recommended (with reasoning)
<your single best read of the debate, clearly labeled as your
recommendation, not a verdict>

📍 Next: pick an option, or `ooo lateral debate <subset>` to drill into a disagreement
```

The verdict is the user's. Never auto-progress to the next workflow step on the user's behalf — wait for their choice.

## Persona-selection heuristics (when args are empty and you must pick one for solo)

You only need this if a parent SKILL or the user explicitly requests *one* persona but doesn't name one. In debate mode, no selection needed (use all 5).

- Repeated similar failures → **contrarian** (challenge assumptions)
- Too many options → **simplifier** (reduce scope)
- Missing information → **researcher** (seek data)
- Analysis paralysis → **hacker** (just make it work)
- Structural issues → **architect** (redesign)

## When MCP is unavailable

- **Solo mode** — read `src/ouroboros/agents/<persona>.md` and answer in that role directly. No numerical analysis; prompt-based reframing only.
- **Debate mode** — already covered by Path A above; no MCP call is required for debate on sub-agent-capable runtimes.

## Examples

### Solo
```
User: I'm stuck on the database schema design.
> ooo lateral simplifier

# Lateral Thinking: Reduce to Minimum Viable Schema
Start with exactly 2 tables. If you can't build the core feature
with 2 tables, you haven't found the core feature yet.

📍 Next: try this, then `ooo run` — or `ooo interview` to re-examine.
```

### Debate (default — Path A, Claude Code / Codex CLI)
```
> ooo lateral

[Read src/ouroboros/agents/{hacker,researcher,simplifier,architect,contrarian}.md]
[Single message: 5 parallel Task calls — one per persona, isolated]
[User sees "Running 5 agents…"]
[Round 1 returns]

## Debate result — 5 personas
### Options
- A (hacker): ship the 50-line version, defer correctness
- B (architect): the data model is wrong, redesign before coding
- C (simplifier): cut feature X, the rest fits in one file
- D (researcher): we don't have user data; instrument first
- E (contrarian): are users actually asking for this?

### Disagreements
- hacker vs architect: ship-first vs redesign-first
- contrarian vs all: whether the problem is real

### Recommended
Lean toward C+E: validate the need (E) before scoping (C); A and B
both assume the feature is wanted.

📍 Next: pick an option, or `ooo lateral debate hacker contrarian` to drill in.
```

### Autonomous chain from interview
```
[ooo interview mid-flow; main session is wearing the socratic-interviewer persona]
[Main session judges that the next question is too tangled — multiple
 reframings could be valid and asking the user to disambiguate would itself
 be confusing. It chains to this SKILL on its own.]

(autonomous) ooo lateral
[5-agent debate runs in parallel sub-agents; the user sees "Running 5 agents…"]
[Main session presents options + disagreements + a recommendation]
[User picks an option — or asks for a follow-up debate]
[Main session resumes the interview with the chosen framing]
```
