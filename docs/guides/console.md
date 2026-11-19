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
| Health | `ouroboros doctor install` checks, and one-click installs of the optional components (`[claude]`, `[litellm]`, `[mcp]`, `[tui]`) pinned to this release. A client integration nobody set up (none of its plugin or launcher files exist) is shown as *Not set up* and counted apart from the warnings; a stale, broken or half-installed one stays a warning. |
| Chat | Interview, Auto and Ask modes with an agent/provider + model picker. *Suggest an answer* drafts a reply from the repository (or a default when the code can't settle it). *Hand off to agents* starts the run and follows it live, then shows the run's worktree, branch and changed files. See [Interview chats](#interview-chats) and [Auto chats](#auto-chats). |
| Connect | Install agent CLIs with their published installers and run `ouroboros setup --runtime <tool>` per tool, optionally keeping the current default agent. |
| Runs | Every execution from the EventStore with a live board per run. When the EventStore's run index fails its consistency check, the page says so instead of showing "No runs yet". |
| Agents & models | Default agent, per-stage agents and models, routing presets. Saves through the same validated, undoable path as `ouroboros config set`; `ouroboros config undo` reverts the last save. |
| Shared MCP | Turn the shared MCP server on, point tools at it and restore them, and start the console at login. |
| Providers | API keys for the LiteLLM backend in `~/.ouroboros/credentials.yaml` (mode 600). A running [opencodex](https://github.com/lidge-jun/opencodex) can be used as the gateway in one click. |

The interface is available in English, Korean, Simplified Chinese, Japanese
and Spanish (side rail → language).

## Interview chats

- **New project or existing project.** A new Interview chat starts as a
  **new project**: the interview reads no folder, whatever directory the
  console was launched from. Pick **Existing project** to have it read the
  folder you choose (a git repository or a folder with a project manifest);
  only then is the interview a brownfield one. Either way, *Hand off to agents*
  runs in a git worktree of the chosen folder, and the interview and Seed
  agents run in that folder (so they load its `CLAUDE.md`, `AGENTS.md` and
  agent settings, not those of the folder the console was started from).
- **The folder must be a git repository with a commit.** The new-chat form says
  under the folder field whether it is one. When it is not, *Hand off to
  agents* explains why and offers *Make this folder a git repository and hand
  off* (or *Add an empty first commit and hand off* for a repository with no
  commits). That runs `git init` when the folder is not a repository, then one
  empty commit, `Start Ouroboros project`; files you staged stay staged. The
  commit uses your git identity, or `Ouroboros <ouroboros@localhost>` for that
  one commit when git has none; no git config is written. A repository that
  already has commits is never changed.
- **Code facts come from the repository.** In an existing project, each
  question the server marks as answerable from code goes to the repository
  investigator first (the one *Suggest an answer* and `ooo auto` use). A found
  answer is shown with its evidence for one-click *Confirm*, *Edit first* or
  *Not right*. An exact match in a manifest, lock file or CI config
  (`pyproject.toml`, `package.json`, `go.mod`, `Dockerfile`, …), all from one
  file, is sent at once and marked *Auto-confirmed from <file>*; *Correct it*
  sends a correction. After three answers in a row that nobody typed, the next
  question goes to you. Questions about what to build (goals, acceptance
  criteria, preferences) always do.
- **A drafted default is in the goal's language.** When the repository doesn't
  settle a question, *Suggest an answer* asks the Interview model for one to
  three short sentences in the language of the chat's goal, read from its
  script (Hangul Korean, kana Japanese, Han Chinese, Cyrillic Russian, else
  English). A reply in another script is drafted once more, and a second miss
  is shown with a note that it may be in a different language.
- **Only installed agents can be picked.** The agent picker greys out agents
  whose CLI is not installed, and the server refuses them before a chat starts,
  with the command that installs them (the Connect page runs it).
- **The meter is the server's gate.** It shows the ambiguity score against the
  server's threshold, each area against the floor the server requires, and the
  streak of qualifying answers it waits for before it completes the interview.
  *Write the spec* becomes the next step when the server completes it; *Write
  the spec now* skips the remaining questions at any time.

## Auto chats

- **Auto works in the folder itself.** `ouroboros auto` writes its changes
  straight into the chosen folder; it does not use a worktree, and it needs no
  git repository. Its job is listed in Activity with the console's other jobs.
- **It reads as a live view, not a terminal.** The console starts
  `ouroboros auto` with `--progress-jsonl` (a file under
  `~/.ouroboros/gui/chats/auto/`) and follows the events it writes:
  - a stepper across the top: Interview, Spec, Run, Verify, Result. Ralph,
    evaluation and the lateral recovery show on Verify. Auto no longer
    reviews or grades the Seed it writes, so there is no review step;
  - the automatic interview: each question, the answer Auto chose, and where
    the answer came from (the goal, the code, an existing convention, a safe
    default, an assumption, out of scope, and so on). The ambiguity the
    interview reports with a question (its `(ambiguity: 0.43)` prefix) is
    kept out of the question text; the card header shows the latest value
    with a small trend line, and each question's value is in its number's
    tooltip. Once the interview is over it folds to one line, which opens;
  - the Seed card the interview uses: the goal, the acceptance criteria and
    constraints, and the Seed's YAML (its file path when it is too long to
    keep in the chat);
  - the run card with its acceptance-criterion tiles, once the run has an
    execution id. Until the run has planned its tasks, which can take
    minutes, the card says it is preparing them. An Auto Seed runs one
    worker attempt for all of its criteria, so every tile shows as executing
    while that attempt runs, and each one settles as its criterion is judged
    after it; the done count moves then.

  The header and side pane name the agent and Execute model the job runs
  on, resolved as `ouroboros auto` resolves them when the chat starts: the
  job's `--runtime` drives every stage (a Claude chat without the Agent SDK
  runs on the Claude Code CLI), and only without one does the configured
  Execute stage apply. For example *Codex CLI default (gpt-6.2-astra)*.

  The raw output stays under *Show detailed log*, with the structlog lines
  hidden until asked for. The view is saved with the chat, and is bounded (the
  last 60 rounds, cut texts). Auto chats started before this view existed show
  their log as before.

  A chat saved by an older console is brought to the current shape when the
  console starts: its progress file is folded again only when it still holds
  the whole saved history, and otherwise the saved view is migrated in place.
  Every round, phase and fact is kept. A chat from an Auto that still
  reviewed its Seed, saved or still running, also shows five steps: its
  review and repair phases sit on Spec, the grades, findings and repair
  rounds it recorded are dropped, and the `grade` and `repair` lines of its
  progress file are read but add nothing.
- **It ends with a result.** When the job ends the chat shows how it ended
  (*Completed* only when its run reported completion; *Not verified* when the
  job succeeded without that report), a link to the run board, the folder,
  and the files `git status` reports as changed there. Those are all
  uncommitted changes in the folder, including any made before Auto started.
  Ouroboros' own state (`.ouroboros/`) and Python bytecode (`__pycache__/`,
  `*.pyc`) are listed apart, under one line that opens, and counted there:
  the stepper says, for example, "3 files (+2 cache)".
  In a folder that is not a git repository the console cannot list them, and
  says so. A chat you stop is shown as stopped, not failed. A stopped, failed
  or unverified chat ends with a line saying where it stopped and where to
  look: the board for a failed criterion, the detailed log for anything
  else. A failed job's exit code is part of that line.

