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

### Step 2 — Gather required context **before** the MCP call

`ouroboros_lateral_think` hard-fails if either `problem_context` or `current_approach` is empty (`evaluation_handlers.py:1324, 1333`). A bare `ooo lateral` from a fresh session has neither — calling MCP directly would crash before any persona work. Resolve both fields *before* Step 3, in this order:

1. **Reuse session state.** If a parent SKILL is invoking this one (autonomous chain) or the current Claude Code / Codex session has clearly recent stuck-point context, extract it. Build:
   - `problem_context` — what the user is stuck on (1–3 sentences, current state of the world).
   - `current_approach` — what has been tried so far (1–3 sentences). For a brand-new attempt, this can be `"none yet — first attempt"`.
   - `failed_attempts` (optional) — short list of prior failures, when the user has volunteered them.
2. **If either field is unrecoverable**, ask the user *one* short combined question before going further (this applies to both solo and debate — the handler requires both fields either way):
   > "Two things I need before lateral thinking can run: (1) what are you stuck on right now, (2) what have you already tried? A sentence each is enough."
   Wait for the answer. Do **not** invent or paraphrase past turns into these fields if you are not certain — `current_approach="not specified"` is acceptable, fabricated content is not.
3. Only when both fields are populated, proceed to Step 3.

This pre-call branch applies to solo *and* debate. Skipping it makes `ooo lateral` (no args, fresh session) reliably error.

### Step 3 — Always call `ouroboros_lateral_think` (routing contract)

Per the `ooo` routing contract in `src/ouroboros/codex/ouroboros.md`, every `ooo lateral` invocation MUST route through the MCP tool — solo *and* debate, in every runtime. This SKILL never substitutes a direct sub-agent fan-out for the MCP call.

1. Call `ToolSearch` with query `"+ouroboros lateral"` to load `ouroboros_lateral_think` (often prefixed, e.g., `mcp__plugin_ouroboros_ouroboros__ouroboros_lateral_think`). Deferred tools won't appear until `ToolSearch` runs.
2. Invoke the tool with the parsed mode and the context from Step 2:
   - **Solo**: `persona=<one>`, `problem_context`, `current_approach`, `failed_attempts`.
   - **Debate**: `personas=[...]`, `problem_context`, `current_approach`, `failed_attempts`.
3. If `ToolSearch` cannot load the tool, **stop and report that the MCP dispatch surface is broken** — same rule the contract applies to `ooo auto`. Do not improvise a sub-agent fan-out as a workaround; that bypasses the contract the bot review explicitly flagged.

The MCP call is cheap. The handler's inline path is a *deterministic prompt builder* — it constructs per-persona reframing prompts via `LateralThinker.generate_alternative` (`src/ouroboros/mcp/tools/evaluation_handlers.py:1444+`); it does not run an LLM rollout.

### Step 4 — Branch on the handler's response shape

The handler picks one of two response shapes based on `should_dispatch_via_plugin(...)` (`src/ouroboros/mcp/tools/subagent.py:186-218`). You do not choose; you observe and act. The envelope key further depends on the mode you called with — solo and debate are not symmetric:

| Mode | Plugin response | Inline response |
|---|---|---|
| Solo (`persona=...`) | single `_subagent` envelope (one object) — `evaluation_handlers.py:1536-1563` | single `# Lateral Thinking: <approach>` block |
| Debate (`personas=[...]`) | `_subagents` array (N objects) — `evaluation_handlers.py:1414+` | N blocks joined by `\n\n---\n\n` in `content`, **plus** the same canonical N payloads under `meta.payloads` for structured access |

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

This is the **default debate UX for Claude Code and Codex**. The MCP call has already happened (Step 3), so the routing contract is satisfied; this step only changes how the *already-built* canonical prompts are rendered to the user.

Use the structured `meta.payloads` array on the MCP response. It carries the **same per-persona prompts that plugin mode dispatches** — produced by `build_lateral_multi_subagent` (`src/ouroboros/mcp/tools/subagent.py:955+`) and exposed on inline responses for exactly this purpose. Each payload is a `{tool_name, title, prompt, agent, model, context}` dict; `prompt` is self-contained (the canonical reframing + the "Task for you (subagent)" wrapper that asks for a concrete plan, the biggest assumption challenged, and a one-line verdict).

Driving fan-out from `meta.payloads` instead of from the joined text avoids two pitfalls the bot review surfaced:
- **Separator collision** — `\n\n---\n\n` can legitimately appear inside a user-supplied `problem_context` or `current_approach`, so splitting the joined text would over-fragment and corrupt prompts. `meta.payloads` is structured, not parsed.
- **Behavioral drift across runtimes** — using `meta.payloads` keeps the per-persona prompt text identical to what `_subagents` carries in plugin mode (same canonical builder), so debate results don't diverge by environment.

1. Read `meta.payloads` from the MCP response. If it's missing (older handler) fall through to the constrained-runtime path below — *do not* split the joined text.
2. Surface a short "what the lateral toolkit suggests" header to the user so the MCP call is visible as a real product surface, not silent.
3. In a **single message**, emit N parallel `Task` calls (`general-purpose` subagent) — one per entry in `meta.payloads`. Each Task receives the payload's `prompt` verbatim plus the payload's `context` so the persona is grounded. Strict isolation per Task. The user sees "Running N agents…".
4. Wait for all N to return.
5. (Optional) **Round 2 cross-attack** — only if Round 1 answers diverge meaningfully. Dispatch a second N-fan-out where each persona receives short summaries of the other answers and is asked: "Identify one weakness in each. ≤200 words." Skip if Round 1 already converges.
6. Synthesize per the **Synthesize** block below.

##### Debate, runtime cannot dispatch sub-agents (constrained subprocess, no Task surface)

Present the concatenated markdown the handler returned, as-is — no parsing required, the user reads the whole text. There is no per-persona visualization and no Round 2 cross-attack — both require a sub-agent surface. Synthesize directly from the inline text.

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

[Step 2: Confirm problem_context + current_approach are present;
 ask the user one short combined question if not]
[ToolSearch loads ouroboros_lateral_think]
[Call ouroboros_lateral_think(personas=[hacker,researcher,simplifier,architect,contrarian], ...)]
[Handler returns: content = inline markdown (surfaced to user),
                  meta.payloads = N canonical per-persona payloads]
[Single message: 5 parallel Task calls — one per meta.payloads entry,
 each Task receives payload.prompt + payload.context verbatim;
 no parsing of the joined text is required]
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
