# /ouroboros:setup

Setup wizard for installing and configuring Ouroboros.

## Usage

```
/ouroboros:setup
/ouroboros:setup --uninstall
```

## Setup Steps

When the user invokes this skill, run the following 5-step wizard:

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
This adds ~40 lines with skill triggers and agent descriptions.
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

## Skills
- "interview me" or /ouroboros:interview - Socratic requirement clarification
- "crystallize" or /ouroboros:seed - Generate validated seed specification
- "ouroboros run" or /ouroboros:run - Execute seed workflow
- "evaluate this" or /ouroboros:evaluate - Three-stage verification
- "think sideways" or /ouroboros:unstuck - Lateral thinking (5 personas)
- "am I drifting?" or /ouroboros:status - Session status and drift check

## Agents
- ouroboros:socratic-interviewer - Exposes hidden assumptions
- ouroboros:ontologist - Finds root problems, not symptoms
- ouroboros:seed-architect - Crystallizes into seed spec
- ouroboros:evaluator - Three-stage verification
- ouroboros:contrarian - "Are we solving the wrong problem?"
- ouroboros:hacker - "Make it work first"
- ouroboros:simplifier - "Cut scope to MVP"
<!-- OUROBOROS:END -->
```

### Step 4: Star Solicitation (Optional)

Check `~/.ouroboros/prefs.json` for `star_asked`. If not already asked:

```
Ouroboros setup complete!

If this project is helpful, a GitHub star supports continued development.

  [1] Yes, star the project
  [2] No thanks
  [3] Remind me after my first interview
```

- Option 1: Run `gh api -X PUT /user/starred/Q00/ouroboros`, save `star_asked: true`
- Option 2: Save `star_asked: true` (permanent opt-out, never ask again)
- Option 3: Save `star_remind_after_interview: true`

Store preferences in `~/.ouroboros/prefs.json`.

### Step 5: Verification

Run a quick verification:

1. Check skills are loadable: List available `/ouroboros:*` skills
2. If MCP registered: Verify the server responds (optional, can skip if uvx not ready)
3. Show summary:

```
Ouroboros Setup Complete
========================
Mode: Full (Python 3.14 + MCP)
Skills: 8 registered
Agents: 7 available
MCP Server: Registered
CLAUDE.md: Updated

Quick start: /ouroboros:interview "your project idea"
```

## Uninstall

When invoked with `--uninstall`:

1. Remove `ouroboros` entry from `.mcp.json`
2. Remove `<!-- OUROBOROS:START -->` to `<!-- OUROBOROS:END -->` block from CLAUDE.md
3. Confirm: "Ouroboros plugin configuration removed. Plugin files remain in .claude-plugin/."
