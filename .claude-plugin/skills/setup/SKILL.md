# /ouroboros:setup

Setup wizard for installing and configuring Ouroboros MCP server.

## Usage

```
ooo setup
/ouroboros:setup
/ouroboros:setup --uninstall
```

> **Note**: `ooo interview` and `ooo seed` work immediately without setup.
> Run setup only when you need MCP features (`ooo run`, `ooo evaluate`, `ooo status`).

## Setup Steps

When the user invokes this skill, run the following 4-step wizard:

### Step 1: Environment Detection

Check the user's environment:

```bash
python3 --version
```

- If Python >= 3.14: **Full Mode** available (MCP server + all features)
- If Python >= 3.12 but < 3.14: Warn about limited support, suggest upgrade
- If no Python: **Plugin-Only Mode** (agents + skills only, no MCP)

Also check for `uvx`:
```bash
uvx --version
```

Report the detection results to the user.

### Step 2: MCP Server Registration

**Only if Full Mode (Python 3.14+)**:

Check if `.mcp.json` exists in the project root. If not, ask the user:

```
Ouroboros can register its MCP server for full Python integration.
This enables: seed execution, 3-stage evaluation, drift measurement,
and session tracking.

Register MCP server? [Yes / No]
```

If yes, create or update `.mcp.json`:
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

If `.mcp.json` already has other servers, merge (don't overwrite).

### Step 3: CLAUDE.md Integration (Optional)

Ask the user:

```
Add Ouroboros quick-reference to CLAUDE.md?
This adds ~30 lines with ooo commands and agent descriptions.
A backup will be created at CLAUDE.md.bak.

[Yes / No / Show preview first]
```

If "Show preview", display the block below. If "Yes":

1. Backup: Copy current CLAUDE.md to CLAUDE.md.bak
2. Append the following block to CLAUDE.md:

```markdown
<!-- OUROBOROS:START -->
<!-- OUROBOROS:VERSION:0.9.0 -->
# Ouroboros - Requirement Crystallization Engine

## Commands (ooo prefix)
- `ooo` - Welcome + quick start
- `ooo interview` - Socratic requirement clarification (Plugin)
- `ooo seed` - Generate validated seed specification (Plugin)
- `ooo run` - Execute seed workflow (MCP)
- `ooo evaluate` - Three-stage verification (MCP)
- `ooo unstuck` - Lateral thinking, 5 personas (Plugin)
- `ooo status` - Session status and drift check (MCP)
- `ooo setup` - Installation wizard (Plugin)
- `ooo help` - Full reference guide (Plugin)

## Agents
- ouroboros:socratic-interviewer - Exposes hidden assumptions
- ouroboros:ontologist - Finds root problems, not symptoms
- ouroboros:seed-architect - Crystallizes into seed spec
- ouroboros:evaluator - Three-stage verification
- ouroboros:contrarian - "Are we solving the wrong problem?"
- ouroboros:hacker - "Make it work first"
- ouroboros:simplifier - "Cut scope to MVP"
- ouroboros:researcher - "Stop coding, start investigating"
- ouroboros:architect - "Redesign if the structure is wrong"
<!-- OUROBOROS:END -->
```

### Step 4: Verification

Run a quick verification:

1. Check skills are loadable: List available `/ouroboros:*` skills
2. If MCP registered: Verify the server responds (optional, can skip if uvx not ready)
3. Show summary:

```
Ouroboros Setup Complete
========================
Mode: Full (Python 3.14 + MCP)
Skills: 9 registered
Agents: 9 available
MCP Server: Registered
CLAUDE.md: Updated

Quick start: ooo interview "your project idea"
```

## Uninstall

When invoked with `--uninstall`:

1. Remove `ouroboros` entry from `.mcp.json`
2. Remove `<!-- OUROBOROS:START -->` to `<!-- OUROBOROS:END -->` block from CLAUDE.md
3. Confirm: "Ouroboros plugin configuration removed. Plugin files remain in .claude-plugin/."
