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

### Step 1 — Load the MCP tool (required first)

The Ouroboros MCP tools are typically registered as deferred tools and must be explicitly loaded.

1. Call `ToolSearch` with query `"+ouroboros lateral"` to load `ouroboros_lateral_think` (often prefixed, e.g., `mcp__plugin_ouroboros_ouroboros__ouroboros_lateral_think`).
2. If the tool loads → use the MCP path below. If not → jump to **Fallback** at the end.

Do NOT skip this step. Deferred tools won't appear in your immediate tool list until ToolSearch runs.

### Step 2 — Parse args → decide mode

Parse the user's argument string (or your autonomous-chain intent):

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

### Step 3 — Dispatch

#### Solo mode

1. Determine the context: what is the user stuck on, what has been tried, why this persona.
2. Call `ouroboros_lateral_think`:
   - `problem_context`: description of the stuck situation
   - `current_approach`: what has been tried
   - `persona`: the chosen persona
   - `failed_attempts`: list of previous failures (if any)
3. Present the result: persona's approach summary, reframing prompt, questions to consider, and a `📍 Next:` suggestion routing back to the workflow.

#### Debate mode

1. Determine the context (same as solo).
2. Call `ouroboros_lateral_think` with **`personas=[...]`** (the list of members from Step 2). Optional: pass `problem_context`, `current_approach`, `failed_attempts`.
3. The handler returns a `_subagents` dispatch envelope — a list of N payloads, each `{tool_name, title, prompt, agent, model, context}`. **This is an *input* envelope (a dispatch list), not a verdict. You must fan it out.**
4. **Fan out — one independent LLM call per persona, in parallel.** How depends on your runtime:

   | Runtime | What you do |
   |---|---|
   | **Claude Code** | In a single message, emit N parallel `Task` (general-purpose subagent) calls — one per `_subagents` payload. The user sees "Running N agents…". |
   | **OpenCode plugin mode** | The plugin handles dispatch automatically (it spawns Task panes). You don't fan out manually; just await the result. |
   | **Codex CLI** | Use Codex's sub-agent mechanism, one call per payload. |
   | **Other (subprocess/inline)** | If no sub-agent mechanism is available, the handler's inline fallback returns a markdown block with all 5 answers. Use that — no manual fan-out, no visualization. |

   You can detect the plugin path from the envelope's `dispatch_mode` field; otherwise default to manual fan-out.

5. Wait for all N answers. Each sub-agent runs with **only its own persona definition + the problem** — do NOT cross-contaminate by passing other sub-agents' answers in Round 1.

6. **Round 2 (optional cross-attack)** — only if Round 1 answers diverge meaningfully. Issue a second fan-out (same N members) where each persona receives a short summary of the other four answers and is asked: "Identify one weakness in each. ≤200 words." If Round 1 already converges, skip.

7. **Synthesize → defer to user.** Do NOT emit a single verdict. Present the result like:

   ```
   ## Debate result — N personas

   ### Options
   - **Option A** (from <persona>): <one-liner>
   - **Option B** (from <persona>): <one-liner>
   - …

   ### Disagreements
   - <persona X> vs <persona Y>: <what they disagree about>

   ### Recommended (with reasoning)
   <your single best read of the debate, clearly labeled as your recommendation, not a verdict>

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

## Fallback (No MCP server)

If `ouroboros_lateral_think` cannot be loaded:

- **Solo mode** — delegate to the matching agent file. Read `src/ouroboros/agents/<persona>.md` and answer in that role. No numerical analysis; prompt-based reframing only.

- **Debate mode** — read all member persona files (`src/ouroboros/agents/{hacker,researcher,simplifier,architect,contrarian}.md` or the chosen subset), then in a single message emit one parallel `Task` call per persona. Each Task gets its persona's full file content + the problem context. After all return, synthesize per Step 3.7 above. Functionally equivalent to the MCP path; only the envelope assembly is manual.

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

### Debate (default)
```
> ooo lateral

[Running 5 agents in parallel: hacker, researcher, simplifier, architect, contrarian]
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
