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

### Python Runtime (Required)

Before running any shell snippet below, define this resolver in the same shell.
It accepts only Python 3.12 or newer, prefers `python3` and then `python`, and
uses uv as the final fallback. Call `ouroboros_python` directly and quote every
argument passed to it; the function preserves arguments and heredoc/stdin input.
Only the probe and child interpreter discard inherited CPython path-selection
overrides; the caller shell keeps its environment unchanged.

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

---

### Pre-Check: Already Completed?

First, check `~/.ouroboros/prefs.json` for `welcomeCompleted`. For upgrades from older releases, also treat legacy `welcomeShown: true` as completed so the welcome prompt does not reappear forever:

```bash
PREFFILE="$HOME/.ouroboros/prefs.json"

if [ -f "$PREFFILE" ]; then
  WELCOME_COMPLETED=$(ouroboros_python - <<'PY'
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
  WELCOME_VERSION=$(ouroboros_python - <<'PY'
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

Before honoring that completion marker, determine whether setup is ready for
the active runtime.
A previously completed welcome must never hide the setup gate from a user who
chose **나중에** or whose setup was later removed.

First accept a completed Claude Code setup:

```bash
if ouroboros_python - "$HOME/.ouroboros/config.yaml" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

config_path = Path(sys.argv[1])

def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
    """Read the top-level mapping scalars this readiness gate owns."""
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}

    parsed: dict[str, dict[str, str]] = {}
    section: str | None = None

    def scalar_value(raw: str) -> str:
        return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")

    def flow_mapping(raw: str) -> dict[str, str]:
        value = raw.strip().split(" #", 1)[0].strip()
        if not (value.startswith("{") and value.endswith("}")):
            return {}
        fields: dict[str, str] = {}
        for part in value[1:-1].split(","):
            key, separator, field_value = part.partition(":")
            if separator:
                fields[key.strip().strip("'\"")] = scalar_value(field_value)
        return fields

    for raw_line in source.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, separator, raw_value = raw_line.strip().partition(":")
        if not separator:
            continue
        if indent == 0:
            section = key.strip("'\"")
            parsed[section] = flow_mapping(raw_value)
        elif section is not None:
            parsed.setdefault(section, {})[key.strip("'\"")] = scalar_value(raw_value)
    return parsed

try:
    config = yaml_mapping(config_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)

orchestrator = config.get("orchestrator") if isinstance(config, dict) else None
llm = config.get("llm") if isinstance(config, dict) else None
# Existing YAML form: runtime_backend: claude. Parsing avoids assuming its order.
# The marketplace plugin owns its MCP capability. Host-owned
# ~/.claude/mcp.json is intentionally not part of SDK setup readiness.
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

If `SETUP_READY` is not true, determine whether the Codex setup is ready:

