# /ouroboros:help

Display Ouroboros usage guide and available features.

## Usage

```
/ouroboros:help
```

## What Is Ouroboros?

Ouroboros is a **requirement crystallization engine** for AI workflows. It transforms vague ideas into validated specifications through:

1. **Socratic Interview** - Exposes hidden assumptions
2. **Seed Generation** - Creates immutable specifications
3. **PAL Routing** - Auto-escalates/descends model complexity
4. **Lateral Thinking** - 5 personas to break stagnation
5. **3-Stage Evaluation** - Mechanical → Semantic → Consensus

## Available Skills

| Skill | Purpose | Status |
|-------|---------|--------|
| `/ouroboros:interview` | Socratic requirement clarification | ✅ |
| `/ouroboros:seed` | Generate validated seed spec | ✅ |
| `/ouroboros:run` | Execute seed workflow (MCP) | ✅ |
| `/ouroboros:evaluate` | 3-stage verification (MCP) | ✅ |
| `/ouroboros:unstuck` | 5 lateral thinking personas | ✅ |
| `/ouroboros:status` | Session status + drift check (MCP) | ✅ |
| `/ouroboros:setup` | Installation wizard | ✅ |
| `/ouroboros:help` | This guide | ✅ |

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

## Magic Keywords

| Keyword | Triggers |
|---------|----------|
| "interview me" | Start `/ouroboros:interview` |
| "crystallize" | Generate `/ouroboros:seed` |
| "think sideways" | Lateral thinking personas |
| "am I drifting?" | Drift measurement check |
| "evaluate this" | 3-stage evaluation |

## Quick Start Example

```
1. /ouroboros:setup                        (first time only)
2. /ouroboros:interview "Build a REST API"
3. [Answer clarifying questions...]
4. /ouroboros:seed
5. /ouroboros:run
6. /ouroboros:evaluate <session_id>
```

## Plugin Modes

- **Plugin-Only**: Skills + Agents work without Python (install only plugin)
- **Full Mode**: MCP server connects to Python core (run `/ouroboros:setup`)
