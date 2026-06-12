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

### Some config is read live, some is construction-bound

The inconsistency is real and unsignalled:

- `orchestrator/adapter.py` captures `self._permission_mode` at construction and
  exposes it as a read-only property. **Construction-bound** — editing
  `~/.ouroboros/config.yaml` does nothing to a live adapter until reconstruction
  (an `/mcp` reconnect).
- `config/loader.py` reads *other* values live from disk (e.g.
  `get_usage_limit_pause_seconds`). **Live.**

So two identical-looking edits behave differently, and nothing tells the user
which is which. `permission_mode: acceptEdits → bypassPermissions` (the fix a
headless run needs) silently requires a reconnect; the mental model is destroyed.

### Config is never proactively surfaced

`config/models.py` + `loader.py` are the single source of truth for ~13 sections
and ~50 env overrides (documented in `docs/config-reference.md`), but Ouroboros
never shows a first-timer the handful of knobs that matter for their runtime, with
defaults and source. `ouroboros config init` generates defaults; `cli/mcp_doctor.py`
exists for MCP diagnostics — a natural home for a coherence check. Nothing today
compares on-disk config against the values a live adapter is actually using.

## Proposal

### 1. A `reload: live | reconnect` classification per field

Tag each config field with a `reload` classification as metadata on the Pydantic
models in `config/models.py` — derived from one place, so it cannot drift from real
binding behavior. A test fails if a `reconnect`-tagged field is actually read live
(and vice-versa).

### 2. Detect-and-warn on stale structural edits

On MCP reconnect / tool entry, diff the on-disk config against the values bound in
the live adapter. If any `reconnect`-class field changed, return a clear, specific
warning:

```
⚠ orchestrator.permission_mode changed on disk (acceptEdits → bypassPermissions)
  but the live session still uses acceptEdits — reconnect with /mcp to apply.
```

Live-class edits produce no warning. The warning annotates by default; it blocks
execution only when the stale field would clearly cause failure (e.g. a headless
run still on `acceptEdits`).

### 3. `ooo config` — one effective-configuration view

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

1. Editing a `reconnect`-class field (e.g. `permission_mode`) without reconnecting
   produces a visible, specific warning the next time a tool runs; a `live`-class
   edit produces none.
2. The `reload` classification is derived from one source, with a test that fails
   if a tag disagrees with real binding behavior.
3. `ooo config` answers "what is the value, where did it come from, and will my
   edit apply" in one view, including runtime-side and env-overridden keys.
4. `ouroboros setup` never reports "complete" when the MCP + runtime/LLM chain is
   unreachable, and names the reconnect when one is required.
