# Ouroboros Claude Code Plugin

> Self-improving AI workflow system. Crystallize requirements before execution with Socratic interview, ambiguity scoring, and 3-stage evaluation.

## Overview

Ouroboros transforms vague ideas into validated, executable specifications through a systematic process:

1. **Big Bang Interview** - Socratic questioning exposes hidden assumptions
2. **Seed Generation** - Immutable specification with ontology schema
3. **PAL Routing** - Complexity-based model escalation (Phase 2)
4. **Lateral Thinking** - 5 personas break stagnation (Phase 3)
5. **3-Stage Evaluation** - Mechanical → Semantic → Consensus (Phase 2)

## Installation

```bash
# Option 1: Clone directly to plugins directory
git clone https://github.com/Q00/ouroboros.git ~/.claude/plugins/ouroboros

# Option 2: Copy the .claude-plugin directory
cp -r .claude-plugin ~/.claude/plugins/ouroboros
```

## Quick Start

### Phase 1 (MVP - Available Now)

```bash
# 1. Start an interview
/ouroboros:interview "Build a CLI task manager"

# 2. Answer clarifying questions
# The interviewer will ask about constraints, features, data structures...

# 3. Generate the seed spec
/ouroboros:seed

# Output: Validated Seed YAML with ontology schema
```

### Phase 2 (MCP Bridge - Planned)

Requires Python 3.14+ and MCP server:

```bash
# 4. Execute the workflow
/ouroboros:run seed.yaml

# Features:
# - PAL Router: Auto-selects model by complexity
# - Double Diamond: Discover → Define → Design → Deliver
# - Event Sourcing: SQLite immutable event store
# - Session Recovery: Resume interrupted workflows
```

### Phase 3 (Growth - Planned)

```bash
# Setup wizard with environment detection
/ouroboros:setup

# Lateral thinking when stuck
/ouroboros:unstuck

# Drift measurement
/ouroboros:status
```

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Claude Code Plugin                        │
│                                                               │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐  │
│  │   Skills    │  │   Agents    │  │      Hooks          │  │
│  │  (8 SKILL)  │  │  (7 .md)    │  │  (keyword detection) │  │
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
| **Plugin-Only** | None | Skills + Agents (prompt-based) |
| **Full Mode** | Python 3.14+ | + MCP server + Python core |

## Features

### ✅ Phase 1: MVP (Current)
- Socratic interview for requirement clarification
- Seed generation with ontology schema
- Agent-based prompts (no Python required)

### 🔜 Phase 2: MCP Bridge (Planned)
- MCP server (`uvx ouroboros-ai mcp serve`)
- Seed execution with PAL Router
- 3-stage evaluation pipeline
- Drift measurement
- Session recovery

### 🔜 Phase 3: Growth (Planned)
- Setup wizard with environment detection
- 5 lateral thinking personas
- Magic keyword hooks
- Star solicitation (Hybrid Option D)

## Skills Reference

| Skill | Description | Phase |
|-------|-------------|-------|
| `/ouroboros:interview` | Socratic Q&A for requirements | 1 ✅ |
| `/ouroboros:seed` | Generate Seed YAML | 1 ✅ |
| `/ouroboros:run` | Execute workflow | 2 🔜 |
| `/ouroboros:evaluate` | 3-stage verification | 2 🔜 |
| `/ouroboros:unstuck` | Lateral thinking personas | 3 🔜 |
| `/ouroboros:status` | Drift measurement | 2 🔜 |
| `/ouroboros:setup` | Installation wizard | 3 🔜 |
| `/ouroboros:help` | Show this guide | 1 ✅ |

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

## Magic Keywords

- `"interview me"` → Start interview
- `"crystallize"` → Generate seed
- `"think sideways"` → Lateral thinking
- `"am I drifting?"` → Drift check
- `"evaluate this"` → 3-stage evaluation

## License

MIT © Q00

## Repository

https://github.com/Q00/ouroboros
