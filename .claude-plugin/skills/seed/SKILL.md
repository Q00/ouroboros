# /ouroboros:seed

Generate validated Seed specifications from interview results.

## Usage

```
ooo seed
/ouroboros:seed
```

**Trigger keywords:** "crystallize", "generate seed"

## How It Works

1. **Input**: Takes the interview Q&A history (from current conversation)
2. **Extraction**: The seed-architect agent extracts structured requirements
3. **Generation**: Creates a Seed YAML specification
4. **Output**: Valid Seed ready for execution

## Seed Components

The seed contains:

- **GOAL**: Clear primary objective
- **CONSTRAINTS**: Hard limitations (e.g., Python 3.14+, no external DB)
- **ACCEPTANCE_CRITERIA**: Measurable success criteria
- **ONTOLOGY_SCHEMA**: Data structure definition (name, fields, types)
- **EVALUATION_PRINCIPLES**: Quality principles with weights
- **EXIT_CONDITIONS**: When the workflow should terminate
- **METADATA**: Version, timestamp, ambiguity score, interview ID

## Example Output

```yaml
goal: Build a CLI task management tool
constraints:
  - Python 3.14+
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
```

## After Seed Generation

On successful seed generation, first announce:

```
Your seed has been crystallized!
```

Then check `~/.ouroboros/prefs.json` for `star_asked`. If `star_asked` is not set to `true`, use the **AskUserQuestion tool** with this single question:

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

- **Star & Setup**: Run `gh api -X PUT /user/starred/Q00/ouroboros`, save `{"star_asked": true}` to `~/.ouroboros/prefs.json`, then read and execute `.claude-plugin/skills/setup/SKILL.md`
- **Just Setup**: Save `{"star_asked": true}` to `~/.ouroboros/prefs.json`, then read and execute `.claude-plugin/skills/setup/SKILL.md`
- **Other** (user provides custom text): Save `{"star_asked": true}`, skip setup

Create `~/.ouroboros/` directory if it doesn't exist.

If `star_asked` is already `true`, skip the question and just announce:

```
Your seed has been crystallized! Run `ooo run` to execute (requires `ooo setup` first).
```
