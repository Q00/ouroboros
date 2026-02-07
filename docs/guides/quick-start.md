# Quick Start Guide

Get Ouroboros running and execute your first AI workflow in under 10 minutes.

## Prerequisites

- Python 3.14+
- [uv](https://github.com/astral-sh/uv) package manager
- An LLM API key (Anthropic, OpenAI, or any [LiteLLM-supported provider](https://docs.litellm.ai/docs/providers))

## 1. Install

```bash
git clone https://github.com/Q00/ouroboros
cd ouroboros
uv sync
uv run ouroboros --version
```

## 2. Configure

Set your API key:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
# or
export OPENAI_API_KEY="sk-..."
```

Initialize default config:

```bash
uv run ouroboros config init
```

This creates `~/.ouroboros/config.yaml` with sensible defaults. See [CLI Usage](./cli-usage.md) for all config options.

## 3. Create a Seed (Big Bang Interview)

The Seed is Ouroboros's "constitution" -- an immutable spec that drives all execution and evaluation. You generate one through an interactive interview:

```bash
uv run ouroboros init "Build a CLI task management tool with SQLite storage"
```

Ouroboros asks Socratic questions to eliminate ambiguity:

```
Q1: What operations should the task manager support beyond basic CRUD?
> Users need to assign priorities (low/medium/high) and filter by status.

Q2: Should the CLI support multiple users or is it single-user?
> Single user only. No authentication needed.

Ambiguity score: 0.18 (threshold: 0.20) -- Interview complete!
Seed generated: ~/.ouroboros/seeds/seed_a1b2c3d4e5f6.yaml
```

The interview continues until ambiguity drops to 0.2 or below.

## 4. Review the Seed

```yaml
# seed.yaml
goal: "Build a single-user CLI task manager with SQLite storage"
task_type: code    # "code", "research", or "analysis"
constraints:
  - "Python 3.14+"
  - "SQLite for persistence"
  - "No external dependencies beyond stdlib"
acceptance_criteria:
  - "Create tasks with title, description, priority, and due date"
  - "List tasks with filtering by status and priority"
  - "Mark tasks as complete"
  - "Delete tasks"
ontology_schema:
  name: "TaskManager"
  description: "Task management domain"
  fields:
    - name: "tasks"
      field_type: "array"
      description: "Collection of task entities"
metadata:
  ambiguity_score: 0.18
  seed_id: "seed_a1b2c3d4e5f6"
```

See [Seed Authoring Guide](./seed-authoring.md) for the complete YAML schema.

## 5. Execute the Workflow

```bash
uv run ouroboros run seed.yaml
```

Ouroboros runs the six-phase pipeline:

1. **PAL Router** -- selects cost-effective model tier per task complexity
2. **Double Diamond** -- decomposes ACs, executes via Claude Agent SDK
3. **Resilience** -- detects stagnation, switches personas if stuck
4. **Evaluation** -- mechanical checks, semantic evaluation, consensus (if triggered)

### Parallel Execution (Default)

ACs without dependencies execute in parallel:

```bash
uv run ouroboros run seed.yaml
```

### Sequential Execution

Force one-at-a-time AC execution:

```bash
uv run ouroboros run seed.yaml --sequential
```

### With External MCP Tools

```bash
uv run ouroboros run seed.yaml --mcp-config mcp.yaml
```

## 6. Monitor with TUI

In a separate terminal, launch the interactive monitor:

```bash
uv run ouroboros monitor
```

See [TUI Guide](./tui-usage.md) for dashboard details.

## 7. Check Results

```bash
# List recent executions
uv run ouroboros status executions

# Inspect a specific execution
uv run ouroboros status execution exec_abc123 --events
```

## Resuming Interrupted Work

```bash
# Resume an interview
uv run ouroboros init --resume interview_20260125_120000

# Resume an orchestrator session
uv run ouroboros run seed.yaml --resume orch_abc123
```

## What's Next

- [Seed Authoring Guide](./seed-authoring.md) -- write seeds from scratch
- [TUI Usage Guide](./tui-usage.md) -- master the interactive dashboard
- [Common Workflows](./common-workflows.md) -- recipes for typical scenarios
- [Architecture Overview](../architecture.md) -- understand the six-phase system
