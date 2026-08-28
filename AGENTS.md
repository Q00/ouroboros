# Ouroboros - Development Environment

> This AGENTS.md is for **local development only**. End users install via:
> ```
> Codex plugin marketplace add Q00/ouroboros
> Codex plugin add ouroboros@ouroboros
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
| `ooo qa` or `ooo qa ...` | Read `skills/qa/SKILL.md` and follow it |
| `ooo help` | Read `skills/help/SKILL.md` and follow it |
| `ooo update` | Read `skills/update/SKILL.md` and follow it |
| `ooo pm` or `ooo pm ...` | Read `skills/pm/SKILL.md` and follow it |
| `ooo brownfield` or `ooo brownfield ...` | Read `skills/brownfield/SKILL.md` and follow it |
| `ooo publish` or `ooo publish ...` | Read `skills/publish/SKILL.md` and follow it |
| `ooo resume-session` | Read `skills/resume-session/SKILL.md` and follow it |

**Important**: Do NOT use the Skill tool. Read the file with the Read tool and execute its instructions directly.

## Agents

Bundled agents live in `src/ouroboros/agents/`. When a skill references an agent (e.g., `ouroboros:socratic-interviewer`), read its definition from `src/ouroboros/agents/{name}.md` and adopt that role. Use `OUROBOROS_AGENTS_DIR` or `.Codex-plugin/agents/` only for explicit custom overrides.

## Shipping a change (read before you commit)

**The code you edit is not the code that runs, by default.** The checked-in
`.mcp.json` points at the published PyPI package, so edits to this working tree
have no effect on a client until you repoint it at local source. See
[docs/contributing/developing.md](docs/contributing/developing.md).

`main` is protected: direct pushes are rejected with `GH006`, for everyone,
including the owner. Every change lands through a squash-merged PR, so a
release tag must be created on the merged `main` commit — not before.

Four checks are required to merge — reproduce them locally first:

```bash
uv run ruff format src/ tests/ && uv run ruff check src/ tests/ --fix
uv run mypy src/ouroboros
uv run pytest
```

(`Ruff Lint`, `MyPy Type Check`, `Test Python 3.12`, `Bridge TypeScript`.)

Other gates fire conditionally and are easy to trip blind:

- **Issue link present** — every PR needs `Refs #123` in the body. Exempt via
  the `no-issue` label, or a title starting `chore(deps)` / `chore(release)` /
  `release:`. Note `chore: release vX.Y.Z` does *not* match the prefix.
- **enforce-module-size** — 2000-line cap per module; grandfathered modules may
  shrink, never grow, and no new entries.
- **enforce-boundary** — no domain keywords (`github`, `jira`, `slack`, …) in
  `src/ouroboros/auto/`.
- **enforce-perf-budget** — PRs touching `src/ouroboros/auto/` need the R-run
  table filled in the PR body.

Full reference, including every escape hatch and the release sequence:
**[docs/contributing/ci-gates.md](docs/contributing/ci-gates.md)**.

### Review Boundary Contract

For every PR, recover the contributor's structured boundary before evaluating
implementation details. The boundary must identify the user problem, promised
behavior, inputs/preconditions, execution conditions, owned subsystems and
owners, non-goals, and verification scenario.

Use this five-question decision gate:

1. Does the finding reproduce under the promised inputs and execution conditions?
2. Does it break the PR's promised contract?
3. Does fixing it require a new subsystem or new ownership?
4. Can the original user problem be solved without the PR-introduced subsystem?
5. Would splitting the extra scope leave immediate user-data or security risk?

Decision rules:

- Questions 1 and 2 true: `REQUEST_CHANGES`, with current-HEAD evidence.
- Question 3 or 4 true and question 5 false: non-blocking follow-up, named owner,
  and no scope expansion in the current PR.
- Questions 3 and 5 true: stop the review escalation and ask the maintainer for
  an RFC/scope decision. Do not design the new subsystem in the blocker.
- Missing boundary: request the missing contract fields once; do not invent
  blockers from an ambiguous or unsupported assumption.

The contributor owns the contract and boundary. The review bot owns only
contract violations and direct risks. A risk outside the valid boundary is a
follow-up owned by the appropriate subsystem owner. The maintainer decides
whether scope expansion is allowed. Consolidate one root cause into one
finding; never drip the same assumption into repeated rollback, concurrency,
filesystem, or lifecycle blockers.
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
| `ooo auto` | MCP: `ouroboros_start_auto` |
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
| `ooo resume-session` | Restore previous Codex session context |

## Agents

Loaded on-demand — not preloaded.

**Core**: socratic-interviewer, ontologist, seed-architect, evaluator, qa-judge, contrarian
**Support**: hacker, simplifier, researcher, architect
**Analysis**: analysis-agent, ontology-analyst, codebase-explorer, research-agent, breadth-keeper
**Execution**: code-executor, seed-closer, consensus-reviewer, semantic-evaluator
**Specialist**: advocate, judge
<!-- ooo:END -->
