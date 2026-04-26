# Phase 2 — Q2: Per-AC Diff Capture (Design)

> Status: **design approved 2026-04-25**, awaiting dogfood run.
> Sibling docs: [`serial-compounding-open-questions.md`](./serial-compounding-open-questions.md), [`../guides/serial-compounding.md`](../guides/serial-compounding.md).
> Phase 1.5 shipped in `ee53eb8`; this is the first Phase-2 work item.

## Why this exists

`ACPostmortem.diff_summary: str = ""` (`level_context.py:510`) is populated as the empty string everywhere today. The postmortem chain carries `files_modified` (reconstructed from Write/Edit events) and `gotchas`, but never *what lines actually changed*. AC-N's downstream prompt sees "AC-2 modified `auth.py`, `middleware.py`" but not "AC-2 added 23 lines to `auth.py` and removed 8 from `middleware.py`."

For compounding, the missing line-level signal blocks two things:

1. **Q4 (inline QA) feedback quality.** A QA verdict that says "the change is incomplete" reads better against a `--stat` than against a bare file list.
2. **Phase-2 prompt-cache fit.** A non-empty `diff_summary` makes the chain section a more cache-friendly stable prefix once the adapter ever supports it.

Phase-1.5 deliberately deferred this (option A in Q2 — "rely on `files_modified` list"). With the chain machinery now stable, we can add the line-level signal without re-architecting anything.

## Decisions (locked)

All four design questions from the brainstorming session resolved. Reasoning lives in this session's transcript; the table is the canonical record.

| # | Question | Decision | Why |
|---|---|---|---|
| 1 | Scope of next dogfood run | **Q2 only** (Q4 + prompt caching deferred) | Smallest unit to validate the loop on non-self-referential work. Prompt caching is blocked by the Claude Code subscription runtime — see saved memory `prompt_caching_blocked.md`. |
| 2 | Per-AC boundary mechanism | **`git stash create`** before and after each AC | Per-AC isolation without polluting commit history or `git stash list`. `stash create` produces an unreferenced commit SHA and modifies nothing. |
| 3 | Diff format + caps | **`git diff --stat <pre> <post>`** truncated to top 20 files + 4KB chars | `--stat` is human-readable and scannable; caps defend against generated-file blowups (lockfiles, schema dumps). Rejected `--shortstat` (overlaps with `files_modified`), patch body (token-budget risk). |
| 4 | Edge cases | **Graceful degradation** — empty `diff_summary` + log on every failure mode (no `.git/`, missing binary, timeout, no-op AC, non-zero exit) | Matches existing executor pattern (`_write_compounding_checkpoint` swallows write errors). Diff capture is a nice-to-have, never a run-blocker. |

## Mechanism

### Per-AC flow

```
1. pre_sha  = git stash create  (in workspace_root, 5s timeout)
2. AC runs (existing serial loop body)
3. post_sha = git stash create
4. if pre_sha == post_sha: diff_summary = ""             # no-op AC
   elif pre_sha is None or post_sha is None: diff_summary = ""   # capture failed
   else:
       raw = git diff --stat <pre_sha> <post_sha>        # 5s timeout
       diff_summary = truncate(raw, file_cap=20, char_cap=4000)
5. ACPostmortem(..., diff_summary=diff_summary)
```

`git stash create` was rejected in the brainstorm doc with "stashes accumulate" — that critique applied to `git stash push`, which writes to `.git/refs/stash`. **`git stash create` produces an unreferenced commit SHA without touching `.git/refs/stash` or the working tree.** Nothing accumulates; `git stash list` stays empty across thousands of ACs.

### Truncation

`--stat` output looks like:

```
src/foo.py                        | 23 ++++++++-------
src/bar.py                        |  8 +++--
tests/test_foo.py                 | 47 +++++++++++++++++
 3 files changed, 65 insertions(+), 13 deletions(-)
```

If file count > 20: keep top 20 by churn (insertions+deletions), append a single line `... and K more files` where `K = total - 20`, then the summary line.

If total chars > 4000 after the file-cap: truncate at 4000 chars and append `[truncated]` marker.

