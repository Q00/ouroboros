# Getting Started with Ouroboros

This guide will help you install Ouroboros and run your first self-improving AI workflow.

## Prerequisites

- Python 3.14 or higher
- [uv](https://github.com/astral-sh/uv) package manager (recommended)
- An LLM API key (OpenAI, Anthropic, or other supported providers)

## Installation

### Using uv (Recommended)

```bash
# Clone the repository
git clone https://github.com/Q00/ouroboros
cd ouroboros

# Install dependencies
uv sync

# Verify installation
uv run ouroboros --version
```

### Using pip

```bash
pip install ouroboros
```

## Configuration

### Environment Variables

Set up your LLM provider credentials:

```bash
# For Anthropic Claude
export ANTHROPIC_API_KEY="your-api-key"

# For OpenAI
export OPENAI_API_KEY="your-api-key"

# For other providers, see LiteLLM documentation
```

### Configuration File

Ouroboros uses a configuration file at `~/.ouroboros/config.yaml`:

```yaml
# Default LLM provider settings
providers:
  default: anthropic/claude-3-5-sonnet
  frugal: anthropic/claude-3-haiku
  standard: anthropic/claude-3-5-sonnet
  frontier: anthropic/claude-3-opus

# Database settings
database:
  path: ~/.ouroboros/ouroboros.db

# Logging
logging:
  level: INFO
  format: json
```

Initialize the configuration:

```bash
uv run ouroboros config init
```

## Quick Start

### Step 1: Start an Interview (Big Bang Phase)

The Big Bang phase transforms your vague idea into a clear specification through Socratic questioning:

```bash
uv run ouroboros init "I want to build a task management CLI tool"
```

This starts an interactive interview session where Ouroboros will ask clarifying questions to:
- Reveal hidden assumptions
- Expose contradictions
- Find the root problem
- Define acceptance criteria

The interview continues until the ambiguity score drops to 0.2 or below.

### Step 2: Review the Generated Seed

After the interview completes, Ouroboros generates a **Seed** - an immutable specification that serves as the "constitution" for your workflow:

```yaml
# seed.yaml
goal: "Build a CLI task management tool with SQLite storage"
constraints:
  - "Python 3.14+"
  - "No external database dependencies"
  - "Must work offline"
acceptance_criteria:
  - "Users can create tasks with title and due date"
  - "Users can list all tasks"
  - "Users can mark tasks as complete"
  - "Users can delete tasks"
ontology_schema:
  name: "TaskManager"
  description: "Task management domain model"
  fields:
    - name: "tasks"
      field_type: "array"
      description: "List of task objects"
metadata:
  ambiguity_score: 0.15
  seed_id: "seed_abc123"
```

### Step 3: Execute the Workflow

Run the workflow using the orchestrator mode (Claude Agent SDK):

```bash
uv run ouroboros run workflow --orchestrator seed.yaml
```

This executes the full Ouroboros pipeline:
1. **PAL Router** selects the appropriate model tier based on task complexity
2. **Double Diamond** decomposes and executes tasks
3. **Resilience** handles stagnation with lateral thinking
4. **Evaluation** verifies outputs at each stage

### Step 4: Check Status

Monitor execution progress:

```bash
uv run ouroboros status health
uv run ouroboros status executions
```

## Example: Complete Workflow

Here's a complete example from start to finish:

```bash
# 1. Initialize configuration
uv run ouroboros config init

# 2. Start interview
uv run ouroboros init "Build a REST API for a todo application"

# 3. (Interactive: Answer questions until ambiguity <= 0.2)

# 4. Execute the generated seed
uv run ouroboros run workflow --orchestrator ~/.ouroboros/seeds/latest.yaml

# 5. Monitor progress
uv run ouroboros status executions --limit 1
```

## Resuming Sessions

If an interview or execution is interrupted, you can resume:

```bash
# Resume an interview
uv run ouroboros init --resume interview_20260125_120000

# Resume an orchestrator session
uv run ouroboros run workflow --orchestrator --resume orch_abc123 seed.yaml
```

## Next Steps

- Read the [Architecture Overview](./architecture.md) to understand the six phases
- Explore the [CLI Usage Guide](./guides/cli-usage.md) for all commands
- Check the [API Reference](./api/README.md) for programmatic usage

## Troubleshooting

### Common Issues

**"No API key found"**
- Ensure your LLM provider API key is set in environment variables
- Check that the key has the correct permissions

**"Ambiguity score not decreasing"**
- Provide more specific answers to interview questions
- Consider breaking down your idea into smaller components

**"Execution stalled"**
- Ouroboros will automatically detect stagnation and switch personas
- If it persists, check the logs: `uv run ouroboros status execution <id> --events`

### Getting Help

- Check [GitHub Issues](https://github.com/Q00/ouroboros/issues)
- Review the [API documentation](./api/README.md)
