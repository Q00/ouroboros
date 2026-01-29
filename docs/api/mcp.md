# MCP Module API Reference

The MCP module (`ouroboros.mcp`) provides Model Context Protocol integration for both consuming external MCP servers and exposing Ouroboros as an MCP server.

## Import

```python
from ouroboros.mcp import (
    # Errors
    MCPError,
    MCPClientError,
    MCPServerError,
    MCPAuthError,
    MCPTimeoutError,
    MCPConnectionError,
    MCPProtocolError,
    MCPResourceNotFoundError,
    MCPToolError,
    # Types
    TransportType,
    ContentType,
    MCPServerConfig,
    MCPToolDefinition,
    MCPToolParameter,
    MCPToolResult,
    MCPContentItem,
    MCPResourceDefinition,
    MCPResourceContent,
    MCPPromptDefinition,
    MCPPromptArgument,
    MCPCapabilities,
    MCPServerInfo,
    MCPRequest,
    MCPResponse,
)

# Client
from ouroboros.mcp.client import (
    MCPClient,
    MCPClientAdapter,
    MCPClientManager,
)

# Server
from ouroboros.mcp.server import (
    MCPServer,
    ToolHandler,
    ResourceHandler,
    MCPServerAdapter,
)

# Tools
from ouroboros.mcp.tools import (
    ToolRegistry,
    OUROBOROS_TOOLS,
)

# Resources
from ouroboros.mcp.resources import (
    OUROBOROS_RESOURCES,
)
```

---

## Types

### Enum: `TransportType`

MCP transport type for server connections.

```python
class TransportType(StrEnum):
    STDIO = "stdio"
    SSE = "sse"
    STREAMABLE_HTTP = "streamable-http"
```

### Enum: `ContentType`

Type of content in an MCP response.

```python
class ContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    RESOURCE = "resource"
```

### Class: `MCPServerConfig`

Configuration for connecting to an MCP server.

```python
@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    name: str                              # Unique name for the connection
    transport: TransportType               # Transport type
    command: str | None = None             # Command for stdio transport
    args: tuple[str, ...] = ()             # Command arguments
    url: str | None = None                 # URL for SSE/HTTP transport
    env: dict[str, str] = {}               # Environment variables
    timeout: float = 30.0                  # Connection timeout (seconds)
    headers: dict[str, str] = {}           # HTTP headers for SSE/HTTP
```

#### Example

```python
# STDIO transport
config = MCPServerConfig(
    name="my-server",
    transport=TransportType.STDIO,
    command="npx",
    args=("-y", "@my/mcp-server"),
    env={"API_KEY": "xxx"},
)

# SSE transport
config = MCPServerConfig(
    name="remote-server",
    transport=TransportType.SSE,
    url="https://api.example.com/mcp",
    headers={"Authorization": "Bearer xxx"},
)
```

### Class: `MCPToolDefinition`

Definition of an MCP tool.

```python
@dataclass(frozen=True, slots=True)
class MCPToolDefinition:
    name: str                                    # Unique tool name
    description: str                             # Human-readable description
    parameters: tuple[MCPToolParameter, ...] = () # Tool parameters
    server_name: str | None = None               # Server providing this tool
```

#### Methods

##### `to_input_schema() -> dict[str, Any]`

Convert to JSON Schema for tool input.

### Class: `MCPToolParameter`

A single parameter for an MCP tool.

```python
@dataclass(frozen=True, slots=True)
class MCPToolParameter:
    name: str                           # Parameter name
    type: ToolInputType                 # JSON Schema type
    description: str = ""               # Description
    required: bool = True               # Is required
    default: Any = None                 # Default value
    enum: tuple[str, ...] | None = None # Allowed values
```

### Class: `MCPToolResult`

Result from an MCP tool invocation.

