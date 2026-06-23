# Safe-but-wrong output failure mode

## Summary

A run can be safe, non-destructive, and even useful, while still being wrong for
the user's stated goal.

This document names that failure mode so `ooo auto`, interview, Seed review, and
QA can discuss it directly.

## Definition

A **safe-but-wrong output** happens when the system produces an artifact that is
locally safe and verifiable, but changes a material user-stated requirement.

Common examples:

- user asks for an executable tool, but the run produces only handoff docs;
- user asks for a reusable workflow, but the run produces a one-off checklist;
- user asks for a batch surface, but the run produces a single-case summary;
- user asks to preserve a legacy behavior, but the run creates a new adjacent
  helper that does not cover that behavior;
- missing input data is represented as `OK`, `0`, or empty instead of
  `insufficient data` / `unchecked`.

The key property is that ordinary safety checks may pass. No files were deleted,
no external system was mutated, tests may pass, and the output may be readable.
It is still wrong because the artifact contract drifted.

## Why it matters

Ouroboros focuses on specification-first development. If the Seed or interview
lets a safe-but-wrong artifact class replace the user's goal, later execution can
look successful while solving the wrong problem.

That is worse than a clear blocker because it creates false confidence.

## Detection checklist

Before calling a run complete, compare the produced artifact against the original
contract:

1. **Artifact class** — Did the user ask for code, CLI, web, workflow, document,
   report, dataset, or another specific output type?
2. **Execution surface** — If the user asked for a repeatable tool, can it be run
   again with a new input?
3. **Supporting outputs** — Are checklists, summaries, docs, or handoff packs
   being represented as the final product when they were only supporting outputs?
4. **Missing data semantics** — Are unknowns clearly labeled as missing,
   insufficient, unchecked, or blocked?
5. **User corrections** — Did the user previously correct the same scope or
   artifact assumption?
6. **Verification evidence** — Did verification prove the requested behavior, or
   only prove that some file exists?

## Recommended response

When safe-but-wrong drift is detected, do not continue execution as if the Seed is
valid.

Prefer one of these outcomes:

- **block** if the final artifact class changed without user authority;
- **ask for confirmation** if the user may intentionally want the narrower scope;
- **regenerate the Seed** if the current Seed encoded the wrong artifact;
- **mark the output as supporting material only** if it is useful but not final.

## Example status language

Good:

```text
Generated supporting handoff docs, but the requested CLI/web artifact was not
built. Status: partial/supporting-output, not complete.
```

Bad:

```text
Done. Created the handoff package.
```

## Relationship to other contracts

This failure mode is related to:

- interview convergence;
- intent preservation;
- Seed QA;
- TraceGuard evidence;
- `ooo auto` final status reporting.

It does not require blocking all conservative defaults. A conservative default is
fine when it fills a local reversible gap. It becomes unsafe when it changes what
kind of thing the user asked to receive.
