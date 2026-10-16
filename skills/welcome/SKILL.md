---
name: welcome
description: "First-touch experience for new Ouroboros users"
disable-model-invocation: true
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

Ask every question with the active runtime's `ask_user` capability, in the
user's language. The Korean labels below are the canonical wording.

### Python Runtime (Required)

Define this resolver in the same shell before any snippet below. It accepts
only Python 3.12 or newer (`python3`, then `python`, then uv) and preserves
arguments and stdin. Quote every argument passed to `ouroboros_python`.

<!-- ouroboros-python-resolver:start -->
```bash
ouroboros_python() {
  if command -v python3 >/dev/null 2>&1 &&
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 12))') >/dev/null 2>&1
  then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python3 "$@")
    return
  fi
  if command -v python >/dev/null 2>&1 &&
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python -c 'import sys; raise SystemExit(sys.version_info < (3, 12))') >/dev/null 2>&1
  then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command python "$@")
    return
  fi
  if command -v uv >/dev/null 2>&1; then
    (unset PYTHONHOME PYTHONPATH PYTHONPLATLIBDIR PYTHONEXECUTABLE __PYVENV_LAUNCHER__; command uv run --no-project --quiet --python '>=3.12' python "$@")
    return
  fi
  printf '%s\n' 'Ouroboros skills require Python >= 3.12 or uv on PATH.' >&2
  return 127
}
```
<!-- ouroboros-python-resolver:end -->

### Pre-Check: Already Completed?

Run the resolver, this section's blocks, and the migration check below in one
shell call, then print the result with
`printf 'completed=%s setup=%s codex=%s migration=%s\n' "$WELCOME_COMPLETED" "${SETUP_READY:-}" "${CODEX_READY:-}" "${LEGACY_CODEX_MODEL_MIGRATION_REQUIRED:-}"`.

Read the completion marker (legacy `welcomeShown: true` also counts):

```bash
WELCOME_COMPLETED=$(ouroboros_python - <<'PY'
import json, os
try:
    prefs = json.load(open(os.path.expanduser('~/.ouroboros/prefs.json'), encoding='utf-8'))
except Exception:
    prefs = {}
if not isinstance(prefs, dict):
    prefs = {}
print(prefs.get('welcomeCompleted') or ('legacy-welcomeShown' if prefs.get('welcomeShown') else ''))
PY
)
```

Before honoring that completion marker, determine whether setup is ready for
the active runtime.
A previously completed welcome must never hide the setup gate from a user who
chose **나중에** or whose setup was later removed.

Claude Code is ready when `~/.ouroboros/config.yaml` has
`orchestrator.runtime_backend` in `{claude, claude_mcp}` and `llm.backend:
claude`. The marketplace plugin owns its MCP registration, so host-owned
`~/.claude/mcp.json` is never part of readiness:

```bash
if ouroboros_python - "$HOME/.ouroboros/config.yaml" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


def scalar(raw: str) -> str:
    return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")


def load_yaml(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}
    parsed: dict[str, dict[str, str]] = {}
    section = None
    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        key = key.strip("'\"")
        if not line[0].isspace():
            section = key
            parsed[section] = {}
            flow = value.strip().split(" #", 1)[0].strip()
            if flow.startswith("{") and flow.endswith("}"):
                for part in flow[1:-1].split(","):
                    field, separator, field_value = part.partition(":")
                    if separator:
                        parsed[section][field.strip().strip("'\"")] = scalar(field_value)
        elif section is not None:
            parsed[section][key] = scalar(value)
    return parsed


try:
    config = load_yaml(sys.argv[1])
except (OSError, ValueError):
    raise SystemExit(1)
orchestrator, llm = config.get("orchestrator"), config.get("llm")
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") in {"claude", "claude_mcp"}
    and isinstance(llm, dict)
    and llm.get("backend") == "claude"
)
raise SystemExit(0 if ready else 1)
PY
then
  SETUP_READY="true"
fi
```