```python
@dataclass(frozen=True, slots=True)
class MCPToolResult:
    content: tuple[MCPContentItem, ...] = ()  # Content items
    is_error: bool = False                    # Was there an error
    meta: dict[str, Any] = {}                 # Metadata
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `text_content` | `str` | Concatenated text from all text items |

### Class: `MCPContentItem`

A single content item in an MCP response.

```python
@dataclass(frozen=True, slots=True)
class MCPContentItem:
    type: ContentType                  # Content type
    text: str | None = None            # Text content
    data: str | None = None            # Binary data (base64)
    mime_type: str | None = None       # MIME type
    uri: str | None = None             # Resource URI
```

### Class: `MCPResourceDefinition`

Definition of an MCP resource.

```python
@dataclass(frozen=True, slots=True)
class MCPResourceDefinition:
    uri: str                           # Resource URI
    name: str                          # Human-readable name
    description: str = ""              # Description
    mime_type: str = "text/plain"      # MIME type
```

### Class: `MCPResourceContent`

Content of an MCP resource.

```python
@dataclass(frozen=True, slots=True)
class MCPResourceContent:
    uri: str                           # Resource URI
    text: str | None = None            # Text content
    blob: str | None = None            # Binary content (base64)
    mime_type: str = "text/plain"      # MIME type
```

### Class: `MCPCapabilities`

Capabilities of an MCP server.

```python
@dataclass(frozen=True, slots=True)
class MCPCapabilities:
    tools: bool = False
    resources: bool = False
    prompts: bool = False
    logging: bool = False
```

### Class: `MCPServerInfo`

Information about an MCP server.

```python
@dataclass(frozen=True, slots=True)
class MCPServerInfo:
    name: str
    version: str = "1.0.0"
    capabilities: MCPCapabilities
    tools: tuple[MCPToolDefinition, ...]
    resources: tuple[MCPResourceDefinition, ...]
    prompts: tuple[MCPPromptDefinition, ...]
```

---

## Error Hierarchy

All MCP-specific exceptions inherit from `MCPError`, which inherits from `OuroborosError`.

```
OuroborosError
+-- MCPError (MCP base)
    +-- MCPClientError          - Client-side failures
    |   +-- MCPConnectionError  - Connection failures
    |   +-- MCPTimeoutError     - Request timeout
    |   +-- MCPProtocolError    - Protocol errors
    +-- MCPServerError          - Server-side failures
        +-- MCPAuthError        - Authentication failures
        +-- MCPResourceNotFoundError - Resource not found
        +-- MCPToolError        - Tool execution failures
