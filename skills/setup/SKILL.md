---
name: setup
description: "Guided onboarding wizard for Ouroboros setup"
---

# /ouroboros:setup

Guided onboarding wizard that converts users into power users.

> **Standalone users** (Codex, pip install): Use `ouroboros setup --runtime codex` in your terminal instead.
> This skill runs inside a Claude Code session. For other runtime backends, the CLI `ouroboros setup` command handles configuration.
> For full install and onboarding instructions, see [Getting Started](docs/getting-started.md).

> **GitHub Copilot CLI users**: Run `ouroboros setup --runtime copilot` (after `pipx install 'ouroboros-ai[mcp]'` or `uv tool install 'ouroboros-ai[mcp]'`). Setup will:
>
> 1. Live-discover available models from the GitHub Copilot models API (uses `gh auth token`) and let you pick a default. A bundled fallback list is used when offline.
> 2. Write `orchestrator.runtime_backend = copilot` and `llm.backend = copilot` plus your chosen default into `~/.ouroboros/config.yaml`.
> 3. Register the MCP server in `~/.copilot/mcp-config.json` so the next `copilot` session can call `ooo ...` skills.
>
> Hyphen Anthropic IDs that the Ouroboros defaults use (for example `claude-opus-4-6`) are auto-mapped at runtime to the dotted form Copilot CLI expects (`claude-opus-4.6`), so existing config files keep working when you switch backends.

## Usage

```
ooo setup
/ouroboros:setup
/ouroboros:setup --uninstall
```

> **Note**: Setup does two things:
> 1. **MCP server registration** (`~/.claude/mcp.json`) — one-time, global across all projects
> 2. **CLAUDE.md integration** (optional) — per-project, adds an Ouroboros command reference block
>
> After the first run, you only need to re-run setup in new projects if you want the CLAUDE.md integration.

---

## Setup Wizard Flow

When the user invokes this skill, guide them through an enhanced 6-step wizard with progressive disclosure and celebration checkpoints.

---

### Step 0: Welcome & Motivation (The Hook)

Start with energy and clear value:

```
Welcome to Ouroboros Setup!

Let's unlock your full AI development potential.

What you'll get:
- Visual TUI dashboard for real-time progress tracking
- 3-stage evaluation pipeline for quality assurance
- Drift detection to keep projects on track
- Cost optimization (85% savings on average)

Setup takes ~2 minutes. Let's go!
```

---

### Step 0.5: Community Support

Before we begin, check `~/.ouroboros/prefs.json` for `star_asked`. If not `true`, use **AskUserQuestion**:

```json
{
  "questions": [{
    "question": "Ouroboros is free and open-source. A GitHub star helps other developers discover it. Star the repo?",
    "header": "Community",
    "options": [
      {
        "label": "Star on GitHub",
        "description": "Takes 1 second — helps the project grow"
      },
      {
        "label": "Skip for now",
        "description": "Continue with setup"
      }
    ],
    "multiSelect": false
  }]
}
```

- **Star on GitHub**: Run `gh api -X PUT /user/starred/Q00/ouroboros`, then merge `{"star_asked": true}` into `~/.ouroboros/prefs.json`
- **Skip for now**: Merge `{"star_asked": true}` into `~/.ouroboros/prefs.json`
- **Other**: Merge `{"star_asked": true}` into `~/.ouroboros/prefs.json`

Create `~/.ouroboros/` directory if it doesn't exist. Preserve any existing keys such as `welcomeShown`, `welcomeCompleted`, and `welcomeVersion` when updating `star_asked`:

```bash
python3 - <<'PY'
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

If `star_asked` is already `true`, skip this step silently.

---

### Step 1: Environment Detection

Check the user's environment with clear feedback:

```bash
python3 --version
which uvx 2>/dev/null && uvx --version 2>/dev/null
which claude 2>/dev/null
```

**IMPORTANT: If system Python is < 3.12 but uvx is available, also check uv-managed Python:**

```bash
uv python list 2>/dev/null | grep "cpython-3.1[2-9]"
```

If `uv python list` shows Python >= 3.12 available, this counts as **Full Mode** because `uvx ouroboros-ai mcp serve` automatically uses uv-managed Python >= 3.12 (not system Python).

**Report results with personality:**

```
Environment Detected:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

