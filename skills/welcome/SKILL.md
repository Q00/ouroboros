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

Before honoring that completion marker, determine whether the Codex setup is
ready. A previously completed welcome must never hide the setup gate from a
user who chose **나중에** or whose setup was later removed:

```bash
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
if python3 - "$HOME/.ouroboros/config.yaml" "$CODEX_HOME_DIR/config.toml" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier hosts
    tomllib = None

config_path, codex_config_path = map(Path, sys.argv[1:])

def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
    """Read the top-level YAML mappings this readiness contract owns.

    The host Python is not guaranteed to include PyYAML. This intentionally
    handles mapping scalars only, but honors indentation and section boundaries
    instead of relying on nearby lines or key order.
    """
    parsed: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw_line in source.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, separator, raw_value = raw_line.strip().partition(":")
        if not separator:
            continue
        value = raw_value.strip().split(" #", 1)[0].strip().strip("'\"")
        if indent == 0:
            section = key.strip("'\"")
            parsed.setdefault(section, {})
        elif section is not None:
            parsed[section][key.strip("'\"")] = value
    return parsed


def toml_mcp_servers(source: str) -> dict[str, dict[str, object]]:
    """Read MCP server table membership when the host lacks ``tomllib``."""
    servers: dict[str, dict[str, object]] = {}
    table: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            table = [part.strip().strip("'\"") for part in line[1:-1].split(".")]
            if len(table) >= 2 and table[0] == "mcp_servers":
                servers.setdefault(table[1], {})
            continue
        if table == ["mcp_servers"] and "=" in line:
            key = line.split("=", 1)[0].strip().strip("'\"")
            servers.setdefault(key, {})
    return servers

try:
    config = yaml_mapping(config_path.read_text(encoding="utf-8"))
    codex_source = codex_config_path.read_text(encoding="utf-8")
    codex_config = tomllib.loads(codex_source) if tomllib is not None else {
        "mcp_servers": toml_mcp_servers(codex_source)
    }
except (OSError, ValueError):
    raise SystemExit(1)

orchestrator = config.get("orchestrator") if isinstance(config, dict) else None
llm = config.get("llm") if isinstance(config, dict) else None
# Equivalent to [mcp_servers\.ouroboros], including quoted TOML key forms.
mcp_servers = codex_config.get("mcp_servers") if isinstance(codex_config, dict) else None
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") == "codex"
    and isinstance(llm, dict)
    and llm.get("backend") == "codex"
    and isinstance(mcp_servers, dict)
    and isinstance(mcp_servers.get("ouroboros"), dict)
)
raise SystemExit(0 if ready else 1)
PY
then
  CODEX_READY="true"
fi
```

### Legacy Codex Model Migration

Some older Ouroboros configurations saved `gpt-5` into all four stage-model
fields. That was a historical default, but it is now an explicit pin and would
stop Codex App/CLI model changes from taking effect. Do not silently rewrite a
possible user pin. Instead, when Codex is ready, detect that exact legacy
shape once before honoring the welcome-completed marker:

```bash
if python3 - "$HOME/.ouroboros/config.yaml" "$HOME/.ouroboros/prefs.json" <<'PY'
from __future__ import annotations

import json
import sys
from pathlib import Path

config_path, prefs_path = map(Path, sys.argv[1:])

def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
    parsed: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw_line in source.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, separator, raw_value = raw_line.strip().partition(":")
        if not separator:
            continue
        value = raw_value.strip().split(" #", 1)[0].strip().strip("'\"")
        if indent == 0:
            section = key.strip("'\"")
            parsed.setdefault(section, {})
        elif section is not None:
            parsed[section][key.strip("'\"")] = value
    return parsed

try:
    config = yaml_mapping(config_path.read_text(encoding="utf-8"))
except OSError:
    raise SystemExit(1)
try:
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    prefs = {}
if not isinstance(prefs, dict):
    prefs = {}

legacy_gpt5 = (
    config.get("clarification", {}).get("default_model") == "gpt-5"
    and config.get("execution", {}).get("default_model") == "gpt-5"
    and config.get("evaluation", {}).get("semantic_model") == "gpt-5"
    and config.get("resilience", {}).get("reflect_model") == "gpt-5"
)
handled = prefs.get("codexModelMigration") in {"automatic-v1", "kept-gpt-5-v1"}
raise SystemExit(0 if legacy_gpt5 and not handled else 1)
PY
then
  LEGACY_CODEX_MODEL_MIGRATION_REQUIRED="true"
fi
```

**If `CODEX_READY` is true and `LEGACY_CODEX_MODEL_MIGRATION_REQUIRED` is true:**

Use **AskUserQuestion**:

```json
{
  "questions": [{
    "question": "현재 설정은 모든 단계에서 gpt-5를 고정해 두고 있어요. Codex에서 선택한 모델을 자동으로 사용하도록 바꿀까요?",
    "header": "모델 설정",
    "options": [
      {
        "label": "Codex 선택으로 전환하기 (권장)",
        "description": "App이나 CLI에서 바꾼 모델을 모든 단계가 자동으로 따라가요"
      },
      {
        "label": "gpt-5 고정 유지하기",
        "description": "지금처럼 모든 단계를 gpt-5로 계속 실행해요"
      }
    ],
    "multiSelect": false
  }]
}
```

- **Codex 선택으로 전환하기**: run these four commands on the current host
  (use `uvx --from 'ouroboros-ai[tui]' ouroboros` in a Marketplace-plugin-only
  install):

  ```bash
  ouroboros config set clarification.default_model default
  ouroboros config set execution.default_model default
  ouroboros config set evaluation.semantic_model default
  ouroboros config set resilience.reflect_model default
  ```

  `default` deliberately sends no model pin to Codex; it does not name a model
  called "default". Confirm that every command succeeded before recording the
  decision.
