<!--
doc_metadata:
  runtime_scope: [all]
-->

# Execution vs. Evaluation Contract

Ouroboros keeps worker execution outcomes separate from formal acceptance-criterion verdicts.

This distinction is part of the Agent OS runtime contract:

> **Execution is not evaluation. Completion is not approval. Task failure is not semantic drift.**

## Why this contract exists

A Seed acceptance criterion can produce at least two different runtime artifacts:

1. A **worker task** derived from the AC. This task can complete, fail, block, or be skipped while the worker is trying to produce an artifact.
2. A **formal AC verdict** produced by an evaluator or verifier. This verdict decides whether the resulting artifact satisfies the acceptance criterion.

Those two states answer different questions.

| Question | Contract surface | Example states |
|---|---|---|
| Did the worker finish the assigned execution unit? | `TaskResult` / task progress | `completed`, `failed`, `blocked`, `skipped` |
| Did the artifact satisfy the acceptance criterion? | `ACResult` / evaluator verdict | `pass`, `fail`, `not_evaluated` |
| Did the artifact drift from the Seed intent? | semantic evaluation / drift signal | `drift_score`, drift alerts |

A completed task is only evidence that execution finished. It is not proof that the AC passed.
A failed task is only evidence that execution did not finish. It is not proof of semantic drift.

## Terminology rules

Use these terms consistently in reports, TUI surfaces, events, and docs.

| Use this | For | Do not use it for |
|---|---|---|
| **task** / **subtask** | Worker execution units derived from ACs | Formal acceptance verdicts |
| **completed** / **failed** | Worker execution outcomes | Evaluator approval |
| **acceptance criterion** / **AC** | User-facing success criteria | Internal worker task status |
| **pass** / **fail** | Formal evaluator or verifier verdicts | Worker completion status |
| **drift** / `drift_score` | Semantic divergence from Seed intent | Mechanical task completion ratio |

## Data model boundary

The shared model boundary should keep these surfaces separate:

- `TaskResult` records worker execution outcomes.
- `ACResult` records formal evaluator/verifier verdicts.
- `EvaluationSummary.task_results` contains execution outcomes.
- `EvaluationSummary.ac_results` contains formal AC verdicts.
- `EvaluationSummary.drift_score` is set only when semantic drift evaluation actually ran.

A cheap mechanical execution-completion ratio may be useful as `score`, but it must not synthesize `drift_score`.

## Legacy report compatibility

Older parallel execution reports may render worker completion as:

```text
### AC 1: [PASS] Implement feature
### AC 2: [FAIL] Add tests
```

That legacy syntax remains parseable for backward compatibility, but it should be interpreted as worker task completion:

```text
Task 1: completed
Task 2: failed
```

It should not directly populate formal `ACResult` verdicts.

Formal AC verdicts should come from explicit evaluation or verification. If a verifier only covers a subset of expected ACs, the missing ACs are not approved. They remain rejected or `not_evaluated` until a formal verdict exists.

## Reporting guidance

Execution surfaces should prefer task language:

```text
Task Execution Progress: 2/3 completed
Task 1: completed
Task 2: failed
Task 3: completed
```

Evaluation surfaces should prefer AC verdict language:

```text
AC Verdicts: 2/3 passed
AC 1: pass
AC 2: fail
AC 3: pass
```

Combined summaries should show both dimensions instead of collapsing them:

```text
Execution: completed
Evaluation: not_evaluated
Run verdict: FAIL
```

or:

```text
Execution: completed
Evaluation: approved
Run verdict: PASS
```

## Migration guidance for maintainers

When migrating a surface that currently uses AC pass/fail wording for worker execution:

1. Keep legacy parsing backward-compatible.
2. Change newly generated worker reports to task/subtask wording.
3. Store worker outcomes in `task_results` or equivalent.
4. Reserve `ac_results` for formal evaluator/verifier output.
5. Do not set `drift_score` unless semantic drift evaluation ran.
6. Add tests showing task completion does not imply AC approval.
7. Add tests showing partial verifier coverage cannot approve a run.

## Related work

- #608 tracks the repository-wide contract split.
- #613 starts the model and MCP-adapter boundary migration.
- Follow-up PRs should migrate UI/HUD/job-monitor/event/docs terminology without breaking legacy report parsing.
