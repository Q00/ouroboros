---
name: status
description: "Check session status and measure goal drift"
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

3. If the user asks about drift (or says "am I drifting?"), also call `ouroboros_measure_drift`:
   ```
   Tool: ouroboros_measure_drift
   Arguments:
     session_id: <session ID>
     current_output: <current execution output or file contents>
     seed_content: <original seed YAML>
     constraint_violations: []  (any known violations)
     current_concepts: []       (concepts in current output)
   ```

4. Present results:
   - Show session status (running, completed, failed)
   - Show progress information
   - If drift measured, show the drift report
   - If drift exceeds threshold (0.3), warn and suggest actions

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
```
