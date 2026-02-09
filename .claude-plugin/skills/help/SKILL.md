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
3. **PAL Routing** - Auto-escalates/descends model complexity (Phase 2)
4. **Lateral Thinking** - 5 personas to break stagnation (Phase 3)
5. **3-Stage Evaluation** - Mechanical → Semantic → Consensus (Phase 2)

## Available Skills

| Skill | Purpose | Phase |
|-------|---------|-------|
| `/ouroboros:interview` | Socratic requirement clarification | 1 ✅ |
| `/ouroboros:seed` | Generate validated seed spec | 1 ✅ |
| `/ouroboros:run` | Execute seed workflow | 2 🔜 |
| `/ouroboros:evaluate` | 3-stage verification | 2 🔜 |
| `/ouroboros:unstuck` | 5 lateral thinking personas | 3 🔜 |
| `/ouroboros:status` | Drift measurement check | 2 🔜 |
| `/ouroboros:setup` | Installation wizard | 3 🔜 |
| `/ouroboros:help` | This guide | 1 ✅ |

## Available Agents

| Agent | Purpose |
|-------|---------|
| `ouroboros:socratic-interviewer` | Exposes hidden assumptions through questioning |
| `ouroboros:ontologist` | Finds root problems vs symptoms |
| `ouroboros:seed-architect` | Crystallizes requirements into seed specs |

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
1. /ouroboros:interview "Build a REST API"
2. [Answer clarifying questions...]
3. /ouroboros:seed
4. /ouroboros:run  (Phase 2 - requires MCP server)
```

## Plugin Modes

- **Plugin-Only**: Skills + Agents work without Python (current)
- **Full Mode**: MCP server connects to Python core (Phase 2)
