# MCP Demo Examples

This directory contains examples demonstrating how users interact with Claude Desktop using Ouroboros MCP tools.

## What These Demos Show

These demos simulate the complete user experience when Claude Desktop uses the MCP `execute_seed` tool:

1. 👤 User makes a request in Claude Desktop
2. 🤖 Claude decides to use the `ouroboros_execute_seed` MCP tool
3. 📺 **Real-time progress display** appears in the MCP server terminal showing:
   - Acceptance criteria status (⏳ → 🔄 → ✅)
   - Progress spinner and timing
   - Live session metrics
4. ✅ Results are returned to Claude and shown to the user

## Available Demos

### 1. Hello World (Simple)
**File:** `demo_seed.yaml`

A minimal example that creates a Python hello world script.
- 4 acceptance criteria
- ~30-40 seconds execution time
- Good for understanding the basic flow

### 2. TODO CLI Application (Realistic)
**File:** `todo_cli_seed.yaml`

A more realistic example that builds a complete command-line TODO application.
- 6 acceptance criteria
- CLI with subcommands (add/list/complete)
- JSON-based persistent storage
- Error handling and tests
- ~2-3 minutes execution time
- Shows real-world complexity

## Running the Demos

### Option 1: Live Execution (See Real Progress)

```bash
# From repo root
uv run ouroboros run workflow --orchestrator examples/mcp_demo/todo_cli_seed.yaml
```

You'll see real-time progress as Ouroboros executes each acceptance criterion.

### Option 2: Full User Experience Simulation

```bash
# Record the TODO CLI demo
cd examples/mcp_demo
./record_todo_cli.sh

# Replay the recording
asciinema play todo_cli_demo.cast
```

This shows the complete flow: user request → Claude response → MCP execution → results.

## Recording Your Own Demos

### Prerequisites

```bash
# Install asciinema for terminal recording
brew install asciinema  # macOS
# or: apt install asciinema  # Linux
```

### Create a Custom Demo

1. Create a seed file (see `todo_cli_seed.yaml` as template)
2. Create a demo script (see `demo_todo_cli.sh` as template)
3. Create a recording script (see `record_todo_cli.sh` as template)
4. Run the recording script

### Playback Controls

- `Space`: Pause/resume
- `q`: Quit
- `.`: Step forward one frame

## What Makes a Good Demo Seed?

**Too Simple (like hello world):**
- Execution time is mostly orchestrator overhead
- Doesn't showcase the value of progress tracking

**Good Complexity (like TODO CLI):**
- Multiple meaningful acceptance criteria (4-8)
- Mix of file operations, logic, and testing
- Takes 1-5 minutes to complete
- Shows real progress updates

**Too Complex:**
- Takes >10 minutes (recordings become unwieldy)
- Too many criteria (hard to follow)

## Architecture Notes

When Claude Desktop calls `execute_seed` via MCP:

```
Claude Desktop
    ↓ (MCP protocol)
MCP Server (FastMCP)
    ↓
ExecuteSeedHandler
    ↓
OrchestratorRunner ← Real-time progress display here!
    ↓
Claude Agent SDK
    ↓
Anthropic API
```

The progress display runs in the **MCP server's terminal**, not in Claude Desktop. This gives developers visibility into what's happening during long-running operations.

## Files

- `demo_seed.yaml` - Simple hello world seed
- `todo_cli_seed.yaml` - TODO CLI application seed
- `demo_mcp_experience.sh` - Hello world user experience simulation
- `demo_todo_cli.sh` - TODO CLI user experience simulation
- `record_mcp_demo.sh` - Record hello world demo
- `record_todo_cli.sh` - Record TODO CLI demo
- `*.cast` - Asciinema recordings (gitignored)

## Tips

- Use `--overwrite` flag when re-recording to replace existing .cast files
- Recordings are JSON-based and can be edited manually if needed
- Upload to asciinema.org for easy sharing: `asciinema upload demo.cast`