If `SETUP_READY` is not true, check Codex. It is ready when both backends are
`codex`, the Codex CLI (`OUROBOROS_CODEX_CLI_PATH`, else
`orchestrator.codex_cli_path`, else `codex` on PATH) is runnable, and
`mcp_servers\.ouroboros` in `$CODEX_HOME/config.toml` (any TOML key spelling)
has a runnable `command` or a non-blank `url`:

```bash
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
case "$CODEX_HOME_DIR" in
  "~") CODEX_HOME_DIR="$HOME" ;;
  "~/"*) CODEX_HOME_DIR="$HOME/${CODEX_HOME_DIR#"~/"}" ;;
esac
if ouroboros_python - "$HOME/.ouroboros/config.yaml" "$CODEX_HOME_DIR/config.toml" <<'PY'
import os
import shutil
import sys
import tomllib
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


def scalar(raw: str) -> str:
    return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")


def load_yaml(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}
    parsed: dict[str, dict[str, str]] = {}
    section = None
    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        key = key.strip("'\"")
        if not line[0].isspace():
            section = key
            parsed[section] = {}
            flow = value.strip().split(" #", 1)[0].strip()
            if flow.startswith("{") and flow.endswith("}"):
                for part in flow[1:-1].split(","):
                    field, separator, field_value = part.partition(":")
                    if separator:
                        parsed[section][field.strip().strip("'\"")] = scalar(field_value)
        elif section is not None:
            parsed[section][key] = scalar(value)
    return parsed


def runnable(candidate: str) -> bool:
    value = candidate.strip()
    if not value:
        return False
    if "/" not in value:
        return shutil.which(value) is not None
    path = Path(value).expanduser()
    return path.is_file() and os.access(path, os.X_OK)


def mcp_ready(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command, url = entry.get("command"), entry.get("url")
    if isinstance(command, str) and command.strip():
        return runnable(command)
    return isinstance(url, str) and bool(url.strip())


try:
    config = load_yaml(sys.argv[1])
    codex_config = tomllib.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
orchestrator, llm = config.get("orchestrator"), config.get("llm")
servers = codex_config.get("mcp_servers")
cli_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH")
if not cli_path and isinstance(orchestrator, dict):
    cli_path = orchestrator.get("codex_cli_path")
if not isinstance(cli_path, str) or not cli_path.strip():
    cli_path = "codex"
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") == "codex"
    and isinstance(llm, dict)
    and llm.get("backend") == "codex"
    and runnable(cli_path)
    and mcp_ready(servers.get("ouroboros") if isinstance(servers, dict) else None)
)
raise SystemExit(0 if ready else 1)
PY
then
  CODEX_READY="true"
fi
```

### Legacy Codex Model Migration

Older configurations saved `gpt-5` into all four stage-model fields. That is
now an explicit pin that stops Codex App/CLI model changes from taking effect.
Never rewrite a possible user pin silently. When Codex is ready, detect that
exact legacy shape (or an interrupted migration) once:

```bash
if ouroboros_python - "$HOME/.ouroboros/config.yaml" "$HOME/.ouroboros/prefs.json" <<'PY'
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

stage_values = (
    config.get("clarification", {}).get("default_model"),
    config.get("execution", {}).get("default_model"),
    config.get("evaluation", {}).get("semantic_model"),
    config.get("resilience", {}).get("reflect_model"),
)
legacy_gpt5 = all(value == "gpt-5" for value in stage_values)
partial_automatic_migration = (
    any(value == "gpt-5" for value in stage_values)
    and any(value == "default" for value in stage_values)
    and all(value in {"gpt-5", "default"} for value in stage_values)
)
handled = prefs.get("codexModelMigration") in {"automatic-v1", "kept-gpt-5-v1"}
raise SystemExit(0 if (legacy_gpt5 or partial_automatic_migration) and not handled else 1)
PY
then
  LEGACY_CODEX_MODEL_MIGRATION_REQUIRED="true"
fi
```

