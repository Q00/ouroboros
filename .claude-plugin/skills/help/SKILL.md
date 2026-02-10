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
| `ooo help` | This reference guide | Plugin |

### Execution Modes

| Command | Purpose | Parallelism |
|---------|---------|-------------|
| `ooo autopilot` | Full autonomous execution with verification | Sequential |
| `ooo ultrawork` | Maximum parallelism for independent tasks | Parallel (tasks) |
| `ooo ralph` | Self-referential loop until verified ("don't stop") | Parallel + loop |
| `ooo ultrapilot` | Parallel autopilot with file partitioning | Parallel (files) |
| `ooo ecomode` | Token-efficient execution (haiku/sonnet only) | Sequential |
| `ooo swarm` | Coordinated multi-agent team | Parallel (agents) |
| `ooo pipeline` | Sequential agent chaining with data passing | Sequential (stages) |

**Plugin** = Works immediately, no setup needed.
**MCP** = Requires Python 3.14+ and `ooo setup` for MCP server registration.

## Natural Language Triggers

| Phrase | Triggers |
|--------|----------|
| "interview me", "clarify requirements", "socratic interview" | `ooo interview` |
| "crystallize", "generate seed", "create seed", "freeze requirements" | `ooo seed` |
| "ouroboros run", "execute seed", "run seed", "run workflow" | `ooo run` |
| "evaluate this", "3-stage check", "verify execution" | `ooo evaluate` |
| "think sideways", "i'm stuck", "break through", "lateral thinking" | `ooo unstuck` |
| "am I drifting?", "drift check", "session status" | `ooo status` |

### Execution Mode Triggers

| Phrase | Triggers |
|--------|----------|
| "autopilot", "build me", "I want a", "make this", "create this for me" | `ooo autopilot` |
| "ultrawork", "ulw", "maximum parallelism", "parallel everything" | `ooo ultrawork` |
| "ralph", "don't stop", "must complete", "until it works", "keep going" | `ooo ralph` |
| "ultrapilot", "parallel build", "parallel autopilot" | `ooo ultrapilot` |
| "ecomode", "eco", "budget", "cheap mode", "token efficient" | `ooo ecomode` |
| "swarm", "team", "coordinated", "multi-agent" | `ooo swarm` |
| "pipeline", "chain agents", "sequential", "step by step" | `ooo pipeline` |

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
| `/ouroboros:help` | This guide | Plugin |

### Execution Mode Skills

| Skill | Purpose | Best For |
|-------|---------|----------|
| `/ouroboros:autopilot` | Autonomous execution with verification | Most tasks, "just do it" |
| `/ouroboros:ultrawork` | Maximum parallelism for independent tasks | Multiple independent ACs |
| `/ouroboros:ralph` | Self-referential loop until verified | "Don't stop", must complete |
| `/ouroboros:ultrapilot` | Parallel autopilot with file partitioning | Multi-file features |
| `/ouroboros:ecomode` | Token-efficient (haiku/sonnet only) | Budget-conscious, simple tasks |
| `/ouroboros:swarm` | Coordinated multi-agent team | Complex, multi-domain work |
| `/ouroboros:pipeline` | Sequential agent chaining | Clear handoff, audit trail |

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

## Plugin Modes

- **Plugin Mode**: Skills + Agents work without Python (install only plugin)
- **Full Mode (MCP)**: MCP server connects to Python core (run `ooo setup`)
