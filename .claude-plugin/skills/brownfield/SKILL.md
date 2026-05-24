---
name: brownfield
description: "Scan and manage brownfield repository/worktree defaults for interviews"
---

# /ouroboros:brownfield

Scan a root directory for existing git repositories and linked worktrees, then manage default repos used as context in interviews.

## Usage

```
ooo brownfield                # Scan repos and set defaults
ooo brownfield scan           # Scan only (no default selection)
ooo brownfield defaults       # Show current defaults
ooo brownfield set 6,18,19   # Set defaults by repo numbers
ooo brownfield detect [path]  # Author mechanical.toml via one AI call
```

**Trigger keywords:** "brownfield", "scan repos", "default repos", "brownfield scan", "mechanical detect"

---

## How It Works

### Default flow (`ooo brownfield` with no args)

**Step 1: Scan**

Show scanning indicator:
```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Scanning for Existing Projects...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Looking for git repositories and worktrees under the scan root directory.
Linked worktrees reported by discovered normal repo roots may also be registered, even outside the scan root directory.
Local repos and repos with any remote name are eligible.
This may take a moment...
```

**Implementation — use MCP tools only, do NOT use CLI or Python scripts:**

1. Load the brownfield MCP tool: `ToolSearch query: "+ouroboros brownfield"`
2. Call scan+register:
   ```
   Tool: ouroboros_brownfield
   Arguments: { "action": "scan" }
   ```
   This walks `scan_root` for valid seed repos/worktrees and registers them in DB. For each discovered normal repo root with a `.git` directory, Git-reported linked worktrees are also considered, even when they live outside `scan_root`. A linked worktree found under `scan_root` with a `.git` file is registered itself, but it is not used to register its main worktree or sibling worktrees outside `scan_root`. Existing defaults are preserved.

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
No git repositories or worktrees found.
```
Then stop.

### Scan boundaries

- The filesystem walk starts at `scan_root`; when omitted, `scan_root` defaults to the current user's home directory.
- Repositories are only discovered directly by walking directories inside `scan_root`.
- Dot-prefixed directories and known noisy directories such as `node_modules` are not walked as seed locations.
- Git worktrees are different: once a normal repo root with a `.git` directory is discovered, Ouroboros runs `git worktree list --porcelain` and may register those linked worktrees even if their paths are outside `scan_root`.
- A linked worktree found inside `scan_root` with a `.git` file is registered itself, but it is not used to register its main worktree or sibling worktrees outside `scan_root`.
- Local repos, repos without remotes, and repos whose remotes are not named `origin` are all eligible.

**Step 2: Default Selection**

**IMMEDIATELY after showing the list**, use `AskUserQuestion` with the current default numbers from the scan response.

**If defaults exist**, show them as the recommended option:

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

**If no defaults exist**, do NOT show a "(Recommended)" option — offer "None" and "Select repos" instead:

```json
{
  "questions": [{
    "question": "Which repos to set as default for interviews? Enter numbers like '6, 18, 19'.",
    "header": "Default Repos",
    "options": [
      {"label": "None", "description": "No default repos — interviews will run in greenfield mode"},
      {"label": "Select repos", "description": "Type repo numbers to set as default"}
    ],
    "multiSelect": false
  }]
}
```

The user can select the recommended defaults (if any), choose "None", or type custom numbers.

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

---

### Subcommand: `detect [path]`

Runs one AI call against the target directory (defaults to the user's cwd)
and writes `.ouroboros/mechanical.toml` with validated lint / build / test /
static / coverage commands. Stage 1 of evaluation reads this file verbatim,
so the toml is the authoritative Stage 1 contract — no hardcoded language
presets exist anymore.

Ouroboros auto-runs this detect the first time `ouroboros_evaluate` is
invoked without a toml present, so most users never need to call it
directly. Run it explicitly when:

- you want to pre-author the toml before the first evaluate,
- you moved to a new build tool and want to refresh (`--force`),
- you want to review/edit the commands before Stage 1 trusts them.

**Implementation:** invoke the CLI via Bash.

```
uvx --from ouroboros-ai ouroboros detect [path]
# or, if already installed:
ouroboros detect [path] [--force]
```

Then print the resulting `.ouroboros/mechanical.toml` contents so the user
can confirm the proposed commands or hand-edit them.

If detect reports "could not propose any verifiable commands", surface the
reason (no manifests found, LLM unavailable, every proposal dropped) and
suggest the user write a minimal toml by hand — any single entry like
`test = "pytest -q"` is enough to opt back in to Stage 1 for that check.
