---
name: run
description: "Execute a Seed specification through the workflow engine"
---

# /ouroboros:run

Execute a Seed specification through the Ouroboros workflow engine.

## Usage

```
/ouroboros:run [seed_file_or_content]
```

**Trigger keywords:** "ouroboros run", "execute seed"

## How It Works

1. **Input**: Provide seed YAML content directly or a path to a `.yaml` file
2. **Validation**: Seed is parsed and validated (goal, constraints, acceptance criteria, ontology)
3. **Execution**: The orchestrator runs the workflow with PAL routing
4. **Progress**: Real-time progress updates via session tracking
5. **Result**: Execution summary with pass/fail status

## Instructions

When the user invokes this skill:

1. Check if the user provided seed content or a file path:
   - If a file path: Read the file with the Read tool
   - If inline YAML: Use directly
   - If neither: Check conversation history for a recently generated seed

2. Call the `ouroboros_execute_seed` MCP tool:
   ```
   Tool: ouroboros_execute_seed
   Arguments:
     seed_content: <the seed YAML>
     model_tier: "medium"  (or as specified by user)
     max_iterations: 10    (or as specified by user)
   ```

3. If resuming an existing session, include `session_id`:
   ```
   Tool: ouroboros_execute_seed
   Arguments:
     seed_content: <the seed YAML>
     session_id: <existing session ID>
   ```

4. Present the execution results to the user:
   - Show success/failure status
   - Show session ID (for later status checks)
   - Show execution summary
   - Suggest `/ouroboros:evaluate` for post-execution verification

## Fallback (No MCP Server)

If the MCP server is not available, inform the user:

```
Ouroboros MCP server is not configured.
To enable full execution mode, run: /ouroboros:setup

Without MCP, you can still:
- Use /ouroboros:interview for requirement clarification
- Use /ouroboros:seed to generate specifications
- Manually implement the seed specification
```

## Example

```
User: /ouroboros:run seed.yaml

[Reads seed.yaml, validates, executes via MCP]

Result:
  Seed Execution SUCCESS
  ========================
  Session ID: sess-abc-123
  Goal: Build a CLI task manager
  Duration: 45.2s
  Messages Processed: 12

  Next: /ouroboros:evaluate sess-abc-123
```
