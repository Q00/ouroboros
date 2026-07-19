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

When this skill is invoked, follow this flow:

---

### Pre-Check: Already Completed?

First, check `~/.ouroboros/prefs.json` for `welcomeCompleted`. For upgrades from older releases, also treat legacy `welcomeShown: true` as completed so the welcome prompt does not reappear forever:

```bash
PREFFILE="$HOME/.ouroboros/prefs.json"

if [ -f "$PREFFILE" ]; then
  WELCOME_COMPLETED=$(python3 - <<'PY'
import json, os
path = os.path.expanduser('~/.ouroboros/prefs.json')
try:
    prefs = json.load(open(path, encoding='utf-8'))
except Exception:
    prefs = {}
if not isinstance(prefs, dict):
    prefs = {}
print(prefs.get('welcomeCompleted') or ('legacy-welcomeShown' if prefs.get('welcomeShown') else ''))
PY
)
  WELCOME_VERSION=$(python3 - <<'PY'
import json, os
path = os.path.expanduser('~/.ouroboros/prefs.json')
try:
    prefs = json.load(open(path, encoding='utf-8'))
except Exception:
    prefs = {}
if not isinstance(prefs, dict):
    prefs = {}
print(prefs.get('welcomeVersion') or '')
PY
)

  if [ -n "$WELCOME_COMPLETED" ] && [ "$WELCOME_COMPLETED" != "null" ]; then
    ALREADY_COMPLETED="true"
  fi
fi
```

Before honoring that completion marker, determine whether setup is ready. A
previously completed welcome must never hide the setup gate from a user who
chose **나중에** or whose setup was later removed:

```bash
if test -f "$HOME/.ouroboros/config.yaml" \
  && grep -A8 '^orchestrator:' "$HOME/.ouroboros/config.yaml" | grep -q 'runtime_backend: claude' \
  && grep -A8 '^llm:' "$HOME/.ouroboros/config.yaml" | grep -q 'backend: claude' \
  && test -f "$HOME/.claude/mcp.json" \
  && grep -q '"ouroboros"' "$HOME/.claude/mcp.json"; then
  SETUP_READY="true"
fi
```

**If `ALREADY_COMPLETED` is true, `SETUP_READY` is true, AND no `--force` flag:**

Use **AskUserQuestion**:
```json
{
  "questions": [{
    "question": "Ouroboros welcome was already completed on $WELCOME_COMPLETED. What would you like to do?",
    "header": "Welcome",
    "options": [
      { "label": "Skip", "description": "Continue to work (recommended)" },
      { "label": "Re-run welcome", "description": "Go through the interactive onboarding again" }
    ],
    "multiSelect": false
  }]
}
```
- **Skip**: Mark as complete and exit
- **Re-run welcome**: Continue to Step 1 below

If the welcome was completed but `SETUP_READY` is not true, bypass this
completion prompt and continue to the Setup Gate below.

**If `--skip` flag present:**
- Merge `welcomeShown: true`, `welcomeCompleted: <current timestamp>`, and `welcomeVersion` into `~/.ouroboros/prefs.json` without deleting existing keys:
  ```bash
python3 - <<'PY'
import json, os
from datetime import UTC, datetime
path = os.path.expanduser('~/.ouroboros/prefs.json')
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, encoding='utf-8') as f:
        prefs = json.load(f)
    if not isinstance(prefs, dict):
        prefs = {}
except Exception:
    prefs = {}
prefs.update({
    'welcomeShown': True,
    'welcomeCompleted': datetime.now(UTC).isoformat(),
    'welcomeVersion': '0.36.0',
})
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
  ```
- Show brief message:
  ```
  Ouroboros welcome skipped.
  Run /ouroboros:welcome --force to re-run onboarding.
  ```
- Exit

---

### Setup Gate: First Use

Before showing the welcome banner, check whether Ouroboros has been prepared
on this machine:

```bash
if test -f "$HOME/.ouroboros/config.yaml" \
  && grep -A8 '^orchestrator:' "$HOME/.ouroboros/config.yaml" | grep -q 'runtime_backend: claude' \
  && grep -A8 '^llm:' "$HOME/.ouroboros/config.yaml" | grep -q 'backend: claude' \
  && test -f "$HOME/.claude/mcp.json" \
  && grep -q '"ouroboros"' "$HOME/.claude/mcp.json"; then
  echo "SETUP_READY"
else
  echo "SETUP_REQUIRED"
fi
```

If setup is required, ask one concise question in the user's language. For a
Korean conversation, use:

