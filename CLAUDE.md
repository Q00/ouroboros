# Ouroboros - Development Environment

> This CLAUDE.md is for **local development only**. End users install via:
> ```
> claude /plugin marketplace add github:Q00/ouroboros
> claude /plugin install ouroboros@ouroboros
> ```
> Once installed as a plugin, skills/hooks/agents work natively without this file.

## ooo Commands (Dev Mode)

When the user types any of these commands, read the corresponding SKILL.md file and follow its instructions exactly:

| Input | Action |
|-------|--------|
| `ooo` (bare, no subcommand) | Read `.claude-plugin/skills/welcome/SKILL.md` and follow it |
| `ooo interview ...` | Read `.claude-plugin/skills/interview/SKILL.md` and follow it |
| `ooo seed` | Read `.claude-plugin/skills/seed/SKILL.md` and follow it |
| `ooo run` | Read `.claude-plugin/skills/run/SKILL.md` and follow it |
| `ooo evaluate` or `ooo eval` | Read `.claude-plugin/skills/evaluate/SKILL.md` and follow it |
| `ooo evolve ...` | Read `.claude-plugin/skills/evolve/SKILL.md` and follow it |
| `ooo unstuck` or `ooo stuck` or `ooo lateral` | Read `.claude-plugin/skills/unstuck/SKILL.md` and follow it |
| `ooo status` or `ooo drift` | Read `.claude-plugin/skills/status/SKILL.md` and follow it |
| `ooo setup` | Read `.claude-plugin/skills/setup/SKILL.md` and follow it |
| `ooo welcome` | Read `.claude-plugin/skills/welcome/SKILL.md` and follow it |
| `ooo help` | Read `.claude-plugin/skills/help/SKILL.md` and follow it |

**Important**: Do NOT use the Skill tool. Read the file with the Read tool and execute its instructions directly.

## Agents

Custom agents are in `.claude-plugin/agents/`. When a skill references an agent (e.g., `ouroboros:socratic-interviewer`), read its definition from `.claude-plugin/agents/{name}.md` and adopt that role.
