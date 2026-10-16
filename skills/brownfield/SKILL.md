---
name: brownfield
description: "Scan and manage brownfield repository/worktree defaults for interviews"
---

# /ouroboros:brownfield

Manage the registry of existing git repositories and linked worktrees, and the default repos used as interview context. The registry lives in the Ouroboros DB: listing, defaults, and search read it and never walk the filesystem. Only `scan` walks the disk.

```
ooo brownfield                # List registered repos and set defaults (scans only if none are registered)
ooo brownfield scan           # Rescan the scan root and refresh the search index
ooo brownfield defaults       # Show current defaults
ooo brownfield find <topic>   # Rank registered repos by relevance to a topic
ooo brownfield set 6,18,19    # Set defaults by repo numbers
ooo brownfield detect [path]  # Author mechanical.toml via one AI call
```

## Loading MCP tools (deferred-schema guard)

Ouroboros tools may be deferred, and a schema loaded on one turn can unload by the next. Immediately before each `ouroboros_*` call, load it with the active runtime's tool-discovery capability: `"+ouroboros brownfield"` for `ouroboros_brownfield`. A call whose schema is not loaded fails with "Invalid tool parameters". If the tool is already callable, discovery is a no-op and an empty result is expected. If it is genuinely absent, stop and say the Ouroboros MCP server is not available. Listing, scan, find, defaults, and set use only this MCP tool, never the CLI or Python scripts.

## Default flow (`ooo brownfield`)

### Step 1: List the registry

```
Tool: ouroboros_brownfield
Arguments: { "action": "query" }
```

If `meta.total` is 0, nothing is registered yet: run the scan below instead and use its response. Otherwise do not scan; the user runs `ooo brownfield scan` when repos were added or moved.

```
Tool: ouroboros_brownfield
Arguments: { "action": "scan" }
```

Scan boundaries:
- The walk starts at `scan_root` (default: the user's home directory) and goes at most two directory levels deep (`~/repo` and `~/group/repo` are found; deeper nesting is not).
- Dot-prefixed and noisy directories such as `node_modules` are not walked.
- Normal repos (`.git` directory) and linked worktrees (`.git` file) are registered self-only when the walk reaches them. Git worktree families are not expanded, so worktrees outside the depth-bounded walk (e.g. under `.ouroboros/worktrees`) are not pulled in.
- Repos without remotes or with remotes not named `origin` are eligible. Existing defaults are preserved; repos no longer on disk are not removed.
- A rescan skips the `git` check for already-registered repos and rebuilds a repo's search document (manifest name and description, README head, top-level entry names) only when those files changed.

The query or scan response `text` already holds the numbered `{id}. {name}` list with ` *` markers on defaults (query lines also carry `: {path} - {desc}`); make no further MCP calls to list repos. Show the numbers and names (never the scores or paths) as a plain-text two-column grid in a code block (not a markdown table), keeping the `*` markers exactly:

```
Scan complete. 8 repositories registered.

 1. repo-alpha                   5. repo-epsilon
 2. repo-bravo *                 6. repo-foxtrot
 3. repo-charlie                 7. repo-golf *
 4. repo-delta                   8. repo-hotel
```

If the scan finds nothing, say "No git repositories or worktrees found." and stop.

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

- **`scan`**: run the Step 1 scan call (even when repos are registered); show the grid and stop.
- **`defaults`**: run the Step 1 query call (no scan) and show only the `*` repos, or "No default repos set. Run 'ooo brownfield' to configure."
- **`find <topic>`**: rank the registered repos for the topic, without scanning:

  ```
  Tool: ouroboros_brownfield
  Arguments: { "action": "query", "text": "<topic>" }
  ```

  Show the returned lines best first (number, name, `*` marker, score; the top 10 unless the user asks for more, then pass `limit`) and offer `ooo brownfield set <numbers>`. Ranking is lexical (BM25 over name, description, path, manifest, README head, and top-level entries), so it matches words, not meaning: if nothing matches, say so and suggest other keywords or `ooo brownfield` for the full list. When the response says some repos have no search document, mention that `ooo brownfield scan` indexes them.
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
