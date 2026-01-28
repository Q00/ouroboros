# MCP Workflows and Architecture

This guide visualizes how the Model Context Protocol (MCP) integration works in Ouroboros, organized by acceptance criteria from Epic 9.

---

## AC 9.1: Type Definitions - Data Flow

The MCP type system provides the foundation for all protocol communication.

```
┌─────────────────────────────────────────────────────────────────┐
│                     MCP Type Hierarchy                          │
└─────────────────────────────────────────────────────────────────┘

Configuration Layer:
┌──────────────────┐
│ MCPServerConfig  │  ← User defines connection parameters
├──────────────────┤
│ • name           │
│ • transport      │
│ • command/url    │
│ • timeout        │
└──────────────────┘

Communication Layer:
┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐
│  MCPRequest      │─────→│  MCPResponse     │─────→│  MCPToolResult   │
├──────────────────┤      ├──────────────────┤      ├──────────────────┤
│ • method         │      │ • result         │      │ • content[]      │
│ • params         │      │ • error          │      │ • is_error       │
└──────────────────┘      └──────────────────┘      │ • meta           │
                                                     └──────────────────┘
                                                            │
                                                            ↓
                                                   ┌──────────────────┐
                                                   │ MCPContentItem   │
                                                   ├──────────────────┤
                                                   │ • type: text     │
                                                   │ • text: "..."    │
                                                   │ • mime_type      │
                                                   └──────────────────┘

Capability Layer:
┌──────────────────┐      ┌──────────────────────────────────────┐
│ MCPServerInfo    │─────→│        MCPCapabilities              │
├──────────────────┤      ├──────────────────────────────────────┤
│ • name           │      │ • tools: true                        │
│ • version        │      │ • resources: true                    │
│ • capabilities   │      │ • prompts: true                      │
│ • tools[]        │      │ • logging: false                     │
│ • resources[]    │      └──────────────────────────────────────┘
└──────────────────┘

Tool Definition Layer:
┌──────────────────────────────────────────────────────────────────┐
│                    MCPToolDefinition                             │
├──────────────────────────────────────────────────────────────────┤
│ name: "execute_seed"                                             │
│ description: "Execute an Ouroboros workflow from a seed"         │
│ parameters: [                                                    │
│   MCPToolParameter(                                              │
│     name="seed_content",                                         │
│     type="string",                                               │
│     description="YAML seed specification",                       │
│     required=True                                                │
│   )                                                              │
│ ]                                                                │
└──────────────────────────────────────────────────────────────────┘
```

**Key Points:**
- All types are immutable (`@dataclass(frozen=True)`)
- Clear separation between config, communication, and capability layers
- Type safety enforced through Pydantic validation

---

## AC 9.2: Server Adapter - Request Lifecycle

The server adapter handles incoming MCP requests and routes them to handlers.