If `CODEX_READY` and `LEGACY_CODEX_MODEL_MIGRATION_REQUIRED` are both true, ask
"현재 설정은 모든 단계에서 gpt-5를 고정해 두고 있어요. Codex에서 선택한 모델을 자동으로 사용하도록 바꿀까요?"
(header `모델 설정`) with these options:

- **Codex 선택으로 전환하기 (권장)**: App이나 CLI에서 바꾼 모델을 모든 단계가 자동으로 따라가요.
  Atomically rewrite the four pins:

```bash
ouroboros_python - "$HOME/.ouroboros/config.yaml" <<'PY'
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
original = path.read_text(encoding="utf-8")
replacements = {
    ("clarification", "default_model"): "default",
    ("execution", "default_model"): "default",
    ("evaluation", "semantic_model"): "default",
    ("resilience", "reflect_model"): "default",
}
seen: set[tuple[str, str]] = set()
section: str | None = None
output: list[str] = []
for line in original.splitlines(keepends=True):
    stripped = line.strip()
    indent = len(line) - len(line.lstrip())
    key = stripped.split(":", 1)[0].strip("'\"") if ":" in stripped else ""
    if indent == 0 and ":" in stripped:
        section = key
    target = (section or "", key)
    if indent > 0 and target in replacements:
        newline = "\n" if line.endswith("\n") else ""
        prefix = line[:indent]
        comment = ""
        value_part = line.strip().split(":", 1)[1]
        if " #" in value_part:
            comment = " #" + value_part.split(" #", 1)[1].rstrip("\n")
        output.append(f"{prefix}{key}: {replacements[target]}{comment}{newline}")
        seen.add(target)
    else:
        output.append(line)
missing = set(replacements) - seen
if missing:
    raise SystemExit(f"Cannot migrate Codex model pins; missing keys: {sorted(missing)}")
updated = "".join(output)
fd, tmp_name = tempfile.mkstemp(prefix=".config.yaml.", dir=str(path.parent))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as tmp:
        tmp.write(updated)
        tmp.flush()
        os.fsync(tmp.fileno())
    os.replace(tmp_name, path)
finally:
    try:
        os.unlink(tmp_name)
    except FileNotFoundError:
        pass
PY
```

  `default` sends no model pin to Codex; it is not a model named "default".
  Confirm the rewrite succeeded before recording the decision. An interrupted
  rewrite leaves a partial `gpt-5`/`default` state that the next check offers
  again.
- **gpt-5 고정 유지하기**: 지금처럼 모든 단계를 gpt-5로 계속 실행해요. Do not
  change `config.yaml`.

For either choice, merge exactly one `codexModelMigration` marker into
`~/.ouroboros/prefs.json`, keeping existing keys:

```bash
ouroboros_python - "automatic-v1" <<'PY'
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
was already completed, confirm briefly and exit; do not also ask the generic
welcome question.

### Routing After the Pre-Check

- **Completed, `SETUP_READY` or `CODEX_READY` true, no `--force`**: ask
  "Ouroboros welcome was already completed on $WELCOME_COMPLETED. What would
  you like to do?" with **Skip** (continue to work, recommended; exit) and
  **Re-run welcome** (continue to Step 1).
- **Completed but neither ready**: skip that prompt and go to the Setup Gate.
- **`--skip`**: write the completion prefs (Step 4), print
  `Ouroboros welcome skipped. Run /ouroboros:welcome --force to re-run onboarding.`,
  and exit.

### Setup Gate: First Use

Use the readiness the Pre-Check printed; a global `config.yaml` alone is not
enough unless it names a runtime these gates recognize. To recheck after a
setup run (or when the Pre-Check did not run), use these standalone gates,
which print their verdict. Claude Code:

```bash
if ouroboros_python - "$HOME/.ouroboros/config.yaml" <<'PY'
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


def scalar(raw: str) -> str:
    return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")


