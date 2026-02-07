# Common Workflow Scenarios

Practical recipes for typical Ouroboros use cases.

## 1. Code Generation: Python Library

Generate a complete Python library from scratch.

```bash
# Step 1: Interview
uv run ouroboros init "Build a Python library for parsing and validating YAML configurations"

# Step 2: Execute
uv run ouroboros run ~/.ouroboros/seeds/latest.yaml

# Step 3: Monitor (separate terminal)
uv run ouroboros tui monitor
```

**Seed template**:
```yaml
goal: "Build a Python library for parsing and validating YAML configurations"
task_type: code
constraints:
  - "Python 3.14+"
  - "PyYAML as only external dependency"
  - "Type hints throughout"
acceptance_criteria:
  - "Parse YAML files into typed Python dataclasses"
  - "Validate field types and required fields with clear error messages"
  - "Support nested configuration with dot-notation access"
  - "Write pytest tests with >90% coverage"
ontology_schema:
  name: "ConfigParser"
  description: "YAML configuration parsing library"
  fields:
    - name: "config_node"
      field_type: "entity"
      description: "A configuration node (scalar, mapping, or sequence)"
metadata:
  seed_id: "config_parser_001"
  ambiguity_score: 0.1
```

## 2. Research: Technology Evaluation

Generate a structured research document.

```yaml
goal: "Research container orchestration options for a startup with 5-10 microservices"
task_type: research
constraints:
  - "Budget under $500/month for infrastructure"
  - "Team has Python/Go experience but limited DevOps"
  - "Must support auto-scaling"
acceptance_criteria:
  - "Compare Kubernetes (EKS/GKE), Docker Swarm, and Nomad across 8+ criteria"
  - "Include cost projections for 5, 10, and 20 service scenarios"
  - "Document learning curve and operational overhead for each"
  - "Provide a decision matrix with weighted scoring"
  - "Write a final recommendation with migration steps"
ontology_schema:
  name: "ContainerOrchestration"
  description: "Container orchestration technology comparison"
  fields:
    - name: "platform"
      field_type: "entity"
      description: "A container orchestration platform"
    - name: "criterion"
      field_type: "entity"
      description: "An evaluation criterion"
metadata:
  seed_id: "container_orch_001"
  ambiguity_score: 0.15
```

## 3. Analysis: Codebase Refactoring Plan

Analyze existing code and produce a refactoring plan.

```yaml
goal: "Analyze the current monolith and produce a refactoring plan to extract payment processing into a separate module"
task_type: analysis
constraints:
  - "Zero downtime during migration"
  - "Preserve all existing API contracts"
  - "Maximum 2 sprint migration window"
acceptance_criteria:
  - "Map all payment-related code paths and dependencies"
  - "Identify the minimum viable extraction boundary"
  - "Document required interface changes with before/after examples"
  - "Produce a risk matrix with mitigation strategies"
  - "Create a phased migration plan with rollback procedures"
ontology_schema:
  name: "RefactoringPlan"
  description: "Codebase refactoring analysis"
  fields:
    - name: "component"
      field_type: "entity"
      description: "A code module or service"
    - name: "dependency"
      field_type: "relation"
      description: "Coupling between components"
metadata:
  seed_id: "payment_refactor_001"
  ambiguity_score: 0.12
```

## 4. Using External MCP Tools

Connect Ouroboros to external tool servers for enhanced capabilities.

### Setup MCP Config

Create `mcp.yaml`:

```yaml
mcp_servers:
  - name: "filesystem"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@anthropic/mcp-server-filesystem", "/workspace"]

  - name: "github"
    transport: "stdio"
    command: "npx"
    args: ["-y", "@anthropic/mcp-server-github"]
    env:
      GITHUB_TOKEN: "${GITHUB_TOKEN}"

connection:
  timeout_seconds: 30
  retry_attempts: 3
```

### Execute with MCP

```bash
# Set credentials
export GITHUB_TOKEN="ghp_..."

# Run with MCP tools
uv run ouroboros run --mcp-config mcp.yaml seed.yaml

# With tool prefix to avoid name conflicts
uv run ouroboros run --mcp-config mcp.yaml --mcp-tool-prefix "ext_" seed.yaml
```

**Tool precedence**: Built-in tools (Read, Write, Edit, Bash, Glob, Grep) always win over MCP tools with the same name.

## 5. Resuming Failed Workflows

When a workflow fails or is interrupted:

```bash
# Check what happened
uv run ouroboros status executions
uv run ouroboros status execution exec_abc123 --events

# Resume from where it stopped
uv run ouroboros run seed.yaml --resume orch_abc123
```

The orchestrator resumes from the last checkpoint, skipping completed ACs.

## 6. Dry Run Validation

Validate a seed file without executing:

```bash
uv run ouroboros run seed.yaml --dry-run
```

This checks:
- YAML syntax and schema compliance
- Required fields presence
- Field value ranges (ambiguity_score, weights)
- Ontology schema validity

## 7. Debug Mode

When things go wrong, enable verbose output:

```bash
# CLI debug flag
uv run ouroboros run seed.yaml --debug

# Or via environment variable
OUROBOROS_LOG_LEVEL=DEBUG uv run ouroboros run seed.yaml
```

Debug mode shows:
- Agent thinking and reasoning
- Tool call inputs and outputs
- Model tier selection decisions
- Evaluation scores and verdicts

## 8. Parallel vs Sequential Execution

By default, independent ACs execute in parallel. To force sequential:

```bash
# Default: parallel execution
uv run ouroboros run seed.yaml

# Force sequential
uv run ouroboros run seed.yaml --sequential
```

**When to use sequential**:
- All ACs have strict ordering dependencies
- Debugging execution order issues
- Comparing parallel vs sequential results

**Parallel execution features**:
- Automatic dependency analysis between ACs
- Level-based scheduling (all ACs in a level run concurrently)
- Inter-level context passing (results from level N inform level N+1)
- Conflict detection for shared file modifications

## 9. Exposing Ouroboros as MCP Server

Let other AI tools (like Claude Desktop) use Ouroboros:

```bash
# Start MCP server (stdio for Claude Desktop)
uv run ouroboros mcp serve

# Or with SSE transport for HTTP clients
uv run ouroboros mcp serve --transport sse --port 9000
```

Add to Claude Desktop config (`~/.config/claude/config.json`):

```json
{
  "mcpServers": {
    "ouroboros": {
      "command": "ouroboros",
      "args": ["mcp", "serve"]
    }
  }
}
```

Available MCP tools:
- `ouroboros_execute_seed` -- execute a seed specification
- `ouroboros_session_status` -- check session status
- `ouroboros_query_events` -- query event history
