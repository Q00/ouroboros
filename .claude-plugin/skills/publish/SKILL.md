---
name: publish
description: "Publish Seed specification as GitHub Issues for team-based project management"
---

# /ouroboros:publish

Convert a Seed specification into structured GitHub Issues for team workflows.

## Usage

```
ooo publish [seed_path]
/ouroboros:publish [seed_path]
```

**Trigger keywords:** "publish to github", "create issues from seed", "seed to issues"

## Instructions

When the user invokes this skill:

### Step 1: Prerequisite Check

**1a. Verify `gh` CLI is installed:**

```bash
command -v gh >/dev/null 2>&1 && echo "OK" || echo "MISSING"
```

If missing, tell the user:
```
GitHub CLI (gh) is not installed.
Install it: https://cli.github.com/
```
Stop.

**1b. Verify `gh` is authenticated:**

```bash
gh auth status
```

If not authenticated, tell the user:
```
GitHub CLI is not authenticated. Run: gh auth login
```
Stop.

### Step 2: Locate the Seed

Ouroboros stores seeds in `~/.ouroboros/seeds/`:
- **Interview seeds**: `~/.ouroboros/seeds/{seed_id}.yaml` (YAML)
- **PM seeds**: `~/.ouroboros/seeds/pm_seed_{id}.json` (JSON)

Determine the Seed source in this priority order:

1. **Explicit path argument**: If the user provided a file path (`.yaml` or `.json`), read it directly
2. **Most recent seed file**: Search for the most recent seed in the standard location:
   ```bash
   ls -t ~/.ouroboros/seeds/*.yaml ~/.ouroboros/seeds/*.json 2>/dev/null | head -5
   ```
   If multiple seeds exist, present the top candidates via AskUserQuestion and let the user choose.
3. **Conversation context**: If `ooo seed` or `ooo pm` was just run in this conversation and the seed path was reported, use that path.

If no seed is found:
```
No Seed found. Run `ooo seed` or `ooo pm` first to generate a specification.
```
Stop.

### Step 3: Parse the Seed

Detect the file format by extension and parse accordingly:

**For YAML seeds** (from `ooo interview` + `ooo seed`):
Read the YAML file and extract:
- `goal` → Epic title and description
- `constraints` → Listed in Epic body
- `acceptance_criteria` → Checklist items in Epic + distributed to Task issues
- `ontology_schema` → Documentation section in Epic
- `evaluation_principles` → Quality criteria reference
- `exit_conditions` → Definition of Done
- `metadata.ambiguity_score` → Confidence indicator
- `metadata.seed_id` → Used for duplicate detection

**For JSON seeds** (from `ooo pm`):
Read the JSON file and extract fields using the actual `PMSeed` schema:

| PMSeed field | Maps to |
|-------------|---------|
| `pm_id` | Seed identifier (for duplicate detection) |
| `product_name` | Epic title prefix |
| `goal` | Epic Goal section |
| `constraints` | Epic Constraints section (array of strings) |
| `success_criteria` | Acceptance Criteria checklist (array of strings) |
| `user_stories` | User Stories section (array of `{persona, action, benefit}`) |
| `deferred_items` | Deferred Items section (array of strings) |
| `decide_later_items` | Open Questions section (array of strings) |
| `assumptions` | Assumptions section (array of strings) |

Format user stories as: "As a **{persona}**, I want to **{action}**, so that **{benefit}**."

If any field is missing or empty, omit that section from the Epic body rather than failing.

### Step 4: Detect Repository

**4a. Attempt auto-detection from current directory:**

```bash
gh repo view --json nameWithOwner -q '.nameWithOwner' 2>/dev/null
```

**4b. Present the target repo choice via AskUserQuestion:**

If auto-detection succeeded:
```json
{
  "questions": [{
    "question": "Publish Seed as GitHub Issues to this repository?",
    "header": "Target Repository",
    "options": [
      {"label": "<detected_repo>", "description": "Use current repository"},
      {"label": "Other", "description": "I'll specify a different owner/repo"}
    ],
    "multiSelect": false
  }]
}
```

If auto-detection failed (not in a git repo):
```json
{
  "questions": [{
    "question": "Which GitHub repository should the issues be created in? (format: owner/repo)",
    "header": "Target Repository"
  }]
}
```

If the user chose "Other", ask:
```json
{
  "questions": [{
    "question": "Enter the target repository (format: owner/repo):",
    "header": "Target Repository"
  }]
}
```

Store the resolved repository as `TARGET_REPO`. **All subsequent `gh` commands MUST include `-R <TARGET_REPO>`** to ensure they target the correct repository.

### Step 5: Duplicate Check

Before creating issues, check if this seed was already published:

```bash
gh issue list -R <TARGET_REPO> --label "ouroboros" --state all --search "<seed_id or pm_id>" --limit 5 --json number,title,state
```

The search uses the seed's unique identifier (`metadata.seed_id` for YAML seeds, `pm_id` for JSON seeds). This works because Step 7 persists the identifier in the Epic body (see the `Seed ID` field in the Epic template).

If matching issues are found, warn the user via AskUserQuestion:
```json
{
  "questions": [{
    "question": "Found existing Ouroboros issues that may be from the same seed:\n\n<list of matching issues>\n\nCreate new issues anyway?",
    "header": "Duplicate Warning",
    "options": [
      {"label": "Create anyway", "description": "Proceed with new issues"},
      {"label": "Cancel", "description": "Do not create duplicate issues"}
    ],
    "multiSelect": false
  }]
}
```

