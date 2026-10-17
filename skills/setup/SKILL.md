---
name: setup
description: "Guided onboarding wizard for Ouroboros setup"
disable-model-invocation: true
---

# /ouroboros:setup

Guided Ouroboros setup inside a Claude Code session.

> Other runtimes (Codex, Copilot, OpenCode, and others) configure through the
> CLI instead, for example `ouroboros setup --runtime codex` or
> `ouroboros setup --runtime copilot`. See
> [Getting Started](https://github.com/Q00/ouroboros/blob/main/docs/getting-started.md).

## Usage

```
ooo setup
/ouroboros:setup
/ouroboros:setup --uninstall
```

Claude setup confirms the runtime profile (the Claude Agent SDK on MCP 1.x)
and optionally adds an Ouroboros reference block to the project's CLAUDE.md.
It deliberately leaves `~/.claude/mcp.json` untouched because marketplace
plugin wiring owns that file. `[claude]` and its explicit `[claude-sdk]` alias
use MCP 1.x. The plugin launches `[mcp]` in a separate MCP 2 process with the
dependency-free `[claude-cli]` worker.

## Setup Wizard Flow

Ask questions with the active runtime's `ask_user` capability unless a step
says otherwise.

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

### Step 0.5: Community Support

If `star_asked` is not `true` in `~/.ouroboros/prefs.json`, ask "Ouroboros is
free and open-source. A GitHub star helps other developers discover it. Star
the repo?" with **Star on GitHub** (run `gh api -X PUT /user/starred/Q00/ouroboros`)
and **Skip for now**. After any answer, merge `star_asked: true` into the
prefs file, keeping existing keys:

```bash
ouroboros_python - <<'PY'
import json, os
path = os.path.expanduser('~/.ouroboros/prefs.json')
os.makedirs(os.path.dirname(path), exist_ok=True)
try:
    with open(path, encoding='utf-8') as f:
        prefs = json.load(f)
    if not isinstance(prefs, dict):
        prefs = {}
except Exception:
    prefs = {}
prefs['star_asked'] = True
with open(path, 'w', encoding='utf-8') as f:
    json.dump(prefs, f, indent=2)
    f.write('\n')
PY
```

### Step 1: Environment Detection

```bash
ouroboros_python --version
which uvx 2>/dev/null && uvx --version 2>/dev/null
which claude 2>/dev/null
uv python list 2>/dev/null | grep "cpython-3.1[2-9]"
```

The resolver already rejects Python below 3.12 and falls back to a uv-managed
interpreter; that does not make the `[claude-sdk]` and MCP 2 profiles
import-compatible. Report what was found, then:

| Environment | Action |
|:------------|:-------|
| Python >= 3.12 + Claude CLI | Ready: configure `[claude]` SDK/MCP 1 and skills |
| uvx + Python >= 3.12 | MCP-capable elsewhere: use a supported CLI-backed runtime setup for isolated `ouroboros-ai[mcp]` |
| Python < 3.12 only | Run `uv python install 3.12`, then re-run `ooo setup` |
| No package runner or Ouroboros package | Install uv (below), then re-run `ooo setup` |

Prefer package managers for uv: `pipx install uv`, `pip install --user uv`, or
`brew install uv`; the vendor one-liner
`curl -LsSf https://astral.sh/uv/install.sh | sh` is the last resort.

**IMPORTANT**: Never install `[mcp,claude]`, `[mcp,claude-sdk]`, or `[all,mcp]`
together and never write a direct
`ouroboros` or `python -m ouroboros` MCP fallback. MCP 2 launchers must use an
isolated `uvx --isolated --python '>=3.12' --from 'ouroboros-ai[mcp]' ...` or
`pipx run --spec 'ouroboros-ai[mcp]' ...` process. Only `[mcp,claude-cli]` is
supported because the CLI worker is out of process. Do not write
an Ouroboros entry to `~/.claude/mcp.json`; the plugin owns that registration.

### Step 1.5: Runtime Configuration

Write the Claude runtime selection to `~/.ouroboros/config.yaml`. The welcome
gate and the CLI read it; nothing else in this wizard writes it. This step
never touches `~/.claude/mcp.json`.

Define `ouroboros_cli` in the same shell as each command below (shell state
does not persist between commands):

```bash
ouroboros_cli() {
  if command -v ouroboros >/dev/null 2>&1; then
    command ouroboros "$@"
  else
    uvx --isolated --python '>=3.12' --from 'ouroboros-ai' ouroboros "$@"
  fi
}
ouroboros_cli config show orchestrator
ouroboros_cli config show llm
```

The welcome gate is ready only when `orchestrator.runtime_backend` is `claude`
or `claude_mcp` AND `llm.backend` is `claude`. A missing file or section
counts as not set.

- Both conditions hold: already configured. Skip to Step 2.
- `runtime_backend` is `claude_mcp` but `llm.backend` is not `claude`: run
  `ouroboros_cli setup --runtime claude-cli --non-interactive` (keeps the CLI
  transport).
- `runtime_backend` is another runtime (for example `codex`): ask the user
  whether to switch the default runtime to Claude. If they decline, keep the
  existing configuration and skip to Step 2.
- Otherwise (`runtime_backend` is `claude` with another `llm.backend`, no
  config, no `orchestrator` section), or after the user agrees to switch, run:

```bash
ouroboros_cli setup --runtime claude --non-interactive
```

Both setup commands set `llm.backend: claude`; `--runtime claude` sets
`orchestrator.runtime_backend: claude` and `--runtime claude-cli` sets `claude_mcp`.
If it fails, show the error and continue the wizard; Step 4 reports the
runtime as not configured.

### Step 2: MCP Profile Boundary

Tell the user: the default Claude SDK profile stays on MCP 1.x, and the
plugin-owned MCP server runs MCP 2 separately with the `[claude-cli]` worker.
**Do not create, update, or remove `~/.claude/mcp.json`.** Existing entries may
be user-managed or belong to another compatible runtime. Advanced MCP
workflows require a host-managed isolated `[mcp]` launcher, which the Claude
marketplace plugin or another supported host setup owns.

### Step 3: CLAUDE.md Integration (Optional)

Offer **Integrate / Skip / Preview first** for adding this ~40-line reference
block (pipeline, command table, agent catalog) to the project's CLAUDE.md. On
**Preview first**, show it:

````markdown
<!-- ooo:START -->
<!-- ooo:VERSION:0.57.1 -->
# Ouroboros — Specification-First AI Development

> Before telling AI what to build, define what should be built.
> As Socrates asked 2,500 years ago — "What do you truly know?"
> Ouroboros turns that question into an evolutionary AI workflow engine.

Most AI coding fails at the input, not the output. Ouroboros fixes this by
**exposing hidden assumptions before any code is written**.

1. **Socratic Clarity** — Question until ambiguity ≤ 0.2
2. **Ontological Precision** — Solve the root problem, not symptoms
3. **Evolutionary Loops** — Each evaluation cycle feeds back into better specs

```
Interview → Seed → Execute → Evaluate
    ↑                           ↓
    └─── Evolutionary Loop ─────┘
```

## ooo Commands

Each command loads its agent/MCP on-demand. Details in each skill file.

| Command | Loads |
|---------|-------|
| `ooo` | — |
| `ooo interview` | `ouroboros:socratic-interviewer` |
| `ooo seed` | `ouroboros:seed-architect` |
| `ooo run` | MCP required |
| `ooo evolve` | MCP: `evolve_step` |
| `ooo evaluate` | `ouroboros:evaluator` |
| `ooo unstuck` | `ouroboros:{persona}` |
| `ooo status` | MCP: `session_status` |
| `ooo setup` | — |
| `ooo help` | — |

## Agents

Loaded on-demand — not preloaded.

**Core**: socratic-interviewer, ontologist, seed-architect, evaluator,
advocate, contrarian, judge
**Support**: hacker, simplifier, researcher, architect
<!-- ooo:END -->
````

On **Integrate**: back up CLAUDE.md to CLAUDE.md.bak, append the block, and
confirm.

### Step 4: Verification

Confirm (with the Step 1.5 `config show` commands) that the saved Ouroboros
config selects the default Claude Agent SDK runtime
on MCP 1.x while `~/.claude/mcp.json` was not mutated by this setup. The
dependency-free Claude CLI worker remains a distinct, explicit `[claude-cli]`
selection for the isolated MCP 2 process.

### Step 5: Success Summary

```
Ouroboros Setup Complete!

Mode:                     Claude Agent SDK (MCP 1.x)
MCP Server:               Host-owned (config not mutated)
CLAUDE.md:                <integrated | skipped>

Next: ooo interview "your project idea" | ooo tutorial | ooo help
```

### Step 5.1: Model Choice (Claude Code)

Offer optional model control. Ask "설정이 완료됐어요. 기본 모델 설정으로 바로
시작할 수 있고, 모델은 언제든 나중에 바꿀 수 있어요." (header `모델 설정`):

- **바로 시작하기 (권장)**: 기본 모델 설정으로 바로 작업을 시작해요. Continue to Step 5.5.
- **직접 모델 설정하기**: 단계별로 모델을 바꾸거나 목록에 없는 모델 ID를 입력해
  고정해요. Read and follow `../config/SKILL.md`. In the local Claude Code
  harness it opens the settings UI in the user's browser at a temporary
  `localhost` address. They can reopen it any time with `ooo config`; this
  choice never permanently locks a model.

### Step 5.5: Brownfield Repository Scan

Register existing git repositories and linked worktrees in the Ouroboros DB so
interviews can use them as brownfield context. Use the MCP tool only, not the
CLI or Python scripts.

#### Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros brownfield"` for `ouroboros_brownfield` (both the scan and the later `set_defaults` call). A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. If the tool is genuinely absent, skip this step and continue to Step 6.

1. Scan and register:
   ```
   Tool: ouroboros_brownfield
   Arguments: { "action": "scan" }
   ```
   The walk starts at `scan_root` (default: the user's home directory) and
   goes at most two directory levels deep, skipping dot-prefixed and noisy
   directories such as `node_modules`. Normal repos (`.git` directory) and
   linked worktrees (`.git` file) reached by the walk are registered self-only.
   Git worktree families are not expanded. Repos with no remote or any remote
   name are eligible. Existing defaults are preserved.

2. The scan response `text` already holds a numbered list with default
   markers. **Do NOT make any additional MCP calls to list or query repos.**
   Show every repo in a plain-text two-column grid inside a code block (not a
   markdown table), keeping the `*` default markers exactly as returned:

   ```
   Scan complete. 8 repositories registered.

    1. repo-alpha                   5. repo-epsilon
    2. repo-bravo *                 6. repo-foxtrot
   ```

   If no repos were found, skip to Step 6.

3. **Do NOT use the active runtime's `ask_user` dialog for this selection.** Text emitted between
   tool calls is not guaranteed to render, so a dialog can appear without the
   list, and option previews truncate long lists. Instead, end the turn with the repo grid as the final message, followed by the prompt:

   - Defaults exist: `Current defaults: <names> (numbers <numbers>)`, then
     `Reply with repo numbers to change defaults (e.g. "6, 18, 19"), "keep" to keep the current defaults, or "none" to clear them.`
   - No defaults: `No defaults set. Reply with repo numbers to set defaults (e.g. "6, 18, 19"), or "none" to run interviews in greenfield mode.`

4. On the next turn: numbers (any separator) select those IDs; "keep" skips
   the MCP call and leaves defaults unchanged; "none" clears all; anything else
   gets the prompt again (never guess). Then make ONE call that replaces all
   defaults (`"indices": ""` for none):

   ```
   Tool: ouroboros_brownfield
   Arguments: { "action": "set_defaults", "indices": "<comma-separated IDs>" }
   ```

   Confirm the resulting defaults, or that interviews will run in greenfield
   mode, and mention that `ooo setup` or `ooo brownfield` changes them later.

### Step 6: First Project Nudge

Suggest `ooo interview "Build a CLI tool for [something you need]"` or
`ooo tutorial`.

## Uninstall

On `--uninstall`, ask for confirmation, explaining that it removes only the
Ouroboros block from CLAUDE.md, not plugin files, seeds, or project data. If
confirmed:
1. Leave `~/.claude/mcp.json` untouched
2. Remove `<!-- ooo:START -->` to `<!-- ooo:END -->` block from CLAUDE.md (if exists)
3. Confirm: "Ouroboros plugin configuration removed. To remove plugin files, run: claude plugin uninstall ouroboros"

## Setup Troubleshooting

- **"No compatible Python found"**: plugin mode works without a global Python
  when uv is on PATH. Otherwise install Python 3.12 (`brew install
  python@3.12`, `sudo apt install python3.12`, or python.org/downloads).
- **"uvx: command not found"**: uvx is recommended, not required. For the
  default Claude SDK runtime, `pip install 'ouroboros-ai[claude]'`
  (`[claude-sdk]` is an explicit alias). Use `[claude-cli]` only for the
  isolated MCP 2 server worker. Never combine `[claude]`, `[claude-sdk]`, or
  `[all]` with `[mcp]`.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
