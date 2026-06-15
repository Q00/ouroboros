---
name: config
description: "Open or drive the Ouroboros settings GUI (browser, TUI, or conversational fallback)"
---

# /ouroboros:config

Settings for `~/.ouroboros/config.yaml`: per-stage runtime/model selects,
global runtime + LLM backend, install badges for missing CLIs, and
env-override warnings.

## Usage

```
ooo config
/ouroboros:config
```

**Trigger keywords:** "ooo config", "open settings", "configure ouroboros", "change model", "change agent"

## Instructions

Pick the branch that matches where you (the agent) are running. The decisive
question: **can the user open a browser pointed at this machine?**

### Branch A — local harness (Claude Code / Codex on the user's own machine)

1. Launch in the background (the command serves until stopped):

   ```bash
   ouroboros config
   ```

   The command detects the non-interactive context itself and serves the
   settings app over a local web server, auto-opening the user's browser.
   In a development checkout use `uv run ouroboros config`.

2. Relay the `http://localhost:<port>` line from the output so the user can
   open it manually if the browser did not pop up.

3. Tell the user: edit → Save → then ask you to stop the server. Remind them
   a running MCP server may need a reconnect to pick up backend changes.

### Branch B — remote host the user can reach over the network (SSH box, home server)

The user cannot see a browser opened *here*, but may be able to reach this
host. Serve without auto-open and hand over the URL:

```bash
ouroboros config --web --host 0.0.0.0 --no-browser
```

Relay the printed URL with this host's address substituted, plus the SSH
tunnel fallback the command prints
(`ssh -L <port>:localhost:<port> <this-host>`).

### Branch C — chat gateway, no browser path at all (e.g. hermes driven from Discord)

Do NOT start a server nobody can reach. Drive the same settings
conversationally over the scriptable surface:

1. Show the current state:

   ```bash
   ouroboros config show
   ```

2. Present the user a short menu in chat — default agent, per-stage agents,
   per-stage models — with the current values, and ask what to change.

3. Apply each choice with the validated setter (same write path as the GUI):

   ```bash
   ouroboros config set orchestrator.runtime_backend <agent>
   ouroboros config set orchestrator.runtime_profile.stages.<interview|execute|evaluate|reflect> <agent>
   ouroboros config set clarification.default_model <model>        # interview & seed
   ouroboros config set evaluation.semantic_model <model>          # evaluate
   ouroboros config set resilience.reflect_model <model>           # reflect
   ouroboros config set llm.backend <backend>                      # internal LLM calls
   ```

4. Confirm with `ouroboros config show` and summarize what changed.

If a `set` is rejected, relay the validation error verbatim — it lists the
valid keys/values.

### All branches

If the command fails with a missing-dependency hint, relay it verbatim
(`pip install 'ouroboros-ai[tui]'`). Scriptable edits always remain on
`ouroboros config show|set|backend|init|validate`.

End your final message with the state breadcrumb footer (RFC #1392), e.g.:

```
◆ Settings GUI serving at <url> → next: Save in browser, then stop the server
◆ Config updated via chat (<keys>) → next: reconnect MCP if the backend changed
```