```bash
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
case "$CODEX_HOME_DIR" in
  "~") CODEX_HOME_DIR="$HOME" ;;
  "~/"*) CODEX_HOME_DIR="$HOME/${CODEX_HOME_DIR#"~/"}" ;;
esac
if ouroboros_python - "$HOME/.ouroboros/config.yaml" "$CODEX_HOME_DIR/config.toml" <<'PY'
from __future__ import annotations

import re
import os
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier hosts
    tomllib = None

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

config_path, codex_config_path = map(Path, sys.argv[1:])

def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
    """Read only the top-level mapping scalars owned by this readiness gate."""
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}

    parsed: dict[str, dict[str, str]] = {}
    section: str | None = None

    def scalar_value(raw: str) -> str:
        return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")

    def flow_mapping(raw: str) -> dict[str, str]:
        value = raw.strip().split(" #", 1)[0].strip()
        if not (value.startswith("{") and value.endswith("}")):
            return {}
        fields: dict[str, str] = {}
        for part in value[1:-1].split(","):
            key, separator, field_value = part.partition(":")
            if separator:
                fields[key.strip().strip("'\"")] = scalar_value(field_value)
        return fields

    for raw_line in source.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, separator, raw_value = raw_line.strip().partition(":")
        if not separator:
            continue
        if indent == 0:
            section = key.strip("'\"")
            parsed[section] = flow_mapping(raw_value)
        elif section is not None:
            parsed[section][key.strip("'\"")] = scalar_value(raw_value)
    return parsed


def toml_mcp_servers(source: str) -> dict[str, dict[str, object]]:
    """Read MCP server table membership when the host lacks ``tomllib``."""
    servers: dict[str, dict[str, object]] = {}
    table: list[str] = []

    def scalar_value(raw: str) -> str:
        value = raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip()
        return value.strip("'\"").strip()

    def inline_value(raw: str, key: str) -> str | None:
        match = re.search(rf"\b{re.escape(key)}\s*=\s*(\"[^\"]*\"|'[^']*'|[^,}}]+)", raw)
        if match is None:
            return None
        return scalar_value(match.group(1))

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
            key, raw_value = line.split("=", 1)
            server = servers.setdefault(key.strip().strip("'\""), {})
            for field in ("command", "url"):
                value = inline_value(raw_value, field)
                if value is not None:
                    server[field] = value
            continue
        if len(table) >= 2 and table[0] == "mcp_servers" and "=" in line:
            key, raw_value = line.split("=", 1)
            key = key.strip().strip("'\"")
            if key in {"command", "url"}:
                servers.setdefault(table[1], {})[key] = scalar_value(raw_value)
    return servers


def executable_candidate(candidate: str) -> bool:
    """Return whether a CLI candidate points to something runnable."""
    value = candidate.strip()
    if not value:
        return False
    if "/" not in value:
        return shutil.which(value) is not None
    path = Path(value).expanduser()
    return path.is_file() and os.access(path, os.X_OK)


def codex_cli_ready(candidate: object) -> bool:
    """Return whether Codex runtime would have an executable launch candidate."""
    if not isinstance(candidate, str):
        return executable_candidate("codex")
    value = candidate.strip()
    if not value:
        return executable_candidate("codex")
    return executable_candidate(value)


def mcp_endpoint_ready(entry: object) -> bool:
    """Return whether the configured MCP endpoint can actually launch."""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if isinstance(command, str) and command.strip():
        return executable_candidate(command)
    url = entry.get("url")
    return isinstance(url, str) and bool(url.strip())

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
ouroboros_mcp = mcp_servers.get("ouroboros") if isinstance(mcp_servers, dict) else None
codex_cli_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH")
if not codex_cli_path and isinstance(orchestrator, dict):
    codex_cli_path = orchestrator.get("codex_cli_path")
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") == "codex"
    and isinstance(llm, dict)
    and llm.get("backend") == "codex"
    and codex_cli_ready(codex_cli_path)
    and mcp_endpoint_ready(ouroboros_mcp)
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

- **Codex 선택으로 전환하기**: atomically rewrite the four legacy model pins
  on the current host:

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

  `default` deliberately sends no model pin to Codex; it does not name a model
  called "default". Confirm that the rewrite succeeded before recording the
  decision. If this step is interrupted before the marker is written, the next
  readiness check recognizes the partial `gpt-5`/`default` state and offers the
  migration again.
- **gpt-5 고정 유지하기**: do not change `config.yaml`.

For either completed choice, merge exactly one marker into
`~/.ouroboros/prefs.json` without deleting existing keys:

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
was already completed, show a short confirmation and exit after recording this
decision; do not make the user answer the generic welcome question too.

**If `ALREADY_COMPLETED` is true, `SETUP_READY` or `CODEX_READY` is true, AND no `--force` flag:**

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

If the welcome was completed but neither `SETUP_READY` nor `CODEX_READY` is
true, bypass this completion prompt and continue to the Setup Gate below.

**If `--skip` flag present:**
- Merge `welcomeShown: true`, `welcomeCompleted: <current timestamp>`, and `welcomeVersion` into `~/.ouroboros/prefs.json` without deleting existing keys:
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
    'welcomeVersion': '0.50.5',
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

Before showing the welcome banner, check whether setup is prepared for the
active runtime on this machine. A global `config.yaml` alone is not enough: it
must name a runtime this gate recognizes.

First accept a completed **Claude Code** setup. The marketplace plugin owns
its MCP capability; host-owned `~/.claude/mcp.json` is intentionally not part
of SDK setup readiness:

```bash
if ouroboros_python - "$HOME/.ouroboros/config.yaml" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

config_path = Path(sys.argv[1])

