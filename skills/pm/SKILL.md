---
name: pm
description: "Generate a PM through guided PM-focused interview with automatic question classification. Use when the user says 'ooo pm', 'prd', 'product requirements', or wants to create a PRD/PM document."
mcp_tool: ouroboros_pm_interview
mcp_args:
  initial_context: "$1"
  cwd: "$CWD"
---

# /ouroboros:pm

PM-focused Socratic interview that produces a Product Requirements Document.

## Instructions

When the user invokes this skill:

### Step 0: Load Tools

**If `ToolSearch` is available** (Claude Code): call in a single message:
1. `ToolSearch` with `+ouroboros pm_interview`
2. `ToolSearch` with `select:AskUserQuestion`

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded via the configured MCP server. Skip directly to Step 1.

Store whichever question tool is available (`AskUserQuestion` or `AskQuestion`) as the **question tool**.
- If PM MCP tool available → **Path A**. If unavailable → **Path B**.

## Shared Rules

- Read `agents/pm-interviewer.md` as the source of truth for PM questioning behavior.
- Ask one PM-facing question at a time. Focus on product goals, scope, user workflows, constraints, non-goals, and success criteria.
- Do not ask implementation questions directly. If a technical ambiguity matters now, reframe it for a PM. Otherwise defer it to the later dev interview.
- **CRITICAL: Always present PM questions using the question tool loaded in Step 0. Never output a question as plain text.**
  - If `AskUserQuestion` is available, call it with `question` and `options` from `question_spec`.
  - If neither tool is available, format as numbered markdown block.
- If `question_spec` is absent or has no options, synthesize a minimal UI with `Not sure yet` and `Other`.
- After PM completion, always generate `pm.md` and suggest `ooo interview <pm.md path>`.

## Path A: MCP + Native Subagent

Use this when the PM MCP tool is available and the runtime supports native subagents.

### Role Split

- `ouroboros_pm_interview`: PM state CRUD only
- main session: orchestrates loop, records turns, shapes question UI
- `@pm-interviewer`: reads PM state, scores ambiguity, returns next PM question

### Flow

1. Start a PM session:
   ```
   ouroboros_pm_interview(action="start", initial_context=<topic>, cwd=<cwd>)
   ```

2. Spawn `@pm-interviewer` with only the `session_id`:
   ```
   Tool: Agent
   Arguments:
     subagent_type: "ouroboros:pm-interviewer"
     description: "Generate next PM question"
     prompt: "session_id: <session_id>"
   ```

3. The agent reads state, persists ambiguity via `action=score`, and returns:
   ```json
   {"question":"...", "ambiguity_score":0.46, "seed_ready":false,
    "question_spec":{...}, "classification":"passthrough|reframed",
    "original_question":null, "deferred_this_round":[], "decide_later_this_round":[]}
   ```

4. Surface alerts when arrays are non-empty:
   - `deferred_this_round` → `[DEV → deferred] "<question>"`
   - `decide_later_this_round` → `[DEV → decide-later] "<question>"`
   - If `original_question` exists → mention the PM question was reframed from a technical question.

5. Present the question by **calling the question tool** with `question_spec` options. If `question_spec` is absent, synthesize a minimal UI.

6. Record the full turn:
   ```
   ouroboros_pm_interview(action="record_turn", session_id=<id>,
     question=<q>, answer=<a>, ambiguity_score=<score>,
     classification=<cls>, original_question=<orig>,
     deferred_this_round=[...], decide_later_this_round=[...])
   ```

7. Repeat from step 2 until `seed_ready` is `true`.

8. Complete: `ouroboros_pm_interview(action="complete", session_id=<id>)`

9. Generate the PM document:
   ```
   ouroboros_pm_interview(action="generate", session_id=<id>, cwd=<cwd>)
   ```

### Context Rule

- Do not pass full Q&A history into the subagent prompt.
- Keep the main session as a thin orchestrator.

### Retry Rule

- Retry the native path once.
- If it still fails, continue via **Path A Fallback**.

### Path A Fallback: Original Internal MCP Flow

If native subagent spawning fails twice:

1. Call `ouroboros_pm_interview(initial_context=<topic>, cwd=<cwd>)` without explicit actions.
2. The MCP tool generates the next PM question internally.
3. Show alerts from `meta`:
   - `meta.deferred_this_round`
   - `meta.decide_later_this_round`
   - `meta.pending_reframe`
4. Show the MCP content text to the user.
5. If `meta.ask_user_question` exists → pass it directly to the question tool. Do NOT modify it.
6. Otherwise → present `meta.question` with the question tool, adding 2-3 suggested answers.
7. Relay the answer: `ouroboros_pm_interview(session_id=<id>, <meta.response_param>=<answer>)`
8. Check `meta.is_complete` — if `true` → generate. Otherwise repeat from step 2.
9. Generate: `ouroboros_pm_interview(session_id=<id>, action="generate", cwd=<cwd>)`

## Path B: No MCP

If MCP is unavailable:

1. Read `agents/pm-interviewer.md` and follow the same PM questioning discipline directly.
2. Ask one PM-facing question at a time using the question tool.
3. Track deferred or decide-later items in conversation context.
4. When complete, generate `pm.md` manually from the captured answers.

## Finish

After generation:

1. Read `meta.pm_path`
2. Copy its contents to the clipboard:
   ```bash
   cat <meta.pm_path> | pbcopy
   ```
3. Show:
   ```
   PM document saved: <meta.pm_path>
   (Copied to clipboard)

   Next step:
     ooo interview <meta.pm_path>
   ```