def load_yaml(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}
    parsed: dict[str, dict[str, str]] = {}
    section = None
    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        key = key.strip("'\"")
        if not line[0].isspace():
            section = key
            parsed[section] = {}
            flow = value.strip().split(" #", 1)[0].strip()
            if flow.startswith("{") and flow.endswith("}"):
                for part in flow[1:-1].split(","):
                    field, separator, field_value = part.partition(":")
                    if separator:
                        parsed[section][field.strip().strip("'\"")] = scalar(field_value)
        elif section is not None:
            parsed[section][key] = scalar(value)
    return parsed


try:
    config = load_yaml(sys.argv[1])
except (OSError, ValueError):
    raise SystemExit(1)
orchestrator, llm = config.get("orchestrator"), config.get("llm")
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") in {"claude", "claude_mcp"}
    and isinstance(llm, dict)
    and llm.get("backend") == "claude"
)
raise SystemExit(0 if ready else 1)
PY
then
  echo "SETUP_READY"
else
  echo "SETUP_REQUIRED"
fi
```

If that printed `SETUP_READY`, skip the Codex gate and continue to Step 1.
Otherwise check Codex:

```bash
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
case "$CODEX_HOME_DIR" in
  "~") CODEX_HOME_DIR="$HOME" ;;
  "~/"*) CODEX_HOME_DIR="$HOME/${CODEX_HOME_DIR#"~/"}" ;;
esac
if ouroboros_python - "$HOME/.ouroboros/config.yaml" "$CODEX_HOME_DIR/config.toml" <<'PY'
import os
import shutil
import sys
import tomllib
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None


def scalar(raw: str) -> str:
    return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")


def load_yaml(path: str) -> dict:
    source = Path(path).read_text(encoding="utf-8")
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}
    parsed: dict[str, dict[str, str]] = {}
    section = None
    for line in source.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        key = key.strip("'\"")
        if not line[0].isspace():
            section = key
            parsed[section] = {}
            flow = value.strip().split(" #", 1)[0].strip()
            if flow.startswith("{") and flow.endswith("}"):
                for part in flow[1:-1].split(","):
                    field, separator, field_value = part.partition(":")
                    if separator:
                        parsed[section][field.strip().strip("'\"")] = scalar(field_value)
        elif section is not None:
            parsed[section][key] = scalar(value)
    return parsed


def runnable(candidate: str) -> bool:
    value = candidate.strip()
    if not value:
        return False
    if "/" not in value:
        return shutil.which(value) is not None
    path = Path(value).expanduser()
    return path.is_file() and os.access(path, os.X_OK)


def mcp_ready(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    command, url = entry.get("command"), entry.get("url")
    if isinstance(command, str) and command.strip():
        return runnable(command)
    return isinstance(url, str) and bool(url.strip())


try:
    config = load_yaml(sys.argv[1])
    codex_config = tomllib.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
orchestrator, llm = config.get("orchestrator"), config.get("llm")
servers = codex_config.get("mcp_servers")
cli_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH")
if not cli_path and isinstance(orchestrator, dict):
    cli_path = orchestrator.get("codex_cli_path")
if not isinstance(cli_path, str) or not cli_path.strip():
    cli_path = "codex"
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") == "codex"
    and isinstance(llm, dict)
    and llm.get("backend") == "codex"
    and runnable(cli_path)
    and mcp_ready(servers.get("ouroboros") if isinstance(servers, dict) else None)
)
raise SystemExit(0 if ready else 1)
PY
then
  echo "CODEX_READY"
else
  echo "CODEX_SETUP_REQUIRED"