System Python 3.11         [!] Below 3.12
uv Python 3.12+            [✓] Available (uvx will use this)
uvx package runner         [✓] Available
Runtime backend            [✓] Detected

→ Full Mode Available (via uvx + uv-managed Python >= 3.12)
```

**Decision Matrix:**

| Environment | Mode | Action |
|:------------|:-----|:-------|
| uvx + Python >= 3.12 | **Ready** | Proceed to MCP registration (uvx mode — extras always included) |
| No uvx + `ouroboros` binary in PATH | **Check deps** | Verify `[mcp,claude]` extras, then proceed (binary mode) |
| No uvx + Python >= 3.12 + `python3 -m ouroboros` works | **Check deps** | Verify `[mcp,claude]` extras, then proceed (pip mode) |
| uvx + Python < 3.12 only | **Install needed** | Run `uv python install 3.12` then proceed |
| No uvx + no ouroboros binary + no pip package | **Install needed** | Install uv first, then proceed |

**For binary/pip modes — verify `[mcp,claude]` extras are installed:**

Check method depends on how ouroboros was installed:
```bash
# Detect install method
pipx list 2>/dev/null | grep -q ouroboros && echo "PIPX" || echo "NOT_PIPX"
```

- **pipx users** (binary mode, installed via pipx):
  ```bash
  pipx runpip ouroboros-ai show claude-agent-sdk 2>/dev/null && pipx runpip ouroboros-ai show mcp 2>/dev/null && echo "DEPS_OK" || echo "DEPS_MISSING"
  ```
  If `DEPS_MISSING`: `pipx install --force 'ouroboros-ai[mcp,claude]'`

- **pip users** (pip mode):
  ```bash
  python3 -c "import claude_agent_sdk, mcp" 2>/dev/null && echo "DEPS_OK" || echo "DEPS_MISSING"
  ```
  If `DEPS_MISSING`: `python3 -m pip install 'ouroboros-ai[mcp,claude]'`

If deps are missing and the user doesn't want to fix manually, recommend uv. Prefer
package-manager paths over the vendor pipe-to-shell when the user's environment supports
them (pipx > pip > brew > vendor one-liner):
```
Or install uv (recommended — handles deps automatically). Any one of:
  pipx install uv
  pip install --user uv
  brew install uv          # macOS / Linuxbrew
  curl -LsSf https://astral.sh/uv/install.sh | sh   # vendor one-liner (last resort)
Then re-run: ooo setup
```

**IMPORTANT**: The MCP server requires one of: (1) uvx, (2) ouroboros binary in PATH, or (3) ouroboros pip-installed. For options 2 and 3, the `[mcp,claude]` extras must also be installed. If none are available, guide the user to install uv — do NOT write a non-working fallback to mcp.json.

**If prerequisites are missing, show:**
```
Ouroboros requires uvx (recommended) or the ouroboros package installed.

Quick install (< 1 minute) — install uv via any of:
  pipx install uv
  pip install --user uv
  brew install uv          # macOS / Linuxbrew
  curl -LsSf https://astral.sh/uv/install.sh | sh   # vendor one-liner (last resort)
Then:
  uv python install 3.12

Then re-run: ooo setup
```

**Celebration Checkpoint 1:**
```
Great news! You're ready for the full Ouroboros experience.
```

---

### Step 2: MCP Server Registration

Check if `~/.claude/mcp.json` exists:

```bash
ls -la ~/.claude/mcp.json 2>/dev/null && echo "EXISTS" || echo "NOT_FOUND"
```

**Show progress:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Registering MCP Server...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Connecting Ouroboros Python core to your runtime backend.
This enables:

  Visual TUI Dashboard    [Watch execution in real-time]
  3-Stage Evaluation     [Mechanical → Semantic → Consensus]
  Drift Detection        [Alert when projects go off-track]
  Session Replay         [Debug any execution from events]
```

**Automatically create or update `~/.claude/mcp.json`** (user-level, works across all projects).