```
┌─────────────────────────────────────────────────────────────────┐
│              MCP Server Request Lifecycle                        │
└─────────────────────────────────────────────────────────────────┘

1. Initialization:
   ┌──────────────────┐
   │ MCPServerAdapter │
   │    __init__()    │
   └────────┬─────────┘
            │
            ├─→ Setup FastMCP instance
            ├─→ Configure auth (if enabled)
            ├─→ Configure rate limiting
            └─→ Initialize tool/resource registries

2. Registration Phase:
   ┌──────────────────┐
   │ register_tool()  │ ← Called for each tool
   └────────┬─────────┘
            │
            ↓
   ┌─────────────────────────────────────────┐
   │     Internal Tool Registry              │
   ├─────────────────────────────────────────┤
   │ "execute_seed" → ExecuteSeedHandler     │
   │ "session_status" → SessionStatusHandler │
   │ "query_events" → QueryEventsHandler     │
   └─────────────────────────────────────────┘

3. Runtime Request Handling:

   Client Request
        │
        ↓
   ┌─────────────────────────────────────┐
   │     1. Transport Layer              │
   │  (STDIO / SSE / HTTP)               │
   └────────────┬────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │     2. Authentication               │
   │  • Validate API key (if enabled)    │
   │  • Check token HMAC                 │
   └────────────┬────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │     3. Authorization                │
   │  • Check tool permissions           │
   │  • Validate resource access         │
   └────────────┬────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │     4. Rate Limiting                │
   │  • Check token bucket               │
   │  • Update request count             │
   └────────────┬────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │     5. Input Validation             │
   │  • Check for dangerous patterns     │
   │  • Validate parameter types         │
   │  • Check size limits                │
   └────────────┬────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │     6. Tool Execution               │
   │  • Find handler in registry         │
   │  • Execute with timeout (30s)       │
   │  • Capture result/error             │
   └────────────┬────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │     7. Response Building            │
   │  • Format MCPToolResult             │
   │  • Add metadata                     │
   │  • Return to client                 │
   └─────────────────────────────────────┘

Error Handling at Each Layer:
   Authentication Failed → MCPAuthError (401)
   Authorization Failed → MCPAuthError (403)
   Rate Limit Exceeded → MCPServerError (429)
   Invalid Input → MCPServerError (400)
   Tool Not Found → MCPToolError (404)
   Execution Timeout → MCPTimeoutError (408)
   Tool Exception → MCPToolError (500)
```

**Security Layers:**
- Authentication: SHA256 API key hashing + HMAC token validation
- Authorization: Per-tool permission checks
- Rate Limiting: Token bucket algorithm (100 req/min burst, 10 req/min sustained)
- Input Validation: Dangerous pattern detection (eval, exec, path traversal, shell injection)

---

## AC 9.3: Tool Implementation - Execution Flow

Tools expose Ouroboros functionality to MCP clients.

```
┌─────────────────────────────────────────────────────────────────┐
│            Tool Execution Flow: execute_seed                     │
└─────────────────────────────────────────────────────────────────┘

Client Call:
┌───────────────────────────────────────────────────────────────┐
│ await client.call_tool(                                        │
│   "execute_seed",                                              │
│   {                                                            │
│     "seed_content": "goal: Build a CLI...",                    │
│     "mode": "orchestrator"                                     │
│   }                                                            │
│ )                                                              │
└────────────────────────┬──────────────────────────────────────┘
                         │
                         ↓
┌─────────────────────────────────────────────────────────────────┐
│                  ExecuteSeedHandler                              │
└─────────────────────────────────────────────────────────────────┘

   Step 1: Parse Input
   ┌──────────────────────────────────────┐
   │ Parse YAML seed_content              │
   │ Validate seed structure              │
   │ Extract goal, constraints, ACs       │
   └────────────┬─────────────────────────┘
                │
                ↓
   Step 2: Initialize Components
   ┌──────────────────────────────────────┐
   │ EventStore()  ← Persistence          │
   │ ClaudeAgentAdapter()  ← LLM          │
   │ OrchestratorRunner()  ← Orchestrator │
   └────────────┬─────────────────────────┘
                │
                ↓
   Step 3: Execute Workflow
   ┌──────────────────────────────────────┐
   │ await runner.execute_seed(seed)      │
   │                                      │
   │ ┌─────────────────────────┐         │
   │ │  Big Bang Phase         │         │
   │ │  (if needed)            │         │
   │ └────────┬────────────────┘         │
   │          │                           │
   │ ┌────────▼────────────────┐         │
   │ │  PAL Router             │         │
   │ │  (select model tier)    │         │
   │ └────────┬────────────────┘         │
   │          │                           │
   │ ┌────────▼────────────────┐         │
   │ │  Double Diamond         │         │
   │ │  (decompose + execute)  │         │
   │ └────────┬────────────────┘         │
   │          │                           │
   │ ┌────────▼────────────────┐         │
   │ │  Resilience             │         │
   │ │  (handle stagnation)    │         │
   │ └────────┬────────────────┘         │
   │          │                           │
   │ ┌────────▼────────────────┐         │
   │ │  Evaluation             │         │
   │ │  (verify outputs)       │         │
   │ └────────┬────────────────┘         │
   │          │                           │
   │          ↓                           │
   │     [Result]                         │
   └────────────┬─────────────────────────┘
                │
                ↓
   Step 4: Build Response
   ┌──────────────────────────────────────┐
   │ MCPToolResult(                       │
   │   content=[                          │
   │     MCPContentItem(                  │
   │       type="text",                   │
   │       text=f"""                      │
   │         Execution completed          │
   │         Session: {session_id}        │
   │         Duration: {duration}s        │
   │         Status: {status}             │
   │       """                            │
   │     )                                │
   │   ],                                 │
   │   is_error=False,                    │
   │   meta={                             │
   │     "session_id": session_id,        │
   │     "execution_id": exec_id          │
   │   }                                  │
   │ )                                    │
   └────────────┬─────────────────────────┘
                │
                ↓
        Return to Client

Alternative: session_status Tool
┌─────────────────────────────────────────────────────────────────┐
│ await client.call_tool("session_status", {"session_id": "..."}) │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ↓
   ┌──────────────────────────────────────┐
   │ SessionStatusHandler                 │
   ├──────────────────────────────────────┤
   │ 1. Query EventStore for session      │
   │ 2. Replay events for session         │
   │ 3. Build status summary              │
   │ 4. Return current state              │
   └──────────────────────────────────────┘

Alternative: query_events Tool
┌─────────────────────────────────────────────────────────────────┐
│ await client.call_tool("query_events", {                        │
│   "aggregate_type": "execution",                                │
│   "aggregate_id": "exec_123"                                    │
│ })                                                              │
└────────────────────────┬────────────────────────────────────────┘
                         │
                         ↓
   ┌──────────────────────────────────────┐
   │ QueryEventsHandler                   │
   ├──────────────────────────────────────┤
   │ 1. Validate aggregate parameters     │
   │ 2. Query EventStore.replay()         │
   │ 3. Format events as JSON             │
   │ 4. Return event history              │
   └──────────────────────────────────────┘
```

