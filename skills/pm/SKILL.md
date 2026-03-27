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

Then use `AskUserQuestion` with `meta.question` and generate 2-3 suggested answers.
Do not wait for `meta.ask_user_question` — the PM backend does not emit that field.

**C. Relay answer back:**

```
Tool: ouroboros_pm_interview
Arguments:
  session_id: <meta.session_id>
  <meta.response_param>: <user's answer>
```

**D. Check completion:**

If `meta.is_complete == true` → go to Step 4.
Otherwise → repeat Step 3.

### Step 4: Generate

```
Tool: ouroboros_pm_interview
Arguments:
  session_id: <session_id>
  action: "generate"
  cwd: <current working directory>
```

### Step 5: Copy to Clipboard

After generation, read the pm.md file from `meta.pm_path` and copy its contents to the clipboard when a local clipboard tool exists:

```bash
if command -v pbcopy >/dev/null 2>&1; then
  cat <meta.pm_path> | pbcopy
elif command -v wl-copy >/dev/null 2>&1; then
  cat <meta.pm_path> | wl-copy
elif command -v xclip >/dev/null 2>&1; then
  cat <meta.pm_path> | xclip -selection clipboard
else
  echo "No clipboard tool found; skipping clipboard copy."
fi
```

### Step 6: Show Result & Next Step

Show the following to the user:

```
PM document saved: <meta.pm_path>
(Clipboard에 복사되었습니다)

Next step:
  ooo interview <meta.pm_path>
```
