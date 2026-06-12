# RFC — Configuration coherence: surface it, make reload honest

> Status: **Draft**
> Relates to [discussion #1376](https://github.com/Q00/ouroboros/discussions/1376)
> (usability & transparency theme). This is the **next slice** after
> [RFC: journey transparency](./transparency-breadcrumb-tui.md) (the breadcrumb +
> TUI slice, #1392), which deliberately deferred `ooo config` to its own RFC.

## Summary

The most corrosive friction reported in #1376 was not a crash — it was a user
editing config, seeing no effect, and concluding the tool was broken
("*I have already done that twice and still this error is not going away*"). The
owner corroborated it directly: *"we've been bitten by the silent-MCP-reconnect
problem ourselves."* This RFC proposes the config slice of #1376 in two halves:

1. **Detect stale reconnect-bound edits and say so** — when a structural config
   field changes on disk but a live adapter still holds the old value, never let
   the no-op edit pass silently; tell the user a reconnect is required and name the
   field.
2. **Surface the effective configuration proactively** — `ooo config` shows each
   key as **active value · default · what it controls · source** (Ouroboros /
   runtime / env), and a config change shows a diff with a per-key
   **"applies live vs. needs reconnect"** flag.

The detect-and-warn half is cheap and removes the entire confusion loop, so it
ships first. Full live-reload is more invasive and is deferred.

## Context

### Config binds at different points, and nothing says which

The same edit can take effect immediately, on the next tool call, or only after a
reconnect — depending on *where* the field binds. Nothing signals which:

- **Live per read.** `config/loader.py` reads some values from disk on each access
  (e.g. `get_usage_limit_pause_seconds`). An edit applies immediately.
- **Re-read at tool entry.** A *fresh* runtime built during tool handling re-reads
  config on the call — `create_agent_runtime()` resolves `permission_mode` via
  `get_agent_permission_mode()` (`config/loader.py`), so a fresh
  `ouroboros_execute_seed` run already picks up an edited `permission_mode` **without
  a reconnect**.
- **Bound to a long-lived handle.** A value captured when a durable object was
  constructed (e.g. `orchestrator/adapter.py` capturing `self._permission_mode`) is
  *not* re-read by that handle until it is rebuilt. An in-flight session that
  inherited such a handle keeps the old value — this is the case that silently needs
  an `/mcp` reconnect.

So the corrosive bug is specifically the **inherited / long-lived-handle** case:
the *same* field can be re-read on a fresh run yet stale on an in-flight one, and
nothing tells the user which path they are on. A coherence mechanism must therefore
classify by the real binding point, not assume a field is globally "live" or
"reconnect-only."

### Config is never proactively surfaced

`config/models.py` + `loader.py` are the single source of truth for the full config
surface (documented in `docs/config-reference.md`), but Ouroboros never shows a
first-timer the handful of knobs that matter for their runtime, with
defaults and source. `ouroboros config init` generates defaults; `cli/mcp_doctor.py`
exists for MCP diagnostics — a natural home for a coherence check. Nothing today
compares on-disk config against the values a live adapter is actually using.

## Proposal

### 1. A binding-point classification per field

Tag each config field with its **binding point** as metadata on the Pydantic models
in `config/models.py`, derived from one place so it cannot drift from real behavior:

- `live` — re-read on every access (applies immediately);
- `tool_entry` — re-read when a fresh runtime is built per tool call (applies on the
  next fresh run, e.g. `permission_mode`);
- `handle_bound` — captured by a long-lived object until it is rebuilt (can go stale
  on an in-flight session; needs a reconnect).

A test fails if a field's tag disagrees with where it actually binds. Only
`handle_bound` fields can silently go stale; `live` / `tool_entry` fields apply
without a reconnect.

### 2. Detect-and-warn on stale inherited handles

On tool entry, diff the on-disk config against the values bound in any **long-lived
handle** the session inherited. If a `handle_bound` field diverged, warn specifically:

```
⚠ orchestrator.permission_mode on disk (acceptEdits → bypassPermissions) differs from
  the value bound in this session's live adapter — reconnect with /mcp to apply, or
  start a fresh run, which re-reads it at tool entry.
```

A `live` or `tool_entry` field that changed produces no warning (it already applied).
The warning annotates by default; it blocks execution only when the stale field would
clearly cause failure (e.g. an in-flight headless run still on `acceptEdits`).

### 3. `ooo config` — one effective-configuration view

(`ooo config` is the skill/MCP shorthand surfaced in-session; the installed CLI
surface is `ouroboros config`. Both render the same view.)

A single in-session table where every key shows **active value · default · what it
controls · source** (Ouroboros / runtime / env), unifying the Ouroboros sections,
the runtime-side config (`~/.codex/*.config.toml`, `~/.hermes/config.yaml`), and env
overrides. A `--relevant` filter narrows to what the active backend actually uses.
The "what it controls" column reuses `config/models.py` field metadata — no
hand-maintained second list.

### 4. Change surfacing + honest setup

- **On a detected change:** show a diff, the new effective values, and the per-key
  `applies live | needs reconnect` flag (rendering the §1 classification).
- **Honest `setup`:** `ouroboros setup` smoke-checks the **chain** (MCP +
  runtime/LLM reachable) and states plainly when a reconnect is needed and why —
  never reports "complete" when the chain is actually unreachable. Complements the
  install flow map from the journey-transparency slice.

## Out of scope (deliberately)

- **Selective coherent live-reload** — rebuilding construction-bound objects
  (adapter, rate bucket, worker pool) on change instead of requiring a manual
  reconnect. Valuable, but invasive; a later slice. Detect-and-warn first.
- **A config GUI** — terminal output + the existing TUI only. The always-on
  cockpit that shows all settings in one place is `ourocode` (shell) territory, per
  the journey-transparency RFC's positioning.
- Auto-editing runtime config files Ouroboros does not own (`~/.codex`,
  `~/.hermes`) — read-only display by default.

## Acceptance criteria

1. Editing a `handle_bound` field on an in-flight session (e.g. an inherited
   adapter's `permission_mode`) produces a visible, specific warning on the next tool
   call; editing a `live` / `tool_entry` field produces none (it already applied, and
   a fresh run re-reads it).
2. The binding-point classification is derived from one source, with a test that
   fails if a field's tag disagrees with where it actually binds.
3. `ooo config` answers "what is the value, where did it come from, and will my
   edit apply" in one view, including runtime-side and env-overridden keys.
4. `ouroboros setup` never reports "complete" when the MCP + runtime/LLM chain is
   unreachable, and names the reconnect when one is required.