def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
    """Read only the top-level mapping scalars owned by this readiness gate."""
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}

    parsed: dict[str, dict[str, str]] = {}
    section: str | None = None

    def scalar_value(raw: str) -> str:
        return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")

    def flow_mapping(raw: str) -> dict[str, str]:
        value = raw.strip().split(" #", 1)[0].strip()
        if not (value.startswith("{") and value.endswith("}")):
            return {}
        fields: dict[str, str] = {}
        for part in value[1:-1].split(","):
            key, separator, field_value = part.partition(":")
            if separator:
                fields[key.strip().strip("'\"")] = scalar_value(field_value)
        return fields

    for raw_line in source.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, separator, raw_value = raw_line.strip().partition(":")
        if not separator:
            continue
        if indent == 0:
            section = key.strip("'\"")
            parsed[section] = flow_mapping(raw_value)
        elif section is not None:
            parsed.setdefault(section, {})[key.strip("'\"")] = scalar_value(raw_value)
    return parsed

try:
    config = yaml_mapping(config_path.read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)

orchestrator = config.get("orchestrator") if isinstance(config, dict) else None
llm = config.get("llm") if isinstance(config, dict) else None
# Existing YAML form: runtime_backend: claude. Parsing avoids assuming its order.
# The marketplace plugin owns its MCP capability. Host-owned
# ~/.claude/mcp.json is intentionally not part of SDK setup readiness.
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

If the Claude gate printed `SETUP_READY`, setup is complete: skip the Codex
gate below and continue directly to the welcome banner.

Otherwise check whether **Codex** is prepared:

```bash
CODEX_HOME_DIR="${CODEX_HOME:-$HOME/.codex}"
case "$CODEX_HOME_DIR" in
  "~") CODEX_HOME_DIR="$HOME" ;;
  "~/"*) CODEX_HOME_DIR="$HOME/${CODEX_HOME_DIR#"~/"}" ;;
esac
if ouroboros_python - "$HOME/.ouroboros/config.yaml" "$CODEX_HOME_DIR/config.toml" <<'PY'
from __future__ import annotations

import re
import os
import shutil
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 and earlier hosts
    tomllib = None

try:
    import yaml
except ModuleNotFoundError:
    yaml = None

config_path, codex_config_path = map(Path, sys.argv[1:])

def yaml_mapping(source: str) -> dict[str, dict[str, str]]:
    """Read only the top-level mapping scalars owned by this readiness gate."""
    if yaml is not None:
        loaded = yaml.safe_load(source) or {}
        return loaded if isinstance(loaded, dict) else {}

    parsed: dict[str, dict[str, str]] = {}
    section: str | None = None

    def scalar_value(raw: str) -> str:
        return raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip().strip("'\"")

    def flow_mapping(raw: str) -> dict[str, str]:
        value = raw.strip().split(" #", 1)[0].strip()
        if not (value.startswith("{") and value.endswith("}")):
            return {}
        fields: dict[str, str] = {}
        for part in value[1:-1].split(","):
            key, separator, field_value = part.partition(":")
            if separator:
                fields[key.strip().strip("'\"")] = scalar_value(field_value)
        return fields

    for raw_line in source.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip())
        key, separator, raw_value = raw_line.strip().partition(":")
        if not separator:
            continue
        if indent == 0:
            section = key.strip("'\"")
            parsed[section] = flow_mapping(raw_value)
        elif section is not None:
            parsed.setdefault(section, {})[key.strip("'\"")] = scalar_value(raw_value)
    return parsed


def toml_mcp_servers(source: str) -> dict[str, dict[str, object]]:
    """Read MCP table membership when the host lacks the TOML standard library."""
    servers: dict[str, dict[str, object]] = {}
    table: list[str] = []

    def scalar_value(raw: str) -> str:
        value = raw.strip().split(" #", 1)[0].strip().rstrip(",}").strip()
        return value.strip("'\"").strip()

    def inline_value(raw: str, key: str) -> str | None:
        match = re.search(rf"\b{re.escape(key)}\s*=\s*(\"[^\"]*\"|'[^']*'|[^,}}]+)", raw)
        if match is None:
            return None
        return scalar_value(match.group(1))

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
            key, raw_value = line.split("=", 1)
            server = servers.setdefault(key.strip().strip("'\""), {})
            for field in ("command", "url"):
                value = inline_value(raw_value, field)
                if value is not None:
                    server[field] = value
            continue
        if len(table) >= 2 and table[0] == "mcp_servers" and "=" in line:
            key, raw_value = line.split("=", 1)
            key = key.strip().strip("'\"")
            if key in {"command", "url"}:
                servers.setdefault(table[1], {})[key] = scalar_value(raw_value)
    return servers


def executable_candidate(candidate: str) -> bool:
    """Return whether a CLI candidate points to something runnable."""
    value = candidate.strip()
    if not value:
        return False
    if "/" not in value:
        return shutil.which(value) is not None
    path = Path(value).expanduser()
    return path.is_file() and os.access(path, os.X_OK)


def codex_cli_ready(candidate: object) -> bool:
    """Return whether Codex runtime would have an executable launch candidate."""
    if not isinstance(candidate, str):
        return executable_candidate("codex")
    value = candidate.strip()
    if not value:
        return executable_candidate("codex")
    return executable_candidate(value)


def mcp_endpoint_ready(entry: object) -> bool:
    """Return whether the configured MCP endpoint can actually launch."""
    if not isinstance(entry, dict):
        return False
    command = entry.get("command")
    if isinstance(command, str) and command.strip():
        return executable_candidate(command)
    url = entry.get("url")
    return isinstance(url, str) and bool(url.strip())

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
ouroboros_mcp = mcp_servers.get("ouroboros") if isinstance(mcp_servers, dict) else None
codex_cli_path = os.environ.get("OUROBOROS_CODEX_CLI_PATH")
if not codex_cli_path and isinstance(orchestrator, dict):
    codex_cli_path = orchestrator.get("codex_cli_path")
ready = (
    isinstance(orchestrator, dict)
    and orchestrator.get("runtime_backend") == "codex"
    and isinstance(llm, dict)
    and llm.get("backend") == "codex"
    and codex_cli_ready(codex_cli_path)
    and mcp_endpoint_ready(ouroboros_mcp)
)
raise SystemExit(0 if ready else 1)
PY
then
  echo "CODEX_READY"
else
  echo "CODEX_SETUP_REQUIRED"
fi
```

