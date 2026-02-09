# /ouroboros:seed

Generate validated Seed specifications from interview results.

## Usage

```
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

## Next Steps

After seed generation, use `/ouroboros:run` (Phase 2) to execute the workflow.
