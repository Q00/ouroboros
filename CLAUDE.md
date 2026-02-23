# Ouroboros - Development Environment

> This CLAUDE.md is for **local development only**. End users install via:
> ```
> claude plugin marketplace add Q00/ouroboros
> claude plugin install ouroboros@ouroboros
> ```
> Once installed as a plugin, skills/hooks/agents work natively without this file.

## ooo Commands (Dev Mode)

When the user types any of these commands, read the corresponding SKILL.md file and follow its instructions exactly:

| Input | Action |
|-------|--------|
| `ooo` (bare, no subcommand) | Read `skills/welcome/SKILL.md` and follow it |
| `ooo interview ...` | Read `skills/interview/SKILL.md` and follow it |
| `ooo seed` | Read `skills/seed/SKILL.md` and follow it |
| `ooo run` | Read `skills/run/SKILL.md` and follow it |
| `ooo evaluate` or `ooo eval` | Read `skills/evaluate/SKILL.md` and follow it |
| `ooo evolve ...` | Read `skills/evolve/SKILL.md` and follow it |
| `ooo unstuck` or `ooo stuck` or `ooo lateral` | Read `skills/unstuck/SKILL.md` and follow it |
| `ooo status` or `ooo drift` | Read `skills/status/SKILL.md` and follow it |
| `ooo ralph` | Read `skills/ralph/SKILL.md` and follow it |
| `ooo tutorial` | Read `skills/tutorial/SKILL.md` and follow it |
| `ooo setup` | Read `skills/setup/SKILL.md` and follow it |
| `ooo welcome` | Read `skills/welcome/SKILL.md` and follow it |
| `ooo help` | Read `skills/help/SKILL.md` and follow it |

**Important**: Do NOT use the Skill tool. Read the file with the Read tool and execute its instructions directly.

## Agents

Custom agents are in `agents/`. When a skill references an agent (e.g., `ouroboros:socratic-interviewer`), read its definition from `agents/{name}.md` and adopt that role.

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
| `ooo ralph` | Persistent loop until verified |
| `ooo tutorial` | Interactive hands-on learning |
| `ooo setup` | — |
| `ooo help` | — |

## Agents

Loaded on-demand — not preloaded.

**Core**: socratic-interviewer, ontologist, seed-architect, evaluator, contrarian
**Support**: hacker, simplifier, researcher, architect
<!-- ooo:END -->