If neither gate reports ready (the Claude gate printed `SETUP_REQUIRED` and
the Codex gate printed `CODEX_SETUP_REQUIRED`), ask one concise question in
the user's language. This includes a user whose existing Ouroboros
configuration names a runtime neither gate recognizes. For a Korean
conversation, use:

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
  `uvx --isolated --python '>=3.12' --from 'ouroboros-ai[mcp]' ouroboros setup --runtime codex`
  instead.
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

### Step 3: Advanced Runtime Check

Ordinary Claude setup uses the default `[claude]` Agent SDK profile on MCP 1.x.
It intentionally leaves host-owned `~/.claude/mcp.json` untouched; do not
inspect or mutate that file as an onboarding health check. The dependency-free
worker is the explicit `[claude-cli]` profile used by an isolated MCP 2 process.

If the active runtime does not expose Ouroboros MCP tools, **AskUserQuestion**:
```json
{
  "questions": [{
    "question": "Advanced MCP workflows require a host-managed MCP 2 launcher. What would you like to do?",
    "header": "Runtime",
    "options": [
      { "label": "Continue native (Recommended)", "description": "Use Claude-native interview, seed, evaluate, and unstuck workflows" },
      { "label": "Show MCP setup", "description": "See supported Codex, OpenCode, Kiro, Copilot, or Hermes setup commands" }
    ],
    "multiSelect": false
  }]
}
```
- **Continue native**: Continue to Step 4
- **Show MCP setup**: Explain that the Claude marketplace plugin or another
  supported host setup owns the isolated `[mcp]` launcher. Never combine
  `[claude-sdk]` with `[mcp]`. Then continue to Step 4.

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
    'star_asked': True,
    'welcomeShown': True,
    'welcomeCompleted': datetime.now(UTC).isoformat(),
    'welcomeVersion': '0.50.5',
})
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
  ```

**If `GH_MISSING` or `star_asked` is true:**
Merge the welcome completion fields into `~/.ouroboros/prefs.json` without deleting existing keys:
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
    'welcomeVersion': '0.50.5',
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
  uvx --python '>=3.12' --from 'ouroboros-ai[tui]' ouroboros tui monitor
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
  "welcomeVersion": "0.50.5",
  "star_asked": true
}
```

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
