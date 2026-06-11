# RFC — Journey transparency: state breadcrumb + TUI surfacing

> Status: **Draft**
> Relates to [discussion #1376](https://github.com/Q00/ouroboros/discussions/1376)
> (usability & transparency theme). Sibling theme:
> [discussion #1377](https://github.com/Q00/ouroboros/discussions/1377) (token
> frugality — out of scope here).

## Summary

Using Ouroboros end-to-end can feel like a black box: at any moment it is
unclear what is happening now, what the recommended next action is, and how to
watch a run without already knowing the TUI exists. This RFC proposes the
first slice of the transparency theme from discussion #1376:

1. **A state breadcrumb contract** — every `ooo` skill ends its turn with a
   one-line "current state + recommended next action" footer, derived from
   live session state rather than a hardcoded step number.
2. **`ouroboros tui open`** — a CLI command that detects the user's terminal
   emulator and spawns the existing TUI monitor in a new window, with an
   honest fallback when no window can be opened.
3. **TUI auto-offer on `ooo run`** — the run skill offers (or, when
   configured, automatically launches) the TUI at job start.

It also records a positioning decision: the richer always-on cockpit
(browser dashboard, few-click parity) belongs to **`ourocode`**, the shell
whose job is to make Ouroboros seamless overall. The core ships the TUI and
the surfacing hooks the shell builds on.

## Context

### The TUI is already a decoupled observer

The TUI does not attach to a running process. It polls the shared event
store and renders whatever sessions it finds:

- `ouroboros tui monitor` connects to `~/.ouroboros/ouroboros.db` and runs
  `OuroborosTUI(event_store=...)` (`src/ouroboros/cli/commands/tui.py`).
- `ooo run` starts a background MCP job and relays compact progress in the
  main session (`skills/run/SKILL.md`, "Recommended monitoring stance").

Two consequences:

1. **No wiring is needed between a run and the TUI.** Any TUI instance
   launched in another terminal window immediately sees the run.
2. **The TUI is runtime-agnostic.** It renders the event store, not a
   particular agent runtime. A run driven from Claude Code, Hermes, Codex, or
   any other runtime that writes through the MCP server is equally visible.
   Surfacing the TUI is therefore a cross-runtime transparency win, not a
   Claude-Code-only feature.

### Why nobody finds it

Nothing in the default journey mentions the TUI. `ooo run` keeps progress
in-session; the TUI is reachable only by users who already read the CLI
reference. Discussion #1376 reports exactly this: a first-time user with no
"you are here" signal and no discoverable monitor.

### Why a linear "Step 2 of 5" breadcrumb is the wrong shape

Ouroboros is an evolutionary loop (interview → seed → run → evaluate →
evolve → …), not a pipeline. A fixed step counter would lie as soon as the
user re-enters the loop (second generation, re-interview after drift, QA
iterations). The breadcrumb must therefore be **state-derived**: report where
the session actually is and what the recommended next action is, never a
position in a fixed sequence.

## Proposal

### 1. State breadcrumb contract

Every `ooo` skill ends its final message with a footer of the form:

```
◆ <current state> → next: <recommended action>
```

Examples:

```
◆ Seed `auth-service` validated → next: `ooo run`
◆ Generation 2 evaluating (3/7 ACs passed) → next: wait, or `ooo status` for detail
◆ No active session → next: `ooo interview` to start, or `ooo resume-session`
```

Contract requirements:

- **State-derived, not scripted.** The footer is computed from live session
  state (the `session_status` MCP projection where available; the skill's own
  outcome otherwise) — never a hardcoded "step N of M".
- **One line, always last.** Skills may say more above it, but the footer is
  the stable, scannable element users learn to look for.
- **Honest about ambiguity.** When the next action is genuinely the user's
  choice (e.g. evolve vs. accept), the footer lists the 2–3 options rather
  than inventing a single answer.

This is a skill-text contract, not new infrastructure: each `skills/*/SKILL.md`
gains a closing-footer instruction, and the welcome/help skills document the
glyph so users know what it means.

### 2. `ouroboros tui open` — terminal-aware TUI launcher

A new CLI command encapsulating "open the TUI in a new window of the user's
own terminal", so skills can invoke one line instead of embedding
platform-specific spawn logic in prose:

```
ouroboros tui open [--db-path PATH]
```

Behavior:

1. **Detect the terminal.** `$TERM_PROGRAM` is the primary signal — set by
   the emulator that owns the session and inherited by the agent runtime.
   Verified values: `ghostty`, `iTerm.app`, `Apple_Terminal`, `WezTerm`,
   `vscode`. On Linux, fall back to probing `gnome-terminal`,
   `x-terminal-emulator`, etc.
2. **Spawn via a per-terminal dispatch table.** Verified on macOS:
   - Ghostty: `open -na Ghostty.app --args --working-directory=<dir> -e <cmd>`
   - Apple Terminal: `osascript … do script "<cmd>"`
   - iTerm2: `osascript … create window with default profile command "<cmd>"`
3. **Resolve the right invocation.** Prefer the installed `ouroboros`
   entrypoint; fall back to `uvx --from 'ouroboros-ai[tui]' ouroboros tui
   monitor` when the TUI extra is missing (the existing ImportError path in
   `cli/commands/tui.py` already prints this hint).
4. **Fail honestly.** In a headless/SSH session or with an unknown emulator,
   do not guess: print the exact command for the user to run in another
   terminal, and exit 0 (advisory, not an error).

Two pitfalls found while prototyping, which the command must own so skills
don't have to:

- A spawned window starts in `$HOME`; the command must set the working
  directory (or use absolute paths) so `uv run` resolves the project
  environment that actually has the `tui` extra installed.
- Shell-wrapper quoting (`zsh -ic '…'`) does not survive `open --args`
  argument splitting; commands must be passed as argv vectors, not quoted
  strings.

### 3. TUI auto-offer in `ooo run`

`skills/run/SKILL.md` gains a step at job start:

- Default: offer once — "Want a live dashboard? I can open the TUI in a new
  terminal window" — and remember the answer for the session.
- With `tui_autolaunch: true` in config: launch `ouroboros tui open`
  unconditionally and mention it in one line.
- Either way, the in-session compact progress relay (the current behavior)
  remains the baseline so a run is never a black box even when no window can
  be opened.

### 4. Positioning: core vs `ourocode`

- **Core (`ouroboros`)** owns: the TUI itself, `ouroboros tui open`, the
  breadcrumb contract, and skill-level surfacing. These are the cheap,
  load-bearing transparency primitives.
- **`ourocode` (shell)** owns the bigger bet from #1376's deferred row: the
  always-on cockpit — all configuration visible in one place, few-click
  parity with the TUI, cross-runtime session overview. `ourocode`'s mandate
  is making Ouroboros seamless end-to-end; the core's job here is to keep its
  primitives (event store, status projections, TUI) consumable by that shell.

## Out of scope (deliberately)

- **`ooo config` effective-configuration view and change surfacing** — strong
  candidate for the next slice of #1376, but it has its own design questions
  (config source unification, live-vs-reconnect detection) and deserves its
  own RFC.
- **Token frugality** (#1377) and the estimator mechanisms (#1384, #1385) —
  separate threads.
- **Browser dashboard** — `ourocode` territory per §4.

## Acceptance criteria

1. Every bundled `ooo` skill ends with the breadcrumb footer, and the footer
   reflects live state across a full interview → seed → run → evaluate loop
   (including a second generation, where a linear step counter would lie).
2. `ouroboros tui open` opens the TUI in a new window of the user's own
   terminal on macOS (Ghostty, iTerm2, Apple Terminal verified) and prints a
   copyable manual command on unsupported/headless environments.
3. `ooo run` surfaces the TUI (offer or auto-launch) without weakening the
   in-session progress relay.
4. A run driven from a non-Claude runtime is visible in a TUI opened the same
   way (runtime-agnostic check).
