---
name: config
description: "Open the Ouroboros settings GUI (browser from a harness, TUI from a terminal)"
---

# /ouroboros:config

Open the mouse-friendly settings GUI for `~/.ouroboros/config.yaml`: per-stage
runtime/model selects, global runtime + LLM backend, install badges for
missing CLIs, and env-override warnings.

## Usage

```
ooo config
/ouroboros:config
```

**Trigger keywords:** "ooo config", "open settings", "configure ouroboros"

## Instructions

When the user invokes this skill:

1. **Launch the GUI in the background** (the command serves until stopped):

   ```bash
   ouroboros config
   ```

   Run it with the Bash tool in background mode. Inside a harness session
   the command detects the non-interactive context (`CLAUDECODE=1` /
   captured stdout) by itself and serves the settings app over a local web
   server, auto-opening the user's browser.

   In a development checkout, use `uv run ouroboros config` instead.

2. **Relay the URL.** Read the command output and surface the
   `http://localhost:<port>` line so the user can open it manually if the
   browser did not pop up.

3. **Tell the user how to finish:** edit settings in the browser, press
   Save, then stop the server (kill the background command) when done.
   Config changes apply to *new* MCP work; remind the user that a running
   MCP server may need a reconnect to pick up backend changes.

4. If the command fails with a missing-dependency hint, relay it verbatim
   (`pip install 'ouroboros-ai[tui]'`).

Scriptable edits stay on the existing surface: `ouroboros config show|set|backend|init|validate`.

End your final message with the state breadcrumb footer (RFC #1392):

```
◆ Settings GUI serving at <url> → next: Save in browser, then stop the server
```
