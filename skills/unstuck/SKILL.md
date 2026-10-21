---
name: unstuck
description: "Use when the user is stuck or going in circles: the same fix failed twice or more, 'nothing works', 'I've tried everything', or a consequential choice with no clear winner that is hard to undo or splits the team (which database, which architecture). Brings hacker, researcher, simplifier, architect, and contrarian perspectives and converges on one recommendation. Not for a first error or normal progress."
aliases: [stuck, lateral]
---

# /ouroboros:unstuck

Break through stagnation, or advise on a hard choice, with lateral thinking personas. **Solo** runs one persona; **debate** runs several in parallel and synthesizes them.

## Usage

```
ooo lateral                       # debate: all 5 lateral personas
ooo lateral <persona>             # solo: one persona
ooo lateral debate <p1> <p2> ...  # debate with explicit members
ooo lateral @all                  # debate preset (the only preset)
```

## Personas (lateral pool)

Stateless reframing lenses only; stateful roles (evaluator, qa-judge, ontologist, socratic-interviewer) have their own skills.

| Persona | Lens | Use when |
|---------|------|----------|
| **hacker** | Make it work first, elegance later | Overthinking blocks progress |
| **researcher** | What information are we missing? | The problem is unclear |
| **simplifier** | Cut scope, return to MVP | Complexity is overwhelming |
| **architect** | Restructure the approach entirely | The current design is wrong |
| **contrarian** | Are we solving the wrong problem? | Assumptions need challenging |

Besides direct `ooo lateral`, you MAY chain into this skill from another skill (for example as `socratic-interviewer` in `ooo interview`) when the current question needs multi-perspective deliberation; afterwards summarize the options, return to the parent flow, and let the user decide.

## Instructions

### Step 1: Parse args into a mode

| Input | Mode | Members |
|---|---|---|
| no args, `debate`, or `@all` | debate | all 5 personas |
| `<persona>` | solo | that persona |
| `debate <p1> <p2> ...` | debate | the listed personas |
| `@<unknown-preset>` | error | reject and list known presets |
| `<unknown-persona>` | error | reject and list the 5 personas |
| `<p1> <p2> ...` without `debate` | error | reject and suggest `ooo lateral debate <p1> <p2>` |

Validate every persona name against the pool; never coerce silently. When a parent skill wants one persona but names none, omit `persona` and pass `stagnation_pattern` (`spinning`, `oscillation`, `no_drift`, `diminishing_returns`) if known; the server picks the persona (default contrarian).

Pick the lateral **mode** too. Use `mode="decision"` when the user faces a consequential choice between options (which database, which architecture) rather than a stall; otherwise omit `mode` (default `unstuck`). In decision mode, `problem_context` describes the options and `current_approach` the currently favored one (or `undecided`), and an omitted persona means a full debate. Pass `research=true` only when the user explicitly asks for web-grounded research; it is slower and never the default.

### Step 2: Gather required context before the MCP call

The tool hard-fails when `problem_context` or `current_approach` is empty, so resolve both first (solo and debate alike):

1. Reuse session state from the parent skill or recent conversation: `problem_context` (what the user is stuck on, 1-3 sentences), `current_approach` (what was tried; `"none yet, first attempt"` is fine), and optional `failed_attempts`.
2. If either field is unrecoverable, ask the user one combined question with the active runtime's `ask_user` capability: "Two things I need before lateral thinking can run: (1) what are you stuck on right now, (2) what have you already tried? A sentence each is enough." Wait for the answer. Never fabricate these fields; `current_approach="not specified"` is acceptable, invented content is not.

### Step 3: Always call `ouroboros_lateral_think`

Every invocation routes through the MCP tool, solo and debate, in every runtime; never substitute a direct subagent fan-out.

#### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros lateral"` for `ouroboros_lateral_think`; `"+ouroboros conductor decision"` for `ouroboros_record_conductor_decision` (decision mode). A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. If the tool is genuinely absent (neither exposed nor loadable), stop and report that the MCP dispatch surface is broken; do not improvise solo or debate without it.

