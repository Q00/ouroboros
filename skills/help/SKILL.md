---
name: ouroboros-help
description: "Ouroboros command reference and troubleshooting guidance for installation, MCP, status, and recovery"
---

# /ouroboros:ouroboros-help

Full reference guide for Ouroboros power users.

## Usage

```
ooo help
/ouroboros:ouroboros-help
```

## Troubleshooting

When asked to diagnose an Ouroboros installation, MCP connection, stalled job,
or unexpected result, read [TROUBLESHOOTING.md](TROUBLESHOOTING.md) in this
skill directory first. Resolve that relative path from the directory containing
this `SKILL.md`, even if that directory is hidden. If the skill was delivered
as a standalone cached entrypoint without its sibling file, use the Python
interpreter belonging to that same Ouroboros installation to read its packaged resource:

```bash
python -c 'from importlib.resources import files; print(files("ouroboros").joinpath("skills", "help", "TROUBLESHOOTING.md").read_text(encoding="utf-8"))'
```

`python` above must be that installation's interpreter, not an arbitrary shell
Python. If it cannot be identified or the resource is unavailable, say so and
use the installed version's help. Follow the installed version's help and preserve the
user's data and running work. Do not claim a repair or successful run without
evidence.

## What Is Ouroboros?

A specification-first workflow engine: it turns vague ideas into a validated
Seed (goal, constraints, acceptance criteria) before any code is written.

```
Interview → Seed → Execute → Evaluate
    ↑                           ↓
    └─── Evolutionary Loop ─────┘
```

## Commands

Each `ooo <name>` is also the skill `/ouroboros:<name>`, except `run`, `status`,
`config`, and `help`, which are `/ouroboros:ouroboros-<name>`.

| Command | Purpose | Needs |
|---------|---------|-------|
| `/ouroboros:welcome` | First-touch onboarding | none |
| `/ouroboros:setup` | Claude Code setup wizard | none |
| `/ouroboros:tutorial` | Interactive hands-on learning | none |
| `ooo help` | This reference | none |
| `ooo interview` | Socratic interview that exposes hidden assumptions | MCP, in-session fallback |
| `ooo idk` | Calibrate interview wording from stated familiarity | MCP, in-session fallback |
| `ooo seed` | Freeze interview results into a Seed | MCP, in-session fallback |
| `ooo auto` | Goal to A-grade Seed, then execute and verify in the background | MCP |
| `ooo run` | Execute a Seed and verify every acceptance criterion | MCP |
| `ooo evaluate` | Mechanical checks, semantic review per criterion, consensus when uncertain | MCP, in-session fallback |
| `ooo evolve` | Start or monitor an evolutionary loop across Seed generations | MCP |
| `ooo ralph` | Server-owned loop until verified (`ouroboros_ralph` runs it as a background job) | MCP |
| `ooo unstuck` | Five lateral thinking personas, single or debate | MCP |
| `ooo status` | Session status and goal drift | MCP |
| `ooo qa` | General-purpose QA verdict for any artifact | MCP, in-session fallback |
| `ooo pm` | PM-focused interview and PRD | MCP |
| `ooo brownfield` | Scan and manage brownfield repo defaults; `find <topic>` ranks registered repos | MCP |
| `ooo config` | Settings GUI for per-stage agents and models | CLI |
| `ooo cancel` | Cancel stuck or orphaned executions | CLI |
| `ooo resume-session` | List in-flight sessions and re-attach commands | CLI |
| `ooo update` | Check for updates and upgrade | CLI |
| `ooo publish` | Publish a Seed as GitHub Issues | `gh` |

**MCP** = the Ouroboros MCP server, which the Claude marketplace plugin or a
supported host setup (`ouroboros setup --runtime <runtime>`) provides.
**In-session fallback** = runs advisory-only through a bundled agent when the
MCP server is unavailable. **CLI** = drives the `ouroboros` command.

## Natural Language Triggers

In Claude Code these phrases route to a command without the `ooo` prefix:

| Phrase | Command |
|--------|---------|
| "interview me", "clarify requirements", "socratic interview" | `ooo interview` |
| "I don't know this term", "explain at my level" | `ooo idk` |
| "crystallize", "generate seed", "create seed", "freeze requirements" | `ooo seed` |
| "ouroboros run", "execute seed", "run seed", "run workflow" | `ooo run` |
| "evaluate this", "3-stage check", "verify execution" | `ooo evaluate` |
| "evolve", "evolutionary loop", "iterate until converged" | `ooo evolve` |
| "ralph", "don't stop", "must complete", "until it works", "keep going" | `ooo ralph` |
| "think sideways", "i'm stuck", "break through", "lateral thinking" | `ooo unstuck` |
| "am I drifting?", "drift check", "session status" | `ooo status` |
| "write prd", "pm interview", "product requirements", "create prd" | `ooo pm` |
| "qa check", "quality check" | `ooo qa` |
| "cancel execution", "stop job", "kill stuck", "abort execution" | `ooo cancel` |
| "update ouroboros", "upgrade ouroboros" | `ooo update` |
| "brownfield defaults", "brownfield scan" | `ooo brownfield` |

## Agents

| Agent | Purpose |
|-------|---------|
| `ouroboros:socratic-interviewer` | Exposes hidden assumptions through questioning |
| `ouroboros:ontologist` | Finds root problems vs symptoms |
| `ouroboros:seed-architect` | Crystallizes requirements into a Seed |
| `ouroboros:evaluator` | Verifies results against acceptance criteria |
| `ouroboros:qa-judge` | Renders QA verdicts for `ooo qa` |
| `ouroboros:contrarian` | "Are we solving the wrong problem?" |
| `ouroboros:hacker` | "Make it work first, elegance later" |
| `ouroboros:simplifier` | "Cut scope to absolute minimum" |
| `ouroboros:researcher` | "Stop coding, start investigating" |
| `ouroboros:architect` | "Question the foundation, redesign if needed" |

The last five are the `ooo unstuck` personas. Other files in
`src/ouroboros/agents/` are internal pipeline roles.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
