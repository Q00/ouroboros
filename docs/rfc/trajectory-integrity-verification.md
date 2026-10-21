# RFC: Trajectory Integrity — Verifying the Path, Not Only the Diff

Status: Draft
Author: Ouroboros core (owner-proposed, 2026-09-03)
Scope: `src/ouroboros/orchestrator/parallel_executor.py` (verify gate, settlement),
`src/ouroboros/orchestrator/evidence/harness_observation.py`,
`src/ouroboros/orchestrator/leaf_dispatcher.py`, `src/ouroboros/core/seed.py`
(AC contract), event vocabulary under `execution.ac.*`

## 1. Problem

The deterministic verify gate judges a final workspace state. It cannot say
*who* produced a change or *from where* the work started, and that blindness is
now the dominant cause of failed runs in the field:

1. **Mutation without ownership.** The gate hashes the entire shared cwd around
   `verify_command`; any difference is "the command mutated the workspace".
   Parallel sibling ACs write to the same cwd, so a sibling saving a file during
   another AC's test run rejected that AC with `workspace_mutated`, and settlement
   invalidated every provisional success (0.53.0 field data: ~70% of verify-gate
   rejections). The stop-gap shipped alongside this RFC attributes by *time*
   ("was anyone else writing?") and defers to a quiescent replay; it cannot
   attribute by *ownership*.
2. **Displacement without a baseline.** `artifacts_missing_found_elsewhere` exists
   because workers `cd` into a subdirectory and produce the artifact at a different
   root. Today that is a failure signature we detect after the fact; there is no
   declared reference point ("this AC's work happens under `packages/app/`")
   against which the move could be judged legitimate or not.
3. **The final diff proves what changed, not that the AC worked from the expected
   premise.** An AC that "succeeds" by editing files a sibling owns, or by starting
   from a stale commit, produces a clean final diff and passes. Nothing in the
   evidence chain records the cwd, base commit, files read, commands run, files
   written, and verification result *per step*, linked by workspace hashes.

## 2. What already exists (borrow, don't port)

- **Per-leaf workspace observation.** `snapshot_workspace` / `diff_workspace_snapshots`
  (`evidence/harness_observation.py`) already fingerprint the workspace before and
  after one leaf turn and record `changed_paths`, `deleted_paths`, and harness-run
  `command_runs` with real exit codes. This is one link of the chain; it is not
  yet linked to the previous link nor bound to an AC scope.
- **Verify-gate digests.** `_workspace_content_digest` (Git-ignore aware since this
  PR) plus `_VerifyGateOutcome.workspace_digest` are already durable per AC and
  survive checkpoints; settlement compares them. They are whole-workspace hashes,
  not scoped.
