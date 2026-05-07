# `ooo auto` Direct Operational Path

Most `ooo auto` goals are exploratory ideas that benefit from a Socratic
interview before Seed generation.  A growing class of goals is
*operational*: they target an existing artifact (a PR or issue URL) and
ask for a concrete action — *merge*, *review*, *fix*, *close*, *rebase*.
For those goals the interview adds only latency and surface area for
`interview.start` timeouts (see #686, #689).

The **direct operational path** lets `ooo auto` recognize such goals and
short-circuit the interview phase, while keeping every destructive
action behind an explicit policy gate.

---

## Quick start

```bash
# Default: classifier + env gate
export OUROBOROS_AUTO_OPERATIONAL=1
ooo auto 'merge https://github.com/Q00/ouroboros/pull/689 once CI is green'

# Explicit override (skips classifier when ambiguous → blocks with guidance)
ooo auto --interview-strategy=never 'merge https://github.com/Q00/ouroboros/pull/689'

# Explicit fallback to the interview-first flow regardless of goal shape
ooo auto --interview-strategy=always 'merge https://github.com/Q00/ouroboros/pull/689'
```

---

## Architecture

```
goal string
   │
   ▼
goal_classifier      (no IO, deterministic)
   │
   ▼
classification → state.classification + ledger.direct_path_reason
   │
   ▼
pipeline.run() routing
   ├── strategy=always or no env gate ─→ interview-first (current behavior)
   └── classifier OK + opt-in           ─→ operational path
                                                │
                                                ▼
                                  gh_pr_provider.fetch_status()
                                                │
                                                ▼
                                  merge_policy.evaluate_merge()
                                                │
                                  ┌─────────────┴─────────────┐
                                  │                           │
                                  ▼                           ▼
                              allowed                       blocked
                              (PR-F wires                   (audit on ledger,
                              destructive call)             actionable guidance)
```

| Component | File | Owner |
|---|---|---|
| Classifier | `src/ouroboros/auto/goal_classifier.py` | PR-A |
| State / ledger persistence | `src/ouroboros/auto/state.py`, `src/ouroboros/auto/ledger.py` | PR-B |
| Pipeline routing + CLI flag | `src/ouroboros/auto/pipeline.py`, `src/ouroboros/cli/commands/auto.py` | PR-C |
| Merge-policy gate | `src/ouroboros/auto/merge_policy.py` | PR-D |
| `gh` CLI provider | `src/ouroboros/auto/gh_pr_provider.py` | PR-E |
| End-to-end wiring + docs | this file, integration tests | PR-F |

---

## Routing matrix

`--interview-strategy` (CLI) is mirrored by `state.interview_strategy`.
The env var `OUROBOROS_AUTO_OPERATIONAL=1` is the master gate for the
default `auto` strategy: without it set, the classifier is consulted
**advisorily** but no routing change is made.

| Strategy | Env       | Goal           | Outcome |
|----------|-----------|----------------|---------|
| `always` | any       | any            | interview-first (current behavior) |
| `auto`   | unset     | any            | interview-first (current behavior) |
| `auto`   | `=1`      | eligible       | direct path → merge gate → execution or block |
| `auto`   | `=1`      | ineligible     | interview-first (fallback) |
| `never`  | any       | eligible       | direct path → merge gate → execution or block |
| `never`  | any       | ineligible     | BLOCKED with "switch to `--interview-strategy=auto`" |

A goal is **eligible** when `goal_classifier.classify_goal` returns
`direct_run_allowed=True`.  All of these must hold:

- exactly one PR URL **or** exactly one issue URL is present (multiple
  PR/issue URLs make the mutation target ambiguous);
- the operational verb's anchor requirement matches: PR-mutation verbs
  (`merge`, `squash`, `force-push`, `delete-branch`, `close`, `reopen`,
  `rebase`, `fix`, `머지`, `수정`, `개선`) require a PR URL — an Issue
  URL alone is the wrong target type and forces interview;
- read-only verbs (`review`, `comment`, `analyze`, `summarize`, `리뷰`,
  `분석`) work against either a PR or an Issue URL;
- no ambiguous planning verb is present (`plan`, `design`,
  `investigate`, `explore`, `어떻게`).

`/pulls` list URLs paired with a destructive verb are intentionally
**not** eligible — the user must narrow the target through interview
before anything destructive runs.

`GoalClassification.from_dict` enforces the invariant
`interview_required != direct_run_allowed`, so contradictory persisted
records (both True or both False) are refused on load.

The persisted classification is **sticky**: an operator who edits the
goal field on a persisted state file cannot flip the routing decision
on resume.

### Resume contract

Sessions blocked at the routing gate are recoverable.  The CLI flag
`--interview-strategy` takes a sentinel default (omitted leaves the
persisted strategy unchanged), and an explicit value — *including*
`--interview-strategy=auto` — overwrites the persisted strategy on
resume.  This is what makes the blocker guidance ("rerun with
`--interview-strategy=auto`") actually work.

`_recoverable_phase_for_tool("goal_classifier")` maps to
`AutoPhase.INTERVIEW`, so a session blocked from `CREATED` re-enters
the interview path on resume rather than returning the same blocked
result forever.

---

## Merge-policy gate

For destructive actions the gate fails closed on every check that lacks
positive evidence:

| Check | Default | Failure → blocks because |
|---|---|---|
| Classifier consistency | always | `HIGH` risk + `requires_confirmation=False` is treated as a classifier bug |
| Action ↔ classification match | `require_action_classification_match=True` | A LOW-risk goal cannot authorize MERGE/CLOSE/etc.; each action requires a minimum classified risk tier |
| Write permission | always | No push/merge access |
| Target branch | `{main, master}` (overridable via `MergePolicy`) | Branch not allow-listed |
| Draft state | `block_on_draft=True` | PR not ready for review |
| `mergeable=False` | always | Conflicts |
| `mergeable=None` | always | GitHub still computing |
| CI state | `require_passing_ci=True` | Pending or failing (also blocks on a malformed `statusCheckRollup` — non-list / list-of-non-dicts → `UNKNOWN`, never silently `SUCCESS`) |
| Approving reviews (MERGE) | `require_approval=True` | <1 approval or any CHANGES_REQUESTED.  GitHub's aggregate `reviewDecision` is authoritative: a stale `COMMENTED` from an approver does not flip the verdict. |

Each invocation of the gate appends a JSON record to
`SeedDraftLedger.merge_policy_decisions`.  The audit log survives
resume so an operator can see exactly which checks fired without
reproducing the run.

---

## Failure modes

### Classifier says ambiguous

You see the auto session block early with `last_tool_name=goal_classifier`
and a message like *"goal mixes operational verb with ambiguous
planning intent"*.  Re-run with a sharper goal, or use
`--interview-strategy=always` to keep the interview-first path.

### Gate refuses to merge

The session blocks with `last_tool_name=merge_policy` and the ledger's
`merge_policy_decisions[-1]` shows the exact failing checks plus
suggested actions.  Resolve the listed conditions (resolve conflicts,
wait for CI, get an approving review) and re-run with `--resume`.

### `gh` CLI not authenticated

The provider raises `GhProviderError` describing the missing auth.
The auto session blocks with that error in `last_error`.  Run
`gh auth login` and re-run with `--resume`.

---

## Non-goals

- **Auto-merging without authorization.**  Even `--interview-strategy=never`
  cannot bypass the gate — no `mergeable=True` + green CI + approving
  review means no merge.
- **Cross-repo automation.**  The provider expects a fully qualified
  `owner/repo` and a single PR number.  Bulk operations on `/pulls`
  list URLs intentionally route to interview.
- **Replacing the interview-first flow.**  The default for goals that
  don't match the classifier remains the existing Socratic interview.

---

## Cross-references

- Issue: [#689](https://github.com/Q00/ouroboros/issues/689)
- Tracker: [#692](https://github.com/Q00/ouroboros/issues/692)
- Sibling fixes in the incident follow-up: #686, #687, #688, #690, #691