The summary line is always preserved (it's small and authoritative).

### Failure modes (all → empty `diff_summary` + structured log)

| Condition | Log key |
|---|---|
| `workspace_root` has no `.git/` directory | `serial_executor.diff_capture.skipped` with `reason=not_a_git_repo` |
| `git` binary missing (`FileNotFoundError`) | `serial_executor.diff_capture.failed` with `reason=git_binary_missing` |
| `git stash create` non-zero exit | `serial_executor.diff_capture.failed` with `reason=stash_create_exit_<N>` |
| 5s timeout on any subprocess call | `serial_executor.diff_capture.failed` with `reason=timeout` |
| `git diff` non-zero exit | `serial_executor.diff_capture.failed` with `reason=diff_exit_<N>` |
| Pre and post stash SHAs match (no changes) | `serial_executor.diff_capture.no_op` (debug-level, expected case) |

All failures leave the run state untouched — `diff_summary = ""`, AC continues, postmortem records the empty value, chain unchanged otherwise.

## Code shape

### New module: `src/ouroboros/orchestrator/diff_capture.py`

Pure helpers, no executor state:

```python
def capture_pre_ac_snapshot(workspace_root: Path) -> str | None:
    """Run `git stash create` in workspace_root. Return SHA or None on any failure."""

def compute_diff_summary(
    pre_sha: str | None,
    workspace_root: Path,
    *,
    file_cap: int = 20,
    char_budget: int = 4000,
) -> str:
    """Compare pre_sha to a fresh post snapshot, return truncated --stat output.

    Returns "" if pre_sha is None, post snapshot fails, SHAs match (no-op AC),
    or any subprocess call fails / times out.
    """
```

Both helpers are synchronous (called from inside the serial loop, not async). 5s timeout per `subprocess.run` call.

Configuration is read inside the helpers from env vars at call time (consistent with `_get_min_reliability` in `serial_executor.py:92`):

- `OUROBOROS_DIFF_CAPTURE_ENABLED` (default `"true"`) — early-return `""` from `compute_diff_summary` when set to `"false"`/`"0"`/`""`.
- `OUROBOROS_DIFF_SUMMARY_FILE_CAP` (default `20`) — overrides `file_cap` if `compute_diff_summary` was called with the default.
- `OUROBOROS_DIFF_SUMMARY_CHAR_BUDGET` (default `4000`) — overrides `char_budget` similarly.

### `SerialCompoundingExecutor` integration

Insertion points (current line refs in `serial_executor.py` post-Phase-1.5):

- The workspace anchor is `self._task_cwd` (a `str | None`). Coerce to `Path` at the call site: `Path(self._task_cwd or ".")`.
- **Before** each per-AC body execution inside the serial loop: `pre_sha = capture_pre_ac_snapshot(Path(self._task_cwd or "."))`.
- **At each `_build_postmortem_from_result` call site** (currently `serial_executor.py:1156` and `serial_executor.py:1328`): compute `diff_summary = compute_diff_summary(pre_sha, Path(self._task_cwd or ""))` first, then pass it through as a new kwarg.
- **`_build_postmortem_from_result`** is a `@staticmethod` at `serial_executor.py:1546`. Add `diff_summary: str = ""` to its signature and pass through to `ACPostmortem(..., diff_summary=diff_summary)`. Default empty so the recursive sub-postmortem call at `serial_executor.py:1605` (parent's recursion into `result.sub_results`) keeps working without per-sub-AC diff capture (out of scope — top-AC diff already covers the union).

`ParallelACExecutor` is **not** touched. Diff capture is a `SerialCompoundingExecutor`-only behavior. Parallel-mode prompts and event streams stay byte-identical (preserved by existing `test_claude_md_disabled_by_default_preserves_prompt`-style invariants).

## Tests

### Unit tests — `tests/unit/orchestrator/test_diff_capture.py` (new file)

1. `test_capture_pre_ac_snapshot_in_clean_repo_returns_sha` — fresh tmp git repo, returns 40-char SHA.
2. `test_capture_pre_ac_snapshot_with_dirty_worktree_includes_changes` — modify a tracked file, snapshot differs from HEAD's tree SHA.
3. `test_capture_pre_ac_snapshot_outside_git_repo_returns_none` — `tmp_path` with no `.git/`, returns `None`, log emitted.
4. `test_capture_pre_ac_snapshot_no_git_binary_returns_none` — `monkeypatch` `PATH=""`, returns `None`.
5. `test_capture_pre_ac_snapshot_timeout_returns_none` — monkeypatch subprocess to hang, 5s timeout fires.
6. `test_compute_diff_summary_no_changes_returns_empty` — pre and post identical, returns `""`.
7. `test_compute_diff_summary_simple_edit_returns_stat` — modify one file, output contains `--stat` line + summary footer.
8. `test_compute_diff_summary_truncates_at_file_cap` — 30 changed files with `file_cap=20` → output has 20 file lines + `... and 10 more files`.
9. `test_compute_diff_summary_truncates_at_char_budget` — pathologically long output → truncated at 4000 chars + `[truncated]` marker, summary line preserved.
10. `test_compute_diff_summary_disabled_via_env_var` — `OUROBOROS_DIFF_CAPTURE_ENABLED=false` → returns `""` early without subprocess.
11. `test_compute_diff_summary_diff_subprocess_failure_returns_empty` — bad pre_sha → graceful empty.

### Integration tests — extend `tests/unit/orchestrator/test_serial_executor.py`

12. `test_serial_executor_populates_diff_summary_in_postmortem` — fake adapter that writes a file via the events path; assert `ACPostmortem.diff_summary` contains `--stat` output.
13. `test_serial_executor_diff_summary_visible_to_next_ac_prompt` — 2-AC run, AC-2's `context_override` (built from chain) contains AC-1's `diff_summary` substring.
14. `test_serial_executor_no_op_ac_records_empty_diff_summary` — fake adapter that makes no file changes, `diff_summary == ""`.
15. `test_serial_executor_diff_capture_failure_does_not_fail_ac` — monkeypatch `capture_pre_ac_snapshot` to raise, AC still completes successfully with `diff_summary=""`.

### Round-trip preservation

16. `test_diff_summary_round_trips_through_serialize_postmortem_chain` — serialize a chain with non-empty `diff_summary`, deserialize, equality holds. The field already exists in the dataclass and the serializer iterates fields, so this should be a one-assertion confirmation rather than new serialization code.

### Phase-1.5 invariants preserved

- Parallel mode prompt byte-identical (existing `test_parallel_executor` suite).
- `context_override=None` path untouched (existing test).
- Chain artifact written for success/failure/partial runs (Phase-1.5 invariant).
- Sub-postmortem flattening (Q1) still works.
- Invariant verifier (Q3) still works.
- Resume + checkpoint (Q6) still works — `diff_summary` rides existing `serialize_postmortem_chain`.

## Out of scope

- **Patch body capture** (option C from question 3 — `--stat -p`). Defer until a real case demands it; would need its own char-budget reasoning at the chain level.
- **Diff capture in parallel mode.** Compounding-only by design.
- **Cumulative since-session-start diff** alongside per-AC diff. The brainstorm's option D (hybrid). Defer; simplest thing first.
- **Smart change-coalescing** when consecutive ACs touch the same file. Each AC's `diff_summary` is independent; chain rendering already handles N digests.
- **Prompt caching.** Blocked by Claude Code subscription runtime (saved memory `prompt_caching_blocked.md`).

## Effort

- Production: ~120 LOC (`diff_capture.py` ~80, `serial_executor.py` integration ~20, `level_context.py` no change — field already exists, possibly +20 in `_build_postmortem_from_result` plumbing).
- Tests: ~250 LOC (unit + integration as enumerated).
- One AC in the dogfood seed — runs as `ooo run workflow seeds/phase-2-q2-diff-capture.yaml --compounding`.

## References

- Phase-1.5 brainstorm: `docs/brainstorm/serial-compounding-open-questions.md` (Q2 section, lines 47-72)
- Phase-1.5 guide: `docs/guides/serial-compounding.md` (M5 deferred entry)
- Existing `ACPostmortem.diff_summary` field: `src/ouroboros/orchestrator/level_context.py:510`
- Insertion sites: `src/ouroboros/orchestrator/serial_executor.py:1156` and `:1328` (the two existing `_build_postmortem_from_result` callers); method definition at `:1546`
- Subprocess pattern reference: existing helpers in the codebase use `subprocess.run` with explicit `timeout=` and `check=False`; follow that.
- Saved memory: `prompt_caching_blocked.md` (subscription constraint)