**Available Tools:**
- `execute_seed`: Run a workflow from seed YAML with real-time progress visualization
- `session_status`: Get orchestrator session status with event-sourced data
- `query_events`: Query event store history using EventStore.replay()

**Available Resources:**
- `seed://template`: Default seed template
- `config://current`: Current Ouroboros configuration

---

## Real-Time Progress Visualization with Event-Driven Updates

The execute_seed tool now features real-time progress tracking with Rich terminal display.

```
┌─────────────────────────────────────────────────────────────────┐
│         Progress Display During Seed Execution                  │
└─────────────────────────────────────────────────────────────────┘

When execute_seed is called, it displays:

┌────────────────────────────────────────────────────────────────┐
│        🚀 Ouroboros Seed Execution Progress                    │
├──────────────────────────┬─────────────────────────────────────┤
│ Session ID               │ orch_abc123def456                   │
│ Phase                    │ Executing via Claude Agent          │
│ Duration                 │ 45.3s                                │
│ Messages                 │ 23                                   │
│ Cost                     │ $0.0234                              │
│ Tokens                   │ 2,341                                │
├──────────────────────────┴─────────────────────────────────────┤
│ Acceptance Criteria                                            │
│   1. Parse seed_content YAML           ✅                      │
│   2. Initialize EventStore              ✅                      │
│   3. Connect to OrchestratorRunner      🔄                      │
│   4. Display progress in real-time      ⏳                      │
│   5. Return execution summary           ⏳                      │
├────────────────────────────────────────────────────────────────┤
│ Overall Progress            60% (3/5)                          │
└────────────────────────────────────────────────────────────────┘

Status Symbols:
  ⏳ - Pending (not started)
  🔄 - In Progress (currently executing)
  ✅ - Completed (successfully finished)
  ❌ - Failed (error encountered)
```

### Progress Display Implementation

The progress display is driven by events from the EventStore:

