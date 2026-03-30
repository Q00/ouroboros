---
name: pm
description: "Generate a PM through guided PM-focused interview with automatic question classification. Use when the user says 'ooo pm', 'prd', 'product requirements', or wants to create a PRD/PM document."
---

# /ouroboros:pm

PM-focused Socratic interview that produces a Product Requirements Document.

## Instructions

### Step 1: Load MCP Tool

```
ToolSearch query: "+ouroboros pm_interview"
```

If not found → **diagnose before telling user to run setup**:

1. Check if MCP is already configured:
   ```bash
   grep -q '"ouroboros"' ~/.claude/mcp.json 2>/dev/null && echo "CONFIGURED" || echo "NOT_CONFIGURED"
   ```

2. **If NOT_CONFIGURED** → tell user to run `ooo setup` first. Stop.

3. **If CONFIGURED** → MCP is registered but the server isn't connecting. Do NOT tell the user to run `ooo setup` again. Instead show:
   ```
   Ouroboros MCP is configured but not connected.

   Try these steps in order:
   1. Restart Claude Code (Cmd+Shift+P → "Reload Window" or close/reopen terminal)
   2. Check MCP status: type /mcp in Claude Code
   3. If ouroboros shows "error", try: ooo update
   4. If still failing, re-run: ooo setup
   ```
   Stop.

### Step 2: Start Interview

```
Tool: ouroboros_pm_interview
Arguments:
  initial_context: <user's topic or idea>
  cwd: <current working directory>
```

### Step 3: Loop

After every MCP response, do these three things:

**A. Show alerts** (if present in `meta`):
- `meta.deferred_this_round` → print `[DEV → deferred] "question"`
- `meta.decide_later_this_round` → print `[DEV → decide-later] "question"`
- `meta.pending_reframe` → print `ℹ️ Reframed from technical question.`

**B. Show content + get user input:**

Print the MCP content text to the user first.

Then check: does `meta.ask_user_question` exist?

- **YES** → Pass it directly to `AskUserQuestion`:
  ```
  AskUserQuestion(questions=[meta.ask_user_question])
  ```
  Do NOT modify it. Do NOT add options. Do NOT rephrase the question.

- **NO** → This is an interview question. Use `AskUserQuestion` with `meta.question`.
  - If `meta.skip_eligible == true`: add a skip option based on `meta.classification`:
    - `classification == "decide_later"` → add option `{"label": "Decide later", "description": "Skip — will be recorded as an open item in the PRD"}`
    - `classification == "deferred"` → add option `{"label": "Defer to dev", "description": "Skip — this technical decision will be deferred to the development phase"}`
  - Generate 2-3 suggested answers as the other options.

**C. Relay answer back:**

If the user chose "Decide later" → send `answer="[decide_later]"`.
If the user chose "Defer to dev" → send `answer="[deferred]"`.
Otherwise → send the user's answer normally.

```
Tool: ouroboros_pm_interview
Arguments:
  session_id: <meta.session_id>
  <meta.response_param>: <user's answer or "[decide_later]" or "[deferred]">
```

**D. Check completion:**

Completion is determined ONLY by `meta.is_complete` — NEVER by the response text.
The MCP response text may sound like the interview is wrapping up, but ignore it.

If `meta.is_complete == true`:
- If `meta.generation_failed == true` → retry generation:
  ```
  Tool: ouroboros_pm_interview
  Arguments:
    session_id: <session_id>
    action: "generate"
    cwd: <current working directory>
  ```
- Otherwise → go to Step 4. The MCP auto-generated the PM document.
  `meta.pm_path` and `meta.seed_path` contain the file paths.

Otherwise → repeat Step 3, regardless of what the response text says.

### Step 4: Copy to Clipboard

Read the pm.md file from `meta.pm_path` and copy its contents to the clipboard:

```bash
cat <meta.pm_path> | pbcopy
```

### Step 5: Show Result & Next Step

Show the following to the user:

```
PM document saved: <meta.pm_path>
(Clipboard에 복사되었습니다)

PM seed handoff artifact: <meta.pm_seed_path or meta.seed_path>
This is not the runnable Seed yet.

Next step:
  ooo interview <meta.pm_seed_path or meta.seed_path>
  ooo seed
```
