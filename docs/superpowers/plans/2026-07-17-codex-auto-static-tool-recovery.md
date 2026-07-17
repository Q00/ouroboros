# Codex Auto Static Tool Recovery Implementation Plan

**Goal:** Let an already-open Codex session whose static tool snapshot missed a late Ouroboros MCP registration run the official Auto pipeline without duplicate dispatch, false completion, detached foreground work, or preference drift.

**Root cause:** Codex takes a static tool snapshot when a thread starts. If Ouroboros is registered or refreshed after that snapshot, the current thread cannot call `ouroboros_start_auto` even though the configured MCP server and CLI are healthy. The official foreground CLI is therefore the only in-scope recovery surface for that already-open thread, but its run-handoff boundary must first fail closed.

**Architecture:** Keep native MCP as branch one. Branch two is allowed only when the full required native tool set is demonstrably absent before any native dispatch attempt. Branch two invokes the official foreground `ouroboros auto` command exactly once, never `--no-wait`, and requires a durable `auto_session_id` plus a terminal success or resumable non-zero verdict. Direct CLI state remains RUN/nonterminal while an owned execution job is pending; only authoritative `status=completed` plus `success=true`, or a separately validated durable execution terminal, may promote it to COMPLETE.

## Invariants

- Discovery precedes dispatch. Required native tools are `ouroboros_start_auto`, `ouroboros_job_wait`, and `ouroboros_job_result`.
- If native start was invoked, timed out, or returned an ambiguous transport outcome, CLI fallback is forbidden; reconcile the possible native run instead.
- CLI recovery is one foreground process and one fresh Auto request, marked with `--codex-recovery`. A user-supplied `--no-wait` is rejected.
- Fresh recovery preserves goal as one argv element, resolved cwd, `--runtime codex`, max rounds, skip/complete flags, timeout translation (`pipeline_timeout_seconds` -> `--timeout`), and resolved efficiency/frugality preferences.
- Resume recovery is `ouroboros auto --resume <auto_session_id>` plus presentation-only flags. It must not resend goal, runtime, preferences, timeout, or fresh-only options.
- Resume rejects preference overrides before saving; persisted state must remain byte-for-byte unchanged.
- Foreground missing ownership, unknown handles, waiter errors, nonterminal snapshots, or detached results are blocked/non-zero and resumable when a durable handle exists.
- A completed job is successful only when its execution metadata says `status=completed` and `success is True`, or durable linked execution evidence independently proves completion.
- Fresh direct CLI state is persisted and prints machine-readable `auto_session_id=<id>` before long-running interview/model work.
- Direct CLI handoff remains durable RUN/pending until positive terminal execution evidence. Abrupt process loss cannot leave a handoff-only session as COMPLETE.
- No manual repository work may be presented as an Auto run.

## Task 1 — RED: foreground durability and verdicts

**Files:** `tests/unit/cli/test_auto_run_handoff_wait.py`, `tests/unit/cli/test_auto_command.py`, and focused pipeline tests when required.

- Add RED cases for missing manager/store ownership, unknown job, waiter error, nonterminal/detached return, and non-zero CLI exit.
- Add RED cases for `meta={}`, `status=completed` without `success=true`, malformed success/status, and explicit success.
- Add RED state assertions proving a direct foreground handoff is persisted as RUN before waiting and becomes COMPLETE only after terminal success.
- Add graceful-cancel and subprocess abrupt-termination coverage around handoff persistence; resume must reconcile the same handle and never start a duplicate.
- Add a concurrent resume/recovery assertion that one persisted Auto idempotency key yields at most one run handoff.

## Task 2 — GREEN: direct CLI state machine

**Files:** `src/ouroboros/auto/pipeline.py`, `src/ouroboros/cli/commands/auto.py`, `src/ouroboros/auto/state.py` only if state helpers are needed.

- Add a direct-foreground policy to `AutoPipeline`: after an owned non-complete-product handoff, persist RUN/pending and return the handle rather than prematurely persisting COMPLETE.
- Make the CLI waiter recognize that pending result and fail closed when it cannot prove ownership or terminal success.
- Persist COMPLETE after positive terminal evidence; persist RUN/BLOCKED or FAILED on every other verdict.
- Tighten `_run_meta_verdict` and reconciliation to require both completed status and explicit true success unless durable execution events prove success.
- Emit and persist the Auto session id before constructing or invoking long-running handlers.