```
Event-Driven Progress Flow:
┌─────────────────────────────────────────────────────────────────┐
│ 1. ExecuteSeedHandler initializes components                    │
│    ├─→ EventStore (for persistence)                             │
│    ├─→ ClaudeAgentAdapter (for LLM execution)                   │
│    └─→ OrchestratorRunner (for workflow orchestration)          │
│                                                                  │
│ 2. OrchestratorRunner emits events during execution:            │
│    ├─→ orchestrator.session.started                             │
│    │   • Updates session_id in display                          │
│    │   • Sets phase to "Executing"                              │
│    │                                                             │
│    ├─→ orchestrator.tool.called                                 │
│    │   • Updates phase to "Using {tool_name}"                   │
│    │   • Increments tool call counter                           │
│    │                                                             │
│    ├─→ orchestrator.progress.updated                            │
│    │   • Increments messages_processed counter                  │
│    │   • Updates duration timer                                 │
│    │                                                             │
│    ├─→ orchestrator.task.started                                │
│    │   • Marks AC as 🔄 (in progress)                           │
│    │   • Links AC to acceptance criterion text                  │
│    │                                                             │
│    ├─→ orchestrator.task.completed                              │
│    │   • Marks AC as ✅ (success) or ❌ (failed)                │
│    │   • Updates overall progress percentage                    │
│    │                                                             │
│    └─→ orchestrator.session.completed / failed                  │
│        • Sets phase to "Completed" or "Failed"                  │
│        • Displays final summary                                 │
│                                                                  │
│ 3. EventStore.poll_events() polls for new events every 0.3s     │
│    • Real-time updates from event stream                        │
│    • Progress table refreshes at 4Hz (4 times per second)       │
│    • Live updates visible to user                               │
│    • No manual polling required                                 │
└─────────────────────────────────────────────────────────────────┘

Technical Implementation:
┌─────────────────────────────────────────────────────────────────┐
│ ProgressTracker class (definitions.py)                          │
│ ├─→ Dataclass with event_store, console, acceptance_criteria   │
│ ├─→ execute_with_live_progress():                              │
│ │   • Starts OrchestratorRunner.execute_seed() as async task  │
│ │   • Starts background event polling task                     │
│ │   • Displays Rich Live table with real-time updates         │
│ │   • Handles orchestrator.* events:                           │
│ │     - session.started: Update session_id                     │
│ │     - tool.called: Show "Using {tool}"                       │
│ │     - task.started: Mark AC as 🔄                            │
│ │     - task.completed: Mark AC as ✅/❌                        │
│ │     - progress.updated: Increment message counter            │
│ │     - session.completed/failed: Final status                 │
│ └─→ Returns Result with execution summary                      │
└─────────────────────────────────────────────────────────────────┘

EventStore polling uses async iterator:
┌─────────────────────────────────────────────────────────────────┐
│ async for event in event_store.poll_events(                    │
│     aggregate_type="session",                                   │
│     aggregate_id=session_id,                                    │
│     poll_interval=0.3                                           │
│ ):                                                              │
│     progress_tracker._handle_event(event)                       │
│     # Live table automatically refreshes                        │
└─────────────────────────────────────────────────────────────────┘
```

### Example: Execute Seed with Progress

```python
# Python example using MCP client
from ouroboros.mcp.client import MCPClientAdapter
from ouroboros.mcp.types import MCPServerConfig, TransportType

# Connect to Ouroboros MCP server
config = MCPServerConfig(
    name="ouroboros",
    transport=TransportType.STDIO,
    command="ouroboros",
    args=("mcp", "serve"),
)

async with MCPClientAdapter() as client:
    await client.connect(config)

    # Define seed in YAML
    seed_yaml = """
goal: Build a simple hello world CLI
constraints:
  - Python 3.14+
  - No external dependencies
acceptance_criteria:
  - Create hello.py with print statement
  - Make it executable
  - Test that it prints correctly
ontology_schema:
  name: HelloWorld
  description: Simple CLI ontology
  fields:
    - name: output
      field_type: string
      description: CLI output
evaluation_principles:
  - name: correctness
    description: Code executes correctly
    weight: 1.0
exit_conditions:
  - name: all_criteria_met
    description: All ACs pass
    evaluation_criteria: 100% pass rate
metadata:
  ambiguity_score: 0.1
"""

    # Execute with real-time progress display
    result = await client.call_tool(
        "ouroboros_execute_seed",
        {"seed_content": seed_yaml}
    )

    if result.is_ok:
        # Progress was displayed during execution
        # Final summary is returned
        print(result.value.text_content)
        # Output:
        # ✅ Seed execution completed successfully!
        #
        # Session ID: orch_abc123def456
        # Execution ID: exec_789ghi012
        # Duration: 45.32s
        # Messages processed: 23
        # Goal: Build a simple hello world CLI
        # Acceptance criteria: 3
        #
        # Final message:
        # All acceptance criteria have been successfully completed...
```