If "Cancel": Stop.

### Step 6: Plan Issue Structure

Before creating issues, present the planned structure to the user for review.

**6a. Break down acceptance criteria into Task groups:**

Analyze the acceptance criteria and group them into logical implementation units. Each unit becomes a Task issue. Use your understanding of the domain to create meaningful groupings (e.g., group by feature area, layer, or dependency order).

**6b. Present the plan via AskUserQuestion:**

```json
{
  "questions": [{
    "question": "Here's the planned issue structure:\n\n**Epic**: <goal summary>\n\n**Tasks**:\n1. <task_1_title> — <brief scope>\n2. <task_2_title> — <brief scope>\n3. <task_3_title> — <brief scope>\n\nProceed with creating these issues?",
    "header": "Issue Plan",
    "options": [
      {"label": "Create issues", "description": "Publish to GitHub now"},
      {"label": "Modify plan", "description": "I want to adjust the structure first"}
    ],
    "multiSelect": false
  }]
}
```

If "Modify plan": Ask what to change, adjust, and re-present.

### Step 7: Create GitHub Issues

**IMPORTANT**: Every `gh` command in this step MUST include `-R <TARGET_REPO>`.

**Issue number extraction**: `gh issue create` outputs a URL like `https://github.com/owner/repo/issues/42`. Extract the issue number by parsing the trailing digits:
```bash
EPIC_URL=$(gh issue create -R <TARGET_REPO> --title "..." --label "..." --body "...")
EPIC_NUM=$(echo "$EPIC_URL" | grep -o '[0-9]*$')
```
Apply the same extraction pattern for every Task issue created.

**7a. Create labels (if they don't exist):**

```bash
gh label create "ouroboros" -R <TARGET_REPO> --description "Created by Ouroboros publish" --color "6f42c1" 2>/dev/null || true
gh label create "epic" -R <TARGET_REPO> --description "Epic / parent issue" --color "0075ca" 2>/dev/null || true
gh label create "task" -R <TARGET_REPO> --description "Implementation task" --color "008672" 2>/dev/null || true
```

**7b. Create the Epic issue:**

```bash
gh issue create -R <TARGET_REPO> \
  --title "[Epic] <goal_summary>" \
  --label "ouroboros,epic" \
  --body "$(cat <<'BODY'
## Goal

<goal from seed>

## Constraints

<constraints as bullet list>

## Acceptance Criteria

- [ ] <criterion_1>
- [ ] <criterion_2>
- ...

## Ontology

| Field | Type | Description |
|-------|------|-------------|
| <field_name> | <type> | <description> |

## Evaluation Principles

| Principle | Weight | Description |
|-----------|--------|-------------|
| <name> | <weight> | <description> |

## Exit Conditions

<exit conditions as bullet list>

---

**Seed ID**: `<seed_id or pm_id>` | **Ambiguity Score**: <score> | **Seed**: `<seed_file_path>`
*Generated by [Ouroboros](https://github.com/Q00/ouroboros) via `ooo publish`*
BODY
)"
```

Capture the Epic issue number from the output.

**7c. Create Task issues (one per implementation unit):**

For each task:

```bash
gh issue create -R <TARGET_REPO> \
  --title "[Task] <task_title>" \
  --label "ouroboros,task" \
  --body "$(cat <<'BODY'
Parent: #<epic_number>

## Scope

<what this task covers>

## Acceptance Criteria

- [ ] <specific_criterion_1>
- [ ] <specific_criterion_2>

## Test Checklist

- [ ] <test_1>
- [ ] <test_2>
- [ ] <test_3>

## Pass Criteria

<measurable conditions for this task to be considered done>

---

*Part of [Epic] #<epic_number> | Generated by [Ouroboros](https://github.com/Q00/ouroboros) via `ooo publish`*
BODY
)"
```

**7d. Update Epic with task links:**

After all tasks are created, add a comment to the Epic:

```bash
gh issue comment <epic_number> -R <TARGET_REPO> --body "$(cat <<'BODY'
## Implementation Tasks

- [ ] #<task_1_number> — <task_1_title>
- [ ] #<task_2_number> — <task_2_title>
- [ ] #<task_3_number> — <task_3_title>

Track overall progress by checking off tasks as their issues are closed.
BODY
)"
```

### Step 8: Summary

Present the results:

```
Published to <TARGET_REPO>:

  #<epic>  [Epic] <goal_summary>
    ├── #<task_1>  [Task] <task_1_title>
    ├── #<task_2>  [Task] <task_2_title>
    └── #<task_3>  [Task] <task_3_title>

View: https://github.com/<TARGET_REPO>/issues/<epic>
```

Then suggest next steps:

```
Next steps:
  - Assign tasks to team members on GitHub
  - Use GitHub Projects board for tracking
  - Run `ooo run` for AI-assisted implementation of individual tasks
```

## Notes

- **No MCP required**: This skill works entirely through `gh` CLI
- **Non-destructive**: Creates new issues only, never modifies existing ones
- **Cross-repo support**: All `gh` commands use `-R <TARGET_REPO>`, so seeds can be published to any repository the user has write access to
- **Works with both seed formats**: YAML seeds from `ooo seed` and JSON seeds from `ooo pm` are both supported — the parser auto-detects format by file extension
