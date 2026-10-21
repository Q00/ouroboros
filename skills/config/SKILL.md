---
name: ouroboros-config
description: "Open or drive the Ouroboros settings GUI (browser, TUI, or conversational fallback)"
---

# /ouroboros:ouroboros-config

Edit `~/.ouroboros/config.yaml`: global and per-stage runtime and model choices, the LLM backend, install badges for missing CLIs, and env-override warnings.

```
ooo config
/ouroboros:ouroboros-config
```

## Instructions

Pick the branch by one question: **can the user open a browser pointed at this machine?** Run commands with the active runtime's `run_shell` capability.

### Branch A: local harness on the user's own machine

1. Launch in the background (it serves until stopped):

   ```bash
   if command -v ouroboros >/dev/null 2>&1; then
     ouroboros config
   else
     uvx --python '>=3.12' --from 'ouroboros-ai[tui]' ouroboros config
   fi
   ```

   In a non-interactive context it serves the settings app on a local web server and opens the browser. The `uvx` fallback covers a plugin-only install where `ouroboros` is not on `PATH`; in a development checkout use `uv run ouroboros config`.

2. Relay the `http://localhost:<port>` line in case the browser did not open.

3. Tell the user: edit, Save, then ask you to stop the server. A running MCP server may need a reconnect to pick up backend changes. `ooo config` reopens the settings any time; a saved model choice is never locked.

### Branch B: remote host the user can reach (SSH box, home server)

```bash
ouroboros config --web --host 0.0.0.0 --no-browser
```

Relay the printed URL with this host's address substituted, plus the SSH tunnel fallback it prints (`ssh -L <port>:localhost:<port> <this-host>`).

### Branch C: chat gateway with no browser path (e.g. hermes driven from Discord)

Do not start a server nobody can reach. Drive the same settings in chat:

1. Show the current state with `ouroboros config show`.
2. Offer a short menu with current values (default agent, per-stage agents, and models: `auto`, a tier, or a model id for every role) and ask what to change.
3. Apply each choice with the validated setter (same write path as the GUI):

   ```bash
   ouroboros config set orchestrator.runtime_backend <agent>
   ouroboros config set orchestrator.runtime_profile.stages.<interview|execute|evaluate|reflect> <agent>
   ouroboros config set models.default <auto|frugal|standard|frontier|model>  # every role
   ouroboros config set models.pin true                            # run the per-role ids below
   ouroboros config set clarification.default_model <model>        # interview & seed
   ouroboros config set execution.default_model <model>            # execute
   ouroboros config set evaluation.semantic_model <model>          # evaluate
   ouroboros config set resilience.reflect_model <model>           # reflect
   ouroboros config set llm.backend <backend>                      # internal LLM calls
   ```

4. Confirm with `ouroboros config show` and summarize what changed.

### All branches

Relay any rejected `set` validation error or missing-dependency hint (`pip install 'ouroboros-ai[tui]'`) verbatim. `ouroboros config undo` restores the configuration saved before the last change.

## RFC #1392 State Breadcrumb Footer

End the final response with exactly one line `◆ <current state> → next: <recommended action>`. Derive the state from live session state via `ouroboros_session_status` when available, otherwise from this skill's actual outcome. Never use a linear `Step N of M` footer; when the next step is a real choice, list 2-3 options.