Call it with:
- **Solo**: `persona=<one>`, `problem_context`, `current_approach`, `failed_attempts`.
- **Debate**: `personas=[...]`, `problem_context`, `current_approach`, `failed_attempts`.
- Plus `mode`/`research` from Step 1, and `session_id` when running inside an interview or lateral session (it binds the fan-out so results submitted from another session are refused).

### Step 4: Act on the response shape (the server picks it)

| Mode | Plugin response | Inline response |
|---|---|---|
| Solo | one `_subagent` object | one `# Lateral Thinking: <approach>` block in `content` |
| Debate | `_subagents` array (N objects) | N blocks joined by `\n\n---\n\n`, plus a hidden dispatch block carrying the same N payloads |

**Inline dispatch block (debate, inline only).** Prefer the structured copy in the response meta when present. Otherwise take the text between `<!-- ouroboros-lateral-inline-dispatch-v1 base64\n` and `\n-->` at the end of `content`, base64-decode it, and parse the JSON. A host that did not declare subagent capability receives the neutral shape:

```json
{"dispatch_mode": "host_decides", "host_action": "dispatch_subagents_if_supported", "execution_preference": "parallel", "fallback_strategy": "sequential", "persona_count": N, "payloads": [...]}
```

An explicitly sequential execution authority receives `dispatch_mode="sequential"` with `legacy_dispatch_mode="inline_fallback"`, which is compatibility metadata only. Never split the joined display text on `---`: user content can contain the separator.

#### Shape A: plugin response (OpenCode plugin mode)

The plugin spawns the children itself. Await the single `_subagent` result (solo) or all `_subagents` results (debate), then present or synthesize. If the response is inline text with neither key, you are not in plugin mode: use Shape B.

#### Shape B: inline response

The dispatch block distinguishes declared `host_driven`, capability-neutral `host_decides`, and explicit `sequential` execution. **Solo**: present the approach summary, reframing prompt, and questions to consider, then route back to the workflow.

##### Debate, runtime supports sub-agent dispatch

1. Show the visible markdown under a short "what the lateral toolkit suggests" header.
2. With `host_action="spawn_subagents"`, or `host_action="dispatch_subagents_if_supported"` when the runtime has a native parallel primitive, use the active runtime's `orchestrate_subagents` capability: one isolated child per payload, launched together, each given its `prompt` verbatim plus its `context`.
3. Await all children and correlate each result by `result_correlation_key` (normally `context.persona`).
4. Optional Round 2 cross-attack, only when Round 1 answers diverge: give each persona short summaries of the others and ask "Identify one weakness in each. 200 words max."
5. Synthesize (below).

##### Debate, constrained runtime without sub-agent dispatch

If the response is stamped with `dispatch_mode="sequential"`, or with `dispatch_mode="host_decides"` and the host has no native parallel primitive, process each recovered payload in order, correlate every result by `result_correlation_key`, and synthesize after the last one. If no payloads are recoverable (no meta copy and no dispatch block), present the handler's markdown as-is and synthesize from that text; there is no Round 2.

##### Synthesize (debate, all shapes)

In decision mode, follow the synthesis contract the response carries: exactly one recommendation stated first, its grounds (citing only sources personas actually fetched), the strongest dissent, and flip conditions. After the user accepts, offer to persist it with `ouroboros_record_conductor_decision` as the contract describes.

In unstuck mode, present options, not a verdict:

```
## Debate result: N personas

### Options
- **Option A** (from <persona>): <one-liner>
- **Option B** (from <persona>): <one-liner>

### Disagreements
- <persona X> vs <persona Y>: <what they disagree about>

### Recommended (with reasoning)
<your single best read, labeled as your recommendation>
```

In both modes the choice is the user's: never auto-progress to the next workflow step on their behalf.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