- **Transcript-proven functional evidence** (#2333) already correlates a claimed
  test command with the transcript's `Bash` tool call and exit status.
- **`verify_cwd`** is recorded on `execution.verify.failed` for local debugging.
- **Closed cause vocabularies** (`_VERIFY_GATE_CAUSES`, `RUN_FAILURE_CAUSES`) give
  any new integrity check a place to name its rejection without prose.

## 3. Proposal

### 3.1 Declare the premise: AC scope and reference point

Extend `AcceptanceCriterionSpec` with two optional, deterministic fields:

```yaml
- description: "…"
  verify_command: "…"
  scope: ["src/app/", "tests/test_app.py"]   # paths this AC may create/modify
  workspace_root: "packages/app"             # optional; default "."
```

- `scope` is the AC's **ownership boundary**: workspace-relative path prefixes the
  worker is expected to touch. Absent → unscoped (today's behavior).
- `workspace_root` is the AC's **reference point**: the directory the contract
  (verify_command, expected_artifacts) is evaluated from. A worker moving into
  that directory is not a defect; a worker producing the artifact *outside* it is.

The seed-architect derives both from the interview when the goal names a
sub-project or a component; otherwise leaves them unset. No new LLM call.

### 3.2 Record the trajectory: an append-only, hash-linked step ledger

Emit one durable `execution.ac.step_recorded` event per leaf turn (the unit the
harness already observes), carrying only machine-readable fields:

| field | source | notes |
|---|---|---|
| `ac_index`, `attempt`, `step` | executor | identity |
| `cwd` | leaf dispatcher | workspace-relative |
| `base_commit` | `git rev-parse HEAD` at step start | may be null outside Git |
| `files_read` | transcript `Read` tool calls | paths only |
| `commands_run` | transcript `Bash` tool calls + harness `command_runs` | argv digest + exit |
| `files_written`, `files_deleted` | `diff_workspace_snapshots` | paths only |
| `verify` | verify-gate outcome (if run this step) | cause vocabulary |
| `workspace_before`, `workspace_after` | `_workspace_content_digest` | hash chain |
| `prev_step_hash` | sha256 of previous step's serialized record | append-only link |

Never prompts, file contents, or command output — those stay in the transcript.
The ledger is the same shape for every runtime (adapters only normalize tool
names, as decided for #2333), so a runtime that cannot supply `files_read` emits
an empty list, not a different schema.

### 3.3 Check invariants, don't re-interpret the work

A deterministic `trajectory_integrity` check runs at the same three points the
verify gate does (per attempt, post-coordinator, settlement) and answers three
questions with closed causes:

| invariant | cause on violation |
|---|---|
| Started from an allowed reference point: step 1 `base_commit` equals the level's start commit (or the parent AC's end commit) and `cwd` resolves under `workspace_root`. | `trajectory_stale_base`, `trajectory_root_escaped` |
| Followed only declared paths: every `files_written`/`files_deleted` entry is under `scope` (when declared); every step's `workspace_before` equals the previous step's `workspace_after` **restricted to `scope`** — changes outside the scope between steps are siblings' and are ignored. | `trajectory_scope_violation` |
| Reached the final state through the recorded path: the chain of `prev_step_hash` is unbroken and the last `workspace_after` restricted to `scope` equals the settlement digest restricted to `scope`. | `trajectory_chain_broken` |

The important consequence for the problem in §1.1: with `scope` declared, the
verify gate's mutation check becomes **scoped** — a change outside the AC's scope
during its verify window is a sibling's by construction, and a change *inside*
it is the command's. Attribution by ownership replaces attribution by time.

### 3.4 Directory moves become a judged invariant, not a failure

`artifacts_missing_found_elsewhere` is subsumed: an artifact produced under
`workspace_root` is at the contract path (the gate evaluates from that root); an
artifact produced elsewhere violates `trajectory_root_escaped` with the actual
root recorded in the step ledger, which the retry hint can quote precisely
("you produced `dist/report.md` under `packages/web/`; this AC's root is
`packages/app/`").

## 4. Non-goals

- Replacing the evaluate stage's semantic judgment. Integrity says the work was
  done from the right place along declared paths; it does not say the work is good.
- Recording content. The ledger is paths, digests, argv digests, and exit codes.
- Per-AC worktrees. Ownership is declared and checked; isolation is a separate,
  costlier design (worktree-per-AC) that this RFC makes unnecessary for the
  common case.

## 5. Rollout

1. **Observe-only.** Emit `execution.ac.step_recorded` and compute the three
   invariants without acting on them; surface violations on the existing
   `ac_verify_failed`-style telemetry (closed causes) and in `ooo status`.
2. **Scoped mutation check.** When an AC declares `scope`, the verify gate uses
   the scoped digest; the time-based deferral from this PR remains the fallback
   for unscoped ACs.
3. **Enforce.** Violations reject the attempt with the trajectory causes and a
   precise retry hint. One release after step 2, same cadence as the verify-gate
   evidential-force stack (#2180–#2182).

## 6. Open questions for the maintainer

- Is `scope` authored by the seed-architect (LLM-derived from the interview) or
  inferred by the dependency analyzer from the goal's file references? Authored
  is auditable; inferred needs no schema change for existing seeds.
- Should `workspace_root` be allowed to differ across ACs of one seed, or is one
  root per seed enough for the first version?
- The step ledger doubles event volume per leaf turn. Keep it in the event store
  (queryable, replayable) or in a per-execution sidecar file (cheaper, not
  replayable through `ouroboros_query_events`)?
