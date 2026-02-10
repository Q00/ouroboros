# Ouroboros Claude Code Plugin

> Self-improving AI workflow system. Crystallize requirements before execution with Socratic interview, ambiguity scoring, and 3-stage evaluation.

## Overview

Ouroboros transforms vague ideas into validated, executable specifications through a systematic process:

1. **Big Bang Interview** - Socratic questioning exposes hidden assumptions
2. **Seed Generation** - Immutable specification with ontology schema
3. **PAL Routing** - Complexity-based model escalation
4. **Lateral Thinking** - 5 personas break stagnation
5. **3-Stage Evaluation** - Mechanical → Semantic → Consensus

## Installation

```bash
# Option 1: Clone directly to plugins directory
git clone https://github.com/Q00/ouroboros.git ~/.claude/plugins/ouroboros

# Option 2: Copy the .claude-plugin directory
cp -r .claude-plugin ~/.claude/plugins/ouroboros
```

## Quick Start

```
ooo interview "Build a CLI task manager"
```

Answer the clarifying questions, then:

```
ooo seed
```

That's it — you have a validated Seed specification.

For execution, evaluation, and drift tracking (requires Python 3.14+):

```
ooo setup
ooo run
ooo evaluate
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Claude Code Plugin                        │
│                                                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Skills    │  │   Agents    │  │      Hooks          │  │
│  │  (9 SKILL)  │  │  (7 .md)    │  │  (keyword detection) │  │
│  └──────┬──────┘  └──────┬──────┘  └──────────┬──────────┘  │
└─────────┼────────────────┼────────────────────┼─────────────┘
          │                │                    │
          │    (Optional)  │                    │
          └────────────────┼────────────────────┘
                           │
                  ┌────────▼────────┐
                  │   MCP Server    │
                  │  (FastMCP)      │
                  └────────┬────────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
    ┌─────▼──────┐  ┌─────▼──────┐  ┌─────▼──────┐
    │ Interview   │  │    Seed    │  │ Execution   │
    │   Engine    │  │  Generator │  │  Pipeline   │
    └─────────────┘  └─────────────┘  └─────────────┘
```

## Plugin Modes

| Mode | Requirements | Features |
|------|-------------|----------|
| **Plugin Mode** | None | Skills + Agents (prompt-based) |
| **Full Mode (MCP)** | Python 3.14+ | + MCP server + Python core |

## Skills Reference

| Skill | Description | Mode |
|-------|-------------|------|
| `/ouroboros:welcome` | First-touch welcome experience | Plugin |
| `/ouroboros:interview` | Socratic Q&A for requirements | Plugin |
| `/ouroboros:seed` | Generate Seed YAML | Plugin |
| `/ouroboros:run` | Execute workflow | MCP |
| `/ouroboros:evaluate` | 3-stage verification | MCP |
| `/ouroboros:unstuck` | Lateral thinking personas | Plugin |
| `/ouroboros:status` | Drift measurement | MCP |
| `/ouroboros:setup` | Installation wizard | Plugin |
| `/ouroboros:help` | Full reference guide | Plugin |

## Agents Reference

| Agent | Purpose |
|-------|---------|
| `ouroboros:socratic-interviewer` | Exposes hidden assumptions |
| `ouroboros:ontologist` | Root cause analysis |
| `ouroboros:seed-architect` | Seed spec generation |
| `ouroboros:evaluator` | 3-stage evaluation |
| `ouroboros:contrarian` | "Wrong problem?" persona |
| `ouroboros:hacker` | "Make it work" persona |
| `ouroboros:simplifier` | "Cut scope" persona |
| `ouroboros:researcher` | "Stop coding, investigate" persona |
| `ouroboros:architect` | "Redesign the structure" persona |

## Magic Keywords

All commands use the `ooo` prefix:

| Command | Natural Language Alternatives |
|---------|------------------------------|
| `ooo` | Welcome + quick start |
| `ooo interview` | "interview me", "clarify requirements" |
| `ooo seed` | "crystallize", "generate seed" |
| `ooo run` | "ouroboros run", "execute seed" |
| `ooo evaluate` | "evaluate this", "3-stage check" |
| `ooo unstuck` | "think sideways", "i'm stuck" |
| `ooo status` | "am I drifting?", "drift check" |

## License

MIT © Q00

## Repository

https://github.com/Q00/ouroboros
