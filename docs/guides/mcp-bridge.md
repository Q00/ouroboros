# MCP Bridge — Server-to-Server Communication

## Overview

The MCP Bridge module enables an MCP server (like Ouroboros) to connect to and
consume tools from other MCP servers during execution. This solves the problem
where `execute_seed` runs in isolation without access to external MCP tools.

## Architecture

```
Claude Session (Host)
    │
    └── Ouroboros MCP Server
            │
            ├── MCPBridge
            │     └── MCPClientManager
            │           ├── openchrome MCP
            │           ├── filesystem MCP
            │           └── database MCP
            │
            └── ExecuteSeedHandler
                  └── OrchestratorRunner (merged tools)
                        └── Child Agent (native + external tools)
```

## Quick Start

### 1. Create config file

Create `~/.ouroboros/mcp_servers.yaml`:

```yaml
mcp_servers:
  - name: openchrome
    transport: stdio
    command: npx
    args: ["-y", "openchrome-mcp@latest", "serve", "--auto-launch"]

connection:
  timeout_seconds: 30
  retry_attempts: 3
```

### 2. Start MCP server

```bash
ouroboros mcp serve
# Output: MCP Bridge: 1/1 upstream server(s) connected
```

### 3. Execute seed with external tools

The child agent now has access to all upstream MCP tools during execution.

## Python API

### MCPBridge

```python
from ouroboros.mcp.bridge import MCPBridge, MCPBridgeConfig

# Async context manager
async with MCPBridge.from_config_file(Path("mcp.yaml")) as bridge:
    tools = await bridge.manager.list_all_tools()

# Manual lifecycle
bridge = MCPBridge.from_config(config)
await bridge.connect()
await bridge.disconnect()
```

### Factory Functions

```python
from ouroboros.mcp.bridge import create_bridge_from_env

bridge = create_bridge_from_env()  # Auto-discovers config
if bridge:
    await bridge.connect()
```

## Config Discovery

Checked in order:
1. `$OUROBOROS_MCP_CONFIG` environment variable
2. `~/.ouroboros/mcp_servers.yaml`
3. `{cwd}/.ouroboros/mcp_servers.yaml`

## Known Limitations

- Evolution loop (`evolve_step`) does not yet pass the bridge manager
- No dynamic server addition after initial connection