Choose the MCP command based on how ouroboros is installed (check in order):
1. If `which uvx` succeeds: `{"command": "uvx", "args": ["--from", "ouroboros-ai[mcp,claude]", "ouroboros", "mcp", "serve"]}`
2. If `which ouroboros` succeeds: `{"command": "ouroboros", "args": ["mcp", "serve"]}`
3. If `python3 -c "import ouroboros"` succeeds: `{"command": "python3", "args": ["-m", "ouroboros", "mcp", "serve"]}`
4. If none of the above → **do NOT write to mcp.json**. Instead show the prerequisites message from Step 1 and stop.

If `~/.claude/mcp.json` already exists, read it, **always overwrite the `ouroboros` key** with the entry above (to fix stale args from older versions), and preserve all other server entries.

**Celebration Checkpoint 2:**
```
MCP Server Registered! You can now:
- Run ooo run for visual TUI execution
- Run ooo evaluate for 3-stage verification
- Run ooo status for drift tracking
```

---

### Step 3: CLAUDE.md Integration (Optional)

Ask with clear value proposition:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  CLAUDE.md Integration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Add Ouroboros quick-reference to your CLAUDE.md?

This gives you instant command reminders without leaving
your project context.

What gets added (~40 lines):
- Philosophy and pipeline overview
- Command routing table with lazy-loaded agents
- Agent catalog summary

A backup will be created: CLAUDE.md.bak

