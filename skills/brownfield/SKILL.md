---
name: brownfield
description: "Scan and manage brownfield repository defaults for interviews"
---

# /ouroboros:brownfield

Scan your home directory for existing git repositories and manage default repos used as context in interviews.

## Usage

```
ooo brownfield                # Scan repos and set defaults
ooo brownfield scan           # Scan only (no default selection)
ooo brownfield defaults       # Show current defaults
ooo brownfield set 6,18,19   # Set defaults by repo numbers
```

**Trigger keywords:** "brownfield", "scan repos", "default repos", "brownfield scan"

---

## How It Works

### Default flow (`ooo brownfield` with no args)

**Step 1: Scan**

Show scanning indicator:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Scanning for Existing Projects...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Looking for git repositories in your home directory.
Only GitHub-hosted repos will be registered.
This may take a moment...
```

**Implementation — use MCP tools only, do NOT use CLI or Python scripts:**

1. Load the brownfield MCP tool: `ToolSearch query: "+ouroboros brownfield"`
2. Call scan+register:
   ```
   Tool: ouroboros_brownfield
   Arguments: { "action": "scan" }
   ```
   This scans `~/` for GitHub repos and registers them in DB. Existing defaults are preserved.

The scan response `text` already contains a pre-formatted numbered list with `[default]` markers. **Do NOT make any additional MCP calls to list or query repos.**

**Display the repos in a compact 2-column grid** so the user can scan all repos quickly. Format the scan response `text` as two side-by-side columns. Include `*` markers for defaults exactly as they appear in the response.

**If no repos found**, show:
```
No GitHub repositories found in your home directory.
```
Then stop.

**Step 2: Default Selection**

**IMMEDIATELY after showing the list**, use `AskUserQuestion` with the current default numbers from the scan response:

```json
{
  "questions": [{
    "question": "Which repos to set as default for interviews? Enter numbers like '6, 18, 19'.",
    "header": "Default Repos",
    "options": [
      {"label": "<current default numbers> (Recommended)", "description": "<current default names>"},
      {"label": "None", "description": "No default repos — interviews will run in greenfield mode"}
    ],
    "multiSelect": false
  }]
}
```

The user can select the recommended defaults, choose "None", or type custom numbers.

After the user responds, use ONE MCP call to update all defaults at once:

```
Tool: ouroboros_brownfield
Arguments: { "action": "set_defaults", "indices": "<comma-separated IDs>" }
```

Example: if the user picks IDs 6, 18, 19 → `{ "action": "set_defaults", "indices": "6,18,19" }`

This clears all existing defaults and sets the selected repos as default in one call.

If "None" → `{ "action": "set_defaults", "indices": "" }` to clear all defaults.

**Step 3: Confirmation**

```
Brownfield defaults updated!
Defaults: grape, podo-app, podo-backend

These repos will be used as context in interviews.
```

Or if "None" selected:
```
No default repos set. Interviews will run in greenfield mode.
You can set defaults anytime with: ooo brownfield
```

---

### Subcommand: `scan`

Scan only, no default selection prompt. Show the numbered list and stop.

---

### Subcommand: `defaults`

Load the brownfield MCP tool and call:
```
Tool: ouroboros_brownfield
Arguments: { "action": "scan" }
```

Display only the repos marked with `*` (defaults). If none, show:
```
No default repos set. Run 'ooo brownfield' to configure.
```

---

### Subcommand: `set <indices>`

Directly set defaults without scanning. Parse the comma-separated indices from the user's input and call:

```
Tool: ouroboros_brownfield
Arguments: { "action": "set_defaults", "indices": "<indices>" }
```

Show confirmation with updated defaults.
