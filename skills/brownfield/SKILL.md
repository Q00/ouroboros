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

1. Load the brownfield MCP tool:
   - **If `ToolSearch` is not available** (Cursor, other runtimes): MCP tools are already loaded. Skip to step 2.
   - **If `ToolSearch` is available** (Claude Code): `ToolSearch query: "+ouroboros brownfield"`
2. Call scan+register:
   ```
   Tool: ouroboros_brownfield
   Arguments: { "action": "scan" }
   ```
   This scans `~/` for GitHub repos and registers them in DB. Existing defaults are preserved.

The scan response `text` already contains a pre-formatted numbered list with `[default]` markers. **Do NOT make any additional MCP calls to list or query repos.**

**Display the repos in a plain-text 2-column grid** (NOT a markdown table). Use a code block so columns align. Example:

```
Scan complete. 8 repositories registered.

 1. repo-alpha                   5. repo-epsilon
 2. repo-bravo *                 6. repo-foxtrot
 3. repo-charlie                 7. repo-golf *
 4. repo-delta                   8. repo-hotel
```

Include `*` markers for defaults exactly as they appear in the scan response.

**If no repos found**, show:
```
No GitHub repositories found in your home directory.
```
Then stop.

**Step 1.5: Load Question Tool**

**If `ToolSearch` is not available** (Cursor, other runtimes): `AskUserQuestion` is already loaded. Use it directly.

**If `ToolSearch` is available** (Claude Code):
```
ToolSearch query: "select:AskUserQuestion"
```
Store whichever tool becomes available (`AskUserQuestion` or `AskQuestion`) as the **question tool**. If neither is available, present choices as numbered markdown options.

**Step 2: Default Selection**

**IMMEDIATELY after showing the list**, ask using the **question tool**:
- Prompt: `Which repos should be the default interview context?`
- Options: `Use current defaults (<current default numbers>)`, `Use no default repos`, `Enter custom repo numbers`
- Include the `custom` choice on Cursor.

The user can select the recommended defaults, choose "None", or enter custom numbers. In Claude Code, the user may type custom numbers directly. In Cursor, if the user picks `custom`, immediately ask: `Enter repo numbers like '6, 18, 19'.`

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