[Integrate / Skip / Preview first]
```

**If "Preview first", show:**
````markdown
<!-- ooo:START -->
<!-- ooo:VERSION:0.50.4 -->
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
wonder, reflect, advocate, contrarian, judge
**Support**: hacker, simplifier, researcher, architect
<!-- ooo:END -->
````

**If Integrate:**
1. Backup existing CLAUDE.md to CLAUDE.md.bak
2. Append the block above
3. Confirm successful integration

**Celebration Checkpoint 3:**
```
CLAUDE.md updated! You now have instant Ouroboros reference
available in every project.
```

---

### Step 4: Quick Verification

Run verification with visual feedback:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Verifying Setup...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Check skills are loadable:
```bash
ls skills/ | wc -l  # Should show 12+ skills
```

Check agents are available:
```bash
ls src/ouroboros/agents/*.md | wc -l  # Should show 20+ bundled agents
```

Check MCP registration (if enabled):
```bash
cat ~/.claude/mcp.json | grep -q ouroboros && echo "MCP: ✓" || echo "MCP: ✗"
```

---

### Step 5: Success Summary

Display with celebration:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Ouroboros Setup Complete!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Mode:                     Full Mode (Python >= 3.12 + MCP)
Skills Registered:        15 workflow skills
Agents Available:         9 specialized agents
MCP Server:               ✓ Registered
CLAUDE.md:                ✓ Integrated

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  You're Ready to Go!
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Start your first project:
  ooo interview "your project idea"

Learn what's possible:
  ooo help

Try the interactive tutorial:
  ooo tutorial

Join the community:
  Star us on GitHub! github.com/Q00/ouroboros
```

---

### Step 5.1: Model Choice (Claude Code)

Before continuing to repository setup, give Claude Code users the same
optional control over models without making it a requirement. Ask in the
user's language; for Korean, use:

```json
{
  "questions": [{
    "question": "설정이 완료됐어요. 기본 모델 설정으로 바로 시작할 수 있고, 모델은 언제든 나중에 바꿀 수 있어요.",
    "header": "모델 설정",
    "options": [
      {
        "label": "바로 시작하기 (권장)",
        "description": "기본 모델 설정으로 바로 작업을 시작해요"
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

- **바로 시작하기**: Continue to Step 5.5.
- **직접 모델 설정하기**: Read and follow `../config/SKILL.md`. In the
  local Claude Code harness, it opens the same settings UI in the user's
  browser at a temporary `localhost` address. They can reopen it any time with
  `ooo config`; this choice never permanently locks a model.

---

### Step 5.5: Brownfield Repository Scan

Scan a root directory for existing git repositories and linked worktrees, then register them in the Ouroboros DB. This enables interviews to use brownfield context for existing projects.

**Show scanning indicator:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Scanning for Existing Projects...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Looking for git repositories and worktrees up to two directories below the scan root.
Only repositories and worktrees reached directly by this depth-bounded walk are registered.
Local repos and repos with any remote name are eligible.
This may take a moment...
```

**Implementation — use MCP tools only, do NOT use CLI or Python scripts:**

**CRITICAL — deferred-schema guard (prevents "Invalid tool parameters"):**
`setup` can call `ouroboros_brownfield` before and after a user-selection turn.
A deferred schema loaded before scan is NOT guaranteed to remain loaded for the
later `set_defaults` call. Immediately before EVERY `ouroboros_brownfield` call
in this section, re-run `tool discovery query: "+ouroboros brownfield"` (idempotent —
a no-op when already loaded). If the load returns no matching tool (and the tool is not already callable — an empty load for an already-exposed tool is an expected no-op, not absence), use the
non-MCP setup fallback instead of retrying the failing call.

1. Load the brownfield MCP tool: `tool discovery query: "+ouroboros brownfield"`
2. Call scan+register:
   ```
   Tool: ouroboros_brownfield
   Arguments: { "action": "scan" }
   ```
   This walks `scan_root` up to two directory levels deep for valid seed repos/worktrees and registers them in DB. Each repo or worktree reached directly by the walk is registered self-only. Git worktree families are not expanded, so main or sibling worktrees outside the depth-bounded walk are not pulled in. Existing defaults are preserved.

**Scan boundaries:**
- The filesystem walk starts at `scan_root`; when omitted, `scan_root` defaults to the current user's home directory.
- Repositories are discovered directly by walking directories inside `scan_root`, at most two levels deep.
- Dot-prefixed directories and known noisy directories such as `node_modules` are not walked as seed locations.
- Both normal repos with a `.git` directory and linked worktrees with a `.git` file are registered when the walk reaches them.
- Git worktree families are not expanded. A worktree is registered only when the depth-bounded walk finds it directly.
- Local repos, repos without remotes, and repos whose remotes are not named `origin` are all eligible.

The scan response `text` already contains a pre-formatted numbered list with `[default]` markers. **Do NOT make any additional MCP calls to list or query repos.**

**Display the repos in a plain-text 2-column grid** (NOT a markdown table). Use a code block so columns align. Example:

```
Scan complete. 8 repositories registered.

 1. repo-alpha                   5. repo-epsilon
 2. repo-bravo *                 6. repo-foxtrot
 3. repo-charlie                 7. repo-golf *
 4. repo-delta                   8. repo-hotel
```

Include `*` markers for defaults exactly as they appear in the scan response. Do not summarize or truncate the list. The user needs to see all repo numbers to pick defaults.

**If no repos found**, skip the default selection prompt and proceed to Step 6.

**Default repo selection — end the turn with the list:**

**Do NOT use `AskUserQuestion` for this selection.** Assistant text emitted
between tool calls is not guaranteed to render, so a question dialog fired in
the same turn can appear without the repo list the user needs to answer it.
Option `preview` fields cannot hold the list either — the preview box has a
fixed height and silently truncates long lists.

Instead, **end the turn with the repo grid as the final message** so its
display is guaranteed, and collect the selection as a plain chat reply.

Immediately below the grid, append the selection prompt:

**If defaults exist:**
```
Current defaults: <current default names> (numbers <current default numbers>)

Reply with repo numbers to change defaults (e.g. "6, 18, 19"),
"keep" to keep the current defaults, or "none" to clear them.
```

**If no defaults exist:**
```
No defaults set.

Reply with repo numbers to set defaults (e.g. "6, 18, 19"),
or "none" to run interviews in greenfield mode.
```

Then **end the turn** — no tool calls after the grid.

On the next turn, parse the user's reply:

- Numbers (any separator) → those indices
- "keep" (defaults exist) → skip the MCP call, confirm defaults unchanged, proceed to Step 6
- "none" → empty indices (clear all)
- Anything else → ask again in plain text; do not guess

Then re-run `tool discovery query: "+ouroboros brownfield"` and use ONE MCP call to update all defaults at once:

```
Tool: ouroboros_brownfield
Arguments: { "action": "set_defaults", "indices": "<comma-separated IDs>" }
```

Example: if the user picks IDs 6, 18, 19 → `{ "action": "set_defaults", "indices": "6,18,19" }`

This clears all existing defaults and sets the selected repos as default in one call.

If "none" → `{ "action": "set_defaults", "indices": "" }` to clear all defaults.

**Celebration Checkpoint 5.5:**
```
Brownfield defaults updated!
Defaults: podo-app, podo-backend, grape

These repos will be used as context in interviews.
```

Or if "none" selected:
```
No default repos set. interviews will run in greenfield mode.
You can set defaults anytime by running ooo setup again.
```

---

### Step 6: First Project Nudge

Encourage immediate action:

```

Your first Ouroboros project is waiting!

The best way to learn is by doing. Try:

  ooo interview "Build a CLI tool for [something you need]"

Or explore examples:
  ooo tutorial

You're going to love seeing vague ideas turn into
crystal-clear specifications. Let's build something amazing!
```

---

## Progressive Disclosure Schedule

Reveal features gradually to avoid overwhelm:

### Immediate (Plugin Mode)
- `ooo interview` - Socratic clarification
- `ooo seed` - Specification generation
- `ooo unstuck` - Lateral thinking

### After Setup (MCP Mode)
- `ooo run` - TUI execution
- `ooo evaluate` - 3-stage verification
- `ooo status` - Drift tracking

### Power User (Discover organically)
- Evolutionary loop and ralph persistence
- Cost prediction and optimization
- Session replay and debugging
- Custom agents and skills

---

## Uninstall

When invoked with `--uninstall`:

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Ouroboros Uninstall
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

This will remove:
- MCP server registration from ~/.claude/mcp.json
- Ouroboros block from CLAUDE.md

This will NOT remove:
- Plugin files in .claude-plugin/
- Any generated seeds or projects
- Your project data

Uninstall Ouroboros configuration? [Yes / No]
```

If Yes:
1. Remove `ouroboros` entry from `~/.claude/mcp.json` (if exists)
2. Remove `<!-- ooo:START -->` to `<!-- ooo:END -->` block from CLAUDE.md (if exists)
3. Confirm: "Ouroboros plugin configuration removed. To remove plugin files, run: claude plugin uninstall ouroboros"

---

## Setup Troubleshooting

### "python3: command not found"
```
Plugin mode still works! You can use:
- ooo interview
- ooo seed
- ooo unstuck

For Full Mode, install Python >= 3.12:
  macOS: brew install python@3.12
  Ubuntu: sudo apt install python3.12
  Windows: python.org/downloads
```

### "uvx: command not found"
```
uvx is recommended but not required. Alternative:

Install Ouroboros globally (see docs/getting-started.md for all options):
  pip install 'ouroboros-ai[mcp,claude]'

Then update ~/.claude/mcp.json with:
  "command": "python"
  "args": ["-m", "ouroboros", "mcp", "serve"]
```

### "~/.claude/mcp.json conflicts"
```
Ouroboros will merge with existing MCP servers.
If you see issues, share your ~/.claude/mcp.json content and
we'll help you resolve conflicts.
```

---

## Conversion Metrics Track

Track these checkpoints for conversion optimization:

- [ ] Started setup (skill invoked)
- [ ] Environment detected successfully
- [ ] MCP server registration accepted
- [ ] CLAUDE.md integration accepted
- [ ] Verification passed
- [ ] Brownfield repos scanned and registered
- [ ] Default brownfield repo selected
- [ ] First project started (ooo interview)
- [ ] First seed generated (ooo seed)
- [ ] First execution completed (ooo run)

A fully converted user = all checkpoints passed

## RFC #1392 State Breadcrumb Footer

Your final response MUST end with exactly one breadcrumb footer line:

```
◆ <current state> → next: <recommended action>
```

Derive `<current state>` from live session state via `ouroboros_session_status` when that MCP projection is available; otherwise derive it from this skill's actual outcome. Never use a linear `Step N of M` footer because Ouroboros is an evolutionary loop. When the next action is genuinely a choice, list 2-3 honest options in the `next:` clause. The breadcrumb line must be the last line of the response.
