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

### Step 1 — Parse args → mode

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

### Step 2 — Always call `ouroboros_lateral_think` (routing contract)

Per the `ooo` routing contract in `src/ouroboros/codex/ouroboros.md`, every `ooo lateral` invocation MUST route through the MCP tool — solo *and* debate, in every runtime. This SKILL never substitutes a direct sub-agent fan-out for the MCP call.

1. Call `ToolSearch` with query `"+ouroboros lateral"` to load `ouroboros_lateral_think` (often prefixed, e.g., `mcp__plugin_ouroboros_ouroboros__ouroboros_lateral_think`). Deferred tools won't appear until `ToolSearch` runs.
2. Invoke the tool with the parsed mode:
   - **Solo**: `persona=<one>`, `problem_context`, `current_approach`, `failed_attempts`.
   - **Debate**: `personas=[...]`, `problem_context`, `current_approach`, `failed_attempts`.
3. If `ToolSearch` cannot load the tool, **stop and report that the MCP dispatch surface is broken** — same rule the contract applies to `ooo auto`. Do not improvise a sub-agent fan-out as a workaround; that bypasses the contract the bot review explicitly flagged.

The MCP call is cheap. The handler's inline path is a *deterministic prompt builder* — it constructs per-persona reframing prompts via `LateralThinker.generate_alternative` (`src/ouroboros/mcp/tools/evaluation_handlers.py:1444+`); it does not run an LLM rollout. Calling it on every invocation costs almost nothing and gives you ready-to-dispatch persona scaffolds.

### Step 3 — Branch on the handler's response shape

The handler picks one of two response shapes based on `should_dispatch_via_plugin(...)` (`src/ouroboros/mcp/tools/subagent.py:186-218`). You do not choose; you observe and act. The envelope key further depends on the mode you called with — solo and debate are not symmetric:

| Mode | Plugin response | Inline response |
|---|---|---|
| Solo (`persona=...`) | single `_subagent` envelope (one object) — `evaluation_handlers.py:1536-1563` | single `# Lateral Thinking: <approach>` block |
| Debate (`personas=[...]`) | `_subagents` array (N objects) — `evaluation_handlers.py:1414+` | N blocks joined by `\n\n---\n\n` |

#### Shape A — `dispatch_mode = "plugin"` (OpenCode plugin mode only)

The plugin runtime spawns Task panes automatically from whichever envelope the handler emitted. You only need to read the right key and await the result(s):

##### Solo (plugin)

The response carries a single `_subagent` object (singular) — `{tool_name, title, prompt, agent, model, context}` — produced by `build_subagent_result` (`evaluation_handlers.py:1554+`). The plugin spawns one Task pane. Await its single result, then present the persona's reframing.

##### Debate (plugin)

The response carries a `_subagents` array (plural) — `[{tool_name, title, prompt, agent, model, context}, ...]` — produced by `build_multi_subagent_result` (`evaluation_handlers.py:1419+`). The plugin spawns N Task panes in parallel. Await all N results, then synthesize per the **Synthesize** block below.

If you expected plugin mode but the response is inline text (neither `_subagent` nor `_subagents`), you are not actually in plugin mode — fall through to Shape B; do not wait for an envelope that will not arrive.

#### Shape B — inline response (Claude Code, Codex CLI, OpenCode subprocess, every other runtime)

The handler ran the prompt builder internally and returned ready-to-use markdown:

- Solo response: a single `# Lateral Thinking: <approach>` block followed by the reframing prompt.
- Debate response (`dispatch_mode = "inline_fallback"`): N such blocks concatenated with `\n\n---\n\n` separators.

##### Solo (any runtime)

Present the persona's approach summary, reframing prompt, questions to consider, and a `📍 Next:` suggestion routing back to the workflow.

##### Debate, runtime supports sub-agent dispatch (Claude Code Task tool, Codex CLI sub-agent, etc.)

This is the **default debate UX for Claude Code and Codex**. The MCP-built scaffolds become the inputs to the Task fan-out — the MCP call has already happened (Step 2), so the contract is satisfied; this step only changes how the result is *rendered* to the user.

1. Split the returned markdown on `\n\n---\n\n` to recover N persona prompt blocks.
2. In a **single message**, emit N parallel `Task` calls (`general-purpose` subagent), one per block. Each Task receives:
   - Its persona prompt block (the scaffold returned by `ouroboros_lateral_think`).
   - The problem context (`problem_context`, `current_approach`, `failed_attempts`) so the persona can ground its answer.
   - Strict isolation: no other persona's output. The user sees "Running N agents…".
3. Wait for all N to return.
4. (Optional) **Round 2 cross-attack** — only if Round 1 answers diverge meaningfully. Dispatch a second N-fan-out where each persona receives short summaries of the other answers and is asked: "Identify one weakness in each. ≤200 words." Skip if Round 1 already converges.
5. Synthesize per the **Synthesize** block below.

##### Debate, runtime cannot dispatch sub-agents (constrained subprocess, no Task surface)

Present the concatenated markdown the handler returned, as-is. There is no per-persona visualization and no Round 2 cross-attack — both require a sub-agent surface. Synthesize directly from the inline text.

##### Synthesize (debate, all shapes)

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

The contract is "fail loud, don't substitute": if `ouroboros_lateral_think` cannot be loaded via `ToolSearch`, stop and report that the MCP dispatch surface is broken. Do not improvise either solo or debate by reading persona files directly when MCP-driven invocation was requested — that re-introduces the contract bypass the bot review flagged.

The one exception, retained for documented offline use: a parent SKILL operating in degraded-offline mode that has *already* announced it cannot reach MCP MAY read `src/ouroboros/agents/<persona>.md` and adopt that persona inline for solo reframing. This is not a fallback for `ooo lateral`; it's a degraded helper for a parent SKILL that has already given up on MCP. Debate has no offline equivalent — report the broken surface and stop.

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

### Debate (default — Claude Code / Codex CLI)
```
> ooo lateral

[ToolSearch loads ouroboros_lateral_think]
[Call ouroboros_lateral_think(personas=[hacker,researcher,simplifier,architect,contrarian], ...)]
[Handler returns inline markdown — 5 persona scaffolds joined by ---]
[Split on --- → 5 prompt blocks]
[Single message: 5 parallel Task calls — one block per persona, isolated]
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
