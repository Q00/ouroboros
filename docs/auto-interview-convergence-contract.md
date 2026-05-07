# Auto-interview convergence contract

`ooo auto` may start from a broad or underspecified goal, but the interview phase
must not converge by accident.  It has to make every required Seed Draft Ledger
section auditable before the pipeline proceeds, or it must stop with a precise
blocker.

This contract is intentionally domain-agnostic.  It describes how the auto
interview behaves for broad benign work, unsafe work, and stalled loops without
encoding prompt-specific defaults.

## Required-section outcomes

Every required ledger section must finish in exactly one of these outcome
classes:

1. **User-provided fact** — the original goal or interview answer directly states
   the value.  These entries are sourced as user facts and should normally be
   `confirmed`.
2. **Bounded repo fact** — the caller supplies already-collected repository
   context, such as runtime/package information with evidence paths.  The
   answerer may use these facts but must not perform unbounded repository or
   network exploration itself.
3. **Safe auto assumption** — the missing detail is local, reversible, and
   non-destructive, so auto mode may choose a conservative default.  The entry
   must be source-tagged as a default/assumption and retain enough rationale for
   review and later repair.
4. **Explicit blocker** — the gap requires human authority, secrets, external or
   destructive side effects, production/billing decisions, regulated-domain
   judgment, or another unsafe choice.  The session must block instead of
   inventing a value.

A session is **converged** only when all required sections are resolved by one of
the first three outcomes and no blocker is present.

## Broad benign goals

For an underspecified but benign goal, such as building a small local tool, auto
mode should keep the scope conservative and steer toward open ledger gaps.  It
may proceed with safe assumptions for actors, IO, non-goals, acceptance criteria,
verification, failure modes, and runtime context when those assumptions are
local, reversible, and observable.

Proceeding does **not** mean merely avoiding `blocked`: the resulting ledger must
include auditable assumptions, non-goals, acceptance criteria, and a verification
plan so seed generation and review can reject weak or untestable work.

## Unsafe or authority-requiring goals

Auto mode must not fill gaps that require human-only authority.  Examples include
real credential values, production deployment authority, destructive data/schema
operations, payment/billing choices, or regulated handling policy.  For those
questions the answerer should emit a blocker and the driver should persist an
actionable blocked state.

## Stalled gap-reduction loops

The interview backend may keep asking generic follow-up questions (`What else?`,
`Any additional context?`) even after auto answers become repetitive.  The driver
must treat unchanged required gaps as a convergence signal:

- steer generic follow-ups toward the highest-priority open gap;
- recognize repeated generic fallback answers as a stall signal that should trigger gap-directed steering;
- when the round cap is reached, report the unresolved sections explicitly.

The blocked reason should name the gaps or unsafe decision, not only say that the
maximum round count was reached.

## Regression coverage

`tests/unit/auto/test_interview_pipeline.py` includes contract tests for:

- broad benign goals resolving via safe assumptions and bounded facts;
- unsafe credential/production-authority questions blocking immediately;
- stalled generic follow-ups producing actionable unresolved-gap blockers.