```json
{
  "questions": [{
    "question": "Ouroboros를 처음 사용하시네요. 시작하기 전에 실행 환경을 설정할까요?",
    "header": "Ouroboros 시작하기",
    "options": [
      {
        "label": "설정하고 시작하기 (권장)",
        "description": "한 번만 설정하면 바로 사용할 수 있어요"
      },
      {
        "label": "나중에",
        "description": "지금은 기본 안내만 보고 나중에 설정할게요"
      }
    ],
    "multiSelect": false
  }]
}
```

- **설정하고 시작하기**: Follow `../setup/SKILL.md`. Do not ask the user
  to copy a command when the current host can run it.
- **나중에**: Continue with the welcome flow, but do not claim that MCP-only
  execution features are ready.

After successful setup, `../setup/SKILL.md` presents the model choice.
Do not repeat it here; continue to Step 1 after the setup skill returns.

Do not show this gate again once the Claude runtime, LLM backend, and MCP entry
are ready. The normal settings UI remains available later through `ooo config`,
so a model choice made now is never permanent.

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

**AskUserQuestion**:
```json
{
  "questions": [{
    "question": "What brings you to Ouroboros?",
    "header": "Welcome",
    "options": [
      {
        "label": "New project idea",
        "description": "I have a vague idea and want to crystallize it into a clear spec"
      },
      {
        "label": "Tired of rewriting prompts",
        "description": "AI keeps building the wrong thing because my requirements are unclear"
      },
      {
        "label": "Just exploring",
        "description": "Heard about Ouroboros and want to see what it does"
      }
    ],
    "multiSelect": false
  }]
}
```

Give brief personalized response (1-2 sentences) based on choice.

---

### Step 3: Quick Reference

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

### Step 4: First Action

**AskUserQuestion**:
```json
{
  "questions": [{
    "question": "What would you like to do first?",
    "header": "Get started",
    "options": [
      { "label": "Start a project", "description": "Run a Socratic interview on your idea right now" },
      { "label": "Try the tutorial", "description": "Interactive hands-on learning with a sample project" },
      { "label": "Read the docs", "description": "Full command reference and architecture overview" }
    ],
    "multiSelect": false
  }]
}
```

Based on choice:
- **Start a project**: Ask "What do you want to build?" → execute `../interview/SKILL.md`
- **Try the tutorial**: Execute `../tutorial/SKILL.md`
- **Read the docs**: Execute `../help/SKILL.md`

---

### Step 5: GitHub Star (Last Step)

Check `gh` availability first:
```bash
gh auth status &>/dev/null && echo "GH_OK" || echo "GH_MISSING"
```

**If `GH_OK` AND `star_asked` not true:**

**AskUserQuestion**:
```json
{
  "questions": [{
    "question": "If you're enjoying Ouroboros, would you like to star it on GitHub?",
    "header": "Community",
    "options": [
      { "label": "Star on GitHub", "description": "Takes 1 second -- helps the project grow" },
      { "label": "Maybe later", "description": "Skip for now" }
    ],
    "multiSelect": false
  }]
}
```

- **Star on GitHub**: `gh api -X PUT /user/starred/Q00/ouroboros`
- Both choices: merge the welcome completion fields into `~/.ouroboros/prefs.json` without deleting existing keys. Set `star_asked: true` after either star prompt choice so the star prompt is not repeated:
  ```bash
python3 - <<'PY'
import json, os
from datetime import UTC, datetime
path = os.path.expanduser('~/.ouroboros/prefs.json')
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, encoding='utf-8') as f:
        prefs = json.load(f)
    if not isinstance(prefs, dict):
        prefs = {}
except Exception:
    prefs = {}
prefs.update({
    'star_asked': True,
    'welcomeShown': True,
    'welcomeCompleted': datetime.now(UTC).isoformat(),
    'welcomeVersion': '0.36.0',
})
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
  ```

**If `GH_MISSING` or `star_asked` is true:**
Merge the welcome completion fields into `~/.ouroboros/prefs.json` without deleting existing keys:
  ```bash
python3 - <<'PY'
import json, os
from datetime import UTC, datetime
path = os.path.expanduser('~/.ouroboros/prefs.json')
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, encoding='utf-8') as f:
        prefs = json.load(f)
    if not isinstance(prefs, dict):
        prefs = {}
except Exception:
    prefs = {}
prefs.update({
    'welcomeShown': True,
    'welcomeCompleted': datetime.now(UTC).isoformat(),
    'welcomeVersion': '0.36.0',
})
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
  ```

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
  uvx --from 'ouroboros-ai[tui]' ouroboros tui monitor
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
  "welcomeVersion": "0.36.0",
  "star_asked": true
}
```
