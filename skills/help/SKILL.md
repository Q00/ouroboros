---
name: help
description: "Full reference guide for Ouroboros commands and agents"
---

# /ouroboros:help

Full reference guide for Ouroboros power users.

## Usage

```
ooo help
/ouroboros:help
```

## What Is Ouroboros?

Ouroboros is a **requirement crystallization engine** for AI workflows. It transforms vague ideas into validated specifications through:

1. **Socratic Interview** - Exposes hidden assumptions
2. **Seed Generation** - Creates immutable specifications
3. **PAL Routing** - Auto-escalates/descends model complexity
4. **Lateral Thinking** - 5 personas to break stagnation
5. **3-Stage Evaluation** - Mechanical > Semantic > Consensus

## All Commands

### Core Commands

| Command | Purpose | Mode |
|---------|---------|------|
| `ooo` | Welcome + quick start | Plugin |
| `ooo interview` | Socratic requirement clarification | Plugin |
| `ooo seed` | Generate validated seed spec | Plugin |
| `ooo run` | Execute seed workflow | MCP |
| `ooo evaluate` | 3-stage verification | MCP |
| `ooo unstuck` | 5 lateral thinking personas | Plugin |
| `ooo status` | Session status + drift check | MCP |
| `ooo setup` | Installation wizard | Plugin |
| `ooo welcome` | First-touch welcome guide | Plugin |
| `ooo tutorial` | Interactive hands-on learning | Plugin |
| `ooo help` | This reference guide | Plugin |

### Evolutionary Loop

| Command | Purpose | Mode |
|---------|---------|------|
| `ooo evolve` | Start/monitor evolutionary development loop | MCP |
| `ooo ralph` | Self-referential loop until verified ("don't stop") | Plugin + MCP |

**Plugin** = Works immediately after `ooo setup`.
**MCP** = Requires `ooo setup` (Python 3.14+ auto-detected). Run setup once to unlock all features.

## Natural Language Triggers

| Phrase | Triggers |
|--------|----------|
| "interview me", "clarify requirements", "socratic interview" | `ooo interview` |
| "crystallize", "generate seed", "create seed", "freeze requirements" | `ooo seed` |
| "ouroboros run", "execute seed", "run seed", "run workflow" | `ooo run` |
| "evaluate this", "3-stage check", "verify execution" | `ooo evaluate` |
| "think sideways", "i'm stuck", "break through", "lateral thinking" | `ooo unstuck` |
| "am I drifting?", "drift check", "session status" | `ooo status` |

### Loop Triggers

| Phrase | Triggers |
|--------|----------|
| "ralph", "don't stop", "must complete", "until it works", "keep going" | `ooo ralph` |
| "evolve", "evolutionary loop", "iterate until converged" | `ooo evolve` |

## Available Skills

### Core Skills

| Skill | Purpose | Mode |
|-------|---------|------|
| `/ouroboros:welcome` | First-touch welcome experience | Plugin |
| `/ouroboros:interview` | Socratic requirement clarification | Plugin |
| `/ouroboros:seed` | Generate validated seed spec | Plugin |
| `/ouroboros:run` | Execute seed workflow | MCP |
| `/ouroboros:evaluate` | 3-stage verification | MCP |
| `/ouroboros:unstuck` | 5 lateral thinking personas | Plugin |
| `/ouroboros:status` | Session status + drift check | MCP |
| `/ouroboros:setup` | Installation wizard | Plugin |
| `/ouroboros:tutorial` | Interactive hands-on learning | Plugin |
| `/ouroboros:help` | This guide | Plugin |

### Loop Skills

| Skill | Purpose | Best For |
|-------|---------|----------|
| `/ouroboros:ralph` | Self-referential loop until verified | "Don't stop", must complete |
| `/ouroboros:evolve` | Evolutionary ontology refinement | Spec iteration until convergence |

## Available Agents

| Agent | Purpose |
|-------|---------|
| `ouroboros:socratic-interviewer` | Exposes hidden assumptions through questioning |
| `ouroboros:ontologist` | Finds root problems vs symptoms |
| `ouroboros:seed-architect` | Crystallizes requirements into seed specs |
| `ouroboros:evaluator` | Three-stage verification |
| `ouroboros:contrarian` | "Are we solving the wrong problem?" |
| `ouroboros:hacker` | "Make it work first, elegance later" |
| `ouroboros:simplifier` | "Cut scope to absolute minimum" |
| `ouroboros:researcher` | "Stop coding, start investigating" |
| `ouroboros:architect` | "Question the foundation, redesign if needed" |

## Setup

After installing the plugin, run `ooo setup` once to register the MCP server.
This connects Claude Code to the Ouroboros Python core and unlocks all features.

```
ooo setup    # One-time setup (~1 minute)
```
