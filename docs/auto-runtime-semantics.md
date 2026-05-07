<!--
doc_metadata:
  runtime_scope: [all]
-->

# `ooo auto --runtime <backend>`: phase-by-phase semantics

This page documents what `--runtime` actually controls inside `ooo auto`. The
short version: **`--runtime` is the same value for both authoring and
run-handoff**, but the *delivery mechanism* differs by phase. Reading this
before reporting an "interview is too slow" issue saves a round-trip.

## Phase × backend matrix

| Phase | What runs | Where it runs | Knob that picks the path |
|---|---|---|---|
| `INTERVIEW` (`interview.start`, `interview.answer`, `interview.resume`) | Socratic question generation through `InterviewHandler` | **In-process** authoring handler (same Python process as `ooo auto`) | `--runtime` selects the backend the in-process handler talks to. There is **no plugin/subagent dispatch** for the first question, regardless of which backend you pick. |
| `SEED_GENERATION` | `GenerateSeedHandler` | **In-process** authoring handler | Same as above. |
| `REVIEW` / `REPAIR` | `SeedReviewer` + `SeedRepairer` | In-process; backend choice is forwarded so review/repair can call out for analysis | Bound to `state.runtime_backend`. |
| `RUN` (handoff) | `StartExecuteSeedHandler` via `HandlerRunStarter` | Dispatches to the configured runtime; for `opencode` plugin mode this is a true subagent dispatch, for other runtimes this is the executor adapter that owns long-running execution | `--runtime` + (for opencode) `OUROBOROS_OPENCODE_MODE=plugin` |

## What the flag does NOT mean

- `--runtime codex` does **not** mean "Codex picks up the entire pipeline as a single subagent task". The first interview question is still generated in-process by the authoring handler that talks to Codex. If the first call is slow, you see `interview.start timed out after <N>s` in the auto state — not a Codex subagent error.
- `--runtime` does **not** silently change between phases. Resume rejects a runtime mismatch (`resume runtime mismatch: session uses <X>, but --runtime <Y> was requested`).
- `--runtime` does **not** disable the Socratic interview for operational goals (PR URLs, merge intents). Path selection lives in [`auto/operational_task.py`](../src/ouroboros/auto/operational_task.py) and is independent of runtime selection — see #689.

## Reading a blocker through this lens

The `auto_78c98678de5d` incident (`auto-blocked-session.json` fixture) is a
canonical example. The state shows:

```
runtime_backend: codex
last_tool_name: interview.start
last_error: interview.start timed out after 60s for auto_78c98678de5d
```

Reading the table above, this is unambiguously an authoring-side timeout,
not a Codex run-handoff failure: phase `INTERVIEW`, tool `interview.start`,
delivered through the in-process authoring handler that talks to Codex. The
fixes for the incident split into:

- #686 — wire the durable interview-phase timeout (was hardcoded 60s).
- #687 — persist `interview_session_id` before the first question generation
  call returns, so a timeout still leaves a resumable handle.
- #688 — make `Resume:` vs `Retry:` truthful given that handle's presence.
- #689 — give operational goals a direct path so they don't enter the
  authoring loop at all.
- #690 (this doc) — make the meaning of `--runtime` legible so users do not
  expect `--runtime codex` to bypass the authoring handler.

## Pinning current behavior with tests

`tests/unit/auto/test_runtime_semantics.py` pins:

- `--runtime codex` (and any other backend) ends up persisted as
  `state.runtime_backend` and is used by **both** the authoring handler and
  the run-handoff dispatcher.
- Plugin/subagent dispatch in the run handoff is gated on opencode plugin
  mode (`should_dispatch_via_plugin`).

These tests are intentionally observation-grade — they document the current
contract so any future change to the dispatch table is a deliberate edit.
