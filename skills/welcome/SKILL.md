---
name: welcome
description: "First-touch experience for new Ouroboros users"
---

# /ouroboros:welcome

Interactive onboarding for new Ouroboros users.

## Usage

```
/ouroboros:welcome              # First-time or update onboarding
/ouroboros:welcome --skip       # Skip welcome, mark as shown
/ouroboros:welcome --force      # Force re-run welcome even if shown
```

## Instructions

### Load Question Tool

**If `ToolSearch` is not available** (Cursor, other runtimes): `AskUserQuestion` is already loaded. Use it directly for all user-facing choices below.

**If `ToolSearch` is available** (Claude Code):
```
ToolSearch query: "select:AskUserQuestion"
```
Store whichever tool becomes available (`AskUserQuestion` or `AskQuestion`) as the **question tool**. Use it for all user-facing choices below. If neither is available, present choices as numbered markdown options.

When this skill is invoked, follow this flow:

---

### Pre-Check: Already Completed?

First, check `~/.ouroboros/prefs.json` for `welcomeCompleted`:

```bash
PREFFILE="$HOME/.ouroboros/prefs.json"

if [ -f "$PREFFILE" ]; then
  WELCOME_COMPLETED=$(jq -r '.welcomeCompleted // empty' "$PREFFILE" 2>/dev/null)
  WELCOME_VERSION=$(jq -r '.welcomeVersion // empty' "$PREFFILE" 2>/dev/null)

  if [ -n "$WELCOME_COMPLETED" ] && [ "$WELCOME_COMPLETED" != "null" ]; then
    ALREADY_COMPLETED="true"
  fi
fi
```

**If `ALREADY_COMPLETED` is true AND no `--force` flag:**

**Ask using the question tool:**
- Prompt: `Ouroboros welcome was already completed on $WELCOME_COMPLETED. What would you like to do?`
- Options: `Skip`, `Re-run welcome`
- **Skip**: Mark as complete and exit
- **Re-run welcome**: Continue to Step 1 below

**If `--skip` flag present:**
- Mark `welcomeShown: true` (if not exists)
- Show brief message:
  ```
  Ouroboros welcome skipped.
  Run /ouroboros:welcome --force to re-run onboarding.
  ```
- Exit

---

### Step 1: Welcome Banner

Display:

```
Welcome to Ouroboros!

The serpent that eats itself -- better every loop.

Most AI coding fails at the input, not the output.
Ouroboros fixes this by exposing hidden assumptions
BEFORE any code is written.

Interview -> Seed -> Execute -> Evaluate
    ^                            |
    +---- Evolutionary Loop -----+
```

---

### Step 2: Persona Detection

**Ask using the question tool:**
- Prompt: `What brings you to Ouroboros?`
- Options: `New project idea`, `Tired of rewriting prompts`, `Just exploring`

Give brief personalized response (1-2 sentences) based on choice.

---

### Step 3: MCP Check

```bash
cat ~/.claude/mcp.json 2>/dev/null | grep -q ouroboros && echo "MCP_OK" || echo "MCP_MISSING"
```

**If MCP_MISSING**, ask with platform-native structured choice UI. Keep the prompt and choices equivalent across platforms; on Cursor, provide explicit `options`.
- Prompt: `Ouroboros has a Python backend for advanced features (TUI dashboard, 3-stage evaluation, drift tracking). Set it up now?`
- Options: `Set up now (Recommended)`, `Skip for now`
- **Set up now**: Read and execute `skills/setup/SKILL.md`, then return to Step 4
- **Skip for now**: Continue to Step 4

---

### Step 4: Quick Reference

```
Available Commands:
+---------------------------------------------------+
| Command         | What It Does                     |
|-----------------|----------------------------------|
| ooo interview   | Socratic Q&A -- expose hidden    |
|                 | assumptions in your requirements |
| ooo seed        | Crystallize answers into spec    |
| ooo run         | Execute with visual TUI          |
| ooo evaluate    | 3-stage verification             |
| ooo unstuck     | Lateral thinking when stuck      |
| ooo help        | Full command reference           |
+---------------------------------------------------+
```

---

### Step 5: First Action

**Ask using the question tool:**
- Prompt: `What would you like to do first?`
- Options: `Start a project`, `Try the tutorial`, `Read the docs`

Based on choice:
- **Start a project**: Ask "What do you want to build?" → execute `skills/interview/SKILL.md`
- **Try the tutorial**: Execute `skills/tutorial/SKILL.md`
- **Read the docs**: Execute `skills/help/SKILL.md`

---

### Step 6: GitHub Star (Last Step)

Check `gh` availability first:
```bash
gh auth status &>/dev/null && echo "GH_OK" || echo "GH_MISSING"
```

**If `GH_OK` AND `star_asked` not true:**

**Ask using the question tool:**
- Prompt: `If you're enjoying Ouroboros, would you like to star it on GitHub?`
- Options: `Star on GitHub`, `Maybe later`

- **Star on GitHub**: `gh api -X PUT /user/starred/Q00/ouroboros`
- Both: Save `{"star_asked": true, "welcomeShown": true, "welcomeCompleted": "$(date -Iseconds)", "welcomeVersion": "0.14.0"}` to `~/.ouroboros/prefs.json`

**If `GH_MISSING` or `star_asked` is true:**
Just save `{"welcomeShown": true, "welcomeCompleted": "$(date -Iseconds)", "welcomeVersion": "0.14.0"}`

---

### Completion Message

```
Ouroboros Setup Complete!

MAGIC KEYWORDS (optional shortcuts):
Just include these naturally in your request:

| Keyword | Effect | Example |
|---------|--------|---------|
| interview | Socratic Q&A | "interview me about my app idea" |
| seed | Crystallize spec | "seed the requirements" |
| evaluate | 3-stage check | "evaluate this implementation" |
| stuck | Lateral thinking | "I'm stuck on the auth flow" |

REAL-TIME MONITORING (TUI):
When running ooo run or ooo evolve, open a separate terminal:
  uvx --from ouroboros-ai ouroboros tui monitor
Press 1-4 to switch screens (Dashboard, Execution, Logs, Debug).

READY TO BUILD:
- ooo interview "your project idea"
- ooo tutorial  # Interactive learning
- ooo help      # Full reference
```

---

## Prefs File Structure

`~/.ouroboros/prefs.json`:
```json
{
  "welcomeShown": true,
  "welcomeCompleted": "2025-02-23T15:30:00+09:00",
  "welcomeVersion": "0.14.0",
  "star_asked": true
}
```
