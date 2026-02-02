# API Reference

This section provides detailed API documentation for the Ouroboros Python library.

## Modules

### Core Module

The [Core Module](./core.md) provides foundational types and utilities:

- **Result** - Generic type for handling expected failures
- **Seed** - Immutable workflow specification
- **Error Hierarchy** - Structured exception types
- **Type Aliases** - Common domain types

```python
from ouroboros.core import Result, Seed, OuroborosError
```

### MCP Module

The [MCP Module](./mcp.md) provides Model Context Protocol integration:

- **MCPClient** - Connect to external MCP servers
- **MCPServer** - Expose Ouroboros as an MCP server
- **ToolRegistry** - Manage MCP tools
- **Error Types** - MCP-specific exceptions

```python
from ouroboros.mcp import MCPClientAdapter, MCPServerAdapter, MCPError
```

## Quick Reference

### Core Types

| Type | Description | Import |
|------|-------------|--------|
| `Result[T, E]` | Success/failure container | `from ouroboros.core import Result` |
| `Seed` | Immutable workflow spec | `from ouroboros.core import Seed` |
| `OuroborosError` | Base exception | `from ouroboros.core import OuroborosError` |

### MCP Types

| Type | Description | Import |
|------|-------------|--------|
| `MCPClientAdapter` | MCP client implementation | `from ouroboros.mcp.client import MCPClientAdapter` |
| `MCPServerAdapter` | MCP server implementation | `from ouroboros.mcp.server import MCPServerAdapter` |
| `MCPToolDefinition` | Tool definition | `from ouroboros.mcp import MCPToolDefinition` |
| `MCPError` | Base MCP exception | `from ouroboros.mcp import MCPError` |

### Orchestrator Types

| Type | Description | Import |
|------|-------------|--------|
| `ClaudeAgentAdapter` | Claude SDK wrapper | `from ouroboros.orchestrator import ClaudeAgentAdapter` |
| `OrchestratorRunner` | Main execution runner | `from ouroboros.orchestrator import OrchestratorRunner` |
| `SessionTracker` | Session state tracking | `from ouroboros.orchestrator import SessionTracker` |

## Common Patterns

### Using Result Type

```python
from ouroboros.core import Result

def divide(a: int, b: int) -> Result[float, str]:
    if b == 0:
        return Result.err("division by zero")
    return Result.ok(a / b)

result = divide(10, 2)
if result.is_ok:
    print(f"Result: {result.value}")
else:
    print(f"Error: {result.error}")
```

### Creating a Seed

```python
from ouroboros.core import (
    Seed,
    SeedMetadata,
    OntologySchema,
    OntologyField,
    EvaluationPrinciple,
    ExitCondition,
)

seed = Seed(
    goal="Build a task management CLI",
    constraints=("Python 3.14+", "SQLite storage"),
    acceptance_criteria=(
        "Tasks can be created",
        "Tasks can be listed",
        "Tasks can be completed",
    ),
    ontology_schema=OntologySchema(
        name="TaskManager",
        description="Task management domain",
        fields=(
            OntologyField(
                name="tasks",
                field_type="array",
                description="List of tasks",
            ),
        ),
    ),
    evaluation_principles=(
        EvaluationPrinciple(
            name="completeness",
            description="All requirements implemented",
            weight=1.0,
        ),
    ),
    exit_conditions=(
        ExitCondition(
            name="all_criteria_met",
            description="All acceptance criteria pass",
            evaluation_criteria="100% pass rate",
        ),
    ),
    metadata=SeedMetadata(ambiguity_score=0.15),
)
```

### Connecting to MCP Server

```python
import asyncio
from ouroboros.mcp import MCPServerConfig, TransportType
from ouroboros.mcp.client import MCPClientAdapter

async def main():
    config = MCPServerConfig(
        name="my-server",
        transport=TransportType.STDIO,
        command="my-mcp-server",
    )

    async with MCPClientAdapter() as client:
        result = await client.connect(config)
        if result.is_ok:
            tools = await client.list_tools()
            print(f"Available tools: {tools.value}")

asyncio.run(main())
```

### Running Orchestrator

```python
import asyncio
from ouroboros.orchestrator import ClaudeAgentAdapter, OrchestratorRunner
from ouroboros.persistence.event_store import EventStore

async def main():
    adapter = ClaudeAgentAdapter()
    event_store = EventStore()
    await event_store.initialize()

    runner = OrchestratorRunner(adapter, event_store)

    # Load seed from file or create programmatically
    seed = Seed.from_dict(load_seed_yaml())

    result = await runner.execute_seed(seed)
    if result.is_ok:
        print(f"Execution complete: {result.value.session_id}")

asyncio.run(main())
```

## See Also

- [Core Module API](./core.md) - Detailed Result, Seed, and error documentation
- [MCP Module API](./mcp.md) - Detailed MCP client/server documentation
