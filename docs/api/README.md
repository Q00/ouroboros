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

## See Also

- [Getting Started](../getting-started.md) - Install and onboarding guide