### Querying Session Status

After execution, query detailed session status:

```python
# Query session status
status_result = await client.call_tool(
    "ouroboros_session_status",
    {"session_id": "orch_abc123def456"}
)

print(status_result.value.text_content)
# Output:
# Session: orch_abc123def456
# Status: completed
# Execution ID: exec_789ghi012
# Messages processed: 23
# Started: 2026-01-28T10:30:45.123456+00:00
# Completed: 2026-01-28T10:31:30.456789+00:00
```

### Querying Event History

Retrieve complete event history for analysis:

```python
# Query all events for session
events_result = await client.call_tool(
    "ouroboros_query_events",
    {
        "session_id": "orch_abc123def456",
        "limit": 100
    }
)

print(events_result.value.text_content)
# Output:
# Event Query Results
# ==================================================
# Session: orch_abc123def456
# Type filter: all
# Events found: 15
#
# 1. [10:30:45] orchestrator.session.started
#    Data: {"execution_id": "exec_789ghi012", ...}
#
# 2. [10:30:46] orchestrator.tool.called
#    Data: {"tool_name": "Read", ...}
#
# 3. [10:30:48] orchestrator.progress.updated
#    Data: {"message_type": "assistant", "step": 1, ...}
# ...
```

### Filter Events by Type

```python
# Query only tool calls
tool_events = await client.call_tool(
    "ouroboros_query_events",
    {
        "session_id": "orch_abc123def456",
        "event_type": "orchestrator.tool.called",
        "limit": 50
    }
)

# Shows only tool invocation events
# Useful for debugging tool usage patterns
```

---

## AC 9.4: CLI Integration - Command Flow

The CLI provides user-friendly commands for MCP operations.