fi
```

If neither is ready (`SETUP_REQUIRED` and `CODEX_SETUP_REQUIRED`, including a
config that names a runtime neither gate recognizes), ask
"Ouroboros를 처음 사용하시네요. 시작하기 전에 실행 환경을 설정할까요?"
(header `Ouroboros 시작하기`):

- **설정하고 시작하기 (권장)**: 한 번만 설정하면 바로 사용할 수 있어요. Run the
  setup for the active host yourself; never ask the user to copy a command the
  host can run. In Codex App or Codex CLI, run `ouroboros setup --runtime codex`
  when the executable is installed. For a Marketplace-plugin-only install run
  `uvx --isolated --python '>=3.12' --from 'ouroboros-ai[mcp]' ouroboros setup --runtime codex`
  instead. In Claude Code, follow `../setup/SKILL.md`.
- **나중에**: 지금은 기본 안내만 보고 나중에 설정할게요. Continue the welcome
  flow without claiming MCP-backed features are ready.

After a successful **Codex** setup, ask "설정이 완료됐어요. 기본적으로 Codex에서
선택한 모델을 사용합니다. 모델은 언제든 나중에 바꿀 수 있어요." (header `준비 완료`):

- **바로 시작하기 (권장)**: 기본 모델로 바로 작업을 시작해요. Continue to Step 1.
- **직접 모델 설정하기**: 단계별로 모델을 바꾸거나 목록에 없는 모델 ID를 입력해
  고정해요. Read and follow `../config/SKILL.md`. On the user's local Codex App
  or CLI it opens the settings UI in their browser at a
  temporary `localhost` address (not an external website). It offers **Use Codex default model** and
  **Enter another model ID…** for a deliberate stage pin. Continue to Step 1
  when the settings session ends.

In Claude Code, `../setup/SKILL.md` asks the equivalent model question itself;
do not ask it again. Do not show this gate again once a runtime is ready; the
settings UI stays available through `ooo config`.

### Step 1: Banner

```
Welcome to Ouroboros!

Most AI coding fails at the input, not the output.
Ouroboros exposes hidden assumptions BEFORE any code is written.

Interview -> Seed -> Execute -> Evaluate
    ^                            |
    +---- Evolutionary Loop -----+
```

Ordinary Claude setup uses the default `[claude]` Agent SDK profile on MCP 1.x.
Never inspect or mutate `~/.claude/mcp.json` as a health check. If Ouroboros
MCP tools are not callable here, say that MCP-backed commands need a supported
host setup, which owns an isolated `[mcp]` launcher; `[claude-sdk]` and
`[mcp]` are never installed together.

### Step 2: Persona

Ask "What brings you to Ouroboros?" with **New project idea** (crystallize a
vague idea into a Seed), **Tired of rewriting prompts** (AI keeps building the
wrong thing), and **Just exploring**. Reply in 1-2 personalized sentences.

### Step 3: First Action

Show the core commands: `ooo interview` (Socratic Q&A that exposes hidden
assumptions), `ooo seed` (freeze answers into a Seed), `ooo run` (execute a
Seed), `ooo evaluate` (verify against the acceptance criteria), `ooo unstuck`
(lateral thinking), `ooo config` (settings), `ooo help` (full reference).

Ask "What would you like to do first?":

- **Start a project**: ask "What do you want to build?", then continue as `ooo interview`.
- **Try the tutorial**: continue as `ooo tutorial`.
- **Read the docs**: continue as `ooo help`.

Run Step 4 before handing off, so completion is recorded even when the next
command takes over the conversation.

### Step 4: GitHub Star and Completion

Run `gh auth status &>/dev/null && echo GH_OK || echo GH_MISSING`. If `GH_OK`
and `star_asked` is not true in `~/.ouroboros/prefs.json`, ask "If you're
enjoying Ouroboros, would you like to star it on GitHub?" with **Star on
GitHub** (run `gh api -X PUT /user/starred/Q00/ouroboros`) and **Maybe later**.

Then merge the completion fields into `~/.ouroboros/prefs.json`, keeping
existing keys. Prefix the command with `STAR_ASKED=true` when the star question
was asked, so it is never repeated:

```bash
ouroboros_python - <<'PY'
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
})
if os.environ.get('STAR_ASKED') == 'true':
    prefs['star_asked'] = True
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
```

Before the handoff, mention that phrases like "interview me", "I'm stuck", or "evaluate this" also
route to the matching command; to watch `ooo run` or `ooo evolve` live, run
`uvx --python '>=3.12' --from 'ouroboros-ai[tui]' ouroboros tui monitor` in a
separate terminal.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