## Models an agent refuses

- **The chat says so in one sentence.** When an agent says it cannot use the
  model it was given, the chat names the agent, the model and the agent's own
  reason, instead of showing the raw error. For example, Codex signed in with
  a ChatGPT account refuses `gpt-6.2-astra`. The raw error stays folded under
  *Details*. Only explicit refusals count: the `model_not_found` code from
  Codex and OpenAI, Claude's "issue with the selected model", the Anthropic and
  Gemini 404s for a model, and the phrases listed in
  `src/ouroboros/providers/model_rejection.py`. Every other failure is shown
  as before.
- **One way on.** In an Ask chat whose model you typed, *Retry with the
  agent's default model* sends the same question again with the model left
  empty. The thread notes the change, and later messages in that chat use the
  default too. The console never changes a model on its own. When the model
  came from **Agents & models**, the chat links to that page instead. That is
  the case for an Interview turn, the spec, *Suggest an answer*, and an Ask
  chat with no model.
- **Codex suggestions come from Codex.** The Codex model list in the Ask model
  field and on **Agents & models** is the list Codex's own picker shows. It is
  read from `models_cache.json` in the Codex home (`$CODEX_HOME`, else
  `~/.codex`). When that file is missing or unreadable, the console uses its
  built-in list. Codex's list is what Codex offers, not what every account can
  use, and both places say so.
- **The agent's own default is offered once.** On **Agents & models** it is
  *Follow the agent*, with the model it currently resolves to. A saved
  `default` also shows as that option. In the Ask model field it is the empty
  field, and the hint names the model that runs when the console knows it.

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
bun run test           # the page's pure rules (src/**/*.test.ts)
OUROBOROS_GUI_TARGET=http://127.0.0.1:10300 bun run dev   # live-reload against a running console
bun run notices        # regenerate public/THIRD_PARTY_NOTICES.txt after changing dependencies
```

Commit the rebuilt `static/` directory with any change to `gui/`; CI rebuilds
it and fails when the two differ.

- Strings live in `gui/src/i18n/{en,ko,zh,ja,es}.ts`; `en.ts` defines the keys
  and the other catalogs are type-checked against it. A word that changes with
  a count is a plural block, `{count, plural, one {# file} other {# files}}`,
  chosen by `Intl.PluralRules` (format in `gui/src/i18n/message.ts`); Korean,
  Japanese and Chinese write one form with a plain `{count}`. `bun test` in
  `gui/` checks every catalog string renders.
- Design tokens (color, type, motion) are in `gui/src/styles/tokens.css`. The
  page uses system fonts only and ships no web fonts or vendor logos.
- Licenses of the bundled packages are in
  `gui/public/THIRD_PARTY_NOTICES.txt` and are served with the page.
