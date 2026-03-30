---
name: status
description: "Check session status and measure goal drift"
mcp_tool: ouroboros_session_status
mcp_args:
  session_id: "$1"
---

# /ouroboros:status

Check session status and measure goal drift.

## Usage

```
/ouroboros:status [session_id]
```

**Trigger keywords:** "am I drifting?", "session status", "drift check"

## How It Works

1. **Session Status**: Queries the current state of an execution session
2. **Drift Measurement**: Measures how far the execution has deviated from the original seed goal

## Instructions

When the user invokes this skill:

### Load MCP Tools (Required first)

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded. Skip directly to the steps below.

**If `ToolSearch` is available** (Claude Code): MCP tools may be registered as deferred tools that must be explicitly loaded.
1. Use the `ToolSearch` tool to find and load the status MCP tools:
   ```
   ToolSearch query: "+ouroboros session status"
   ```
2. If ToolSearch finds the tools → proceed with the steps below. If not → skip to **Fallback** section.

### Status Steps

**Architecture**: `ouroboros_session_status` is the source of truth. This skill has no native-subagent/internal-agent split; it talks to MCP directly.

1. Determine the session to check:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for recent session IDs
   - If none found: Ask user for the session ID

2. Call `ouroboros_session_status` MCP tool:
   ```
   Tool: ouroboros_session_status
   Arguments:
     session_id: <session ID>
   ```

3. If the user asks about drift (or says "am I drifting?"), only call `ouroboros_measure_drift` when you have the required payload:
   ```
   Tool: ouroboros_measure_drift
   Arguments:
     session_id: <session ID>
     current_output: <current execution output or file contents>
     seed_content: <original seed YAML>
     constraint_violations: []  (any known violations)
     current_concepts: []       (concepts in current output)
   ```
   - If `current_output` or `seed_content` is missing, do not guess or reconstruct large context from scratch.
   - Ask the user for the missing artifact/seed, or explain that drift cannot be computed from `session_id` alone.

4. Present results:
   - Show session status (running, completed, failed)
   - Show progress information
   - If drift measured, show the drift report
   - If drift exceeds threshold (0.3), warn and suggest actions
   - End with a `📍` next-step based on context:
     - No drift measured: `📍 Session active — say "am I drifting?" to measure drift, or continue with ooo run`
     - Drift ≤ 0.3: `📍 On track — continue with ooo run or ooo evaluate when ready`
     - Drift > 0.3: `📍 Warning: significant drift detected. Consider ooo interview to re-clarify, or ooo evolve to course-correct`

## Drift Thresholds

| Combined Drift | Status | Action |
|----------------|--------|--------|
| 0.0 - 0.15 | Excellent | On track |
| 0.15 - 0.30 | Acceptable | Monitor closely |
| 0.30+ | Exceeded | Consider consensus review or course correction |

## Fallback (No MCP Server)

If the MCP server is not available:

```
Session tracking requires the Ouroboros MCP server.
Run /ouroboros:setup to configure.

Without MCP, you can manually check drift by comparing
your current implementation against the seed specification.
```

## Example

```
User: am I drifting?

Session: sess-abc-123
Status: running
Seed ID: seed-456
Messages Processed: 8

Drift Measurement Report
========================
Combined Drift: 0.12
Status: ACCEPTABLE

Component Breakdown:
  Goal Drift: 0.08 (50% weight)
  Constraint Drift: 0.10 (30% weight)
  Ontology Drift: 0.20 (20% weight)

You're on track. Goal alignment is strong.

📍 On track — continue with `ooo run` or `ooo evaluate` when ready
```