```
┌─────────────────────────────────────────────────────────────────┐
│                  CLI Command Architecture                        │
└─────────────────────────────────────────────────────────────────┘

Command Structure:
┌─────────────────────────────────────────────────────────────────┐
│ ouroboros mcp <subcommand> [options]                            │
└─────────────────────────────────────────────────────────────────┘

1. Start MCP Server:
   ┌──────────────────────────────────────────────────────────────┐
   │ $ ouroboros mcp serve                                         │
   │     --transport stdio                                         │
   │     --auth                                                    │
   │     --rate-limit                                              │
   └────────────┬─────────────────────────────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │  MCP Command Handler                │
   ├─────────────────────────────────────┤
   │  1. Parse CLI args                  │
   │  2. Load config from                │
   │     ~/.ouroboros/config.yaml        │
   │  3. Initialize MCPServerAdapter:    │
   │     • Load auth config              │
   │     • Setup rate limiter            │
   │     • Register OUROBOROS_TOOLS      │
   │     • Register OUROBOROS_RESOURCES  │
   │  4. Start transport:                │
   │     • STDIO: sys.stdin/stdout       │
   │     • SSE: HTTP server on port      │
   │  5. Handle signals (SIGINT/SIGTERM) │
   │  6. Cleanup on shutdown             │
   └────────────┬────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │  Server Running                     │
   │  • Listening for MCP requests       │
   │  • Logging to structured logs       │
   │  • Health checks enabled            │
   └─────────────────────────────────────┘

2. Show Server Info:
   ┌──────────────────────────────────────────────────────────────┐
   │ $ ouroboros mcp info                                          │
   └────────────┬─────────────────────────────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │  Display:                           │
   │  • Server name & version            │
   │  • Capabilities (tools, resources)  │
   │  • Available tools:                 │
   │    - execute_seed                   │
   │    - session_status                 │
   │    - query_events                   │
   │  • Available resources:             │
   │    - seed://template                │
   │    - config://current               │
   │  • Security features:               │
   │    - Authentication: enabled/off    │
   │    - Rate limiting: enabled/off     │
   └─────────────────────────────────────┘

3. Test Connection:
   ┌──────────────────────────────────────────────────────────────┐
   │ $ ouroboros mcp test                                          │
   │     --server-config ~/.ouroboros/mcp_servers.json            │
   └────────────┬─────────────────────────────────────────────────┘
                │
                ↓
   ┌─────────────────────────────────────┐
   │  Connection Test Flow:              │
   │  1. Load server configs             │
   │  2. Create MCPClientManager         │
   │  3. For each server:                │
   │     • Attempt connection            │
   │     • List tools                    │
   │     • Check latency                 │
   │     • Verify auth                   │
   │  4. Display results table           │
   └─────────────────────────────────────┘

CLI Integration with Orchestrator:
┌─────────────────────────────────────────────────────────────────┐
│                  End-to-End Workflow                             │
└─────────────────────────────────────────────────────────────────┘

   Terminal 1: Start Server
   ┌─────────────────────────────────┐
   │ $ ouroboros mcp serve           │
   │   --transport stdio             │
   │                                 │
   │ [INFO] MCP Server started       │
   │ [INFO] Transport: stdio         │
   │ [INFO] Tools: 3 registered      │
   │ [INFO] Ready for connections    │
   └─────────────────────────────────┘

   Terminal 2: Connect via Claude Desktop
   ┌─────────────────────────────────────────────────────────────┐
   │ // claude_desktop_config.json                               │
   │ {                                                           │
   │   "mcpServers": {                                           │
   │     "ouroboros": {                                          │
   │       "command": "ouroboros",                               │
   │       "args": ["mcp", "serve", "--transport", "stdio"]      │
   │     }                                                       │
   │   }                                                         │
   │ }                                                           │
   └─────────────────────────────────────────────────────────────┘
           │
           ↓
   ┌─────────────────────────────────────────────────────────────┐
   │ Claude Desktop UI                                           │
   ├─────────────────────────────────────────────────────────────┤
   │ User: "Execute this seed for a task manager CLI"           │
   │                                                             │
   │ [Claude uses execute_seed tool internally]                 │
   │                                                             │
   │ Claude: "I've started the workflow execution.              │
   │         Session ID: orch_abc123                            │
   │         You can check status with session_status."         │
   └─────────────────────────────────────────────────────────────┘

   Later: Check Status
   ┌─────────────────────────────────────────────────────────────┐
   │ User: "What's the status of orch_abc123?"                  │
   │                                                             │
   │ [Claude uses session_status tool]                          │
   │                                                             │
   │ Claude: "Session orch_abc123:                              │
   │         • Status: Running                                  │
   │         • Phase: Double Diamond                            │
   │         • Messages: 45/100                                 │
   │         • Duration: 2m 34s                                 │
   │         • Cost: $0.23                                      │
   │                                                             │
   │         The workflow is executing acceptance criteria..."   │
   └─────────────────────────────────────────────────────────────┘
```

**CLI Commands:**
- `ouroboros mcp serve`: Start MCP server (STDIO/SSE)
- `ouroboros mcp info`: Show server capabilities
- `ouroboros mcp test`: Test connection to servers

**Configuration File:**
```yaml
# ~/.ouroboros/config.yaml
mcp:
  server:
    name: "ouroboros-mcp"
    version: "1.0.0"
    auth:
      enabled: true
      api_keys:
        - "your-api-key-here"
    rate_limit:
      enabled: true
      requests_per_minute: 100
      burst: 10
```

---

## Complete Integration Example

