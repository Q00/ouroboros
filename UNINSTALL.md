# Clean Uninstall Guide

## One Command

```bash
ouroboros uninstall
```

This interactively removes all Ouroboros configuration:
- MCP server registration (`~/.claude/mcp.json`, `~/.codex/config.toml`)
- CLAUDE.md integration block (`<!-- ooo:START -->` ... `<!-- ooo:END -->`)
- Codex rules and skills (`~/.codex/rules/ouroboros.md`, `~/.codex/skills/ouroboros/`)
- Project-level config (`.ouroboros/`)
- Data directory (`~/.ouroboros/` — config, credentials, DB, seeds, logs, locks, prefs)

Then finish with:

```bash
uv tool uninstall ouroboros-ai            # or: pip uninstall ouroboros-ai
claude plugin uninstall ouroboros         # if using Claude Code plugin
```

### Options

| Flag | Effect |
|:-----|:-------|
| `-y`, `--yes` | Skip confirmation prompt |
| `--dry-run` | Preview what would be removed, change nothing |
| `--keep-data` | Keep entire `~/.ouroboros/` (config, credentials, seeds, DB, logs) |

### Inside Claude Code

Inside an active Claude Code session, type:

```
/ouroboros:setup --uninstall
```

This is a **skill command** (not a CLI flag) that removes MCP registration and the CLAUDE.md block interactively.

---

## What Lives Where

| Path | Created by | Contents |
|:-----|:-----------|:---------|
| `~/.claude/mcp.json` | `ooo setup` / `ouroboros setup` | MCP server entry |
| `~/.codex/config.toml` | `ouroboros setup --runtime codex` | Codex MCP section |
| `~/.codex/rules/ouroboros.md` | `ouroboros setup --runtime codex` | Codex rules |
| `~/.codex/skills/ouroboros/` | `ouroboros setup --runtime codex` | Codex skills |
| `CLAUDE.md` | `ooo setup` | Command reference block |
| `~/.ouroboros/config.yaml` | `ouroboros setup` | Runtime configuration |
| `~/.ouroboros/credentials.yaml` | `ouroboros setup` | API credentials |
| `~/.ouroboros/ouroboros.db` | First run | Event store + brownfield registry |
| `~/.ouroboros/seeds/` | `ooo seed` / `ooo interview` | Generated seed specs |
| `~/.ouroboros/data/` | `ooo interview` | Interview state |
| `~/.ouroboros/logs/` | Any run | Log files |
| `~/.ouroboros/locks/` | `ooo run` | Heartbeat locks |
| `~/.ouroboros/prefs.json` | `ooo setup` | Preferences |
| `.ouroboros/` (project) | `ooo evaluate` | Mechanical eval config |

## What Is NOT Removed

- Your project source code and git history
- Generated seed YAML files copied outside `~/.ouroboros/seeds/`
- Package manager caches (run `uv cache clean ouroboros-ai` or `pip cache purge` if needed)
