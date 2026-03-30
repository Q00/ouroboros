---
name: interview
description: "Socratic interview to crystallize vague requirements"
mcp_tool: ouroboros_interview
mcp_args:
  initial_context: "$1"
  cwd: "$CWD"
---

# /ouroboros:interview

Socratic interview to crystallize vague requirements into clear specifications.

## Usage

```
ooo interview [topic]
/ouroboros:interview [topic]
```

**Trigger keywords:** "interview me", "clarify requirements"

## Instructions

When the user invokes this skill:

### Step 0: Version Check (runs before interview)

Before starting the interview, check if a newer version is available:

```bash
curl -s --max-time 3 https://api.github.com/repos/Q00/ouroboros/releases/latest | grep -o '"tag_name": "[^"]*"' | head -1
```

Compare the result with `.claude-plugin/plugin.json`.

- If a newer version exists, ask whether to update first.
- If the user chooses update:
  1. Run `claude plugin marketplace update ouroboros`.
  2. Run `claude plugin update ouroboros@ouroboros`.
  3. Upgrade the MCP server:
     - `uv tool upgrade ouroboros-ai` if installed via `uv`
     - `pipx upgrade ouroboros-ai` if installed via `pipx`
     - otherwise tell the user to run `pip install --upgrade ouroboros-ai`
  4. Tell the user to restart and run `ooo interview` again.
- If the user chooses skip/continue, immediately continue the same interview invocation using the original topic or resumable session. Do not drop the pending `initial_context`.
- If the check fails, times out, or returns nothing: silently continue.

### Step 0.5: Load Tools (Required before Path A/B decision)

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded via the configured MCP server. Skip to the Path A/B decision below.

**If `ToolSearch` is available** (Claude Code): MCP tools may be registered as deferred tools that must be explicitly loaded.

1. Load the interview MCP tool with `ToolSearch`:
   ```
   +ouroboros interview
   ```
2. Load the structured question tool with `ToolSearch`:
   ```
   select:AskUserQuestion
   ```
   Store whichever tool becomes available (`AskUserQuestion` or `AskQuestion`) as the **question tool** for later use.
3. If the interview MCP tool becomes available, use **Path A** and fall back to **Path B** if native subagents are unavailable or fail.
4. If the interview MCP tool is not available, use **Path B**.

Do not assume MCP is unavailable until this lookup fails.

## Shared Rules

- Preserve the original topic/session across any update-check turn. If there is no topic and no active interview session to resume, ask for the interview topic first.
- Read `agents/socratic-interviewer.md` as the source of truth for questioning behavior.
- Ask one question at a time. Preserve breadth across ambiguity tracks and prefer closure once scope, outputs, verification, and non-goals are stable.
- Never make product or design decisions for the user.
- Use code scanning only for factual confirmation.
- If the subagent returns `question_spec`, treat it as the canonical UI contract for the next turn.
- The subagent generates the question. The main session only routes it and shapes the final user-facing UI.
- **CRITICAL: Always present interview questions using the question tool loaded in Step 0.5.** Never output a question as plain text.
  - If `AskUserQuestion` is available, call it with `question` and `options` from `question_spec`.
  - If `AskQuestion` is available, call it with the `cursor_question_payload` format.
  - If neither tool is available, format the question as a numbered markdown block:
    ```
    **Q: [question text]**

    1. Option A
    2. Option B
    3. Other (Write your own answer)

    > Please choose a number or enter your own answer.
    ```
- Keep the prompt and choices equivalent across platforms.
- Do not ask raw plain-text interview turns. If `question_spec` is absent or has no options, synthesize a minimal UI with `Not sure yet` and `Other`. If `question_spec.has_custom_input` is true, keep `Other` instead of a separate free-text follow-up.
- Suggest `ooo seed` only after the interview is complete or `seed_ready` is `true`.

### Routing Rule

Use the same three routing modes in both paths:

- `PATH 1` Code confirmation:
  read code first, present findings as a confirmation, prefix recorded answer with `[from-code]`
- `PATH 2` Human judgment:
  ask the user directly, no prefix
- `PATH 3` Code + judgment:
  present verified facts, then ask the user for the decision, no prefix

When in doubt, use `PATH 2`.

If three consecutive questions were effectively `PATH 1`, force the next turn to be `PATH 2` or `PATH 3`.

## Path A: MCP + Native Subagent

Use this path when the interview MCP tool is available and the runtime supports native subagents.

### Role Split

- `ouroboros_interview`: state CRUD only
- main session: orchestrates the loop, shapes the user-facing question, records the turn
- `@socratic-interviewer`: reads state, scores ambiguity, returns the next question

### Flow

0. If no topic argument is available and no resumable interview session exists, ask the user for a short topic/problem statement and stop. Do not start MCP yet.
1. Start a session with `action=start`, `initial_context=<topic>`, and `cwd=<current working directory>`.
2. Spawn `@socratic-interviewer` with only the `session_id`.
3. The agent reads state from MCP, persists ambiguity via `action=score`, and returns `{question, ambiguity_score, seed_ready}` and optionally `question_spec`.
4. Route the question via `PATH 1/2/3`, then present it by **calling the question tool** (loaded in Step 0.5) with the `question_spec` options. If `question_spec` is absent, synthesize a minimal single-select UI with `Not sure yet` and `Other`.
5. Record the full turn with `action=record_turn`.
6. Show only the ambiguity status line.
7. Repeat until the user stops or `seed_ready` is true.
8. Complete with `action=complete`.

### Context Rule

- Do not pass Q&A history into the subagent prompt.
- Do not restate prior turns between MCP calls.
- Keep the main session as a thin orchestrator.

### Retry Rule

- Retry the native path once.
- If it still fails, continue via **Path B**.

## Path B: Compatibility Fallback

Use this path when:

- the runtime cannot spawn native subagents, or
- the native path fails twice and continuity matters more than persistence, or
- the MCP tool is unavailable

### Flow

If the MCP tool is still available, prefer the original internal MCP flow:

1. Call `ouroboros_interview` without explicit native actions.
2. Let the MCP tool run the original internal LLM-in-MCP interview logic.
3. Present each returned question by **calling the question tool** with the same rules as Path A, then relay each answer back with `session_id`.
4. Keep the MCP tool as the source of truth for the interview state.

If the MCP tool is unavailable, fall back to direct interviewing:

1. Read `agents/socratic-interviewer.md` and follow the same interviewing discipline directly.
2. If brownfield context is likely, pre-scan the codebase and turn facts into confirmation-style questions.
3. Ask one clarifying question at a time by **calling the question tool** with the same rules as Path A.
4. Maintain the same breadth and closure discipline as the native path.

## Next Steps

After interview completion, use `ooo seed` to generate the Seed specification.
