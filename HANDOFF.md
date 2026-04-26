# Handoff Document

> Last Updated: 2026-04-26
> Session: Phase-2 Q2 (per-AC diff capture) shipped + dogfood lessons

---

## Goal

Ship serial-compounding Phase 2 incrementally as dogfood runs. Each work item from the brainstorm doc (`docs/brainstorm/serial-compounding-open-questions.md`) becomes one `ooo run --compounding` cycle: design → seed → run → evaluate → ship.

Phase 1.5 already shipped (PR #3, commit `ee53eb8`). This handoff covers the Q2 cycle that just landed and what's next.

---

## Current Progress

### ✅ Phase-2 Q2 — Per-AC diff capture (PR #4 merged)

**Squash-merged as `ccfc479` on `KeithMoc/ouroboros-loop:main` at 2026-04-26 08:45:35 UTC.**

What shipped:
- New module `src/ouroboros/orchestrator/diff_capture.py` (~440 lines): `capture_pre_ac_snapshot` + `compute_diff_summary` helpers using `git stash create` boundaries around each AC. Truncated `git diff --stat` populated into `ACPostmortem.diff_summary`.
- `SerialCompoundingExecutor` wired to call the helpers per-AC; parallel mode untouched.
- 25 unit tests in new `tests/unit/orchestrator/test_diff_capture.py` + 5 integration tests in `tests/unit/orchestrator/test_serial_executor.py`.
- Test-fixture cleanup: autouse fixture in `tests/conftest.py` redirects `OUROBOROS_CHAIN_ARTIFACT_DIR` to `tmp_path` per test (was leaking ~280 `chain-*.md` files into `docs/brainstorm/` per CI run before this).
- Design doc: `docs/brainstorm/phase-2-q2-diff-capture-design.md`. Dogfood seed: `seeds/phase-2-q2-diff-capture.yaml`.

Test status: **5551 passed, 2 skipped** on `main`.

### Decisions made during this cycle

| # | Decision | Why |
|---|---|---|
| Scope | Q2 only this cycle (Q4 + prompt caching deferred) | First non-self-referential dogfood |
| Boundary | `git stash create` (not commit-per-AC, not session-start ref) | Per-AC isolation, no commit pollution, `stash list` stays empty |
| Format | `git diff --stat` + 20-file / 4 KB caps | Human-readable, defends against generated-file blowups |
| Failures | Empty `diff_summary` + structured log on every git error | Best-effort, never a run-blocker |
| API | `file_cap`/`char_budget=None` consults env, explicit int hard-overrides | Hard-override semantics fixed via CodeRabbit review |
| Filter | Index-based (not set-based) file-cap truncation | Byte-identical rows can't slip past the cap |
| Tight budget | Hard-cap at `char_budget` even when overhead alone busts it | Contract preserved unconditionally |

### CodeRabbit review timeline (PR #4)

3 review passes, all addressed:

1. Pass 1 (`d2b6cf6`) — 1 actionable: `char_budget` cap not enforced when overhead alone busts budget. Fixed in `9672cbb` with 3 regression tests.
2. Pass 2 (`9672cbb`) — clean.
3. Pass 3 (`e96e29a`) — 2 nitpicks: magic-default env-override coupling + set-based duplicate filter. Fixed with 7 regression tests.
4. Pass 4 (`e96e29a` re-review) — clean. Human merge followed.

Reviews replied to inline (`gh api .../comments/<id>/replies`) for inline comments and via `gh pr comment` for review-level findings.

---

## Important Files (Phase-2 Q2)

```
docs/brainstorm/serial-compounding-open-questions.md   # Master Q-list, decisions log
docs/brainstorm/phase-2-q2-diff-capture-design.md      # This cycle's design
docs/guides/serial-compounding.md                      # Living guide (Phase-1.5 + 2 entries)
seeds/phase-2-q2-diff-capture.yaml                     # The seed that drove this run
seeds/phase-1.5-dogfood*.yaml                          # Prior cycle's seeds (kept for reference)

src/ouroboros/orchestrator/diff_capture.py             # New module (Q2 core)
src/ouroboros/orchestrator/serial_executor.py          # Wired at lines 1298–1361, 1585, 1691
src/ouroboros/orchestrator/level_context.py            # ACPostmortem.diff_summary field, render path
tests/unit/orchestrator/test_diff_capture.py           # 25 unit tests
tests/conftest.py                                      # Autouse chain-artifact-dir fixture
```

---

## What Worked

- **Brainstorm-with-skill → seed → ooo run pattern.** Spending a few clarifying questions to lock the four design decisions (boundary, format, caps, failure mode) before writing the seed paid off — the dogfood agent had ~370 lines of LOC + 17 tests shipped in ~20 min and the eval came back APPROVED at 0.88 with no rework on the implementation itself.
- **`git stash create` for boundaries.** Confirmed the brainstorm-doc lean: zero side effects (no commit, no `stash list` entry, working tree untouched). All 30+ tests using real `tmp_path` git repos work.
- **Eating CodeRabbit's review findings via the loop.** Each finding was real (the char_budget overhead bug, the magic-default coupling, the set-based filter) and the fixes were small. Two PR-revision rounds, ~5 min each, total turnaround ~30 min between push and merge.
- **The autouse `OUROBOROS_CHAIN_ARTIFACT_DIR` redirect.** Discovered ~280 leaked artifacts in `docs/brainstorm/` from prior test runs; the autouse fixture in `tests/conftest.py` makes future leaks impossible.
- **Saving project memory for the prompt-caching constraint.** The user runs Claude Code on a subscription, which blocks the prompt-caching adapter rewrite indefinitely. Memory at `prompt_caching_blocked.md` ensures it doesn't get re-proposed.

## What Didn't Work / Open Gaps

- **The MCP `ouroboros_execute_seed` tool doesn't expose a `mode` parameter.** The seed's `metadata.execution_mode_required: "compounding"` is not used by the runner. So this Q2 dogfood run actually executed in **parallel** mode, not compounding — meaning the rolling postmortem chain was never exercised on Q2 itself. Q2's *code* is fully tested via unit + integration tests, but a real end-to-end compounding run via the CLI (`ouroboros run workflow seeds/phase-2-q2-diff-capture.yaml --compounding`) would close the loop on the wiring. Flagged in the PR body.
- **Inline review-level nitpicks aren't reachable via the per-comment reply API.** CodeRabbit puts review-level findings in the review body, not as inline comments. Had to use `gh pr comment` (issue-level comment) instead of `gh api .../comments/<id>/replies`.

---

## Next Steps

### Immediate cleanup (one-shot)

```bash
# Delete the merged feature branch
git branch -D feat/phase-2-q2-diff-capture

# Remove the dogfood worktree + its branch
git worktree remove /home/keith/.ouroboros/worktrees/ouroboros-loop/orch_156fb152019d
git branch -D ooo/orch_156fb152019d
```

### Close the dogfood gap (optional but recommended before Q4)

Run a real compounding execution via the CLI to confirm `diff_summary` actually rides the postmortem chain end-to-end:

```bash
# Need a real multi-AC seed for a meaningful compounding test.
# Option A: re-run Phase-1.5 seeds (they're 4-AC compounding-shaped).
ouroboros run workflow seeds/phase-1.5-dogfood.yaml --compounding --skip-completed seeds/phase-1.5-dogfood.completed-ac123.yaml

# Then inspect the chain artifact:
ls docs/brainstorm/chain-orch_*.md
# Confirm diff_summary entries are non-empty per AC.
```

If the run produces non-empty `diff_summary` entries in the chain artifact, Q2 is fully validated.

### Phase-2 Q4 — Inline QA (next dogfood cycle)

Per the brainstorm doc and decisions log:
- Wire `QAHandler` (`mcp/tools/qa.py:397`) inline at the existing `_build_postmortem_from_result` call sites in `serial_executor.py:1156` / `:1328`.
- Add `--inline-qa` CLI flag (default off — roughly doubles model calls).
- Add separate `--max-qa-retries` counter (default 1) so QA-failure retries don't share the stall-retry budget.
- Estimated ~250 LOC.
- Suggested ordering: **wire QA → add `--inline-qa` flag → add `--max-qa-retries`** as separate ACs in one seed, OR a single AC if the wiring is small enough.

Same workflow:
1. `/superpowers:brainstorming continue with Q4 inline QA via ooo proper workflow`
2. Lock 3-4 design decisions (where exactly to call QA, what to do on REVISE, retry-counter semantics)
3. Write spec + seed at `docs/brainstorm/phase-2-q4-inline-qa-design.md` and `seeds/phase-2-q4-inline-qa.yaml`
4. `ooo run workflow seeds/phase-2-q4-inline-qa.yaml --compounding`
5. `ooo evaluate <session_id>`
6. Squash-merge to `KeithMoc/ouroboros-loop:main`

### Deferred (not blocked)

- **Q5 — `ooo evolve` integration**: end-of-run hint only (option B from brainstorm). Cheap; can land any time.
- **Phase-2 prompt caching**: blocked by Claude Code subscription runtime. See memory `prompt_caching_blocked.md`. Reopen only if the runtime constraint changes.

---

## Repo State at Handoff Time

- Branch: `main` at `ccfc479` (synced with `origin/main`).
- Untracked: `.claude/scheduled_tasks.lock` (transient, ignore).
- Stale local artifacts: `feat/phase-2-q2-diff-capture` branch + `ooo/orch_156fb152019d` branch + the `/home/keith/.ouroboros/worktrees/ouroboros-loop/orch_156fb152019d` worktree directory. All safe to delete (their content is in `ccfc479`).
- A previously-scheduled wakeup (the watch-PR loop) will fire once around 09:01 and exit cleanly when it sees PR #4 in `MERGED` state. Not worth canceling.

---

## Verification Commands

```bash
# Tests
uv run pytest tests/unit/orchestrator/test_diff_capture.py -q
uv run pytest tests/unit/orchestrator/test_serial_executor.py -q
uv run pytest tests/unit/ -q  # full suite — 5551 passed, 2 skipped expected

# Lint
uv run ruff check src/ouroboros/orchestrator/diff_capture.py

# Repo state
git log --oneline -5
git status --short
```

---

*Phase-2 Q2 shipped clean (3 CodeRabbit passes, 2 dev-driven fixes, 7 regression tests, 1 squash merge). Next dogfood cycle: Q4 inline QA.*
