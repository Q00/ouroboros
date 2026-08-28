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
| `ooo auto ...` | Read `skills/auto/SKILL.md` and follow it |
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
| `ooo cancel` | Read `skills/cancel/SKILL.md` and follow it |
| `ooo config` | Read `skills/config/SKILL.md` and follow it |
| `ooo qa` or `ooo qa ...` | Read `skills/qa/SKILL.md` and follow it |
| `ooo help` | Read `skills/help/SKILL.md` and follow it |
| `ooo update` | Read `skills/update/SKILL.md` and follow it |
| `ooo pm` or `ooo pm ...` | Read `skills/pm/SKILL.md` and follow it |
| `ooo brownfield` or `ooo brownfield ...` | Read `skills/brownfield/SKILL.md` and follow it |
| `ooo publish` or `ooo publish ...` | Read `skills/publish/SKILL.md` and follow it |
| `ooo resume-session` | Read `skills/resume-session/SKILL.md` and follow it |

**Important**: Do NOT use the Skill tool. Read the file with the Read tool and execute its instructions directly.

## Agents

Bundled agents live in `src/ouroboros/agents/`. When a skill references an agent (e.g., `ouroboros:socratic-interviewer`), read its definition from `src/ouroboros/agents/{name}.md` and adopt that role. Use `OUROBOROS_AGENTS_DIR` or `.claude-plugin/agents/` only for explicit custom overrides.

## Pull Request Boundary Policy

Before implementing or reviewing a PR, read [Review Boundary Contract](CONTRIBUTING.md#review-boundary-contract). The PR description is the review contract, not an informal suggestion.

Require contributors to declare: the one user problem; supported inputs and execution conditions; observable contract and invariants; changed subsystem, data/security boundary, and owner; non-goals; and evidence. Do not absorb new lifecycle, rollback, concurrency, filesystem-authority, or other ownership requirements without a maintainer decision.

For every review finding, ask:

1. Does it reproduce under the promised inputs and execution conditions?
2. Does it break the promised contract?
3. Does fixing it require a new subsystem or ownership boundary?
4. Can the original user problem be solved without the subsystem introduced by this PR?
5. If scope is split, does immediate user-data or security risk remain?

Use these decisions:

- Q1 + Q2: **Changes Requested**.
- Q3 + Q5: stop and revisit the RFC with the maintainer; do not silently absorb the new subsystem or owner.
- Q3 + Q4 and not Q5: owned follow-up, not a blocker; require a named owner and link.
- Q5 without Q3 + Q5 escalation: **Changes Requested** for the immediate user-data or security risk.
- Outside the declared boundary without a contract violation: not a blocker; do not expand the PR.

Only the maintainer decides scope expansion. Keep review comments tied to the declared boundary and directly evidenced behavior.

<!-- ooo:START -->
<!-- ooo:VERSION:0.26.0 -->
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
| `ooo auto` | MCP: `ouroboros_auto` |
| `ooo interview` | `ouroboros:socratic-interviewer` |
| `ooo seed` | `ouroboros:seed-architect` |
| `ooo run` | MCP required |
| `ooo evolve` | MCP: `evolve_step` |
| `ooo evaluate` | `ouroboros:evaluator` |
| `ooo qa` | `ouroboros:qa-judge` |
| `ooo unstuck` | `ouroboros:{persona}` |
| `ooo status` | MCP: `session_status` |
| `ooo ralph` | Persistent loop until verified |
| `ooo tutorial` | Interactive hands-on learning |
| `ooo setup` | — |
| `ooo help` | — |
| `ooo update` | PyPI version check + upgrade |
| `ooo publish` | `gh` CLI — Seed to GitHub Issues |
| `ooo resume-session` | Restore previous Claude Code session context |

## Agents

Loaded on-demand — not preloaded.

**Core**: socratic-interviewer, ontologist, seed-architect, evaluator, qa-judge, contrarian
**Support**: hacker, simplifier, researcher, architect
<!-- ooo:END -->
