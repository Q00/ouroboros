# /ouroboros:setup

Guided onboarding wizard that converts users into power users.

## Usage

```
ooo setup
/ouroboros:setup
/ouroboros:setup --uninstall
```

> **Note**: `ooo interview` and `ooo seed` work immediately without setup.
> Run setup only when you need advanced features (TUI dashboard, evaluation, drift tracking).

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

### Step 1: Environment Detection

Check the user's environment with clear feedback:

```bash
python3 --version
which uvx 2>/dev/null && uvx --version 2>/dev/null
which claude 2>/dev/null
```

**IMPORTANT: If system Python is < 3.14 but uvx is available, also check uv-managed Python:**

```bash
uv python list 2>/dev/null | grep "cpython-3.14"
```

If `uv python list` shows Python 3.14+ available, this counts as **Full Mode** because `uvx ouroboros-ai mcp serve` automatically uses uv-managed Python 3.14+ (not system Python).

**Report results with personality:**

```
Environment Detected:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

System Python 3.13         [!] Below 3.14
uv Python 3.14+            [✓] Available (uvx will use this)
uvx package runner         [✓] Available
Claude Code CLI            [✓] Detected

→ Full Mode Available (via uvx + uv-managed Python 3.14)
```

**Decision Matrix:**

| Environment | Mode | Features |
|:------------|:-----|:---------|
| uvx + uv Python 3.14+ | **Full Mode** | Everything (TUI, MCP, evaluation, drift) |
| System Python 3.14+ | **Full Mode** | Everything |
| uvx + Python < 3.14 only | Plugin-Only | `uv python install 3.14` to upgrade |
| No Python, no uvx | Plugin-Only | Plugin skills work immediately |

**Celebration Checkpoint 1:**
```
Great news! You're ready for Full Mode with all features enabled.
```

---

### Step 2: MCP Server Registration

**Only if Full Mode (Python 3.14+)**

Check if `~/.claude/mcp.json` exists:

```bash
ls -la ~/.claude/mcp.json 2>/dev/null && echo "EXISTS" || echo "NOT_FOUND"
```

**Engaging Question:**
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  MCP Server Registration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Ouroboros can register its MCP server for complete
Python integration. This unlocks:

  Visual TUI Dashboard    [Watch execution in real-time]
  3-Stage Evaluation     [Mechanical → Semantic → Consensus]
  Drift Detection        [Alert when projects go off-track]
  Session Replay         [Debug any execution from events]

Register MCP server? [Yes / No / Learn more]
```

**If "Learn more":**
```
MCP (Model Context Protocol) enables Claude Code to
communicate directly with Ouroboros Python core.

Without MCP: Plugin skills work (interview, seed, unstuck)
With MCP: Full execution pipeline with visualization

Recommendation: Enable for complete Ouroboros experience.
```

**If Yes, create or update `~/.claude/mcp.json`** (user-level, works across all projects):
```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "uvx",
      "args": ["ouroboros-ai", "mcp", "serve"]
    }
  }
}
```

If `~/.claude/mcp.json` already exists, merge intelligently (preserve other servers).

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
<!-- ooo:VERSION:0.11.0 -->
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
ls agents/ | wc -l  # Should show 9+ agents
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

Mode:                     Full Mode (Python 3.14 + MCP)
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
3. Confirm: "Ouroboros plugin configuration removed. To remove plugin files, run: claude /plugin uninstall ouroboros"

---

## Setup Troubleshooting

### "python3: command not found"
```
Plugin mode still works! You can use:
- ooo interview
- ooo seed
- ooo unstuck

For Full Mode, install Python 3.14+:
  macOS: brew install python@3.14
  Ubuntu: sudo apt install python3.14
  Windows: python.org/downloads
```

### "uvx: command not found"
```
uvx is recommended but not required. Alternative:

Install Ouroboros globally:
  pip install ouroboros-ai

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
- [ ] First project started (ooo interview)
- [ ] First seed generated (ooo seed)
- [ ] First execution completed (ooo run)

A fully converted user = all checkpoints passed
