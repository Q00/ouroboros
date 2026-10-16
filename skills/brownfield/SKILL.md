---
name: brownfield
description: "Scan and manage brownfield repository/worktree defaults for interviews"
---

# /ouroboros:brownfield

Scan a root directory for existing git repositories and linked worktrees, then manage the default repos used as interview context.

```
ooo brownfield                # Scan repos and set defaults
ooo brownfield scan           # Scan only (no default selection)
ooo brownfield defaults       # Show current defaults
ooo brownfield set 6,18,19   # Set defaults by repo numbers
ooo brownfield detect [path]  # Author mechanical.toml via one AI call
```

## Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros brownfield"` for `ouroboros_brownfield`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. If it is genuinely absent, stop and say the Ouroboros MCP server is not available. Scan, defaults, and set use only this MCP tool, never the CLI or Python scripts.

## Default flow (`ooo brownfield`)

### Step 1: Scan

```
Tool: ouroboros_brownfield
Arguments: { "action": "scan" }
```

Scan boundaries:
- The walk starts at `scan_root` (default: the user's home directory) and goes at most two directory levels deep (`~/repo` and `~/group/repo` are found; deeper nesting is not).
- Dot-prefixed and noisy directories such as `node_modules` are not walked.
- Normal repos (`.git` directory) and linked worktrees (`.git` file) are registered self-only when the walk reaches them. Git worktree families are not expanded, so worktrees outside the depth-bounded walk (e.g. under `.ouroboros/worktrees`) are not pulled in.
- Repos without remotes or with remotes not named `origin` are eligible. Existing defaults are preserved.

The scan response `text` already holds the numbered list with ` *` markers on defaults; make no further MCP calls to list or query repos. Show it as a plain-text two-column grid in a code block (not a markdown table), keeping the `*` markers exactly:

```
Scan complete. 8 repositories registered.

 1. repo-alpha                   5. repo-epsilon
 2. repo-bravo *                 6. repo-foxtrot
 3. repo-charlie                 7. repo-golf *
 4. repo-delta                   8. repo-hotel
```

If nothing is found, say "No git repositories or worktrees found." and stop.

### Step 2: Default selection

**Do NOT use the active runtime's `ask_user` dialog for this selection**: text between tool calls may not render, and dialog previews truncate long lists. Instead, **end the turn with the repo grid as the final message** and collect the selection as a plain chat reply. Directly below the grid, append:

- With defaults: `Current defaults: <names> (numbers <numbers>)`, then "Reply with repo numbers to change defaults (e.g. "6, 18, 19"), "keep" to keep the current defaults, or "none" to clear them."
- Without defaults: `No defaults set.`, then "Reply with repo numbers to set defaults (e.g. "6, 18, 19"), or "none" to run interviews in greenfield mode."

Make no tool calls after the grid; the breadcrumb footer is still the last line.

Next turn, parse the reply: numbers (any separator) → those indices; "keep" → confirm defaults unchanged, no MCP call; "none" → empty indices; anything else → ask again in plain text, never guess. Then update all defaults in one call (it replaces every existing default):

```
Tool: ouroboros_brownfield
Arguments: { "action": "set_defaults", "indices": "<comma-separated IDs>" }
```

For example `{ "action": "set_defaults", "indices": "6,18,19" }`, or `"indices": ""` for "none".

### Step 3: Confirm

Name the new defaults and that they will be used as interview context; for "none", say interviews run in greenfield mode and `ooo brownfield` can set defaults later.

## Subcommands

- **`scan`**: Step 1 only; show the grid and stop.
- **`defaults`**: run the Step 1 scan call and show only the `*` repos, or "No default repos set. Run 'ooo brownfield' to configure."
- **`set <indices>`**: parse the comma-separated indices and call `{ "action": "set_defaults", "indices": "<indices>" }` without scanning; confirm the updated defaults.
- **`detect [path]`**: one AI call against the target directory (default: cwd) writes `.ouroboros/mechanical.toml` with validated lint/build/test/static/coverage commands, which Stage 1 of evaluation reads verbatim. Evaluation auto-runs it when no toml exists, so run it explicitly only to pre-author, refresh after a build-tool change (`--force`), or review commands before Stage 1 trusts them. This subcommand uses the CLI through the active runtime's `run_shell` capability:

  ```
  uvx --python '>=3.12' --from ouroboros-ai ouroboros detect [path]
  # or, if already installed:
  ouroboros detect [path] [--force]
  ```

  Print the resulting toml so the user can confirm or edit it. If detect reports "could not propose any verifiable commands", surface the reason (no manifests, LLM unavailable, every proposal dropped) and suggest a minimal hand-written toml; one entry such as `test = "pytest -q"` opts that check back in to Stage 1.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