## Task 3 — RED/GREEN: preference and argv parity

**Files:** `src/ouroboros/cli/commands/auto.py`, `tests/unit/cli/test_auto_command.py`, `tests/unit/auto/test_surface.py`.

- Add `--efficiency-mode adaptive|quality_first` and `--frugality-assurance off|observe|strict` to direct CLI.
- Test defaults and every resolver combination: default adaptive/observe, adaptive/observe, quality_first/off, explicit quality_first/observe, and explicit strict.
- Test invalid values, Typer forwarding, AutoStore round-trip, status/result rendering, and resume override rejection with unchanged persisted bytes.
- Preserve existing max-round increase behavior, but recovery documentation must use the immutable minimal resume command.

## Task 4 — RED/GREEN: ordered recovery contract

**Files:** `skills/auto/SKILL.md`, `src/ouroboros/codex/ouroboros.md`, `tests/unit/test_codex_artifacts.py`, `tests/unit/auto/test_surface.py`.

- Replace token-presence assertions with an ordered decision matrix: discovery, full native set, native branch, no-fallback-after-attempt, then absent-before-dispatch CLI branch.
- Lock distinct fresh and resume templates; forbid mutable resume args and executable `--no-wait` examples.
- Require active cwd resolution and argv-safe invocation without shell-concatenating the goal.
- Require CLI capability verification; if required options are unavailable, fail closed rather than silently dropping them.
- Keep the manual-emulation prohibition and narrow the old unconditional-stop language to attempted/uncertain native dispatch.

## Task 5 — verification, active artifact refresh, and review

- Run focused RED/GREEN tests after each behavior slice, then the complete CLI/Auto/Codex artifact regression.
- Run a controlled recovery smoke proving: native wins; absent-before-dispatch chooses CLI once; preferences persist; foreground reaches terminal success; blocked paths exit non-zero and are resumable; abrupt restart reuses the same handle; no orphaned process remains.
- The owner-approved patch includes a bounded refresh of the active Codex rule/skill artifacts only. Resolve the active `CODEX_HOME`; refresh only copies owned by that home and the canonical `~/.codex/skills` source. Do not mutate a second home without proving it is active.
- Verify source, installed rule, and installed skill using semantic assertions and SHA-256 hashes. This is artifact synchronization, not a tool install/update.
- Run completion GPT-5.6 Pro insane-review with RED/GREEN output, smoke receipts, hashes, dirty classification, and cleanup evidence. Apply all non-gated findings and repeat until no actionable blocker remains.
- Update the bounded Hanoa handoff/service-impact card only if the actual changed paths meet the common-service routing trigger. Commit/push remain owner-gated.

## Acceptance Matrix

1. Native tool set available -> native starter exactly once; CLI never.
2. Deferred discovery succeeds -> native starter exactly once.
3. Required native tool absent before dispatch -> CLI exactly once.
4. Native attempted with ambiguous outcome -> CLI never.
5. User `--no-wait` -> rejected before start.
6. Fresh argv parity includes goal, cwd, runtime, preferences, timeout, bounds, skip/complete.
7. Shell metacharacters remain one unchanged goal argument.
8. Resume uses only the session handle and presentation-only flags.
9. Every preference resolver combination round-trips through AutoStore.
10. Resume preference rejection leaves the persisted file unchanged.
11. Missing/unowned job manager -> blocked/non-zero.
12. Waiter error -> blocked/non-zero.
13. Completed job with empty metadata -> not success.
14. `status=completed` without `success=true` -> not success.
15. Detached/nonterminal -> not successful foreground completion.
16. Graceful or abrupt loss around handoff persistence cannot leave false COMPLETE.
17. Resume reconciles the existing handle and does not dispatch a duplicate.
18. Concurrent recovery/resume produces at most one run handoff.
19. Installed rule and skill expose the same ordered contract.
20. End-to-end controlled recovery exits only on genuine terminal success or an honest resumable blocker, with no orphan process.