```

### Class: `MCPError`

Base exception for all MCP-related errors.

```python
class MCPError(OuroborosError):
    def __init__(
        self,
        message: str,
        *,
        server_name: str | None = None,
        is_retriable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None: ...
```

| Attribute | Type | Description |
|-----------|------|-------------|
| `server_name` | `str | None` | Name of the MCP server involved |
| `is_retriable` | `bool` | Whether the operation can be retried |

### Class: `MCPConnectionError`

Failed to connect to an MCP server. Typically retriable.

```python
class MCPConnectionError(MCPClientError):
    transport: str | None  # Transport type
```

### Class: `MCPTimeoutError`

MCP request timed out. Typically retriable with backoff.

```python
class MCPTimeoutError(MCPClientError):
    timeout_seconds: float | None  # Timeout value
    operation: str | None          # Operation that timed out
```

### Class: `MCPToolError`

Error during tool execution.

```python
class MCPToolError(MCPServerError):
    tool_name: str | None    # Tool that failed
    error_code: str | None   # Tool-specific error code
```

---

## MCP Client

### Class: `MCPClientAdapter`

Concrete implementation of MCPClient protocol using the MCP SDK.

```python
class MCPClientAdapter:
    def __init__(
        self,
        *,
        max_retries: int = 3,
        retry_wait_initial: float = 1.0,
        retry_wait_max: float = 10.0,
    ) -> None: ...
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `is_connected` | `bool` | True if currently connected |
| `server_info` | `MCPServerInfo | None` | Connected server info |

#### Methods

##### `async connect(config: MCPServerConfig) -> Result[MCPServerInfo, MCPClientError]`

Connect to an MCP server.

```python
async with MCPClientAdapter() as client:
    result = await client.connect(config)
    if result.is_ok:
        print(f"Connected to {result.value.name}")
```

##### `async disconnect() -> Result[None, MCPClientError]`

Disconnect from the current MCP server.

##### `async list_tools() -> Result[Sequence[MCPToolDefinition], MCPClientError]`

List available tools from the connected server.

##### `async call_tool(name: str, arguments: dict[str, Any] | None = None) -> Result[MCPToolResult, MCPClientError]`

Call a tool on the connected server.

```python
result = await client.call_tool(
    "search_files",
    {"pattern": "*.py", "path": "/src"},
)
if result.is_ok:
    print(result.value.text_content)
```

##### `async list_resources() -> Result[Sequence[MCPResourceDefinition], MCPClientError]`

List available resources from the connected server.

##### `async read_resource(uri: str) -> Result[MCPResourceContent, MCPClientError]`

Read a resource from the connected server.

##### `async list_prompts() -> Result[Sequence[MCPPromptDefinition], MCPClientError]`

List available prompts from the connected server.

##### `async get_prompt(name: str, arguments: dict[str, str] | None = None) -> Result[str, MCPClientError]`

Get a filled prompt from the connected server.

### Class: `MCPClientManager`

Manager for multiple MCP server connections with connection pooling and health checks.

```python
class MCPClientManager:
    def __init__(
        self,
        *,
        max_retries: int = 3,
        health_check_interval: float = 60.0,
        default_timeout: float = 30.0,
    ) -> None: ...
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `servers` | `Sequence[str]` | List of server names |

#### Methods

##### `async add_server(config: MCPServerConfig, *, connect: bool = False) -> Result[MCPServerInfo | None, MCPClientError]`

Add a server configuration.

##### `async remove_server(server_name: str) -> Result[None, MCPClientError]`

Remove a server and disconnect if connected.

##### `async connect(server_name: str) -> Result[MCPServerInfo, MCPClientError]`

Connect to a specific server.

##### `async connect_all() -> dict[str, Result[MCPServerInfo, MCPClientError]]`

Connect to all registered servers.

##### `async disconnect_all() -> dict[str, Result[None, MCPClientError]]`

Disconnect from all servers.

##### `async list_all_tools() -> Sequence[MCPToolDefinition]`

List all tools from all connected servers.

##### `find_tool_server(tool_name: str) -> str | None`

Find which server provides a given tool.

##### `async call_tool(server_name: str, tool_name: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None) -> Result[MCPToolResult, MCPClientError]`

Call a tool on a specific server.

##### `async call_tool_auto(tool_name: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None) -> Result[MCPToolResult, MCPClientError]`

Call a tool, automatically finding the server that provides it.

##### `start_health_checks() -> None`

Start periodic health checks for all connections.

#### Example

```python
manager = MCPClientManager()

# Add multiple servers
await manager.add_server(MCPServerConfig(
    name="filesystem",
    transport=TransportType.STDIO,
    command="npx",
    args=("-y", "@modelcontextprotocol/server-filesystem"),
))

await manager.add_server(MCPServerConfig(
    name="github",
    transport=TransportType.STDIO,
    command="npx",
    args=("-y", "@modelcontextprotocol/server-github"),
    env={"GITHUB_TOKEN": os.environ["GITHUB_TOKEN"]},
))

# Connect to all
results = await manager.connect_all()

# Use tools from any server
all_tools = await manager.list_all_tools()

# Call tool with auto-discovery
result = await manager.call_tool_auto("read_file", {"path": "/etc/hosts"})

# Cleanup
await manager.disconnect_all()
```

---

## MCP Server

### Class: `MCPServerAdapter`

Concrete implementation of MCPServer protocol using FastMCP.

```python
class MCPServerAdapter:
    def __init__(
        self,
        *,
        name: str = "ouroboros-mcp",
        version: str = "1.0.0",
        auth_config: AuthConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
    ) -> None: ...
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `info` | `MCPServerInfo` | Server information |

#### Methods

##### `register_tool(handler: ToolHandler) -> None`

Register a tool handler.

##### `register_resource(handler: ResourceHandler) -> None`

Register a resource handler.

##### `register_prompt(handler: PromptHandler) -> None`

Register a prompt handler.

##### `async list_tools() -> Sequence[MCPToolDefinition]`

List all registered tools.

##### `async call_tool(name: str, arguments: dict[str, Any], credentials: dict[str, str] | None = None) -> Result[MCPToolResult, MCPServerError]`

Call a registered tool.

##### `async read_resource(uri: str) -> Result[MCPResourceContent, MCPServerError]`

Read a registered resource.

##### `async serve() -> None`

Start serving MCP requests. This method blocks until the server is stopped.

##### `async shutdown() -> None`

Shutdown the server gracefully.

#### Example

```python
from ouroboros.mcp.server import MCPServerAdapter

server = MCPServerAdapter(
    name="my-ouroboros-server",
    version="1.0.0",
)

# Register custom handlers
server.register_tool(MyToolHandler())
server.register_resource(MyResourceHandler())

# Start serving
await server.serve()
```

---

## Tool Registry

### Class: `ToolRegistry`

Registry for managing MCP tool handlers.

```python
class ToolRegistry:
    def __init__(self) -> None: ...
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `tool_count` | `int` | Number of registered tools |

#### Methods

##### `register(handler: ToolHandler, *, category: str = "default") -> None`

Register a tool handler.

##### `register_all(handlers: Sequence[ToolHandler], *, category: str = "default") -> None`

Register multiple tool handlers.

##### `unregister(name: str) -> bool`

Unregister a tool handler. Returns True if found.

##### `get(name: str) -> ToolHandler | None`

Get a tool handler by name.

##### `list_tools(category: str | None = None) -> Sequence[MCPToolDefinition]`

List all registered tools, optionally filtered by category.

##### `list_categories() -> Sequence[str]`

List all tool categories.

##### `async call(name: str, arguments: dict[str, Any]) -> Result[MCPToolResult, MCPServerError]`

Call a registered tool.

##### `has_tool(name: str) -> bool`

Check if a tool is registered.

##### `clear() -> None`

Clear all registered tools.

#### Example

```python
from ouroboros.mcp.tools import ToolRegistry

registry = ToolRegistry()

# Register tools by category
registry.register(ExecuteSeedHandler(), category="execution")
registry.register(SessionStatusHandler(), category="status")

# List tools
all_tools = registry.list_tools()
execution_tools = registry.list_tools(category="execution")

# Call a tool
result = await registry.call("execute_seed", {"seed_id": "123"})
```

### Global Registry

A global registry instance is available for convenience:

```python
from ouroboros.mcp.tools import get_global_registry, register_tool

# Get global registry
registry = get_global_registry()

# Register to global registry
register_tool(MyHandler(), category="custom")
```

---

## Convenience Functions

### `create_mcp_client`

Context manager for creating and connecting an MCP client.

```python
from ouroboros.mcp.client.adapter import create_mcp_client

async with create_mcp_client(config) as client:
    tools = await client.list_tools()
    # client is automatically connected and will disconnect on exit
```

### `create_ouroboros_server`

Factory function for creating an Ouroboros MCP server with default configuration.

```python
from ouroboros.mcp.server import create_ouroboros_server

server = create_ouroboros_server(
    name="my-server",
    version="1.0.0",
)
# Register additional handlers as needed
await server.serve()
```

---

## Orchestrator MCP Integration

### Class: `MCPToolProvider`

Provider for MCP tools to integrate with OrchestratorRunner.

```python
from ouroboros.orchestrator import MCPToolProvider

class MCPToolProvider:
    def __init__(
        self,
        mcp_manager: MCPClientManager,
        *,
        default_timeout: float = 30.0,
        tool_prefix: str = "",
    ) -> None: ...
```

#### Properties

| Property | Type | Description |
|----------|------|-------------|
| `tool_prefix` | `str` | Prefix added to tool names |
| `conflicts` | `Sequence[ToolConflict]` | Tool conflicts detected |

#### Methods

##### `async get_tools(builtin_tools: Sequence[str] | None = None) -> Sequence[MCPToolInfo]`

Discover tools from all connected MCP servers.

```python
provider = MCPToolProvider(manager, tool_prefix="mcp_")
tools = await provider.get_tools(builtin_tools=["Read", "Write", "Edit"])
# Returns MCPToolInfo for each non-conflicting tool
```

##### `get_tool_names() -> Sequence[str]`

Get list of available tool names (with prefix).

##### `has_tool(name: str) -> bool`

Check if a tool is available.

##### `get_tool_info(name: str) -> MCPToolInfo | None`

Get info for a specific tool.

##### `async call_tool(name: str, arguments: dict[str, Any] | None = None, *, timeout: float | None = None) -> Result[MCPToolResult, MCPToolError]`

Call an MCP tool with retry logic and graceful error handling.

```python
result = await provider.call_tool("mcp_read_file", {"path": "/tmp/test"})
if result.is_ok:
    print(result.value.text_content)
else:
    print(f"Error: {result.error}")  # Never raises, returns error
```

### Class: `MCPToolInfo`

Information about an available MCP tool.

```python
@dataclass(frozen=True, slots=True)
class MCPToolInfo:
    name: str           # Tool name (possibly prefixed)
    original_name: str  # Original tool name from server
    server_name: str    # Server providing this tool
    description: str    # Tool description
    input_schema: dict[str, Any]  # JSON Schema for parameters
```

### Class: `ToolConflict`

Information about a tool name conflict.

```python
@dataclass(frozen=True, slots=True)
class ToolConflict:
    tool_name: str      # Conflicting tool name
    source: str         # Server name or "built-in"
    shadowed_by: str    # What shadowed this tool
    resolution: str     # How conflict was resolved
```

### Configuration Loading

#### Function: `load_mcp_config`

Load MCP client configuration from a YAML file.

```python
from ouroboros.orchestrator import load_mcp_config

result = load_mcp_config(Path("mcp.yaml"))
if result.is_ok:
    config = result.value
    # config.servers - list of MCPServerConfig
    # config.connection - MCPConnectionConfig
    # config.tool_prefix - optional prefix
```

#### Class: `MCPClientConfig`

Complete MCP client configuration.

```python
@dataclass(frozen=True, slots=True)
class MCPClientConfig:
    servers: tuple[MCPServerConfig, ...]
    connection: MCPConnectionConfig
    tool_prefix: str = ""
```

#### Class: `MCPConnectionConfig`

Connection settings for MCP servers.

```python
@dataclass(frozen=True, slots=True)
class MCPConnectionConfig:
    timeout_seconds: float = 30.0
    retry_attempts: int = 3
    health_check_interval: float = 60.0
```

### Example: Using MCP Tools with OrchestratorRunner

```python
from ouroboros.orchestrator import (
    ClaudeAgentAdapter,
    OrchestratorRunner,
    load_mcp_config,
)
from ouroboros.mcp.client.manager import MCPClientManager
from ouroboros.persistence.event_store import EventStore

# Load MCP config
config_result = load_mcp_config(Path("mcp.yaml"))
config = config_result.value

# Create and connect MCP manager
manager = MCPClientManager(
    max_retries=config.connection.retry_attempts,
    default_timeout=config.connection.timeout_seconds,
)

for server_config in config.servers:
    await manager.add_server(server_config)

await manager.connect_all()

# Create runner with MCP integration
event_store = EventStore("sqlite+aiosqlite:///~/.ouroboros/ouroboros.db")
await event_store.initialize()

adapter = ClaudeAgentAdapter()
runner = OrchestratorRunner(
    adapter,
    event_store,
    mcp_manager=manager,
    mcp_tool_prefix=config.tool_prefix,
)

# Execute seed - MCP tools will be available to the agent
result = await runner.execute_seed(seed)

# Cleanup
await manager.disconnect_all()
```