- **gpt-5 고정 유지하기**: do not change `config.yaml`.

For either completed choice, merge exactly one marker into
`~/.ouroboros/prefs.json` without deleting existing keys:

```bash
python3 - "automatic-v1" <<'PY'
import json, os, sys
path = os.path.expanduser('~/.ouroboros/prefs.json')
try:
    prefs = json.load(open(path, encoding='utf-8'))
except Exception:
    prefs = {}
if not isinstance(prefs, dict):
    prefs = {}
prefs['codexModelMigration'] = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
```

Pass `kept-gpt-5-v1` instead of `automatic-v1` for the keep choice. If welcome
was already completed, show a short confirmation and exit after recording this
decision; do not make the user answer the generic welcome question too.

**If `ALREADY_COMPLETED` is true, `CODEX_READY` is true, AND no `--force` flag:**

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

If the welcome was completed but `CODEX_READY` is not true, bypass this
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

Before showing the welcome banner, check whether **Codex** is prepared on this
machine. A global `config.yaml` alone is not enough: it may belong to a Claude
or another runtime.

```bash
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
if python3 - "$HOME/.ouroboros/config.yaml" "$CODEX_HOME_DIR/config.toml" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier hosts
    tomllib = None

config_path, codex_config_path = map(Path, sys.argv[1:])

def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
    """Read only the top-level mapping scalars owned by this readiness gate."""
    parsed: dict[str, dict[str, str]] = {}
    section: str | None = None
    for raw_line in source.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, separator, raw_value = raw_line.strip().partition(":")
        if not separator:
            continue
        value = raw_value.strip().split(" #", 1)[0].strip().strip("'\"")
        if indent == 0:
            section = key.strip("'\"")
            parsed.setdefault(section, {})
        elif section is not None:
            parsed[section][key.strip("'\"")] = value
    return parsed


def toml_mcp_servers(source: str) -> dict[str, dict[str, object]]:
    """Read MCP table membership when the host lacks the TOML standard library."""
    servers: dict[str, dict[str, object]] = {}
    table: list[str] = []
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            table = [part.strip().strip("'\"") for part in line[1:-1].split(".")]
            if len(table) >= 2 and table[0] == "mcp_servers":
                servers.setdefault(table[1], {})
            continue
        if table == ["mcp_servers"] and "=" in line:
            key = line.split("=", 1)[0].strip().strip("'\"")
            servers.setdefault(key, {})
    return servers

try:
    config = yaml_mapping(config_path.read_text(encoding="utf-8"))
    codex_source = codex_config_path.read_text(encoding="utf-8")
    codex_config = tomllib.loads(codex_source) if tomllib is not None else {
        "mcp_servers": toml_mcp_servers(codex_source)
    }
except (OSError, ValueError):
    raise SystemExit(1)

orchestrator = config.get("orchestrator") if isinstance(config, dict) else None
llm = config.get("llm") if isinstance(config, dict) else None
# This is equivalent to checking [mcp_servers\.ouroboros], but TOML parsing
# also accepts a quoted "ouroboros" key and does not depend on table ordering.
mcp_servers = codex_config.get("mcp_servers") if isinstance(codex_config, dict) else None
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") == "codex"
    and isinstance(llm, dict)
    and llm.get("backend") == "codex"
    and isinstance(mcp_servers, dict)
    and isinstance(mcp_servers.get("ouroboros"), dict)
)
raise SystemExit(0 if ready else 1)
PY
then
  echo "CODEX_READY"
else
  echo "CODEX_SETUP_REQUIRED"
fi
```

If Codex setup is required, ask one concise question in the user's language.
This includes a user who has an existing Ouroboros configuration for another
runtime. For a Korean conversation, use:

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

- **설정하고 시작하기**: Run the setup command for the active host. In Codex
  App or Codex CLI, use `ouroboros setup --runtime codex` when the executable
  is installed. For a Marketplace-plugin-only install, use
  `uvx --from 'ouroboros-ai[mcp]' ouroboros setup --runtime codex` instead.
  In Claude Code, follow `../setup/SKILL.md`. Do not ask the user to copy a
  command when the current host can run it.
- **나중에**: Continue with the welcome flow, but do not claim that MCP-only
  execution features are ready.

After successful **Codex** setup, immediately ask:

```json
{
  "questions": [{
    "question": "설정이 완료됐어요. 기본적으로 Codex에서 선택한 모델을 사용합니다. 모델은 언제든 나중에 바꿀 수 있어요.",
    "header": "준비 완료",
    "options": [
      {
        "label": "바로 시작하기 (권장)",
        "description": "기본 모델로 바로 작업을 시작해요"
      },
      {
        "label": "직접 모델 설정하기",
        "description": "단계별로 모델을 바꾸거나 목록에 없는 모델 ID를 입력해 고정해요"
      }
    ],
    "multiSelect": false
  }]
}
```

- **바로 시작하기**: Continue to Step 1.
- **직접 모델 설정하기**: Read and follow `../config/SKILL.md`. On the
  user's local Codex App or Codex CLI this opens the settings UI in their
  browser at a temporary `localhost` address; it is not an external website.
  The UI offers **Use Codex default model** for the current Codex selection and
  **Enter another model ID…** for a deliberate stage pin. After the settings
  session ends, continue to Step 1.

For **Claude Code**, `../setup/SKILL.md` presents the equivalent model
choice during its own completion flow. Do not show this Codex-specific question
a second time; continue to Step 1 after the Claude setup skill returns.

Do not show this gate again once Codex is ready. The normal settings UI remains
available later through `ooo config`, so a model choice made now is never
permanent.

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
| ooo config      | Settings GUI: agents & models    |
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

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