Putting it all together - from type definitions to CLI usage:

```
┌─────────────────────────────────────────────────────────────────┐
│            Full MCP Integration Workflow                         │
└─────────────────────────────────────────────────────────────────┘

Developer Setup:
   1. Install Ouroboros: pip install ouroboros
   2. Configure MCP: ouroboros config init
   3. Set API key: export ANTHROPIC_API_KEY=xxx

Start MCP Server:
   $ ouroboros mcp serve --transport stdio

   ┌─────────────────────────────────────┐
   │ MCPServerAdapter                    │
   ├─────────────────────────────────────┤
   │ Types: All frozen dataclasses ✓     │
   │ Server: FastMCP initialized ✓       │
   │ Tools: 3 registered ✓               │
   │ Resources: 2 registered ✓           │
   │ Auth: Enabled ✓                     │
   │ Rate Limit: Active ✓                │
   └─────────────────────────────────────┘

Claude Desktop Connection:
   MCP Protocol Handshake
        ↓
   [List Tools] → Returns: execute_seed, session_status, query_events
        ↓
   User asks Claude to run a workflow
        ↓
   [Call Tool: execute_seed]
        │
        ├─→ Authentication (API key)
        ├─→ Authorization (tool permission)
        ├─→ Rate Limiting (check bucket)
        ├─→ Input Validation (no dangerous patterns)
        ├─→ Execute via OrchestratorRunner
        │    │
        │    ├─→ EventStore persists events
        │    ├─→ ClaudeAgent executes phases
        │    └─→ Returns Result[ExecutionResult, Error]
        │
        └─→ Format MCPToolResult
             │
             └─→ Return to Claude

Claude shows user:
   "✓ Workflow completed successfully
    Session: orch_abc123
    Duration: 3m 45s
    Cost: $0.67"

User asks for status:
   [Call Tool: session_status]
        │
        └─→ Query EventStore
             │
             └─→ Return session state

User queries event history:
   [Call Tool: query_events]
        │
        └─→ Replay events from store
             │
             └─→ Return chronological event list
```

**Benefits of This Architecture:**
1. **Type Safety**: Frozen dataclasses prevent mutation bugs
2. **Separation of Concerns**: Types → Server → Tools → CLI
3. **Security Layers**: Auth → Authz → Rate Limit → Validation
4. **Observability**: EventStore tracks all operations
5. **Flexibility**: Multiple transports (STDIO, SSE, HTTP)

---

## Troubleshooting

### Common Issues

**Connection Failed**
```
Error: Failed to connect to MCP server

Check:
1. Server is running: ps aux | grep ouroboros
2. Transport matches: stdio vs sse
3. Permissions: Check file/network access
```

**Authentication Failed**
```
Error: MCPAuthError (401)

Solutions:
1. Verify API key in config
2. Check token timestamp (must be within 5 min)
3. Ensure HMAC signature is valid
```

**Rate Limited**
```
Error: MCPServerError (429) - Rate limit exceeded

Info:
- Default: 100 requests/min burst, 10 sustained
- Bucket refills at 10 tokens/min
- Wait 6 seconds per token needed
```

**Tool Timeout**
```
Error: MCPTimeoutError (408) - Tool execution timeout

Cause:
- Default timeout: 30 seconds
- Long-running workflows exceed limit

Solution:
- For execute_seed, use orchestrator mode
- Poll session_status for updates
```

---

## Performance Considerations

**Latency:**
- STDIO: ~50ms per request (local process)
- SSE: ~100-200ms per request (network)
- Tool execution: Varies by complexity

**Concurrency:**
- Rate limiter prevents overload
- Global lock in manager (consider per-server locks)
- Tool handlers are async-safe

**Resource Usage:**
- Memory: ~50MB base + tool overhead
- CPU: Minimal except during tool execution
- Disk: EventStore grows with events (SQLite)

**Optimization Tips:**
1. Use connection pooling for multiple servers
2. Cache server info to avoid repeated queries
3. Batch event queries when possible
4. Clean up old events from EventStore periodically
