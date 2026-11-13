# Ouroboros console (`ouroboros gui`)

A local web app for everything you would otherwise do with `ooo setup`,
`ooo config`, `ouroboros status` and the run dashboard — plus a chat that runs
the interview → spec → run → evaluate pipeline. It ships inside the Python
package; there is nothing extra to install.

```bash
ouroboros gui                 # opens http://localhost:10300
ouroboros gui --page models   # jump to a page
ouroboros gui --host 0.0.0.0  # reach it from another machine (prints a token URL)
ouroboros service install     # keep it running in the background at login
```

`ooo config` opens the same console on its **Agents & models** page when it
runs inside an agent session; in a plain terminal it still opens the terminal
settings app.

## Pages

| Page | What it does |
| --- | --- |
| Home | Version, default agent, connected tools, recent runs, a setup checklist. |
| Health | `ouroboros doctor install` checks, and one-click installs of the optional components (`[claude]`, `[litellm]`, `[mcp]`, `[tui]`) pinned to this release. |
| Chat | Interview, Auto and Ask modes with an agent/provider + model picker. *Suggest an answer* drafts a reply from the repository (or a default when the code can't settle it). *Hand off to agents* starts the run and follows it live, then shows the run's worktree, branch and changed files. |
| Connect | Install agent CLIs with their published installers and run `ouroboros setup --runtime <tool>` per tool, optionally keeping the current default agent. |
| Runs | Every execution from the EventStore with a live board per run. |
| Agents & models | Default agent, per-stage agents and models, routing presets. Saves through the same validated, undoable path as `ouroboros config set`; `ouroboros config undo` reverts the last save. |
| Shared MCP | Turn the shared MCP server on, point tools at it and restore them, and start the console at login. |
| Providers | API keys for the LiteLLM backend in `~/.ouroboros/credentials.yaml` (mode 600). A running [opencodex](https://github.com/lidge-jun/opencodex) can be used as the gateway in one click. |

The interface is available in English, Korean, Simplified Chinese, Japanese
and Spanish (side rail → language).

## Background service and the shared MCP server

Every MCP host normally starts its own `ouroboros mcp serve` per session. The
console can instead run **one** `streamable-http` server on
`http://127.0.0.1:8765/mcp` and supervise it: it restarts the server when it
exits and stops it with its whole process group, so nothing is left behind.

- **Shared MCP** turns the shared server on and
  points Codex, Kiro, Copilot, Gemini CLI or Cursor at it. The previous
  Ouroboros entry of each tool is saved in `~/.ouroboros/gui/mcp-wiring.json`
  and restored when you turn sharing off; other servers in those files are not
  touched.
- **Start at login** (`ouroboros service install`) registers a launchd agent
  (`~/Library/LaunchAgents/ai.ouroboros.console.plist`) or a systemd user unit
  (`ouroboros-console.service`). It restarts after a crash but not after a
  deliberate stop. `ouroboros service uninstall` removes it.
- Claude Code loads Ouroboros from its plugin, so Shared MCP leaves its
  configuration alone.

## Security

The server binds to loopback by default. Every `/api` call needs the
per-launch token embedded in the page, requests must name a loopback `Host`
(no DNS rebinding) and cross-origin `Origin`s are refused. With
`--host 0.0.0.0` the page itself also requires the token, which `ouroboros gui`
prints in the URL. Provider keys are never sent back to the page — only
whether one is set and its last four characters.

## Building the page

The page is an original front end written for Ouroboros. Its source lives in
`gui/` (React, TypeScript, Vite, hand-written CSS; Radix UI primitives, Motion
and Lucide icons) and builds into `src/ouroboros/gui/static`, which the wheel
ships and `ouroboros gui` serves:

```bash
cd gui
bun install            # or npm install
bun run build          # type-checks, then writes ../src/ouroboros/gui/static
OUROBOROS_GUI_TARGET=http://127.0.0.1:10300 bun run dev   # live-reload against a running console
bun run notices        # regenerate public/THIRD_PARTY_NOTICES.txt after changing dependencies
```

Commit the rebuilt `static/` directory with any change to `gui/`; CI rebuilds
it and fails when the two differ.

- Strings live in `gui/src/i18n/{en,ko,zh,ja,es}.ts`; `en.ts` defines the keys
  and the other catalogs are type-checked against it.
- Design tokens (color, type, motion) are in `gui/src/styles/tokens.css`. The
  page uses system fonts only and ships no web fonts or vendor logos.
- Licenses of the bundled packages are in
  `gui/public/THIRD_PARTY_NOTICES.txt` and are served with the page.
