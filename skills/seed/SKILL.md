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

### Load MCP Tools (Required before Path A/B decision)

Ouroboros MCP tools must be available before proceeding. How they are discovered depends on your host:

- **Claude Code**: Tools are deferred — use `ToolSearch` to load them:
  ```
  ToolSearch query: "+ouroboros seed"
  ```
- **Cursor / other MCP clients**: Tools are auto-loaded when the server connects. They should already be callable as `ouroboros_generate_seed`.

If the tool is available → proceed to **Path A**. If not → skip to **Path B**.

### Path A: MCP Mode (Preferred)

If the `ouroboros_generate_seed` MCP tool is available (loaded via ToolSearch above):

1. Determine the interview session:
   - If `session_id` provided: Use it directly
   - If no session_id: Check conversation for a recent `ouroboros_interview` session ID
   - If none found: Ask the user

2. Call the MCP tool:
   ```
   Tool: ouroboros_generate_seed
   Arguments:
     session_id: <interview session ID>
   ```

3. The tool extracts requirements from the interview, calculates ambiguity score, and generates the Seed YAML.

4. Present the generated seed to the user.

**Advantages of MCP mode**: Automated ambiguity scoring (must be <= 0.2), structured extraction from persisted interview state, reproducible.

### Path B: Plugin Fallback (No MCP Server)

If the MCP tool is NOT available, fall back to agent-based generation:

1. Read `src/ouroboros/agents/seed-architect.md` and adopt that role
2. Extract structured requirements from the interview Q&A in conversation history
3. Generate a Seed YAML specification
4. Present the seed to the user

## Seed Components

The seed contains:

- **GOAL**: Clear primary objective
- **CONSTRAINTS**: Hard limitations (e.g., Python >= 3.12, no external DB)
- **ACCEPTANCE_CRITERIA**: Measurable success criteria
- **ONTOLOGY_SCHEMA**: Data structure definition (name, fields, types)
- **EVALUATION_PRINCIPLES**: Quality principles with weights
- **EXIT_CONDITIONS**: When the workflow should terminate
- **METADATA**: Version, timestamp, ambiguity score, interview ID

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

Then check `~/.ouroboros/prefs.json` for `star_asked`. If `star_asked` is not set to `true`, use **AskUserQuestion** (Claude Code) or **AskQuestion** (Cursor) with this single question:

```json
{
  "questions": [{
    "question": "If Ouroboros helped clarify your thinking, a GitHub star supports continued development. Ready to unlock Full Mode?",
    "header": "Next step",
    "options": [
      {
        "label": "\u2b50 Star & Setup",
        "description": "Star on GitHub + run ooo setup to enable run, evaluate, status"
      },
      {
        "label": "Just Setup",
        "description": "Skip star, go straight to ooo setup for Full Mode"
      }
    ],
    "multiSelect": false
  }]
}
```

- **Star & Setup**: Run `gh api -X PUT /user/starred/Q00/ouroboros`, save `{"star_asked": true}` to `~/.ouroboros/prefs.json`, then read and execute `skills/setup/SKILL.md`
- **Just Setup**: Save `{"star_asked": true}` to `~/.ouroboros/prefs.json`, then read and execute `skills/setup/SKILL.md`
- **Other** (user provides custom text): Save `{"star_asked": true}`, skip setup

Create `~/.ouroboros/` directory if it doesn't exist.

If `star_asked` is already `true`, skip the question and just announce:

```
Your seed has been crystallized!
📍 Next: `ooo run` to execute this seed (requires `ooo setup` first)
```
