---
name: seed
description: "Generate validated Seed specifications from interview results"
mcp_tool: ouroboros_generate_seed
mcp_args:
  session_id: "$1"
---

# /ouroboros:seed

Generate validated Seed specifications from interview results.

## Usage

```
ooo seed [session_id]
/ouroboros:seed [session_id]
```

**Trigger keywords:** "crystallize", "generate seed"

## Instructions

When the user invokes this skill:

### Step 0: Load Tools

**If `ToolSearch` is available** (Claude Code): run both in a single message (parallel):
1. `ToolSearch` with `+ouroboros seed`
2. `ToolSearch` with `select:AskUserQuestion`

**If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded via the configured MCP server. Skip directly to Path A.

Store whichever question tool is available (`AskUserQuestion` or `AskQuestion`) as the **question tool**. Used for the post-generation star prompt.
- If MCP tool reachable → **Path A**. If not → **Path B**.

### Path A: MCP Mode (Preferred)

If the `ouroboros_generate_seed` MCP tool is available (loaded via ToolSearch above):

**Architecture**: Same as interview/evaluate — main is thin, and `ouroboros_generate_seed(action="state")` is the stage-local source of truth.

```
MCP (state + validate) ←→ @seed-architect (read state, generate YAML, save)
You (main) = just pass session_id and present the summary
```

1. Determine the interview session:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for a recent `ouroboros_interview` session ID
   - If none found: Ask the user

2. **If native subagents are available, delegate to @seed-architect**:
   ```
   Tool: Agent
   Arguments:
     prompt: "session_id: <session_id>"
     subagent_type: "ouroboros:seed-architect"
     description: "Generate seed from interview"
   ```

   The agent reads seed-generation state from `ouroboros_generate_seed(action="state")`, generates YAML, validates+saves via `action="generate"`, and returns a compact summary.
   Do NOT include Q&A history or interview context in the prompt — the agent reads it from MCP.

3. Present the summary returned by the agent or tool to the user.

If native subagents are unavailable or fail twice, call `ouroboros_generate_seed(session_id=<session_id>)` directly and let MCP run the original internal LLM-in-MCP seed generation flow.

**Context minimization** (CRITICAL):
- When native subagents are available, do NOT call `ouroboros_generate_seed(session_id=...)` from main — that bypasses the thin-orchestrator contract.
- Do NOT call `ouroboros_interview(action="state")` from main for seed generation.
- Do NOT pass Q&A data in the agent prompt.
- The agent handles ALL MCP interaction in its own isolated context.

**Advantages of MCP mode**: Automated ambiguity scoring (must be <= 0.2), structured extraction from persisted interview state, reproducible.

### Path B: Plugin Fallback (No MCP Server)

If the MCP tool is NOT available, fall back to agent-based generation:

1. Read `agents/seed-architect.md` and adopt that role with `session_id`
2. Follow the agent's workflow instructions

## Seed Components

The seed-architect agent handles component extraction — see agents/seed-architect.md

## Example Output

```yaml
goal: Build a CLI task management tool
constraints:
  - Python >= 3.12
  - No external database
  - SQLite for persistence
acceptance_criteria:
  - Tasks can be created
  - Tasks can be listed
  - Tasks can be marked complete
ontology_schema:
  name: TaskManager
  description: Task management domain model
  fields:
    - name: tasks
      type: array
      description: List of tasks
    - name: title
      type: string
      description: Task title
metadata:
  ambiguity_score: 0.15
```

## After Seed Generation

On successful seed generation, first announce:

```
Your seed has been crystallized!
```

Then check `~/.ouroboros/prefs.json` for `star_asked`. If `star_asked` is not set to `true`, **ask using the question tool loaded in Step 0**:
- Prompt: `If Ouroboros helped clarify your thinking, a GitHub star supports continued development. Ready to unlock Full Mode?`
- Options: `⭐ Star & Setup`, `Just Setup`

- **Star & Setup**: Run `gh api -X PUT /user/starred/Q00/ouroboros`, save `{"star_asked": true}` to `~/.ouroboros/prefs.json`, then read and execute `skills/setup/SKILL.md`
- **Just Setup**: Save `{"star_asked": true}` to `~/.ouroboros/prefs.json`, then read and execute `skills/setup/SKILL.md`

Create `~/.ouroboros/` directory if it doesn't exist.

If `star_asked` is already `true`, skip the question and just announce:

```
Your seed has been crystallized!
📍 Next: `ooo run` to execute this seed (requires `ooo setup` first)
```
